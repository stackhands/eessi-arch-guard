"""Build a fake EESSI tree, archdetect stub, nvidia-smi, and LMOD_CMD."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import private_tmp  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
# The program under test is the static binary: TOOL_BIN names it, and unset
# it is the one `zig build -Drelease=true` leaves in zig-out/bin.
TOOL_BIN = os.environ.get("TOOL_BIN", "")
PROGRAM = Path(TOOL_BIN or ROOT / "zig-out" / "bin" / "eessi-arch-guard").resolve()
GUARD_ARGV = [str(PROGRAM)]
if not PROGRAM.is_file():
    raise RuntimeError(
        "the program under test %s does not exist; run `zig build -Drelease=true` "
        "or set TOOL_BIN" % PROGRAM
    )


STUBS = Path(__file__).resolve().parent / "stubs"
FAKE_LMOD = Path(__file__).resolve().parent / "fake_lmod.py"

CPU_MATCHES = {
    "zen2": "x86_64/amd/zen2:x86_64/generic",
    "zen4": "x86_64/amd/zen4:x86_64/amd/zen3:x86_64/amd/zen2:x86_64/generic",
    "zen5": "x86_64/amd/zen5:x86_64/amd/zen4:x86_64/amd/zen3:x86_64/generic",
    "generic": "x86_64/generic",
}


class Site:
    def __init__(self, directory: str = "") -> None:
        if directory:
            os.makedirs(directory, exist_ok=True)
            self.root = tempfile.mkdtemp(prefix="eessi-arch-guard-", dir=directory)
        else:
            self.root = tempfile.mkdtemp(prefix="eessi-arch-guard-")
        self.prefix = os.path.join(self.root, "prefix")
        self.init = os.path.join(self.prefix, "init")
        self.bindir = os.path.join(self.root, "bin")
        self.log = os.path.join(self.root, "lmod.log")
        self.arch_log = os.path.join(self.root, "arch.log")
        self.cpuinfo = os.path.join(self.root, "cpuinfo")
        self.bash_env_log = os.path.join(self.root, "bash-env.log")
        # The TMPDIR of the guard runs base_env sets up, private to the site.
        self.tmpdir = os.path.join(self.root, "temp")
        os.mkdir(self.tmpdir, 0o700)
        os.makedirs(self.init)
        os.makedirs(self.bindir)
        shutil.copy(STUBS / "eessi_archdetect.sh", os.path.join(self.init, "eessi_archdetect.sh"))
        os.chmod(os.path.join(self.init, "eessi_archdetect.sh"), 0o755)
        shutil.copy(STUBS / "nvidia-smi", os.path.join(self.bindir, "nvidia-smi"))
        os.chmod(os.path.join(self.bindir, "nvidia-smi"), 0o755)
        wrapper = os.path.join(self.bindir, "lmod")
        with open(wrapper, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/bash\nexec /usr/bin/python3 -I %s \"$@\"\n" % FAKE_LMOD)
        os.chmod(wrapper, 0o755)
        self.lmod = wrapper

    def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def add_cpu(self, subdir: str) -> None:
        base = os.path.join(self.prefix, "software", "linux", subdir)
        os.makedirs(os.path.join(base, "software"), exist_ok=True)
        os.makedirs(os.path.join(base, "modules", "all"), exist_ok=True)
        os.makedirs(os.path.join(base, "bin"), exist_ok=True)

    def add_accel(self, cpu: str, accel: str) -> None:
        base = os.path.join(self.prefix, "software", "linux", cpu, accel, "modules", "all")
        os.makedirs(base, exist_ok=True)

    def add_module(self, cpu: str, name: str, accel: str = "") -> None:
        if accel:
            root = os.path.join(self.prefix, "software", "linux", cpu, accel, "modules", "all")
        else:
            root = os.path.join(self.prefix, "software", "linux", cpu, "modules", "all")
        path = os.path.join(root, name + ".lua")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("-- %s\n" % name)
        soft = os.path.join(
            self.prefix,
            "software",
            "linux",
            cpu,
            "software",
            name.split("/")[0],
            name.split("/")[1],
            "bin",
        )
        os.makedirs(soft, exist_ok=True)

    def write_cpuinfo(self, kind: str = "", matches: str = "", fail: bool = False) -> None:
        lines = ["vendor_id : AuthenticAMD", "flags : fpu avx2"]
        if kind == "zen2":
            lines.append("model name : AMD EPYC 7702 64-Core Processor")
        elif kind == "zen4":
            lines.append("model name : AMD EPYC 9654 96-Core Processor")
        elif kind == "zen5":
            lines.append("model name : AMD EPYC 9755 128-Core Processor")
        if matches or kind in CPU_MATCHES:
            lines.append("eessi_arch_matches: %s" % (matches or CPU_MATCHES[kind]))
        if fail:
            lines.append("eessi_arch_fail: 1")
        with open(self.cpuinfo, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")


def on_task_cpus(cpuinfo: str, visible: int):
    """``cpuinfo`` renumbered so that its first ``visible`` processors are ones
    this process may run on, with those IDs.

    A fixture numbers its processors from 0, and a host may give a test only
    processors with higher IDs, so a test that binds the task to fixture
    processors renumbers them first. The first blocks take the lowest IDs of
    the affinity mask in order, and the blocks past the mask take the IDs
    after its highest one. On a host that may use processors 0 to 7 the
    fixture keeps its numbers. Skips when the mask has fewer than
    ``visible`` processors.
    """
    allowed = sorted(os.sched_getaffinity(0))
    if len(allowed) < visible:
        raise unittest.SkipTest(
            "the test needs %d processors and may run on %d" % (visible, len(allowed))
        )
    pattern = re.compile(r"^(processor[ \t]*:[ \t]*)[0-9]+", re.M)
    blocks = len(pattern.findall(cpuinfo))
    ids = allowed[:blocks]
    ids += range(allowed[-1] + 1, allowed[-1] + 1 + blocks - len(ids))
    numbers = iter(ids)
    text = pattern.sub(lambda m: m.group(1) + str(next(numbers)), cpuinfo)
    return text, ids[:visible]


def base_env(
    site: Site, cpu: str, modules, accel: str = "", gpu: str = "none", gpu_cc: str = "9.0"
):
    names = ["EESSI/2025.06", *modules]
    modulepath = [os.path.join(site.prefix, "software", "linux", cpu, "modules", "all")]
    if accel:
        modulepath.insert(
            0, os.path.join(site.prefix, "software", "linux", cpu, accel, "modules", "all")
        )
    bins = [site.bindir]
    for name in modules:
        soft = os.path.join(
            site.prefix,
            "software",
            "linux",
            cpu,
            "software",
            name.split("/")[0],
            name.split("/")[1],
            "bin",
        )
        bins.append(soft)
    bins.append(os.path.join(site.prefix, "software", "linux", cpu, "bin"))
    bins.extend(["/usr/bin", "/bin"])
    env = {
        "PATH": ":".join(bins),
        "HOME": "/tmp",
        "TMPDIR": site.tmpdir,
        "USER": "tester",
        "LANG": "C",
        "LC_ALL": "C",
        "EESSI_PREFIX": site.prefix,
        "EESSI_OS_TYPE": "linux",
        "EESSI_INIT_PREFIX": site.init,
        "EESSI_VERSION": "2025.06",
        "EESSI_SOFTWARE_SUBDIR": cpu,
        "EESSI_PROC_CPUINFO": site.cpuinfo,
        "LOADEDMODULES": ":".join(names),
        "MODULEPATH": ":".join(modulepath),
        "LMOD_CMD": site.lmod,
        "EESSI_TEST_GPU": gpu,
        # The stub's own nvidia-smi, so a host nvidia-smi on a PATH the test
        # replaces never answers for the site.
        "EESSI_TEST_NVIDIA_SMI": os.path.join(site.bindir, "nvidia-smi"),
        "EESSI_TEST_GPU_CC": gpu_cc,
        # Survives a purge the way /usr/bin does, so nvidia-smi is still found
        # when the reloaded EESSI module runs archdetect.
        "EESSI_TEST_BASE_PATH": site.bindir + ":/usr/bin:/bin",
        "EESSI_TEST_EVENT_LOG": site.log,
        "EESSI_TEST_ARCH_LOG": site.arch_log,
        "_ModuleTable001_": "table-for-" + cpu.replace("/", "-"),
    }
    if accel:
        env["EESSI_ACCEL_SUBDIR"] = accel
    for name in modules:
        soft = os.path.join(
            site.prefix,
            "software",
            "linux",
            cpu,
            "software",
            name.split("/")[0],
            name.split("/")[1],
        )
        token = "".join(ch for ch in name.split("/")[0] if ch.isalnum()).upper()
        env["EBROOT" + token] = soft
        env["EBVERSION" + token] = name.split("/")[1]
    return env


def run_guard(env, args=()):
    proc = subprocess.run(
        [*GUARD_ARGV, *args],
        capture_output=True,
        encoding="utf-8",
        env=env,
        check=False,
    )
    return proc


STUB_TARGET = "x86_64/amd/zen4"
STUB_PLANNER = """#!/usr/bin/python3
import sys
sys.stderr.buffer.write(%r)
sys.stdout.buffer.write(%r)
if %r:
    easystack = sys.argv[sys.argv.index('--easystack') + 1]
    with open(easystack, encoding='utf-8') as handle:
        names = [line.strip()[2:] for line in handle if line.startswith('  - ')]
    for name in names:
        print(name + ' is flagged on x86_64: it has never been installed there')
sys.exit(%d)
"""


def run_check_submit_stub(
    program,
    stderr: bytes = b"",
    stdout: bytes = b"",
    status: int = 3,
    flagged: bool = False,
    extra_env=None,
    timeout: float = 30.0,
):
    """Run ``program --check-submit --json`` with a stub in place of eb-stack.

    The stub is an executable script in a tree of its own, next to a prefix
    with one module. It writes ``stderr`` and ``stdout`` as bytes and exits
    with ``status``. With ``flagged`` set it also writes a flagged line for
    each module of the easystack file it is given, after ``stdout``. The guard
    runs with a clean environment plus ``extra_env``. The result is the exit
    status, the parsed standard output (``None`` when it is empty) and the
    standard error as bytes.
    """
    with tempfile.TemporaryDirectory(prefix="eag-stub-") as tmp:
        root = Path(tmp)
        prefix = root / "prefix"
        modules = prefix / "software" / "linux" / STUB_TARGET / "modules" / "all" / "R"
        modules.mkdir(parents=True)
        (modules / "4.4.2.lua").write_text("--\n", encoding="utf-8")
        stub = root / "eb-stack"
        stub.write_text(STUB_PLANNER % (stderr, stdout, flagged, status), encoding="utf-8")
        stub.chmod(0o755)
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(root / "home"),
            "LANG": "C",
            "LC_ALL": "C",
            "EESSI_PREFIX": str(prefix),
            "EESSI_OS_TYPE": "linux",
            "LOADEDMODULES": "R/4.4.2",
        }
        env.update(extra_env or {})
        argv = [*program, "--check-submit", "--json", "--target", STUB_TARGET]
        argv += ["--eb-stack", str(stub)]
        proc = run_with_timeout(argv, env=env, timeout=timeout, text=False)
        payload = json.loads(proc.stdout) if proc.stdout else None
        return proc.returncode, payload, proc.stderr


def log_lines(path: str):
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as handle:
        return [line.rstrip("\n") for line in handle if line.strip()]


def run_with_timeout(argv, env=None, timeout: float = 2.0, text: bool = True, cwd=None, input=None):
    """Run ``argv`` with a bounded wall-clock timeout, in ``cwd`` if given.

    ``input`` is fed to the program.

    The streams are decoded as UTF-8, whatever the locale, unless ``text`` is
    false. Then they stay bytes, and ``env`` may hold bytes, so a comparison
    sees input and output that are not valid UTF-8.
    """
    return subprocess.run(
        list(argv),
        input=input,
        env=env,
        cwd=cwd,
        capture_output=True,
        encoding="utf-8" if text else None,
        timeout=timeout,
        check=False,
    )
