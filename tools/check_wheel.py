"""Check that a built wheel installs on every CPython 3, free-threaded or not.

The wheel carries Python source and a static executable, no extension module.
Its tag is therefore ``py3-none-manylinux2014_<arch>``: ``none`` ties it to no
CPython ABI. An ``abi3`` or ``cp3XY`` tag would be wrong for such a wheel, and
a free-threaded CPython does not install ``abi3`` wheels.

Usage: python tools/check_wheel.py DIST/NAME.whl
Exit status 0 when every check passes, 1 with one line per failure otherwise.
"""

from __future__ import annotations

import re
import sys
import zipfile
from email.parser import Parser
from pathlib import Path

TAG = re.compile(r"^py3-none-manylinux2014_(x86_64|aarch64)$")
EXTENSION = re.compile(r"\.(so|pyd|dylib)$|\.cpython-|\.abi3\.")
FREE_THREADING = "Programming Language :: Python :: Free Threading :: 3 - Stable"


def check(path: Path) -> list[str]:
    problems = []
    parts = path.name[: -len(".whl")].split("-")
    file_tag = "-".join(parts[-3:])
    if not TAG.match(file_tag):
        problems.append(f"file name tag {file_tag}, expected py3-none-manylinux2014_<arch>")
    with zipfile.ZipFile(path) as wheel:
        names = wheel.namelist()
        dist_info = next(n.split("/")[0] for n in names if n.split("/")[0].endswith(".dist-info"))
        meta = Parser().parsestr(wheel.read(f"{dist_info}/WHEEL").decode())
        tags = meta.get_all("Tag") or []
        if tags != [file_tag]:
            problems.append(f"WHEEL Tag lines {tags}, expected [{file_tag}]")
        if meta.get("Root-Is-Purelib") != "false":
            problems.append("WHEEL Root-Is-Purelib is not false; the wheel carries a platform binary")
        for name in names:
            if EXTENSION.search(name):
                problems.append(f"{name}: a compiled Python extension; the wheel must carry none")
        metadata = Parser().parsestr(wheel.read(f"{dist_info}/METADATA").decode())
        if FREE_THREADING not in (metadata.get_all("Classifier") or []):
            problems.append(f"METADATA lacks the classifier {FREE_THREADING!r}")
    return problems


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        sys.stderr.write("usage: check_wheel.py WHEEL\n")
        return 2
    problems = check(Path(argv[0]))
    for line in problems:
        print(f"check_wheel: {line}")
    if not problems:
        print(f"check_wheel: {Path(argv[0]).name}: py3-none, no extension module, free-threading classifier")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
