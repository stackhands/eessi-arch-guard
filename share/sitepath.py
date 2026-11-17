#!/usr/bin/env python3
"""Whether the EESSI client repo is a CVMFS mount and the site module path is in front.

Nothing here changes MODULEPATH. It reads the mount table and the module path
and says whether a build or a job would see the site's modules before EESSI's.
"""

from __future__ import annotations

import argparse
import os
import sys


def _norm(path: str) -> str:
    path = (path or "").strip()
    while len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return path


def _unescape(text: str) -> str:
    """mountinfo writes space, tab, newline and backslash as \\ooo."""
    if "\\" not in text:
        return text
    out = []
    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 3 < len(text) and text[i + 1 : i + 4].isdigit():
            out.append(chr(int(text[i + 1 : i + 4], 8)))
            i += 4
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def holding_mount(mountinfo: str, path: str):
    """The (target, fstype) of the last, longest mount that holds path."""
    path = _norm(path)
    best = None
    for line in mountinfo.splitlines():
        if " - " not in line:
            continue
        head, tail = line.split(" - ", 1)
        fields = head.split()
        if len(fields) < 6:
            continue
        target = _norm(_unescape(fields[4]))
        parts = tail.split()
        if not parts:
            continue
        fstype = parts[0]
        inside = target == "/" or path == target or path.startswith(target + "/")
        if not inside:
            continue
        if best is None or len(target) >= len(best[0]):
            best = (target, fstype)
    return best


def client_mount(mountinfo: str, repo: str):
    """The fstype when repo is on a CVMFS mount, else None."""
    held = holding_mount(mountinfo, repo)
    if held is None or "cvmfs" not in held[1]:
        return None
    return held[1]


def order(modulepath: str, site: str, eessi: str) -> str:
    """Where the site module directory sits relative to the EESSI one."""
    entries = [_norm(item) for item in modulepath.split(":") if _norm(item)]
    site_n, eessi_n = _norm(site), _norm(eessi)
    site_at = next((i for i, item in enumerate(entries) if item == site_n), None)
    eessi_at = next((i for i, item in enumerate(entries) if item == eessi_n), None)
    if site_at is None and eessi_at is None:
        return "both_missing"
    if site_at is None:
        return "site_missing"
    if eessi_at is None:
        return "eessi_missing"
    if site_at < eessi_at:
        return "site_first"
    if site_at > eessi_at:
        return "eessi_first"
    return "same"


def report(mountinfo: str, modulepath: str, repo: str, site: str, eessi: str):
    """(exit status, text). 0 when the repo is a CVMFS mount and site is first."""
    lines = []
    failed = False
    kind = client_mount(mountinfo, repo)
    if kind:
        lines.append(f"ok   {repo} is on a cvmfs mount ({kind})")
    else:
        held = holding_mount(mountinfo, repo)
        where = held[1] if held else "no mount"
        lines.append(f"FAIL {repo} is not on a cvmfs mount ({where})")
        failed = True
    place = order(modulepath, site, eessi)
    site_n, eessi_n = _norm(site), _norm(eessi)
    if place == "site_first":
        lines.append(f"ok   site modules {site_n} precede {eessi_n}")
    elif place == "eessi_first":
        lines.append(f"FAIL site modules {site_n} are behind {eessi_n}")
        failed = True
    elif place == "site_missing":
        lines.append(f"FAIL site modules {site_n} are not on MODULEPATH")
        failed = True
    elif place == "eessi_missing":
        lines.append(f"FAIL EESSI modules {eessi_n} are not on MODULEPATH")
        failed = True
    else:
        lines.append(f"FAIL site modules {site_n} and the EESSI modules are the same path")
        failed = True
    return (1 if failed else 0), "\n".join(lines) + "\n"


def _modules(value: str) -> str:
    value = value.strip()
    if value.endswith("/modules/all"):
        return value
    return value.rstrip("/") + "/modules/all"


def _placeholder_modules_path(value: str) -> bool:
    """True when a shell expanded an unset install variable to /modules/all."""
    return _norm(value) == "/modules/all"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="sitepath.py")
    parser.add_argument("--repo", default=os.environ.get("EESSI_CVMFS_REPO") or "/cvmfs/software.eessi.io")
    parser.add_argument("--site", default="")
    parser.add_argument("--eessi", default="")
    parser.add_argument("--modulepath", default=os.environ.get("MODULEPATH") or "")
    parser.add_argument("--mountinfo", default="")
    parser.add_argument(
        "--require-input",
        action="store_true",
        help="exit 2 when install paths are missing or expand to /modules/all alone",
    )
    args = parser.parse_args(argv)
    site = args.site or os.environ.get("EESSI_SITE_SOFTWARE_PATH") or ""
    eessi = args.eessi or os.environ.get("EESSI_SOFTWARE_PATH") or ""
    if not site or not eessi:
        sys.stderr.write("sitepath.py: need --site and --eessi, or EESSI_SITE_SOFTWARE_PATH and EESSI_SOFTWARE_PATH\n")
        return 2
    if args.require_input and (_placeholder_modules_path(site) or _placeholder_modules_path(eessi)):
        sys.stderr.write(
            "sitepath.py: --require-input: install paths are missing "
            "(unset shell variables expand to /modules/all)\n"
        )
        return 2
    if not args.site:
        site = _modules(site)
    if not args.eessi:
        eessi = _modules(eessi)
    path = args.mountinfo or "/proc/self/mountinfo"
    try:
        mountinfo = open(path, encoding="utf-8").read()
    except OSError as exc:
        sys.stderr.write(f"sitepath.py: cannot read {path}: {exc.strerror}\n")
        return 2
    code, text = report(mountinfo, args.modulepath, args.repo, site, eessi)
    sys.stdout.write(text)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
