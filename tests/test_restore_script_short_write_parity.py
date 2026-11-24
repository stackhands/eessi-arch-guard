"""The Python and Zig restore helpers must treat a short script write alike.

The restore helper writes the shell code Lmod printed to a private file with
one os.write and runs it. When a size limit stops that write short, Python
does not raise: the file holds the first bytes of the code and bash runs
what is there. The Zig helper looped until every byte was written, so a
short write became an error and the restore failed where Python goes on.

The helper is run alone, as the restore parent runs it, with the request on
standard input, each program on a fresh site. The shared size limiter caps
the size of any file the helper may write, so the kernel answers with a
short write. The Lmod command prints a fixed script, so the point of the cut
does not depend on a path or on timing: the limit lies inside a comment
line, above the size of the wrapper script, and the line that sets the
marker variable comes after it. The comparison skips when TOOL_BIN is unset.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from typing import Dict, Mapping, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import differential  # noqa: E402
import harness  # noqa: E402

ZEN4 = "x86_64/amd/zen4"
LIMIT = 100
FRAGMENT = "export LEAD=1\\n# " + "x" * 120 + "\\nexport MARKER=full\\n"


def build(site: harness.Site) -> Dict[str, str]:
    site.add_cpu(ZEN4)
    site.add_module(ZEN4, "R/4.4.2")
    base = os.path.join(site.root, "workbase")
    os.mkdir(base, 0o700)
    lmod = Path(site.root, "fixed-lmod")
    lmod.write_text("#!/bin/sh\nprintf '" + FRAGMENT + "'\n", encoding="utf-8")
    lmod.chmod(0o755)
    env = harness.base_env(site, ZEN4, ["R/4.4.2"], gpu="none")
    env["TMPDIR"] = base
    env["LMOD_CMD"] = str(lmod)
    return env


def request(env: Mapping[str, str]) -> str:
    return json.dumps(
        {
            "v": 1,
            "env": dict(env),
            "eessi": "EESSI/2025.06",
            "modules": [],
            "lmod": env["LMOD_CMD"],
            "pin": "",
        }
    )


def exported(stdout: str) -> Optional[Dict[str, str]]:
    return json.loads(stdout).get("env")


class RestoreScriptShortWriteParityTests(unittest.TestCase):
    def _python(self, limit: Optional[int]) -> differential.Result:
        return differential.run_fresh(
            differential.python_guard(), build, ["--restore"], limit, request
        )

    def test_python_helper_goes_on_with_the_short_script(self) -> None:
        full = self._python(None)
        self.assertEqual(full.returncode, 0, full.stderr)
        self.assertEqual(exported(full.stdout)["MARKER"], "full")
        short = self._python(LIMIT)
        self.assertEqual(short.returncode, 0, short.stderr)
        env = exported(short.stdout)
        self.assertEqual(env["LEAD"], "1")
        self.assertNotIn("MARKER", env)

    def test_programs_agree_when_a_script_is_written_short(self) -> None:
        harness.skip_unless_tool_bin(self)
        differential.assert_agree(self, build, ["--restore"], LIMIT, request)


if __name__ == "__main__":
    unittest.main()
