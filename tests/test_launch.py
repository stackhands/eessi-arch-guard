"""eessi_arch_guard._launch.execv: argument checks, errors and a real exec."""

from __future__ import annotations

import errno
import os
import sys
import tracemalloc
import unittest
from pathlib import Path

import harness

sys.path.insert(0, str(harness.ROOT / "src"))
from eessi_arch_guard import _launch  # noqa: E402


class ArgumentTests(unittest.TestCase):
    def test_wrong_argument_types_are_type_errors(self) -> None:
        for args in (
            (),
            ("/bin/echo",),
            (1, ["x"]),
            (None, ["x"]),
            ("/bin/echo", "echo"),
            ("/bin/echo", b"echo"),
            ("/bin/echo", 5),
            ("/bin/echo", [1]),
            ("/bin/echo", [None]),
            ("/bin/echo", ["x"], "extra"),
        ):
            with self.subTest(args=args):
                with self.assertRaises(TypeError):
                    _launch.execv(*args)

    def test_an_embedded_nul_is_a_value_error(self) -> None:
        for args in (
            ("/bin/ec\0ho", ["echo"]),
            (b"/bin/ec\0ho", ["echo"]),
            ("/bin/echo", ["ec\0ho"]),
            ("/bin/echo", ["echo", b"a\0b"]),
        ):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    _launch.execv(*args)

    def test_an_empty_argv_is_a_value_error(self) -> None:
        with self.assertRaises(ValueError):
            _launch.execv("/bin/echo", [])

    def test_a_missing_file_is_file_not_found_with_the_filename(self) -> None:
        missing = "/nonexistent-eessi-arch-guard/prog"
        for path in (missing, missing.encode(), Path(missing)):
            with self.subTest(path=path):
                with self.assertRaises(FileNotFoundError) as caught:
                    _launch.execv(path, [missing])
                self.assertEqual(caught.exception.errno, errno.ENOENT)
                self.assertEqual(caught.exception.filename, path)
                self.assertEqual(caught.exception.strerror, os.strerror(errno.ENOENT))

    def test_a_directory_is_permission_denied(self) -> None:
        with self.assertRaises(PermissionError) as caught:
            _launch.execv("/", ["/"])
        self.assertEqual(caught.exception.errno, errno.EACCES)

    def test_a_failing_call_frees_what_it_allocated(self) -> None:
        def failing() -> None:
            for _ in range(2000):
                with self.assertRaises(OSError):
                    _launch.execv("/nonexistent-eessi-arch-guard/prog", ["a", b"b", Path("c")])

        failing()
        tracemalloc.start()
        try:
            before = tracemalloc.get_traced_memory()[0]
            failing()
            after = tracemalloc.get_traced_memory()[0]
        finally:
            tracemalloc.stop()
        self.assertLess(after - before, 4096)

    def test_the_module_has_the_documented_name(self) -> None:
        self.assertEqual(_launch.__name__, "eessi_arch_guard._launch")
        self.assertTrue(_launch.execv.__doc__.startswith("Replace the current process"))


class ExecTests(unittest.TestCase):
    def run_child(self, code: str):
        env = {"PYTHONPATH": str(harness.ROOT / "src"), "PATH": "/usr/bin:/bin", "LC_ALL": "C"}
        return harness.run_with_timeout([sys.executable, "-c", code], env=env, timeout=30.0)

    def test_exec_replaces_the_process_with_the_program(self) -> None:
        code = (
            "from eessi_arch_guard import _launch\n"
            "print('before', flush=True)\n"
            "_launch.execv('/bin/echo', ['echo', 'hello', 'world'])\n"
            "print('after')\n"
        )
        done = self.run_child(code)
        self.assertEqual((done.returncode, done.stdout), (0, "before\nhello world\n"), done.stderr)

    def test_path_like_and_bytes_arguments_reach_the_program(self) -> None:
        code = (
            "import pathlib\n"
            "from eessi_arch_guard import _launch\n"
            "_launch.execv(pathlib.Path('/bin/echo'), (b'echo', pathlib.Path('p'), 'caf\\xe9'))\n"
        )
        done = self.run_child(code)
        self.assertEqual((done.returncode, done.stdout), (0, "p café\n"), done.stderr)

    def test_argv0_is_what_the_caller_names(self) -> None:
        code = (
            "from eessi_arch_guard import _launch\n"
            "_launch.execv('/bin/sh', ['named', '-c', 'echo $0'])\n"
        )
        done = self.run_child(code)
        self.assertEqual((done.returncode, done.stdout), (0, "named\n"), done.stderr)


if __name__ == "__main__":
    unittest.main()
