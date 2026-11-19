"""Cgroup v1 cpuset directories come from mountinfo, not a fixed prefix."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import eessi_arch_guard as guard  # noqa: E402


class CgroupV1ProcTree(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="eag-cgroup-v1-")
        self.root = Path(self.tmp.name)
        self.proc = self.root / "proc" / "self"
        self.proc.mkdir(parents=True)
        self.cg = self.root / "sys" / "fs" / "cgroup"
        self.cg.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="ascii")

    def lookup(self):
        return guard.cgroup_cpus_from(str(self.proc / "cgroup"), str(self.cg))

    def test_mountinfo_unescape_keeps_invalid_octal_literal(self):
        cases = ((r"\089", r"\089"), (r"\777", r"\777"), (r"\040", " "))
        for escaped, expected in cases:
            with self.subTest(escaped=escaped):
                self.assertEqual(guard._unescape_mountinfo(escaped), expected)

    def test_v1_cpuset_mounted_off_the_default_path(self):
        point = self.root / "cgroup" / "cpuset"
        other = self.root / "cgroup" / "notcpuset"
        self.write(self.proc / "cgroup", "2:cpuset:/job:42\n")
        self.write(
            self.proc / "mountinfo",
            "21 20 0:21 / %s rw - cgroup cgroup rw,cpusetfoo\n"
            "22 20 0:22 / %s rw - cgroup cgroup rw,cpuset\n" % (other, point),
        )
        self.write(other / "job:42" / "cpuset.cpus.effective", "0-3\n")
        self.write(point / "job:42" / "cpuset.cpus.effective", "4-5\n")
        self.write(self.cg / "cpuset" / "job:42" / "cpuset.cpus.effective", "0\n")
        self.write(self.cg / "job:42" / "cpuset.cpus.effective", "1\n")
        self.assertEqual(self.lookup(), frozenset({4, 5}))

    def test_nested_cgroup_stops_at_the_cpuset_mount(self):
        point = self.root / "cgroup" / "cpuset"
        self.write(self.proc / "cgroup", "2:cpuset:/a/b/c\n")
        self.write(
            self.proc / "mountinfo",
            "22 20 0:22 / %s rw - cgroup cgroup rw,cpuset\n" % point,
        )
        self.write(point / "a" / "cpuset.cpus.effective", "6,7\n")
        self.write(self.root / "cgroup" / "cpuset.cpus.effective", "9\n")
        self.write(self.cg / "cpuset" / "a" / "cpuset.cpus.effective", "1\n")
        self.assertEqual(self.lookup(), frozenset({6, 7}))

    def test_missing_cpuset_controller_is_not_a_cpuset(self):
        cpu = self.cg / "cpu,cpuacct"
        self.write(self.proc / "cgroup", "2:cpu,cpuacct:/job\n0::/job\n")
        self.write(
            self.proc / "mountinfo",
            "30 20 0:30 / %s rw - cgroup cgroup rw,cpu,cpuacct\n"
            "31 20 0:31 / %s rw - cgroup2 cgroup2 rw\n" % (cpu, self.cg),
        )
        self.write(cpu / "job" / "cgroup.controllers", "cpu mycpuset memory\n")
        self.write(cpu / "job" / "cpuset.cpus.effective", "8\n")
        self.write(self.cg / "job" / "cgroup.controllers", "cpu mycpuset memory\n")
        self.write(self.cg / "job" / "cpuset.cpus.effective", "8\n")
        self.write(self.cg / "cgroup.controllers", "cpu mycpuset memory\n")
        self.write(self.cg / "cpuset.cpus.effective", "9\n")
        self.assertIsNone(self.lookup())

    def test_dotdot_mount_root_keeps_the_cgroup_root(self):
        outside = self.root / "outside"
        self.write(self.proc / "cgroup", "0::/../job\n")
        self.write(
            self.proc / "mountinfo",
            "40 20 0:40 /.. %s rw - cgroup2 cgroup2 rw\n" % outside,
        )
        self.write(outside / "job" / "cpuset.cpus.effective", "9\n")
        self.write(self.cg / "cpuset.cpus.effective", "0-3\n")
        self.assertEqual(self.lookup(), frozenset({0, 1, 2, 3}))
