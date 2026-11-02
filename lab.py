#!/usr/bin/env python3
"""Relayer Failure Lab: run the scenarios against an OpenZeppelin Relayer image and record results.

Needs Docker with Compose and Python 3.10+ (standard library only on the host).

  lab.py run [--target v1.9.0 | --version V --image REF@sha256:...] [--scenario 817 ...] [--jobs 4] [--expect]
  lab.py merge --version V --image REF DIR...       write results/<V>.json from scenario results
  lab.py table                                      rewrite the results table in README.md
  lab.py matrix [--latest]                          GitHub Actions matrix for the pinned target(s)
  lab.py releases [--force V]                       Relayer releases that have no results yet
"""
import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"
TARGETS = ROOT / "targets.json"
README = ROOT / "README.md"
UPSTREAM_REPO = "OpenZeppelin/openzeppelin-relayer"
RELAYER_REPO = "openzeppelin/openzeppelin-relayer"
FOUNDRY = "ghcr.io/foundry-rs/foundry:v1.8.1@sha256:0c00cb0bda1ab1b91c9a6bf60f4c76c09c1a8870824b6d4718afbabacf6f9a17"
REDIS = "redis:bookworm@sha256:164c759a0c342ee69d08fc99219382b0fd682181465c0df2e0e6911f4c85d73c"
ANVIL_BASE = ["--host", "0.0.0.0", "--port", "8545", "--chain-id", "31337", "--base-fee", "100000000", "--gas-limit", "30000000"]

SCENARIOS = {
    "817": {
        "title": "Mined transaction marked Failed after a stale receipt read",
        "short": "#817 double payout",
        "anvil": ["--block-time", "2"],
        "proxy": {"HIDE_RECEIPT_SECS": "420"},
        "budget_s": 1200,
        "patterns": ["transaction not yet mined", "transaction has been pending for too long, resubmitting",
                     "nonce too low", "scheduling nonce recovery", "nonce consumed externally"],
    },
    "912": {
        "title": "Sign endpoints skip whitelist_receivers",
        "short": "#912 signing policy",
        "anvil": [],
        "proxy": {},
        "budget_s": 600,
        "patterns": ["Transaction target address is not whitelisted"],
    },
    "843": {
        "title": "Nonce too high jam that recovery never clears",
        "short": "#843 nonce jam",
        "anvil": ["--block-time", "2"],
        "proxy": {"STRICT_NONCE": "1"},
        "budget_s": 1800,
        "patterns": ["max attempts reached", "no nonce gaps detected", "rewound nonce counter", "nonce gaps confirmed below tx",
                     "reclaiming nonce slots", "nonce too high", "HTTP error 503"],
    },
    "808": {
        "title": "gas_price_cap leaves a transaction stuck after a gas spike",
        "short": "#808 gas cap stall",
        "anvil": ["--no-mining", "--order", "fifo"],
        "proxy": {},
        "budget_s": 2100,
        "patterns": ["transaction has been pending for too long, resubmitting",
                     "bumped gas price does not meet minimum requirement, skipping resubmission", "transaction not yet mined"],
    },
}
ORDER = ["817", "912", "843", "808"]
ANSI = re.compile(r"\x1b\[[0-9;]*m")
_print_lock = threading.Lock()


def say(msg, prefix=""):
    with _print_lock:
        print(f"{prefix}{msg}", flush=True)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_targets():
    return json.loads(TARGETS.read_text())


def tools_image():
    """Build the tools image, tagged by a hash of its inputs so a change rebuilds it."""
    h = hashlib.sha256()
    for p in sorted((ROOT / "harness").iterdir()) + [ROOT / "contracts" / "AuthToken.json"]:
        if p.is_file() and p.suffix in (".py", ".txt", ".json", "") and p.name != "__pycache__":
            h.update(p.name.encode() + p.read_bytes())
    tag = f"relayer-failure-lab-tools:{h.hexdigest()[:12]}"
    if subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode != 0:
        say(f"building {tag}")
        subprocess.run(["docker", "build", "-q", "-f", "harness/Dockerfile", "-t", tag, "."], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    return tag


def image_digest(image):
    """Repository digest of a pulled image reference."""
    out = subprocess.run(["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", image], capture_output=True, text=True)
    try:
        digests = json.loads(out.stdout.strip() or "[]")
    except json.JSONDecodeError:
        digests = []
    for d in digests:
        if d.startswith(RELAYER_REPO + "@") or d.startswith("docker.io/" + RELAYER_REPO + "@"):
            return d.split("@", 1)[1]
    m = re.search(r"@(sha256:[0-9a-f]{64})", image)
    return m.group(1) if m else None


def compose_spec(sid, image, tools, work, uid):
    sc = SCENARIOS[sid]
    user = f"{uid}:{os.getgid()}"
    vol = [f"{work}:/work"]
    proxy_env = {"UPSTREAM": "http://anvil:8545", "PROXY_LOG": "/work/logs/proxy.log", **sc["proxy"]}
    driver_env = {"API_KEY": "${LAB_API_KEY}", "RELAYER_ID": "lab", "RESULT": "/work/result.json", "DRIVER_LOG": "/work/logs/driver.log"}
    for k, v in os.environ.items():  # scenario parameters, e.g. LAB_843_TOTAL=720 -> TOTAL=720
        if k.startswith(f"LAB_{sid}_"):
            driver_env[k[len(f"LAB_{sid}_"):]] = v
    return {
        "name": None,
        "services": {
            "anvil": {"image": FOUNDRY, "entrypoint": ["anvil"], "command": ANVIL_BASE + sc["anvil"]},
            "redis": {"image": REDIS},
            "proxy": {"image": tools, "user": user, "command": ["python", "-u", "/lab/faultproxy.py"], "environment": proxy_env,
                      "volumes": vol, "depends_on": ["anvil"]},
            "receiver": {"image": tools, "user": user, "command": ["python", "-u", "/lab/receiver.py"], "volumes": vol},
            "relayer": {
                "image": image,
                "depends_on": ["redis", "proxy", "anvil", "receiver"],
                "environment": {
                    "REDIS_URL": "redis://redis:6379", "API_KEY": "${LAB_API_KEY}", "WEBHOOK_SIGNING_KEY": "${LAB_WEBHOOK_KEY}",
                    "KEYSTORE_PASSPHRASE": "${LAB_KEYSTORE_PASSPHRASE}", "IN_DOCKER": "true", "LOG_LEVEL": "debug",
                    "RPC_BLOCK_PRIVATE_IPS": "false",
                },
                "volumes": [f"{work}/config:/app/config:ro"],
            },
            "driver": {"image": tools, "user": user, "command": ["python", "-u", f"/lab/s{sid}.py"], "environment": driver_env, "volumes": vol},
        },
    }


def run_scenario(sid, version, image, out_dir, work_root):
    sc = SCENARIOS[sid]
    pre = f"[{sid}] "
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    work = work_root / f"{version}-{sid}-{stamp}"
    (work / "logs").mkdir(parents=True)
    (work / "config").mkdir()
    (work / "config" / "config.json").write_text((ROOT / "scenarios" / sid / "config.json").read_text())
    for name in ("proxy.log", "webhooks.jsonl", "driver.log"):
        (work / "logs" / name).touch()
    env = {**os.environ, "LAB_API_KEY": secrets.token_hex(24), "LAB_WEBHOOK_KEY": secrets.token_hex(24),
           "LAB_KEYSTORE_PASSPHRASE": secrets.token_hex(16)}
    redact = {env["LAB_API_KEY"]: "[api key]", env["LAB_WEBHOOK_KEY"]: "[webhook key]", env["LAB_KEYSTORE_PASSPHRASE"]: "[passphrase]"}
    tools = tools_image()
    spec = compose_spec(sid, image, tools, str(work), os.getuid())
    project = f"rfl-{sid}-{secrets.token_hex(3)}"
    spec["name"] = project
    cfile = work / "compose.json"
    cfile.write_text(json.dumps(spec, indent=1))
    dc = ["docker", "compose", "-p", project, "-f", str(cfile)]
    started = now_iso()
    t0 = time.time()
    result = {"outcome": "error", "summary": "the driver produced no result", "observed": {}}
    try:
        subprocess.run(["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}", "-e", "KEYSTORE_PASSPHRASE",
                        "-v", f"{work}:/work", tools, "python", "/lab/keystore.py", "/work/config/keys/anvil0.json"],
                       env={**env, "KEYSTORE_PASSPHRASE": env["LAB_KEYSTORE_PASSPHRASE"]}, check=True, capture_output=True)
        say(f"{version} {sc['title']}: starting ({image})", pre)
        subprocess.run(dc + ["up", "-d", "--quiet-pull", "anvil", "redis", "proxy", "receiver", "relayer"], env=env, check=True,
                       capture_output=True)
        p = subprocess.Popen(dc + ["run", "--rm", "--no-deps", "-T", "driver"], env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True)
        deadline = time.time() + sc["budget_s"]
        timer = threading.Timer(sc["budget_s"], p.kill)
        timer.start()
        for line in p.stdout:
            say(line.rstrip(), pre)
        p.wait()
        timer.cancel()
        if time.time() > deadline:
            result["summary"] = f"driver exceeded its {sc['budget_s']} s budget"
        rfile = work / "result.json"
        if rfile.exists():
            result = json.loads(rfile.read_text())
    except subprocess.CalledProcessError as e:
        result["summary"] = f"setup failed: {' '.join(e.cmd[:4])}... exit {e.returncode}: {(e.stderr or b'')[-300:].decode(errors='replace') if isinstance(e.stderr, bytes) else e.stderr}"
    finally:
        logs = subprocess.run(dc + ["logs", "--no-color", "--timestamps", "relayer"], env=env, capture_output=True, text=True)
        text = ANSI.sub("", logs.stdout + logs.stderr)
        for k, v in redact.items():
            text = text.replace(k, v)
        (work / "logs" / "relayer.log").write_text(text)
        subprocess.run(dc + ["down", "-v", "--remove-orphans", "-t", "5"], env=env, capture_output=True)
    m = re.search(r"service_version[\"=: ]+\"?([0-9][0-9A-Za-z.\-]*)", text)
    record = {
        "scenario": sid,
        "issue": f"https://github.com/{UPSTREAM_REPO}/issues/{sid}",
        "title": sc["title"],
        "outcome": result["outcome"],
        "summary": result["summary"],
        "relayer": {"version": version, "image": image, "digest": image_digest(image), "service_version": m.group(1) if m else None},
        "started_at": started,
        "finished_at": now_iso(),
        "duration_s": int(time.time() - t0),
        "log_counts": {pat: text.count(pat) for pat in sc["patterns"]},
        "observed": result.get("observed", {}),
        "logs": str(work.relative_to(ROOT)) if work.is_relative_to(ROOT) else str(work),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{sid}.json").write_text(json.dumps(record, indent=1) + "\n")
    say(f"{record['outcome'].upper()} in {record['duration_s']} s: {record['summary']}", pre)
    return record


def cmd_run(a):
    if a.target or not a.image:
        targets = load_targets()
        version = a.target or targets["default"]
        t = targets["targets"][version]
        image = f"{t['image']}@{t['digest']}"
    else:
        version, image = a.version, a.image
    if "@sha256:" not in image and not a.allow_unpinned:
        sys.exit("the image must be pinned by digest (REPO:TAG@sha256:...); --allow-unpinned runs a local build")
    sids = ORDER if not a.scenario or a.scenario == ["all"] else a.scenario
    for s in sids:
        if s not in SCENARIOS:
            sys.exit(f"unknown scenario {s}; known: {', '.join(ORDER)}")
    if "@sha256:" in image or subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode != 0:
        subprocess.run(["docker", "pull", "-q", image], check=True, stdout=subprocess.DEVNULL)
    out_dir = Path(a.out) if a.out else ROOT / ".work" / "out" / version
    work_root = Path(a.work).resolve() if a.work else ROOT / ".work"
    work_root.mkdir(parents=True, exist_ok=True)
    tools_image()
    records = {}
    threads = []
    sem = threading.Semaphore(max(1, a.jobs))

    def one(s):
        with sem:
            records[s] = run_scenario(s, version, image, out_dir, work_root)

    for s in sids:
        th = threading.Thread(target=one, args=(s,))
        th.start()
        threads.append(th)
        time.sleep(2)
    for th in threads:
        th.join()
    rc = 0
    expected = {}
    exp_file = RESULTS / f"{version}.json"
    if a.expect and exp_file.exists():
        expected = {k: v["outcome"] for k, v in json.loads(exp_file.read_text())["scenarios"].items()}
    say("")
    for s in sids:
        r = records.get(s, {"outcome": "error", "summary": "no record"})
        exp = expected.get(s)
        flag = ""
        if r["outcome"] not in ("reproduces", "fixed"):
            rc = 1
            flag = "  <- no verdict"
        elif exp and r["outcome"] != exp:
            rc = 1
            flag = f"  <- recorded outcome for {version} is {exp}"
        say(f"{s:>4} {r['outcome']:<12} {r['summary']}{flag}")
    return rc


def collect(dirs):
    recs = {}
    for d in dirs:
        for p in sorted(Path(d).rglob("*.json")):
            try:
                r = json.loads(p.read_text())
            except json.JSONDecodeError:
                continue
            if isinstance(r, dict) and r.get("scenario") in SCENARIOS and "outcome" in r:
                recs[r["scenario"]] = r
    return recs


def cmd_merge(a):
    recs = collect(a.dirs)
    missing = [s for s in ORDER if s not in recs]
    if missing and not a.allow_partial:
        sys.exit(f"missing scenario results: {', '.join(missing)}")
    run = {"at": max((r["finished_at"] for r in recs.values()), default=now_iso())}
    if os.environ.get("GITHUB_RUN_ID"):
        run.update({"url": f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}",
                    "event": os.environ.get("GITHUB_EVENT_NAME"), "commit": os.environ.get("GITHUB_SHA")})
    else:
        run["where"] = "local"
    sv = {r["relayer"].get("service_version") for r in recs.values()} - {None}
    digest = a.image.split("@", 1)[1] if "@" in a.image else None
    for r in recs.values():
        r.pop("logs", None)
    doc = {"relayer_version": a.version, "image": a.image.split("@", 1)[0], "digest": digest,
           "service_version": sv.pop() if len(sv) == 1 else sorted(sv), "run": run,
           "scenarios": {s: recs[s] for s in ORDER if s in recs}}
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"{a.version}.json").write_text(json.dumps(doc, indent=1) + "\n")
    if a.add_target:
        t = load_targets()
        t["targets"].setdefault(a.version, {"image": doc["image"], "digest": digest})
        TARGETS.write_text(json.dumps(t, indent=1) + "\n")
    lines = [f"### Relayer {a.version} ({digest[:19] if digest else 'no digest'})", "", "| Scenario | Outcome | Observed |", "|---|---|---|"]
    for s in ORDER:
        if s in recs:
            lines.append(f"| [#{s}]({recs[s]['issue']}) {recs[s]['title']} | {recs[s]['outcome']} | {recs[s]['summary']} |")
    print("\n".join(lines))
    return 0


def vkey(v):
    return tuple(int(x) for x in re.findall(r"[0-9]+", v)[:3])


def cmd_table(a):
    rows = []
    for p in sorted(RESULTS.glob("v*.json"), key=lambda p: vkey(p.stem), reverse=True):
        d = json.loads(p.read_text())
        cells = []
        for s in ORDER:
            r = d["scenarios"].get(s)
            cells.append(r["outcome"] if r else "not run")
        run = d.get("run", {})
        link = f"[run]({run['url']})" if run.get("url") else f"local run, {run.get('at', '')[:10]}"
        rows.append(f"| [{d['relayer_version']}](results/{p.name}) | " + " | ".join(cells) + f" | {run.get('at', '')[:10]} | {link} |")
    head = "| Relayer | " + " | ".join(SCENARIOS[s]["short"] for s in ORDER) + " | Date | Evidence |"
    table = "\n".join([head, "|" + "---|" * (len(ORDER) + 3)] + rows)
    text = README.read_text()
    new = re.sub(r"<!-- results:start -->.*?<!-- results:end -->",
                 lambda m: "<!-- results:start -->\n" + table + "\n<!-- results:end -->", text, flags=re.S)
    if new != text:
        README.write_text(new)
        print("README.md results table updated")
    else:
        print("README.md results table unchanged")
    return 0


def matrix_for(versions):
    t = load_targets()["targets"]
    include = []
    for v in versions:
        img = f"{t[v]['image']}@{t[v]['digest']}" if v in t else v
        for s in ORDER:
            include.append({"version": v, "image": img, "scenario": s})
    return {"include": include}


def gh_output(pairs):
    path = os.environ.get("GITHUB_OUTPUT")
    for k, v in pairs.items():
        if path:
            with open(path, "a") as f:
                f.write(f"{k}={v}\n")
        print(f"{k}={v}")


def cmd_matrix(a):
    t = load_targets()
    versions = [a.target or t["default"]]
    if a.latest:
        latest = max(t["targets"], key=vkey)
        if latest not in versions:
            versions.append(latest)
    gh_output({"matrix": json.dumps(matrix_for(versions))})
    return 0


def http_json(url, token=None):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "relayer-failure-lab"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def cmd_releases(a):
    token = os.environ.get("GITHUB_TOKEN") or None
    floor = vkey(a.since)
    rels = http_json(f"https://api.github.com/repos/{UPSTREAM_REPO}/releases?per_page=30", token)
    found = []
    for r in rels:
        tag = r["tag_name"]
        if r.get("draft") or r.get("prerelease") or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag):
            continue
        if tag != a.force and (vkey(tag) < floor or (RESULTS / f"{tag}.json").exists()):
            continue
        try:
            hub = http_json(f"https://hub.docker.com/v2/repositories/{RELAYER_REPO}/tags/{tag}")
        except urllib.error.HTTPError as e:
            print(f"{tag}: released on GitHub but not on Docker Hub yet (HTTP {e.code}); next check picks it up", file=sys.stderr)
            continue
        digest = hub.get("digest")
        if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            print(f"{tag}: Docker Hub tag has no valid digest yet ({str(digest)[:80]!r}); skipped", file=sys.stderr)
            continue
        found.append({"version": tag, "image": f"{RELAYER_REPO}:{tag}@{digest}", "published_at": r.get("published_at")})
        print(f"{tag}: published {r.get('published_at')}, image digest {digest}", file=sys.stderr)
    include = [{"version": f["version"], "image": f["image"], "scenario": s} for f in found for s in ORDER]
    gh_output({"found": "true" if found else "false", "versions": json.dumps(found), "matrix": json.dumps({"include": include})})
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run scenarios against a Relayer image")
    r.add_argument("--target", help="a version listed in targets.json (default: its default)")
    r.add_argument("--version", help="label for an image not in targets.json")
    r.add_argument("--image", help="REPO:TAG@sha256:... (with --version)")
    r.add_argument("--scenario", action="append", help="817, 912, 843, 808 or all (repeatable)")
    r.add_argument("--jobs", type=int, default=4, help="scenarios to run at once (default 4)")
    r.add_argument("--out", help="directory for the scenario result files")
    r.add_argument("--work", help="directory for per-run files and logs (default .work)")
    r.add_argument("--expect", action="store_true", help="fail when an outcome differs from results/<version>.json")
    r.add_argument("--allow-unpinned", action="store_true", help="accept an image without a digest, e.g. a local build of a fix branch")
    m = sub.add_parser("merge", help="write results/<version>.json")
    m.add_argument("--version", required=True)
    m.add_argument("--image", required=True)
    m.add_argument("--allow-partial", action="store_true")
    m.add_argument("--add-target", action="store_true", help="also add the version to targets.json")
    m.add_argument("dirs", nargs="+")
    sub.add_parser("table", help="rewrite the README results table")
    mx = sub.add_parser("matrix", help="matrix for the pinned target")
    mx.add_argument("--target")
    mx.add_argument("--latest", action="store_true", help="also the newest version in targets.json")
    rl = sub.add_parser("releases", help="new Relayer releases without results")
    rl.add_argument("--since", default="v1.9.0")
    rl.add_argument("--force", default="", help="include this release even if results exist")
    a = ap.parse_args()
    if a.cmd == "run" and a.image and not a.version:
        ap.error("--image needs --version")
    return {"run": cmd_run, "merge": cmd_merge, "table": cmd_table, "matrix": cmd_matrix, "releases": cmd_releases}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
