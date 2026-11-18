"""The EESSI client mount, site module path, retained roots, and Lmod cache checks."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load():
    path = ROOT / "share" / "sitepath.py"
    spec = importlib.util.spec_from_file_location("sitepath", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MOUNT = """\
36 35 0:36 / / rw,relatime - ext4 /dev/sda1 rw
47 36 0:47 / /cvmfs/software.eessi.io rw,nosuid,nodev - fuse.cvmfs software.eessi.io rw
"""
PLAIN_MOUNT = "36 35 0:36 / / rw - ext4 /dev/sda1 rw\n"


def _write_module(root: Path, name: str, body: str = "-- module\n", lua: bool = True) -> Path:
    suffix = ".lua" if lua else ""
    path = root / f"{name}{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _encode_module_table(entries: list[tuple[str, str, bool]]) -> str:
    rows = []
    for full_name, fn, sticky in entries:
        short = full_name.split("/", 1)[0]
        prop = '["propT"] = { ["lmod"] = { ["sticky"] = 1 } }, ' if sticky else ""
        rows.append(
            f'    ["{short}"] = {{ ["fn"] = "{fn}", ["fullName"] = "{full_name}", '
            f'{prop}["status"] = "active" }},'
        )
    lua_src = "_ModuleTable_ = {\n  ['mT'] = {\n" + "\n".join(rows) + "\n  },\n}\n"
    return base64.b64encode(lua_src.encode("utf-8")).decode("ascii")


def _tree_state(root: Path) -> tuple[tuple[str, int, bytes], ...]:
    items = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            items.append((str(path.relative_to(root)), path.stat().st_mtime_ns, path.read_bytes()))
    return tuple(items)


def _write_module_stub(bindir: Path, mode: str = "file", stream: str = "stdout") -> Path:
    """Write a read-only `module` executable that speaks `-t avail`."""
    script = bindir / "module"
    script.write_text(
        f"""#!/usr/bin/python3
import os
import sys

args = sys.argv[1:]
if args != ["-t", "avail"]:
    sys.stderr.write(f"unexpected args: {{args}}\\n")
    raise SystemExit(2)

ignore = os.environ.get("LMOD_IGNORE_CACHE", "") == "yes"
key = "EESSI_TEST_AVAIL_UNCACHED" if ignore else "EESSI_TEST_AVAIL_CACHED"
payload = os.environ.get(key, "")
out = sys.stderr if "{stream}" == "stderr" else sys.stdout
out.write(payload)
""",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


class SitePathTests(unittest.TestCase):
    def test_a_cvmfs_mount_holds_the_repo_and_the_root_disk_does_not(self):
        sp = load()
        self.assertEqual(sp.client_mount(MOUNT, "/cvmfs/software.eessi.io"), "fuse.cvmfs")
        self.assertIsNone(sp.client_mount(MOUNT, "/cvmfs/other.example"))
        self.assertIsNone(sp.client_mount(PLAIN_MOUNT, "/cvmfs/software.eessi.io"))

    def test_the_site_path_has_to_precede_the_eessi_path(self):
        sp = load()
        site = "/opt/site/modules/all"
        eessi = (
            "/cvmfs/software.eessi.io/versions/2026.06"
            "/software/linux/x86_64/amd/zen4/modules/all"
        )
        self.assertEqual(sp.order(f"{site}:{eessi}", site, eessi), "site_first")
        self.assertEqual(sp.order(f"{eessi}:{site}", site, eessi), "eessi_first")
        self.assertEqual(sp.order(f"{site}/:{eessi}/", site, eessi), "site_first")
        self.assertEqual(sp.order(eessi, site, eessi), "site_missing")
        self.assertEqual(sp.order(site, site, eessi), "eessi_missing")
        # /opt/site-extra must not count as /opt/site
        self.assertEqual(
            sp.order("/opt/site-extra/modules/all:" + eessi, site, eessi),
            "site_missing",
        )

    def test_both_checks_pass_only_together(self):
        sp = load()
        site = "/opt/site/modules/all"
        eessi = (
            "/cvmfs/software.eessi.io/versions/2026.06"
            "/software/linux/x86_64/amd/zen4/modules/all"
        )
        code, text = sp.report(MOUNT, f"{site}:{eessi}", "/cvmfs/software.eessi.io", site, eessi)
        self.assertEqual(code, 0, text)
        self.assertIn("ok", text)
        self.assertNotIn("FAIL", text)
        code, text = sp.report(MOUNT, f"{eessi}:{site}", "/cvmfs/software.eessi.io", site, eessi)
        self.assertEqual(code, 1)
        self.assertIn("behind", text)
        code, text = sp.report(
            PLAIN_MOUNT, f"{site}:{eessi}", "/cvmfs/software.eessi.io", site, eessi
        )
        self.assertEqual(code, 1)
        self.assertIn("not on a cvmfs mount", text)

    def test_the_how_to_names_the_command_and_the_default(self):
        page = (ROOT / "docs" / "orgmode" / "howto" / "check-the-client-mount.org").read_text()
        self.assertIn("share/sitepath.py", page)
        self.assertIn("site modules", page)
        self.assertIn("exit status", page.lower())

    def test_require_input_rejects_placeholder_modules_path(self):
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "share" / "sitepath.py"),
                "--require-input",
                "--site",
                "/modules/all",
                "--eessi",
                "/modules/all",
                "--mountinfo",
                "/proc/self/mountinfo",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("--require-input", proc.stderr)

    def test_placeholder_modules_path_without_flag_still_runs(self):
        sp = load()
        code, text = sp.report(
            PLAIN_MOUNT,
            "/modules/all",
            "/cvmfs/software.eessi.io",
            "/modules/all",
            "/modules/all",
        )
        self.assertEqual(code, 1)
        self.assertIn("FAIL", text)

    def test_default_cli_output_is_byte_identical_without_new_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mount_ok = root / "mountinfo"
            mount_ok.write_text(MOUNT, encoding="utf-8")
            mount_plain = root / "plain"
            mount_plain.write_text(PLAIN_MOUNT, encoding="utf-8")

            site = "/opt/site/modules/all"
            eessi = (
                "/cvmfs/software.eessi.io/versions/2026.06"
                "/software/linux/x86_64/amd/zen4/modules/all"
            )
            cases = [
                (
                    [
                        "--site",
                        site,
                        "--eessi",
                        eessi,
                        "--modulepath",
                        f"{site}:{eessi}",
                        "--mountinfo",
                        str(mount_ok),
                    ],
                    0,
                    b"ok   /cvmfs/software.eessi.io is on a cvmfs mount (fuse.cvmfs)\n"
                    b"ok   site modules /opt/site/modules/all precede "
                    b"/cvmfs/software.eessi.io/versions/2026.06/software/linux/"
                    b"x86_64/amd/zen4/modules/all\n",
                    b"",
                ),
                (
                    [
                        "--site",
                        site,
                        "--eessi",
                        eessi,
                        "--modulepath",
                        f"{eessi}:{site}",
                        "--mountinfo",
                        str(mount_ok),
                    ],
                    1,
                    b"ok   /cvmfs/software.eessi.io is on a cvmfs mount (fuse.cvmfs)\n"
                    b"FAIL site modules /opt/site/modules/all are behind "
                    b"/cvmfs/software.eessi.io/versions/2026.06/software/linux/"
                    b"x86_64/amd/zen4/modules/all\n",
                    b"",
                ),
                (
                    [
                        "--site",
                        site,
                        "--eessi",
                        eessi,
                        "--modulepath",
                        f"{site}:{eessi}",
                        "--mountinfo",
                        str(mount_plain),
                    ],
                    1,
                    b"FAIL /cvmfs/software.eessi.io is not on a cvmfs mount (ext4)\n"
                    b"ok   site modules /opt/site/modules/all precede "
                    b"/cvmfs/software.eessi.io/versions/2026.06/software/linux/"
                    b"x86_64/amd/zen4/modules/all\n",
                    b"",
                ),
                (
                    ["--mountinfo", str(mount_ok)],
                    2,
                    b"",
                    b"sitepath.py: need --site and --eessi, or EESSI_SITE_SOFTWARE_PATH "
                    b"and EESSI_SOFTWARE_PATH\n",
                ),
                (
                    [
                        "--require-input",
                        "--site",
                        "/modules/all",
                        "--eessi",
                        "/modules/all",
                        "--mountinfo",
                        str(mount_ok),
                    ],
                    2,
                    b"",
                    b"sitepath.py: --require-input: install paths are missing "
                    b"(unset shell variables expand to /modules/all)\n",
                ),
            ]
            for argv, want_code, want_out, want_err in cases:
                proc = subprocess.run(
                    [sys.executable, str(ROOT / "share" / "sitepath.py"), *argv],
                    env={"PATH": "/usr/bin:/bin"},
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(proc.returncode, want_code, argv)
                self.assertEqual(proc.stdout, want_out, argv)
                self.assertEqual(proc.stderr, want_err, argv)

    def test_retain_passes_with_disjoint_fixture_trees_and_makes_no_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            site = root / "site" / "modules" / "all"
            ret1 = root / "retained-local" / "modules" / "all"
            ret2 = root / "retained-vendor" / "modules" / "all"
            eessi = root / "eessi" / "modules" / "all"
            _write_module(site, "SiteApp/1.0")
            _write_module(ret1, "LocalTool/2.1")
            _write_module(ret2, "VendorCompiler/1.0", lua=False)
            _write_module(eessi, "GROMACS/2024.1")

            mountinfo = root / "mountinfo"
            mountinfo.write_text(MOUNT, encoding="utf-8")
            before = _tree_state(root)

            proc = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "share" / "sitepath.py"),
                    "--site",
                    str(site),
                    "--eessi",
                    str(eessi),
                    "--modulepath",
                    f"{site}:{ret1}:{ret2}:{eessi}",
                    "--mountinfo",
                    str(mountinfo),
                    "--retain",
                    str(ret1),
                    "--retain",
                    str(ret2),
                ],
                env={"PATH": "/usr/bin:/bin"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(proc.stderr, "")
            self.assertIn(f"ok   retained root {ret1} is present on MODULEPATH", proc.stdout)
            self.assertIn(f"ok   retained root {ret2} is present on MODULEPATH", proc.stdout)
            self.assertIn(
                "ok   no duplicate MODULEPATH entries or module precedence collisions",
                proc.stdout,
            )
            self.assertEqual(_tree_state(root), before)

    def test_retain_reports_disappeared_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            site = root / "site" / "modules" / "all"
            dropped = root / "dropped" / "modules" / "all"
            missing = root / "gone" / "modules" / "all"
            eessi = root / "eessi" / "modules" / "all"
            _write_module(site, "SiteApp/1.0")
            _write_module(dropped, "LocalTool/1.0")
            _write_module(eessi, "GROMACS/2024.1")

            mountinfo = root / "mountinfo"
            mountinfo.write_text(MOUNT, encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "share" / "sitepath.py"),
                    "--site",
                    str(site),
                    "--eessi",
                    str(eessi),
                    "--modulepath",
                    f"{site}:{eessi}",
                    "--mountinfo",
                    str(mountinfo),
                    "--retain",
                    str(dropped),
                    "--retain",
                    str(missing),
                ],
                env={"PATH": "/usr/bin:/bin"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 1, proc.stdout)
            self.assertIn(f"FAIL retained root {dropped} disappeared from MODULEPATH", proc.stdout)
            self.assertIn(f"FAIL retained root {missing} disappeared", proc.stdout)

    def test_retain_reports_duplicate_modulepath_and_precedence_collisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            site = root / "site" / "modules" / "all"
            ret1 = root / "ret1" / "modules" / "all"
            ret2 = root / "ret2" / "modules" / "all"
            eessi = root / "eessi" / "modules" / "all"
            _write_module(site, "SiteApp/1.0")
            _write_module(ret1, "SharedMPI/4.1.6")
            _write_module(ret1, "DupBoth/1.0", lua=True)
            _write_module(ret1, "DupBoth/1.0", lua=False)
            _write_module(ret2, "Shadowed/2.0")
            _write_module(eessi, "SharedMPI/4.1.6")
            _write_module(eessi, "Shadowed/2.0")

            alias = root / "ret1-alias"
            alias.symlink_to(ret1)

            mountinfo = root / "mountinfo"
            mountinfo.write_text(MOUNT, encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "share" / "sitepath.py"),
                    "--site",
                    str(site),
                    "--eessi",
                    str(eessi),
                    "--modulepath",
                    f"{site}:{ret1}:{alias}:{eessi}:{ret2}:{ret1}",
                    "--mountinfo",
                    str(mountinfo),
                    "--retain",
                    str(ret1),
                    "--retain",
                    str(ret2),
                ],
                env={"PATH": "/usr/bin:/bin"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 1, proc.stdout)
            self.assertIn(f"FAIL duplicate MODULEPATH entry {alias}", proc.stdout)
            self.assertIn(f"FAIL duplicate MODULEPATH entry {ret1}", proc.stdout)
            self.assertIn("FAIL duplicate modulefile for DupBoth/1.0", proc.stdout)
            self.assertIn(
                f"FAIL precedence collision for SharedMPI/4.1.6: {ret1} shadows {eessi}",
                proc.stdout,
            )
            self.assertIn(
                f"FAIL precedence collision for Shadowed/2.0: {eessi} shadows {ret2}",
                proc.stdout,
            )

    def test_retain_reports_loaded_sticky_modules_with_absent_source_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            site = root / "site" / "modules" / "all"
            ret = root / "ret" / "modules" / "all"
            dropped_root = root / "dropped-sticky" / "modules" / "all"
            deleted_root = root / "deleted-sticky" / "modules" / "all"
            eessi = root / "eessi" / "modules" / "all"

            ok_sticky = _write_module(
                ret,
                "SiteEnv/2026",
                'add_property("lmod", "sticky")\n',
            )
            dropped_sticky = _write_module(
                dropped_root,
                "DroppedSticky/1.0",
                'add_property("lmod", "sticky")\n',
            )
            non_sticky = _write_module(dropped_root, "OptionalTool/1.0", "-- regular\n")
            _write_module(site, "SiteApp/1.0")
            _write_module(eessi, "GROMACS/2024.1")

            deleted_fn = str(deleted_root / "DeletedSticky" / "1.0.lua")
            mt_b64 = _encode_module_table(
                [
                    ("SiteEnv/2026", str(ok_sticky), True),
                    ("DeletedSticky/1.0", deleted_fn, True),
                ]
            )

            mountinfo = root / "mountinfo"
            mountinfo.write_text(MOUNT, encoding="utf-8")

            # Case 1: Lua + Tcl sticky modules present; non-sticky module from dropped root.
            tcl_sticky = _write_module(
                ret,
                "TclSticky/1.0",
                "#%Module\nadd-property lmod sticky\n",
                lua=False,
            )
            proc_ok = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "share" / "sitepath.py"),
                    "--site",
                    str(site),
                    "--eessi",
                    str(eessi),
                    "--modulepath",
                    f"{site}:{ret}:{eessi}",
                    "--mountinfo",
                    str(mountinfo),
                    "--retain",
                    str(ret),
                ],
                env={
                    "PATH": "/usr/bin:/bin",
                    "LOADEDMODULES": "SiteEnv/2026:TclSticky/1.0:OptionalTool/1.0",
                    "_LMFILES_": f"{ok_sticky}:{tcl_sticky}:{non_sticky}",
                    "_ModuleTable001_": "table-for-zen4",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc_ok.returncode, 0, proc_ok.stdout + proc_ok.stderr)
            self.assertIn(
                "ok   loaded sticky modules have present source roots "
                "(SiteEnv/2026, TclSticky/1.0)",
                proc_ok.stdout,
            )

            # Case 2: sticky module dropped from MODULEPATH + sticky module deleted from disk.
            proc_fail = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "share" / "sitepath.py"),
                    "--site",
                    str(site),
                    "--eessi",
                    str(eessi),
                    "--modulepath",
                    f"{site}:{ret}:{eessi}",
                    "--mountinfo",
                    str(mountinfo),
                    "--retain",
                    str(ret),
                ],
                env={
                    "PATH": "/usr/bin:/bin",
                    "LOADEDMODULES": "SiteEnv/2026:DroppedSticky/1.0:DeletedSticky/1.0",
                    "_LMFILES_": f"{ok_sticky}:{dropped_sticky}:{deleted_fn}",
                    "_ModuleTable_Sz_": "1",
                    "_ModuleTable001_": mt_b64,
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc_fail.returncode, 1, proc_fail.stdout)
            self.assertIn(
                f"FAIL loaded sticky module DroppedSticky/1.0 source root {dropped_root} "
                "is absent from MODULEPATH",
                proc_fail.stdout,
            )
            self.assertIn(
                f"FAIL loaded sticky module DeletedSticky/1.0 source root {deleted_root} is absent",
                proc_fail.stdout,
            )

    def test_check_cache_reports_current_unreadable_and_stale_caches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bindir = root / "bin"
            bindir.mkdir()
            _write_module_stub(bindir)

            site = root / "site" / "modules" / "all"
            eessi = root / "eessi" / "modules" / "all"
            _write_module(site, "SiteApp/1.0")
            mod_eessi = _write_module(eessi, "GROMACS/2024.1")

            cache_dir = root / "eessi" / ".lmod" / "cache"
            cache_dir.mkdir(parents=True)
            spider = cache_dir / "spiderT.lua"
            spider.write_text(
                f'spiderT = {{ ["{eessi}"] = {{ ["GROMACS"] = {{}} }} }}\n',
                encoding="utf-8",
            )
            ts_file = cache_dir / "timestamp"
            ts_file.write_text("1700000000\n", encoding="utf-8")

            lmodrc = root / "lmodrc.lua"
            lmodrc.write_text(
                f'scDescriptT = {{\n  {{\n    ["dir"] = "{cache_dir}",\n'
                f'    ["timestamp"] = "{ts_file}",\n  }},\n}}\n',
                encoding="utf-8",
            )
            mountinfo = root / "mountinfo"
            mountinfo.write_text(MOUNT, encoding="utf-8")

            # Set cache mtimes newer than module tree.
            now = mod_eessi.stat().st_mtime + 100
            os.utime(spider, (now, now))
            os.utime(ts_file, (now, now))

            avail_text = f"{site}:\nSiteApp/1.0\n{eessi}:\nGROMACS/2024.1\n"
            base_env = {
                "PATH": f"{bindir}:/usr/bin:/bin",
                "HOME": str(root / "home"),
                "LMOD_RC": str(lmodrc),
                "EESSI_TEST_AVAIL_CACHED": avail_text,
                "EESSI_TEST_AVAIL_UNCACHED": avail_text,
            }
            before = _tree_state(root)

            proc_ok = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "share" / "sitepath.py"),
                    "--site",
                    str(site),
                    "--eessi",
                    str(eessi),
                    "--modulepath",
                    f"{site}:{eessi}",
                    "--mountinfo",
                    str(mountinfo),
                    "--check-cache",
                ],
                env=base_env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc_ok.returncode, 0, proc_ok.stdout + proc_ok.stderr)
            self.assertIn(
                f"ok   configured cache in {cache_dir} is readable and current",
                proc_ok.stdout,
            )
            self.assertIn(
                "ok   cached module -t avail inventory matches LMOD_IGNORE_CACHE=yes",
                proc_ok.stdout,
            )
            self.assertEqual(_tree_state(root), before)

            # Stale cache: a modulefile in the covered root is newer than the cache.
            stale_time = now + 200
            os.utime(mod_eessi, (stale_time, stale_time))
            proc_stale = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "share" / "sitepath.py"),
                    "--site",
                    str(site),
                    "--eessi",
                    str(eessi),
                    "--modulepath",
                    f"{site}:{eessi}",
                    "--mountinfo",
                    str(mountinfo),
                    "--check-cache",
                ],
                env=base_env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc_stale.returncode, 1, proc_stale.stdout)
            self.assertIn(f"FAIL configured cache in {cache_dir} is stale", proc_stale.stdout)

            # Unreadable cache: empty file and missing LMOD_RC.
            spider.write_bytes(b"")
            proc_empty = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "share" / "sitepath.py"),
                    "--site",
                    str(site),
                    "--eessi",
                    str(eessi),
                    "--modulepath",
                    f"{site}:{eessi}",
                    "--mountinfo",
                    str(mountinfo),
                    "--check-cache",
                ],
                env={**base_env, "LMOD_RC": f"{lmodrc}:{root / 'missing-rc.lua'}"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc_empty.returncode, 1, proc_empty.stdout)
            self.assertIn("FAIL configured LMOD_RC file", proc_empty.stdout)
            self.assertIn("is unreadable", proc_empty.stdout)

            if hasattr(os, "geteuid") and os.geteuid() != 0:
                spider.write_text('spiderT = {}\n', encoding="utf-8")
                spider.chmod(0o000)
                try:
                    proc_perm = subprocess.run(
                        [
                            sys.executable,
                            str(ROOT / "share" / "sitepath.py"),
                            "--site",
                            str(site),
                            "--eessi",
                            str(eessi),
                            "--modulepath",
                            f"{site}:{eessi}",
                            "--mountinfo",
                            str(mountinfo),
                            "--check-cache",
                        ],
                        env=base_env,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(proc_perm.returncode, 1, proc_perm.stdout)
                    self.assertIn(
                        f"FAIL configured cache file {spider} is unreadable",
                        proc_perm.stdout,
                    )
                finally:
                    spider.chmod(0o644)

    def test_check_cache_reports_inventory_diff_and_user_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bindir = root / "bin"
            bindir.mkdir()
            _write_module_stub(bindir)

            site = root / "site" / "modules" / "all"
            eessi = root / "eessi" / "modules" / "all"
            _write_module(site, "SiteApp/1.0")
            mod_new = _write_module(eessi, "NewPkg/2.0")

            user_home = root / "home"
            user_cache = user_home / ".cache" / "lmod"
            user_cache.mkdir(parents=True)
            user_spider = user_cache / "spiderT.lua"
            user_spider.write_text("spiderT = {}\n", encoding="utf-8")
            old_ts = mod_new.stat().st_mtime - 500
            os.utime(user_spider, (old_ts, old_ts))

            mountinfo = root / "mountinfo"
            mountinfo.write_text(MOUNT, encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "share" / "sitepath.py"),
                    "--site",
                    str(site),
                    "--eessi",
                    str(eessi),
                    "--modulepath",
                    f"{site}:{eessi}",
                    "--mountinfo",
                    str(mountinfo),
                    "--check-cache",
                ],
                env={
                    "PATH": f"{bindir}:/usr/bin:/bin",
                    "HOME": str(user_home),
                    "EESSI_TEST_AVAIL_CACHED": f"{site}:\nSiteApp/1.0\n{eessi}:\nOldPkg/1.0\n",
                    "EESSI_TEST_AVAIL_UNCACHED": f"{site}:\nSiteApp/1.0\n{eessi}:\nNewPkg/2.0\n",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 1, proc.stdout)
            self.assertIn(f"FAIL user cache in {user_cache} is stale", proc.stdout)
            self.assertIn(
                "FAIL cached module -t avail inventory differs from LMOD_IGNORE_CACHE=yes",
                proc.stdout,
            )
            self.assertIn("OldPkg/1.0", proc.stdout)
            self.assertIn("NewPkg/2.0", proc.stdout)

    def test_check_cache_supports_lmod_cmd_stderr_and_rejects_empty_retain(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lmod_stub = root / "lmod"
            lmod_stub.write_text(
                """#!/usr/bin/python3
import os
import sys

if sys.argv[1:] != ["bash", "-t", "avail"]:
    raise SystemExit(2)
sys.stderr.write("/opt/site/modules/all:\\nSiteApp/1.0\\n")
""",
                encoding="utf-8",
            )
            lmod_stub.chmod(lmod_stub.stat().st_mode | stat.S_IXUSR)

            mountinfo = root / "mountinfo"
            mountinfo.write_text(MOUNT, encoding="utf-8")
            site = "/opt/site/modules/all"
            eessi = "/cvmfs/software.eessi.io/versions/2026.06/software/linux/x86_64/modules/all"

            proc = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "share" / "sitepath.py"),
                    "--site",
                    site,
                    "--eessi",
                    eessi,
                    "--modulepath",
                    f"{site}:{eessi}",
                    "--mountinfo",
                    str(mountinfo),
                    "--check-cache",
                ],
                env={"PATH": "/usr/bin:/bin", "LMOD_CMD": str(lmod_stub)},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("ok   cached module -t avail inventory matches", proc.stdout)

            bad_retain = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "share" / "sitepath.py"),
                    "--site",
                    site,
                    "--eessi",
                    eessi,
                    "--retain",
                    "",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(bad_retain.returncode, 2)
            self.assertIn("--retain", bad_retain.stderr)

    @unittest.skipUnless(
        Path("/usr/share/lmod/lmod/libexec/lmod").is_file(),
        "Lmod libexec is not installed",
    )
    def test_check_cache_isolates_real_lmod_and_leaves_user_caches_untouched(self):
        lmod = Path("/usr/share/lmod/lmod/libexec/lmod")

        def snapshot_tree(base: Path) -> tuple[tuple[str, int, str], ...]:
            rows = []
            if not base.exists():
                return ()
            for item in sorted(base.rglob("*")):
                rel = str(item.relative_to(base))
                if item.is_file():
                    digest = hashlib.sha256(item.read_bytes()).hexdigest()
                    rows.append((rel, item.stat().st_mtime_ns, digest))
                else:
                    rows.append((rel, item.stat().st_mtime_ns, "<dir>"))
            return tuple(rows)

        with tempfile.TemporaryDirectory(prefix="eessi-sitepath-real-lmod-") as tmp:
            root = Path(tmp)
            site = root / "site" / "modules" / "all"
            eessi = root / "eessi" / "modules" / "all"
            mod_site = _write_module(site, "SiteApp/1.0", 'setenv("SITE_APP", "1")\n')
            mod_eessi = _write_module(eessi, "GROMACS/2024.1", 'setenv("GMX", "1")\n')

            spider_bin = Path("/usr/share/lmod/lmod/libexec/spider")
            spider_lua = subprocess.check_output(
                [str(spider_bin), "-o", "spiderT", f"{site}:{eessi}"],
                text=True,
            )

            sys_cache = root / "sys_cache"
            sys_cache.mkdir()
            sys_spider = sys_cache / "spiderT.lua"
            sys_spider.write_text(spider_lua, encoding="utf-8")
            sys_ts = sys_cache / "timestamp"
            sys_ts.write_text("1700000000\n", encoding="utf-8")
            future = max(mod_site.stat().st_mtime, mod_eessi.stat().st_mtime) + 100
            os.utime(sys_spider, (future, future))
            os.utime(sys_ts, (future, future))

            lmodrc = root / "lmodrc.lua"
            lmodrc.write_text(
                f'scDescriptT = {{\n  {{\n    ["dir"] = "{sys_cache}",\n'
                f'    ["timestamp"] = "{sys_ts}",\n  }},\n}}\n',
                encoding="utf-8",
            )

            real_home = root / "real_home"
            real_home.mkdir()
            real_xdg = root / "real_xdg"
            real_xdg_lmod = real_xdg / "lmod"
            real_xdg_lmod.mkdir(parents=True)
            xdg_spider = real_xdg_lmod / "spiderT.lua"
            xdg_spider.write_text(spider_lua, encoding="utf-8")
            os.utime(xdg_spider, (future, future))

            real_cachedir = root / "real_lmod_cachedir"
            real_cachedir.mkdir()
            work_tmp = root / "work_tmp"
            work_tmp.mkdir()

            mountinfo = root / "mountinfo"
            mountinfo.write_text(MOUNT, encoding="utf-8")

            before_home = snapshot_tree(real_home)
            before_xdg = snapshot_tree(real_xdg)
            before_cachedir = snapshot_tree(real_cachedir)

            proc = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "share" / "sitepath.py"),
                    "--site",
                    str(site),
                    "--eessi",
                    str(eessi),
                    "--modulepath",
                    f"{site}:{eessi}",
                    "--mountinfo",
                    str(mountinfo),
                    "--check-cache",
                ],
                env={
                    "PATH": "/usr/bin:/bin",
                    "HOME": str(real_home),
                    "XDG_CACHE_HOME": str(real_xdg),
                    "LMOD_CACHEDIR": str(real_cachedir),
                    "LMOD_CMD": str(lmod),
                    "LMOD_RC": str(lmodrc),
                    "LMOD_CACHED_LOADS": "yes",
                    "LMOD_SHORT_TIME": "0",
                    "TMPDIR": str(work_tmp),
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn(
                f"ok   configured cache in {sys_cache} is readable and current",
                proc.stdout,
            )
            self.assertIn(
                "ok   cached module -t avail inventory matches LMOD_IGNORE_CACHE=yes (2 entries)",
                proc.stdout,
            )
            self.assertEqual(snapshot_tree(real_home), before_home)
            self.assertEqual(snapshot_tree(real_xdg), before_xdg)
            self.assertEqual(snapshot_tree(real_cachedir), before_cachedir)
            self.assertEqual(list(work_tmp.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
