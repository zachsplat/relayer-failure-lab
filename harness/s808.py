"""Scenario 808: gas_price_cap leaves a transaction stuck after a gas spike.

https://github.com/OpenZeppelin/openzeppelin-relayer/issues/808

Setup: anvil with mining off, so the driver sets the base fee and mines each
block. The relayer has gas_price_cap 0.8 gwei. With the base fee at 0.1 gwei a
"fast" transfer is priced at the cap and broadcast. The base fee then goes to
5 gwei for 90 s, every hash the relayer knows is dropped from anvil's pool (the
mempool eviction a real spike causes), and the base fee returns to 0.1 gwei,
one eighth of the cap, with a block every 5 s for RECOVERY seconds. The proxy
in front of anvil only counts broadcasts.

Outcome:
  reproduces  after the recovery window the transfer is still not mined, the
              sender's nonce has not moved and the Relayer made no broadcast
              during the recovery window
  fixed       the transfer is mined or confirmed during the recovery window
  inconclusive  anything else (for example a terminal status)
"""
import os
import time

from labkit import ACCOUNT_1, log, on_chain_nonce, proxy_stats, rpc, submit_transfer, tx, wait_ready, write_result

CAP = 800_000_000
LOW_BASE = 100_000_000
SPIKE_BASE = 5_000_000_000
SPIKE_SECS = int(os.environ.get("SPIKE_SECS", "90"))
RECOVERY = int(os.environ.get("RECOVERY", "1200"))


def mine(base):
    rpc("anvil_setNextBlockBaseFeePerGas", [hex(base)])
    rpc("evm_mine")


def pool():
    p = rpc("txpool_content")
    return sum(len(v) for v in p.get("pending", {}).values()), sum(len(v) for v in p.get("queued", {}).values())


def main():
    wait_ready()
    for _ in range(2):
        mine(LOW_BASE)
    nonce0 = on_chain_nonce()
    log(f"base fee {LOW_BASE} wei, gas_price_cap {CAP} wei, sender nonce {nonce0}")
    code, j = submit_transfer(ACCOUNT_1, 1, "fast")
    log(f"submit -> HTTP {code}")
    tx_id = j["data"]["id"]
    for _ in range(40):
        d = tx(tx_id)
        if d.get("hash"):
            break
        time.sleep(3)
    first = {"status": d.get("status"), "max_fee_per_gas": d.get("max_fee_per_gas"), "max_priority_fee_per_gas": d.get("max_priority_fee_per_gas")}
    log(f"broadcast: {first}; anvil pool pending/queued {pool()}")

    log(f"--- spike: base fee {SPIKE_BASE} wei for {SPIKE_SECS} s")
    t0 = time.time()
    while time.time() - t0 < SPIKE_SECS:
        mine(SPIKE_BASE)
        time.sleep(6)
    d = tx(tx_id)
    for h in set(d.get("hashes") or []) | ({d["hash"]} if d.get("hash") else set()):
        rpc("anvil_dropTransaction", [h])
    log(f"evicted the relayer's hashes; pool {pool()}; relayer status {d.get('status')}")

    sends_before = proxy_stats().get("send_raw", 0)
    log(f"--- recovery: base fee {LOW_BASE} wei, a block every 5 s for up to {RECOVERY} s")
    t0 = time.time()
    status = d.get("status")
    while time.time() - t0 < RECOVERY:
        mine(LOW_BASE)
        time.sleep(5)
        d = tx(tx_id)
        status = d.get("status")
        if int(time.time() - t0) % 120 < 5:
            log(f"recovery t+{int(time.time() - t0)} s: status {status}, pool {pool()}, broadcasts since spike {proxy_stats().get('send_raw', 0) - sends_before}")
        if status in ("mined", "confirmed", "failed", "expired", "canceled"):
            break
    stats = proxy_stats()
    observed = {
        "final_status": status,
        "status_reason": d.get("status_reason"),
        "first_broadcast": first,
        "recovery_seconds": int(time.time() - t0),
        "sender_nonce_before": nonce0,
        "sender_nonce_after": on_chain_nonce(),
        "broadcasts_total": stats.get("send_raw"),
        "broadcasts_during_recovery": stats.get("send_raw", 0) - sends_before,
        "anvil_pool_pending_queued": pool(),
    }
    if status in ("mined", "confirmed"):
        write_result("fixed", f"transfer {status} {observed['recovery_seconds']} s into the recovery window", observed)
    elif status in ("submitted", "sent", "pending") and observed["sender_nonce_after"] == nonce0 and observed["broadcasts_during_recovery"] == 0:
        write_result("reproduces", f"transfer still {status} after {observed['recovery_seconds']} s at a base fee one eighth of the cap, with no broadcast since the spike", observed)
    else:
        write_result("inconclusive", f"final status {status}; broadcasts during recovery {observed['broadcasts_during_recovery']}; nonce {nonce0} -> {observed['sender_nonce_after']}", observed)


if __name__ == "__main__":
    main()
