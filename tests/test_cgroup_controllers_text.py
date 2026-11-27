"""The cgroup.controllers file is split into words on Unicode whitespace.

The binary reads the file as text and asks whether the word cpuset is one of
its words, so Unicode whitespace separates the words as ASCII whitespace does,
and a character that is not whitespace joins them. A bytes sequence that is not
valid UTF-8 makes the file name no controller, and a missing file allows the
directory. A cpuset.cpus.effective file of whitespace alone is skipped, and
the walk goes on to the parent, and surrounding Unicode whitespace around a
number does not stop it from being read.

The binary reads the cgroup of the process and the tree under the system
root, so a whole-program run needs a private mount namespace. The test starts
the binary under unshare with a user and mount namespace and binds a generated
tree over the cgroup root. The tree holds the cgroup path of the test process
itself, so every child the binary starts finds the same cgroup. The cgroup is
a leaf whose parents hold no cpuset. When the leaf is taken, it limits the
task to the processor 1, and the binary chooses the Icelake directory that
processor allows; when it is turned away, the binary finds no cpuset and
chooses another directory. The whole output of each run is pinned in
tests/golden/cgroup-controllers-text.txt. A host that cannot make the
namespace, or does not use cgroup v2, fails the tests: they are the only check
of the walk as the binary runs it, and the CI and release workflows lift the
AppArmor limit on user namespaces so they run there.
"""

from __future__ import annotations

import functools
import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from typing import List, Mapping, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import differential  # noqa: E402
import golden  # noqa: E402
import harness  # noqa: E402

CASES = (
    ("joined", "cpu\u00a0cpuset\u2003memory\n", True),
    ("padded", "\u3000cpuset\u1680\x1f\n", True),
    ("glued", "cpuset\u200bmemory cpu\n", False),
    ("suffixed", "cpu cpuset\ufffd memory\n", False),
    ("absent", "cpu memory\n", False),
)


# Each case: label, bytes of the leaf's cgroup.controllers (None: no file),
# bytes of the leaf's cpuset.cpus.effective, bytes of the parent's
# cpuset.cpus.effective (None: the parent has no cpuset), and whether the
# processor 1 is the one the task may use.
CASES = (
    ("joined", "cpu\u00a0cpuset\u2003memory\n".encode(), b"1\n", None, True),
    ("padded", "\u3000cpuset\u1680\x1f\n".encode(), b"1\n", None, True),
    ("glued", "cpuset\u200bmemory cpu\n".encode(), b"1\n", None, False),
    ("suffixed", "cpu cpuset\ufffd memory\n".encode(), b"1\n", None, False),
    ("absent", b"cpu memory\n", b"1\n", None, False),
    ("invalid bytes", b"cpu cpuset\xff memory\n", b"1\n", None, False),
    ("no controllers file", None, b"1\n", None, True),
    ("number padded by whitespace", b"cpu cpuset\n", "\u00a01\u2003\n\u3000".encode(), None, True),
    ("blank file falls to the parent", b"cpu cpuset\n", "\u00a0\n\u3000".encode(), b"1\n", True),
    ("blank file and no parent file", b"cpu cpuset\n", "\u00a0\n\u3000".encode(), None, False),
)
Case = Tuple[str, Optional[bytes], bytes, Optional[bytes], bool]


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


def _build(case: Case) -> differential.Build:
    _label, controllers, leaf_cpus, parent_cpus, _taken = case

    def build(site: harness.Site) -> Mapping[str, str]:
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
        if parent_cpus is not None:
            (leaf.parent / "cgroup.controllers").write_text("cpu cpuset\n", encoding="utf-8")
            (leaf.parent / "cpuset.cpus.effective").write_bytes(parent_cpus)
        if controllers is None:
            (leaf / "cgroup.controllers").unlink(missing_ok=True)
        else:
            (leaf / "cgroup.controllers").write_bytes(controllers)
        (leaf / "cpuset.cpus.effective").write_bytes(leaf_cpus)
        env = harness.base_env(site, LOADED, ["R/4.4.2"], gpu="none")
        env["EAG_FAKE_TREE"] = str(tree)
        return env

    return build


class ControllersProgramTests(unittest.TestCase):
    def setUp(self) -> None:
        problem = _namespace_problem()
        if problem:
            self.fail(
                "these tests need a user and mount namespace over a cgroup v2 tree, and "
                + problem
                + "; on Ubuntu run sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0"
            )

    def test_the_binary_splits_the_controllers_and_reads_the_cpuset(self) -> None:
        sheet = golden.Sheet("cgroup-controllers-text")
        self.addCleanup(sheet.finish)
        for case in CASES:
            label, _controllers, _leaf, _parent, taken = case
            with self.subTest(case=label):
                run = differential.run_fresh(
                    _in_namespace(differential.zig_program()), _build(case), ["--json"]
                )
                sheet.check(self, label, golden.render(run.returncode, run.stdout, run.stderr))
                self.assertEqual(run.returncode, 0, run.stderr)
                chosen = json.loads(run.stdout)["now"]
                if taken:
                    self.assertEqual(chosen, ICELAKE)
                else:
                    self.assertNotEqual(chosen, ICELAKE)


if __name__ == "__main__":
    unittest.main()
