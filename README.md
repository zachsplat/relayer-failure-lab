# Relayer Failure Lab

[![suite](https://github.com/zachsplat/relayer-failure-lab/actions/workflows/suite.yml/badge.svg)](https://github.com/zachsplat/relayer-failure-lab/actions/workflows/suite.yml) [![release](https://github.com/zachsplat/relayer-failure-lab/actions/workflows/release.yml/badge.svg)](https://github.com/zachsplat/relayer-failure-lab/actions/workflows/release.yml)

Tests for four open issues in [OpenZeppelin Relayer](https://github.com/OpenZeppelin/openzeppelin-relayer), the self-hosted service that signs and sends backend transactions. Each scenario runs the official `openzeppelin/openzeppelin-relayer` image, pinned by tag and digest, against a local anvil chain, injects the fault the issue describes, and asserts what the Relayer does: the issue **reproduces**, or it is **fixed**. CI runs all four on every push and every week, and on each new Relayer release it runs them and commits the results here.

The repository also ships `nonceworks-validate`, a static checker for Relayer and Monitor configuration, as a command line tool and a GitHub Action.

Maintained by Nonceworks. Not affiliated with OpenZeppelin.

## Results

<!-- results:start -->
| Relayer | #817 double payout | #912 signing policy | #843 nonce jam | #808 gas cap stall | Date | Evidence |
|---|---|---|---|---|---|---|
| [v1.9.0](results/v1.9.0.json) | reproduces | reproduces | reproduces | reproduces | 2026-11-02 | [run](https://github.com/zachsplat/relayer-failure-lab/actions/runs/41385667287) |
| [v1.8.0](results/v1.8.0.json) | reproduces | reproduces | reproduces | reproduces | 2026-11-02 | local run, 2026-11-02 |
<!-- results:end -->

One file per release in [results/](results/): the image digest, the `service_version` the container reported, each scenario's outcome, what was observed and counts of the relevant Relayer log lines. A new release gets a row without anyone touching the repository: [release.yml](.github/workflows/release.yml) checks for one every six hours.

## Scenarios

| Scenario | What goes wrong in production | What the test does | Reproduces when | Fixed when |
|---|---|---|---|---|
| [#817](https://github.com/OpenZeppelin/openzeppelin-relayer/issues/817) | A stale receipt read marks a mined transaction Failed, so an app that retries failed payouts pays twice | Anvil mines every 2 s. A proxy answers `null` to `eth_getTransactionReceipt` for 420 s after a receipt exists, while nonce reads pass through. One `fast` transfer | The Relayer finalises the transfer `failed` while anvil holds its receipt with status 1 | The Relayer reports it mined or confirmed |
| [#912](https://github.com/OpenZeppelin/openzeppelin-relayer/issues/912) | `whitelist_receivers` does not apply to `/sign` and `/sign-typed-data`, so the API key can authorize EIP-3009 or Permit2 transfers to anyone | A test token with EIP-3009 `transferWithAuthorization` ([contracts/AuthToken.sol](contracts/AuthToken.sol)) holds 1,000 tokens on the Relayer's address; `whitelist_receivers` allows account #1 only. A transaction to account #2 must be refused; then `/sign-typed-data` is asked to authorize a transfer of the whole balance to account #2, and account #2 submits it | The policy refuses the transaction, the endpoint signs anyway and the tokens move to account #2 | The endpoint refuses, or its signature does not authorize the transfer |
| [#843](https://github.com/OpenZeppelin/openzeppelin-relayer/issues/843) | After a provider outage, transactions jam behind `nonce too high` while traffic continues | Anvil mines every 2 s behind a proxy that rejects a gapped nonce at once with `nonce too high` (Arbitrum holds one for about a second, then rejects it) and answers HTTP 503 to every broadcast for 60 s. The network carries the tags of the Relayer's own `arbitrum-one` definition (`arbitrum-based`, `rollup`, `no-mempool`), since the report is from Arbitrum. One transfer every 2 s for 12 minutes; the drift (Relayer's next nonce minus on-chain nonce) is sampled every 30 s | The drift never falls below 10 in the last 5 minutes of intake | The drift never rises above 2 in that window |
| [#808](https://github.com/OpenZeppelin/openzeppelin-relayer/issues/808) | A transaction priced at `gas_price_cap` during a gas spike is never resent after the spike | Anvil with mining off; `gas_price_cap` 0.8 gwei. A `fast` transfer is broadcast at the cap, the base fee goes to 5 gwei for 90 s, the driver drops the transfer from anvil's pool (the eviction a real spike causes), and the base fee returns to 0.1 gwei with a block every 5 s for 20 minutes | After 20 minutes the transfer is not mined, the sender's nonce has not moved and the Relayer made no broadcast since the spike | The transfer is mined or confirmed in the window |

Anything else is **inconclusive**, and a run that ends without a verdict fails CI. The fault proxy is [harness/faultproxy.py](harness/faultproxy.py); each scenario's driver is `harness/s<issue>.py` and its Relayer config `scenarios/<issue>/config.json`.

A fix registers as **fixed**: [PR #862](https://github.com/OpenZeppelin/openzeppelin-relayer/pull/862) for #843, rebased onto v1.9.0 and built locally, ran the #843 scenario with a drift of 0 in every sample of the last 5 minutes and an empty queue 30 s after intake stopped, where stock v1.9.0 held a drift of 39 to 44 ([results/branches/pr862-on-v1.9.0-843.json](results/branches/pr862-on-v1.9.0-843.json)). #862 only acts on networks tagged `arbitrum-based`; the same build on the untagged network still reproduced, with a drift of 39 to 40 ([results/branches/pr862-on-v1.9.0-843-untagged.json](results/branches/pr862-on-v1.9.0-843-untagged.json)).

How these setups differ from the original reports: anvil instead of Base, Arbitrum or mainnet; one proxy that makes a provider's misbehavior deterministic instead of a real provider pool; minutes instead of days. The end states match what the reports describe. The earlier single-bug reproductions, with longer write-ups, are [relayer-817-repro](https://github.com/zachsplat/relayer-817-repro), [relayer-843-repro](https://github.com/zachsplat/relayer-843-repro) and [relayer-808-repro](https://github.com/zachsplat/relayer-808-repro).

## Run it

Requirements: Docker with Compose v2 and Python 3.10 or later. The host needs nothing else: the proxy, the webhook receiver and the drivers run in a small tools image built from [harness/Dockerfile](harness/Dockerfile) with pinned dependencies.

```bash
python3 lab.py run                       # all four against the pinned release (targets.json), in parallel
python3 lab.py run --scenario 912        # one scenario
python3 lab.py run --version v1.9.0 \
  --image openzeppelin/openzeppelin-relayer:v1.9.0@sha256:3ceb3986c636cd41c8eadc685a50c8e3df643206f039485694e41c6922d47fae
python3 lab.py run --expect              # also fail if an outcome differs from results/<version>.json
```

Run times on our test machine: #912 under a minute, #817 about 4 minutes, #843 about 15, #808 about 22; in parallel the whole suite takes as long as #808. Each run gets its own compose project with no host ports, so runs do not collide. Per-run files and logs (Relayer, proxy, driver, webhooks) land in `.work/<version>-<scenario>-<time>/`, the scenario results in `.work/out/<version>/`. The API key, webhook signing key and keystore passphrase are random per run and are redacted from the saved Relayer log; the signer is anvil's public development key.

To test a fix branch, build the image and pass it with `--version` and `--image`; a local build has no registry digest, so add `--allow-unpinned`:

```bash
docker build -f Dockerfile.production -t relayer:my-fix .     # in your openzeppelin-relayer checkout
python3 lab.py run --version my-fix --image relayer:my-fix --allow-unpinned --scenario 843
```

## CI

- [suite.yml](.github/workflows/suite.yml): on every push and pull request, the four scenarios against the pinned release, each as its own job, with logs uploaded as artifacts. Weekly, also against the newest release in `targets.json`. Fails when a scenario reaches no verdict or its outcome differs from the recorded one.
- [release.yml](.github/workflows/release.yml): every six hours, lists OpenZeppelin Relayer releases on GitHub that have no file in `results/`, takes the digest Docker Hub reports for the tag, runs the four scenarios on that image, and commits `results/<version>.json`, the new `targets.json` entry and the table above. A release whose image is not on Docker Hub yet is picked up by a later run. `workflow_dispatch` with a version re-runs a release that already has results. Nothing is committed unless every scenario reached reproduces or fixed; otherwise the job fails and a later run retries.
- The weekly suite run also writes its date and outcome to `results/last-verified.json`, at most once every 28 days, so GitHub never disables the scheduled workflows for inactivity between Relayer releases.
- [validator.yml](.github/workflows/validator.yml): unit tests for the validator, a package install, and the Action on a valid and a broken config.

## Validator

`nonceworks-validate` reads a config directory, a ZIP archive (for example a Defender export) or a JSON file and reports errors, warnings and notices, each with a rule id. Static only: nothing is executed and nothing leaves the machine. Standard library only.

```bash
pip install "git+https://github.com/zachsplat/relayer-failure-lab#subdirectory=validator"
nonceworks-validate config/              # exit 1 on errors
nonceworks-validate --strict config/     # exit 1 on warnings too
nonceworks-validate --format json config/ | jq .counts
nonceworks-validate --ignore relayer-local-signer,relayer-failed-status config/
nonceworks-validate --list-rules
```

As a GitHub Action, in the repository that holds your Relayer or Monitor config:

```yaml
- uses: actions/checkout@v7
- uses: zachsplat/relayer-failure-lab@v1
  with:
    path: config            # directory, ZIP or JSON file; one per line for several
    strict: "false"         # "true" fails on warnings too
    ignore: ""              # rule ids to skip, comma-separated
```

The Action annotates the files, writes a report to the job summary, and sets the outputs `errors`, `warnings` and `notices`. It is tested on `ubuntu-24.04` and needs `python3` and bash 4 or later on the runner.

Rules that come from this lab: `relayer-gas-price-cap` (#808), `relayer-single-rpc` (#843), `relayer-sign-endpoints` (#912) and the notice `relayer-failed-status` (#817). The others check structure and cross-references: unknown signer, notification or network ids, webhook signing keys under 32 characters, Monitor secrets written with the Relayer's `env` spelling, Monitor cron and signature formats, and more. `nonceworks-validate --list-rules` prints all of them.

## Layout

```
lab.py                 run scenarios, merge results, update the table, find new releases
targets.json           Relayer releases under test, pinned by digest
harness/               tools image: fault proxy, webhook receiver, scenario drivers
scenarios/<issue>/     Relayer config per scenario
contracts/             EIP-3009 test token for #912 (source, build script, compiled artifact)
results/               one JSON file per Relayer release
validator/             nonceworks-validate (Python package)
action.yml             the validator as a GitHub Action
```

## License

MIT, see [LICENSE](LICENSE).
