"""Import the launcher and call it from many threads; print what happened as JSON.

Run by ``tests/test_free_threading.py`` against the checkout and by
``docs/examples/check_install.sh`` against an installed wheel. ``gil`` is what
``sys._is_gil_enabled()`` says after the import (``null`` before Python 3.13),
``free_threaded_build`` whether this CPython was built without the GIL, and
``answers`` the distinct (binary, exit status, stdout) triples the threads got.
"""

from __future__ import annotations

import json
import subprocess
import sys
import sysconfig
import threading

import eessi_arch_guard

THREADS = 16
CALLS = 8


def main() -> int:
    gil = sys._is_gil_enabled() if hasattr(sys, "_is_gil_enabled") else None
    start = threading.Barrier(THREADS)
    answers = set()
    errors = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            start.wait()
            for _ in range(CALLS):
                binary = eessi_arch_guard.locate()
                done = subprocess.run([binary, "--version"], capture_output=True, text=True)
                with lock:
                    answers.add((binary, done.returncode, done.stdout))
        except BaseException as error:
            with lock:
                errors.append(repr(error))

    threads = [threading.Thread(target=worker) for _ in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    json.dump(
        {
            "gil": gil,
            "free_threaded_build": bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
            "answers": sorted(answers),
            "errors": errors,
        },
        sys.stdout,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
