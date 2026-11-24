"""The two guards must read a Slurm CPU bind value as Python does.

Python strips the bind value with str.strip(), which removes every Unicode
whitespace character, and reads each mask of a mask_cpu list with
int(piece, 16). int() takes a decimal digit of any script as that digit,
one underscore after the prefix and between digits, and whitespace after
the digits. The Zig guard stripped ASCII whitespace and took ASCII hex
digits only, so a bind value Python reads as the processor 1 was ignored
there, and the task was matched to a CPU with every processor in view.

Each value is run on a cpuinfo whose processor 0 lacks the flags of the
others, so reading the value as processor 1 makes the guard choose an Icelake
directory and ignoring it does not. Python alone is checked for choosing it
on the values it reads and for ignoring the rest, and then the two programs
are compared, each on a site of its own. The comparison skips when TOOL_BIN is
unset.
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
POOR_FLAGS = "flags\t\t: fpu vme de pse tsc msr pae"

SPACES = {
    "VT": "\x0b",
    "FF": "\x0c",
    "FS": "\x1c",
    "GS": "\x1d",
    "RS": "\x1e",
    "US": "\x1f",
    "NEL": "\u0085",
    "NBSP": "\u00a0",
    "OGHAM": "\u1680",
    "EN": "\u2003",
    "LS": "\u2028",
    "PS": "\u2029",
    "NNBSP": "\u202f",
    "MMSP": "\u205f",
    "IDEOGRAPHIC": "\u3000",
}
# str.strip() removes U+001C to U+001F and int() does not skip them.
ASCII_CONTROL = ("FS", "GS", "RS", "US")


def _cpuinfo() -> str:
    """The fixture with a processor 0 that lacks the flags of the others."""
    parts = re.split(r"(?m)^(?=processor\t: )", FIXTURE.read_text(encoding="utf-8"))
    blocks = [part for part in parts if part.strip()]
    first = re.sub(r"(?m)^flags\t\t: .*$", POOR_FLAGS, blocks[0])
    return first + "".join(blocks[1:])


def _read() -> Iterator[Tuple[str, Dict[str, str]]]:
    """Bind values Python reads as the processor 1."""
    for name, space in SPACES.items():
        yield "list before " + name, {"SLURM_CPU_BIND_LIST": space + "1"}
        yield "list after " + name, {"SLURM_CPU_BIND_LIST": "1" + space}
        yield "verbose after " + name, {"SLURM_CPU_BIND": "quiet,map_cpu:1" + space}
        if name not in ASCII_CONTROL:
            yield "mask before comma " + name, {"SLURM_CPU_BIND_LIST": "0x2" + space + ",0x2"}
    yield "mask after a space and a tab", {"SLURM_CPU_BIND_LIST": "0x2 \t,0x2"}
    yield "underscore after the prefix", {"SLURM_CPU_BIND_LIST": "0x_2"}
    yield "underscore between digits", {"SLURM_CPU_BIND_LIST": "0x0_2"}
    yield "Arabic-Indic digit", {"SLURM_CPU_BIND_LIST": "0x\u0662"}
    yield "fullwidth digit", {"SLURM_CPU_BIND_LIST": "0x\uff12"}
    yield "mathematical digit", {"SLURM_CPU_BIND_LIST": "0x\U0001d7d0"}


def _refused() -> Iterator[Tuple[str, Dict[str, str]]]:
    """Bind values Python does not read as a mask, so the value is ignored."""
    for name in ASCII_CONTROL:
        yield "control before comma " + name, {"SLURM_CPU_BIND_LIST": "0x2" + SPACES[name] + ",0x2"}
    yield "control after a non-ASCII digit", {"SLURM_CPU_BIND_LIST": "0x\u0662\x1f,0x2"}
    for text in ("0x2_", "0x__2", "0x_", "0x2__0", "0x 2", "0x\u00a02", "0x\udcff2"):
        yield "refused " + ascii(text), {"SLURM_CPU_BIND_LIST": text}


class CpuBindTextTests(unittest.TestCase):
    def _run(self, program: Sequence[str], extra: Dict[str, str]) -> Tuple[int, str, str]:
        def build(site: harness.Site) -> Dict[str, str]:
            for cpu in CPUS:
                site.add_cpu(cpu)
                site.add_module(cpu, "R/4.4.2")
            Path(site.cpuinfo).write_text(_cpuinfo(), encoding="utf-8")
            env = harness.base_env(site, LOADED, ["R/4.4.2"], gpu="none")
            env.update(extra)
            return env

        return differential.run_fresh(program, build, ["--json"]).observed()

    def _python(self) -> Sequence[str]:
        return differential.python_guard()

    def test_python_reads_the_values_and_ignores_the_rest(self) -> None:
        ignored = json.loads(self._run(self._python(), {"SLURM_CPU_BIND_LIST": "zz"})[1])
        self.assertNotEqual(ignored["now"], ICELAKE)
        for name, extra in _read():
            with self.subTest(case=name):
                code, out, err = self._run(self._python(), extra)
                self.assertEqual(code, 0, err)
                self.assertEqual(json.loads(out)["now"], ICELAKE)
        for name, extra in _refused():
            with self.subTest(case=name):
                code, out, err = self._run(self._python(), extra)
                self.assertEqual(code, 0, err)
                self.assertEqual(json.loads(out)["now"], ignored["now"])

    def test_programs_agree(self) -> None:
        harness.skip_unless_tool_bin(self)
        for name, extra in [*_read(), *_refused()]:
            with self.subTest(case=name):
                self.assertEqual(
                    self._run(differential.zig_program(), extra),
                    self._run(self._python(), extra),
                )


if __name__ == "__main__":
    unittest.main()
