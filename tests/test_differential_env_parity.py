"""Python and Zig guards must agree on cpupath, override, and accelerator inputs.

Each case runs the Python guard and the program named by TOOL_BIN on the same
environment and compares exit status, standard output, and standard error. The
cases are a fixed table over a generated fake tree, with a stand-in for
eessi_archdetect.sh that prints what a test variable holds. The tests skip
when TOOL_BIN is unset, because the Python guard alone has nothing to be
compared with.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402

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


def _env_key(text: str) -> str:
    return text.encode("ascii", "backslashreplace").decode("ascii")


class DifferentialEnvParityTests(unittest.TestCase):
    def setUp(self):
        harness.skip_unless_tool_bin(self)
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

    def _env(self, cpupath: str = GOOD_CPUPATH, **extra: Optional[str]) -> Dict[str, str]:
        env = harness.base_env(self.site, ZEN4, ["R/4.4.2"], gpu="none")
        env["EESSI_TEST_CPUPATH"] = cpupath
        for key, value in extra.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    def _compare(
        self, label: str, env: Dict[str, str], args: Iterable[str]
    ) -> Tuple[int, str, str]:
        argv = list(args)
        python = harness.run_with_timeout(
            [sys.executable, "-I", str(harness.GUARD), *argv], env=env, timeout=RUN_TIMEOUT
        )
        program = harness.run_with_timeout(
            [str(harness.PROGRAM), *argv], env=env, timeout=RUN_TIMEOUT
        )
        shown = "%s %s" % (_env_key(label), " ".join(argv))
        with self.subTest(case=shown):
            self.assertEqual(program.returncode, python.returncode, shown)
            self.assertEqual(program.stderr, python.stderr, shown)
            self.assertEqual(program.stdout, python.stdout, shown)
        outcome = (python.returncode, python.stdout, python.stderr)
        self.outcomes.append(outcome)
        return outcome

    def _assert_varied(self, minimum: int) -> None:
        # A table whose cases all end the same way would pass for any pair of
        # programs that agree on one answer.
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
                _rc, out, _err = self._compare(
                    "cpupath %r" % cpupath, self._env(cpupath), args
                )
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
                _rc, out, _err = self._compare("override %r" % value, env, args)
                if anchor is not None:
                    self.assertIn(anchor, out)
        self._assert_varied(6)

    def test_guard_switch_variables(self):
        # One value that keeps the decision and one that must not change it,
        # except the options override, where a space skips.
        for key in SWITCHES:
            same = SWITCH_VALUES[0]
            other = " " if key == "EESSI_ARCHDETECT_OPTIONS_OVERRIDE" else SWITCH_VALUES[2]
            self._compare("%s=%r" % (key, same), self._env(**{key: same}), DRY)
            self._compare("%s=%r" % (key, other), self._env(**{key: other}), DRY)
        for value in (ARCHDETECT_OPTIONS[0], ARCHDETECT_OPTIONS[-1]):
            env = self._env(EESSI_ARCHDETECT_OPTIONS=value)
            self._compare("archdetect options %r" % value, env, LIVE)
        for subdir in (LOADED_SUBDIRS[0], LOADED_SUBDIRS[2], LOADED_SUBDIRS[-1]):
            self._compare("loaded %r" % subdir, self._env(EESSI_SOFTWARE_SUBDIR=subdir), DRY)
        self._assert_varied(5)

    def test_installed_target_layouts(self):
        for cpupath in (
            "x86_64/amd/nosoft",
            "x86_64/amd/filesoft",
            "x86_64/amd/linkzen2",
            "x86_64/amd/dangling:" + GENERIC,
        ):
            self._compare("layout %r" % cpupath, self._env(cpupath), DRY)
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
