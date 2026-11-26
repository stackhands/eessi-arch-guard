"""Build the _launch extension.

GIL CPython builds it against the limited API (Py_LIMITED_API 3.9), so one
wheel tagged cp39-abi3 serves CPython 3.9 and later. A free-threaded CPython
does not take abi3 wheels and does not support the limited API before 3.15, so
there the extension is a plain build tagged cp313t or cp314t.

CI copies the static binary to src/eessi_arch_guard/bin/eessi-arch-guard
before it builds the wheel. Without it the wheel carries the extension only
and the launcher finds a binary on PATH.
"""

import sysconfig

from setuptools import Extension, setup

LIMITED = not sysconfig.get_config_var("Py_GIL_DISABLED")

extension = Extension(
    "eessi_arch_guard._launch",
    sources=["src/eessi_arch_guard/_launch.c"],
    define_macros=[("Py_LIMITED_API", "0x03090000")] if LIMITED else [],
    py_limited_api=LIMITED,
)

options = {"bdist_wheel": {"py_limited_api": "cp39"}} if LIMITED else {}

setup(ext_modules=[extension], options=options)
