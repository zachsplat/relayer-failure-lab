#!/bin/sh
# Rebuild AuthToken.json from AuthToken.sol with the pinned Foundry image.
# usage: contracts/build.sh            (writes contracts/AuthToken.json)
#        contracts/build.sh --check    (fails if the committed bytecode differs)
set -eu
cd "$(dirname "$0")"
FOUNDRY=ghcr.io/foundry-rs/foundry:v1.8.1@sha256:0c00cb0bda1ab1b91c9a6bf60f4c76c09c1a8870824b6d4718afbabacf6f9a17
OUT=$(mktemp -d)
trap 'rm -rf "$OUT"' EXIT
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD:/src:ro" -v "$OUT:/out" -w /tmp --entrypoint forge "$FOUNDRY" \
  build /src/AuthToken.sol --use 0.8.26 --optimize --optimizer-runs 200 --evm-version cancun --out /out/art --cache-path /out/cache >/dev/null
python3 - "$OUT/art/AuthToken.sol/AuthToken.json" "${1:-}" <<'PY'
import json, sys
art = json.load(open(sys.argv[1]))
out = {"contract": "AuthToken", "source": "contracts/AuthToken.sol", "solc": "0.8.26", "optimizer_runs": 200, "evm_version": "cancun",
       "bytecode": art["bytecode"]["object"], "abi": art["abi"]}
text = json.dumps(out, indent=1) + "\n"
if sys.argv[2] == "--check":
    old = json.load(open("AuthToken.json"))
    if old["bytecode"] != out["bytecode"]:
        sys.exit("AuthToken.json bytecode differs from a fresh build of AuthToken.sol")
    print("AuthToken.json matches a fresh build")
else:
    open("AuthToken.json", "w").write(text)
    print("wrote contracts/AuthToken.json,", (len(out["bytecode"]) - 2) // 2, "bytes")
PY
