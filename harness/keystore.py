"""Write an encrypted keystore for anvil's public development account #0.

The key is public test material (anvil prints it at startup); never use it anywhere
real. The passphrase comes from KEYSTORE_PASSPHRASE, a value generated per run.
usage: python keystore.py /work/config/keys/anvil0.json
"""
import json
import os
import sys

from eth_account import Account

ANVIL_KEY_0 = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"

path = sys.argv[1]
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w") as f:
    json.dump(Account.encrypt(ANVIL_KEY_0, os.environ["KEYSTORE_PASSPHRASE"]), f)
print("keystore written for", Account.from_key(ANVIL_KEY_0).address)
