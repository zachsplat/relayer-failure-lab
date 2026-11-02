import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from nonceworks_validate import RULES, validate_files, validate_path  # noqa: E402
from nonceworks_validate.cli import main  # noqa: E402

FIX = os.path.join(HERE, "fixtures")
LAB = os.path.join(HERE, "..", "..", "scenarios")


def rules(rep, severity=None):
    return sorted({f["rule"] for f in rep["findings"] if severity is None or f["severity"] == severity})


def run_cli(*args):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(list(args))
    return rc, buf.getvalue()


class RelayerChecks(unittest.TestCase):
    def test_ok_config_has_no_errors_but_flags_912(self):
        rep = validate_path(os.path.join(FIX, "relayer-ok"))
        self.assertTrue(rep["ok"])
        self.assertEqual(rules(rep, "error"), [])
        self.assertEqual(rules(rep, "warning"), ["relayer-sign-endpoints"])
        self.assertIn("relayer-failed-status", rules(rep, "notice"))
        self.assertEqual(rep["secret_refs"], ["WEBHOOK_SIGNING_KEY"])

    def test_bad_config(self):
        rep = validate_path(os.path.join(FIX, "relayer-bad"))
        self.assertFalse(rep["ok"])
        self.assertEqual(rules(rep, "error"), ["relayer-unknown-notification", "relayer-unknown-signer", "relayer-webhook-key"])
        for r in ("relayer-gas-price-cap", "relayer-no-whitelist", "relayer-single-rpc", "relayer-plain-secret"):
            self.assertIn(r, rules(rep, "warning"))
        self.assertIn("relayer-local-signer", rules(rep, "notice"))

    def test_plain_secret_value_is_not_echoed(self):
        rep = validate_path(os.path.join(FIX, "relayer-bad"))
        text = json.dumps(rep)
        self.assertNotIn("hunter22", text)
        self.assertNotIn("short-key", text)

    def test_lab_scenario_configs_are_valid(self):
        for sid in ("817", "912", "843", "808"):
            rep = validate_path(os.path.join(LAB, sid, "config.json"))
            self.assertEqual(rules(rep, "error"), [], sid)
        self.assertIn("relayer-gas-price-cap", rules(validate_path(os.path.join(LAB, "808", "config.json"))))
        self.assertIn("relayer-sign-endpoints", rules(validate_path(os.path.join(LAB, "912", "config.json"))))

    def test_broken_json(self):
        rep = validate_files({"config.json": '{"relayers": ['})
        self.assertEqual(rules(rep, "error"), ["json-parse"])

    def test_nothing_recognised(self):
        rep = validate_files({"package.json": '{"name": "x"}'})
        self.assertEqual(rules(rep, "error"), ["no-config"])


class MonitorChecks(unittest.TestCase):
    def test_ok_tree(self):
        rep = validate_path(os.path.join(FIX, "monitor-ok"))
        self.assertEqual(rep["findings"], [])
        self.assertEqual([m["name"] for m in rep["monitors"]], ["Large transfers"])

    def test_bad_tree(self):
        rep = validate_path(os.path.join(FIX, "monitor-bad"))
        self.assertEqual(rules(rep, "error"), ["monitor-secret-type"])
        self.assertEqual(rules(rep, "warning"), ["monitor-cron", "monitor-filter-missing", "monitor-max-past-blocks",
                                                 "monitor-signature", "monitor-status-498", "monitor-unknown-network",
                                                 "monitor-unknown-trigger"])

    def test_zip_with_nested_root(self):
        with tempfile.TemporaryDirectory() as d:
            z = os.path.join(d, "export.zip")
            with zipfile.ZipFile(z, "w") as zf:
                for sub in ("monitors", "triggers", "networks"):
                    for name in os.listdir(os.path.join(FIX, "monitor-bad", sub)):
                        zf.write(os.path.join(FIX, "monitor-bad", sub, name), f"defender-export/config/{sub}/{name}")
                zf.writestr("__MACOSX/._x.json", "junk")
            rep = validate_path(z)
            self.assertIn("monitors/transfers.json", rep["files"])
            self.assertEqual(rules(rep, "error"), ["monitor-secret-type"])

    def test_zip_slip_refused(self):
        with tempfile.TemporaryDirectory() as d:
            z = os.path.join(d, "evil.zip")
            with zipfile.ZipFile(z, "w") as zf:
                zf.writestr("../../etc/x.json", "{}")
            from nonceworks_validate.core import InputError
            with self.assertRaises(InputError):
                validate_path(z)


class Cli(unittest.TestCase):
    def test_exit_codes(self):
        self.assertEqual(run_cli(os.path.join(FIX, "relayer-ok"))[0], 0)
        self.assertEqual(run_cli("--strict", os.path.join(FIX, "relayer-ok"))[0], 1)
        self.assertEqual(run_cli("--strict", "--ignore", "relayer-sign-endpoints", os.path.join(FIX, "relayer-ok"))[0], 0)
        self.assertEqual(run_cli(os.path.join(FIX, "relayer-bad"))[0], 1)
        self.assertEqual(run_cli(os.path.join(FIX, "does-not-exist"))[0], 2)

    def test_github_format(self):
        rc, out = run_cli("--format", "github", os.path.join(FIX, "relayer-bad"))
        self.assertEqual(rc, 1)
        self.assertIn("::error file=", out)
        self.assertIn("title=relayer-webhook-key::", out)

    def test_json_and_markdown(self):
        rc, out = run_cli("--format", "json", os.path.join(FIX, "monitor-ok"))
        self.assertEqual(json.loads(out)["counts"], {"error": 0, "warning": 0, "notice": 0})
        with tempfile.TemporaryDirectory() as d:
            s = os.path.join(d, "summary.md")
            run_cli("--summary", s, os.path.join(FIX, "relayer-bad"))
            with open(s) as fh:
                self.assertIn("| error | `relayer-webhook-key` |", fh.read())

    def test_rule_table_is_complete(self):
        rc, out = run_cli("--list-rules")
        self.assertEqual(rc, 0)
        self.assertEqual(len(out.strip().splitlines()), len(RULES))


def monitor_tree(cron="0 */1 * * * *", signature="Transfer(address,address,uint256)", tx=None):
    net = {"name": "Sepolia", "slug": "s", "network_type": "EVM", "chain_id": 1, "rpc_urls": [], "cron_schedule": cron, "max_past_blocks": 5}
    mon = {"name": "m", "networks": ["s"], "addresses": [], "triggers": [],
           "match_conditions": {"events": [{"signature": signature}], "functions": [], "transactions": tx or []}}
    return {"networks/s.json": json.dumps(net), "monitors/m.json": json.dumps(mon)}


class MonitorRuleDetails(unittest.TestCase):
    def test_cron_six_or_seven_fields_and_shortcuts(self):
        for cron in ("0 */1 * * * *", "0 0 12 * * * 2027", "@hourly", "@daily", "@weekly", "@monthly", "@yearly"):
            self.assertNotIn("monitor-cron", rules(validate_files(monitor_tree(cron=cron))), cron)
        for cron in ("*/1 * * * *", "@every5m", "0 0 0 0 0 0 0 0"):
            self.assertIn("monitor-cron", rules(validate_files(monitor_tree(cron=cron))), cron)
        msg = [f["message"] for f in validate_files(monitor_tree(cron="* * * * *"))["findings"] if f["rule"] == "monitor-cron"][0]
        self.assertIn("6 or 7 fields", msg)

    def test_signature_whitespace_is_fine_names_and_keywords_are_not(self):
        for sig in ("Transfer(address,address,uint256)", "Transfer(address, address, uint256)", " Swap( (address,uint256)[] , bytes32 )", "ping()"):
            self.assertNotIn("monitor-signature", rules(validate_files(monitor_tree(signature=sig))), sig)
        for sig in ("Transfer(address from, address to, uint256 value)", "Transfer(address indexed,address,uint256)",
                    "Swap((address a,uint256),bytes32)", "Transfer"):
            self.assertIn("monitor-signature", rules(validate_files(monitor_tree(signature=sig))), sig)
        msg = [f["message"] for f in validate_files(monitor_tree(signature="Transfer(address indexed,address,uint256)"))["findings"]
               if f["rule"] == "monitor-signature"][0]
        self.assertIn("address indexed", msg)
        self.assertNotIn("no spaces", msg)

    def test_status_498_text(self):
        rep = validate_files(monitor_tree(tx=[{"status": "Failure"}]))
        msg = [f["message"] for f in rep["findings"] if f["rule"] == "monitor-status-498"][0]
        self.assertIn("can miss failed transactions on Monitor 1.6.0 when another transaction in the block emits a log (#498, fixed in 1.7.0)", msg)
        self.assertIn("adding gas_used to the expression forces the receipt read", msg)
        self.assertNotIn("monitor-status-498", rules(validate_files(monitor_tree(tx=[{"status": "Failure", "expression": "gas_used > 0"}]))))


class NestedRootPaths(unittest.TestCase):
    def test_annotation_keeps_the_prefix(self):
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "export", "config")
            for sub in ("monitors", "triggers", "networks"):
                os.makedirs(os.path.join(root, sub))
                for name in os.listdir(os.path.join(FIX, "monitor-bad", sub)):
                    with open(os.path.join(FIX, "monitor-bad", sub, name)) as src, open(os.path.join(root, sub, name), "w") as dst:
                        dst.write(src.read())
            rep = validate_path(d)
            f = [f for f in rep["findings"] if f["rule"] == "monitor-secret-type"][0]
            self.assertEqual(f["file"], "triggers/webhook.json")
            self.assertEqual(f["path"], "export/config/triggers/webhook.json")
            rc, out = run_cli("--format", "github", d)
            self.assertIn(f"file={os.path.join(d, 'export/config/triggers/webhook.json')},", out)
            self.assertTrue(os.path.exists(os.path.join(d, "export/config/triggers/webhook.json")))
            rc, out = run_cli(d)
            self.assertIn("export/config/triggers/webhook.json:", out)


if __name__ == "__main__":
    unittest.main()
