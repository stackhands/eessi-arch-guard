"""--json decision documents validate against the published JSON Schema."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

import harness

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "docs" / "orgmode" / "reference" / "json"
DECISION_SCHEMA = SCHEMA_DIR / "decision.schema.json"
DRY_RUN_SCHEMA = SCHEMA_DIR / "dry-run.schema.json"
RESTORE_SCHEMA = SCHEMA_DIR / "restore-reply.schema.json"
ZEN2 = "x86_64/amd/zen2"
ZEN4 = "x86_64/amd/zen4"

try:
    import jsonschema

    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False


def _validator(schema_path: Path):
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema)


@unittest.skipUnless(HAS_JSONSCHEMA, "jsonschema is not installed")
class JsonOutputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.decision_validator = _validator(DECISION_SCHEMA)
        cls.dry_run_validator = _validator(DRY_RUN_SCHEMA)
        cls.restore_validator = _validator(RESTORE_SCHEMA)

    def run_guard(self, env, *flags):
        proc = subprocess.run(
            [*harness.GUARD_ARGV, *flags],
            env=env,
            capture_output=True,
            check=False,
        )
        return proc

    def test_opt_out_json_validates(self):
        proc = self.run_guard({"EESSI_ARCH_GUARD": "0"}, "--json")
        self.assertEqual(proc.returncode, 0)
        doc = json.loads(proc.stdout)
        self.decision_validator.validate(doc)
        self.assertFalse(doc["reload_needed"])
        self.assertEqual(doc["decision"], "skip")
        self.assertEqual(doc["exit_semantic"], "ok")

    def test_empty_software_subdir_emits_null_was(self):
        proc = self.run_guard({"EESSI_SOFTWARE_SUBDIR": ""}, "--json")
        self.assertEqual(proc.returncode, 0)
        doc = json.loads(proc.stdout)
        self.decision_validator.validate(doc)
        self.assertEqual(doc["decision"], "skip")
        self.assertIsNone(doc["was"])

    def test_json_on_mismatch_reports_reload(self):
        site = harness.Site()
        try:
            site.add_cpu(ZEN2)
            site.add_cpu(ZEN4)
            site.write_cpuinfo("zen2")
            site.add_module(ZEN4, "R/4.4.2")
            site.add_module(ZEN2, "R/4.4.2")
            env = harness.base_env(site, ZEN4, ["R/4.4.2"], gpu="none")
            proc = self.run_guard(env, "--json")
            self.assertEqual(proc.returncode, 0, proc.stderr.decode())
            doc = json.loads(proc.stdout)
            self.decision_validator.validate(doc)
            self.assertEqual(doc["decision"], "reloaded")
            self.assertTrue(doc["reload_needed"])
            self.assertIn(ZEN2, doc["now"] or "")
            self.assertNotIn(b"export ", proc.stdout)
        finally:
            site.close()

    def test_strict_failure_exit_semantic(self):
        site = harness.Site()
        try:
            site.add_cpu(ZEN2)
            site.write_cpuinfo("zen2")
            site.add_module(ZEN4, "R/4.4.2")
            env = harness.base_env(site, ZEN4, ["R/4.4.2"], gpu="none")
            env.pop("LMOD_CMD", None)
            proc = self.run_guard(env, "--json", "--strict")
            self.assertEqual(proc.returncode, 78)
            doc = json.loads(proc.stdout)
            self.decision_validator.validate(doc)
            self.assertEqual(doc["decision"], "failed")
            self.assertTrue(doc["reload_needed"])
            self.assertEqual(doc["exit"], 78)
            self.assertEqual(doc["exit_semantic"], "strict-failure")
        finally:
            site.close()

    def test_dry_run_json_validates(self):
        site = harness.Site()
        try:
            site.add_cpu(ZEN2)
            site.add_cpu(ZEN4)
            site.write_cpuinfo("zen2")
            site.add_module(ZEN4, "R/4.4.2")
            site.add_module(ZEN2, "R/4.4.2")
            env = harness.base_env(site, ZEN4, ["R/4.4.2"], gpu="none")
            proc = self.run_guard(env, "--dry-run", "--json")
            self.assertEqual(proc.returncode, 0, proc.stderr.decode())
            doc = json.loads(proc.stdout)
            self.dry_run_validator.validate(doc)
            self.assertEqual(doc["kind"], "decision")
            self.assertEqual(doc["mode"], "prolog")
            self.assertFalse(doc["shell"])
            self.assertIn("config", doc)
            self.assertIn("chain", doc)
            self.assertIn("cache", doc)
            self.assertIn("syslog", doc)
            self.assertEqual(doc["schema_version"], 1)
        finally:
            site.close()

    def test_prolog_lines_unchanged_without_json(self):
        site = harness.Site()
        try:
            site.add_cpu(ZEN2)
            site.add_cpu(ZEN4)
            site.write_cpuinfo("zen2")
            site.add_module(ZEN4, "R/4.4.2")
            site.add_module(ZEN2, "R/4.4.2")
            env = harness.base_env(site, ZEN4, ["R/4.4.2"], gpu="none")
            plain = self.run_guard(env)
            self.assertIn(b"export ", plain.stdout)
            with_json = self.run_guard(env, "--json")
            self.assertNotIn(b"export ", with_json.stdout)
            self.assertTrue(with_json.stdout.strip().startswith(b"{"))
        finally:
            site.close()

    def test_restore_reply_validates_fixture(self):
        sys.path.insert(0, str(ROOT / "src"))
        import eessi_arch_guard as guard  # noqa: E402
        sample = guard.do_restore(
            {
                "env": {"PATH": guard.to_wire("/bin")},
                "eessi": guard.to_wire("EESSI/2026"),
                "modules": [],
                "lmod": guard.to_wire("/bin/false"),
                "pin": "",
            }
        )
        self.restore_validator.validate(sample)


if __name__ == "__main__":
    unittest.main()
