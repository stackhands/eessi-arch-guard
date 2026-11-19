"""A test that reads EB_STACK_BIN runs only where the variable is set.

The ci workflow has to set it, for every such test, to a pinned release.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
HEX64 = re.compile(r"\b[0-9a-f]{64}\b")


def job_text() -> str:
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    if "  real-eb-stack:" not in lines:
        raise AssertionError("ci.yml has no real-eb-stack job")
    start = lines.index("  real-eb-stack:")
    body = []
    for line in lines[start + 1 :]:
        if re.fullmatch(r"  [A-Za-z0-9_-]+:", line):
            break
        body.append(line)
    return "\n".join(body)


def tests_reading_the_variable() -> list[str]:
    found = []
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        if path.name != Path(__file__).name and "EB_STACK_BIN" in path.read_text(encoding="utf-8"):
            found.append(path.name)
    return found


class RealEbStackCi(unittest.TestCase):
    def test_the_job_runs_every_test_that_reads_the_variable(self) -> None:
        job = job_text()
        names = tests_reading_the_variable()
        self.assertTrue(names)
        for name in names:
            self.assertIn("-p '%s'" % name, job, "the job does not run tests/%s" % name)
        for pattern in re.findall(r"-p '([^']+)'", job):
            self.assertIn(
                pattern, names, "the job runs %s, which does not read EB_STACK_BIN" % pattern
            )

    def test_the_job_covers_both_builds(self) -> None:
        job = job_text()
        self.assertIn("zig build", job)
        self.assertIn('TOOL_BIN="$PWD/zig-out/bin/eessi-arch-guard"', job)

    def test_the_job_installs_a_pinned_release_and_exports_it(self) -> None:
        job = job_text()
        self.assertIn("https://github.com/stackhands/eb-stack/releases/download/v0.8.0/", job)
        check = [line for line in job.splitlines() if "sha256sum --check" in line]
        self.assertTrue(check, "the job does not check the archive")
        self.assertTrue(HEX64.search(check[0]), "the checksum is not a SHA-256: %s" % check[0])
        export = job.find("EB_STACK_BIN=")
        self.assertGreaterEqual(export, 0, "the job does not export EB_STACK_BIN")
        self.assertIn('>> "$GITHUB_ENV"', job)
        self.assertLess(export, job.find("unittest"), "EB_STACK_BIN is set after the tests ran")
        self.assertIn('grep -Fx "eb-stack 0.8.0"', job)


if __name__ == "__main__":
    unittest.main()
