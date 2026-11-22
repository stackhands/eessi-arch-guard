"""The two guards must read a cpuinfo header and field as Python reads them.

Python limits a cpuinfo to the processors a task may use. It strips each
line with str.strip(), splits a field name and a flag list with str.split(),
and reads the number on a processor line with a regular expression whose
digit class takes a decimal digit of any script. All three work on Unicode
whitespace and Unicode digits. The Zig guard cut ASCII whitespace and
read ASCII digits, so a processor line or a flags line that Python reads
was skipped there, the processor was left out of the task or the flags
were not intersected, and the guard saw a richer CPU than Python did.

Each text is run on a cpuinfo whose processor 0 lacks the flags of the
others. A header Python reads as processor 1 makes the guard choose an
Icelake directory when the task is bound to processor 1, and a flags line
Python reads makes the intersection keep the flags of the others. Python
alone is checked for choosing that directory, and then the two programs
are compared, each on a site of its own. The comparison skips when
TOOL_BIN is unset.

The whitespace characters at which str.splitlines also ends a line, such as
a vertical tab, a form feed, NEL and the line separator, are left out. Both
guards split the file at them before a line is read, so a line holding one
becomes two lines, and the case would test where a line ends rather than
how a line is read. test_cpuinfo_line_break_parity covers those characters.
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path
from typing import Dict, Iterator, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import differential  # noqa: E402
import harness  # noqa: E402

LOADED = "x86_64/amd/zen4"
CPUS = ("x86_64/intel/icelake", "x86_64/generic", LOADED)
FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "cpuinfo"
    / "emeraldrapids-kvm-no-waitpkg.cpuinfo"
)
ICELAKE = "x86_64/intel/icelake"
POOR_FLAGS = "fpu vme de pse tsc msr pae"

SPACES = {
    "NBSP": "\u00a0",
    "OGHAM": "\u1680",
    "EN": "\u2003",
    "NNBSP": "\u202f",
    "MMSP": "\u205f",
    "IDEOGRAPHIC": "\u3000",
    "US": "\x1f",
}


def _blocks() -> Sequence[str]:
    parts = re.split(r"(?m)^(?=processor\t: )", FIXTURE.read_text(encoding="utf-8"))
    return [part for part in parts if part.strip()]


def _flags_of(block: str) -> str:
    match = re.search(r"(?m)^flags\t\t: (.*)$", block)
    assert match is not None
    return match.group(1)


def _header(line: str) -> Tuple[str, Dict[str, str]]:
    """A cpuinfo whose processor 1 has this header and a task bound to it."""
    blocks = list(_blocks())
    flags = re.compile(r"(?m)^flags\t\t: .*$")
    blocks[0] = flags.sub("flags\t\t: " + POOR_FLAGS, blocks[0])
    blocks[1] = blocks[1].replace("processor\t: 1", line, 1)
    return "".join(blocks), {"SLURM_CPU_BIND_LIST": "1"}


def _flags(key: str, edit) -> Tuple[str, Dict[str, str]]:
    """A cpuinfo whose processors 0 and 1 share the flags under another key."""
    blocks = list(_blocks())
    rich = _flags_of(blocks[0])
    flags = re.compile(r"(?m)^flags\t\t: .*$")
    blocks[0] = flags.sub(lambda _m: key + ": " + edit(rich), blocks[0])
    blocks[1] = flags.sub(lambda _m: "flags\t\t: " + rich + " zzextra", blocks[1])
    return "".join(blocks), {"SLURM_CPU_BIND_LIST": "0,1"}


def _read() -> Iterator[Tuple[str, Tuple[str, Dict[str, str]]]]:
    for name, space in SPACES.items():
        yield "header after " + name, _header("processor\t: 1" + space)
        yield "header before " + name, _header(space + "processor\t: 1")
        yield "flags joined by " + name, _flags("flags\t\t", lambda r, s=space: r.replace(" ", s))
        yield "flags value after " + name, _flags("flags\t\t", lambda r, s=space: r + s)
        yield "flags value before " + name, _flags("flags\t\t", lambda r, s=space: s + r)
        yield "flags key after " + name, _flags("flags" + space, lambda r: r)
        yield "flags key before " + name, _flags(space + "flags\t\t", lambda r: r)
    yield "Arabic-Indic digit", _header("processor\t: \u0661")
    yield "fullwidth digit", _header("processor\t: \uff11")
    yield "mathematical digit", _header("processor\t: \U0001d7cf")
    yield "padded digits", _header("processor\t: 0001")
    yield "upper case header", _header("PROCESSOR\t: 1")
    yield "upper case flags", _flags("FLAGS\t\t", lambda r: r)


def _ignored() -> Iterator[Tuple[str, Tuple[str, Dict[str, str]]]]:
    """Headers Python does not read, as it allows only a space or a tab there."""
    for name, space in SPACES.items():
        yield "header at the colon " + name, _header("processor" + space + ": 1")
        yield "header after the colon " + name, _header("processor\t:" + space + "1")


class CpuinfoTextTests(unittest.TestCase):
    def _run(
        self, program: Sequence[str], case: Tuple[str, Dict[str, str]]
    ) -> Tuple[int, str, str]:
        text, extra = case

        def build(site: harness.Site) -> Dict[str, str]:
            for cpu in CPUS:
                site.add_cpu(cpu)
                site.add_module(cpu, "R/4.4.2")
            with open(site.cpuinfo, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
            env = harness.base_env(site, LOADED, ["R/4.4.2"], gpu="none")
            env.update(extra)
            return env

        return differential.run_fresh(program, build, ["--json"]).observed()

    def _python(self) -> Sequence[str]:
        return differential.python_guard()

    def test_python_keeps_the_flags_of_a_header_it_reads(self) -> None:
        plain, _bound = _header("processor\t: 1")
        control = self._run(self._python(), (plain, {"SLURM_CPU_BIND_LIST": "0,1"}))
        self.assertNotEqual(json.loads(control[1])["now"], ICELAKE)
        for name, case in _read():
            with self.subTest(case=name):
                code, out, err = self._run(self._python(), case)
                self.assertEqual(code, 0, err)
                self.assertEqual(json.loads(out)["now"], ICELAKE)

    def test_python_leaves_out_a_header_it_does_not_read(self) -> None:
        for name, case in _ignored():
            with self.subTest(case=name):
                code, out, err = self._run(self._python(), case)
                self.assertEqual(code, 0, err)
                self.assertNotEqual(json.loads(out)["now"], ICELAKE)

    def test_programs_agree(self) -> None:
        harness.skip_unless_tool_bin(self)
        for name, case in [*_read(), *_ignored()]:
            with self.subTest(case=name):
                self.assertEqual(
                    self._run(differential.zig_program(), case),
                    self._run(self._python(), case),
                )


if __name__ == "__main__":
    unittest.main()
