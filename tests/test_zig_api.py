"""Tests for docs/tools/zig_api.zig source link generation."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import private_tmp  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
ZIG = shutil.which("zig")
ZIG_API = ROOT / "docs" / "tools" / "zig_api.zig"


@unittest.skipUnless(ZIG, "zig is not installed")
class ZigApiTests(unittest.TestCase):
    def _run_zig_api(self, out_dir: Path, extra_env: dict[str, str]) -> Path:
        env = os.environ.copy()
        env.pop("DOCS_SOURCE_REF", None)
        env.pop("GITHUB_SHA", None)
        env.update(extra_env)
        proc = subprocess.run(
            [
                ZIG,
                "run",
                str(ZIG_API),
                "--",
                "--module",
                "eessi-arch-guard",
                "--root",
                "zig/text.zig",
                "--out",
                str(out_dir),
                "--source-url",
                "https://github.com/stackhands/eessi-arch-guard/blob/{ref}/",
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        return out_dir / "eessi-arch-guard.md"

    def test_empty_ref_env_falls_back_to_github_sha_then_main(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            page_main = self._run_zig_api(
                root / "out-main",
                {"DOCS_SOURCE_REF": "", "GITHUB_SHA": ""},
            ).read_text(encoding="utf-8")
            self.assertIn(
                ":source: https://github.com/stackhands/eessi-arch-guard/blob/main/zig/text.zig",
                page_main,
            )

            page_sha = self._run_zig_api(
                root / "out-sha",
                {"DOCS_SOURCE_REF": "", "GITHUB_SHA": "abc1234"},
            ).read_text(encoding="utf-8")
            self.assertIn(
                ":source: https://github.com/stackhands/eessi-arch-guard/blob/abc1234/zig/text.zig",
                page_sha,
            )


if __name__ == "__main__":
    unittest.main()
