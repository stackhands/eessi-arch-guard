from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

import harness

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "cli_output_compat"
BASE_FIXTURE_COMMIT = "b8f42a6df68de57b49f987af25e55154556b098a"


def _stable_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        if key.startswith(("EESSI_", "SLURM_")):
            env.pop(key, None)
    for key in ("MODULEPATH", "LOADEDMODULES", "_LMFILES_", "_ModuleTable001_"):
        env.pop(key, None)
    return env


class CliOutputCompatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = Path(__file__).resolve().parents[1]

    def _run_script(self, script: Path, *args: str) -> tuple[int, bytes, bytes]:
        cp = subprocess.run(
            [sys.executable, "-I", str(script), *args],
            cwd=self.repo,
            env=_stable_env(),
            capture_output=True,
            check=False,
        )
        return cp.returncode, cp.stdout, cp.stderr

    def _load_fixture(self, stem: str) -> tuple[int, bytes, bytes]:
        # Regenerate these fixtures from BASE_FIXTURE_COMMIT in a full clone:
        # python tests/regenerate_cli_output_compat_fixtures.py
        rc = int((FIXTURES_DIR / f"{stem}.returncode").read_text(encoding="utf-8").strip())
        out = (FIXTURES_DIR / f"{stem}.stdout").read_bytes()
        err = (FIXTURES_DIR / f"{stem}.stderr").read_bytes()
        return rc, out, err

    def test_default_help_output_matches_main(self) -> None:
        current = self.repo / "src" / "eessi_arch_guard.py"
        self.assertEqual(self._run_script(current, "--help"), self._load_fixture("help"))

    def test_default_run_output_matches_main(self) -> None:
        current = self.repo / "src" / "eessi_arch_guard.py"
        self.assertEqual(self._run_script(current), self._load_fixture("run"))

    def test_restore_malformed_stdin_matches_python(self) -> None:
        env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
        cases = (
            ("", "eessi-arch-guard: restore failed: Expecting value: line 1 column 1 (char 0)\n"),
            (
                "{not json",
                "eessi-arch-guard: restore failed: "
                "Expecting property name enclosed in double quotes: line 1 column 2 (char 1)\n",
            ),
            (
                "[]",
                "eessi-arch-guard: restore failed: 'list' object has no attribute 'get'\n",
            ),
            ("NaN", "eessi-arch-guard: restore failed: NaN is not JSON\n"),
            (
                b"\xff",
                b"eessi-arch-guard: restore failed: "
                b"'utf-8' codec can't decode byte 0xff in position 0: invalid start byte\n",
            ),
        )
        for payload, expected in cases:
            with self.subTest(payload=payload):
                proc = harness.run_guard(env, ["--restore"], input=payload)
                self.assertEqual(proc.returncode, 1)
                self.assertEqual(proc.stderr, expected)
