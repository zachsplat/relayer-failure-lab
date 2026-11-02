"""Command line entry point: nonceworks-validate PATH [PATH ...]"""
import argparse
import json
import os
import sys

from . import __version__
from .core import CHECKED_AGAINST, RULES, InputError, validate_path

SEV_ORDER = {"error": 0, "warning": 1, "notice": 2}


def _text(path, rep, out):
    c = rep["counts"]
    out.write(f"{path}: {c['error']} error(s), {c['warning']} warning(s), {c['notice']} notice(s)\n")
    for f in sorted(rep["findings"], key=lambda f: (SEV_ORDER[f["severity"]], f["file"], f["rule"])):
        loc = f["path"] or path
        out.write(f"  {f['severity']:<7} {f['rule']:<28} {loc}: {f['message']}\n")
        if f.get("link"):
            out.write(f"          {'':<28} {f['link']}\n")
    if rep["relayers"]:
        out.write("  relayers: " + ", ".join(f"{r['id']} ({r['network']}, signer {r['signer']})" for r in rep["relayers"]) + "\n")
    if rep["monitors"]:
        out.write("  monitors: " + ", ".join(m["name"] for m in rep["monitors"]) + "\n")
    if rep["secret_refs"]:
        out.write("  environment variables referenced: " + ", ".join(rep["secret_refs"]) + "\n")


def _github(path, rep, out):
    level = {"error": "error", "warning": "warning", "notice": "notice"}
    for f in rep["findings"]:
        file = os.path.join(path, f["path"]) if os.path.isdir(path) and f["path"] else path
        msg = f"{f['message']}" + (f" ({f['link']})" if f.get("link") else "")
        msg = msg.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        out.write(f"::{level[f['severity']]} file={file},title={f['rule']}::{msg}\n")


def _markdown(reports):
    lines = ["## Relayer and Monitor config check", "", f"Checked against {CHECKED_AGAINST}. Static only: nothing was executed or sent anywhere.", ""]
    for path, rep in reports:
        c = rep["counts"]
        lines.append(f"**{path}**: {c['error']} error(s), {c['warning']} warning(s), {c['notice']} notice(s)")
        lines.append("")
        if rep["findings"]:
            lines += ["| Severity | Rule | File | Finding |", "|---|---|---|---|"]
            for f in sorted(rep["findings"], key=lambda f: (SEV_ORDER[f["severity"]], f["file"])):
                link = f" [details]({f['link']})" if f.get("link") else ""
                lines.append(f"| {f['severity']} | `{f['rule']}` | {f['path'] or path} | {f['message'].replace('|', '/')}{link} |")
            lines.append("")
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="nonceworks-validate", description="Static checks for OpenZeppelin Relayer and Monitor configuration. Nothing is executed or sent anywhere.")
    ap.add_argument("paths", nargs="*", help="config directory, ZIP archive (e.g. a Defender export) or JSON file")
    ap.add_argument("--format", choices=("text", "json", "github", "markdown"), default="text")
    ap.add_argument("--strict", action="store_true", help="exit 1 on warnings too")
    ap.add_argument("--ignore", action="append", default=[], metavar="RULE", help="skip a rule id (repeatable, or comma-separated)")
    ap.add_argument("--summary", metavar="FILE", help="also append a Markdown report to FILE (e.g. $GITHUB_STEP_SUMMARY)")
    ap.add_argument("--list-rules", action="store_true", help="print the rule ids and exit")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__} ({CHECKED_AGAINST})")
    a = ap.parse_args(argv)
    if a.list_rules:
        for rid, (sev, desc) in RULES.items():
            print(f"{rid:<30} {sev:<8} {desc}")
        return 0
    if not a.paths:
        ap.error("give at least one path")
    ignore = [r.strip() for item in a.ignore for r in item.split(",") if r.strip()]
    unknown = [r for r in ignore if r not in RULES]
    if unknown:
        ap.error(f"unknown rule id(s): {', '.join(unknown)} (see --list-rules)")
    reports = []
    for p in a.paths:
        try:
            reports.append((p, validate_path(p, ignore)))
        except (OSError, InputError) as e:
            print(f"{p}: {e}", file=sys.stderr)
            return 2
    if a.format == "json":
        doc = {p: r for p, r in reports} if len(reports) > 1 else reports[0][1]
        json.dump(doc, sys.stdout, indent=1)
        sys.stdout.write("\n")
    elif a.format == "markdown":
        sys.stdout.write(_markdown(reports))
    else:
        for p, r in reports:
            (_github if a.format == "github" else _text)(p, r, sys.stdout)
    if a.summary:
        with open(a.summary, "a") as fh:
            fh.write(_markdown(reports))
    errors = sum(r["counts"]["error"] for _, r in reports)
    warnings = sum(r["counts"]["warning"] for _, r in reports)
    return 1 if errors or (a.strict and warnings) else 0


if __name__ == "__main__":
    sys.exit(main())
