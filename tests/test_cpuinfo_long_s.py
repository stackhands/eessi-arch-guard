"""The words processor and flags are matched with Unicode case folding.

A cpuinfo line is a processor line or a flags line when its key matches the
word with letters of either case, and the Unicode form of that match folds
U+017F, the long s, to an s. A line that starts with proce, two long s
letters and or is a processor line, and one that starts with flag and a long
s is a flags line.

A flags line the binary skipped would leave it without the waitpkg flag, so
it would turn away a candidate directory that a match with the long s
accepts. A processor line it skipped would leave its processor out of the
task, as when the number on it is written with another script.

A processor line is also a field of its block, and only the field named
processor is dropped. Lower-casing keeps the long s, so a header spelled with
one stays a field, and two processors alike but for that header differ. The
binary then writes a scoped copy where it would write none for alike
processors. With the file size limit at zero no temporary directory takes the
copy, which makes the difference show in the output.

The cases check the directory chosen for each reading, and pin the whole
output of each run in tests/golden/cpuinfo-long-s-read.txt and
tests/golden/cpuinfo-long-s-header.txt.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from typing import Dict, Iterator, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import differential  # noqa: E402
import golden  # noqa: E402
import harness  # noqa: E402
from test_cpuinfo_text import CPUS, ICELAKE, _blocks, _header  # noqa: E402

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
        self,
        sheet: golden.Sheet,
        label: str,
        case: Tuple[str, Dict[str, str]],
        mode: Sequence[str] = ("--json",),
        fsize: Optional[int] = None,
    ) -> differential.Result:
        return differential.assert_golden(self, sheet, label, _build(case), mode, fsize)

    def test_the_binary_reads_the_words_with_a_long_s(self) -> None:
        sheet = golden.Sheet("cpuinfo-long-s-read")
        self.addCleanup(sheet.finish)
        for flags in ("flags", "flag\u017f"):
            with self.subTest(control=flags):
                run = self._run(sheet, "control " + ascii(flags), _flags(flags, FLAGS))
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertEqual(json.loads(run.stdout)["now"], ICELAKE)
        for name, case, chosen in _read():
            with self.subTest(case=name):
                run = self._run(sheet, name, case)
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertEqual(json.loads(run.stdout)["now"], chosen)

    def test_the_binary_keeps_a_long_s_header_as_a_field(self) -> None:
        sheet = golden.Sheet("cpuinfo-long-s-header")
        self.addCleanup(sheet.finish)
        # The header line is a field too. Its name keeps the long s, so the
        # two processors differ and the binary writes a scoped copy, which
        # no temporary directory takes when no file may grow.
        for header in ("proce\u017f\u017for\t: 1", "PROCE\u017fSOR\t: 1"):
            with self.subTest(header=ascii(header)):
                for mode in ((), ("--json",)):
                    run = self._run(
                        sheet,
                        "%s [%s]" % (ascii(header), " ".join(mode) or "text"),
                        _twins(header),
                        mode,
                        0,
                    )
                    if mode:
                        self.assertEqual(run.returncode, 0, run.stderr)
                        report = json.loads(run.stdout)
                        self.assertEqual(
                            (report["decision"], report["cause"]), ("failed", "FileNotFoundError")
                        )
                        self.assertIn(NO_TEMP_DIR, report["message"])
        # Alike processors need no copy, so the run gets as far as the
        # detection, which then fails on a write of its own.
        for mode in ((), ("--json",)):
            run = self._run(
                sheet,
                "plain header [%s]" % (" ".join(mode) or "text"),
                _twins("processor\t: 1"),
                mode,
                0,
            )
            if mode:
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertEqual(json.loads(run.stdout)["cause"], "archdetect-failed", run.stdout)


def setUpModule() -> None:
    """Run from the tests directory: the temporary directory candidates and PWD name it."""
    unittest.addModuleCleanup(os.chdir, os.getcwd())
    os.chdir(Path(__file__).resolve().parent)


if __name__ == "__main__":
    unittest.main()
