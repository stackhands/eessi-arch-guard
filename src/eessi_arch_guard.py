#!/usr/bin/python3 -I
"""Reload EESSI modules when a Slurm task lands on another CPU or GPU target.

The TaskProlog target is this file. See DESIGN.md for the design record.
The documentation is under docs/.
Output is either Slurm TaskProlog lines (export, unset, print) or, with
--shell, bash assignments for eval. Default TaskProlog mode exits 0 unless
a chained site prolog exits non-zero. Opt-in --strict returns 78 for failed
decisions and unexpected exceptions. A non-zero status cancels the task.
"""

from __future__ import annotations

import argparse
import datetime
import fcntl
import hashlib
import json
import locale
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
from typing import Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple

__version__ = "0.3.1"

# The cache key. EESSI_ARCH_GUARD is absent on purpose.
KEY_FIELDS = (
    "EESSI_SOFTWARE_SUBDIR",
    "EESSI_ACCEL_SUBDIR",
    "LOADEDMODULES",
    "MODULEPATH",
    "EESSI_INIT_PREFIX",
    "EESSI_PROC_CPUINFO",
    "PATH",
)

STARTUP_VARS = (
    "BASH_ENV",
    "ENV",
    "SHELLOPTS",
    "BASHOPTS",
    "BASH_XTRACEFD",
    "PS4",
)

# Never publish these through the prolog. TMPDIR is created by slurmstepd
# after the prolog; exporting it would change that directory.
NEVER_EMIT = frozenset(
    {
        "_",
        "SHLVL",
        "PWD",
        "OLDPWD",
        "TMPDIR",
        "BASH_ENV",
        "ENV",
        "SHELLOPTS",
        "BASHOPTS",
        "BASH_XTRACEFD",
        "PS4",
        "BASH_ARGC",
        "BASH_ARGV",
        "BASH_LINENO",
        "BASH_SOURCE",
        "BASH_VERSINFO",
        "BASH_VERSION",
        "DIRSTACK",
        "EPOCHREALTIME",
        "EPOCHSECONDS",
        "FUNCNAME",
        "GROUPS",
        "HOSTNAME",
        "HOSTTYPE",
        "IFS",
        "MACHTYPE",
        "OPTERR",
        "OPTIND",
        "OSTYPE",
        "PIPESTATUS",
        "PPID",
    }
)

# A newline here must not abort an otherwise good restore.
SOFT_DROP = frozenset({"PS1", "PS2", "PS3", "PROMPT_COMMAND"})

_MOD_SEGMENT = r"[A-Za-z0-9_][A-Za-z0-9._+-]*"
MODULE_NAME_RE = re.compile(rf"^{_MOD_SEGMENT}(?:/{_MOD_SEGMENT})+$")
YEAR_MODULE_RE = re.compile(r"^[0-9]{4}$")
EESSI_MODULE_RE = re.compile(rf"^EESSI/{_MOD_SEGMENT}$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ACCEL_RE = re.compile(r"^accel/(?:nvidia/cc[0-9]+|amd/gfx[0-9a-f]+)$")
_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.+-]+$")
_SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

DIAGNOSE_LIMIT = 32
MAX_MODULE_NAME = 256
MAX_ARCH_STDOUT = 65536
# Slurm 17.11 src/slurmd/slurmstepd/task.c `_run_script_and_set_env` reads a
# TaskProlog line with fgets(buf, sizeof(buf) - 1) and `char buf[4096]`.
# fgets stores at most size-1 bytes and size is sizeof(buf)-1, so the line
# is 4094 bytes. Current master does not cap the line. Stay inside the
# 17.11 read so one export cannot continue as a second directive.
PROLOG_LINE_MAX = 4094
# Slurm src/common/read_config.h DEFAULT_UNKILLABLE_TIMEOUT. One deadline
# covers the whole decision.
DEFAULT_TIMEOUT = 60.0
STRICT_FAILURE_EXIT = 78
_STRICT_MODE = False
# subprocess waits for a command with poll(), whose timeout is a C int of
# milliseconds: a wait past 2**31 - 1 ms raises OverflowError.
TIMEOUT_MAX = 2147483.647
# flock has no timeout and Python retries EINTR, so the wait is a
# non-blocking loop. 0.05 s still ends a 1 s deadline in one shot, and a
# 192-task step does not spin on the lock.
LOCK_POLL_SECONDS = 0.05
# After SIGKILL, a setsid grandchild can keep the pipe open. 5 s is enough
# for a killed child to drop its fds. _finish_child may wait this long in
# communicate and twice more in wait, so the worst case is
# 3x POST_KILL_WAIT_SECONDS.
POST_KILL_WAIT_SECONDS = 5.0

APPLY_SH = """\
source "$1" >/dev/null || exit 4
exec env -0
"""


def _seconds_label(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return str(value)


HELP = """\
usage: eessi-arch-guard [--shell] [--strict] [--dry-run] [--json]
                        [--chain PATH] [--timeout SEC] [--syslog TARGET]
                        [--restore] [-h|--help] [--version]
       eessi-arch-guard --check-submit --partition NAME [--constraint NAME]
                        [--eb-stack PATH] [--json]
       eessi-arch-guard --check-submit --target TARGET [--eb-stack PATH] [--json]

Slurm TaskProlog: when the loaded EESSI CPU or accelerator target is not this
node's, print export/unset lines for the reloaded module environment.
--check-submit checks LOADEDMODULES against a mapped target without changing it.
--partition NAME selects submit_target.partition.NAME from the site config.
--constraint NAME also requires submit_target.constraint.NAME to select that target.
--target TARGET checks against that EESSI CPU target, such as x86_64/amd/zen4,
with no site config.
--eb-stack PATH selects the eb-stack executable used for the read-only check.
--shell prints bash for: eval "$(eessi-arch-guard --shell)"
--strict returns 78 on a failed decision or an unexpected exception.
--dry-run evaluates config, chain, probes, and decision without writing state.
--json prints one line of JSON with schema_version 1. On a decision run it
replaces prolog or shell lines; with --dry-run it also carries the diagnostic
keys; with --check-submit it replaces the text summary.
--chain PATH runs an existing task prolog first. Slurm does not pass arguments,
so a site sets this in a wrapper or in a root-owned eessi-arch-guard.conf.
--timeout SEC bounds the decision. The default is %s seconds.
--syslog TARGET sends structured RFC 5424 or journald decision events.
--restore reads a JSON restore request on stdin and writes the JSON result.
-h, --help print this text. --version prints the version.
""" % _seconds_label(DEFAULT_TIMEOUT)

DECISION_JSON_SCHEMA_VERSION = 1


class Outcome(NamedTuple):
    """Environment delta, optional message, kind, and optional detail.

    ``kind`` is ``skip``, ``noop``, ``reloaded``, or ``failed``.
    """

    changes: Dict[str, Optional[str]]
    message: Optional[str]
    kind: str
    detail: Optional[str] = None


class CpuProbe(NamedTuple):
    """CPU walk result.

    ``status`` is ``ok``, ``none``, or ``error``. ``override`` is the
    installed directory when it differs from the unfiltered archdetect choice.
    """

    status: str  # ok, none, error
    target: str
    candidates: Tuple[str, ...]
    error: str
    # Set when the installed directory archdetect would pick needs a flag
    # this CPU does not have. The reload passes it as
    # EESSI_SOFTWARE_SUBDIR_OVERRIDE so the EESSI module follows the fallback.
    override: str = ""


class AccelProbe(NamedTuple):
    """Accelerator probe. ``status`` is ``ok``, ``none``, or ``error``."""

    status: str  # ok, none, error
    subdir: str
    error: str


class LoadedModules(NamedTuple):
    """The one ``EESSI/<version>`` name, the other loaded names, and an error token."""

    eessi: str
    others: Tuple[str, ...]
    error: str


class RestoreResult(NamedTuple):
    """Helper result: success flag, restored environment, missing modules, error text.

    ran is False when the restore stopped before Lmod could answer: no
    workspace, no helper, or no usable reply from it.
    """

    ok: bool
    env: Optional[Dict[str, str]]
    missing: Tuple[str, ...]
    error: str
    ran: bool = True


def one_line(text: str, limit: int = 500) -> str:
    """Single line, no controls, bounded. Safe to place after print or on stderr."""
    chars = []
    for char in text:
        if char in "\r\n\0" or ord(char) < 32 or ord(char) == 127:
            chars.append(" ")
        else:
            chars.append(char)
    cleaned = "".join(chars).strip()
    if len(cleaned) > limit:
        return cleaned[: limit - 3] + "..."
    return cleaned


def module_name_ok(name: str) -> bool:
    """True when ``name`` matches the module grammar and is at most 256 characters."""
    return (
        isinstance(name, str)
        and 0 < len(name) <= MAX_MODULE_NAME
        and (
            MODULE_NAME_RE.fullmatch(name) is not None or YEAR_MODULE_RE.fullmatch(name) is not None
        )
    )


def safe_rel_subdir(value: str) -> bool:
    """Relative EESSI subdirectory: no empty, absolute, or .. segments."""
    if not value or value.startswith("/") or "\\" in value:
        return False
    parts = value.split("/")
    for part in parts:
        if part in (".", "..") or _PATH_SEGMENT_RE.fullmatch(part) is None:
            return False
    return True


def safe_absolute(path: str) -> bool:
    """Absolute path: starts with ``/``, no newline, CR or NUL, no ``..`` segment."""
    if not path or not path.startswith("/") or any(c in path for c in "\n\r\0"):
        return False
    return all(part != ".." for part in path.split("/"))


def strip_startup(env: Mapping[str, str]) -> Dict[str, str]:
    """A copy of ``env`` without STARTUP_VARS, which make bash read a file or trace."""
    child = dict(env)
    for key in STARTUP_VARS:
        child.pop(key, None)
    return child


def value_emit_ok(value: str, multiline: bool) -> bool:
    """Whether a value can be published.

    A NUL byte never can; a newline or carriage return only when
    ``multiline`` is true, for the quoted values of the shell output.
    """
    if "\0" in value:
        return False
    if not multiline and ("\n" in value or "\r" in value):
        return False
    return True


def _never_touch(name: str) -> bool:
    return (
        name in NEVER_EMIT
        or name.startswith("BASH_FUNC_")
        or name.startswith("SLURM_")
    )


def environment_delta(
    old: Mapping[str, str],
    new: Mapping[str, str],
    multiline: bool,
) -> Optional[Dict[str, Optional[str]]]:
    """Changes to publish. None means a value cannot be carried: publish nothing."""
    changes: Dict[str, Optional[str]] = {}
    unsafe = False
    for key in set(old) | set(new):
        if _never_touch(key):
            continue
        old_v = old.get(key)
        new_v = new.get(key)
        if old_v == new_v:
            continue
        if ENV_NAME_RE.fullmatch(key) is None:
            if key in SOFT_DROP:
                continue
            unsafe = True
            continue
        if new_v is None:
            changes[key] = None
            continue
        if not value_emit_ok(new_v, multiline):
            if key in SOFT_DROP:
                continue
            unsafe = True
            continue
        changes[key] = new_v
    if unsafe:
        return None
    return changes


def _fit_encoded(raw: bytes, room: int, encoding: str) -> bytes:
    """Keep a prefix that is valid text and at most `room` bytes."""
    if len(raw) <= room:
        return raw
    cut = raw[:room]
    while cut:
        try:
            cut.decode(encoding)
            return cut
        except UnicodeDecodeError:
            cut = cut[:-1]
    return b""


def message_bytes(text: str) -> bytes:
    """One locale-encoded message, inside the prolog line buffer."""
    cleaned = one_line(text)
    encoding = locale.getpreferredencoding(False) or "ascii"
    try:
        raw = cleaned.encode(encoding, errors="replace")
    except LookupError:
        encoding = "ascii"
        raw = cleaned.encode(encoding, errors="replace")
    room = PROLOG_LINE_MAX - len(b"print ") - 1
    return _fit_encoded(raw, room, encoding)


def write_fd(fd: int, data: bytes) -> None:
    """Write bytes to a descriptor. A failed write is dropped."""
    if not data:
        return
    view = memoryview(data)
    while len(view):
        try:
            written = os.write(fd, view)
        except OSError:
            return
        if written <= 0:
            return
        view = view[written:]


def render_bytes(
    changes: Mapping[str, Optional[str]],
    message: Optional[str],
    shell: bool,
    announce: bool,
) -> bytes:
    """Prolog or shell bytes. Export values keep the bytes they stand for."""
    chunks = []
    for key in sorted(changes):
        if ENV_NAME_RE.fullmatch(key) is None:
            raise ValueError("refusing to emit an unsafe name")
        value = changes[key]
        if value is None:
            chunks.append(b"unset " + key.encode("ascii") + b"\n")
            continue
        if not isinstance(value, str):
            raise ValueError("refusing to emit a non-text value")
        if shell:
            line = "export %s=%s" % (key, shlex.quote(value))
            chunks.append(os.fsencode(line) + b"\n")
        else:
            if not value_emit_ok(value, multiline=False):
                raise ValueError("refusing to emit an unsafe prolog value")
            line = b"export " + key.encode("ascii") + b"=" + os.fsencode(value) + b"\n"
            if len(line) > PROLOG_LINE_MAX:
                raise ValueError("refusing to emit a prolog line past the Slurm 17.11 read")
            chunks.append(line)
    if message and announce and not shell:
        line = b"print " + message_bytes(message) + b"\n"
        if len(line) > PROLOG_LINE_MAX:
            raise ValueError("refusing to emit a prolog line past the Slurm 17.11 read")
        chunks.append(line)
    return b"".join(chunks)


def parse_loaded_modules(env: Mapping[str, str]) -> LoadedModules:
    """The EESSI module and the other modules of LOADEDMODULES, in order.

    The error token is set instead when the list is empty, holds a name that
    is not safe to pass to Lmod, or has no single ``EESSI/<version>`` entry.
    """
    raw = env.get("LOADEDMODULES", "")
    names = [part for part in raw.split(":") if part]
    if not names:
        return LoadedModules("", (), "LOADEDMODULES is empty")
    if any(not module_name_ok(name) for name in names):
        return LoadedModules("", (), "unsafe-module-name")
    eessi = [name for name in names if name.startswith("EESSI/")]
    if len(eessi) != 1 or EESSI_MODULE_RE.fullmatch(eessi[0]) is None:
        return LoadedModules("", (), "eessi-module-name")
    others = tuple(name for name in names if name != eessi[0])
    return LoadedModules(eessi[0], others, "")


def parse_cpu_line(stdout: str) -> Optional[List[str]]:
    """The colon-separated candidates of the one line ``cpupath`` prints.

    None when the output holds a NUL or another control character, is longer
    than MAX_ARCH_STDOUT, or is not exactly one non-empty line.
    """
    if "\0" in stdout or len(stdout) > MAX_ARCH_STDOUT:
        return None
    parts = [line for line in stdout.splitlines() if line != ""]
    if len(parts) != 1:
        return None
    line = parts[0]
    if any(ord(char) < 32 for char in line):
        return None
    return line.split(":")


class _Deadline(BaseException):
    """The decision's time limit ran out. Not an Exception, so it is not cached."""


class _Terminated(BaseException):
    """SIGTERM arrived during the decision. Not an Exception, so it is not cached."""


class DecisionLimit:
    """One deadline for the lock, both archdetect runs, and the restore helper."""

    def __init__(self, seconds: float) -> None:
        """Start the deadline ``seconds`` from now; ``install`` takes SIGTERM."""
        self.seconds = seconds
        self.deadline = time.monotonic() + seconds
        self.child_pgid: Optional[int] = None
        self.terminated = False
        self.committing = False
        self._previous = None

    def remaining(self) -> float:
        """Seconds left before the deadline; zero or less once it has passed."""
        return self.deadline - time.monotonic()

    def check(self) -> None:
        """Stop the decision after a SIGTERM or once no time is left.

        Raises ``_Terminated`` or ``_Deadline``; neither while ``committing``,
        when the result is being written to the cache.
        """
        if self.committing:
            return
        if self.terminated:
            raise _Terminated()
        if self.remaining() <= 0:
            raise _Deadline()

    def install(self) -> None:
        """Handle SIGTERM: note it and kill the running command's process group."""
        self._previous = signal.signal(signal.SIGTERM, self._handle)

    def restore(self) -> None:
        """Put back the SIGTERM handler ``install`` replaced."""
        if self._previous is not None:
            signal.signal(signal.SIGTERM, self._previous)
            self._previous = None

    def _handle(self, _signum: int, _frame: object) -> None:
        self.terminated = True
        if self.committing:
            return
        pgid = self.child_pgid
        if not pgid:
            return
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            return

    def hold_signals(self) -> None:
        """Block SIGTERM while a command starts, until ``child_pgid`` names it."""
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})

    def release_signals(self) -> None:
        """Unblock SIGTERM; one that arrived while it was held is handled now."""
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})


_ACTIVE: Optional[DecisionLimit] = None


def _killpg(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        return


def _finish_child(proc: subprocess.Popen) -> Tuple[bytes, bytes]:
    """Collect output after a kill.

    A setsid grandchild can hold the pipe. This may wait in communicate
    and then twice in wait, so the worst case is 3x POST_KILL_WAIT_SECONDS.
    """
    try:
        return proc.communicate(timeout=POST_KILL_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        for stream in (proc.stdout, proc.stderr, proc.stdin):
            if stream is None:
                continue
            try:
                stream.close()
            except OSError:
                pass
        if proc.poll() is None:
            _killpg(proc.pid)
            try:
                proc.wait(timeout=POST_KILL_WAIT_SECONDS)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=POST_KILL_WAIT_SECONDS)
                except subprocess.TimeoutExpired:
                    pass
        return b"", b""


def run_captured(
    argv: Sequence[str],
    env: Mapping[str, str],
    input_bytes: Optional[bytes] = None,
    new_session: bool = False,
) -> subprocess.CompletedProcess:
    """Run a command. Under a decision limit, its session is killed at the deadline."""
    limit = _ACTIVE
    if limit is None or not new_session:
        return subprocess.run(
            list(argv),
            input=input_bytes,
            capture_output=True,
            env=dict(env),
            check=False,
        )
    limit.check()
    limit.hold_signals()
    proc = None
    try:
        proc = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(env),
            start_new_session=True,
        )
        limit.child_pgid = proc.pid
    finally:
        limit.release_signals()
    if proc is None:
        raise OSError("could not start")
    try:
        if limit.terminated and not limit.committing:
            _killpg(proc.pid)
            _finish_child(proc)
            raise _Terminated()
        remaining = limit.remaining()
        if remaining <= 0:
            _killpg(proc.pid)
            _finish_child(proc)
            raise _Deadline()
        try:
            out, err = proc.communicate(input=input_bytes, timeout=remaining)
        except subprocess.TimeoutExpired:
            _killpg(proc.pid)
            _finish_child(proc)
            raise _Deadline()
        if limit.terminated and not limit.committing:
            raise _Terminated()
        return subprocess.CompletedProcess(list(argv), proc.returncode, out, err)
    finally:
        if limit.child_pgid == proc.pid:
            limit.child_pgid = None
        if proc.poll() is None:
            _killpg(proc.pid)
            _finish_child(proc)


def _run_bash(args: Sequence[str], env: Mapping[str, str]) -> subprocess.CompletedProcess:
    return run_captured(
        ["bash", "--noprofile", "--norc", "--", *args],
        strip_startup(env),
        new_session=_ACTIVE is not None,
    )


# GCC -march names whose PTA mask includes PTA_WAITPKG (tpause, umwait,
# umonitor). Leaves use '_' where the march spells '-'. Taken from GCC 16
# gcc/config/i386/i386.h (branch releases/gcc-16): the set GCC 15.2.0
# carried plus novalake; znver6, GCC 16's other new mask, has no WAITPKG.
# archdetect's x86 spec does not require the flag, so a VM that hides it
# is still offered that directory. A binary built with the march then
# SIGILLs.
WAITPKG_MARCHES = frozenset(
    (
        "alderlake",
        "arrowlake",
        "arrowlake_s",
        "clearwaterforest",
        "diamondrapids",
        "grandridge",
        "graniterapids",
        "graniterapids_d",
        "novalake",
        "pantherlake",
        "sapphirerapids",
        "sierraforest",
        "tremont",
    )
)

_CPUINFO_FLAGS_RE = re.compile(r"(?im)^flags[ \t]*:[ \t]*(.*)$")
_CPUINFO_READ_MAX = 16 * 1024 * 1024


def read_cpu_flags(path: str) -> frozenset:
    """Intersection of every flags line. One CPU without the flag rejects it."""
    if not path:
        return frozenset()
    try:
        with open(path, "rb") as handle:
            blob = handle.read(_CPUINFO_READ_MAX + 1)
    except OSError:
        return frozenset()
    text = blob[:_CPUINFO_READ_MAX].decode("utf-8", "replace")
    found = []
    for match in _CPUINFO_FLAGS_RE.finditer(text):
        found.append(frozenset(match.group(1).split()))
    if not found:
        return frozenset()
    common = found[0]
    for item in found[1:]:
        common &= item
    return common


# sched_getaffinity and cpuset lists name processors in this range. A bind
# mask with a bit past it is ignored rather than applied in part.
_CPU_ID_MAX = 4096
_DECIMAL_CAP = 1 << 64
_CPU_ITEM_RE = re.compile(r"[0-9]+(?:-[0-9]+)?")
_CPU_HOSTLIST_RE = re.compile(r"(?:cpu)?\[([^\[\]]+)\]", re.IGNORECASE)
_PROCESSOR_LINE_RE = re.compile(r"(?i)^processor[ \t]*:[ \t]*(\d+)\s*$")
_FLAG_KEYS = frozenset(("flags", "features", "isa"))
_IDENTITY_KEYS = frozenset(
    (
        "vendor_id",
        "cpu_family",
        "model",
        "model_name",
        "cpu_implementer",
        "cpu_part",
        "mvendorid",
    )
)


def _decimal_value(digits: str) -> int:
    """``int(digits)`` for text of decimal digits, at most ``_DECIMAL_CAP``.

    ``int`` refuses more digits than ``sys.get_int_max_str_digits()``, leading
    zeros included, and raises ValueError. A processor number is far below
    the cap, so a long run of digits is read one digit at a time and a value
    past the cap is the cap. The Zig guard reads such text the same way.
    """
    if len(digits) <= 600:
        return int(digits)
    value = 0
    for char in digits:
        value = min(value * 10 + unicodedata.decimal(char), _DECIMAL_CAP)
    return value


def _hex_mask_cpus(text: str) -> Optional[frozenset]:
    """Processors in one hex mask, or the union of a comma-separated list.

    Slurm stores ``--cpu-bind=mask_cpu:0x2,0x4`` in ``SLURM_CPU_BIND_LIST``
    as ``0x2,0x4``, one mask per task. None when a piece is not a mask, or
    when a mask names a bit at or past ``_CPU_ID_MAX``.
    """
    found = set()
    for part in text.split(","):
        piece = part.strip(" ")
        if not piece.lower().startswith("0x"):
            return None
        try:
            mask = int(piece, 16)
        except ValueError:
            return None
        if mask < 0:
            return None
        bit = 0
        while mask and bit < _CPU_ID_MAX:
            if mask & 1:
                found.add(bit)
            mask >>= 1
            bit += 1
        if mask:
            return None
    return frozenset(found)


def parse_cpu_spec(text: str) -> Optional[frozenset]:
    """CPUs in a hex mask, a ``0-3,8`` list, or a ``cpu[0-3]`` hostlist.

    A comma-separated list of hex masks is their union. None means the text
    is not one of those forms. A mask with a bit at or past ``_CPU_ID_MAX``
    is None too: applying the low bits alone would hide a processor the
    mask named.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.lower().startswith("0x"):
        return _hex_mask_cpus(raw)
    if raw.endswith("]"):
        host = _CPU_HOSTLIST_RE.fullmatch(raw)
        if host is None:
            return None
        body = host.group(1)
    elif "[" in raw:
        return None
    else:
        body = raw
    if not body:
        return None
    found = set()
    for raw_part in body.split(","):
        part = raw_part.strip(" ")
        if _CPU_ITEM_RE.fullmatch(part) is None:
            return None
        if "-" in part:
            left, right = part.split("-", 1)
            start, end = _decimal_value(left), _decimal_value(right)
            if end < start or end >= _CPU_ID_MAX:
                return None
            found.update(range(start, end + 1))
        else:
            cpu = _decimal_value(part)
            if cpu >= _CPU_ID_MAX:
                return None
            found.add(cpu)
    return frozenset(found)


def _read_text(path: str, limit: int = 65536) -> Optional[str]:
    try:
        with open(path, "rb") as handle:
            blob = handle.read(limit)
    except OSError:
        return None
    return blob.decode("utf-8", "replace")


class _CgroupMount(NamedTuple):
    root: str
    point: str
    v2: bool
    cpuset: bool


def _unescape_mountinfo(text: str) -> str:
    """mountinfo writes space, tab, newline and backslash as \\ooo.

    Mirrors ``share/sitepath.py`` ``_unescape``. The guard is one file and
    does not import that script.
    """
    if "\\" not in text:
        return text
    out = []
    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 3 < len(text):
            digits = text[i + 1 : i + 4]
            if all("0" <= digit <= "7" for digit in digits):
                value = int(digits, 8)
                if value <= 0xFF:
                    out.append(chr(value))
                    i += 4
                    continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _abs_mount_path(path: str) -> bool:
    return path.startswith("/") and ".." not in path.split("/")


def _trim_mount(path: str) -> str:
    if path == "/":
        return path
    return path.rstrip("/") or "/"


def _controllers_allow(directory: str) -> bool:
    """A readable controllers file that omits cpuset is not a cpuset."""
    listed = _read_text(os.path.join(directory, "cgroup.controllers"))
    if listed is None:
        return True
    return "cpuset" in listed.split()


def _walk_cpuset(directory: str, stop: str) -> Optional[frozenset]:
    """``cpuset.cpus.effective`` at ``directory`` or a parent inside ``stop``.

    At most 64 parents. ``stop`` of ``/`` allows any absolute path. Any
    other stop matches the directory itself or a child (``stop`` + ``/``).
    """
    for _ in range(64):
        if stop != "/" and directory != stop and not directory.startswith(stop + "/"):
            break
        raw = _read_text(os.path.join(directory, "cpuset.cpus.effective"))
        if raw is not None and raw.strip() and _controllers_allow(directory):
            parsed = parse_cpu_spec(raw.strip())
            if parsed is not None:
                return parsed
        if directory == "/":
            break
        parent = os.path.dirname(directory.rstrip("/"))
        if parent == directory:
            break
        directory = parent
    return None


def _cgroup_mounts(mount_text: str) -> List[_CgroupMount]:
    mounts = []
    for line in mount_text.splitlines():
        if " - " not in line:
            continue
        head, tail = line.split(" - ", 1)
        fields = head.split()
        if len(fields) < 6:
            continue
        root = _trim_mount(_unescape_mountinfo(fields[3]))
        point = _trim_mount(_unescape_mountinfo(fields[4]))
        tail_fields = tail.split()
        if not tail_fields or tail_fields[0] not in ("cgroup", "cgroup2"):
            continue
        opts = tail_fields[2] if len(tail_fields) > 2 else ""
        mounts.append(
            _CgroupMount(
                root,
                point,
                tail_fields[0] == "cgroup2",
                "cpuset" in opts.split(","),
            )
        )
    return mounts


def _mount_rel(mount_root: str, cgroup_path: str) -> Optional[str]:
    root = _trim_mount(mount_root)
    path = cgroup_path if cgroup_path.startswith("/") else "/" + cgroup_path
    if root == "/":
        return "" if path == "/" else path
    if path == root:
        return ""
    prefix = root + "/"
    if path.startswith(prefix):
        return path[len(root) :]
    return None


def _join_mount(point: str, rel: str) -> str:
    base = _trim_mount(point)
    if not rel:
        return base
    if base == "/":
        return rel if rel.startswith("/") else "/" + rel
    if rel.startswith("/"):
        return base + rel
    return base + "/" + rel


def _ranked_mounts(
    mounts: Sequence[_CgroupMount], v2: bool, cgroup_path: str
) -> List[Tuple[_CgroupMount, str]]:
    found = []
    for mount in mounts:
        if mount.v2 != v2 or (not v2 and not mount.cpuset):
            continue
        rel = _mount_rel(mount.root, cgroup_path)
        if rel is None:
            continue
        found.append((len(mount.root), mount, rel))
    found.sort(key=lambda item: item[0], reverse=True)
    return [(mount, rel) for _length, mount, rel in found]


def _cgroup_line(line: str) -> Optional[Tuple[str, str, str]]:
    parts = line.split(":", 2)
    if len(parts) != 3:
        return None
    hier, controllers, path = parts
    if not path.startswith("/"):
        path = "/" + path
    return hier, controllers, path


def _legacy_directories(root: str, text: str) -> List[str]:
    base = "" if root == "/" else root
    directories = []
    for line in text.splitlines():
        parsed = _cgroup_line(line)
        if parsed is None:
            continue
        hier, controllers, path = parsed
        rel = "" if path == "/" else path
        if hier == "0" and controllers == "":
            directories.append((base + rel) or "/")
        elif "cpuset" in controllers.split(","):
            directories.append(base + "/cpuset" + rel)
            directories.append((base + rel) or "/")
    return directories


def _cgroup_cpus_legacy(root: str, text: str) -> Optional[frozenset]:
    for start in _legacy_directories(root, text):
        found = _walk_cpuset(start, root)
        if found is not None:
            return found
    return None


def _cgroup_cpus_mounted(root: str, text: str, mount_text: str) -> Optional[frozenset]:
    mounts = _cgroup_mounts(mount_text)
    dotdot = False
    for line in text.splitlines():
        parsed = _cgroup_line(line)
        if parsed is None:
            continue
        hier, controllers, path = parsed
        if hier == "0" and controllers == "":
            ranked = _ranked_mounts(mounts, True, path)
        elif "cpuset" in controllers.split(","):
            ranked = _ranked_mounts(mounts, False, path)
        else:
            continue
        usable = [
            item
            for item in ranked
            if _abs_mount_path(item[0].root) and _abs_mount_path(item[0].point)
        ]
        if ranked and not usable:
            dotdot = True
            continue
        for mount, rel in usable:
            found = _walk_cpuset(_join_mount(mount.point, rel), _trim_mount(mount.point))
            if found is not None:
                return found
    if dotdot:
        return _cgroup_cpus_legacy(root, text)
    return None


def cgroup_cpus_from(
    proc_cgroup_path: str = "/proc/self/cgroup",
    cgroup_root: str = "/sys/fs/cgroup",
) -> Optional[frozenset]:
    """Processors in this process's cgroup cpuset, walking up to an effective list.

    A cgroup v1 line names the cpuset controller. The directory is that
    controller's mount in the sibling ``mountinfo`` plus the cgroup path.
    A cgroup v2 line uses the cgroup2 mount. The walk stops at the mount.
    A mount root containing ``..`` keeps the historical ``cgroup_root`` paths.
    """
    text = _read_text(proc_cgroup_path)
    if not text:
        return None
    root = cgroup_root.rstrip("/") or "/"
    mount_path = os.path.join(os.path.dirname(proc_cgroup_path) or ".", "mountinfo")
    mount_text = _read_text(mount_path, 1 << 20)
    if mount_text is None:
        return _cgroup_cpus_legacy(root, text)
    return _cgroup_cpus_mounted(root, text, mount_text)


def cgroup_cpus() -> Optional[frozenset]:
    """Processors in this process's cgroup cpuset, walking up to an effective list."""
    return cgroup_cpus_from()


def sched_cpus() -> Optional[frozenset]:
    """Processors in this process's affinity mask, below ``_CPU_ID_MAX``."""
    try:
        found = os.sched_getaffinity(0)
    except (AttributeError, OSError):
        return None
    return frozenset(cpu for cpu in found if cpu < _CPU_ID_MAX)


def bind_cpus(env: Mapping[str, str]) -> Optional[frozenset]:
    """``SLURM_CPU_BIND_LIST``, or the mask at the end of ``SLURM_CPU_BIND``."""
    listed = (env.get("SLURM_CPU_BIND_LIST") or "").strip()
    if listed:
        return parse_cpu_spec(listed)
    verbose = (env.get("SLURM_CPU_BIND") or "").strip()
    if ":" not in verbose:
        return None
    return parse_cpu_spec(verbose.rsplit(":", 1)[-1].strip())


def _task_cpu_bounds(
    env: Mapping[str, str],
    sched: Optional[frozenset],
    cgroup: Optional[frozenset],
) -> Tuple[Optional[frozenset], Optional[frozenset]]:
    bind = bind_cpus(env)
    base_cpus: Optional[frozenset] = cgroup if cgroup is not None else sched
    parts = [item for item in (sched, cgroup, bind) if item is not None]
    if not parts:
        return None, None
    allowed_set = set(parts[0])
    for item in parts[1:]:
        allowed_set &= item
    return frozenset(allowed_set), base_cpus


def task_cpu_ids(env: Mapping[str, str]) -> Optional[frozenset]:
    """Intersection of affinity, cpuset, and the Slurm bind list.

    None when none of the three can be read. An empty set means the three
    disagree: the task is not allowed to run on any processor they share.
    """
    allowed, _base = _task_cpu_bounds(env, sched_cpus(), cgroup_cpus())
    return allowed


def _cpuinfo_field(line: str) -> Optional[Tuple[str, str, str]]:
    stripped = line.strip()
    if ":" not in stripped:
        return None
    key, value = stripped.split(":", 1)
    norm = "_".join(key.split()).lower()
    return norm, key.strip(), value.strip()


def _block_logical_ids(
    blocks: Sequence[dict],
    base_cpus: Optional[frozenset],
) -> List[Optional[int]]:
    raw_ids = [block["id"] for block in blocks]
    if (
        base_cpus is not None
        and len(blocks) == len(base_cpus)
        and raw_ids == list(range(len(blocks)))
        and set(raw_ids) != base_cpus
    ):
        return sorted(base_cpus)
    return raw_ids


def scope_cpuinfo(
    text: str,
    allowed: Optional[frozenset],
    base_cpus: Optional[frozenset] = None,
) -> Optional[str]:
    """Cpuinfo limited to ``allowed``, or None when ``text`` already is.

    No ``processor`` lines means the file is not a per-CPU cpuinfo (an
    ``lscpu`` snapshot or a test stub's ``eessi_arch_matches`` line) and is
    left alone. Kept processors that do not share a vendor, model, and flag
    set are collapsed to one record whose flags are the intersection, so a
    consumer that reads only the last record cannot see a richer CPU than
    every processor the task may run. When LXCFS renumbers sequential
    ``0..K-1`` blocks for a constrained cgroup and affinity set
    ``base_cpus``, those blocks are mapped by ordinal position to the
    sorted logical IDs in ``base_cpus``.
    """
    if allowed is None:
        return None
    blocks: List[dict] = []
    current: Optional[dict] = None
    for line in text.splitlines():
        match = _PROCESSOR_LINE_RE.match(line.strip())
        if match:
            if current is not None:
                blocks.append(current)
            current = {"id": _decimal_value(match.group(1)), "lines": [line]}
        elif current is not None:
            current["lines"].append(line)
    if current is not None:
        blocks.append(current)
    if not blocks:
        return None
    logical_ids = _block_logical_ids(blocks, base_cpus)
    kept = [block for block, lid in zip(blocks, logical_ids) if lid in allowed]

    def fields_of(block: dict) -> List[Tuple[str, str, str]]:
        found = []
        for line in block["lines"]:
            field = _cpuinfo_field(line)
            if field is not None and field[0] != "processor":
                found.append(field)
        return found

    def value_map(fields: Sequence[Tuple[str, str, str]]) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for norm, _raw, value in fields:
            out.setdefault(norm, value)
        return out

    parsed = [fields_of(block) for block in kept]
    if kept and len(kept) == len(blocks):
        maps = [value_map(item) for item in parsed]
        if all(item == maps[0] for item in maps[1:]):
            return None
    if not kept:
        return "processor : 0\nflags :\n"
    first = parsed[0]
    raw_of: Dict[str, str] = {}
    for norm, raw, _value in first:
        raw_of.setdefault(norm, raw)
    maps = [value_map(item) for item in parsed]

    def unanimous(norm: str) -> Optional[str]:
        values = []
        for item in maps:
            if norm not in item:
                return None
            values.append(item[norm])
        if any(item != values[0] for item in values[1:]):
            return None
        return values[0]

    def intersection(norm: str) -> str:
        common: Optional[set] = None
        for item in maps:
            tokens = set(item.get(norm, "").split())
            common = tokens if common is None else common & tokens
        if not common:
            return ""
        return " ".join(sorted(common))

    lines = ["processor : %s" % kept[0]["id"]]
    seen = set()
    for norm, _raw, _value in first:
        if norm in seen:
            continue
        seen.add(norm)
        if norm in _FLAG_KEYS:
            lines.append("%s : %s" % (raw_of[norm], intersection(norm)))
            continue
        value = unanimous(norm)
        if value is None:
            continue
        lines.append("%s : %s" % (raw_of[norm], value))
    return "\n".join(lines) + "\n"


def _prepare_cpuinfo(env: Mapping[str, str]) -> Tuple[str, Optional[str], Mapping[str, str]]:
    """Path archdetect should read, a temp file to remove, and the child environment.

    ``EESSI_PROC_CPUINFO`` set by the caller is still scoped to the task's
    processors. A file with no ``processor`` lines is used as it stands.
    """
    explicit = env.get("EESSI_PROC_CPUINFO") or ""
    path = explicit if explicit else "/proc/cpuinfo"
    allowed, base_cpus = _task_cpu_bounds(env, sched_cpus(), cgroup_cpus())
    if allowed is None:
        return path, None, env
    try:
        with open(path, "rb") as handle:
            blob = handle.read(_CPUINFO_READ_MAX + 1)
    except OSError:
        return path, None, env
    text = blob[:_CPUINFO_READ_MAX].decode("utf-8", "replace")
    scoped = scope_cpuinfo(text, allowed, base_cpus=base_cpus)
    if scoped is None:
        return path, None, env
    fd, tmp = tempfile.mkstemp(prefix="eessi-arch-guard-cpu-")
    try:
        os.write(fd, scoped.encode("utf-8"))
    finally:
        os.close(fd)
    child = dict(env)
    child["EESSI_PROC_CPUINFO"] = tmp
    return tmp, tmp, child


def march_of(candidate: str) -> str:
    """The compiler target of a CPU subdirectory: its last segment, ``-`` as ``_``."""
    leaf = candidate.rsplit("/", 1)[-1]
    return leaf.replace("-", "_")


def candidate_supports_cpu(candidate: str, flags: frozenset) -> bool:
    """False when this march implies waitpkg and the CPU flags omit it."""
    if march_of(candidate) in WAITPKG_MARCHES:
        return "waitpkg" in flags
    return True


def probe_cpu(env: Mapping[str, str]) -> CpuProbe:
    """Select the first installed cpupath candidate that satisfies the waitpkg rule."""
    prefix = env.get("EESSI_PREFIX", "")
    os_type = env.get("EESSI_OS_TYPE", "")
    init = env.get("EESSI_INIT_PREFIX", "")
    if (
        not safe_absolute(prefix)
        or not safe_rel_subdir(os_type)
        or not safe_absolute(init)
    ):
        return CpuProbe(
            "error",
            "",
            (),
            "EESSI_PREFIX, EESSI_OS_TYPE, or EESSI_INIT_PREFIX is missing or unsafe",
        )
    script = os.path.join(init, "eessi_archdetect.sh")
    if not os.path.isfile(script):
        return CpuProbe("error", "", (), "eessi_archdetect.sh is not available")
    cpuinfo_path, cleanup, child = _prepare_cpuinfo(env)
    try:
        try:
            proc = _run_bash([script, "-a", "cpupath"], child)
        except OSError as exc:
            return CpuProbe("error", "", (), "could not run archdetect: %s" % exc)
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").splitlines()
            detail = err[0] if err else "exit %s" % proc.returncode
            return CpuProbe("error", "", (), one_line(detail, 200))
        stdout = proc.stdout.decode("utf-8", "replace")
        parsed = parse_cpu_line(stdout)
        if parsed is None:
            return CpuProbe("error", "", (), "cpupath output is not a single line")
        candidates = [item for item in parsed if item]
        if not candidates:
            return CpuProbe("error", "", (), "cpupath returned no candidates")
        for candidate in candidates:
            if not safe_rel_subdir(candidate):
                return CpuProbe(
                    "error", "", tuple(candidates), "cpupath returned an unsafe candidate"
                )
        flags = read_cpu_flags(cpuinfo_path)
    finally:
        if cleanup:
            try:
                os.unlink(cleanup)
            except OSError:
                pass
    compatible = [item for item in candidates if candidate_supports_cpu(item, flags)]

    def installed(items: Sequence[str]) -> str:
        for item in items:
            directory = os.path.join(prefix, "software", os_type, item, "software")
            if os.path.isdir(directory):
                return item
        return ""

    chosen = installed(compatible)
    # archdetect walks the unfiltered list. When that directory needs a
    # missing flag, pin the fallback so the reloaded module does not
    # select the rejected one again.
    raw = installed(candidates)
    override = chosen if chosen and chosen != raw else ""
    if chosen:
        return CpuProbe("ok", chosen, tuple(compatible), "", override)
    return CpuProbe("none", "", tuple(compatible), "")


def probe_accel(env: Mapping[str, str]) -> AccelProbe:
    """Return accelpath stdout, or status ``none`` when the script reports no GPU."""
    init = env.get("EESSI_INIT_PREFIX", "")
    if not safe_absolute(init):
        return AccelProbe("error", "", "EESSI_INIT_PREFIX is missing or unsafe")
    script = os.path.join(init, "eessi_archdetect.sh")
    if not os.path.isfile(script):
        return AccelProbe("error", "", "eessi_archdetect.sh is not available")
    try:
        proc = _run_bash([script, "accelpath"], env)
    except OSError as exc:
        return AccelProbe("error", "", "could not run archdetect: %s" % exc)
    # Exit 2 (no nvidia-smi) and exit 3 (nvidia-smi failed) are the script's
    # no-GPU paths. The EESSI wrapper stores that as an empty subdirectory.
    if proc.returncode != 0:
        return AccelProbe("none", "", "")
    text = proc.stdout.decode("utf-8", "replace").strip()
    if text == "":
        return AccelProbe("none", "", "")
    if "\n" in text or "\r" in text or ACCEL_RE.fullmatch(text) is None:
        return AccelProbe("error", "", "accelpath output is not an accelerator subdirectory")
    return AccelProbe("ok", text, "")


def lmod_invocable(path: str) -> bool:
    """Whether LMOD_CMD can be run as given.

    Not empty, not an option, no line break or NUL, and a regular file this
    user may execute.
    """
    if not path or path.startswith("-") or any(c in path for c in "\n\r\0"):
        return False
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _write_private(path: str, data: bytes) -> None:
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def _parse_env0(blob: bytes) -> Dict[str, str]:
    parsed: Dict[str, str] = {}
    for part in blob.split(b"\0"):
        if b"=" not in part:
            continue
        key, value = part.split(b"=", 1)
        parsed[key.decode("utf-8", "surrogateescape")] = value.decode(
            "utf-8", "surrogateescape"
        )
    return parsed


def _private_directory(path: str) -> Optional[str]:
    """Return an existing private directory owned by the invoking user."""
    if not safe_absolute(path):
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            return None
        return os.path.realpath(path)
    finally:
        os.close(fd)


def restore_work_base(env: Mapping[str, str], strict: Optional[bool] = None) -> str:
    """Select the configured workspace without creating or widening its base."""
    is_strict = _STRICT_MODE if strict is None else strict
    explicit = env.get("EESSI_ARCH_GUARD_WORKDIR", "")
    if explicit:
        selected = _private_directory(explicit)
        if selected is None:
            raise ValueError("EESSI_ARCH_GUARD_WORKDIR is not an existing private owned directory")
        return selected
    temporary = env.get("TMPDIR", "")
    selected = _private_directory(temporary)
    if selected is not None:
        return selected
    if is_strict:
        state = env.get("XDG_STATE_HOME", "")
        if not state:
            state = os.path.join(env.get("HOME", ""), ".local", "state")
        selected = _private_directory(os.path.join(state, "eessi-arch-guard"))
        if selected is not None:
            return selected
        raise ValueError("strict mode needs an existing private job or state workspace")
    if safe_absolute(temporary) and os.path.isdir(temporary):
        return temporary
    return tempfile.gettempdir()


def source_exports(
    module_env: Mapping[str, str], script: bytes
) -> Tuple[Optional[Dict[str, str]], str]:
    """Run the shell code Lmod printed and return the environment it leaves.

    The code runs in bash from ``module_env``, without STARTUP_VARS, from a
    private directory under the restore workspace. Returns None and the
    error when it fails.
    """
    base = restore_work_base(os.environ)
    tmp = tempfile.mkdtemp(prefix="eessi-arch-guard-", dir=base)
    try:
        os.chmod(tmp, 0o700)
        fragment = os.path.join(tmp, "fragment.sh")
        apply_sh = os.path.join(tmp, "apply.sh")
        _write_private(fragment, script)
        _write_private(apply_sh, APPLY_SH.encode("ascii"))
        os.chmod(apply_sh, 0o700)
        proc = subprocess.run(
            ["bash", "--noprofile", "--norc", "--", apply_sh, fragment],
            capture_output=True,
            env=strip_startup(module_env),
            check=False,
        )
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace")
            return None, err or "apply exited %s" % proc.returncode
        return _parse_env0(proc.stdout), ""
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_lmod(
    module_env: Mapping[str, str], lmod_cmd: str, args: Sequence[str]
) -> subprocess.CompletedProcess:
    """Run ``$LMOD_CMD bash ARGS`` from ``module_env`` without STARTUP_VARS.

    Its stdout is the shell code that makes the change.
    """
    return subprocess.run(
        [lmod_cmd, "bash", *args],
        capture_output=True,
        env=strip_startup(module_env),
        check=False,
    )


def apply_lmod(
    module_env: Mapping[str, str], lmod_cmd: str, args: Sequence[str]
) -> Tuple[Optional[Dict[str, str]], str]:
    """The environment after one Lmod command, such as ``load R/4.4.2``.

    Returns None and Lmod's stderr when the command fails, or None and the
    error when its shell code does.
    """
    proc = run_lmod(module_env, lmod_cmd, args)
    if proc.returncode != 0:
        return None, proc.stderr.decode("utf-8", "replace")
    return source_exports(module_env, proc.stdout)


def pinned_env(env: Mapping[str, str], pin: str) -> Dict[str, str]:
    """The environment for loading EESSI pinned to one CPU subdirectory.

    A login-node EESSI_ARCHDETECT_OPTIONS would outrank the pin inside the
    EESSI module, so it is dropped; the module's archdetect wrapper then
    exports the pinned subdirectory. The purge before this load needs that
    variable: the module's unload computes the tree to remove from it.
    """
    pinned = dict(env)
    if pin:
        pinned.pop("EESSI_ARCHDETECT_OPTIONS", None)
        pinned["EESSI_SOFTWARE_SUBDIR_OVERRIDE"] = pin
    return pinned


def _software_root(env: Mapping[str, str]) -> str:
    """Known software root for this environment, or empty when unavailable."""
    prefix = env.get("EESSI_PREFIX", "")
    os_type = env.get("EESSI_OS_TYPE", "")
    if not safe_absolute(prefix) or not safe_rel_subdir(os_type):
        return ""
    root = os.path.join(prefix, "software", os_type)
    if not os.path.isdir(root):
        return ""
    return root


def _invalid_guard_environment(env: Mapping[str, str]) -> Optional[str]:
    """Invalid env inputs that must fail loudly instead of being ignored."""
    override = env.get("EESSI_SOFTWARE_SUBDIR_OVERRIDE", "")
    if override:
        if override.strip() != override or override.endswith("/"):
            return "EESSI_SOFTWARE_SUBDIR_OVERRIDE is not a safe subdirectory: %s" % override
        if safe_rel_subdir(override):
            root = _software_root(env)
            if root and not os.path.isdir(os.path.join(root, override, "software")):
                return "EESSI_SOFTWARE_SUBDIR_OVERRIDE is not an installed target: %s" % override
    accel = env.get("EESSI_ACCEL_SUBDIR", "")
    if accel:
        if accel.strip() != accel or ACCEL_RE.fullmatch(accel) is None:
            return "EESSI_ACCEL_SUBDIR is not a valid accelerator subdirectory: %s" % accel
    return None


def _load_in_order(
    current: Dict[str, str], lmod_cmd: str, modules: Sequence[str]
) -> Tuple[Dict[str, str], List[str], str]:
    """Load the modules one Lmod call each, in order, past any that fail.

    One call finds every name before it loads any, so a module from a tree
    an earlier module adds (EESSI-extend and the user's installations) loads
    only in a later call. Stops at DIAGNOSE_LIMIT failures; the error is the
    first failure's.
    """
    missing: List[str] = []
    first_err = ""
    for name in modules:
        nxt, err = apply_lmod(current, lmod_cmd, ["load", name])
        if nxt is None:
            missing.append(name)
            first_err = first_err or err
            if len(missing) == DIAGNOSE_LIMIT:
                break
            continue
        current = nxt
    return current, missing, first_err


def to_wire(value: str) -> str:
    """One character per byte this process would pass to an exec."""
    return os.fsencode(value).decode("latin-1")


def from_wire(value: str) -> str:
    """Inverse of to_wire in this process."""
    return os.fsdecode(value.encode("latin-1"))


def _wire_map(mapping: Mapping[str, str]) -> Dict[str, str]:
    return {to_wire(str(key)): to_wire(str(value)) for key, value in mapping.items()}


def do_restore(payload: Mapping[str, object]) -> Dict[str, object]:
    """Purge and reload from a JSON request. The return value is the JSON object."""
    env = payload.get("env")
    eessi = payload.get("eessi")
    modules = payload.get("modules")
    lmod_cmd = payload.get("lmod")
    pin = payload.get("pin", "")
    if not isinstance(env, dict) or not isinstance(eessi, str) or not isinstance(modules, list):
        return {"ok": False, "missing": [], "error": "restore request is malformed"}
    if not isinstance(lmod_cmd, str) or not isinstance(pin, str):
        return {"ok": False, "missing": [], "error": "restore request is malformed"}
    if not all(isinstance(value, str) for value in env.values()):
        return {"ok": False, "missing": [], "error": "restore request is malformed"}
    try:
        eessi = from_wire(eessi)
        lmod_cmd = from_wire(lmod_cmd)
        pin = from_wire(pin)
        modules = [from_wire(name) if isinstance(name, str) else name for name in modules]
        module_env = {from_wire(key): from_wire(value) for key, value in env.items()}
    except UnicodeError:
        return {"ok": False, "missing": [], "error": "restore request is malformed"}
    if pin and not safe_rel_subdir(pin):
        return {"ok": False, "missing": [], "error": "restore request is malformed"}
    if not lmod_invocable(lmod_cmd):
        return {"ok": False, "missing": [], "error": "LMOD_CMD is not executable"}
    if not module_name_ok(eessi) or EESSI_MODULE_RE.fullmatch(eessi) is None:
        return {"ok": False, "missing": [], "error": "unsafe-module-name"}
    if any(not isinstance(name, str) or not module_name_ok(name) for name in modules):
        return {"ok": False, "missing": [], "error": "unsafe-module-name"}
    current, err = apply_lmod(module_env, lmod_cmd, ["--force", "purge"])
    if current is None:
        return {"ok": False, "missing": [], "error": one_line(err or "purge failed", 300)}
    had_cpuinfo = "EESSI_PROC_CPUINFO" in current
    orig_cpuinfo = current.get("EESSI_PROC_CPUINFO")
    _cpuinfo_path, cleanup, load_env = _prepare_cpuinfo(pinned_env(current, pin))
    try:
        current, err = apply_lmod(load_env, lmod_cmd, ["load", eessi])
    finally:
        if cleanup:
            try:
                os.unlink(cleanup)
            except OSError:
                pass
    if current is None:
        return {
            "ok": False,
            "missing": [eessi],
            "error": one_line(err or "EESSI load failed", 300),
        }
    if had_cpuinfo and orig_cpuinfo is not None:
        current["EESSI_PROC_CPUINFO"] = orig_cpuinfo
    else:
        current.pop("EESSI_PROC_CPUINFO", None)
    if modules:
        nxt, err = apply_lmod(current, lmod_cmd, ["load", *modules])
        if nxt is None:
            nxt, missing, err = _load_in_order(current, lmod_cmd, modules)
            if missing:
                return {
                    "ok": False,
                    "missing": missing,
                    "error": one_line(err or "module load failed", 300),
                }
        current = nxt
    return {"ok": True, "env": _wire_map(current), "missing": [], "error": ""}


def _not_json(name: str) -> float:
    # json.loads reads NaN, Infinity and -Infinity, which JSON does not have.
    raise ValueError("%s is not JSON" % name)


def restore_main() -> int:
    """The ``--restore`` helper: a JSON request on stdin, the JSON result on stdout.

    Returns 0; on an error it writes the reason to stderr and returns 1.
    """
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"), parse_constant=_not_json)
        sys.stdout.write(json.dumps(do_restore(payload)))
        return 0
    except Exception as exc:
        sys.stderr.write("eessi-arch-guard: restore failed: %s\n" % one_line(str(exc), 300))
        return 1


def call_restore(
    env: Mapping[str, str], eessi: str, modules: Sequence[str], pin: str = ""
) -> RestoreResult:
    """Reload ``eessi`` and ``modules`` through the restore helper.

    The helper is this file again with ``--restore``, run in a new private
    workspace with a clean environment. ``pin`` is the CPU subdirectory the
    loads after the purge are pinned to, empty for none.
    """
    payload = {
        "v": 1,
        "env": _wire_map(env),
        "eessi": to_wire(eessi),
        "modules": [to_wire(name) for name in modules],
        "lmod": to_wire(env.get("LMOD_CMD", "")),
        "pin": to_wire(pin),
    }
    try:
        work = tempfile.mkdtemp(prefix="eessi-arch-guard-", dir=restore_work_base(env))
    except (OSError, ValueError) as exc:
        return _not_run("restore workspace unavailable: %s" % exc)
    clean = {
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C",
        "LANG": "C",
        "HOME": work,
        "TMPDIR": work,
        "EESSI_ARCH_GUARD_WORKDIR": work,
    }
    helper_args = [sys.executable, "-I", "-X", "utf8", os.path.abspath(__file__), "--restore"]
    if _STRICT_MODE:
        helper_args.append("--strict")
    try:
        try:
            proc = run_captured(
                helper_args,
                clean,
                input_bytes=json.dumps(payload).encode("utf-8"),
                new_session=_ACTIVE is not None,
            )
        except OSError as exc:
            return _not_run("could not start restore helper: %s" % exc)
        if proc.returncode != 0 or not proc.stdout:
            err = proc.stderr.decode("utf-8", "replace")
            return _not_run(one_line(err or "restore helper failed", 300))
        try:
            data = json.loads(proc.stdout.decode("utf-8"))
        except json.JSONDecodeError:
            return _not_run("restore helper returned invalid output")
        missing_raw = data.get("missing") or []
        missing = tuple(
            name for name in missing_raw if isinstance(name, str) and module_name_ok(name)
        )
        if not data.get("ok"):
            return RestoreResult(False, None, missing, one_line(str(data.get("error") or ""), 300))
        restored = data.get("env")
        if not isinstance(restored, dict):
            return _not_run("restore helper returned no environment")
        try:
            cleaned = {
                from_wire(str(key)): from_wire(str(value)) for key, value in restored.items()
            }
        except UnicodeError:
            return _not_run("restore helper returned no environment")
        return RestoreResult(True, cleaned, missing, "")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _not_run(error: str) -> RestoreResult:
    return RestoreResult(False, None, (), error, ran=False)


def _side(text: str) -> str:
    if not text or any(char in text for char in "\n\r\0"):
        return "unrecognized"
    return text


def _failed(was: str, now: str, message: str, detail: Optional[str] = None) -> Outcome:
    token = "%s->%s" % (_side(was), _side(now))
    return Outcome(
        {"EESSI_ARCH_GUARD_FAILED": token},
        one_line(message),
        "failed",
        one_line(detail) if detail else None,
    )


def _restore_mismatch(
    new: Mapping[str, str],
    now: str,
    now_accel: str,
    compare_accel: bool,
    eessi: str,
    others: Sequence[str],
) -> str:
    if new.get("EESSI_SOFTWARE_SUBDIR", "") != now:
        return "reload did not select this node's CPU target"
    if compare_accel and new.get("EESSI_ACCEL_SUBDIR", "") != now_accel:
        return "reload did not select this node's accelerator target"
    loaded = [part for part in new.get("LOADEDMODULES", "").split(":") if part]
    if eessi not in loaded:
        return "reload did not load the EESSI module"
    for name in others:
        if name not in loaded:
            return "reload did not load %s" % name
    return ""


def decide(
    env: Mapping[str, str],
    shell: bool,
    cpu_probe=probe_cpu,
    accel_probe=probe_accel,
    restore=call_restore,
) -> Outcome:
    """Compare the loaded target with this node and build the environment delta."""
    if _ACTIVE is not None:
        _ACTIVE.check()
    if env.get("EESSI_ARCH_GUARD") == "0":
        return Outcome({}, None, "skip", "EESSI_ARCH_GUARD=0")
    was = env.get("EESSI_SOFTWARE_SUBDIR", "")
    software_override = env.get("EESSI_SOFTWARE_SUBDIR_OVERRIDE", "")
    if not was:
        return Outcome({}, None, "skip", "EESSI is not loaded")
    invalid = _invalid_guard_environment(env)
    if invalid:
        return _failed(
            was,
            "invalid-environment",
            "eessi-arch-guard: %s; environment left as it was" % invalid,
        )
    if software_override and safe_rel_subdir(software_override):
        return Outcome({}, None, "skip", "EESSI_SOFTWARE_SUBDIR_OVERRIDE is set")
    if env.get("EESSI_ARCHDETECT_OPTIONS_OVERRIDE"):
        return Outcome({}, None, "skip", "EESSI_ARCHDETECT_OPTIONS_OVERRIDE is set")

    cpu = cpu_probe(env)
    if cpu.status == "error":
        return _failed(
            was,
            "archdetect-failed",
            "eessi-arch-guard: archdetect failed (%s); environment left as it was" % cpu.error,
        )
    if cpu.status == "none":
        if env.get("EESSI_ARCH_GUARD_MISSING") == "ignore":
            return Outcome({}, None, "skip", "no EESSI software directory (ignored)")
        shown = " ".join(cpu.candidates) or "(none)"
        return _failed(
            was,
            "no-software-dir",
            "eessi-arch-guard: no EESSI software directory for this node "
            "under %s (candidates %s); modules were loaded for %s; "
            "environment left as it was" % (env.get("EESSI_PREFIX", ""), shown, was),
        )

    compare_accel = not env.get("EESSI_ACCELERATOR_TARGET_OVERRIDE")
    was_accel = env.get("EESSI_ACCEL_SUBDIR", "")
    if compare_accel:
        accel = accel_probe(env)
        if accel.status == "error":
            return _failed(
                was,
                "archdetect-failed",
                "eessi-arch-guard: archdetect failed (%s); environment left as it was"
                % accel.error,
            )
        now_accel = accel.subdir
    else:
        now_accel = was_accel

    now = cpu.target
    if now == was and now_accel == was_accel:
        return Outcome({}, None, "noop", "target already matches this node")

    if compare_accel and was_accel != now_accel:
        was_label = "%s %s" % (was, was_accel or "no accel")
        now_label = "%s %s" % (now, now_accel or "no accel")
    else:
        was_label, now_label = was, now

    loaded = parse_loaded_modules(env)
    if loaded.error:
        why = (
            "a loaded module name is not safe to pass to Lmod"
            if loaded.error == "unsafe-module-name"
            else "the loaded module list has no single EESSI/<version> entry"
        )
        return _failed(
            was_label,
            "unsafe-module-name" if loaded.error == "unsafe-module-name" else "restore-failed",
            "eessi-arch-guard: %s; environment left as it was" % why,
        )
    if not lmod_invocable(env.get("LMOD_CMD", "")):
        return _failed(
            was_label,
            "lmod-unavailable",
            "eessi-arch-guard: LMOD_CMD is missing or not executable; "
            "this node is %s, the modules were loaded for %s; environment left as it was"
            % (now_label, was_label),
        )

    restore_env = env
    if software_override and not safe_rel_subdir(software_override):
        restore_env = dict(env)
        restore_env.pop("EESSI_SOFTWARE_SUBDIR_OVERRIDE", None)
    try:
        result = restore(restore_env, loaded.eessi, loaded.others, cpu.override)
    except Exception as exc:
        result = _not_run(str(exc))
    if not result.ran:
        return _failed(
            was_label,
            "restore-failed",
            "eessi-arch-guard: restore failed (%s); environment left as it was"
            % one_line(result.error, 200),
        )
    if not result.ok or result.env is None:
        if result.missing:
            names = " ".join(result.missing)
            if len(result.missing) >= DIAGNOSE_LIMIT and len(loaded.others) > len(result.missing):
                names += " (stopped after %s)" % DIAGNOSE_LIMIT
            why = "not available for this target: " + names
        else:
            why = "module load failed"
        return _failed(
            was_label,
            now_label,
            "eessi-arch-guard: this node is %s, the modules were loaded for %s; "
            "%s; environment left as it was" % (now_label, was_label, why),
            result.error,
        )

    mismatch = _restore_mismatch(
        result.env, now, now_accel, compare_accel, loaded.eessi, loaded.others
    )
    if mismatch:
        return _failed(
            was_label,
            "restore-failed",
            "eessi-arch-guard: %s; environment left as it was" % mismatch,
        )

    delta = environment_delta(env, result.env, multiline=shell)
    if delta is None:
        return _failed(
            was_label,
            "unsafe-environment",
            "eessi-arch-guard: reload produced a value this output mode cannot carry; "
            "environment left as it was",
        )
    delta["EESSI_ARCH_GUARD_RELOADED"] = "%s->%s" % (_side(was_label), _side(now_label))
    # A successful reload must not leave a previous failure flag set.
    if "EESSI_ARCH_GUARD_FAILED" in env and "EESSI_ARCH_GUARD_FAILED" not in delta:
        delta["EESSI_ARCH_GUARD_FAILED"] = None
    message = "eessi-arch-guard: reloaded modules for %s (loaded for %s)" % (
        now_label,
        was_label,
    )
    if not shell:
        try:
            render_bytes(delta, message, False, True)
        except ValueError:
            return _failed(
                was_label,
                "unsafe-environment",
                "eessi-arch-guard: reload produced a prolog line past the "
                "Slurm 17.11 read; environment left as it was",
            )
    return Outcome(delta, message, "reloaded", None)


_GPU_SCOPE_KEYS = (
    "CUDA_VISIBLE_DEVICES",
    "ROCR_VISIBLE_DEVICES",
    "GPU_DEVICE_ORDINAL",
    "SLURM_STEP_GPUS",
    "SLURM_GPUS_ON_NODE",
)


def task_scope_key(env: Mapping[str, str]) -> str:
    """Canonical processors for the cache key. ``-`` is an empty intersection."""
    allowed = task_cpu_ids(env)
    if allowed is None:
        return ""
    if not allowed:
        return "-"
    return ",".join(str(cpu) for cpu in sorted(allowed))


def gpu_scope_key(env: Mapping[str, str]) -> str:
    """Accelerator visibility the cache must not share across tasks."""
    return "\0".join(
        "%s=%s" % (name, env[name]) for name in _GPU_SCOPE_KEYS if name in env
    )


def cache_digest(env: Mapping[str, str]) -> str:
    """The cache key in a cache file's name: 16 hex digits of a SHA-256.

    The hash covers the KEY_FIELDS values, the task's processors and its
    visible GPUs, joined by NUL bytes.
    """
    parts = [env.get(key, "") for key in KEY_FIELDS]
    parts.append(task_scope_key(env))
    parts.append(gpu_scope_key(env))
    inputs = "\0".join(parts)
    return hashlib.sha256(inputs.encode("utf-8", "surrogateescape")).hexdigest()[:16]


def _safe_component(text: str) -> str:
    if _SAFE_COMPONENT_RE.fullmatch(text or ""):
        return text
    return hashlib.sha256((text or "").encode("utf-8", "surrogateescape")).hexdigest()[:16]


def cache_directory(env: Mapping[str, str]) -> Optional[str]:
    """The directory for the decision cache, or None for no cache.

    With ``--strict`` it is the private restore workspace; otherwise TMPDIR
    when it is a safe absolute directory, else ``/tmp``.
    """
    if _STRICT_MODE:
        try:
            return restore_work_base(env)
        except (OSError, ValueError):
            return None
    tmp = env.get("TMPDIR", "")
    if tmp and safe_absolute(tmp) and os.path.isdir(tmp):
        return tmp
    if os.path.isdir("/tmp"):
        return "/tmp"
    return None


def cache_file_path(env: Mapping[str, str], shell: bool = False) -> Optional[str]:
    """Cache path for this uid, node, job, step, and output mode. None when the job id is unset.

    The mode is part of the name because the decision depends on it: a value
    with a newline reloads for --shell and fails in a prolog, and only a
    prolog line has the Slurm 17.11 length cap.
    """
    job = env.get("SLURM_JOB_ID", "")
    if not job:
        return None
    directory = cache_directory(env)
    if directory is None:
        return None
    step = env.get("SLURM_STEP_ID", "batch")
    name = ".eessi-arch-guard.%s.%s.%s.%s.%s.%s" % (
        os.getuid(),
        _safe_component(os.uname().nodename),
        _safe_component(job),
        _safe_component(step),
        "shell" if shell else "prolog",
        cache_digest(env),
    )
    return os.path.join(directory, name)


def _never_cache(name: str) -> bool:
    """Names a cache entry may not carry: the never-touch list and the loader's LD_*."""
    return _never_touch(name) or name.startswith("LD_")


def _owned_private_file(fd: int) -> bool:
    """True when fd is a regular file of this uid with one link and no group or other bits.

    The cache and its lock sit in a directory other users can write, such as
    /tmp; a file someone else created at the name is not ours to trust.
    """
    try:
        st = os.fstat(fd)
    except OSError:
        return False
    return (
        stat.S_ISREG(st.st_mode)
        and st.st_uid == os.getuid()
        and st.st_nlink == 1
        and st.st_mode & 0o077 == 0
    )


def _cache_changes_ok(changes: object, shell: bool) -> bool:
    if not isinstance(changes, dict):
        return False
    for key, value in changes.items():
        if not isinstance(key, str) or ENV_NAME_RE.fullmatch(key) is None:
            return False
        if _never_cache(key):
            return False
        if value is None:
            continue
        if not isinstance(value, str) or not value_emit_ok(value, multiline=shell):
            return False
    return True


def _read_cache(directory: str, name: str, shell: bool) -> Optional[Outcome]:
    dirfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        try:
            # O_NONBLOCK: a FIFO planted at the name must not hang the prolog.
            fd = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=dirfd,
            )
        except OSError:
            return None
        try:
            if not _owned_private_file(fd):
                return None
            blob = os.read(fd, 8 * 1024 * 1024)
        finally:
            os.close(fd)
    finally:
        os.close(dirfd)
    try:
        data = json.loads(blob.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("v") != 1:
        return None
    message = data.get("message")
    detail = data.get("detail")
    bad_message = message is not None and (
        not isinstance(message, str) or any(c in message for c in "\n\r\0")
    )
    if bad_message:
        return None
    if detail is not None and not isinstance(detail, str):
        return None
    if not _cache_changes_ok(data.get("changes"), shell):
        return None
    kind = data.get("kind")
    if kind not in ("skip", "noop", "reloaded", "failed"):
        return None
    return Outcome(dict(data["changes"]), message, kind, detail)


def _discard_file(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _write_cache(path: str, outcome: Outcome) -> None:
    directory, name = os.path.split(path)
    payload = {
        "v": 1,
        "kind": outcome.kind,
        "message": outcome.message,
        "detail": outcome.detail,
        "changes": outcome.changes,
    }
    data = json.dumps(payload, ensure_ascii=True).encode("ascii") + b"\n"
    tmp = ".%s.%s.%s.tmp" % (name, os.getpid(), secrets.token_hex(6))
    tmp_path = os.path.join(directory, tmp)
    dirfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    fd = -1
    try:
        try:
            fd = os.open(
                tmp,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=dirfd,
            )
            os.write(fd, data)
            os.fchmod(fd, 0o600)
            os.close(fd)
            fd = -1
            os.replace(tmp_path, path)
        except OSError:
            _discard_file(tmp_path)
            return
        except BaseException:
            _discard_file(tmp_path)
            raise
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(dirfd)


def _lock_exclusive(lockfd: int) -> None:
    limit = _ACTIVE
    if limit is None:
        fcntl.flock(lockfd, fcntl.LOCK_EX)
        return
    while True:
        limit.check()
        try:
            fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            pause = min(LOCK_POLL_SECONDS, max(limit.remaining(), 0.0))
            time.sleep(pause)


_LAST_CACHED = False


def cached_decide(
    env: Mapping[str, str],
    shell: bool,
    cpu_probe=probe_cpu,
    accel_probe=probe_accel,
    restore=call_restore,
) -> Outcome:
    """``decide`` once per job step, node, user, output mode and cache key.

    The first task takes the lock and writes its outcome; the others wait for
    the lock and read it. Without SLURM_JOB_ID, or without a usable cache
    directory or lock file, every task decides on its own.
    """
    global _LAST_CACHED
    _LAST_CACHED = False
    path = cache_file_path(env, shell)
    if path is None or not hasattr(os, "O_NOFOLLOW"):
        return decide(env, shell, cpu_probe, accel_probe, restore)
    directory, name = os.path.split(path)
    try:
        dirfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return decide(env, shell, cpu_probe, accel_probe, restore)
    try:
        try:
            lockfd = os.open(
                name + ".lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                0o600,
                dir_fd=dirfd,
            )
        except OSError:
            return decide(env, shell, cpu_probe, accel_probe, restore)
        try:
            # A lock file someone else made is neither locked on nor chmodded:
            # decide without the cache.
            if not _owned_private_file(lockfd):
                return decide(env, shell, cpu_probe, accel_probe, restore)
            _lock_exclusive(lockfd)
            loaded = _read_cache(directory, name, shell)
            if loaded is not None:
                _LAST_CACHED = True
                return loaded
            if _ACTIVE is not None:
                _ACTIVE.check()
            outcome = decide(env, shell, cpu_probe, accel_probe, restore)
            if _ACTIVE is not None:
                _ACTIVE.committing = True
            try:
                _write_cache(path, outcome)
            finally:
                if _ACTIVE is not None:
                    _ACTIVE.committing = False
            return outcome
        finally:
            os.close(lockfd)
    finally:
        os.close(dirfd)


def _limit_outcome(env: Mapping[str, str], token: str, message: str) -> Outcome:
    return _failed(env.get("EESSI_SOFTWARE_SUBDIR", ""), token, message)


def limited_decide(
    env: Mapping[str, str],
    shell: bool,
    timeout: float = DEFAULT_TIMEOUT,
    cpu_probe=probe_cpu,
    accel_probe=probe_accel,
    restore=call_restore,
) -> Outcome:
    """Decide under one deadline. A timeout or SIGTERM is not written to the cache."""
    global _ACTIVE, _LAST_CACHED
    _LAST_CACHED = False
    if _ACTIVE is not None:
        return cached_decide(env, shell, cpu_probe, accel_probe, restore)
    limit = DecisionLimit(timeout)
    _ACTIVE = limit
    limit.install()
    try:
        try:
            return cached_decide(env, shell, cpu_probe, accel_probe, restore)
        except _Deadline:
            path = cache_file_path(env, shell)
            if path is not None:
                directory, name = os.path.split(path)
                loaded = _read_cache(directory, name, shell)
                if loaded is not None:
                    _LAST_CACHED = True
                    return loaded
            return _limit_outcome(
                env,
                "timeout",
                "eessi-arch-guard: decision exceeded the time limit; "
                "environment left as it was",
            )
        except _Terminated:
            return _limit_outcome(
                env,
                "terminated",
                "eessi-arch-guard: decision was terminated; environment left as it was",
            )
    finally:
        limit.restore()
        _ACTIVE = None


def parse_timeout(text: str) -> Optional[float]:
    """A positive number of seconds at most TIMEOUT_MAX, or None."""
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    if value != value or value <= 0 or value > TIMEOUT_MAX:
        return None
    return value


def resolve_timeout(
    cli_text: Optional[str], settings: Optional[Mapping[str, str]]
) -> Tuple[float, Optional[str]]:
    """CLI overrides the config key, which overrides DEFAULT_TIMEOUT."""
    chosen = DEFAULT_TIMEOUT
    warnings = []
    if settings and "timeout" in settings:
        parsed = parse_timeout(settings["timeout"])
        if parsed is None:
            warnings.append(
                "eessi-arch-guard: config timeout is not a positive number of seconds "
                "at most %s" % TIMEOUT_MAX
            )
        else:
            chosen = parsed
    if cli_text is not None:
        parsed = parse_timeout(cli_text)
        if parsed is None:
            warnings.append(
                "eessi-arch-guard: --timeout is not a positive number of seconds at most %s"
                % TIMEOUT_MAX
            )
        else:
            chosen = parsed
    if not warnings:
        return chosen, None
    return chosen, " ".join(warnings)


def _config_untrusted(path: str) -> Optional[str]:
    try:
        info = os.lstat(path)
    except OSError as exc:
        return "%s (%s)" % (path, exc)
    if stat.S_ISLNK(info.st_mode):
        return "%s is a symlink and is ignored" % path
    if not stat.S_ISREG(info.st_mode):
        return "%s is not a regular file and is ignored" % path
    if info.st_uid != 0:
        return "%s is not owned by root and is ignored" % path
    if info.st_mode & 0o022:
        return "%s is writable by group or others and is ignored" % path
    return None


def parse_config(text: str) -> Dict[str, str]:
    """The settings of a configuration file given as text, as ``parse_config_bytes`` reads them."""
    settings, _bad = parse_config_bytes(text.encode("utf-8"))
    return settings


def parse_config_bytes(blob: bytes) -> Tuple[Dict[str, str], Tuple[str, ...]]:
    """Settings from raw config bytes. A bad value does not drop other keys."""
    settings: Dict[str, str] = {}
    bad: List[str] = []
    for raw in blob.splitlines():
        line = raw.strip()
        if not line or line.startswith(b"#") or b"=" not in line:
            continue
        key_b, value_b = line.split(b"=", 1)
        key_b = key_b.strip()
        value_b = value_b.strip()
        try:
            key = key_b.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if not key:
            continue
        try:
            value = value_b.decode("utf-8")
        except UnicodeDecodeError:
            value = value_b.decode("utf-8", "surrogateescape")
            bad.append(key)
        settings[key] = value
    return settings, tuple(bad)


def config_chain(settings: Optional[Mapping[str, str]]) -> Optional[str]:
    """The ``chain`` setting, or None when it is unset or empty."""
    if not settings:
        return None
    return settings.get("chain") or None


_SUBMIT_SELECTOR_RE = re.compile(r"^[A-Za-z0-9_.+-]{1,64}$")


def _selector_hint(config: Mapping[str, str], prefix: str, value: str, kind: str) -> str:
    keys = [key[len(prefix):] for key in config if key.startswith(prefix)]
    if not keys:
        return ""
    best: Optional[str] = None
    best_score = (-1, -10**9, "")
    for candidate in keys:
        char_prefix = 0
        for left, right in zip(value, candidate):
            if left != right:
                break
            char_prefix += 1
        if char_prefix == 0:
            continue
        length_delta = -abs(len(candidate) - len(value))
        score = (char_prefix, length_delta, candidate)
        if score > best_score:
            best = candidate
            best_score = score
    if best is None:
        return ""
    return "; nearest known %s is %s%s" % (kind, prefix, best)


def submit_target(
    settings: Optional[Mapping[str, str]],
    partition: str,
    constraint: str,
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve a partition and optional constraint through the site configuration."""
    if _SUBMIT_SELECTOR_RE.fullmatch(partition or "") is None:
        return None, "--partition needs one safe partition name"
    if constraint and _SUBMIT_SELECTOR_RE.fullmatch(constraint) is None:
        return None, "--constraint needs one safe constraint name"
    config = settings or {}
    part_key = "submit_target.partition." + partition
    target = config.get(part_key, "")
    if not target:
        hint = _selector_hint(config, "submit_target.partition.", partition, "partition")
        return None, "site config has no %s mapping%s" % (part_key, hint)
    if target.strip() != target:
        return None, "%s has invalid target value %r" % (part_key, target)
    if target != target.lower():
        return None, "%s has invalid target value %r" % (part_key, target)
    if not safe_rel_subdir(target):
        return None, "%s has invalid target value %r" % (part_key, target)
    if constraint:
        constraint_key = "submit_target.constraint." + constraint
        other = config.get(constraint_key, "")
        if not other:
            hint = _selector_hint(
                config, "submit_target.constraint.", constraint, "constraint"
            )
            return None, "site config has no %s mapping%s" % (constraint_key, hint)
        if other.strip() != other:
            return None, "%s has invalid target value %r" % (constraint_key, other)
        if other != other.lower():
            return None, "%s has invalid target value %r" % (constraint_key, other)
        if not safe_rel_subdir(other):
            return None, "%s has invalid target value %r" % (constraint_key, other)
        if other != target:
            return None, "%s and %s select different targets" % (part_key, constraint_key)
    return target, None


def target_modules(root: str) -> Tuple[str, ...]:
    """Module names represented by regular files below one ``modules/all`` tree."""
    found = set()
    if not os.path.isdir(root):
        return ()
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if not name.startswith("."))
        for filename in filenames:
            if filename.startswith(".") or filename in ("default", "modulerc"):
                continue
            path = os.path.join(directory, filename)
            if not os.path.isfile(path):
                continue
            relative = os.path.relpath(path, root).replace(os.sep, "/")
            if filename.endswith(".lua"):
                relative = relative[:-4]
            if module_name_ok(relative):
                found.add(relative)
    return tuple(sorted(found))


def extend_module_roots(env: Mapping[str, str], os_type: str, target: str) -> Tuple[str, ...]:
    """``modules/all`` of the user and project trees EESSI-extend installs into, for ``target``.

    EESSI-extend lays an install prefix out as
    ``PREFIX/versions/EESSI_VERSION/software/OS/TARGET/modules/all``. The prefixes
    are ``EESSI_USER_INSTALL`` and ``EESSI_PROJECT_INSTALL`` when set, and
    ``HOME/eessi``, EESSI-extend's default user prefix, when neither
    ``EESSI_USER_INSTALL`` nor ``EESSI_PROJECT_INSTALL`` is in the environment.
    Unsafe values are skipped.
    """
    version = env.get("EESSI_VERSION", "")
    if not safe_rel_subdir(version):
        return ()
    bases = []
    for name in ("EESSI_USER_INSTALL", "EESSI_PROJECT_INSTALL"):
        value = env.get(name, "")
        if value and safe_absolute(value):
            bases.append(value)
    home = env.get("HOME", "")
    neither = "EESSI_USER_INSTALL" not in env and "EESSI_PROJECT_INSTALL" not in env
    if neither and home and safe_absolute(home):
        bases.append(os.path.join(home, "eessi"))
    roots = []
    for base in bases:
        root = os.path.join(
            base, "versions", version, "software", os_type, target, "modules", "all"
        )
        if root not in roots:
            roots.append(root)
    return tuple(roots)


def _planner_module(module: str) -> str:
    """Lossless EasyBuild-shaped identity for any safe Lmod module name."""
    return "EAGCheck" + os.fsencode(module).hex() + "/1.0"


def _easyconfig_filename(module: str) -> str:
    return _planner_module(module).replace("/", "-") + ".eb"


def _check_submit_loaded(env: Mapping[str, str]) -> Tuple[Optional[Tuple[str, ...]], str]:
    raw = env.get("LOADEDMODULES", "")
    names = tuple(part for part in raw.split(":") if part)
    if not names:
        return None, "LOADEDMODULES is empty"
    if any(not module_name_ok(name) for name in names):
        return None, "LOADEDMODULES contains an unsafe module name"
    seen = set()
    eessi_count = 0
    apps = []
    for name in names:
        if name in seen:
            return None, "LOADEDMODULES has duplicate module entry: %s" % name
        seen.add(name)
        if name.startswith("EESSI/"):
            if EESSI_MODULE_RE.fullmatch(name) is None or eessi_count > 0:
                return None, "LOADEDMODULES has an invalid EESSI module entry"
            eessi_count += 1
        else:
            apps.append(name)
    return tuple(apps), ""


def _check_submit_semantic(code: int) -> str:
    if code == 0:
        return "ok"
    if code == 1:
        return "missing"
    return "invalid"


def _finish_check_submit(
    as_json: bool,
    code: int,
    partition: str,
    constraint: str,
    target: Optional[str],
    loaded: Optional[int],
    missing: Sequence[str],
    message: Optional[str],
    stderr_lines: Sequence[str],
) -> int:
    """Write the check result. ``--json`` replaces the text summary on stdout."""
    for line in stderr_lines:
        sys.stderr.write(line)
    if as_json:
        payload = {
            "schema_version": DECISION_JSON_SCHEMA_VERSION,
            "kind": "check-submit",
            "partition": partition or None,
            "constraint": constraint or None,
            "target": target,
            "loaded": loaded,
            "missing": list(missing),
            "message": message,
            "exit": code,
            "exit_semantic": _check_submit_semantic(code),
        }
        sys.stdout.write(
            json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n"
        )
    elif code == 0 and message:
        sys.stdout.write("eessi-arch-guard: check-submit: %s\n" % message)
    return code


def _direct_target(
    target: str, partition: Optional[str], constraint: Optional[str]
) -> Tuple[Optional[str], Optional[str]]:
    """The target ``--target`` names, refused next to ``--partition`` or ``--constraint``."""
    if partition or constraint:
        return None, "--target is used without --partition and --constraint"
    if not safe_rel_subdir(target):
        return None, "--target needs one safe relative EESSI target, such as x86_64/amd/zen4"
    return target, None


def check_submit(
    env: Mapping[str, str],
    settings: Optional[Mapping[str, str]],
    partition: str,
    constraint: str,
    eb_stack_arg: str,
    as_json: bool = False,
    target_arg: Optional[str] = None,
) -> int:
    """Check loaded modules against a mapped target through ``eb-stack stack arch``.

    ``target_arg`` names the target directly, in place of a partition the site
    config maps; it excludes ``partition`` and ``constraint``.
    """

    def finish(
        code: int,
        target: Optional[str] = None,
        loaded: Optional[int] = None,
        missing: Sequence[str] = (),
        message: Optional[str] = None,
        stderr_lines: Sequence[str] = (),
    ) -> int:
        return _finish_check_submit(
            as_json,
            code,
            partition,
            constraint,
            target,
            loaded,
            missing,
            message,
            stderr_lines,
        )

    if target_arg is not None:
        target, error = _direct_target(target_arg, partition, constraint)
    else:
        target, error = submit_target(settings, partition, constraint)
    if error or target is None:
        line = "eessi-arch-guard: check-submit: %s\n" % error
        return finish(2, message=error, stderr_lines=(line,))
    loaded, error = _check_submit_loaded(env)
    if loaded is None:
        line = "eessi-arch-guard: check-submit: %s\n" % error
        return finish(2, message=error, stderr_lines=(line,))
    prefix = env.get("EESSI_PREFIX", "")
    os_type = env.get("EESSI_OS_TYPE", "")
    if not safe_absolute(prefix) or not safe_rel_subdir(os_type):
        reason = "EESSI_PREFIX or EESSI_OS_TYPE is missing or unsafe"
        return finish(
            2,
            message=reason,
            stderr_lines=("eessi-arch-guard: check-submit: %s\n" % reason,),
        )
    configured = (settings or {}).get("eb_stack", "")
    selected = eb_stack_arg or configured or "eb-stack"
    eb_stack = shutil.which(selected)
    if eb_stack is None or not os.path.isfile(eb_stack):
        reason = "eb-stack is not executable: %s" % one_line(selected, 200)
        return finish(
            2,
            message=reason,
            stderr_lines=("eessi-arch-guard: check-submit: %s\n" % reason,),
        )

    module_root = os.path.join(prefix, "software", os_type, target, "modules", "all")
    if not os.path.isdir(module_root):
        reason = "target module tree is unavailable: %s" % one_line(module_root, 300)
        return finish(
            2,
            target=target,
            loaded=len(loaded),
            message=reason,
            stderr_lines=("eessi-arch-guard: check-submit: %s\n" % reason,),
        )
    found = set(target_modules(module_root))
    for root in extend_module_roots(env, os_type, target):
        found.update(target_modules(root))
    installed = tuple(sorted(found))
    if not installed:
        reason = "submit target has empty module inventory: %s" % target
        return finish(
            2,
            target=target,
            loaded=len(loaded),
            message=reason,
            stderr_lines=("eessi-arch-guard: check-submit: %s\n" % reason,),
        )
    files = {_easyconfig_filename(name): name for name in loaded}
    try:
        with tempfile.TemporaryDirectory(prefix="eessi-arch-guard-submit-") as work:
            easystack = os.path.join(work, "loaded.yml")
            installed_path = os.path.join(work, "installed.txt")
            unsupported = os.path.join(work, "unsupported.toml")
            with open(easystack, "w", encoding="utf-8") as handle:
                handle.write("easyconfigs:\n")
                for filename in files:
                    handle.write("  - %s\n" % filename)
            with open(installed_path, "w", encoding="utf-8") as handle:
                for name in installed:
                    handle.write(_planner_module(name) + "\n")
            open(unsupported, "w", encoding="utf-8").close()
            proc = subprocess.run(
                [
                    eb_stack,
                    "stack",
                    "arch",
                    "--arch",
                    target,
                    "--easystack",
                    easystack,
                    "--installed",
                    installed_path,
                    "--unsupported",
                    unsupported,
                ],
                capture_output=True,
                text=True,
                env=strip_startup(env),
                check=False,
            )
    except OSError as exc:
        reason = "could not run eb-stack: %s" % one_line(str(exc), 200)
        return finish(
            2,
            target=target,
            loaded=len(loaded),
            message=reason,
            stderr_lines=("eessi-arch-guard: check-submit: %s\n" % reason,),
        )

    if proc.returncode == 0:
        summary = "%s loaded module(s) available for %s" % (len(loaded), target)
        return finish(0, target=target, loaded=len(loaded), message=summary)
    missing: List[str] = []
    for line in proc.stdout.splitlines():
        filename = line.split(" is flagged on ", 1)[0]
        name = files.get(filename)
        if name and name not in missing:
            missing.append(name)
    if proc.returncode == 1 and missing:
        lines = tuple(
            "eessi-arch-guard: check-submit: loaded module absent from %s: %s\n" % (target, name)
            for name in missing
        )
        return finish(1, target=target, loaded=len(loaded), missing=missing, stderr_lines=lines)
    detail = proc.stderr.strip() or proc.stdout.strip() or "exit %s" % proc.returncode
    reason = "eb-stack failed: %s" % one_line(detail, 300)
    return finish(
        2,
        target=target,
        loaded=len(loaded),
        message=reason,
        stderr_lines=("eessi-arch-guard: check-submit: %s\n" % reason,),
    )


def _chain_path_ok(path: str) -> bool:
    if not path or not path.startswith("/") or any(c in path for c in "\n\r\0"):
        return False
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _config_candidate_path(script_path: str) -> Optional[str]:
    beside = os.path.join(os.path.dirname(os.path.abspath(script_path)), "eessi-arch-guard.conf")
    for path in ("/etc/eessi-arch-guard.conf", beside):
        if os.path.lexists(path):
            return path
    return None


def _invoked_path(argv: Sequence[str]) -> str:
    """Path used to locate the beside-program configuration file."""
    if not argv:
        return __file__
    argv0 = argv[0]
    if "/" in argv0:
        return argv0
    found = shutil.which(argv0)
    if found:
        return found
    return __file__


def site_config(script_path: str) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    """Return (settings, warning). The first existing candidate wins."""
    path = _config_candidate_path(script_path)
    if path is None:
        return None, None
    reason = _config_untrusted(path)
    if reason:
        return None, "eessi-arch-guard: config ignored: " + reason
    try:
        with open(path, "rb") as handle:
            blob = handle.read()
    except OSError as exc:
        return None, "eessi-arch-guard: config ignored: %s" % exc
    settings, bad = parse_config_bytes(blob)
    if bad:
        shown = " ".join(bad)
        return settings, (
            "eessi-arch-guard: config value kept with its original bytes: %s" % shown
        )
    return settings, None


def run_chain(path: str, env: Mapping[str, str]) -> Tuple[int, bytes, bytes]:
    """Run the chained prolog with no arguments and ``env``.

    Returns its exit status, 1 when a signal ended it, its stdout and its
    stderr.
    """
    proc = subprocess.run(
        [path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env),
        check=False,
    )
    code = proc.returncode if proc.returncode >= 0 else 1
    return code, proc.stdout, proc.stderr


class Args(NamedTuple):
    """Parsed output mode, helper mode, configuration and failure policy."""

    shell: bool
    restore: bool
    help: bool
    version: bool
    chain: Optional[str]
    timeout: Optional[str]
    strict: bool = False
    syslog: Optional[str] = None
    dry_run: bool = False
    json: bool = False
    check_submit: bool = False
    partition: Optional[str] = None
    constraint: Optional[str] = None
    eb_stack: Optional[str] = None
    target: Optional[str] = None


def build_arg_parser() -> argparse.ArgumentParser:
    """Parser for generating completion and manpage assets."""
    parser = argparse.ArgumentParser(add_help=False, prog="eessi-arch-guard")
    parser.add_argument("--shell", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--chain", nargs="?", const="", default=None, metavar="PATH")
    parser.add_argument("--timeout", nargs="?", const="", default=None, metavar="SEC")
    parser.add_argument("--syslog", nargs="?", const="", default=None, metavar="TARGET")
    parser.add_argument("--restore", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--check-submit", action="store_true")
    parser.add_argument("--partition", nargs="?", const="", default=None, metavar="NAME")
    parser.add_argument("--constraint", nargs="?", const="", default=None, metavar="NAME")
    parser.add_argument("--eb-stack", nargs="?", const="", default=None, metavar="PATH")
    parser.add_argument("--target", nargs="?", const="", default=None, metavar="TARGET")
    parser.add_argument("-h", "--help", dest="help", action="store_true")
    parser.add_argument("--version", action="store_true")
    return parser


def _option_value(argv: Sequence[str], index: int) -> Tuple[str, int]:
    """The value of the option at ``argv[index]``, and the index of its last word.

    The value is the next word, unless that starts with ``-`` and so is an
    option of its own; it is empty then, and when there is no next word.
    """
    if index + 1 < len(argv) and not argv[index + 1].startswith("-"):
        return argv[index + 1], index + 1
    return "", index


def parse_args(argv: Sequence[str]) -> Args:
    """Parse the prolog argv. Flags other than the documented ones are skipped."""
    shell = restore = help_ = version = strict = dry_run = json = check_submit = False
    chain = None
    timeout = None
    syslog = None
    partition = None
    constraint = None
    eb_stack = None
    target = None
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--shell":
            shell = True
        elif arg == "--check-submit":
            check_submit = True
        elif arg == "--strict":
            strict = True
        elif arg == "--dry-run":
            dry_run = True
        elif arg == "--restore":
            restore = True
        elif arg == "--json":
            json = True
        elif arg in ("-h", "--help"):
            help_ = True
        elif arg == "--version":
            version = True
        elif arg == "--chain":
            chain, index = _option_value(argv, index)
        elif arg.startswith("--chain="):
            chain = arg.split("=", 1)[1]
        elif arg == "--timeout":
            timeout, index = _option_value(argv, index)
        elif arg.startswith("--timeout="):
            timeout = arg.split("=", 1)[1]
        elif arg == "--syslog":
            syslog, index = _option_value(argv, index)
        elif arg.startswith("--syslog="):
            syslog = arg.split("=", 1)[1]
        elif arg == "--partition":
            partition, index = _option_value(argv, index)
        elif arg.startswith("--partition="):
            partition = arg.split("=", 1)[1]
        elif arg == "--constraint":
            constraint, index = _option_value(argv, index)
        elif arg.startswith("--constraint="):
            constraint = arg.split("=", 1)[1]
        elif arg == "--eb-stack":
            eb_stack, index = _option_value(argv, index)
        elif arg.startswith("--eb-stack="):
            eb_stack = arg.split("=", 1)[1]
        elif arg == "--target":
            target, index = _option_value(argv, index)
        elif arg.startswith("--target="):
            target = arg.split("=", 1)[1]
        index += 1
    return Args(
        shell,
        restore,
        help_,
        version,
        chain,
        timeout,
        strict,
        syslog,
        dry_run,
        json,
        check_submit,
        partition,
        constraint,
        eb_stack,
        target,
    )


def _exit_semantic(exit_code: int) -> str:
    if exit_code == STRICT_FAILURE_EXIT:
        return "strict-failure"
    if exit_code == 1:
        return "restore-failed"
    return "ok"


def _reload_needed(kind: str) -> bool:
    return kind in ("reloaded", "failed")


def decision_view(
    outcome: Outcome,
    env: Mapping[str, str],
    cached: bool,
    strict: bool,
    shell: bool,
    exit_code: int,
) -> Dict[str, object]:
    """The fields ``--json`` prints as one object."""
    was = env.get("EESSI_SOFTWARE_SUBDIR") or None
    now: Optional[str] = None
    cause: Optional[str] = None
    kind = outcome.kind
    if kind == "reloaded":
        marker = outcome.changes.get("EESSI_ARCH_GUARD_RELOADED") or ""
        if "->" in marker:
            was, now = marker.split("->", 1)
        else:
            now = outcome.changes.get("EESSI_SOFTWARE_SUBDIR")
    elif kind == "failed":
        marker = outcome.changes.get("EESSI_ARCH_GUARD_FAILED") or ""
        if "->" in marker:
            was, cause = marker.split("->", 1)
        else:
            cause = "failed"
    elif kind == "skip":
        cause = _skip_cause(outcome.detail)
    elif kind == "noop":
        now = was
    return {
        "schema_version": DECISION_JSON_SCHEMA_VERSION,
        "kind": "decision",
        "decision": kind,
        "reload_needed": _reload_needed(kind),
        "cached": cached,
        "strict": strict,
        "shell": shell,
        "was": was,
        "now": now,
        "cause": cause,
        "message": outcome.message,
        "detail": outcome.detail,
        "exit": exit_code,
        "exit_semantic": _exit_semantic(exit_code),
    }


def emit_decision_json(
    outcome: Outcome,
    env: Mapping[str, str],
    cached: bool,
    strict: bool,
    shell: bool,
    exit_code: int,
) -> bytes:
    """Write one minified JSON line for the decision."""
    payload = decision_view(outcome, env, cached, strict, shell, exit_code)
    return (json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n").encode("ascii")


class SyslogTarget(NamedTuple):
    """Parsed syslog or journald destination."""

    scheme: str  # rfc5424-unix, journal-unix, rfc5424-udp, rfc5424-tcp
    host_or_path: str
    port: int = 0


def _split_host_port(addr: str) -> Optional[Tuple[str, int]]:
    if not addr:
        return None
    if addr.startswith("["):
        end = addr.find("]")
        if end < 0 or end + 2 > len(addr) or addr[end + 1] != ":":
            return None
        host = addr[1:end]
        port_s = addr[end + 2 :]
    else:
        if ":" not in addr:
            return None
        host, port_s = addr.rsplit(":", 1)
    if not host or not (port_s.isascii() and port_s.isdigit()):
        return None
    port = int(port_s)
    if port < 1 or port > 65535:
        return None
    return host, port


def parse_syslog_target(raw: str) -> Optional[SyslogTarget]:
    """Parse a syslog or journald target string, or None when invalid."""
    text = raw.strip()
    if not text or any(ch in text for ch in "\r\n\0"):
        return None
    if text == "syslog":
        return SyslogTarget("rfc5424-unix", "/dev/log", 0)
    if text in ("journald", "journal"):
        return SyslogTarget("journal-unix", "/run/systemd/journal/socket", 0)
    if text.startswith("/"):
        return SyslogTarget("rfc5424-unix", text, 0)
    if text.startswith("unix://"):
        path = text[len("unix://") :]
        if not path.startswith("/"):
            return None
        return SyslogTarget("rfc5424-unix", path, 0)
    if text.startswith("journal+unix://"):
        path = text[len("journal+unix://") :]
        if not path.startswith("/"):
            return None
        return SyslogTarget("journal-unix", path, 0)
    if text.startswith("udp://"):
        hp = _split_host_port(text[len("udp://") :])
        return SyslogTarget("rfc5424-udp", hp[0], hp[1]) if hp else None
    if text.startswith("tcp://"):
        hp = _split_host_port(text[len("tcp://") :])
        return SyslogTarget("rfc5424-tcp", hp[0], hp[1]) if hp else None
    return None


# IANA documentation Private Enterprise Number (RFC 5398 / RFC 5424 section 6.3.2),
# used as the default SD-ID suffix until a site or project PEN is assigned.
DEFAULT_SYSLOG_PEN = "32473"


def parse_syslog_pen(raw: str) -> Optional[str]:
    """Positive decimal Private Enterprise Number of at most 20 digits, or None."""
    text = raw.strip()
    if not text or len(text) > 20 or not all("0" <= ch <= "9" for ch in text):
        return None
    val = int(text)
    if val <= 0:
        return None
    return str(val)


def resolve_syslog_pen(
    env: Mapping[str, str],
    settings: Optional[Mapping[str, str]],
) -> Tuple[str, Optional[str]]:
    """Resolve the RFC 5424 SD-ID enterprise number: env overrides config, else default."""
    chosen = DEFAULT_SYSLOG_PEN
    warnings: List[str] = []
    if settings and settings.get("syslog_pen"):
        parsed = parse_syslog_pen(settings["syslog_pen"])
        if parsed is None:
            warnings.append(
                "eessi-arch-guard: config syslog_pen is not a positive integer: %s"
                % one_line(settings["syslog_pen"], 64)
            )
        else:
            chosen = parsed
    env_pen = env.get("EESSI_ARCH_GUARD_SYSLOG_PEN", "")
    if env_pen:
        parsed = parse_syslog_pen(env_pen)
        if parsed is None:
            warnings.append(
                "eessi-arch-guard: EESSI_ARCH_GUARD_SYSLOG_PEN is not a positive integer: %s"
                % one_line(env_pen, 64)
            )
        else:
            chosen = parsed
    if not warnings:
        return chosen, None
    return chosen, " ".join(warnings)


def resolve_syslog(
    cli_text: Optional[str],
    env: Mapping[str, str],
    settings: Optional[Mapping[str, str]],
) -> Tuple[Optional[SyslogTarget], bool, Optional[str]]:
    """Resolve destination, log-all flag, and any warning."""
    raw: Optional[str] = None
    source = "syslog"
    if cli_text is not None:
        raw = cli_text
        source = "--syslog"
    elif env.get("EESSI_ARCH_GUARD_SYSLOG"):
        raw = env["EESSI_ARCH_GUARD_SYSLOG"]
        source = "EESSI_ARCH_GUARD_SYSLOG"
    elif settings and settings.get("syslog"):
        raw = settings["syslog"]
        source = "config syslog"

    log_all = env.get("EESSI_ARCH_GUARD_SYSLOG_ALL") == "1" or bool(
        settings and settings.get("syslog_all") in ("1", "true", "yes")
    )
    if raw is None or raw == "":
        return None, log_all, None
    target = parse_syslog_target(raw)
    if target is None:
        return (
            None,
            log_all,
            "eessi-arch-guard: %s is not a valid syslog target: %s"
            % (source, one_line(raw, 120)),
        )
    return target, log_all, None


def _sd_escape(value: str) -> str:
    return (
        one_line(value, 256)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("]", "\\]")
    )


def _rfc5424_token(value: str, fallback: str = "-") -> str:
    cleaned = "".join(ch for ch in value if 33 <= ord(ch) <= 126)
    return cleaned[:255] if cleaned else fallback


def _skip_cause(detail: Optional[str]) -> str:
    text = detail or ""
    if "EESSI_ARCH_GUARD=0" in text:
        return "opt-out"
    if "not loaded" in text:
        return "not-loaded"
    if "OVERRIDE" in text:
        return "pinned"
    if "ignored" in text:
        return "no-software-dir-ignored"
    return "skipped"


def _event_fields(
    outcome: Outcome,
    env: Mapping[str, str],
    cached: bool,
    strict: bool,
) -> Optional[Tuple[int, str, List[Tuple[str, str]], str]]:
    kind = outcome.kind
    was = env.get("EESSI_SOFTWARE_SUBDIR", "")
    now = ""
    cause = ""
    if kind == "reloaded":
        severity = 5
        msgid = "EESSI_ARCH_RELOAD"
        marker = outcome.changes.get("EESSI_ARCH_GUARD_RELOADED") or ""
        if "->" in marker:
            was, now = marker.split("->", 1)
        else:
            now = outcome.changes.get("EESSI_SOFTWARE_SUBDIR") or ""
        msg = outcome.message or ("reloaded %s->%s" % (was, now))
    elif kind == "failed":
        severity = 3
        msgid = "EESSI_ARCH_FAIL"
        marker = outcome.changes.get("EESSI_ARCH_GUARD_FAILED") or ""
        if "->" in marker:
            was, cause = marker.split("->", 1)
        else:
            cause = "failed"
        msg = outcome.message or "eessi-arch-guard failed"
    elif kind == "skip":
        severity = 6
        msgid = "EESSI_ARCH_SKIP"
        cause = _skip_cause(outcome.detail)
        msg = outcome.detail or "eessi-arch-guard skipped"
    elif kind == "noop":
        severity = 6
        msgid = "EESSI_ARCH_NOOP"
        now = was
        msg = outcome.detail or "target already matches this node"
    else:
        return None

    params: List[Tuple[str, str]] = [
        ("kind", kind),
        ("cached", "1" if cached else "0"),
        ("strict", "1" if strict else "0"),
    ]
    if "SLURM_JOB_ID" in env and env["SLURM_JOB_ID"]:
        params.append(("job", env["SLURM_JOB_ID"]))
    if "SLURM_STEP_ID" in env and env["SLURM_STEP_ID"]:
        params.append(("step", env["SLURM_STEP_ID"]))
    if "SLURM_LOCALID" in env and env["SLURM_LOCALID"]:
        params.append(("localid", env["SLURM_LOCALID"]))
    if was:
        params.append(("was", was))
    if now:
        params.append(("now", now))
    if cause:
        params.append(("cause", cause))
    return severity, msgid, params, one_line(msg, 500)


def _format_rfc5424(
    severity: int,
    msgid: str,
    params: Sequence[Tuple[str, str]],
    msg: str,
    pen: str = DEFAULT_SYSLOG_PEN,
) -> bytes:
    pri = 8 + severity  # facility 1 (user)
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    host = _rfc5424_token(socket.gethostname())
    pid = str(os.getpid())
    sd_parts = " ".join('%s="%s"' % (k, _sd_escape(v)) for k, v in params)
    sd = "[archGuard@%s %s]" % (pen, sd_parts) if sd_parts else "-"
    line = "<%d>1 %s %s eessi-arch-guard %s %s %s %s" % (
        pri,
        ts,
        host,
        pid,
        msgid,
        sd,
        msg,
    )
    return line.encode("utf-8", "replace")


def _journal_kv(key: str, value: str) -> bytes:
    val_b = value.encode("utf-8", "replace")
    if b"\n" in val_b or b"\0" in val_b:
        return key.encode("ascii") + b"\n" + len(val_b).to_bytes(8, "little") + val_b + b"\n"
    return key.encode("ascii") + b"=" + val_b + b"\n"


def _format_journald(
    severity: int,
    msgid: str,
    params: Sequence[Tuple[str, str]],
    msg: str,
) -> bytes:
    entries: List[Tuple[str, str]] = [
        ("MESSAGE", msg),
        ("PRIORITY", str(severity)),
        ("SYSLOG_IDENTIFIER", "eessi-arch-guard"),
        ("SYSLOG_FACILITY", "1"),
        ("MSGID", msgid),
    ]
    param_map = {
        "kind": "ARCH_GUARD_KIND",
        "cached": "ARCH_GUARD_CACHED",
        "strict": "ARCH_GUARD_STRICT",
        "was": "ARCH_GUARD_WAS",
        "now": "ARCH_GUARD_NOW",
        "cause": "ARCH_GUARD_CAUSE",
        "job": "SLURM_JOB_ID",
        "step": "SLURM_STEP_ID",
        "localid": "SLURM_LOCALID",
    }
    for key, value in params:
        field = param_map.get(key)
        if field:
            entries.append((field, one_line(value, 500)))
    return b"".join(_journal_kv(k, v) for k, v in entries)


def _send_unix(path: str, payload: bytes, stream_nl: bool) -> None:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.5)
            sock.connect(path)
            sock.sendall(payload)
            return
    except OSError:
        if not stream_nl:
            return
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            sock.connect(path)
            sock.sendall(payload + b"\n")
    except OSError:
        return


def _send_syslog(
    target: SyslogTarget,
    severity: int,
    msgid: str,
    params: Sequence[Tuple[str, str]],
    msg: str,
    pen: str = DEFAULT_SYSLOG_PEN,
) -> None:
    try:
        if target.scheme == "journal-unix":
            payload = _format_journald(severity, msgid, params, msg)
            _send_unix(target.host_or_path, payload, stream_nl=False)
            return
        payload = _format_rfc5424(severity, msgid, params, msg, pen)
        if target.scheme == "rfc5424-unix":
            _send_unix(target.host_or_path, payload, stream_nl=True)
            return
        if target.scheme == "rfc5424-udp":
            for family, _, _, _, sockaddr in socket.getaddrinfo(
                target.host_or_path, target.port, 0, socket.SOCK_DGRAM
            ):
                try:
                    with socket.socket(family, socket.SOCK_DGRAM) as sock:
                        sock.settimeout(0.5)
                        sock.sendto(payload, sockaddr)
                    return
                except OSError:
                    continue
            return
        if target.scheme == "rfc5424-tcp":
            frame = ("%d " % len(payload)).encode("ascii") + payload
            with socket.create_connection((target.host_or_path, target.port), timeout=0.5) as sock:
                sock.sendall(frame)
    except Exception:
        return


def _emit_decision_syslog(
    target: Optional[SyslogTarget],
    log_all: bool,
    outcome: Outcome,
    env: Mapping[str, str],
    cached: bool,
    strict: bool,
    pen: str = DEFAULT_SYSLOG_PEN,
) -> None:
    if target is None:
        return
    if outcome.kind in ("skip", "noop") and not log_all:
        return
    built = _event_fields(outcome, env, cached, strict)
    if built is None:
        return
    severity, msgid, params, msg = built
    _send_syslog(target, severity, msgid, params, msg, pen)


def _announce(env: Mapping[str, str], shell: bool) -> bool:
    if shell:
        return False
    return env.get("SLURM_LOCALID", "0") == "0"


def _write_stdout(chain_out: bytes, text: bytes) -> None:
    if chain_out:
        write_fd(1, chain_out)
        if not chain_out.endswith(b"\n"):
            write_fd(1, b"\n")
    write_fd(1, text)


def _emergency(exc: BaseException, shell: bool) -> None:
    message = "eessi-arch-guard: skipped, %s: %s" % (type(exc).__name__, exc)
    try:
        encoded = message_bytes(message) + b"\n"
        if shell:
            write_fd(2, encoded)
        elif _announce(os.environ, shell):
            write_fd(1, b"print " + encoded)
    except Exception:
        return


class DryRunDecision(NamedTuple):
    """Read-only decision summary produced by ``--dry-run``.

    ``kind`` is ``skip``, ``noop``, ``reloaded``, or ``failed``.
    """

    kind: str
    was_cpu: str = ""
    now_cpu: str = ""
    was_accel: str = ""
    now_accel: str = ""
    compare_accel: bool = True
    pin: str = ""
    eessi: str = ""
    modules: Tuple[str, ...] = ()
    marker: str = ""
    detail: Optional[str] = None
    message: Optional[str] = None


def _read_cache_readonly(path: Optional[str], shell: bool) -> Tuple[bool, Optional[str]]:
    if path is None or not hasattr(os, "O_NOFOLLOW"):
        return False, None
    directory, name = os.path.split(path)
    try:
        loaded = _read_cache(directory, name, shell)
    except OSError:
        return False, None
    if loaded is None:
        return False, None
    return True, loaded.kind


def dry_run_decide(
    env: Mapping[str, str],
    shell: bool,
    strict: bool = False,
    cpu_probe=probe_cpu,
    accel_probe=probe_accel,
) -> DryRunDecision:
    """Compute the guard decision without running restore or writing any state."""
    if _ACTIVE is not None:
        _ACTIVE.check()
    if env.get("EESSI_ARCH_GUARD") == "0":
        return DryRunDecision("skip", detail="EESSI_ARCH_GUARD=0")
    was = env.get("EESSI_SOFTWARE_SUBDIR", "")
    if not was:
        return DryRunDecision("skip", detail="EESSI is not loaded")
    invalid = _invalid_guard_environment(env)
    if invalid:
        return DryRunDecision(
            "failed",
            was_cpu=was,
            marker="%s->%s" % (_side(was), "invalid-environment"),
            message=one_line("eessi-arch-guard: %s; environment left as it was" % invalid),
        )
    software_override = env.get("EESSI_SOFTWARE_SUBDIR_OVERRIDE", "")
    if software_override and safe_rel_subdir(software_override):
        return DryRunDecision("skip", was_cpu=was, detail="EESSI_SOFTWARE_SUBDIR_OVERRIDE is set")
    if env.get("EESSI_ARCHDETECT_OPTIONS_OVERRIDE"):
        return DryRunDecision(
            "skip", was_cpu=was, detail="EESSI_ARCHDETECT_OPTIONS_OVERRIDE is set"
        )

    cpu = cpu_probe(env)
    if cpu.status == "error":
        return DryRunDecision(
            "failed",
            was_cpu=was,
            marker="%s->%s" % (_side(was), "archdetect-failed"),
            message=one_line(
                "eessi-arch-guard: archdetect failed (%s); environment left as it was"
                % cpu.error
            ),
        )
    if cpu.status == "none":
        if env.get("EESSI_ARCH_GUARD_MISSING") == "ignore":
            return DryRunDecision(
                "skip", was_cpu=was, detail="no EESSI software directory (ignored)"
            )
        shown = " ".join(cpu.candidates) or "(none)"
        return DryRunDecision(
            "failed",
            was_cpu=was,
            marker="%s->%s" % (_side(was), "no-software-dir"),
            message=one_line(
                "eessi-arch-guard: no EESSI software directory for this node "
                "under %s (candidates %s); modules were loaded for %s; "
                "environment left as it was" % (env.get("EESSI_PREFIX", ""), shown, was)
            ),
        )

    compare_accel = not env.get("EESSI_ACCELERATOR_TARGET_OVERRIDE")
    was_accel = env.get("EESSI_ACCEL_SUBDIR", "")
    if compare_accel:
        accel = accel_probe(env)
        if accel.status == "error":
            return DryRunDecision(
                "failed",
                was_cpu=was,
                now_cpu=cpu.target,
                was_accel=was_accel,
                compare_accel=compare_accel,
                pin=cpu.override,
                marker="%s->%s" % (_side(was), "archdetect-failed"),
                message=one_line(
                    "eessi-arch-guard: archdetect failed (%s); environment left as it was"
                    % accel.error
                ),
            )
        now_accel = accel.subdir
    else:
        now_accel = was_accel

    now = cpu.target
    if now == was and now_accel == was_accel:
        return DryRunDecision(
            "noop",
            was_cpu=was,
            now_cpu=now,
            was_accel=was_accel,
            now_accel=now_accel,
            compare_accel=compare_accel,
            pin=cpu.override,
            detail="target already matches this node",
        )

    if compare_accel and was_accel != now_accel:
        was_label = "%s %s" % (was, was_accel or "no accel")
        now_label = "%s %s" % (now, now_accel or "no accel")
    else:
        was_label, now_label = was, now

    loaded = parse_loaded_modules(env)
    if loaded.error:
        why = (
            "a loaded module name is not safe to pass to Lmod"
            if loaded.error == "unsafe-module-name"
            else "the loaded module list has no single EESSI/<version> entry"
        )
        tok = "unsafe-module-name" if loaded.error == "unsafe-module-name" else "restore-failed"
        return DryRunDecision(
            "failed",
            was_cpu=was,
            now_cpu=now,
            was_accel=was_accel,
            now_accel=now_accel,
            compare_accel=compare_accel,
            pin=cpu.override,
            marker="%s->%s" % (_side(was_label), _side(tok)),
            message=one_line("eessi-arch-guard: %s; environment left as it was" % why),
        )
    if not lmod_invocable(env.get("LMOD_CMD", "")):
        return DryRunDecision(
            "failed",
            was_cpu=was,
            now_cpu=now,
            was_accel=was_accel,
            now_accel=now_accel,
            compare_accel=compare_accel,
            pin=cpu.override,
            eessi=loaded.eessi,
            modules=loaded.others,
            marker="%s->%s" % (_side(was_label), "lmod-unavailable"),
            message=one_line(
                "eessi-arch-guard: LMOD_CMD is missing or not executable; "
                "this node is %s, the modules were loaded for %s; environment left as it was"
                % (now_label, was_label)
            ),
        )
    try:
        restore_work_base(env, strict=strict)
    except (OSError, ValueError) as exc:
        return DryRunDecision(
            "failed",
            was_cpu=was,
            now_cpu=now,
            was_accel=was_accel,
            now_accel=now_accel,
            compare_accel=compare_accel,
            pin=cpu.override,
            eessi=loaded.eessi,
            modules=loaded.others,
            marker="%s->%s" % (_side(was_label), "restore-failed"),
            message=one_line(
                "eessi-arch-guard: restore failed (restore workspace unavailable: %s); "
                "environment left as it was" % exc
            ),
        )

    return DryRunDecision(
        "reloaded",
        was_cpu=was,
        now_cpu=now,
        was_accel=was_accel,
        now_accel=now_accel,
        compare_accel=compare_accel,
        pin=cpu.override,
        eessi=loaded.eessi,
        modules=loaded.others,
        marker="%s->%s" % (_side(was_label), _side(now_label)),
        message=one_line(
            "eessi-arch-guard: would reload modules for %s (loaded for %s)"
            % (now_label, was_label)
        ),
    )


def limited_dry_run(
    env: Mapping[str, str],
    shell: bool,
    timeout: float = DEFAULT_TIMEOUT,
    strict: bool = False,
    cpu_probe=probe_cpu,
    accel_probe=probe_accel,
) -> DryRunDecision:
    """Evaluate a dry-run decision under one deadline without touching the cache."""
    global _ACTIVE
    if _ACTIVE is not None:
        return dry_run_decide(env, shell, strict, cpu_probe, accel_probe)
    limit = DecisionLimit(timeout)
    _ACTIVE = limit
    limit.install()
    was = env.get("EESSI_SOFTWARE_SUBDIR", "")
    try:
        try:
            return dry_run_decide(env, shell, strict, cpu_probe, accel_probe)
        except _Deadline:
            return DryRunDecision(
                "failed",
                was_cpu=was,
                marker="%s->%s" % (_side(was), "timeout"),
                message="eessi-arch-guard: decision exceeded the time limit; "
                "environment left as it was",
            )
        except _Terminated:
            return DryRunDecision(
                "failed",
                was_cpu=was,
                marker="%s->%s" % (_side(was), "terminated"),
                message="eessi-arch-guard: decision was terminated; "
                "environment left as it was",
            )
    finally:
        limit.restore()
        _ACTIVE = None


def format_syslog_target(target: Optional[SyslogTarget]) -> Optional[str]:
    """Canonical string representation of a parsed syslog target."""
    if target is None:
        return None
    if target.scheme == "rfc5424-unix":
        return "unix://" + target.host_or_path
    if target.scheme == "journal-unix":
        return "journal+unix://" + target.host_or_path
    if target.scheme == "rfc5424-udp":
        host = "[%s]" % target.host_or_path if ":" in target.host_or_path else target.host_or_path
        return "udp://%s:%d" % (host, target.port)
    if target.scheme == "rfc5424-tcp":
        host = "[%s]" % target.host_or_path if ":" in target.host_or_path else target.host_or_path
        return "tcp://%s:%d" % (host, target.port)
    return None


class DryRunReport(NamedTuple):
    """Configuration, environment, and decision summary for ``--dry-run``."""

    mode: str
    strict: bool
    announce: bool
    config_path: Optional[str]
    config_status: str
    config_warning: Optional[str]
    chain_path: Optional[str]
    chain_source: str
    chain_status: str
    timeout: float
    syslog_text: Optional[str]
    syslog_all: bool
    syslog_pen: str
    job_id: str
    step_id: str
    local_id: Optional[str]
    cache_path: Optional[str]
    cache_hit: bool
    cached_kind: Optional[str]
    decision: DryRunDecision
    exit_code: int


def _dry_run_cause(decision: DryRunDecision) -> Optional[str]:
    if decision.kind == "skip" and decision.detail:
        return _skip_cause(decision.detail)
    if decision.kind == "failed" and decision.marker and "->" in decision.marker:
        return decision.marker.rsplit("->", 1)[-1]
    return None


def _dry_run_payload(report: DryRunReport) -> Dict[str, object]:
    decision = report.decision
    kind = decision.kind
    return {
        "schema_version": DECISION_JSON_SCHEMA_VERSION,
        "kind": "decision",
        "decision": kind,
        "reload_needed": _reload_needed(kind),
        "cached": report.cache_hit,
        "strict": report.strict,
        "shell": report.mode == "shell",
        "cause": _dry_run_cause(decision),
        "mode": report.mode,
        "announce": report.announce,
        "config": {
            "path": report.config_path,
            "status": report.config_status,
            "warning": report.config_warning,
        },
        "chain": {
            "path": report.chain_path,
            "source": report.chain_source,
            "status": report.chain_status,
        },
        "timeout": report.timeout,
        "syslog": {
            "target": report.syslog_text,
            "all": report.syslog_all,
            "pen": report.syslog_pen,
        },
        "job": {
            "id": report.job_id or None,
            "step": report.step_id if report.job_id else None,
            "localid": report.local_id,
        },
        "cache": {
            "path": report.cache_path,
            "hit": report.cache_hit,
            "cached_kind": report.cached_kind,
        },
        "was": decision.was_cpu or None,
        "now": decision.now_cpu or None,
        "was_accel": decision.was_accel or None,
        "now_accel": decision.now_accel or None,
        "compare_accel": decision.compare_accel,
        "pin": decision.pin or None,
        "eessi": decision.eessi or None,
        "modules": list(decision.modules),
        "marker": decision.marker or None,
        "detail": decision.detail,
        "message": decision.message,
        "exit": report.exit_code,
        "exit_semantic": _exit_semantic(report.exit_code),
    }


def render_dry_run_json(report: DryRunReport) -> bytes:
    """Format a ``--dry-run --json`` report as one ASCII JSON line."""
    return (
        json.dumps(_dry_run_payload(report), ensure_ascii=True, sort_keys=True) + "\n"
    ).encode("ascii")


def render_dry_run_text(report: DryRunReport) -> bytes:
    """Format a ``--dry-run`` diagnostic report as ``key: value`` lines."""
    decision = report.decision
    if report.config_status == "none":
        config_line = "none"
    else:
        shown_cfg = one_line(report.config_path) if report.config_path else "none"
        config_line = "%s (%s)" % (shown_cfg, report.config_status)

    if report.chain_status == "none":
        chain_line = "none"
    elif report.chain_status == "skipped":
        chain_line = "none (skipped in --shell)"
    else:
        shown_chain = one_line(report.chain_path) if report.chain_path else "(empty)"
        chain_line = "%s (%s, %s)" % (shown_chain, report.chain_source, report.chain_status)

    if not report.job_id:
        step_line = "none"
    elif report.step_id:
        step_line = one_line(report.step_id)
    else:
        step_line = "(empty)"

    if report.local_id is None:
        localid_line = "unset"
    elif report.local_id:
        localid_line = one_line(report.local_id)
    else:
        localid_line = "(empty)"

    if report.cache_path is None:
        cache_line = "none"
    elif report.cache_hit:
        cache_line = "%s (hit: %s)" % (one_line(report.cache_path), report.cached_kind or "unknown")
    else:
        cache_line = "%s (miss)" % one_line(report.cache_path)

    lines = [
        "mode: %s" % report.mode,
        "strict: %s" % ("true" if report.strict else "false"),
        "announce: %s" % ("true" if report.announce else "false"),
        "config: %s" % config_line,
        "chain: %s" % chain_line,
        "timeout: %s" % _seconds_label(report.timeout),
        "syslog: %s" % (one_line(report.syslog_text) if report.syslog_text else "none"),
        "syslog_all: %s" % ("true" if report.syslog_all else "false"),
        "syslog_pen: %s" % report.syslog_pen,
        "job: %s" % (one_line(report.job_id) if report.job_id else "none"),
        "step: %s" % step_line,
        "localid: %s" % localid_line,
        "cache: %s" % cache_line,
        "decision: %s" % decision.kind,
        "was: %s" % (one_line(decision.was_cpu) if decision.was_cpu else "none"),
        "now: %s" % (one_line(decision.now_cpu) if decision.now_cpu else "none"),
        "was_accel: %s" % (one_line(decision.was_accel) if decision.was_accel else "none"),
        "now_accel: %s" % (one_line(decision.now_accel) if decision.now_accel else "none"),
        "compare_accel: %s" % ("true" if decision.compare_accel else "false"),
        "pin: %s" % (one_line(decision.pin) if decision.pin else "none"),
        "eessi: %s" % (one_line(decision.eessi) if decision.eessi else "none"),
        "modules: %s"
        % (" ".join(one_line(m) for m in decision.modules) if decision.modules else "none"),
        "marker: %s" % (one_line(decision.marker) if decision.marker else "none"),
        "detail: %s" % (one_line(decision.detail) if decision.detail else "none"),
        "message: %s" % (one_line(decision.message) if decision.message else "none"),
        "exit: %d" % report.exit_code,
    ]
    return ("\n".join(lines) + "\n").encode("utf-8", "replace")


def _main(argv: Sequence[str]) -> int:
    args = parse_args(argv)
    if args.help:
        write_fd(1, HELP.encode("ascii"))
        return 0
    if args.version:
        write_fd(1, (__version__ + "\n").encode("ascii"))
        return 0
    if args.restore:
        return restore_main()

    env = dict(os.environ)
    chain_out = b""
    # `timeout` is read for `--shell` and for a `--chain` wrapper too.
    script_path = _invoked_path(argv)
    config_path = _config_candidate_path(script_path)
    settings, config_warning = site_config(script_path)
    if settings is not None:
        config_status = "loaded"
    elif config_path is not None or config_warning is not None:
        config_status = "ignored"
    else:
        config_status = "none"

    notes = []
    if config_warning:
        notes.append(config_warning)
    if args.check_submit:
        for note in notes:
            write_fd(2, message_bytes(note) + b"\n")
        return check_submit(
            env,
            settings,
            args.partition or "",
            args.constraint or "",
            args.eb_stack or "",
            args.json,
            args.target,
        )
    chain = None
    chain_path: Optional[str] = None
    chain_source = "none"
    chain_status = "none"
    if args.shell:
        chain_status = "skipped"
    else:
        chain = args.chain
        if chain is None:
            chain = config_chain(settings)
            if chain:
                chain_source = "config"
                chain_path = chain
                if _chain_path_ok(chain):
                    chain_status = "valid"
                else:
                    chain_status = "invalid"
                    notes.append(
                        "eessi-arch-guard: config chain is not an absolute executable: %s"
                        % one_line(chain, 200)
                    )
                    chain = None
        else:
            chain_source = "cli"
            chain_path = chain
            if _chain_path_ok(chain):
                chain_status = "valid"
            else:
                chain_status = "invalid"
                notes.append("eessi-arch-guard: --chain is not an absolute executable")
                chain = None
    for note in notes:
        write_fd(2, message_bytes(note) + b"\n")
    if chain and not args.dry_run:
        code, chain_out, err = run_chain(chain, env)
        if err:
            write_fd(2, err)
        if code != 0:
            _write_stdout(chain_out, b"")
            return code

    timeout, timeout_warning = resolve_timeout(args.timeout, settings)
    if timeout_warning:
        write_fd(2, message_bytes(timeout_warning) + b"\n")
    syslog_target, syslog_all, syslog_warning = resolve_syslog(args.syslog, env, settings)
    if syslog_warning:
        write_fd(2, message_bytes(syslog_warning) + b"\n")
    syslog_pen, pen_warning = resolve_syslog_pen(env, settings)
    if pen_warning:
        write_fd(2, message_bytes(pen_warning) + b"\n")

    if args.dry_run:
        mode = "shell" if args.shell else "prolog"
        announce = _announce(env, args.shell)
        job_id = env.get("SLURM_JOB_ID", "")
        step_id = env.get("SLURM_STEP_ID", "batch")
        local_id = env.get("SLURM_LOCALID")
        cache_path = cache_file_path(env, shell=args.shell)
        cache_hit, cached_kind = _read_cache_readonly(cache_path, shell=args.shell)
        try:
            decision = limited_dry_run(
                env, shell=args.shell, timeout=timeout, strict=args.strict
            )
            exit_code = STRICT_FAILURE_EXIT if (args.strict and decision.kind == "failed") else 0
            formatter = render_dry_run_json if args.json else render_dry_run_text
            report = DryRunReport(
                mode=mode,
                strict=args.strict,
                announce=announce,
                config_path=config_path,
                config_status=config_status,
                config_warning=config_warning,
                chain_path=chain_path,
                chain_source=chain_source,
                chain_status=chain_status,
                timeout=timeout,
                syslog_text=format_syslog_target(syslog_target),
                syslog_all=syslog_all,
                syslog_pen=syslog_pen,
                job_id=job_id,
                step_id=step_id,
                local_id=local_id,
                cache_path=cache_path,
                cache_hit=cache_hit,
                cached_kind=cached_kind,
                decision=decision,
                exit_code=exit_code,
            )
            output = formatter(report)
        except Exception as exc:
            _emergency(exc, True)
            return STRICT_FAILURE_EXIT if args.strict else 0
        write_fd(1, output)
        return exit_code

    try:
        outcome = limited_decide(env, shell=args.shell, timeout=timeout)
    except Exception as exc:
        failed = _failed(env.get("EESSI_SOFTWARE_SUBDIR", ""), type(exc).__name__, str(exc))
        _emit_decision_syslog(
            syslog_target,
            syslog_all,
            failed,
            env,
            False,
            args.strict,
            syslog_pen,
        )
        code = STRICT_FAILURE_EXIT if args.strict else 0
        if args.json:
            write_fd(1, emit_decision_json(failed, env, False, args.strict, args.shell, code))
        else:
            _write_stdout(chain_out, b"")
            _emergency(exc, args.shell)
        return code

    _emit_decision_syslog(
        syslog_target, syslog_all, outcome, env, _LAST_CACHED, args.strict, syslog_pen
    )
    code = STRICT_FAILURE_EXIT if (args.strict and outcome.kind == "failed") else 0
    if args.shell and outcome.message:
        write_fd(2, message_bytes(outcome.message) + b"\n")
    if env.get("EESSI_ARCH_GUARD_VERBOSE") == "1" and outcome.detail:
        write_fd(2, message_bytes(outcome.detail) + b"\n")
    if args.json:
        write_fd(1, emit_decision_json(outcome, env, _LAST_CACHED, args.strict, args.shell, code))
    else:
        text = render_bytes(
            outcome.changes, outcome.message, args.shell, _announce(env, args.shell)
        )
        _write_stdout(chain_out, text)
    return code


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the guard with its selected decision failure policy."""
    global _STRICT_MODE
    if argv is None:
        argv = sys.argv[1:]
    previous = _STRICT_MODE
    _STRICT_MODE = "--strict" in argv
    try:
        return _main(argv)
    except (Exception, KeyboardInterrupt) as exc:
        if "--dry-run" in argv:
            _emergency(exc, "--shell" in argv or "--dry-run" in argv)
        elif "--json" in argv and _STRICT_MODE:
            failed = _failed(
                os.environ.get("EESSI_SOFTWARE_SUBDIR", ""),
                type(exc).__name__,
                str(exc),
            )
            payload = emit_decision_json(
                failed, os.environ, False, True, "--shell" in argv, STRICT_FAILURE_EXIT
            )
            write_fd(1, payload)
        elif "--json" in argv:
            failed = _failed(
                os.environ.get("EESSI_SOFTWARE_SUBDIR", ""),
                type(exc).__name__,
                str(exc),
            )
            payload = emit_decision_json(
                failed, os.environ, False, False, "--shell" in argv, 0
            )
            write_fd(1, payload)
        else:
            _emergency(exc, "--shell" in argv)
        return STRICT_FAILURE_EXIT if _STRICT_MODE else 0
    finally:
        _STRICT_MODE = previous


if __name__ == "__main__":
    sys.exit(main())
