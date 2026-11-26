"""The decision for cpupath, override, and accelerator inputs.

Each case runs the guard on one environment and pins exit status, standard
output, and standard error in a golden sheet, ``golden/env-<test name>.txt``.
The cases are a fixed table over a generated fake tree, with a stand-in for
eessi_archdetect.sh that prints what a test variable holds. Anchor rows
state the outcome of a few cases in words as well, so that a sheet cannot
record a wrong answer unnoticed, and each table test requires that its cases
end in several different outcomes.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import golden  # noqa: E402
import harness  # noqa: E402
import retire_b_support as support  # noqa: E402

ZEN2 = "x86_64/amd/zen2"
ZEN4 = "x86_64/amd/zen4"
GENERIC = "x86_64/generic"
CC80 = "accel/nvidia/cc80"
CC90 = "accel/nvidia/cc90"
GOOD_CPUPATH = ZEN2 + ":" + GENERIC
RUN_TIMEOUT = 20.0

DRY = ("--dry-run",)
DRY_JSON = ("--dry-run", "--json")
LIVE: Tuple[str, ...] = ()

ARCHDETECT_STUB = """#!/bin/bash
if [ "$1" = "-a" ] && [ "$2" = "cpupath" ]; then
    printf '%s' "$EESSI_TEST_CPUPATH"
    exit 0
fi
if [ "$1" = "accelpath" ]; then
    printf '%s' "$EESSI_TEST_ACCEL"
    exit "${EESSI_TEST_ACCEL_RC:-0}"
fi
exit 64
"""

CPUPATHS = [
    GOOD_CPUPATH,
    GOOD_CPUPATH + ":",
    ":" + ZEN2,
    ZEN2 + "::" + GENERIC,
    ZEN2 + ":" + ZEN2 + ":" + GENERIC,
    GENERIC + ":" + GENERIC,
    ZEN2 + ":" + ZEN4,
    ZEN4 + ":" + ZEN2,
    ZEN2 + ":bogus/leaf:" + GENERIC,
    "bogus/leaf:" + ZEN2,
    ZEN2 + ":" * 3,
    ":",
    "::",
    "",
    "\n",
    ZEN2 + "\n",
    ZEN2 + "\n\n",
    "\n" + ZEN2 + "\n",
    ZEN2 + "\n" + GENERIC + "\n",
    ZEN2 + "\r\n",
    ZEN2 + "\r",
    ZEN2 + "\x0b",
    ZEN2 + "\x0c" + GENERIC,
    ZEN2 + "\x1c",
    ZEN2 + "\x1f",
    ZEN2 + "\x85",
    ZEN2 + "\u2028",
    ZEN2 + "\u2029x",
    ZEN2 + "\t",
    " " + ZEN2,
    ZEN2 + " ",
    ZEN2 + " :" + GENERIC,
    ZEN2 + "/",
    ZEN2 + "/:" + GENERIC,
    "x86_64//amd/zen2",
    "/" + ZEN2,
    "./" + ZEN2,
    "x86_64/amd/../amd/zen2",
    ZEN2 + "\\",
    "x86_64/amd/zen\u00e92",
    ZEN2 + "\u00a0",
    ZEN2.upper(),
    ZEN2 + ":+:" + GENERIC,
    ZEN2 + ":.:" + GENERIC,
    ZEN2 + ":..",
    ZEN2 + "\udcff:" + GENERIC,
    "\udcff" + ZEN2,
    (ZEN2 + ":") * 50 + GENERIC,
    "a" * 65536,
    "a" * 65537,
]
# These also run in prolog mode, which reloads through the fake Lmod when the
# decision is to change targets.
CPUPATH_LIVE = {0, 3, 8, 9, 13, 18, 33}
CPUPATH_JSON = {0, 13, 33}

OVERRIDES = [
    "",
    " ",
    "\t",
    "\n",
    "\u00a0",
    ZEN2,
    GENERIC,
    ZEN2 + "/",
    " " + ZEN2,
    ZEN2 + " ",
    ZEN2 + "\n",
    ZEN2 + "\u00a0",
    ZEN2 + "\u3000",
    ZEN2 + "\x1f",
    ZEN2 + "\x1c",
    ZEN2 + "\x85",
    ZEN2 + "\x0b",
    ZEN2 + "\u2009",
    ZEN2 + "\u200b",
    ZEN2 + "\ufeff",
    ZEN2 + "\udcff",
    "x86_64/amd/unknown",
    ZEN2 + "\ny",
    "x86_64//amd/zen2",
    "x86_64/amd//zen2",
    "./" + ZEN2,
    "/" + ZEN2,
    "../x",
    "x86_64/amd/..",
    ZEN2 + "/..",
    "x86_64",
    "x86_64/amd",
    ZEN2 + ":" + GENERIC,
    "x86_64/AMD/zen2",
    ZEN2 + "/software",
    "x86_64/amd/zen\u00e92",
    ZEN2 + "\\",
]
OVERRIDE_LIVE = {0, 5, 7, 13, 21, 25}

SWITCH_VALUES = ("", " ", "1", "\u00a0")
SWITCHES = (
    "EESSI_ARCHDETECT_OPTIONS_OVERRIDE",
    "EESSI_ACCELERATOR_TARGET_OVERRIDE",
    "EESSI_ARCH_GUARD",
    "EESSI_ARCH_GUARD_MISSING",
)
ARCHDETECT_OPTIONS = ("", " ", ZEN4, "\n", "x86_64/amd/zen2:x86_64/generic")
LOADED_SUBDIRS = ("", " ", ZEN2, ZEN2 + "/", ZEN4 + " ", "bogus")

# Layouts the install lookup has to read the same way: a directory without a
# software directory, a software entry that is a file, a symbolic link to an
# installed target, and a dangling link.
LAYOUT_CPUPATHS = (
    "x86_64/amd/nosoft:" + ZEN2,
    "x86_64/amd/filesoft:" + ZEN2,
    "x86_64/amd/linkzen2",
    "x86_64/amd/linkzen2:" + ZEN2,
    "x86_64/amd/dangling:" + GENERIC,
    "x86_64/amd/nosoft",
    "x86_64/amd/filesoft",
)
LAYOUT_OVERRIDES = ("x86_64/amd/nosoft", "x86_64/amd/filesoft", "x86_64/amd/linkzen2")

ACCEL_OUTPUTS = [
    "",
    "\n",
    " ",
    CC90,
    CC90 + "\n",
    CC90 + "\n\n",
    " " + CC90 + " ",
    CC90 + ":",
    CC90 + ":" + CC80,
    "accel/nvidia/cc",
    "accel/nvidia/cc9x",
    "accel/amd/gfx90a",
    "accel/amd/gfx90A",
    "accel/amd/gfx",
    CC90 + "/",
    "accel/nvidia/cc\uff19\uff10",
    "accel/nvidia/cc\u0669\u0660",
    CC90 + "\r\n",
    CC90 + "\x0b",
    CC90 + "\u00a0",
    "\u00a0" + CC90,
    CC90 + "\x1f",
    CC90 + "\x85",
    "a\nb",
]
ACCEL_LIVE = {3, 11}


class EnvVariableDecisionTests(unittest.TestCase):
    def setUp(self):
        self.site = harness.Site()
        self.addCleanup(self.site.close)
        for cpu in (ZEN2, ZEN4, GENERIC):
            self.site.add_cpu(cpu)
            self.site.add_module(cpu, "R/4.4.2")
        software = Path(self.site.prefix) / "software" / "linux" / "x86_64" / "amd"
        (software / "nosoft").mkdir(parents=True, exist_ok=True)
        (software / "filesoft").mkdir(parents=True, exist_ok=True)
        (software / "filesoft" / "software").write_text("", encoding="utf-8")
        (software / "linkzen2").symlink_to(software / "zen2")
        (software / "dangling").symlink_to(Path(self.site.root) / "absent")
        self.site.write_cpuinfo("zen2")
        stub = Path(self.site.init) / "eessi_archdetect.sh"
        stub.write_text(ARCHDETECT_STUB, encoding="utf-8")
        stub.chmod(0o755)
        self.outcomes: List[Tuple[int, str, str]] = []
        name = self._testMethodName.replace("test_", "env-").replace("_", "-")
        self.sheet = support.sheet(self, name)

    def _env(self, cpupath: str = GOOD_CPUPATH, **extra: Optional[str]) -> Dict[str, str]:
        env = harness.base_env(self.site, ZEN4, ["R/4.4.2"], gpu="none")
        env["EESSI_TEST_CPUPATH"] = cpupath
        for key, value in extra.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    def _show(self, value: object) -> str:
        """``repr(value)`` with the site named, cut to a length a label can hold."""
        shown = golden.normalize(repr(value), support.site_work(self.site))
        if len(shown) > 60:
            shown = "%s...(%d characters)" % (shown[:40], len(shown))
        return shown

    def _compare(
        self, label: str, env: Dict[str, str], args: Iterable[str]
    ) -> Tuple[int, str, str]:
        argv = list(args)
        proc = harness.run_with_timeout(
            [*harness.GUARD_ARGV, *argv], env=env, timeout=RUN_TIMEOUT
        )
        shown = support.label("%s %s" % (label, " ".join(argv) or "live"))
        with self.subTest(case=shown):
            support.check(self, self.sheet, shown, proc, self.site)
        outcome = (proc.returncode, proc.stdout, proc.stderr)
        self.outcomes.append(outcome)
        return outcome

    def _assert_varied(self, minimum: int) -> None:
        # A table whose cases all end the same way would pass for a program
        # that gave one answer to every input.
        distinct = len({outcome for outcome in self.outcomes})
        self.assertGreaterEqual(distinct, minimum, "cases do not exercise different paths")

    def test_cpupath_output_shapes(self):
        # One row from each outcome class, plus the two length boundaries
        # and the reload, unsafe, and empty anchors.
        # index, modes, anchor text checked on the dry-run row
        rows = (
            (0, (DRY, DRY_JSON, LIVE), "decision: reloaded"),
            (5, (DRY,), None),
            (7, (DRY,), None),
            (11, (DRY,), "decision: failed"),
            (13, (DRY, DRY_JSON, LIVE), None),
            (29, (DRY,), None),
            (32, (DRY,), "decision: failed"),
            (33, (DRY_JSON, LIVE), None),
            (41, (DRY,), None),
            (48, (DRY,), None),
            (49, (DRY,), None),
        )
        for index, modes, anchor in rows:
            cpupath = CPUPATHS[index]
            for args in modes:
                name = "cpupath %d %s" % (index, self._show(cpupath))
                _rc, out, _err = self._compare(name, self._env(cpupath), args)
                if anchor is not None and args == DRY:
                    self.assertIn(anchor, out)
        self._assert_varied(10)

    def test_software_override_values(self):
        # index, modes, text the anchor rows must contain
        rows = (
            (0, (DRY, DRY_JSON), None),
            (1, (DRY,), None),
            (5, (DRY,), "decision: skip"),
            (7, (LIVE,), "invalid-environment"),
            (21, (LIVE,), "invalid-environment"),
        )
        for index, modes, anchor in rows:
            value = OVERRIDES[index]
            for args in modes:
                env = self._env(EESSI_SOFTWARE_SUBDIR_OVERRIDE=value)
                env = self._env(EESSI_SOFTWARE_SUBDIR_OVERRIDE=value)
                _rc, out, _err = self._compare(
                    "override %d %s" % (index, self._show(value)), env, args
                )
                if anchor is not None:
                    self.assertIn(anchor, out)
        self._assert_varied(6)

    def test_guard_switch_variables(self):
        # One value that keeps the decision and one that must not change it,
        # except the options override, where a space skips.
        for key in SWITCHES:
            same = SWITCH_VALUES[0]
            other = " " if key == "EESSI_ARCHDETECT_OPTIONS_OVERRIDE" else SWITCH_VALUES[2]
            for value in (same, other):
                self._compare(
                    "%s=%s" % (key, self._show(value)), self._env(**{key: value}), DRY
                )
        for value in (ARCHDETECT_OPTIONS[0], ARCHDETECT_OPTIONS[-1]):
            env = self._env(EESSI_ARCHDETECT_OPTIONS=value)
            self._compare("archdetect options %s" % self._show(value), env, LIVE)
        for subdir in (LOADED_SUBDIRS[0], LOADED_SUBDIRS[2], LOADED_SUBDIRS[-1]):
            env = self._env(EESSI_SOFTWARE_SUBDIR=subdir)
            self._compare("loaded %s" % self._show(subdir), env, DRY)
        self._assert_varied(5)

    def test_installed_target_layouts(self):
        for cpupath in (
            "x86_64/amd/nosoft",
            "x86_64/amd/filesoft",
            "x86_64/amd/linkzen2",
            "x86_64/amd/dangling:" + GENERIC,
        ):
            self._compare("layout %s" % self._show(cpupath), self._env(cpupath), DRY)
        self._assert_varied(4)

    def test_path_variable_spellings(self):
        prefix = self.site.prefix
        self._compare("prefix slash", self._env(EESSI_PREFIX=prefix + "/"), DRY)
        self._compare("prefix doubleslash", self._env(EESSI_PREFIX=prefix + "//"), DRY)
        self._compare("os empty", self._env(EESSI_OS_TYPE=""), DRY)
        self._compare("prefix dotdot", self._env(EESSI_PREFIX=prefix + "/init/.."), DRY)
        self._assert_varied(2)

    def test_accelerator_answers(self):
        mismatch = self._env(EESSI_TEST_ACCEL=CC90, EESSI_ACCEL_SUBDIR=CC80)
        self._compare("accel mismatch", mismatch, DRY)
        self._compare("accel mismatch live", mismatch, LIVE)
        match = self._env(EESSI_TEST_ACCEL=CC90, EESSI_ACCEL_SUBDIR=CC90)
        self._compare("accel match", match, DRY)
        bad = self._env(EESSI_TEST_ACCEL=ACCEL_OUTPUTS[0], EESSI_ACCEL_SUBDIR=CC80)
        self._compare("accel bad", bad, DRY)
        status = self._env(EESSI_TEST_ACCEL=CC90, EESSI_TEST_ACCEL_RC="1")
        self._compare("accel status", status, DRY)
        self._assert_varied(5)


if __name__ == "__main__":
    unittest.main()
