"""`--restore` stderr matches CPython 3.14 on the committed decode vectors.

The vectors were produced by Python 3.14: ``decode('utf-8')`` then
``json.loads``, and the same bytes fed to the Python guard. This test runs
the Python guard again when this interpreter reports the stored exception,
then runs ``TOOL_BIN`` against every vector. A Python older than 3.13 words
a trailing comma differently, so a live exception that is not the stored one
does not fail the Python half. The binary is always compared with the stored
bytes. ``TOOL_BIN`` unset skips only that half, after the Python assertions.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

import harness

ROOT = Path(__file__).resolve().parents[1]
VECTORS = ROOT / "zig" / "testdata" / "restore_decode_vectors.json"
ENV = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "LANG": "C"}
REQUIRED = (
    "empty",
    "truncated-object",
    "truncated-array",
    "bad-escape",
    "short-u-escape",
    "bad-u-escape",
    "invalid-utf8-start",
    "invalid-utf8-continuation",
    "utf8-unexpected-end",
    "utf8-overlong",
    "utf8-surrogate",
    "utf8-too-large",
    "trailing-comma-array",
    "trailing-comma-object",
    "control-in-string",
    "control-raw",
    "lone-surrogate-string",
    "lone-surrogate-object",
    "array-depth-31",
    "array-depth-32",
    "array-depth-33",
    "object-depth-31",
    "object-depth-32",
    "object-depth-33",
    "extra-data",
    "nan",
    "infinity",
    "neg-infinity",
    "nested-nan",
    "bom",
    "not-object-array",
    "leading-zero",
    "multibyte-value",
    "multibyte-escape",
)


def _not_json(name):
    raise ValueError("%s is not JSON" % name)


def exception_text(raw):
    """The exception ``json.loads`` raises after a strict utf-8 decode, or None."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return str(exc)
    try:
        json.loads(text, parse_constant=_not_json)
    except Exception as exc:
        return str(exc)
    return None


class RestoreDecodeParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vectors = json.loads(VECTORS.read_text(encoding="utf-8"))

    def test_vectors_match_the_guard_and_the_binary(self):
        self.assertGreaterEqual(len(self.vectors), 40)
        self.assertGreaterEqual(sum(1 for vector in self.vectors if vector["code"] == 1), 30)
        names = {vector["name"] for vector in self.vectors}
        for name in REQUIRED:
            self.assertIn(name, names)
        for depth in (31, 32, 33):
            array = next(
                vector for vector in self.vectors if vector["name"] == "array-depth-%d" % depth
            )
            obj = next(
                vector for vector in self.vectors if vector["name"] == "object-depth-%d" % depth
            )
            self.assertEqual(array["code"], 1, array["name"])
            self.assertIn("'list' object has no attribute 'get'", array["stderr"])
            self.assertEqual(obj["code"], 0, obj["name"])
            self.assertEqual(obj["stderr"], "")
            self.assertIn("restore request is malformed", obj["stdout"])
        for vector in self.vectors:
            raw = bytes.fromhex(vector["input_hex"])
            self.check_python(vector, raw)
        harness.skip_unless_tool_bin(self)
        for vector in self.vectors:
            raw = bytes.fromhex(vector["input_hex"])
            self.check_binary(vector, raw)

    def check_python(self, vector, raw):
        if exception_text(raw) != vector["exc"]:
            return
        proc = subprocess.run(
            [sys.executable, "-I", str(harness.GUARD), "--restore"],
            input=raw,
            capture_output=True,
            env=ENV,
            check=False,
        )
        self.assertEqual(proc.returncode, vector["code"], vector["name"])
        self.assertEqual(proc.stdout.decode("utf-8"), vector["stdout"], vector["name"])
        self.assertEqual(proc.stderr.decode("utf-8"), vector["stderr"], vector["name"])

    def check_binary(self, vector, raw):
        proc = subprocess.run(
            [harness.TOOL_BIN, "--restore"],
            input=raw,
            capture_output=True,
            env=ENV,
            check=False,
        )
        self.assertEqual(proc.returncode, vector["code"], vector["name"])
        self.assertEqual(proc.stdout.decode("utf-8"), vector["stdout"], vector["name"])
        self.assertEqual(proc.stderr.decode("utf-8"), vector["stderr"], vector["name"])
