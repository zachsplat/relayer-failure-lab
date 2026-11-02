"""Helpers shared by the scenario drivers: Relayer API, anvil RPC, proxy counters, result file."""
import json
import os
import time

import httpx

RELAYER = os.environ.get("RELAYER_URL", "http://relayer:8080")
API_KEY = os.environ["API_KEY"]
ANVIL = os.environ.get("ANVIL_URL", "http://anvil:8545")
PROXY = os.environ.get("PROXY_URL", "http://proxy:8546")
RID = os.environ.get("RELAYER_ID", "lab")
RESULT = os.environ.get("RESULT", "/work/result.json")
DRIVER_LOG = os.environ.get("DRIVER_LOG", "/work/logs/driver.log")

# anvil's public development accounts; #0 is the Relayer's signer
SENDER = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
ACCOUNT_1 = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
ACCOUNT_2 = "0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC"
ACCOUNT_9 = "0xa0Ee7A142d267C1f36714E4a8F75612F20a79720"

_log = open(DRIVER_LOG, "a")


def log(msg):
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}"
    print(line, flush=True)
    _log.write(line + "\n")
    _log.flush()


def rpc(method, params=None):
    j = httpx.post(ANVIL, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}, timeout=30).json()
    if "error" in j:
        raise RuntimeError(f"{method}: {j['error']}")
    return j["result"]


def api(method, path, body=None):
    r = httpx.request(method, RELAYER + path, headers={"Authorization": f"Bearer {API_KEY}"}, json=body, timeout=30)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"raw": r.text[:300]}


def submit_transfer(to=ACCOUNT_1, value=1, speed="fast"):
    return api("POST", f"/api/v1/relayers/{RID}/transactions",
               {"to": to, "value": value, "data": "0x", "speed": speed, "gas_limit": 21000})


def tx(tx_id):
    return api("GET", f"/api/v1/relayers/{RID}/transactions/{tx_id}")[1].get("data") or {}


def list_txs():
    items, page = [], 1
    while page <= 50:
        _, j = api("GET", f"/api/v1/relayers/{RID}/transactions?page={page}&per_page=100")
        data = j.get("data") or []
        items += data
        if len(data) < 100:
            break
        page += 1
    return items


def on_chain_nonce(addr=SENDER):
    return int(rpc("eth_getTransactionCount", [addr, "latest"]), 16)


def proxy_stats():
    try:
        return httpx.get(PROXY + "/stats", timeout=5).json()
    except Exception as e:
        return {"error": str(e)}


def wait_ready(timeout=240):
    """Wait for the API to answer and the lab relayer to be listed, then let the workers settle."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if httpx.get(RELAYER + "/api/v1/health", timeout=5).status_code == 200:
                code, j = api("GET", f"/api/v1/relayers/{RID}")
                if code == 200:
                    log(f"relayer API ready after {int(time.time() - t0)} s; relayer {RID} address {(j.get('data') or {}).get('address')}")
                    time.sleep(15)
                    return
        except httpx.HTTPError:
            pass
        time.sleep(2)
    raise SystemExit(f"relayer API not ready after {timeout} s")


def write_result(outcome, summary, observed):
    assert outcome in ("reproduces", "fixed", "inconclusive")
    with open(RESULT, "w") as f:
        json.dump({"outcome": outcome, "summary": summary, "observed": observed}, f, indent=1)
    log(f"=== OUTCOME: {outcome}: {summary}")
