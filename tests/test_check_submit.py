"""Read-only pre-submit checks against a mapped architecture."""

from __future__ import annotations

import contextlib
import io
import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import eessi_arch_guard as guard  # noqa: E402
import harness  # noqa: E402

try:
    import jsonschema
except ImportError:
    jsonschema = None

SCHEMA = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "orgmode"
    / "reference"
    / "json"
    / "check-submit.schema.json"
)

ZEN4 = "x86_64/amd/zen4"
ZEN2 = "x86_64/amd/zen2"


def _snapshot(root: Path) -> tuple[str, ...]:
    return tuple(sorted(str(path.relative_to(root)) for path in root.rglob("*")))


def _fake_eb_stack(root: Path) -> Path:
    script = root / "eb-stack"
    script.write_text(
        """#!/usr/bin/python3
import os
import sys

args = sys.argv[1:]
with open(os.environ["EESSI_TEST_EB_STACK_LOG"], "w", encoding="utf-8") as log:
    log.write(" ".join(args) + "\\n")
easystack = args[args.index("--easystack") + 1]
installed_path = args[args.index("--installed") + 1]
with open(easystack, encoding="utf-8") as handle:
    files = [line.strip()[2:] for line in handle if line.startswith("  - ")]
with open(installed_path, encoding="utf-8") as handle:
    installed = {line.strip().replace("/", "-") + ".eb" for line in handle if line.strip()}
missing = [name for name in files if name not in installed]
for name in missing:
    print(f"{name} is flagged on x86_64: it has never been installed there")
print(f"parse_failed=0 unsupported=0 flagged={len(missing)}")
raise SystemExit(1 if missing else 0)
""",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


class CheckSubmitTests(unittest.TestCase):
    def test_site_map_requires_matching_partition_and_constraint(self) -> None:
        settings = {
            "submit_target.partition.cpu": ZEN4,
            "submit_target.constraint.zen4": ZEN4,
            "submit_target.constraint.zen2": ZEN2,
        }
        self.assertEqual(guard.submit_target(settings, "cpu", "zen4"), (ZEN4, None))
        target, error = guard.submit_target(settings, "cpu", "zen2")
        self.assertIsNone(target)
        self.assertIn("different targets", error or "")
        target, error = guard.submit_target(settings, "missing", "")
        self.assertIsNone(target)
        self.assertIn("no submit_target.partition.missing", error or "")

    def test_check_submit_names_missing_module_without_changing_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefix = root / "prefix"
            modules = prefix / "software" / "linux" / ZEN4 / "modules" / "all"
            (modules / "R").mkdir(parents=True)
            (modules / "R" / "4.4.2.lua").write_text("-- module\n", encoding="utf-8")
            fake = _fake_eb_stack(root)
            log = root / "eb-stack.log"
            env = {
                "PATH": "/usr/bin:/bin",
                "EESSI_PREFIX": str(prefix),
                "EESSI_OS_TYPE": "linux",
                "LOADEDMODULES": "EESSI/2025.06:R/4.4.2:OnlyOther/1.0",
                "EESSI_TEST_EB_STACK_LOG": str(log),
            }
            settings = {
                "submit_target.partition.cpu": ZEN4,
                "submit_target.constraint.zen4": ZEN4,
            }
            before = _snapshot(prefix)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = guard.check_submit(env, settings, "cpu", "zen4", str(fake))
            self.assertEqual(code, 1)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("loaded module absent", stderr.getvalue())
            self.assertIn("OnlyOther/1.0", stderr.getvalue())
            self.assertNotIn("R/4.4.2", stderr.getvalue())
            self.assertEqual(_snapshot(prefix), before)
            invocation = log.read_text(encoding="utf-8")
            self.assertIn("stack arch --arch " + ZEN4, invocation)
            self.assertIn("--easystack", invocation)
            self.assertIn("--installed", invocation)
            self.assertIn("--unsupported", invocation)

    def test_all_loaded_modules_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefix = root / "prefix"
            modules = prefix / "software" / "linux" / ZEN4 / "modules" / "all"
            (modules / "R").mkdir(parents=True)
            (modules / "R" / "4.4.2").write_text("#%Module\n", encoding="utf-8")
            (modules / "2025").write_text("#%Module\n", encoding="utf-8")
            (modules / "Compiler" / "GCCcore").mkdir(parents=True)
            (modules / "Compiler" / "GCCcore" / "15.2.0").write_text(
                "#%Module\n", encoding="utf-8"
            )
            fake = _fake_eb_stack(root)
            env = {
                "PATH": "/usr/bin:/bin",
                "EESSI_PREFIX": str(prefix),
                "EESSI_OS_TYPE": "linux",
                "LOADEDMODULES": "EESSI/2025.06:2025:Compiler/GCCcore/15.2.0:R/4.4.2",
                "EESSI_TEST_EB_STACK_LOG": str(root / "eb-stack.log"),
            }
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = guard.check_submit(
                    env,
                    {"submit_target.partition.cpu": ZEN4},
                    "cpu",
                    "",
                    str(fake),
                )
            self.assertEqual(code, 0)
            self.assertIn("3 loaded module(s) available", stdout.getvalue())

    def test_cli_arguments_are_opt_in(self) -> None:
        plain = guard.parse_args([])
        self.assertFalse(plain.check_submit)
        args = guard.parse_args(
            [
                "--check-submit",
                "--partition",
                "cpu",
                "--constraint=zen4",
                "--eb-stack",
                "/opt/bin/eb-stack",
            ]
        )
        self.assertTrue(args.check_submit)
        self.assertEqual(args.partition, "cpu")
        self.assertEqual(args.constraint, "zen4")
        self.assertEqual(args.eb_stack, "/opt/bin/eb-stack")
        self.assertTrue(guard.parse_args(["--check-submit", "--json"]).json)

    def test_json_reports_missing_modules_with_schema_version_1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefix = root / "prefix"
            modules = prefix / "software" / "linux" / ZEN4 / "modules" / "all"
            (modules / "R").mkdir(parents=True)
            (modules / "R" / "4.4.2.lua").write_text("-- module\n", encoding="utf-8")
            fake = _fake_eb_stack(root)
            env = {
                "PATH": "/usr/bin:/bin",
                "EESSI_PREFIX": str(prefix),
                "EESSI_OS_TYPE": "linux",
                "LOADEDMODULES": "EESSI/2025.06:R/4.4.2:OnlyOther/1.0",
                "EESSI_TEST_EB_STACK_LOG": str(root / "eb-stack.log"),
            }
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = guard.check_submit(
                    env,
                    {"submit_target.partition.cpu": ZEN4},
                    "cpu",
                    "",
                    str(fake),
                    True,
                )
            self.assertEqual(code, 1)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["kind"], "check-submit")
            self.assertEqual(payload["partition"], "cpu")
            self.assertIsNone(payload["constraint"])
            self.assertEqual(payload["target"], ZEN4)
            self.assertEqual(payload["loaded"], 2)
            self.assertEqual(payload["missing"], ["OnlyOther/1.0"])
            self.assertIsNone(payload["message"])
            self.assertEqual(payload["exit"], 1)
            self.assertEqual(payload["exit_semantic"], "missing")
            self.assertEqual(stdout.getvalue(), json.dumps(payload, sort_keys=True) + "\n")
            self.assertIn("OnlyOther/1.0", stderr.getvalue())
            self.assertNotIn("loaded module(s) available", stdout.getvalue())
            if jsonschema is not None:
                schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
                jsonschema.Draft202012Validator(schema).validate(payload)

    def test_json_invalid_partition_is_exit_semantic_invalid(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = guard.check_submit({}, {}, "bad name", "", "", True)
        self.assertEqual(code, 2)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["exit_semantic"], "invalid")
        self.assertEqual(payload["exit"], 2)
        self.assertIsNone(payload["target"])
        self.assertIsNone(payload["loaded"])
        self.assertEqual(payload["missing"], [])
        self.assertIn("safe partition", payload["message"])
        self.assertIn("safe partition", stderr.getvalue())

    def test_cli_json_uses_eb_stack_and_matches_the_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefix = root / "prefix"
            modules = prefix / "software" / "linux" / ZEN4 / "modules" / "all"
            (modules / "R").mkdir(parents=True)
            (modules / "R" / "4.4.2.lua").write_text("-- module\n", encoding="utf-8")
            fake = _fake_eb_stack(root)
            conf = harness.PROGRAM.with_name("eessi-arch-guard.conf")
            conf.write_text(
                "submit_target.partition.cpu = %s\n" % ZEN4,
                encoding="utf-8",
            )
            subprocess.check_call(["sudo", "chown", "root:root", str(conf)])
            subprocess.check_call(["sudo", "chmod", "644", str(conf)])
            try:
                env = {
                    "PATH": "/usr/bin:/bin",
                    "HOME": "/tmp",
                    "LANG": "C",
                    "LC_ALL": "C",
                    "EESSI_PREFIX": str(prefix),
                    "EESSI_OS_TYPE": "linux",
                    "LOADEDMODULES": "EESSI/2025.06:R/4.4.2:OnlyOther/1.0",
                    "EESSI_TEST_EB_STACK_LOG": str(root / "eb-stack.log"),
                }
                proc = subprocess.run(
                    [
                        *harness.GUARD_ARGV,
                        "--check-submit",
                        "--json",
                        "--partition",
                        "cpu",
                        "--eb-stack",
                        str(fake),
                    ],
                    capture_output=True,
                    text=True,
                    env=env,
                    check=False,
                )
                self.assertEqual(proc.returncode, 1, proc.stderr)
                payload = json.loads(proc.stdout)
                self.assertEqual(payload["schema_version"], 1)
                self.assertEqual(payload["kind"], "check-submit")
                self.assertEqual(payload["missing"], ["OnlyOther/1.0"])
                self.assertEqual(payload["exit_semantic"], "missing")
                self.assertIn("stack arch --arch " + ZEN4, (root / "eb-stack.log").read_text())
                self.assertIn("--unsupported", (root / "eb-stack.log").read_text())
                self.assertIn("OnlyOther/1.0", proc.stderr)
                if jsonschema is not None:
                    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
                    jsonschema.Draft202012Validator(schema).validate(payload)
            finally:
                subprocess.check_call(["sudo", "rm", "-f", str(conf)])

    def test_check_submit_warns_on_dangling_config_symlink(self) -> None:
        conf = harness.PROGRAM.with_name("eessi-arch-guard.conf")
        conf.unlink(missing_ok=True)
        conf.symlink_to("missing-site-config.conf")
        try:
            env = {
                "PATH": "/usr/bin:/bin",
                "HOME": "/tmp",
                "LANG": "C",
                "LC_ALL": "C",
            }
            proc = subprocess.run(
                [
                    *harness.GUARD_ARGV,
                    "--check-submit",
                    "--partition",
                    "cpu",
                ],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            self.assertEqual(proc.returncode, 2)
            self.assertIn("config ignored:", proc.stderr)
            self.assertIn("is a symlink and is ignored", proc.stderr)
            self.assertTrue(
                "site config has no submit_target.partition.cpu mapping" in proc.stderr
                or "site config has no submit target mappings" in proc.stderr
            )
        finally:
            conf.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
