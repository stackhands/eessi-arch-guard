"""The decision for cpupath, override, and accelerator inputs.

Each case runs the guard on one environment and pins exit status, standard
output, and standard error in a golden sheet, ``golden/env-<test name>.txt``.
The cases are a fixed table over a generated fake tree, with a stand-in for
eessi_archdetect.sh that prints what a test variable holds. The anchor tests
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
        for index, cpupath in enumerate(CPUPATHS):
            modes = [DRY]
            if index in CPUPATH_JSON:
                modes.append(DRY_JSON)
            if index in CPUPATH_LIVE:
                modes.append(LIVE)
            for args in modes:
                name = "cpupath %d %s" % (index, self._show(cpupath))
                self._compare(name, self._env(cpupath), args)
        self._assert_varied(10)

    def test_cpupath_anchors(self):
        _rc, out, _err = self._compare("anchor reload", self._env(), DRY)
        self.assertIn("decision: reloaded", out)
        _rc, out, _err = self._compare("anchor unsafe", self._env(ZEN2 + "/"), DRY)
        self.assertIn("decision: failed", out)
        _rc, out, _err = self._compare("anchor empty", self._env(":"), DRY)
        self.assertIn("decision: failed", out)

    def test_software_override_values(self):
        for index, value in enumerate(OVERRIDES):
            modes = [DRY]
            if index in OVERRIDE_LIVE:
                modes.append(LIVE)
            if index % 12 == 0:
                modes.append(DRY_JSON)
            for args in modes:
                env = self._env(EESSI_SOFTWARE_SUBDIR_OVERRIDE=value)
                self._compare("override %d %s" % (index, self._show(value)), env, args)
        self._assert_varied(6)

    def test_software_override_anchors(self):
        env = self._env(EESSI_SOFTWARE_SUBDIR_OVERRIDE=ZEN2)
        _rc, out, _err = self._compare("anchor installed", env, DRY)
        self.assertIn("decision: skip", out)
        env = self._env(EESSI_SOFTWARE_SUBDIR_OVERRIDE=ZEN2 + "/")
        _rc, out, _err = self._compare("anchor slash", env, LIVE)
        self.assertIn("invalid-environment", out)
        env = self._env(EESSI_SOFTWARE_SUBDIR_OVERRIDE="x86_64/amd/unknown")
        _rc, out, _err = self._compare("anchor not installed", env, LIVE)
        self.assertIn("invalid-environment", out)

    def test_guard_switch_variables(self):
        for key in SWITCHES:
            for value in SWITCH_VALUES:
                self._compare("%s=%s" % (key, self._show(value)), self._env(**{key: value}), DRY)
        for value in ARCHDETECT_OPTIONS:
            env = self._env(EESSI_ARCHDETECT_OPTIONS=value)
            self._compare("archdetect options %s" % self._show(value), env, LIVE)
        for subdir in LOADED_SUBDIRS:
            env = self._env(EESSI_SOFTWARE_SUBDIR=subdir)
            self._compare("loaded %s" % self._show(subdir), env, DRY)
        self._assert_varied(5)

    def test_installed_target_layouts(self):
        for cpupath in LAYOUT_CPUPATHS:
            self._compare("layout %s" % self._show(cpupath), self._env(cpupath), DRY)
        for cpupath in LAYOUT_CPUPATHS[:3]:
            self._compare("layout %s" % self._show(cpupath), self._env(cpupath), LIVE)
        for value in LAYOUT_OVERRIDES:
            env = self._env(EESSI_SOFTWARE_SUBDIR_OVERRIDE=value)
            self._compare("layout override %s" % self._show(value), env, DRY)
        self._assert_varied(4)

    def test_path_variable_spellings(self):
        prefix = self.site.prefix
        for value in (prefix + "/", prefix + "//", prefix + "/./", prefix + "/init/.."):
            self._compare("prefix %s" % self._show(value), self._env(EESSI_PREFIX=value), DRY)
        for value in ("linux/", "linux//", "./linux", "linux/.", "", "x/../linux"):
            self._compare("os type %s" % self._show(value), self._env(EESSI_OS_TYPE=value), DRY)
        init = self.site.init
        for value in (init + "/", init + "//", "", "relative", init + "/.."):
            self._compare("init %s" % self._show(value), self._env(EESSI_INIT_PREFIX=value), DRY)
        self._assert_varied(2)

    def test_accelerator_answers(self):
        for index, output in enumerate(ACCEL_OUTPUTS):
            env = self._env(EESSI_TEST_ACCEL=output, EESSI_ACCEL_SUBDIR=CC80)
            self._compare("accel %d %s" % (index, self._show(output)), env, DRY)
            if index in ACCEL_LIVE:
                self._compare("accel %d %s" % (index, self._show(output)), env, LIVE)
        for status in ("1", "2", "3", "7"):
            env = self._env(EESSI_TEST_ACCEL=CC90, EESSI_TEST_ACCEL_RC=status)
            self._compare("accel status %s" % status, env, DRY)
        for loaded in (None, CC90):
            env = self._env(EESSI_TEST_ACCEL=CC90, EESSI_ACCEL_SUBDIR=loaded)
            self._compare("accel loaded %s" % self._show(loaded), env, DRY)
        for value in ("x", " "):
            env = self._env(
                EESSI_TEST_ACCEL=CC90,
                EESSI_ACCEL_SUBDIR=CC80,
                EESSI_ACCELERATOR_TARGET_OVERRIDE=value,
            )
            self._compare("accel override %s" % self._show(value), env, DRY)
        self._assert_varied(5)


if __name__ == "__main__":
    unittest.main()
