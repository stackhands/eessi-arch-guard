"""The lower-case table of the Zig guard must be what str.lower() does.

The Zig guard lower-cases a cpuinfo field name with the tables in
zig/pylower_table.zig, which tools/gen_pylower.py writes from the Python
interpreter. These tests read the file back and check it against the
interpreter over every code point, so that a table that has fallen behind
the interpreter, or that was edited by hand, fails here. They also run a
port of the Zig lookup over the table: each code point alone, and each next
to a capital sigma, which is the one letter whose lower case depends on its
neighbours.

The tables belong to Unicode 13.0.0, the version of Python 3.9, which the
file names. On that version every code point is checked. An interpreter with
a newer Unicode assigns more code points, and lower-cases some of those, so
there the checks cover the code points that both versions assign, and only
the comparison with the generator's output is skipped. Beside a capital
sigma, the code points in SIGMA_CLASS_CHANGED are expected to differ there,
and no others. The Zig side is covered by the tests in
zig/pylower.zig, which compare the lookup with the table, and by the
differential tests of the field names.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import unicodedata
import unittest
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
TABLE = ROOT / "zig" / "pylower_table.zig"
GENERATOR = ROOT / "tools" / "gen_pylower.py"
SIGMA = "\u03a3"
FINAL_SIGMA = "\u03c2"
MEDIAL_SIGMA = "\u03c3"

Ranges = List[Tuple[int, int]]

# Code points that Unicode 13.0.0 assigns and a later version took out of the
# case-ignorable class or put into it, with that version. A capital sigma
# beside one of them lower-cases another way on a Python with that Unicode
# or a later one, while the Zig guard keeps Unicode 13.0.0. COMPAT.md lists
# them. The sigma test expects exactly these to differ, so an entry that no
# longer differs fails as a new difference does.
SIGMA_CLASS_CHANGED: Dict[int, str] = {
    0x1734: "14.0.0",  # HANUNOO SIGN PAMUDPOD: Mn in 13.0.0, Mc from 14.0.0 on
    0x1171E: "16.0.0",  # AHOM CONSONANT SIGN MEDIAL RA: Mn in 13.0.0, Mc from 16.0.0 on
}


def load_generator():
    spec = importlib.util.spec_from_file_location("gen_pylower", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_table() -> Tuple[str, Dict[int, str], Ranges, Ranges, Ranges]:
    """The version, the lower-case map, the two sigma classes, and the
    assigned code points in the file."""
    source = TABLE.read_text(encoding="utf-8")
    version = re.search(r'pub const unicode_version = "([^"]+)";', source)
    assert version is not None, "the table names no Unicode version"
    lower: Dict[int, str] = {}
    run = re.compile(
        r"\.first = 0x([0-9A-F]+), \.last = 0x([0-9A-F]+), "
        r"\.stride = ([12]), \.delta = (-?[0-9]+)"
    )
    for first, last, stride, delta in run.findall(source):
        for cp in range(int(first, 16), int(last, 16) + 1, int(stride)):
            assert cp not in lower, "U+%04X is in two runs" % cp
            lower[cp] = chr(cp + int(delta))
    special = re.compile(r"\.cp = 0x([0-9A-F]+), \.to = \.\{ 0x([0-9A-F]+), 0x([0-9A-F]+) \}")
    for cp, first, second in special.findall(source):
        assert int(cp, 16) not in lower, "U+%s is in a run and special" % cp
        lower[int(cp, 16)] = chr(int(first, 16)) + chr(int(second, 16))
    classes: Dict[str, Ranges] = {}
    for name in ("cased", "case_ignorable", "assigned"):
        block = re.search(r"pub const %s = \[_\]\[2\]u21\{(.*?)\};" % name, source, re.S)
        assert block is not None, "the table has no %s ranges" % name
        classes[name] = [
            (int(a, 16), int(b, 16))
            for a, b in re.findall(r"\.\{ 0x([0-9A-F]+), 0x([0-9A-F]+) \}", block.group(1))
        ]
    return (
        version.group(1),
        lower,
        classes["cased"],
        classes["case_ignorable"],
        classes["assigned"],
    )


def in_ranges(ranges: Ranges, cp: int) -> bool:
    low, high = 0, len(ranges)
    while low < high:
        mid = (low + high) // 2
        first, last = ranges[mid]
        if cp < first:
            high = mid
        elif cp > last:
            low = mid + 1
        else:
            return True
    return False


class Lowerer:
    """The Zig lookup, over the tables of the file."""

    def __init__(self) -> None:
        _version, self.lower, self.cased, self.ignorable, _assigned = read_table()

    def final_sigma(self, text: str, index: int) -> bool:
        before = index - 1
        while before >= 0 and in_ranges(self.ignorable, ord(text[before])):
            before -= 1
        if before < 0 or not in_ranges(self.cased, ord(text[before])):
            return False
        after = index + 1
        while after < len(text) and in_ranges(self.ignorable, ord(text[after])):
            after += 1
        return after == len(text) or not in_ranges(self.cased, ord(text[after]))

    def __call__(self, text: str) -> str:
        out = []
        for index, ch in enumerate(text):
            cp = ord(ch)
            if cp < 0x80:
                out.append(ch.lower())
            elif ch == SIGMA:
                out.append(FINAL_SIGMA if self.final_sigma(text, index) else MEDIAL_SIGMA)
            else:
                out.append(self.lower.get(cp, ch))
        return "".join(out)


def version_key(version: str) -> Tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def sigma_class_changed() -> List[int]:
    """The entries of SIGMA_CLASS_CHANGED that this Python's Unicode has."""
    version, *_rest = read_table()
    if version == unicodedata.unidata_version:
        return []
    have = version_key(unicodedata.unidata_version)
    return sorted(cp for cp, since in SIGMA_CLASS_CHANGED.items() if version_key(since) <= have)


def every_code_point() -> List[int]:
    return [cp for cp in range(0x110000) if not 0xD800 <= cp <= 0xDFFF]


def checked_code_points() -> List[int]:
    """Every code point when this Python has the table's Unicode, else the
    code points that both versions assign."""
    version, _lower, _cased, _ignorable, assigned = read_table()
    if version == unicodedata.unidata_version:
        return every_code_point()
    return [
        cp
        for cp in every_code_point()
        if in_ranges(assigned, cp) and unicodedata.category(chr(cp)) != "Cn"
    ]


class PyLowerTableTests(unittest.TestCase):
    def setUp(self) -> None:
        if not TABLE.is_file() or not GENERATOR.is_file():
            self.skipTest("the Zig table and its generator are not in this tree")

    def test_table_is_the_unicode_of_the_oldest_supported_python(self) -> None:
        version, *_rest = read_table()
        self.assertEqual(version, load_generator().UNICODE_VERSION)
        if sys.version_info[:2] == (3, 9):
            self.assertEqual(unicodedata.unidata_version, version)

    def test_a_newer_python_shares_the_assigned_code_points(self) -> None:
        version, *_rest = read_table()
        if version == unicodedata.unidata_version:
            self.skipTest("this Python has the table's Unicode %s" % version)
        # Unicode 13.0.0 assigns more than 280,000, private use ones among them.
        self.assertGreater(len(checked_code_points()), 280000)

    def test_table_is_what_the_generator_reads_off_this_python(self) -> None:
        version, lower, cased, ignorable, assigned = read_table()
        if version != unicodedata.unidata_version:
            self.skipTest(
                "the table is Unicode %s and this Python has %s"
                % (version, unicodedata.unidata_version)
            )
        data = load_generator().tables()
        self.assertEqual(version, data["unicode_version"])
        expected = {cp: "".join(chr(c) for c in to) for cp, to in data["lower_special"]}
        for first, last, stride, delta in data["lower_runs"]:
            for cp in range(first, last + 1, stride):
                expected[cp] = chr(cp + delta)
        self.assertEqual(lower, expected)
        self.assertEqual(cased, data["cased"])
        self.assertEqual(ignorable, data["case_ignorable"])
        self.assertEqual(assigned, data["assigned"])

    def test_every_code_point_lower_cases_as_str_lower_does(self) -> None:
        lowerer = Lowerer()
        wrong = []
        for cp in checked_code_points():
            ch = chr(cp)
            if cp == 0x3A3:
                continue
            if lowerer(ch) != ch.lower():
                wrong.append("U+%04X" % cp)
        self.assertEqual(wrong, [])

    def test_the_only_non_ascii_code_point_with_an_ascii_lower_case_is_the_kelvin_sign(
        self,
    ) -> None:
        got = [cp for cp in every_code_point() if cp >= 0x80 and chr(cp).lower().isascii()]
        self.assertEqual(got, [0x212A])
        self.assertEqual(Lowerer()("\u212a"), "k")
        self.assertEqual(Lowerer()("\u0130"), "i\u0307")
        self.assertEqual(Lowerer()("\u017f"), "\u017f")

    def test_a_capital_sigma_beside_every_code_point_lower_cases_as_str_lower_does(self) -> None:
        lowerer = Lowerer()
        wrong = []
        for cp in checked_code_points():
            ch = chr(cp)
            for text in (
                "a" + ch + SIGMA,
                ch + SIGMA,
                SIGMA + ch + "a",
                "a" + SIGMA + ch,
            ):
                if lowerer(text) != text.lower():
                    wrong.append(cp)
                    break
        # On the table's own Unicode no code point may differ.
        self.assertEqual(
            ["U+%04X" % cp for cp in wrong[:20]],
            ["U+%04X" % cp for cp in sigma_class_changed()],
        )

    def test_sigma_strings_lower_case_as_str_lower_does(self) -> None:
        lowerer = Lowerer()
        for text in (
            "\u0391\u03a3",
            "\u03a3",
            "\u03a3\u0391",
            "\u0391\u03a3\u0391",
            "\u0391.\u03a3",
            "\u0391_\u03a3",
            "\u0391\u03a3\u03a3",
            "\u0391\u03a3\u03a3\u0391",
            "\u0391\u0345\u03a3",
            "\u0391\u03a3\u0345\u0391",
        ):
            with self.subTest(text=text):
                self.assertEqual(lowerer(text), text.lower())


if __name__ == "__main__":
    sys.exit(unittest.main())
