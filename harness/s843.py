"""Scenario 843: a "nonce too high" jam that the Relayer's nonce recovery never clears.

https://github.com/OpenZeppelin/openzeppelin-relayer/issues/843

Setup: anvil mines a block every 2 s; the Relayer reaches it through the fault
proxy with strict nonces on (a raw transaction above the sender's pending nonce
is rejected "nonce too high", as on Arbitrum), and the network carries the
tags of the Relayer's arbitrum-one definition. One provider, no failover. The driver submits one transfer every PERIOD s for TOTAL
s. WARMUP s in, the proxy answers HTTP 503 to every eth_sendRawTransaction for
OUTAGE s. Submit jobs that hit the outage are not retried, so their nonces stay
occupied by payloads the chain never saw, and every later transaction is
rejected "nonce too high". Every 30 s the driver samples the drift: the
Relayer's next nonce minus the sender's on-chain nonce. After intake stops it
keeps sampling for up to POST s to see whether the queue drains.

Outcome, judged on the drift samples in the last OBSERVE s of intake:
  reproduces  the drift never falls below JAM (10) while transactions keep arriving
  fixed       the drift never rises above 2
  inconclusive  anything in between
"""
import json
import os
import time

import httpx

from labkit import ACCOUNT_1, PROXY, list_txs, log, on_chain_nonce, proxy_stats, submit_transfer, wait_ready, write_result

PERIOD = float(os.environ.get("PERIOD", "2"))
WARMUP = int(os.environ.get("WARMUP", "40"))
OUTAGE = int(os.environ.get("OUTAGE", "60"))
TOTAL = int(os.environ.get("TOTAL", "720"))
OBSERVE = int(os.environ.get("OBSERVE", "300"))
POST = int(os.environ.get("POST", "240"))
JAM = int(os.environ.get("JAM", "10"))


def relayer_view():
    items = list_txs()
    hist, top, sent_null = {}, -1, 0
    for t in items:
        s = t.get("status")
        hist[s] = hist.get(s, 0) + 1
        if isinstance(t.get("nonce"), int):
            top = max(top, t["nonce"])
        if s == "sent" and t.get("sent_at") in (None, ""):
            sent_null += 1
    return top + 1, hist, sent_null, len(items)


def sample(t_rel, phase):
    oc = on_chain_nonce()
    nxt, hist, sent_null, n = relayer_view()
    drift = nxt - oc if n else 0
    log(f"{phase} t+{t_rel} s: on-chain nonce {oc}, relayer next nonce {nxt}, drift {drift}, statuses {json.dumps(hist)}, sent with null sent_at {sent_null}")
    return {"t": t_rel, "phase": phase, "on_chain_nonce": oc, "relayer_next_nonce": nxt, "drift": drift, "statuses": hist, "sent_null_sent_at": sent_null}


def main():
    wait_ready()
    log(f"intake: one transfer every {PERIOD:g} s for {TOTAL} s; send outage {OUTAGE} s from t+{WARMUP} s")
    t0 = time.time()
    submitted, rejected = 0, 0
    outage_opened = False
    samples = []
    next_submit, next_sample = t0, t0 + 30
    while time.time() - t0 < TOTAL:
        now = time.time()
        if not outage_opened and now - t0 >= WARMUP:
            httpx.post(PROXY + "/control", json={"fail_send_for": OUTAGE}, timeout=5)
            outage_opened = True
            log(f"--- outage opened: eth_sendRawTransaction answers HTTP 503 for {OUTAGE} s")
        if now >= next_submit:
            code, _ = submit_transfer(ACCOUNT_1, 1, "fast")
            submitted += 1
            if code not in (200, 201):
                rejected += 1
            next_submit += PERIOD
        if now >= next_sample:
            samples.append(sample(int(now - t0), "intake"))
            next_sample += 30
        time.sleep(0.2)
    log(f"--- intake stopped after {submitted} submissions ({rejected} refused by the API)")
    t1 = time.time()
    drained_after = None
    while time.time() - t1 < POST:
        time.sleep(30)
        s = sample(int(time.time() - t0), "after intake")
        samples.append(s)
        if s["drift"] == 0 and not (s["statuses"].get("sent") or s["statuses"].get("submitted") or s["statuses"].get("pending")):
            drained_after = int(time.time() - t1)
            break
    tail = [s["drift"] for s in samples if s["phase"] == "intake" and s["t"] >= TOTAL - OBSERVE]
    nxt, hist, sent_null, n = relayer_view()
    stats = proxy_stats()
    observed = {
        "submitted": submitted,
        "refused_by_api": rejected,
        "drift_last_window": tail,
        "drift_min_last_window": min(tail) if tail else None,
        "drift_max_last_window": max(tail) if tail else None,
        "queue_empty_seconds_after_intake": drained_after,
        "final_statuses": hist,
        "final_sent_null_sent_at": sent_null,
        "proxy": {k: stats.get(k) for k in ("send_raw", "send_ok", "send_503", "send_nonce_too_high", "send_other_error")},
        "samples": samples,
    }
    if not tail:
        write_result("inconclusive", "no drift samples in the observation window", observed)
    elif min(tail) >= JAM:
        write_result("reproduces", f"drift stayed between {min(tail)} and {max(tail)} over the last {OBSERVE} s of intake after a {OUTAGE} s send outage; queue empty {drained_after if drained_after is not None else 'not within ' + str(POST)} s after intake stopped", observed)
    elif max(tail) <= 2:
        write_result("fixed", f"drift between {min(tail)} and {max(tail)} over the last {OBSERVE} s of intake after a {OUTAGE} s send outage", observed)
    else:
        write_result("inconclusive", f"drift between {min(tail)} and {max(tail)} over the last {OBSERVE} s of intake", observed)


if __name__ == "__main__":
    main()
