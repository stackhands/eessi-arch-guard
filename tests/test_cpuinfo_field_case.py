"""A cpuinfo field is told from another by its lower-cased name.

The binary writes a scoped cpuinfo copy only when the processors the task may
use do not all describe the same CPU. Two processors describe the same CPU
when their fields, taken by lower-cased name, hold the same first values. A
copy that is not needed is not written, so the case where a field is spelled
with another case on another processor decides whether the binary goes on to
the CPU detection or tries to place a copy.

The cases run on a fresh site under a file-size limit of zero. Placing the
copy then fails with a missing temporary directory, and the detection fails
on its own file. The cause in the decision says which of the two happened. A
field that two processors spell alike in the lower-cased name, such as
``key``, ``Key`` and ``KEY``, needs no copy. A field that only one processor
has, or that holds another value, needs one. The cause of each case is
asserted, and the whole output of each run in each output mode is pinned in
tests/golden/cpuinfo-field-case.txt and tests/golden/cpuinfo-field-values.txt.

The names are lower-cased as full Unicode lower-casing does it. U+212A, the
Kelvin sign, has the lower case ``k``, so a processor that spells the name
with it and one that spells it ``key`` have the same field. U+0130 gives ``i``
and U+0307; a capital letter of any other script gives its small letter; and a
capital sigma gives U+03C2 at the end of a word and U+03C3 elsewhere. U+017F,
the long s, is a small letter already and stays as it is, so it is not an s
here.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from typing import Callable, Dict, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import differential  # noqa: E402
import golden  # noqa: E402
import harness  # noqa: E402

CPU = "x86_64/intel/sapphirerapids"
FALLBACK = "x86_64/intel/icelake"
NO_COPY = "archdetect-failed"
COPY_FAILED = "FileNotFoundError"
Case = Tuple[str, Sequence[str], str]


def builder(
    names: Sequence[str], value: Callable[[int], str]
) -> Callable[[harness.Site], Dict[str, str]]:
    """A site whose first processors carry one field each, named by ``names``."""

    def build(site: harness.Site) -> Dict[str, str]:
        for cpu in (CPU, FALLBACK):
            site.add_cpu(cpu)
            site.add_module(cpu, "R/4.4.2")
        visible = sorted(os.sched_getaffinity(0))[: len(names)]
        blocks = []
        for index, (cpu_id, name) in enumerate(zip(visible, names)):
            blocks.append(
                "processor\t: %d\nvendor_id\t: GenuineIntel\n%s\t: %s\nflags\t\t: avx2 fma\n"
                % (cpu_id, name, value(index))
            )
        Path(site.cpuinfo).write_text("\n".join(blocks), encoding="utf-8", errors="surrogateescape")
        env = harness.base_env(site, CPU, ["R/4.4.2"], gpu="none")
        env["SLURM_CPU_BIND_LIST"] = ",".join(str(cpu_id) for cpu_id in visible)
        return env

    return build


CASES: Sequence[Case] = (
    ("same name", ("key", "key"), NO_COPY),
    ("title and lower", ("Key", "key"), NO_COPY),
    ("upper and lower", ("KEY", "key"), NO_COPY),
    ("title and upper", ("Key", "KEY"), NO_COPY),
    ("three spellings", ("key", "Key", "KEY"), NO_COPY),
    ("padded name", ("key", "  Key  "), NO_COPY),
    ("other name", ("key", "Kez"), COPY_FAILED),
    ("kelvin sign and lower", ("key", "\u212aey"), NO_COPY),
    ("kelvin sign and title", ("\u212aey", "Key"), NO_COPY),
    ("kelvin sign and upper", ("KEY", "\u212aey"), NO_COPY),
    ("kelvin sign beside spaces", ("\u212aey", " \u212a E Y "), COPY_FAILED),
    ("kelvin sign beside another letter", ("\u212aez", "key"), COPY_FAILED),
    ("dotted capital i and its lower case", ("\u0130d", "i\u0307d"), NO_COPY),
    ("dotted capital i and a plain i", ("\u0130d", "id"), COPY_FAILED),
    ("long s is not an s", ("\u017fize", "size"), COPY_FAILED),
    ("long s and a capital s", ("\u017fize", "SIZE"), COPY_FAILED),
    ("accented capital", ("\u00c9cole", "\u00e9cole"), NO_COPY),
    ("cherokee capital", ("\u13a0x", "\uab70x"), NO_COPY),
    ("georgian mtavruli", ("\u1c90x", "\u10d0x"), NO_COPY),
    ("deseret capital", ("\U00010400x", "\U00010428x"), NO_COPY),
    ("titlecase digraph", ("\u01c5x", "\u01c6x"), NO_COPY),
    ("final sigma", ("\u0391\u03a3", "\u03b1\u03c2"), NO_COPY),
    ("final sigma written as a medial one", ("\u0391\u03a3", "\u03b1\u03c3"), COPY_FAILED),
    ("medial sigma", ("\u0391\u03a3\u0391", "\u03b1\u03c3\u03b1"), NO_COPY),
    ("sigma after a full stop", ("\u0391.\u03a3", "\u03b1.\u03c2"), NO_COPY),
    ("sigma after an underscore", ("\u0391_\u03a3", "\u03b1_\u03c3"), NO_COPY),
    ("ill-formed byte", ("a\udcffb", "a\ufffdb"), NO_COPY),
)


def cause_of(run: differential.Result) -> str:
    return json.loads(run.stdout.splitlines()[0])["cause"]


class CpuinfoFieldCaseTests(unittest.TestCase):
    def setUp(self) -> None:
        if len(os.sched_getaffinity(0)) < 3:
            self.skipTest("the cases need three processors in the affinity set")

    def test_the_binary_places_a_copy_only_for_fields_that_differ(self) -> None:
        sheet = golden.Sheet("cpuinfo-field-case")
        self.addCleanup(sheet.finish)
        for label, names, cause in CASES:
            for mode in ((), ("--json",)):
                with self.subTest(case=label, mode=" ".join(mode)):
                    run = differential.assert_golden(
                        self,
                        sheet,
                        "%s [%s]" % (label, " ".join(mode) or "text"),
                        builder(names, lambda _i: "x"),
                        mode,
                        0,
                    )
                    if mode:
                        self.assertEqual(cause_of(run), cause, run.stdout)

    def test_the_binary_places_a_copy_when_the_values_differ(self) -> None:
        sheet = golden.Sheet("cpuinfo-field-values")
        self.addCleanup(sheet.finish)
        for mode in ((), ("--json",)):
            with self.subTest(mode=" ".join(mode)):
                run = differential.assert_golden(
                    self,
                    sheet,
                    "key and Key with other values [%s]" % (" ".join(mode) or "text"),
                    builder(("key", "Key"), lambda index: "v%d" % index),
                    mode,
                    0,
                )
                if mode:
                    self.assertEqual(cause_of(run), COPY_FAILED, run.stdout)


if __name__ == "__main__":
    unittest.main()
