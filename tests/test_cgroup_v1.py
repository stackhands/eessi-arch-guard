"""Cgroup v1 cpuset directories come from mountinfo, not a fixed prefix."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import eessi_arch_guard as guard  # noqa: E402
import private_tmp  # noqa: E402, F401


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

    def test_root_cpuset_walk_does_not_read_relative_to_cwd(self):
        paths = []

        def read(path):
            paths.append(path)
            return "7\n" if path == "cpuset.cpus.effective" else None

        with mock.patch.object(guard, "_read_text", side_effect=read):
            self.assertIsNone(guard._walk_cpuset("/", "/"))
        self.assertEqual(paths, ["/cpuset.cpus.effective"])

    def test_double_slash_cpuset_walk_does_not_read_relative_to_cwd(self):
        paths = []

        def read(path):
            paths.append(path)
            return "1\n" if path == "cpuset.cpus.effective" else None

        with mock.patch.object(guard, "_read_text", side_effect=read):
            self.assertIsNone(guard._walk_cpuset("//job", "/"))
        self.assertNotIn("cpuset.cpus.effective", paths)

    def test_double_slash_cgroup_path_does_not_hide_another_processor(self):
        # Processor 0 is Haswell. Processor 1 is Sapphire Rapids. A cgroup
        # path that starts with // must not adopt cpuset.cpus.effective from
        # the working directory, or the task is judged on processor 1 alone
        # and keeps waitpkg.
        old = os.getcwd()
        os.chdir(self.root)
        try:
            (self.root / "cpuset.cpus.effective").write_text("1\n", encoding="ascii")
            self.write(self.proc / "cgroup", "0:://job\n")
            self.write(
                self.proc / "mountinfo",
                "22 20 0:22 / / rw - cgroup2 cgroup2 rw\n",
            )
            found = self.lookup()
            cpuinfo = "\n".join(
                (
                    "processor : 0",
                    "vendor_id : GenuineIntel",
                    "cpu family : 6",
                    "model : 60",
                    "flags : avx2 fma",
                    "",
                    "processor : 1",
                    "vendor_id : GenuineIntel",
                    "cpu family : 6",
                    "model : 143",
                    "flags : avx2 fma avx512f avx512bw avx512cd avx512dq "
                    "avx512vl avx512_bf16 amx_tile waitpkg",
                    "",
                )
            )
            allowed, base = guard._task_cpu_bounds({}, frozenset({0, 1}), found)
            scoped = guard.scope_cpuinfo(cpuinfo, allowed, base_cpus=base)
            self.assertIsNotNone(scoped)
            self.assertNotIn("waitpkg", scoped)
            self.assertIn("avx2", scoped)
            self.assertIn("fma", scoped)
        finally:
            os.chdir(old)

    def _cpuset_rows(self):
        point = self.root / "cgroup" / "cpuset"
        other = self.root / "cgroup" / "notcpuset"
        cpu = self.cg / "cpu,cpuacct"
        outside = self.root / "outside"
        return (
            (
                "mounted off the default path",
                "2:cpuset:/job:42\n",
                "21 20 0:21 / %s rw - cgroup cgroup rw,cpusetfoo\n"
                "22 20 0:22 / %s rw - cgroup cgroup rw,cpuset\n" % (other, point),
                (
                    (other / "job:42" / "cpuset.cpus.effective", "0-3\n"),
                    (point / "job:42" / "cpuset.cpus.effective", "4-5\n"),
                    (self.cg / "cpuset" / "job:42" / "cpuset.cpus.effective", "0\n"),
                    (self.cg / "job:42" / "cpuset.cpus.effective", "1\n"),
                ),
                frozenset({4, 5}),
            ),
            (
                "nested cgroup stops at the cpuset mount",
                "2:cpuset:/a/b/c\n",
                "22 20 0:22 / %s rw - cgroup cgroup rw,cpuset\n" % point,
                (
                    (point / "a" / "cpuset.cpus.effective", "6,7\n"),
                    (self.root / "cgroup" / "cpuset.cpus.effective", "9\n"),
                    (self.cg / "cpuset" / "a" / "cpuset.cpus.effective", "1\n"),
                ),
                frozenset({6, 7}),
            ),
            (
                "missing cpuset controller is not a cpuset",
                "2:cpu,cpuacct:/job\n0::/job\n",
                "30 20 0:30 / %s rw - cgroup cgroup rw,cpu,cpuacct\n"
                "31 20 0:31 / %s rw - cgroup2 cgroup2 rw\n" % (cpu, self.cg),
                (
                    (cpu / "job" / "cgroup.controllers", "cpu mycpuset memory\n"),
                    (cpu / "job" / "cpuset.cpus.effective", "8\n"),
                    (self.cg / "job" / "cgroup.controllers", "cpu mycpuset memory\n"),
                    (self.cg / "job" / "cpuset.cpus.effective", "8\n"),
                    (self.cg / "cgroup.controllers", "cpu mycpuset memory\n"),
                    (self.cg / "cpuset.cpus.effective", "9\n"),
                ),
                None,
            ),
            (
                "dotdot mount root keeps the cgroup root",
                "0::/../job\n",
                "40 20 0:40 /.. %s rw - cgroup2 cgroup2 rw\n" % outside,
                (
                    (outside / "job" / "cpuset.cpus.effective", "9\n"),
                    (self.cg / "cpuset.cpus.effective", "0-3\n"),
                ),
                frozenset({0, 1, 2, 3}),
            ),
        )

    def test_cpuset_comes_from_the_mountinfo_table(self):
        # One row per mount shape: off-path cpuset, a nested stop, a
        # controller that only contains the name, and a ".." mount root.
        names = [row[0] for row in self._cpuset_rows()]
        for name in names:
            with self.subTest(name=name):
                self.tearDown()
                self.setUp()
                row = next(item for item in self._cpuset_rows() if item[0] == name)
                _name, cgroup, mountinfo, files, expected = row
                self.write(self.proc / "cgroup", cgroup)
                self.write(self.proc / "mountinfo", mountinfo)
                for path, text in files:
                    self.write(path, text)
                self.assertEqual(self.lookup(), expected)
