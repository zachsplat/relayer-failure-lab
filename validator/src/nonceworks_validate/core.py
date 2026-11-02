"""Static checks for OpenZeppelin Relayer and Monitor configuration.

Nothing is executed and nothing leaves the machine. Input is a directory, a ZIP
archive (for example a Defender export) or a single JSON file. Each finding has
a rule id, a severity (error, warning or notice), the file it concerns and a
message; some carry a link to a write-up or to the upstream issue.
"""
import io
import json
import os
import posixpath
import re
import zipfile

CHECKED_AGAINST = "Relayer 1.9.0 and Monitor 1.7.0 configuration rules"
MAX_FILES = 500
MAX_BYTES = 16 * 1024 * 1024
TEXT_SUFFIXES = (".json", ".sh", ".py", ".js", ".yml", ".yaml", ".txt")
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "target"}

SECRET_REF = re.compile(r'"type"\s*:\s*"(env|environment|plain|hashicorpcloudvault)"\s*,\s*"value"\s*:\s*"([^"]+)"', re.I)
MONITOR_SECRET_TYPES = {"plain", "environment", "hashicorpcloudvault"}
TRIGGER_TYPES = {"slack", "discord", "telegram", "email", "webhook", "script"}
SIGNATURE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\(.*\)$")
CRON_SHORTCUTS = {"@yearly", "@monthly", "@weekly", "@daily", "@hourly"}


def _cron_ok(cs):
    cs = str(cs).strip()
    return cs in CRON_SHORTCUTS or len(cs.split()) in (6, 7)


def _signature_problem(sig):
    """Monitor 1.7.0 strips whitespace before matching, so only names or keywords inside a parameter break it."""
    if not SIGNATURE.match(re.sub(r"\s+", "", sig)):
        return "is not of the form name(type,type,...)"
    inner = sig[sig.index("(") + 1:sig.rindex(")")]
    named = [p.strip() for p in re.split(r"[,()]", inner) if len(p.split()) > 1]
    if named:
        return f"has parameter names or keywords ({', '.join(named)})"
    return None

NOTES = "https://nonceworks.com/notes/"
ISSUE = "https://github.com/OpenZeppelin/openzeppelin-relayer/issues/"
LINK = {
    "env": NOTES + "monitor-failed-to-load-triggers.html",
    "808": NOTES + "relayer-gas-price-cap-stuck-808.html",
    "843": NOTES + "relayer-nonce-too-high-jam-843.html",
    "817": NOTES + "relayer-nonce-consumed-externally-817.html",
    "912": NOTES + "relayer-sign-endpoints-not-policied.html",
    "498": NOTES + "monitor-status-failure-never-matches-498.html",
    "key": NOTES + "relayer-signing-key-32-characters.html",
}

# rule id -> (severity, one-line description); README and --list-rules print this table
RULES = {
    "json-parse": ("error", "A JSON file does not parse"),
    "no-config": ("error", "No Relayer config.json or Monitor tree found"),
    "monitor-trigger-shape": ("error", "Monitor trigger lacks trigger_type or config"),
    "monitor-trigger-type": ("error", "Monitor trigger_type is not one the Monitor knows"),
    "monitor-secret-type": ("error", "Monitor secret uses the Relayer's \"env\" spelling; the Monitor exits with 'Failed to load triggers'"),
    "monitor-network-shape": ("error", "Monitor network lacks network_type, slug or rpc_urls"),
    "monitor-shape": ("error", "Monitor lacks name, networks, addresses, match_conditions or triggers"),
    "monitor-chain-id": ("warning", "EVM network without chain_id"),
    "monitor-cron": ("warning", "cron_schedule does not have 6 or 7 fields (seconds first) and is not a @yearly, @monthly, @weekly, @daily or @hourly shortcut"),
    "monitor-max-past-blocks": ("warning", "max_past_blocks not set; the default can skip blocks after downtime"),
    "monitor-unknown-network": ("warning", "Monitor references a network that is not in networks/"),
    "monitor-unknown-trigger": ("warning", "Monitor references a trigger that is not in triggers/"),
    "monitor-signature": ("warning", "Event or function signature carries parameter names or keywords instead of types only"),
    "monitor-status-498": ("warning", "Transaction status condition without gas_used can miss failed transactions on Monitor 1.6.0 (#498, fixed in 1.7.0)"),
    "monitor-filter-missing": ("warning", "trigger_conditions script is not in filters/"),
    "relayer-unknown-signer": ("error", "Relayer references a signer that is not in signers[]"),
    "relayer-unknown-notification": ("error", "Relayer references a notification that is not in notifications[]"),
    "relayer-webhook-key": ("error", "Plain webhook signing_key shorter than 32 characters; the Relayer refuses it"),
    "relayer-unknown-network": ("warning", "Relayer references a network that is not in the inline networks list"),
    "relayer-plain-secret": ("warning", "Signer carries a plain-text secret"),
    "relayer-gas-price-cap": ("warning", "gas_price_cap set: a transaction that reaches the cap during a spike is never resent (#808, reported on 1.5.0, reproduced on 1.8.0 and 1.9.0)"),
    "relayer-no-whitelist": ("warning", "EVM relayer without whitelist_receivers signs transactions to any recipient"),
    "relayer-sign-endpoints": ("warning", "whitelist_receivers does not cover /sign and /sign-typed-data (#912, reproduced on 1.9.0)"),
    "relayer-single-rpc": ("warning", "One RPC URL: a transport error during submit leaves a stuck Sent slot (#843, reproduced on 1.9.0)"),
    "relayer-failed-status": ("notice", "A Failed status can belong to a mined transaction after a stale receipt read (#817, reproduced on 1.9.0)"),
    "relayer-local-signer": ("notice", "Signer is a local keystore file on the host"),
    "relayer-networks-path": ("notice", "networks is a directory path; those files are checked only when they are in the input"),
}


class InputError(Exception):
    pass


def _finding(rule, file, msg, link=None):
    sev = RULES[rule][0]
    f = {"rule": rule, "severity": sev, "file": file, "message": msg}
    if link:
        f["link"] = link
    return f


def read_input(path):
    """Return ({relative_path: text}, root prefix) from a directory, a ZIP archive or a single file."""
    files = {}
    if os.path.isdir(path):
        total = 0
        for dirpath, dirnames, filenames in os.walk(path):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for name in sorted(filenames):
                if not name.endswith(TEXT_SUFFIXES):
                    continue
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, path).replace(os.sep, "/")
                size = os.path.getsize(full)
                total += size
                if len(files) >= MAX_FILES or total > MAX_BYTES:
                    raise InputError(f"input is larger than {MAX_FILES} files or {MAX_BYTES // 2**20} MB")
                with open(full, encoding="utf-8", errors="replace") as fh:
                    files[rel] = fh.read()
    else:
        with open(path, "rb") as fh:
            data = fh.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise InputError(f"input is larger than {MAX_BYTES // 2**20} MB")
        if zipfile.is_zipfile(io.BytesIO(data)):
            z = zipfile.ZipFile(io.BytesIO(data))
            infos = [i for i in z.infolist() if not i.is_dir()]
            if len(infos) > MAX_FILES:
                raise InputError(f"more than {MAX_FILES} files in the archive")
            total = 0
            for i in infos:
                name = posixpath.normpath(i.filename.replace("\\", "/"))
                if name.startswith("/") or name.startswith("..") or "/../" in name:
                    raise InputError(f"unsafe path in archive: {i.filename}")
                if "__MACOSX" in name or name.endswith(".DS_Store") or not name.endswith(TEXT_SUFFIXES):
                    continue
                total += i.file_size
                if total > MAX_BYTES:
                    raise InputError(f"archive expands beyond {MAX_BYTES // 2**20} MB")
                files[name] = z.read(i).decode("utf-8", "replace")
        else:
            files[os.path.basename(path) or "input.json"] = data.decode("utf-8", "replace")
    return _strip_root(files)


def _strip_root(files):
    """Return (files re-rooted, root prefix removed)."""
    """Re-root paths at the shallowest directory holding monitors/, triggers/, networks/, filters/ or config.json."""
    roots = set()
    for n in files:
        parts = n.split("/")
        for d in ("monitors", "triggers", "networks", "filters"):
            if d in parts[:-1]:
                roots.add("/".join(parts[:parts.index(d)]))
        if parts[-1] == "config.json":
            roots.add("/".join(parts[:-1]))
    root = min(roots, key=len) if roots else ""
    if root:
        files = {n[len(root) + 1:] if n.startswith(root + "/") else n: v for n, v in files.items()}
    return files, root


def _parse(files, out):
    parsed = {}
    for n, text in files.items():
        if not n.endswith(".json"):
            continue
        try:
            parsed[n] = json.loads(text)
        except json.JSONDecodeError as e:
            out.append(_finding("json-parse", n, f"JSON does not parse: {e.msg} at line {e.lineno}, column {e.colno}"))
    return parsed


def _check_monitor(parsed, files, out, summary):
    triggers, networks = {}, {}
    for n, data in parsed.items():
        if n.startswith("triggers/") and isinstance(data, dict):
            for tid, t in data.items():
                if not isinstance(t, dict) or "trigger_type" not in t or "config" not in t:
                    out.append(_finding("monitor-trigger-shape", n, f"trigger {tid} lacks trigger_type or config"))
                    continue
                triggers[tid] = t["trigger_type"]
                if t["trigger_type"] not in TRIGGER_TYPES:
                    out.append(_finding("monitor-trigger-type", n, f"trigger {tid}: trigger_type {t['trigger_type']!r} is not one of {sorted(TRIGGER_TYPES)}"))
            for m in SECRET_REF.finditer(files[n]):
                if m.group(1).lower() not in MONITOR_SECRET_TYPES:
                    out.append(_finding("monitor-secret-type", n, f"secret source type {m.group(1)!r} for {m.group(2)}: the Monitor accepts plain, environment or hashicorpcloudvault; env is the Relayer's spelling and the Monitor exits with 'Failed to load triggers'", LINK["env"]))
        elif n.startswith("networks/") and isinstance(data, dict):
            missing = [k for k in ("network_type", "slug", "rpc_urls") if k not in data]
            if missing:
                out.append(_finding("monitor-network-shape", n, f"network lacks {', '.join(missing)}"))
                continue
            networks[data["slug"]] = data["network_type"]
            if str(data.get("network_type", "")).upper() == "EVM" and "chain_id" not in data:
                out.append(_finding("monitor-chain-id", n, "EVM network without chain_id"))
            cs = data.get("cron_schedule")
            if cs and not _cron_ok(cs):
                out.append(_finding("monitor-cron", n, f"cron_schedule {cs!r} does not have 6 or 7 fields (the Monitor uses seconds-first cron) and is not an @yearly, @monthly, @weekly, @daily or @hourly shortcut"))
            if "max_past_blocks" not in data:
                out.append(_finding("monitor-max-past-blocks", n, "max_past_blocks not set; the default can skip blocks after downtime"))
            for m in SECRET_REF.finditer(files[n]):
                if m.group(1).lower() not in MONITOR_SECRET_TYPES:
                    out.append(_finding("monitor-secret-type", n, f"secret source type {m.group(1)!r} for {m.group(2)} (the Monitor wants environment)", LINK["env"]))
    for n, m in parsed.items():
        if not (n.startswith("monitors/") and isinstance(m, dict)):
            continue
        missing = [k for k in ("name", "networks", "addresses", "match_conditions", "triggers") if k not in m]
        if missing:
            out.append(_finding("monitor-shape", n, f"monitor lacks {', '.join(missing)}"))
            continue
        for net in m["networks"]:
            if networks and net not in networks:
                out.append(_finding("monitor-unknown-network", n, f"monitor {m['name']} references network {net!r}, which is not in networks/"))
        for t in m["triggers"]:
            if triggers and t not in triggers:
                out.append(_finding("monitor-unknown-trigger", n, f"monitor {m['name']} references trigger {t!r}, which is not in triggers/"))
        mc = m.get("match_conditions") or {}
        for kind in ("events", "functions"):
            for c in mc.get(kind) or []:
                sig = c.get("signature", "")
                problem = _signature_problem(sig)
                if problem:
                    out.append(_finding("monitor-signature", n, f"{kind[:-1]} signature {sig!r} {problem}; the Monitor matches on types only, e.g. Transfer(address,address,uint256)"))
        for c in mc.get("transactions") or []:
            st = c.get("status", "Any")
            if st in ("Success", "Failure") and "gas_used" not in (c.get("expression") or ""):
                out.append(_finding("monitor-status-498", n, f"transaction condition with status {st} and no gas_used in the expression: can miss failed transactions on Monitor 1.6.0 when another transaction in the block emits a log (#498, fixed in 1.7.0); adding gas_used to the expression forces the receipt read", LINK["498"]))
        for tc in m.get("trigger_conditions") or []:
            sp = posixpath.basename(tc.get("script_path", ""))
            if sp and not any(f.endswith("filters/" + sp) for f in files):
                out.append(_finding("monitor-filter-missing", n, f"trigger_condition script {sp} is not in filters/"))
        summary["monitors"].append({"name": m["name"], "networks": m["networks"], "paused": bool(m.get("paused", False)), "triggers": m["triggers"]})
    summary["triggers"] = [{"id": k, "type": v} for k, v in triggers.items()]
    summary["networks"] = [{"slug": k, "type": v} for k, v in networks.items()]


def _relayer_configs(parsed):
    for n, d in parsed.items():
        if posixpath.basename(n) == "config.json" and isinstance(d, dict) and ("relayers" in d or "signers" in d):
            yield n, d


def _check_relayer(parsed, out, summary):
    for f, cfg in _relayer_configs(parsed):
        sig_ids = {s.get("id") for s in cfg.get("signers") or [] if isinstance(s, dict)}
        notif_ids = {n.get("id") for n in cfg.get("notifications") or [] if isinstance(n, dict)}
        nets = cfg.get("networks")
        net_ids = {n.get("network") for n in nets if isinstance(n, dict)} if isinstance(nets, list) else set()
        if isinstance(nets, str):
            out.append(_finding("relayer-networks-path", f, f"networks is a directory path ({nets}); those files are checked only when they are in the input"))
        for s in cfg.get("signers") or []:
            if not isinstance(s, dict):
                continue
            summary["signers"].append({"id": s.get("id"), "type": s.get("type")})
            if s.get("type") == "local":
                out.append(_finding("relayer-local-signer", f, f"signer {s.get('id')} is a local keystore file; a KMS signer (aws_kms, google_cloud_kms, turnkey) keeps the key off the host"))
            for m in SECRET_REF.finditer(json.dumps(s)):
                if m.group(1).lower() == "plain":
                    out.append(_finding("relayer-plain-secret", f, f"signer {s.get('id')} carries a plain-text secret; use an env reference"))
        for n in cfg.get("notifications") or []:
            if not isinstance(n, dict):
                continue
            sk = n.get("signing_key")
            if isinstance(sk, dict) and sk.get("type") == "plain" and len(str(sk.get("value", ""))) < 32:
                out.append(_finding("relayer-webhook-key", f, f"notification {n.get('id')}: signing_key shorter than 32 characters; the Relayer refuses it at start", LINK["key"]))
        for r in cfg.get("relayers") or []:
            if not isinstance(r, dict):
                continue
            rid = r.get("id")
            pol = r.get("policies") or {}
            summary["relayers"].append({"id": rid, "network": r.get("network"), "network_type": r.get("network_type"), "signer": r.get("signer_id"), "policies": sorted(pol)})
            if r.get("signer_id") not in sig_ids:
                out.append(_finding("relayer-unknown-signer", f, f"relayer {rid} references signer {r.get('signer_id')!r}, which is not in signers[]"))
            if r.get("notification_id") and r.get("notification_id") not in notif_ids:
                out.append(_finding("relayer-unknown-notification", f, f"relayer {rid} references notification {r.get('notification_id')!r}, which is not in notifications[]"))
            if net_ids and r.get("network") not in net_ids:
                out.append(_finding("relayer-unknown-network", f, f"relayer {rid} references network {r.get('network')!r}, which is not in the inline networks list"))
            if r.get("network_type") != "evm":
                continue
            if "gas_price_cap" in pol:
                out.append(_finding("relayer-gas-price-cap", f, f"relayer {rid} sets gas_price_cap {pol['gas_price_cap']}; a transaction priced at the cap when the base fee spikes is never resent, even after the fee falls (#808, reported on 1.5.0, reproduced on 1.8.0 and 1.9.0). Leave headroom above expected spikes, or alert on 'bumped gas price does not meet minimum requirement'", LINK["808"]))
            if pol.get("whitelist_receivers"):
                out.append(_finding("relayer-sign-endpoints", f, f"relayer {rid} has whitelist_receivers, but POST /sign and /sign-typed-data ignore it: the API key can get an EIP-3009 or Permit2 signature for any recipient and amount (#912). Keep only gas on the relayer address, give it no token approvals, and keep the API key off clients", LINK["912"]))
            else:
                out.append(_finding("relayer-no-whitelist", f, f"relayer {rid} has no whitelist_receivers; it signs transactions to any recipient within its balance"))
            urls = r.get("custom_rpc_urls")
            if isinstance(urls, list) and len(urls) == 1:
                out.append(_finding("relayer-single-rpc", f, f"relayer {rid} has a single custom RPC URL; submit jobs are not retried (WORKER_TRANSACTION_SUBMIT_RETRIES = 0 on 1.9.0), so one transport error leaves a Sent slot the chain never saw and a nonce jam follows on strict-nonce chains (#843). Add a second URL", LINK["843"]))
            out.append(_finding("relayer-failed-status", f, f"relayer {rid}: a Failed status with reason 'consumed externally' can belong to a mined transaction after a stale receipt read (#817). Check every hash on chain before retrying a payout", LINK["817"]))
        if isinstance(nets, list):
            for n in nets:
                if isinstance(n, dict) and n.get("type") == "evm" and len(n.get("rpc_urls") or []) == 1:
                    out.append(_finding("relayer-single-rpc", f, f"network {n.get('network')} has one rpc_urls entry; one transport error during submit leaves a stuck Sent slot (#843). Add a second provider", LINK["843"]))


def validate_files(files, ignore=(), root=""):
    findings = []
    summary = {"files": sorted(files), "monitors": [], "triggers": [], "networks": [], "relayers": [], "signers": []}
    parsed = _parse(files, findings)
    _check_monitor(parsed, files, findings, summary)
    _check_relayer(parsed, findings, summary)
    refs = set()
    for t in files.values():
        for m in SECRET_REF.finditer(t):
            if m.group(1).lower() in ("env", "environment"):
                refs.add(m.group(2))
    summary["secret_refs"] = sorted(refs)
    if not parsed or not (summary["relayers"] or summary["monitors"] or summary["triggers"] or summary["networks"] or summary["signers"]):
        if not any(f["rule"] == "json-parse" for f in findings):
            findings.append(_finding("no-config", "", "no Relayer config.json or Monitor tree (monitors/, triggers/, networks/) found"))
    findings = [f for f in findings if f["rule"] not in set(ignore)]
    for f in findings:  # path relative to the input, with the prefix the re-rooting removed
        f["path"] = f"{root}/{f['file']}" if root and f["file"] else f["file"]
    counts = {s: sum(1 for f in findings if f["severity"] == s) for s in ("error", "warning", "notice")}
    return {"checked_against": CHECKED_AGAINST, "ok": counts["error"] == 0, "counts": counts, "findings": findings, "root": root, **summary}


def validate_path(path, ignore=()):
    files, root = read_input(path)
    return validate_files(files, ignore, root)
