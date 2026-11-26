"""Check that a built wheel has the tag and the contents its CPython needs.

The wheel carries Python source, the ``_launch`` C extension and a static
executable. There are two kinds:

* ``cp39-abi3-manylinux*``: one wheel for every GIL CPython from 3.9, with
  exactly one ``_launch.abi3.so``, built against the limited API.
* ``cp313-cp313t-manylinux*`` and ``cp314-cp314t-manylinux*``: free-threaded
  CPython installs no abi3 wheel, so each free-threaded version has its own,
  with ``_launch.cpython-31Xt-<triplet>.so``.

Both carry the bundled binary and the Free Threading classifier. The
extension links libc, so the platform is a manylinux tag, which
``auditwheel repair`` writes. ``--local`` also accepts ``linux_<arch>`` for a
wheel built outside a manylinux image; use it for local testing only.

Usage: python tools/check_wheel.py [--local] DIST/NAME.whl
Exit status 0 when every check passes, 1 with one line per failure otherwise.
"""

from __future__ import annotations

import re
import sys
import zipfile
from email.parser import Parser
from pathlib import Path

ARCH = r"(?:x86_64|aarch64)"
MANYLINUX = rf"manylinux(?:2014|_2_\d+)_{ARCH}"
LOCAL = rf"linux_{ARCH}"
FREE_THREADING = "Programming Language :: Python :: Free Threading :: 3 - Stable"
BINARY = "eessi_arch_guard/bin/eessi-arch-guard"
FREE_THREADED = {"cp313t": "cp313-cp313t", "cp314t": "cp314-cp314t"}


def expand(tag: str) -> set[str]:
    """The tags a compressed ``py.abi.plat`` triple stands for, as in PEP 425."""
    python, abi, plat = tag.split("-")
    return {f"{p}-{a}-{t}" for p in python.split(".") for a in abi.split(".") for t in plat.split(".")}


def check(path: Path, local: bool = False) -> list[str]:
    problems = []
    parts = path.name[: -len(".whl")].split("-")
    file_tag = "-".join(parts[-3:])
    python, abi, plat = parts[-3:]
    platform_ok = re.fullmatch(rf"{MANYLINUX}(?:\.{MANYLINUX})*", plat) or (
        local and re.fullmatch(LOCAL, plat)
    )
    if not platform_ok:
        want = "manylinux2014_<arch>" + (" or linux_<arch>" if local else "")
        problems.append(f"file name platform {plat}, expected {want}")
    if f"{python}-{abi}" == "cp39-abi3":
        suffix = "abi3"
    elif f"{python}-{abi}" in FREE_THREADED.values():
        suffix = f"cpython-{python[2:]}t"
    else:
        problems.append(
            f"file name tag {python}-{abi}, expected cp39-abi3, cp313-cp313t or cp314-cp314t"
        )
        suffix = ""
    with zipfile.ZipFile(path) as wheel:
        names = wheel.namelist()
        dist_info = next(n.split("/")[0] for n in names if n.split("/")[0].endswith(".dist-info"))
        meta = Parser().parsestr(wheel.read(f"{dist_info}/WHEEL").decode())
        tags = meta.get_all("Tag") or []
        if set(tags) != expand(file_tag):
            problems.append(f"WHEEL Tag lines {tags}, expected {sorted(expand(file_tag))}")
        if meta.get("Root-Is-Purelib") != "false":
            problems.append("WHEEL Root-Is-Purelib is not false; the wheel carries a compiled extension")
        extensions = [n for n in names if re.search(r"\.(so|pyd|dylib)$", n)]
        tail = "" if suffix == "abi3" else "-[^/]+"
        wanted = re.compile(rf"eessi_arch_guard/_launch\.{re.escape(suffix)}{tail}\.so")
        launch = [n for n in extensions if wanted.fullmatch(n)] if suffix else []
        if suffix and len(launch) != 1:
            problems.append(f"expected exactly one _launch.{suffix}*.so, found {launch}")
        for name in extensions:
            if name not in launch:
                problems.append(f"{name}: an extension module the wheel should not carry")
        if BINARY not in names:
            problems.append(f"{BINARY} is missing; the wheel must bundle the binary")
        metadata = Parser().parsestr(wheel.read(f"{dist_info}/METADATA").decode())
        if FREE_THREADING not in (metadata.get_all("Classifier") or []):
            problems.append(f"METADATA lacks the classifier {FREE_THREADING!r}")
    return problems


def main(argv: list[str]) -> int:
    local = "--local" in argv
    args = [a for a in argv if a != "--local"]
    if len(args) != 1:
        sys.stderr.write("usage: check_wheel.py [--local] WHEEL\n")
        return 2
    problems = check(Path(args[0]), local)
    for line in problems:
        print(f"check_wheel: {line}")
    if not problems:
        print(f"check_wheel: {Path(args[0]).name}: tag, extension, binary and classifier match")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
