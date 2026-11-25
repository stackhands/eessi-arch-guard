"""The test helpers decode program output as UTF-8 whatever the locale.

``harness.run_with_timeout`` and the other helpers pass ``encoding="utf-8"``
to subprocess. ``text=True`` would decode with the codec of the process
locale at the time of the call, and a test that leaves the locale at "C"
makes that ASCII for every later test: the UTF-8 quotes of a gcc warning
then raise UnicodeDecodeError. The decode stays strict, so output that is
not UTF-8 still raises, as ``test_raw_bytes_parity`` relies on.

Python turns its UTF-8 mode on when it starts in the C locale, as under
``LANG=C LC_ALL=C``, and then decodes ``text=True`` output as UTF-8 too.
One case therefore runs the helper in an interpreter with UTF-8 mode off,
so the check holds in either locale the suite runs in.
"""

from __future__ import annotations

import json
import locale
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402
import test_unit  # noqa: E402

TESTS = Path(__file__).resolve().parent
RUN_TIMEOUT = 20.0
TEXT = "\u2018fs_fail.c\u2019 caf\u00e9 \u2192 x86_64/amd/zen4"
EMIT_TEXT = "import sys; sys.stdout.buffer.write(%r)" % TEXT.encode("utf-8")
EMIT_STRAY = "import sys; sys.stdout.buffer.write(b'a\\xffb')"
CHILD = """
import codecs, json, locale, sys
sys.path.insert(0, %(tests)r)
import harness
locale.setlocale(locale.LC_ALL, "C")
result = {
    "utf8_mode": sys.flags.utf8_mode,
    "codec": codecs.lookup(locale.getpreferredencoding(False)).name,
}
run = harness.run_with_timeout([sys.executable, "-c", %(text)r], timeout=%(timeout)r)
result["text"] = run.stdout
try:
    harness.run_with_timeout([sys.executable, "-c", %(stray)r], timeout=%(timeout)r)
    result["stray"] = "decoded"
except UnicodeDecodeError:
    result["stray"] = "raised"
print(json.dumps(result))
"""


def _c_locale(case: unittest.TestCase) -> None:
    case.addCleanup(locale.setlocale, locale.LC_ALL, locale.setlocale(locale.LC_ALL))
    locale.setlocale(locale.LC_ALL, "C")


class LocaleIndependenceTests(unittest.TestCase):
    def test_utf8_output_decodes_in_the_c_locale(self) -> None:
        _c_locale(self)
        run = harness.run_with_timeout([sys.executable, "-c", EMIT_TEXT], timeout=RUN_TIMEOUT)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout, TEXT)

    def test_output_that_is_not_utf8_raises_in_the_c_locale(self) -> None:
        _c_locale(self)
        with self.assertRaises(UnicodeDecodeError):
            harness.run_with_timeout([sys.executable, "-c", EMIT_STRAY], timeout=RUN_TIMEOUT)

    def test_utf8_output_decodes_with_utf8_mode_off(self) -> None:
        code = CHILD % {
            "tests": str(TESTS),
            "text": EMIT_TEXT,
            "stray": EMIT_STRAY,
            "timeout": RUN_TIMEOUT,
        }
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C",
            "LC_ALL": "C",
            "PYTHONCOERCECLOCALE": "0",
            "TOOL_BIN": "",
        }
        child = harness.run_with_timeout(
            [sys.executable, "-X", "utf8=0", "-c", code], env=env, timeout=RUN_TIMEOUT * 3
        )
        self.assertEqual(child.returncode, 0, child.stderr)
        result = json.loads(child.stdout)
        self.assertEqual(result["utf8_mode"], 0)
        self.assertNotEqual(result["codec"], "utf-8", "the child's locale codec is UTF-8")
        self.assertEqual(result["text"], TEXT)
        self.assertEqual(result["stray"], "raised")

    def test_unit_puts_back_the_locale_it_found(self) -> None:
        self.addCleanup(locale.setlocale, locale.LC_ALL, locale.setlocale(locale.LC_ALL))
        for start in ("C", "C.UTF-8"):
            with self.subTest(start=start):
                locale.setlocale(locale.LC_ALL, start)
                found = locale.setlocale(locale.LC_ALL)
                test_unit.UnitTests().test_message_trim_keeps_a_character_boundary()
                self.assertEqual(locale.setlocale(locale.LC_ALL), found)


if __name__ == "__main__":
    unittest.main()
