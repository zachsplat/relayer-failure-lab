"""Fault-injecting JSON-RPC proxy between the Relayer and anvil.

Every call is forwarded to UPSTREAM unchanged unless one of these is switched on:

HIDE_RECEIPT_SECS (seconds, 0 = off)
    eth_getTransactionReceipt answers null for a hash until that many seconds
    have passed since the proxy first saw a real receipt for it. Nonce reads
    are never delayed, so the Relayer sees the advanced nonce while the receipt
    is still "missing": the read inconsistency of a load-balanced provider
    pool (#817).
STRICT_NONCE (1 = on)
    eth_sendRawTransaction whose nonce is above the sender's pending nonce is
    rejected with the JSON-RPC error "nonce too high" instead of being queued,
    as a rollup without a public mempool does (#843).
POST /control {"fail_send_for": N}
    For the next N seconds every eth_sendRawTransaction gets HTTP 503, as a
    flaky provider would answer (#843).

GET /stats returns the counters as JSON. Logs go to PROXY_LOG.
"""
import json
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import rlp

UPSTREAM = os.environ.get("UPSTREAM", "http://anvil:8545")
HIDE_RECEIPT_SECS = float(os.environ.get("HIDE_RECEIPT_SECS", "0"))
STRICT_NONCE = os.environ.get("STRICT_NONCE", "0") == "1"
SENDER = os.environ.get("SENDER", "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266")
LOG = os.environ.get("PROXY_LOG", "/work/logs/proxy.log")
PORT = int(os.environ.get("PORT", "8546"))

lock = threading.Lock()
first_seen = {}
state = {"fail_send_until": 0.0}
stats = {
    "requests": 0, "receipt_calls": 0, "receipt_hidden": 0, "receipt_passed": 0, "nonce_calls": 0,
    "send_raw": 0, "send_ok": 0, "send_503": 0, "send_nonce_too_high": 0, "send_other_error": 0,
    "send_error_messages": [],
}


class TransportError(Exception):
    pass


def log(msg):
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def forward(body: bytes) -> bytes:
    req = urllib.request.Request(UPSTREAM, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def call(method, params):
    return json.loads(forward(json.dumps({"jsonrpc": "2.0", "id": 0, "method": method, "params": params}).encode()))["result"]


def raw_nonce(raw_hex: str) -> int:
    raw = bytes.fromhex(raw_hex[2:] if raw_hex.startswith("0x") else raw_hex)
    if raw[0] <= 0x7F:  # typed transaction: type byte, then an RLP list with the nonce at index 1
        field = rlp.decode(raw[1:])[1]
    else:  # legacy transaction: nonce at index 0
        field = rlp.decode(raw)[0]
    return int.from_bytes(field, "big") if field else 0


def note_send_error(msg):
    with lock:
        stats["send_other_error"] += 1
        if len(stats["send_error_messages"]) < 20:
            stats["send_error_messages"].append(str(msg)[:160])
    log(f"eth_sendRawTransaction error from upstream: {msg}")


def handle_one(obj):
    method = obj.get("method")
    with lock:
        stats["requests"] += 1
    if method == "eth_sendRawTransaction":
        with lock:
            stats["send_raw"] += 1
            failing = time.time() < state["fail_send_until"]
        if failing:
            with lock:
                stats["send_503"] += 1
            raise TransportError()
        if STRICT_NONCE:
            try:
                nonce = raw_nonce(obj["params"][0])
            except Exception as e:  # an undecodable payload goes upstream and fails there
                log(f"nonce decode failed: {e!r}")
                nonce = None
            if nonce is not None:
                pending = int(call("eth_getTransactionCount", [SENDER, "pending"]), 16)
                if nonce > pending:
                    with lock:
                        stats["send_nonce_too_high"] += 1
                        n = stats["send_nonce_too_high"]
                    if n in (1, 2, 5, 10, 25, 50, 100, 200, 400, 800, 1600, 3200):
                        log(f"strict nonce: rejected nonce {nonce} (pending {pending}); rejections so far {n}")
                    return {"jsonrpc": "2.0", "id": obj.get("id"), "error": {"code": -32000, "message": "nonce too high"}}
        resp = json.loads(forward(json.dumps(obj).encode()))
        if "error" in resp:
            note_send_error(resp["error"].get("message"))
        else:
            with lock:
                stats["send_ok"] += 1
            log(f"eth_sendRawTransaction ok: {str(resp.get('result'))[:18]}..")
        return resp
    resp = json.loads(forward(json.dumps(obj).encode()))
    if method == "eth_getTransactionReceipt" and HIDE_RECEIPT_SECS > 0:
        h = (obj.get("params") or [""])[0]
        with lock:
            stats["receipt_calls"] += 1
            if resp.get("result") is not None:
                age = time.time() - first_seen.setdefault(h, time.time())
                if age < HIDE_RECEIPT_SECS:
                    stats["receipt_hidden"] += 1
                    hidden = stats["receipt_hidden"]
                    resp["result"] = None
                    if hidden in (1, 2, 5, 10, 20, 50):
                        log(f"hiding receipt for {h[:14]}.. (on anvil for {int(age)} s, hidden until {int(HIDE_RECEIPT_SECS)} s)")
                else:
                    stats["receipt_passed"] += 1
                    if stats["receipt_passed"] == 1:
                        log(f"receipt for {h[:14]}.. now passed through")
    elif method == "eth_getTransactionCount":
        with lock:
            stats["nonce_calls"] += 1
    return resp


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, data: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        with lock:
            body = json.dumps({**stats, "failing_now": time.time() < state["fail_send_until"]}).encode()
        self._send(200, body)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path.startswith("/control"):
            secs = float(json.loads(body).get("fail_send_for", 0))
            with lock:
                state["fail_send_until"] = time.time() + secs
            log(f"control: eth_sendRawTransaction answers HTTP 503 for the next {secs:g} s")
            self._send(200, b'{"ok": true}')
            return
        try:
            obj = json.loads(body)
            out = [handle_one(o) for o in obj] if isinstance(obj, list) else handle_one(obj)
            self._send(200, json.dumps(out).encode())
        except TransportError:
            self._send(503, b"proxy: simulated provider outage", "text/plain")
        except Exception as e:
            log(f"proxy error: {e!r}")
            self._send(200, json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": f"proxy: {e}"}}).encode())

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    log(f"faultproxy up: upstream {UPSTREAM}, hide receipts {HIDE_RECEIPT_SECS:g} s, strict nonce {STRICT_NONCE}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
