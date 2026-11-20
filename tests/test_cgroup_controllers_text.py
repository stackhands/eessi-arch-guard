"""The cgroup.controllers file is split as str.split() splits it.

Python reads the file as text and asks whether the word cpuset is one of
its words, so Unicode whitespace separates the words as ASCII whitespace
does, and a character that is not whitespace joins them. The unit tests
of the Zig guard hold the same cases, so both programs name the same
cpuset for a directory.

The programs read the cgroup of the process and the tree under the system
root, so a whole-program run needs a private mount namespace. The test below
starts each program under unshare with a user and mount namespace and binds
a generated tree over the cgroup root. The tree holds the cgroup path of the
test process itself, so every child the guard starts finds the same cgroup.
The cgroup is a leaf whose controllers file separates cpuset from the other
names with a no-break space, and its parents hold no cpuset. Python takes
the leaf, which limits the task to the processor 1, and the guard chooses
the Icelake directory that processor allows. A program that does not split
the file as Python does turns the leaf away, finds no cpuset, and chooses
another directory. Each program runs on a fresh site through the
differential runner. The comparison skips when TOOL_BIN is unset. A host
that cannot make the namespace, or does not use cgroup v2, fails both
tests: they are the only check of the walk as each program runs it, and the
CI and release workflows lift the AppArmor limit on user namespaces so they
run there.
"""

from __future__ import annotations

import functools
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import List, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import differential  # noqa: E402
import eessi_arch_guard as guard  # noqa: E402
import harness  # noqa: E402

CASES = (
    ("joined", "cpu\u00a0cpuset\u2003memory\n", True),
    ("padded", "\u3000cpuset\u1680\x1f\n", True),
    ("glued", "cpuset\u200bmemory cpu\n", False),
    ("suffixed", "cpu cpuset\ufffd memory\n", False),
    ("absent", "cpu memory\n", False),
)


class ControllersTextTests(unittest.TestCase):
    def test_controllers_are_split_on_unicode_whitespace(self) -> None:
        for name, listed, allowed in CASES:
            with self.subTest(case=name), tempfile.TemporaryDirectory(prefix="eag-ctl-") as tmp:
                Path(tmp, "cgroup.controllers").write_text(listed, encoding="utf-8")
                self.assertIs(guard._controllers_allow(tmp), allowed)

    def test_invalid_bytes_do_not_make_a_controller(self) -> None:
        with tempfile.TemporaryDirectory(prefix="eag-ctl-") as tmp:
            Path(tmp, "cgroup.controllers").write_bytes(b"cpu cpuset\xff memory\n")
            self.assertFalse(guard._controllers_allow(tmp))

    def test_a_missing_file_allows_the_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="eag-ctl-") as tmp:
            self.assertTrue(guard._controllers_allow(tmp))

    def test_a_cpuset_file_of_whitespace_alone_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory(prefix="eag-ctl-") as tmp:
            root = Path(tmp)
            (root / "job").mkdir()
            (root / "cpuset.cpus.effective").write_text("6\n", encoding="utf-8")
            (root / "job" / "cpuset.cpus.effective").write_text("\u00a0\n\u3000", encoding="utf-8")
            self.assertEqual(guard._walk_cpuset(str(root / "job"), str(root)), frozenset({6}))


LOADED = "x86_64/amd/zen4"
CPUS = ("x86_64/intel/icelake", "x86_64/generic", LOADED)
ICELAKE = "x86_64/intel/icelake"
FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "cpuinfo"
    / "emeraldrapids-kvm-no-waitpkg.cpuinfo"
)
POOR_FLAGS = "flags\t\t: fpu vme de pse tsc msr pae"
ENTER = 'mount --bind "$EAG_FAKE_TREE" /sys/fs/cgroup && exec "$@"'
PROBE = ["unshare", "--map-root-user", "--mount", "true"]
PROBE_TIMEOUT = 30.0


def _cpuinfo() -> str:
    """The fixture with a processor 0 that lacks the flags of the others."""
    parts = re.split(r"(?m)^(?=processor\t: )", FIXTURE.read_text(encoding="utf-8"))
    blocks = [part for part in parts if part.strip()]
    first = re.sub(r"(?m)^flags\t\t: .*$", POOR_FLAGS, blocks[0])
    return first + "".join(blocks[1:])


def _own_cgroup() -> str:
    """The cgroup v2 path of this process, or an empty string."""
    try:
        text = Path("/proc/self/cgroup").read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in text.splitlines():
        if line.startswith("0::"):
            return line[3:]
    return ""


@functools.lru_cache(maxsize=None)
def _namespace_problem() -> str:
    """Why a mount namespace over a cgroup v2 tree cannot be made, or ''."""
    if not _own_cgroup():
        return "this process has no cgroup v2 path in /proc/self/cgroup"
    if shutil.which("unshare") is None:
        return "unshare is not on PATH"
    try:
        probe = harness.run_with_timeout(PROBE, timeout=PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return "%s did not finish in %s s" % (" ".join(PROBE), PROBE_TIMEOUT)
    if probe.returncode != 0:
        return "%s failed with status %d: %s" % (
            " ".join(PROBE),
            probe.returncode,
            probe.stderr.strip(),
        )
    return ""


def _in_namespace(program: Sequence[str]) -> List[str]:
    return ["unshare", "--map-root-user", "--mount", "sh", "-c", ENTER, "sh", *program]


def _build(site: harness.Site) -> Mapping[str, str]:
    for cpu in CPUS:
        site.add_cpu(cpu)
        site.add_module(cpu, "R/4.4.2")
    Path(site.cpuinfo).write_text(_cpuinfo(), encoding="utf-8")
    tree = Path(site.root) / "cgroup-tree"
    leaf = tree / _own_cgroup().strip("/")
    leaf.mkdir(parents=True, exist_ok=True)
    for parent in leaf.parents:
        if parent == tree.parent:
            break
        (parent / "cgroup.controllers").write_text("cpu memory\n", encoding="utf-8")
    (leaf / "cgroup.controllers").write_text("cpu\u00a0cpuset\n", encoding="utf-8")
    (leaf / "cpuset.cpus.effective").write_text("1\n", encoding="utf-8")
    env = harness.base_env(site, LOADED, ["R/4.4.2"], gpu="none")
    env["EAG_FAKE_TREE"] = str(tree)
    return env


class ControllersProgramTests(unittest.TestCase):
    def setUp(self) -> None:
        problem = _namespace_problem()
        if problem:
            self.fail(
                "these tests need a user and mount namespace over a cgroup v2 tree, and "
                + problem
                + "; on Ubuntu run sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0"
            )

    def test_python_takes_the_leaf_whose_controllers_use_a_no_break_space(self) -> None:
        run = differential.run_fresh(
            _in_namespace(differential.python_guard()), _build, ["--json"]
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)["now"], ICELAKE)

    def test_programs_agree_on_the_cpuset(self) -> None:
        harness.skip_unless_tool_bin(self)
        python = differential.run_fresh(
            _in_namespace(differential.python_guard()), _build, ["--json"]
        )
        zig = differential.run_fresh(_in_namespace(differential.zig_program()), _build, ["--json"])
        self.assertEqual(zig.observed(), python.observed())


if __name__ == "__main__":
    unittest.main()
