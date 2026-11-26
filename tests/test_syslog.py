"""Tests for structured RFC 5424 and native journald decision logging."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import harness  # noqa: E402

ZEN2 = "x86_64/amd/zen2"
ZEN4 = "x86_64/amd/zen4"

RFC5424_RE = re.compile(
    r"^<(\d+)>1 "
    r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z) "
    r"(\S+) "
    r"(eessi-arch-guard) "
    r"(\d+|-) "
    r"([A-Z0-9_]+) "
    r"(\[archGuard@([0-9]+) [^\]]*\]) "
    r"(.*)$"
)


def _parse_sd_params(sd_block: str) -> dict[str, str]:
    m = re.match(r"^\[archGuard@[0-9]+ (.*)\]$", sd_block)
    assert m is not None, sd_block
    inner = m.group(1)
    out: dict[str, str] = {}
    i = 0
    while i < len(inner):
        while i < len(inner) and inner[i] == " ":
            i += 1
        if i >= len(inner):
            break
        eq = inner.index("=", i)
        key = inner[i:eq]
        assert inner[eq + 1] == '"', inner[i:]
        j = eq + 2
        val_chars = []
        while j < len(inner):
            ch = inner[j]
            if ch == "\\":
                val_chars.append(inner[j + 1])
                j += 2
            elif ch == '"':
                j += 1
                break
            else:
                val_chars.append(ch)
                j += 1
        out[key] = "".join(val_chars)
        i = j
    return out


def _parse_journal_datagram(raw: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    i = 0
    while i < len(raw):
        nl = raw.index(b"\n", i)
        line = raw[i:nl]
        if b"=" in line:
            k, v = line.split(b"=", 1)
            out[k.decode("ascii")] = v.decode("utf-8")
            i = nl + 1
        else:
            k = line.decode("ascii")
            length = int.from_bytes(raw[nl + 1 : nl + 9], "little")
            val = raw[nl + 9 : nl + 9 + length].decode("utf-8")
            assert raw[nl + 9 + length : nl + 10 + length] == b"\n"
            out[k] = val
            i = nl + 10 + length
    return out


class SyslogTests(unittest.TestCase):
    def setUp(self):
        self.site = harness.Site()
        for cpu in (ZEN2, ZEN4):
            self.site.add_cpu(cpu)
            self.site.add_module(cpu, "R/4.4.2")
        self.sockdir = tempfile.mkdtemp(prefix="arch-guard-sock-")

    def tearDown(self):
        self.site.close()
        for entry in os.listdir(self.sockdir):
            os.unlink(os.path.join(self.sockdir, entry))
        os.rmdir(self.sockdir)

    def _dgram_server(self, name: str):
        path = os.path.join(self.sockdir, name)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.bind(path)
        sock.settimeout(2.0)
        return sock, path

    def _recv_all_dgram(self, sock: socket.socket) -> list[bytes]:
        msgs: list[bytes] = []
        sock.settimeout(0.2)
        while True:
            try:
                msgs.append(sock.recv(65536))
            except socket.timeout:
                break
        return msgs

    def _cpu_env(self, loaded: str, node: str, extra: dict[str, str] | None = None):
        self.site.write_cpuinfo(node)
        env = harness.base_env(self.site, loaded, ["R/4.4.2"], gpu="none")
        if extra:
            env.update(extra)
        return env

    def test_reload_emits_rfc5424_and_cache_hit_marks_cached(self):
        sock, path = self._dgram_server("syslog.sock")
        try:
            env0 = self._cpu_env(
                ZEN4,
                "zen2",
                {
                    "SLURM_JOB_ID": "4242",
                    "SLURM_STEP_ID": "3",
                    "SLURM_LOCALID": "0",
                    "EESSI_ARCH_GUARD_SYSLOG": f"unix://{path}",
                },
            )
            p0 = harness.run_guard(env0)
            self.assertEqual(p0.returncode, 0, p0.stderr)
            self.assertIn("export EESSI_SOFTWARE_SUBDIR=" + ZEN2, p0.stdout)

            raw0 = sock.recv(65536).decode("utf-8")
            m0 = RFC5424_RE.fullmatch(raw0)
            self.assertIsNotNone(m0, raw0)
            assert m0 is not None
            self.assertEqual(m0.group(1), "13")  # facility=1 (user), severity=5 (notice)
            self.assertEqual(m0.group(6), "EESSI_ARCH_RELOAD")
            sd0 = _parse_sd_params(m0.group(7))
            self.assertEqual(sd0["kind"], "reloaded")
            self.assertEqual(sd0["cached"], "0")
            self.assertEqual(sd0["strict"], "0")
            self.assertEqual(sd0["job"], "4242")
            self.assertEqual(sd0["step"], "3")
            self.assertEqual(sd0["localid"], "0")
            self.assertEqual(sd0["was"], ZEN4)
            self.assertEqual(sd0["now"], ZEN2)

            env1 = dict(env0)
            env1["SLURM_LOCALID"] = "1"
            p1 = harness.run_guard(env1)
            self.assertEqual(p1.returncode, 0, p1.stderr)
            raw1 = sock.recv(65536).decode("utf-8")
            m1 = RFC5424_RE.fullmatch(raw1)
            self.assertIsNotNone(m1, raw1)
            assert m1 is not None
            self.assertEqual(m1.group(6), "EESSI_ARCH_RELOAD")
            sd1 = _parse_sd_params(m1.group(7))
            self.assertEqual(sd1["cached"], "1")
            self.assertEqual(sd1["localid"], "1")
        finally:
            sock.close()

    def test_failed_strict_emits_err_msgid(self):
        sock, path = self._dgram_server("fail.sock")
        try:
            env = self._cpu_env(ZEN4, "zen2")
            env["EESSI_PREFIX"] = os.path.join(self.site.root, "missing-prefix")
            env["SLURM_JOB_ID"] = "77"
            proc = harness.run_guard(env, ["--strict", f"--syslog=unix://{path}"])
            self.assertEqual(proc.returncode, 78, proc.stderr)
            raw = sock.recv(65536).decode("utf-8")
            m = RFC5424_RE.fullmatch(raw)
            self.assertIsNotNone(m, raw)
            assert m is not None
            self.assertEqual(m.group(1), "11")  # facility=1, severity=3 (err)
            self.assertEqual(m.group(6), "EESSI_ARCH_FAIL")
            sd = _parse_sd_params(m.group(7))
            self.assertEqual(sd["kind"], "failed")
            self.assertEqual(sd["strict"], "1")
            self.assertEqual(sd["job"], "77")
            self.assertEqual(sd["cause"], "no-software-dir")
        finally:
            sock.close()

    def test_noop_and_skip_require_syslog_all(self):
        sock, path = self._dgram_server("all.sock")
        try:
            env_noop = self._cpu_env(ZEN2, "zen2", {"EESSI_ARCH_GUARD_SYSLOG": f"unix://{path}"})
            p_quiet = harness.run_guard(env_noop)
            self.assertEqual(p_quiet.returncode, 0)
            self.assertEqual(self._recv_all_dgram(sock), [])

            env_noop["EESSI_ARCH_GUARD_SYSLOG_ALL"] = "1"
            p_noop = harness.run_guard(env_noop)
            self.assertEqual(p_noop.returncode, 0)
            raw_noop = sock.recv(65536).decode("utf-8")
            m_noop = RFC5424_RE.fullmatch(raw_noop)
            self.assertIsNotNone(m_noop, raw_noop)
            assert m_noop is not None
            self.assertEqual(m_noop.group(1), "14")  # facility=1, severity=6 (info)
            self.assertEqual(m_noop.group(6), "EESSI_ARCH_NOOP")
            self.assertEqual(_parse_sd_params(m_noop.group(7))["kind"], "noop")

            env_skip = dict(env_noop)
            env_skip["EESSI_ARCH_GUARD"] = "0"
            p_skip = harness.run_guard(env_skip)
            self.assertEqual(p_skip.returncode, 0)
            raw_skip = sock.recv(65536).decode("utf-8")
            m_skip = RFC5424_RE.fullmatch(raw_skip)
            self.assertIsNotNone(m_skip, raw_skip)
            assert m_skip is not None
            self.assertEqual(m_skip.group(6), "EESSI_ARCH_SKIP")
            sd_skip = _parse_sd_params(m_skip.group(7))
            self.assertEqual(sd_skip["kind"], "skip")
            self.assertEqual(sd_skip["cause"], "opt-out")
        finally:
            sock.close()

    def test_native_journald_wire_format(self):
        sock, path = self._dgram_server("journal.sock")
        try:
            env = self._cpu_env(
                ZEN4,
                "zen2",
                {
                    "SLURM_JOB_ID": "9001",
                    "SLURM_STEP_ID": "0",
                    "SLURM_LOCALID": "0",
                },
            )
            proc = harness.run_guard(env, ["--syslog", f"journal+unix://{path}"])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            fields = _parse_journal_datagram(sock.recv(65536))
            for key in fields:
                self.assertFalse(key.startswith("_"), f"client must not send trusted field {key}")
            self.assertEqual(fields["PRIORITY"], "5")
            self.assertEqual(fields["SYSLOG_IDENTIFIER"], "eessi-arch-guard")
            self.assertEqual(fields["MSGID"], "EESSI_ARCH_RELOAD")
            self.assertEqual(fields["ARCH_GUARD_KIND"], "reloaded")
            self.assertEqual(fields["ARCH_GUARD_WAS"], ZEN4)
            self.assertEqual(fields["ARCH_GUARD_NOW"], ZEN2)
            self.assertEqual(fields["ARCH_GUARD_CACHED"], "0")
            self.assertEqual(fields["SLURM_JOB_ID"], "9001")
            self.assertIn("MESSAGE", fields)
        finally:
            sock.close()

    def test_tcp_octet_counting_and_udp(self):
        from unittest import mock

        import eessi_arch_guard as guard

        target_tcp = guard.SyslogTarget("rfc5424-tcp", "inprocess", 0)
        server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            with mock.patch("socket.create_connection", return_value=client):
                guard._send_syslog(
                    target_tcp, 5, "EESSI_ARCH_RELOAD", [("kind", "reloaded")], "reloaded"
                )
            frame = server.recv(65536).decode("utf-8")
            length_s, msg = frame.split(" ", 1)
            self.assertEqual(int(length_s), len(msg.encode("utf-8")))
            self.assertIsNotNone(RFC5424_RE.fullmatch(msg), msg)
        finally:
            server.close()
            client.close()

        captured: list[bytes] = []

        class _Datagram:
            def __init__(self, *_args, **_kwargs):
                pass

            def settimeout(self, _timeout):
                return None

            def sendto(self, data, _addr):
                captured.append(data)
                return len(data)

            def close(self):
                return None

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        real_socket = socket.socket

        def _socket(family, kind, *args, **kwargs):
            if kind == socket.SOCK_DGRAM:
                return _Datagram()
            return real_socket(family, kind, *args, **kwargs)

        target_udp = guard.SyslogTarget("rfc5424-udp", "inprocess", 0)
        fake_addr = [(socket.AF_UNIX, socket.SOCK_DGRAM, 0, "", ("inprocess", 0))]
        with (
            mock.patch("socket.getaddrinfo", return_value=fake_addr),
            mock.patch("socket.socket", side_effect=_socket),
        ):
            guard._send_syslog(
                target_udp, 5, "EESSI_ARCH_RELOAD", [("kind", "reloaded")], "reloaded"
            )
        self.assertEqual(len(captured), 1)
        self.assertIsNotNone(RFC5424_RE.fullmatch(captured[0].decode("utf-8")), captured[0])

    def test_syslog_failure_never_breaks_prolog_or_pollutes_stdout(self):
        env = self._cpu_env(ZEN4, "zen2")
        proc = harness.run_guard(env, ["--syslog=unix:///nonexistent/eessi-arch-guard.sock"])
        self.assertEqual(proc.returncode, 0)
        self.assertIn("export EESSI_SOFTWARE_SUBDIR=" + ZEN2, proc.stdout)
        self.assertNotIn("syslog", proc.stdout.lower())

        bad = harness.run_guard(env, ["--syslog=bogus://target"])
        self.assertEqual(bad.returncode, 0)
        self.assertIn("export EESSI_SOFTWARE_SUBDIR=" + ZEN2, bad.stdout)
        self.assertIn("syslog", bad.stderr.lower())

    def test_custom_syslog_pen_from_env_and_config_and_invalid_pen_warns(self):
        import eessi_arch_guard as guard  # noqa: E402

        self.assertEqual(guard.DEFAULT_SYSLOG_PEN, "32473")
        pen, warn = guard.resolve_syslog_pen(
            {"EESSI_ARCH_GUARD_SYSLOG_PEN": "49152"},
            {"syslog_pen": "12345"},
        )
        self.assertEqual(pen, "49152")
        self.assertIsNone(warn)

        pen_cfg, warn_cfg = guard.resolve_syslog_pen({}, {"syslog_pen": "12345"})
        self.assertEqual(pen_cfg, "12345")
        self.assertIsNone(warn_cfg)

        pen_bad, warn_bad = guard.resolve_syslog_pen(
            {"EESSI_ARCH_GUARD_SYSLOG_PEN": "0"},
            {"syslog_pen": "12345"},
        )
        self.assertEqual(pen_bad, "12345")
        self.assertIsNotNone(warn_bad)

        sock, path = self._dgram_server("pen.sock")
        try:
            env = self._cpu_env(
                ZEN4,
                "zen2",
                {
                    "EESSI_ARCH_GUARD_SYSLOG": f"unix://{path}",
                    "EESSI_ARCH_GUARD_SYSLOG_PEN": "55555",
                },
            )
            proc = harness.run_guard(env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            raw = sock.recv(65536).decode("utf-8")
            m = RFC5424_RE.fullmatch(raw)
            self.assertIsNotNone(m, raw)
            assert m is not None
            self.assertEqual(m.group(8), "55555")
            self.assertIn("[archGuard@55555 ", raw)

            bad_env = self._cpu_env(
                ZEN4,
                "zen2",
                {
                    "EESSI_ARCH_GUARD_SYSLOG": f"unix://{path}",
                    "EESSI_ARCH_GUARD_SYSLOG_PEN": "bad-pen",
                },
            )
            p_bad = harness.run_guard(bad_env)
            self.assertEqual(p_bad.returncode, 0)
            self.assertIn("syslog_pen", p_bad.stderr.lower())
            raw_bad = sock.recv(65536).decode("utf-8")
            m_bad = RFC5424_RE.fullmatch(raw_bad)
            self.assertIsNotNone(m_bad, raw_bad)
            assert m_bad is not None
            self.assertEqual(m_bad.group(8), "32473")
        finally:
            sock.close()

    def test_dry_run_preserves_ipv6_brackets_in_syslog_target(self):
        env = self._cpu_env(ZEN4, "zen2")
        for spec in ("tcp://[::1]:514", "udp://[2001:db8::1]:1514"):
            proc = harness.run_guard(env, ["--dry-run", "--json", f"--syslog={spec}"])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            doc = json.loads(proc.stdout)
            self.assertEqual(doc["syslog"]["target"], spec)

        text_proc = harness.run_guard(env, ["--dry-run", "--syslog=tcp://[::1]:514"])
        self.assertEqual(text_proc.returncode, 0, text_proc.stderr)
        self.assertIn("syslog: tcp://[::1]:514\n", text_proc.stdout)

    def test_non_utf8_env_bytes_are_replaced_not_raw(self):
        sock_rfc, path_rfc = self._dgram_server("utf8-rfc.sock")
        sock_jrn, path_jrn = self._dgram_server("utf8-jrn.sock")
        bad_job = os.fsdecode(b"77\xff")
        try:
            env = self._cpu_env(
                ZEN2,
                "zen2",
                {
                    "SLURM_JOB_ID": bad_job,
                    "EESSI_ARCH_GUARD_SYSLOG_ALL": "1",
                },
            )
            p_rfc = harness.run_guard(env, [f"--syslog=unix://{path_rfc}"])
            self.assertEqual(p_rfc.returncode, 0, p_rfc.stderr)
            raw_rfc = sock_rfc.recv(65536).decode("utf-8")
            m = RFC5424_RE.fullmatch(raw_rfc)
            self.assertIsNotNone(m, raw_rfc)
            assert m is not None
            self.assertEqual(_parse_sd_params(m.group(7))["job"], "77?")

            p_jrn = harness.run_guard(env, [f"--syslog=journal+unix://{path_jrn}"])
            self.assertEqual(p_jrn.returncode, 0, p_jrn.stderr)
            fields = _parse_journal_datagram(sock_jrn.recv(65536))
            self.assertEqual(fields["SLURM_JOB_ID"], "77?")
        finally:
            sock_rfc.close()
            sock_jrn.close()

    def test_unicode_whitespace_trimmed_for_syslog_target_and_pen(self):
        env = self._cpu_env(ZEN4, "zen2", {"EESSI_ARCH_GUARD_SYSLOG_PEN": "\u00a042\u3000"})
        proc = harness.run_guard(env, ["--dry-run", "--json", "--syslog=\u00a0journald\u3000"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        doc = json.loads(proc.stdout)
        self.assertEqual(doc["syslog"]["target"], "journal+unix:///run/systemd/journal/socket")
        self.assertEqual(doc["syslog"]["pen"], "42")

    def test_invalid_syslog_target_and_config_pen_warn_and_keep_defaults(self):
        env = self._cpu_env(ZEN2, "zen2")
        cli_proc = harness.run_guard(env, ["--dry-run", "--json", "--syslog=bad://x"])
        self.assertEqual(cli_proc.returncode, 0, cli_proc.stderr)
        self.assertEqual(
            cli_proc.stderr,
            "eessi-arch-guard: --syslog is not a valid syslog target: bad://x\n",
        )
        self.assertIsNone(json.loads(cli_proc.stdout)["syslog"]["target"])

        env_bad = dict(env)
        env_bad["EESSI_ARCH_GUARD_SYSLOG"] = "bad://y"
        env_proc = harness.run_guard(env_bad, ["--dry-run", "--json"])
        self.assertEqual(env_proc.returncode, 0, env_proc.stderr)
        self.assertEqual(
            env_proc.stderr,
            "eessi-arch-guard: EESSI_ARCH_GUARD_SYSLOG is not a valid syslog target: bad://y\n",
        )
        self.assertIsNone(json.loads(env_proc.stdout)["syslog"]["target"])

        conf = harness.PROGRAM.with_name("eessi-arch-guard.conf")
        conf.write_text("syslog_pen = bogus\n", encoding="utf-8")
        subprocess.check_call(["sudo", "chown", "root:root", str(conf)])
        subprocess.check_call(["sudo", "chmod", "644", str(conf)])
        try:
            pen_proc = harness.run_guard(env, ["--dry-run", "--json"])
            self.assertEqual(pen_proc.returncode, 0, pen_proc.stderr)
            self.assertEqual(
                pen_proc.stderr,
                "eessi-arch-guard: config syslog_pen is not a positive integer: bogus\n",
            )
            self.assertEqual(json.loads(pen_proc.stdout)["syslog"]["pen"], "32473")
        finally:
            subprocess.check_call(["sudo", "rm", "-f", str(conf)])

    def test_invalid_syslog_target_and_pen_emit_separate_ordered_warnings(self):
        env = self._cpu_env(ZEN2, "zen2", {"EESSI_ARCH_GUARD_SYSLOG_PEN": "not-int"})
        proc = harness.run_guard(env, ["--dry-run", "--json", "--syslog=bad-target"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stderr,
            "eessi-arch-guard: --syslog is not a valid syslog target: bad-target\n"
            "eessi-arch-guard: EESSI_ARCH_GUARD_SYSLOG_PEN is not a positive integer: not-int\n",
        )


if __name__ == "__main__":
    unittest.main()
