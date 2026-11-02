"""Scenario 817: a mined transaction is finalised as Failed after a stale receipt read.

https://github.com/OpenZeppelin/openzeppelin-relayer/issues/817

Setup: anvil mines a block every 2 s; the Relayer reaches it only through the
fault proxy, which answers null to eth_getTransactionReceipt for 420 s after a
receipt first exists, while nonce reads pass through. That is longer than the
3 minute resubmit timeout of speed "fast", so the Relayer resubmits, anvil
answers "nonce too low" because the original mined, and the Relayer's nonce
reconciliation decides what the transaction became.

Outcome:
  reproduces  the Relayer marks the transaction failed (or expired) while anvil
              holds a receipt with status 1 for one of its hashes
  fixed       the Relayer reports it mined or confirmed
  inconclusive  neither within TIMEOUT seconds
"""
import os
import time

from labkit import ACCOUNT_1, SENDER, log, on_chain_nonce, proxy_stats, rpc, submit_transfer, tx, wait_ready, write_result

TIMEOUT = int(os.environ.get("TIMEOUT", "900"))


def receipt(h):
    r = rpc("eth_getTransactionReceipt", [h])
    return None if r is None else {"status": int(r["status"], 16), "block": int(r["blockNumber"], 16)}


def main():
    wait_ready()
    log(f"anvil block {int(rpc('eth_blockNumber'), 16)}, sender nonce {on_chain_nonce()}")
    code, j = submit_transfer(ACCOUNT_1, 1, "fast")
    log(f"submit -> HTTP {code}")
    tx_id = j["data"]["id"]
    t0 = time.time()
    mined = None
    last = None
    final = None
    while time.time() - t0 < TIMEOUT:
        d = tx(tx_id)
        hashes = [h for h in ([d.get("hash")] + (d.get("hashes") or [])) if h]
        if mined is None:
            for h in dict.fromkeys(hashes):
                rc = receipt(h)
                if rc:
                    mined = {"hash": h, **rc, "seconds_after_submit": int(time.time() - t0), "sender_nonce_after": on_chain_nonce(SENDER)}
                    log(f"anvil: {h[:14]}.. mined in block {rc['block']} with status {rc['status']} at t+{mined['seconds_after_submit']} s")
        view = (d.get("status"), d.get("hash"), len(d.get("hashes") or []), d.get("status_reason"))
        if view != last:
            log(f"relayer t+{int(time.time() - t0)} s: status={view[0]} hash={(view[1] or '')[:14]} hashes={view[2]} reason={view[3]}")
            last = view
        if d.get("status") in ("failed", "expired", "canceled", "mined", "confirmed"):
            final = d
            break
        time.sleep(10)
    d = final or tx(tx_id)
    status = d.get("status")
    observed = {
        "final_status": status,
        "status_reason": d.get("status_reason"),
        "nonce": d.get("nonce"),
        "hashes_recorded": len(set([d.get("hash")] + (d.get("hashes") or [])) - {None}),
        "anvil_receipt": mined,
        "seconds_to_final": int(time.time() - t0),
        "proxy": {k: v for k, v in proxy_stats().items() if k in ("receipt_calls", "receipt_hidden", "receipt_passed", "send_raw", "send_ok", "send_other_error", "send_error_messages")},
    }
    if status in ("failed", "expired") and mined and mined["status"] == 1:
        write_result("reproduces", f"Relayer finalised the transaction as {status} ({d.get('status_reason')}) although anvil mined it with status 1 in block {mined['block']}", observed)
    elif status in ("mined", "confirmed"):
        write_result("fixed", f"Relayer reported the transaction {status} after {observed['seconds_to_final']} s despite the stale receipt reads", observed)
    else:
        write_result("inconclusive", f"final status {status} after {observed['seconds_to_final']} s; anvil receipt {mined}", observed)


if __name__ == "__main__":
    main()
