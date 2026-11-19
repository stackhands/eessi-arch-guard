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

    def test_site_map_names_bad_target_values(self) -> None:
        settings = {
            "submit_target.partition.cpu": "x86_64/amd/zen4 ",
            "submit_target.constraint.zen4": "x86_64/amd/zen4\n",
        }
        target, error = guard.submit_target(settings, "cpu", "")
        self.assertIsNone(target)
        self.assertIn("invalid target", error or "")
        self.assertIn("x86_64/amd/zen4 ", error or "")

        settings = {
            "submit_target.partition.cpu": ZEN4,
            "submit_target.constraint.zen4": "x86_64/amd/zen4\n",
        }
        target, error = guard.submit_target(settings, "cpu", "zen4")
        self.assertIsNone(target)
        self.assertIn("invalid target", error or "")
        self.assertIn("x86_64/amd/zen4", error or "")

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

    def _user_tree_case(self, root: Path, env_extra: dict) -> tuple[int, str]:
        prefix = root / "prefix"
        (prefix / "software" / "linux" / ZEN4 / "modules" / "all" / "R").mkdir(parents=True)
        (prefix / "software" / "linux" / ZEN4 / "modules" / "all" / "R" / "4.4.2.lua").write_text(
            "-- module\n", encoding="utf-8"
        )
        fake = _fake_eb_stack(root)
        env = {
            "PATH": "/usr/bin:/bin",
            "EESSI_PREFIX": str(prefix),
            "EESSI_OS_TYPE": "linux",
            "EESSI_VERSION": "2026.06",
            "LOADEDMODULES": "EESSI/2026.06:R/4.4.2:Mine/1.0",
            "EESSI_TEST_EB_STACK_LOG": str(root / "eb-stack.log"),
        }
        env.update(env_extra)
        stderr = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            code = guard.check_submit(
                env, {"submit_target.partition.cpu": ZEN4}, "cpu", "", str(fake)
            )
        return code, stderr.getvalue()

    @staticmethod
    def _install(base: Path, target: str, module: str) -> None:
        name, version = module.split("/")
        tree = base / "versions" / "2026.06" / "software" / "linux" / target
        lua = tree / "modules" / "all" / name
        lua.mkdir(parents=True, exist_ok=True)
        (lua / (version + ".lua")).write_text("-- module\n", encoding="utf-8")

    def test_a_module_in_the_users_eessi_extend_tree_for_the_target_is_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            user = root / "user-prefix"
            self._install(user, ZEN4, "Mine/1.0")
            code, err = self._user_tree_case(root, {"EESSI_USER_INSTALL": str(user)})
            self.assertEqual(code, 0, err)

    def test_a_module_built_only_for_another_target_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            self._install(project, ZEN2, "Mine/1.0")
            extra = {"EESSI_PROJECT_INSTALL": str(project), "HOME": str(root / "home")}
            code, err = self._user_tree_case(root, extra)
            self.assertEqual(code, 1)
            self.assertIn("Mine/1.0", err)

    def test_home_eessi_is_read_unless_a_user_prefix_is_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            self._install(home / "eessi", ZEN4, "Mine/1.0")
            code, err = self._user_tree_case(root, {"HOME": str(home)})
            self.assertEqual(code, 0, err)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            self._install(home / "eessi", ZEN4, "Mine/1.0")
            # A set but empty EESSI_USER_INSTALL is a user prefix to EESSI-extend,
            # so the default is not read.
            code, err = self._user_tree_case(root, {"HOME": str(home), "EESSI_USER_INSTALL": ""})
            self.assertEqual(code, 1)
            self.assertIn("Mine/1.0", err)

    def test_home_eessi_is_not_read_when_a_project_prefix_is_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            self._install(home / "eessi", ZEN4, "Mine/1.0")
            project = root / "project"
            project.mkdir()
            extra = {"HOME": str(home), "EESSI_PROJECT_INSTALL": str(project)}
            code, err = self._user_tree_case(root, extra)
            self.assertEqual(code, 1)
            self.assertIn("Mine/1.0", err)

    def test_without_eessi_version_only_the_eessi_tree_is_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            user = root / "user-prefix"
            self._install(user, ZEN4, "Mine/1.0")
            extra = {"EESSI_USER_INSTALL": str(user), "EESSI_VERSION": ""}
            code, err = self._user_tree_case(root, extra)
            self.assertEqual(code, 1)
            self.assertIn("Mine/1.0", err)

    def test_target_needs_no_site_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefix = root / "prefix"
            r_dir = prefix / "software" / "linux" / ZEN4 / "modules" / "all" / "R"
            r_dir.mkdir(parents=True)
            (r_dir / "4.4.2.lua").write_text("-- module\n", encoding="utf-8")
            self._install(root / "home" / "eessi", ZEN2, "Mine/1.0")
            fake = _fake_eb_stack(root)
            env = {
                "PATH": "/usr/bin:/bin",
                "HOME": str(root / "home"),
                "EESSI_PREFIX": str(prefix),
                "EESSI_OS_TYPE": "linux",
                "EESSI_VERSION": "2026.06",
                "LOADEDMODULES": "EESSI/2026.06:R/4.4.2:Mine/1.0",
                "EESSI_TEST_EB_STACK_LOG": str(root / "eb-stack.log"),
            }
            stderr = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
                code = guard.check_submit(env, None, "", "", str(fake), target_arg=ZEN4)
            self.assertEqual(code, 1)
            self.assertIn("Mine/1.0", stderr.getvalue())
            self.assertNotIn("R/4.4.2", stderr.getvalue())
            self.assertIn("stack arch --arch " + ZEN4, (root / "eb-stack.log").read_text())

    def test_target_is_refused_with_a_partition_or_an_unsafe_value(self) -> None:
        for partition, target in (("cpu", ZEN4), ("", "../zen4"), ("", "/abs")):
            with self.subTest(partition=partition, target=target):
                stderr = io.StringIO()
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
                    code = guard.check_submit({}, None, partition, "", "", target_arg=target)
                self.assertEqual(code, 2)
                self.assertIn("--target", stderr.getvalue())

    def test_cli_target_runs_without_a_site_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefix = root / "prefix"
            (prefix / "software" / "linux" / ZEN4 / "modules" / "all").mkdir(parents=True)
            self._install(root / "home" / "eessi", ZEN4, "Mine/1.0")
            self._install(root / "home" / "eessi", ZEN2, "OnlyZen2/1.0")
            fake = _fake_eb_stack(root)
            env = {
                "PATH": "/usr/bin:/bin",
                "HOME": str(root / "home"),
                "LANG": "C",
                "LC_ALL": "C",
                "EESSI_PREFIX": str(prefix),
                "EESSI_OS_TYPE": "linux",
                "EESSI_VERSION": "2026.06",
                "LOADEDMODULES": "EESSI/2026.06:Mine/1.0:OnlyZen2/1.0",
                "EESSI_TEST_EB_STACK_LOG": str(root / "eb-stack.log"),
            }
            proc = subprocess.run(
                [
                    *harness.GUARD_ARGV,
                    "--check-submit",
                    "--json",
                    "--target",
                    ZEN4,
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
            self.assertEqual(payload["missing"], ["OnlyZen2/1.0"])
            self.assertEqual(payload["target"], ZEN4)
            self.assertIsNone(payload["partition"])
            if jsonschema is not None:
                schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
                jsonschema.Draft202012Validator(schema).validate(payload)

    def test_missing_target_module_tree_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefix = root / "prefix"
            fake = _fake_eb_stack(root)
            env = {
                "PATH": "/usr/bin:/bin",
                "HOME": "/tmp",
                "LANG": "C",
                "LC_ALL": "C",
                "EESSI_PREFIX": str(prefix),
                "EESSI_OS_TYPE": "linux",
                "LOADEDMODULES": "EESSI/2025.06:R/4.4.2",
                "EESSI_TEST_EB_STACK_LOG": str(root / "eb-stack.log"),
            }
            conf = harness.PROGRAM.with_name("eessi-arch-guard.conf")
            conf.write_text("submit_target.partition.cpu = %s\n" % ZEN4, encoding="utf-8")
            subprocess.check_call(["sudo", "chown", "root:root", str(conf)])
            subprocess.check_call(["sudo", "chmod", "644", str(conf)])
            try:
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
                self.assertEqual(proc.returncode, 2, proc.stderr)
                payload = json.loads(proc.stdout)
                self.assertEqual(payload["schema_version"], 1)
                self.assertEqual(payload["exit"], 2)
                self.assertEqual(payload["exit_semantic"], "invalid")
                self.assertEqual(payload["target"], ZEN4)
                self.assertEqual(payload["loaded"], 1)
                self.assertEqual(payload["missing"], [])
                self.assertIsInstance(payload["message"], str)
                self.assertIn("module tree", payload["message"])
                self.assertIn("module tree", proc.stderr)
            finally:
                subprocess.check_call(["sudo", "rm", "-f", str(conf)])

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

    def test_unknown_target_is_invalid_even_without_app_modules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefix = root / "prefix"
            env = {
                "PATH": "/usr/bin:/bin",
                "EESSI_PREFIX": str(prefix),
                "EESSI_OS_TYPE": "linux",
                "LOADEDMODULES": "EESSI/2025.06",
            }
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = guard.check_submit(
                    env,
                    {"submit_target.partition.cpu": "x86_64/amd/zen404"},
                    "cpu",
                    "",
                    "/usr/bin/false",
                    True,
                )
            self.assertEqual(code, 2)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["exit_semantic"], "invalid")
            self.assertIn("x86_64/amd/zen404", payload["message"] or "")
            self.assertIn("x86_64/amd/zen404", stderr.getvalue())

    def test_duplicate_loaded_modules_are_invalid(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = guard.check_submit(
                {
                    "PATH": "/usr/bin:/bin",
                    "EESSI_PREFIX": "/tmp/prefix",
                    "EESSI_OS_TYPE": "linux",
                    "LOADEDMODULES": "EESSI/2025.06:R/4.4.2:R/4.4.2",
                },
                {"submit_target.partition.cpu": ZEN4},
                "cpu",
                "",
                "/usr/bin/false",
                True,
            )
        self.assertEqual(code, 2)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["exit_semantic"], "invalid")
        self.assertIn("duplicate", payload["message"] or "")
        self.assertIn("R/4.4.2", payload["message"] or "")
        self.assertIn("R/4.4.2", stderr.getvalue())

    def test_duplicate_or_malformed_eessi_loaded_modules_are_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefix = root / "prefix"
            (prefix / "software" / "linux" / ZEN4 / "modules" / "all" / "R").mkdir(parents=True)
            module_file = (
                prefix
                / "software"
                / "linux"
                / ZEN4
                / "modules"
                / "all"
                / "R"
                / "4.4.2.lua"
            )
            module_file.write_text("--\n", encoding="utf-8")
            fake = _fake_eb_stack(root)
            for loaded_mods, expected in (
                ("EESSI/2025.06:EESSI/2025.06:R/4.4.2", "duplicate module entry: EESSI/2025.06"),
                ("EESSI/2025.06:EESSI/2026.06:R/4.4.2", "invalid EESSI module entry"),
                ("EESSI/2025.06/extra:R/4.4.2", "invalid EESSI module entry"),
            ):
                with self.subTest(loaded_mods=loaded_mods):
                    proc = subprocess.run(
                        [
                            *harness.GUARD_ARGV,
                            "--check-submit",
                            "--json",
                            "--target",
                            ZEN4,
                            "--eb-stack",
                            str(fake),
                        ],
                        capture_output=True,
                        text=True,
                        env={
                            "PATH": "/usr/bin:/bin",
                            "HOME": str(root / "home"),
                            "LANG": "C",
                            "LC_ALL": "C",
                            "EESSI_PREFIX": str(prefix),
                            "EESSI_OS_TYPE": "linux",
                            "LOADEDMODULES": loaded_mods,
                            "EESSI_TEST_EB_STACK_LOG": str(root / "eb-stack.log"),
                        },
                        check=False,
                    )
                    self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                    payload = json.loads(proc.stdout)
                    self.assertEqual(payload["exit_semantic"], "invalid")
                    self.assertIn(expected, payload["message"] or "")
                    self.assertIn(expected, proc.stderr)

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
            self.assertTrue(
                "site config has no submit_target.partition.cpu mapping" in proc.stderr
                or "site config has no submit target mappings" in proc.stderr
            )
            if "config ignored:" in proc.stderr:
                self.assertIn("is a symlink and is ignored", proc.stderr)
        finally:
            conf.unlink(missing_ok=True)

    def test_cli_respects_tmpdir_strips_startup_vars_and_deduplicates_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefix = root / "prefix"
            modules = prefix / "software" / "linux" / ZEN4 / "modules" / "all"
            (modules / "R").mkdir(parents=True)
            (modules / "R" / "4.4.2").write_text("#%Module\n", encoding="utf-8")
            (modules / "R" / "4.4.2.lua").write_text("-- module\n", encoding="utf-8")
            custom_tmp = root / "custom-tmp"
            custom_tmp.mkdir()
            env_log = root / "eb-stack-env.json"
            fake = root / "eb-stack"
            fake.write_text(
                f"""#!/usr/bin/python3
import json, os, sys
args = sys.argv[1:]
ea = args[args.index("--easystack") + 1]
inst = args[args.index("--installed") + 1]
with open({str(env_log)!r}, "w", encoding="utf-8") as handle:
    json.dump({{
        "easystack_dir": os.path.dirname(ea),
        "startup": {{
            k: os.environ[k]
            for k in ("BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "BASH_XTRACEFD", "PS4")
            if k in os.environ
        }},
        "installed": open(inst, encoding="utf-8").read().splitlines(),
    }}, handle)
""",
                encoding="utf-8",
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
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
                    "TMPDIR": str(custom_tmp),
                    "BASH_ENV": "/nonexistent/bash_env",
                    "ENV": "/nonexistent/env",
                    "SHELLOPTS": "errexit",
                    "BASHOPTS": "cmdhist",
                    "BASH_XTRACEFD": "9",
                    "PS4": "+ ",
                    "LANG": "C",
                    "LC_ALL": "C",
                    "EESSI_PREFIX": str(prefix),
                    "EESSI_OS_TYPE": "linux",
                    "LOADEDMODULES": "EESSI/2025.06:R/4.4.2",
                }
                proc = subprocess.run(
                    [
                        *harness.GUARD_ARGV,
                        "--check-submit",
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
                self.assertEqual(proc.returncode, 0, proc.stderr)
                recorded = json.loads(env_log.read_text(encoding="utf-8"))
                self.assertEqual(Path(recorded["easystack_dir"]).parent, custom_tmp)
                self.assertEqual(recorded["startup"], {})
                self.assertEqual(len(recorded["installed"]), 1)
                self.assertFalse(Path(recorded["easystack_dir"]).exists())
            finally:
                subprocess.check_call(["sudo", "rm", "-f", str(conf)])


    def test_cli_counts_a_module_from_the_users_eessi_extend_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefix = root / "prefix"
            (prefix / "software" / "linux" / ZEN4 / "modules" / "all").mkdir(parents=True)
            self._install(root / "home" / "eessi", ZEN4, "Mine/1.0")
            self._install(root / "home" / "eessi", ZEN2, "OnlyZen2/1.0")
            fake = _fake_eb_stack(root)
            conf = harness.PROGRAM.with_name("eessi-arch-guard.conf")
            conf.write_text("submit_target.partition.cpu = %s\n" % ZEN4, encoding="utf-8")
            subprocess.check_call(["sudo", "chown", "root:root", str(conf)])
            subprocess.check_call(["sudo", "chmod", "644", str(conf)])
            try:
                env = {
                    "PATH": "/usr/bin:/bin",
                    "HOME": str(root / "home"),
                    "LANG": "C",
                    "LC_ALL": "C",
                    "EESSI_PREFIX": str(prefix),
                    "EESSI_OS_TYPE": "linux",
                    "EESSI_VERSION": "2026.06",
                    "LOADEDMODULES": "EESSI/2026.06:Mine/1.0:OnlyZen2/1.0",
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
                self.assertEqual(json.loads(proc.stdout)["missing"], ["OnlyZen2/1.0"])
            finally:
                subprocess.check_call(["sudo", "rm", "-f", str(conf)])


class CheckSubmitEmptyTargetTests(unittest.TestCase):
    def test_bare_or_empty_target_is_refused_as_target_mode(self) -> None:
        cases = (
            (["--check-submit", "--target"], "--target needs one safe relative EESSI target"),
            (["--check-submit", "--target", ""], "--target needs one safe relative EESSI target"),
            (
                ["--check-submit", "--target", "--partition", "cpu"],
                "--target is used without --partition and --constraint",
            ),
        )
        for argv, expected in cases:
            with self.subTest(argv=argv):
                proc = subprocess.run(
                    [*harness.GUARD_ARGV, *argv],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertIn(expected, proc.stderr)


class CheckSubmitZigParityTests(unittest.TestCase):
    def test_early_invalid_input_reports_loaded_null(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_missing_prefix = {
                "PATH": "/usr/bin:/bin",
                "HOME": str(root / "home"),
                "LANG": "C",
                "LC_ALL": "C",
                "EESSI_PREFIX": "",
                "EESSI_OS_TYPE": "linux",
                "LOADEDMODULES": "R/4.4.2",
            }
            proc = subprocess.run(
                [*harness.GUARD_ARGV, "--check-submit", "--json", "--target", ZEN4],
                capture_output=True,
                text=True,
                env=env_missing_prefix,
                check=False,
            )
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            self.assertIsNone(json.loads(proc.stdout)["loaded"])

            prefix = root / "prefix"
            (prefix / "software" / "linux" / ZEN4 / "modules" / "all").mkdir(parents=True)
            env_missing_eb = {
                "PATH": "/usr/bin:/bin",
                "HOME": str(root / "home"),
                "LANG": "C",
                "LC_ALL": "C",
                "EESSI_PREFIX": str(prefix),
                "EESSI_OS_TYPE": "linux",
                "LOADEDMODULES": "R/4.4.2",
            }
            proc_eb = subprocess.run(
                [
                    *harness.GUARD_ARGV,
                    "--check-submit",
                    "--json",
                    "--target",
                    ZEN4,
                    "--eb-stack",
                    str(root / "no-such-eb-stack"),
                ],
                capture_output=True,
                text=True,
                env=env_missing_eb,
                check=False,
            )
            self.assertEqual(proc_eb.returncode, 2, proc_eb.stdout + proc_eb.stderr)
            self.assertIsNone(json.loads(proc_eb.stdout)["loaded"])


    def test_eb_stack_silent_failure_and_exec_error_match_python(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefix = root / "prefix"
            mod_dir = prefix / "software" / "linux" / ZEN4 / "modules" / "all" / "R"
            mod_dir.mkdir(parents=True)
            (mod_dir / "4.4.2.lua").write_text("--\n", encoding="utf-8")

            silent = root / "eb-stack-silent"
            silent.write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
            silent.chmod(0o755)

            bad_exec = root / "eb-stack-bad-exec"
            bad_exec.write_text("not-a-script\n", encoding="utf-8")
            bad_exec.chmod(0o755)

            env = {
                "PATH": "/usr/bin:/bin",
                "HOME": str(root / "home"),
                "LANG": "C",
                "LC_ALL": "C",
                "EESSI_PREFIX": str(prefix),
                "EESSI_OS_TYPE": "linux",
                "LOADEDMODULES": "R/4.4.2",
            }
            proc_silent = subprocess.run(
                [
                    *harness.GUARD_ARGV,
                    "--check-submit",
                    "--json",
                    "--target",
                    ZEN4,
                    "--eb-stack",
                    str(silent),
                ],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            self.assertEqual(proc_silent.returncode, 2, proc_silent.stderr)
            self.assertIn("eb-stack failed: exit 2", proc_silent.stderr)
            self.assertEqual(json.loads(proc_silent.stdout)["message"], "eb-stack failed: exit 2")

            proc_exec = subprocess.run(
                [
                    *harness.GUARD_ARGV,
                    "--check-submit",
                    "--json",
                    "--target",
                    ZEN4,
                    "--eb-stack",
                    str(bad_exec),
                ],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            self.assertEqual(proc_exec.returncode, 2, proc_exec.stderr)
            self.assertIn("could not run eb-stack:", proc_exec.stderr)
            self.assertIn("Exec format error", proc_exec.stderr)
            self.assertIn("could not run eb-stack:", json.loads(proc_exec.stdout)["message"])

    def test_empty_eb_stack_flag_falls_back_to_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefix = root / "eessi"
            mod_dir = prefix / "software" / "linux" / ZEN4 / "modules" / "all" / "R"
            mod_dir.mkdir(parents=True)
            (mod_dir / "4.4.2.lua").write_text("-- R\n", encoding="utf-8")
            bindir = root / "bin"
            bindir.mkdir()
            _fake_eb_stack(bindir)
            log_path = root / "eb-stack.log"

            env_ok = {
                "PATH": f"{bindir}:/usr/bin:/bin",
                "LC_ALL": "C",
                "TMPDIR": tmp,
                "EESSI_PREFIX": str(prefix),
                "EESSI_OS_TYPE": "linux",
                "EESSI_TEST_EB_STACK_LOG": str(log_path),
                "LOADEDMODULES": "EESSI/2025.06:R/4.4.2",
            }
            for extra in (["--eb-stack="], ["--eb-stack", "--json"]):
                with self.subTest(extra=extra):
                    proc = harness.run_guard(
                        env_ok, ["--check-submit", "--target", ZEN4, *extra]
                    )
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertEqual(proc.stderr, "")

            env_missing = dict(env_ok, PATH="/usr/bin:/bin")
            proc_fail = harness.run_guard(
                env_missing, ["--check-submit", "--target", ZEN4, "--eb-stack", ""]
            )
            self.assertEqual(proc_fail.returncode, 2, proc_fail.stderr)
            self.assertEqual(
                proc_fail.stderr,
                "eessi-arch-guard: check-submit: eb-stack is not executable: eb-stack\n",
            )


if __name__ == "__main__":
    unittest.main()
