"""The usage text, the version line and the precedence of help and version."""

from __future__ import annotations

import os
import re
import unittest

import golden
import harness

ENV = {"PATH": "/usr/bin:/bin", "HOME": "/tmp", "LANG": "C", "LC_ALL": "C"}


def run(*args: str, stdin: str = "", env=None):
    return harness.run_with_timeout(
        [*harness.GUARD_ARGV, *args],
        env=ENV if env is None else env,
        input=stdin,
    )


class CliOutputTests(unittest.TestCase):
    def test_help_and_a_bare_run_match_the_expected_output(self) -> None:
        sheet = golden.Sheet("cli-output")
        self.addCleanup(sheet.finish)
        for label, args in (("help", ["--help"]), ("short help", ["-h"]), ("bare run", [])):
            with self.subTest(case=label):
                proc = run(*args)
                sheet.check(self, label, golden.render(proc.returncode, proc.stdout, proc.stderr))

    def test_restore_without_stdin_reports_a_parse_error(self) -> None:
        proc = run("--restore")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("restore failed:", proc.stderr)

    def test_help_overrides_restore(self) -> None:
        proc = run("--help", "--restore")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("usage: eessi-arch-guard", proc.stdout)
        self.assertEqual(proc.stderr, "")

    def test_version_overrides_restore(self) -> None:
        proc = run("--version", "--restore")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertRegex(proc.stdout, r"^\d+\.\d+\.\d+ \(.*\)\n\Z")
        self.assertEqual(proc.stderr, "")

    def test_the_environment_of_the_caller_does_not_change_the_bare_run(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("EESSI_", "SLURM_"))}
        for name in ("MODULEPATH", "LOADEDMODULES", "_LMFILES_", "_ModuleTable001_"):
            env.pop(name, None)
        proc = run(env=env)
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        self.assertIsNone(re.search(r"Traceback", proc.stderr))


if __name__ == "__main__":
    unittest.main()
