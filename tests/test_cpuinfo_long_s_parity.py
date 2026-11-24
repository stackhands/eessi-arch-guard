"""The two guards must match the words processor and flags as Python does.

Python reads the processor lines and the flags lines of a cpuinfo with
regular expressions that ignore case, and the Unicode form of that match
folds U+017F, the long s, to an s. A line that starts with proce, two long
s letters and or is a processor line to Python, and one that starts with
flag and a long s is a flags line. The Zig guard ignored ASCII case only,
so it skipped both.

A flags line that Zig skipped left it without the waitpkg flag, so it
turned away a candidate directory that Python accepted. A processor line
that Zig skipped left its processor out of the task, as when the number
on it is written with another script.

Python also reads a processor line as a field of its block, and drops
only the field named processor. str.lower() keeps the long s, so a header
spelled with one stays a field, and two processors alike but for that
header differ. Python then writes a scoped copy where it would write none
for alike processors. With the file size limit at zero no temporary
directory takes the copy, which makes the difference show in the output.

Python alone is checked for choosing the directory its reading gives, and
then the two programs are compared, each on a site of its own. The
comparison skips when TOOL_BIN is unset.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Dict, Iterator, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import differential  # noqa: E402
import harness  # noqa: E402
from test_cpuinfo_python_text_parity import CPUS, ICELAKE, _blocks, _header  # noqa: E402

SPR = "x86_64/intel/sapphirerapids"
NO_TEMP_DIR = "No usable temporary directory found in ["
FLAGS = "avx2 fma avx512f avx512bw avx512cd avx512dq avx512vl avx512_bf16 amx_tile"


def _flags(key: str, flags: str) -> Tuple[str, Dict[str, str]]:
    """A cpuinfo whose only flags line has this key, and that offers SPR first."""
    text = (
        "vendor_id : GenuineIntel\n"
        + key
        + "\t: "
        + flags
        + "\neessi_arch_matches: "
        + SPR
        + ":"
        + ICELAKE
        + "\n"
    )
    return text, {}


def _read() -> Iterator[Tuple[str, Tuple[str, Dict[str, str]], str]]:
    for key in ("flags", "FLAGS", "flag\u017f", "FLAG\u017f", "Flag\u017f"):
        yield "waitpkg under " + ascii(key), _flags(key, FLAGS + " waitpkg"), SPR
    yield "processor with two long s", _header("proce\u017f\u017for\t: 1"), ICELAKE
    yield "processor with one long s", _header("proce\u017fsor\t: 1"), ICELAKE
    yield "upper case processor with a long s", _header("PROCE\u017fSOR\t: 1"), ICELAKE


def _twins(header: str) -> Tuple[str, Dict[str, str]]:
    """Two processors alike but for the header of the second, both in the task."""
    first = _blocks()[0]
    assert first.startswith("processor\t: 0\n")
    return first + first.replace("processor\t: 0", header, 1), {"SLURM_CPU_BIND_LIST": "0,1"}


def _build(case: Tuple[str, Dict[str, str]]) -> differential.Build:
    text, extra = case

    def build(site: harness.Site) -> Dict[str, str]:
        for cpu in (*CPUS, SPR):
            site.add_cpu(cpu)
            site.add_module(cpu, "R/4.4.2")
        with open(site.cpuinfo, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        loaded = "x86_64/generic"
        env = harness.base_env(site, loaded, ["R/4.4.2"], gpu="none")
        env.update(extra)
        return env

    return build


class LongSTests(unittest.TestCase):
    def _run(
        self, program: Sequence[str], case: Tuple[str, Dict[str, str]]
    ) -> Tuple[int, str, str]:
        return differential.run_fresh(program, _build(case), ["--json"]).observed()

    def _python(self) -> Sequence[str]:
        return differential.python_guard()

    def test_python_reads_the_words_with_a_long_s(self) -> None:
        for flags in ("flags", "flag\u017f"):
            with self.subTest(control=flags):
                code, out, err = self._run(self._python(), _flags(flags, FLAGS))
                self.assertEqual(code, 0, err)
                self.assertEqual(json.loads(out)["now"], ICELAKE)
        for name, case, chosen in _read():
            with self.subTest(case=name):
                code, out, err = self._run(self._python(), case)
                self.assertEqual(code, 0, err)
                self.assertEqual(json.loads(out)["now"], chosen)

    def test_programs_agree(self) -> None:
        harness.skip_unless_tool_bin(self)
        for name, case, _chosen in _read():
            with self.subTest(case=name):
                self.assertEqual(
                    self._run(differential.zig_program(), case),
                    self._run(self._python(), case),
                )

    def test_python_keeps_a_long_s_header_as_a_field(self) -> None:
        # The header line is a field too. Its name keeps the long s, so the
        # two processors differ and the guard writes a scoped copy, which
        # no temporary directory takes when no file may grow.
        code, out, err = differential.run_fresh(
            self._python(), _build(_twins("proce\u017f\u017for\t: 1")), ["--json"], 0
        ).observed()
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual((report["decision"], report["cause"]), ("failed", "FileNotFoundError"))
        self.assertIn(NO_TEMP_DIR, report["message"])
        code, out, err = differential.run_fresh(
            self._python(), _build(_twins("processor\t: 1")), ["--json"], 0
        ).observed()
        # Alike processors need no copy, so the run gets as far as the
        # detection, which then fails on a write of its own.
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["cause"], "archdetect-failed", out)

    def test_programs_agree_on_a_long_s_header_field(self) -> None:
        harness.skip_unless_tool_bin(self)
        for header in ("proce\u017f\u017for\t: 1", "PROCE\u017fSOR\t: 1", "processor\t: 1"):
            for mode in ((), ("--json",)):
                with self.subTest(header=ascii(header), mode=" ".join(mode)):
                    differential.assert_agree(self, _build(_twins(header)), mode, 0)


if __name__ == "__main__":
    unittest.main()
