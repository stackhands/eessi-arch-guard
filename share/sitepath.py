#!/usr/bin/env python3
"""Whether the EESSI client repo is a CVMFS mount and the site module path is in front.

Nothing here changes MODULEPATH or the environment. It reads the mount table,
the module path, loaded module metadata, and optional Lmod cache state, and
reports whether a build or a job would see the expected module trees.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

_LUA_STICKY_RE = re.compile(r"""add_property\s*\(\s*["']lmod["']\s*,\s*["']sticky["']""")
_TCL_STICKY_RE = re.compile(r"""^\s*add-property\s+lmod\s+sticky\b""", re.MULTILINE)
_LUA_QUOTED_KEY_RE = re.compile(
    r"""\[\s*(?:"([^"\\]*(?:\\.[^"\\]*)*)"|'([^'\\]*(?:\\.[^'\\]*)*)')\s*\]"""
)
_LUA_STR_FIELD_RE = (
    r"""(?:\[\s*["']{key}["']\s*\]|{key})\s*=\s*"""
    r"""(?:"([^"\\]*(?:\\.[^"\\]*)*)"|'([^'\\]*(?:\\.[^'\\]*)*)')"""
)


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


def _modulepath_entries(modulepath: str) -> List[str]:
    return [_norm(item) for item in modulepath.split(":") if _norm(item)]


def order(modulepath: str, site: str, eessi: str) -> str:
    """Where the site module directory sits relative to the EESSI one."""
    entries = _modulepath_entries(modulepath)
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


def _scan_module_root(root: str) -> Tuple[List[str], List[str]]:
    """Return (sorted module ids, sorted intra-root duplicates) under root."""
    seen: Dict[str, str] = {}
    duplicates: List[str] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if not name.startswith("."))
        for filename in sorted(filenames):
            if filename.startswith(".") or filename in ("default", "modulerc"):
                continue
            path = os.path.join(directory, filename)
            if not os.path.isfile(path):
                continue
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            mod_id = rel[:-4] if filename.endswith(".lua") else rel
            if mod_id in seen:
                duplicates.append(mod_id)
            else:
                seen[mod_id] = path
    return sorted(seen), sorted(set(duplicates))


def _check_retained_and_collisions(
    modulepath: str, retain: Sequence[str]
) -> Tuple[bool, List[str]]:
    """Check retained roots, duplicate MODULEPATH entries, and module precedence collisions."""
    lines: List[str] = []
    failed = False
    entries = _modulepath_entries(modulepath)
    retained = list(dict.fromkeys(_norm(p) for p in retain if _norm(p)))

    seen_norm: Set[str] = set()
    seen_real: Dict[str, str] = {}
    unique_dirs: List[str] = []
    dup_found = False

    for entry in entries:
        if entry in seen_norm:
            lines.append(f"FAIL duplicate MODULEPATH entry {entry}")
            failed = True
            dup_found = True
            continue
        seen_norm.add(entry)
        if os.path.isdir(entry):
            real = os.path.realpath(entry)
            if real in seen_real:
                lines.append(
                    f"FAIL duplicate MODULEPATH entry {entry} resolves to the same directory as "
                    f"{seen_real[real]}"
                )
                failed = True
                dup_found = True
                continue
            seen_real[real] = entry
            unique_dirs.append(entry)

    entry_reals = set(seen_real)
    for ret in retained:
        is_dir = os.path.isdir(ret)
        on_mp = (ret in seen_norm) or (is_dir and os.path.realpath(ret) in entry_reals)
        if not on_mp and not is_dir:
            lines.append(
                f"FAIL retained root {ret} disappeared "
                "(absent from MODULEPATH and directory missing)"
            )
            failed = True
        elif not on_mp:
            lines.append(f"FAIL retained root {ret} disappeared from MODULEPATH")
            failed = True
        elif not is_dir:
            lines.append(f"FAIL retained root {ret} directory is absent")
            failed = True
        else:
            lines.append(f"ok   retained root {ret} is present on MODULEPATH")

    seen_modules: Dict[str, str] = {}
    collision_found = False
    for root in unique_dirs:
        mods, intra_dups = _scan_module_root(root)
        for mod_id in intra_dups:
            lines.append(f"FAIL duplicate modulefile for {mod_id} in {root}")
            failed = True
            collision_found = True
        for mod_id in mods:
            if mod_id in seen_modules:
                lines.append(
                    f"FAIL precedence collision for {mod_id}: {seen_modules[mod_id]} shadows {root}"
                )
                failed = True
                collision_found = True
            else:
                seen_modules[mod_id] = root

    if not dup_found and not collision_found:
        lines.append("ok   no duplicate MODULEPATH entries or module precedence collisions")

    return failed, lines


def _extract_lua_brace_block(text: str, open_brace_idx: int) -> str:
    """Return substring from open_brace_idx to matching '}' respecting Lua strings."""
    depth = 0
    i = open_brace_idx
    n = len(text)
    in_quote = ""
    while i < n:
        ch = text[i]
        if in_quote:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == in_quote:
                in_quote = ""
            i += 1
            continue
        if ch in ('"', "'"):
            in_quote = ch
        elif ch == "-" and i + 1 < n and text[i + 1] == "-":
            nl = text.find("\n", i + 2)
            i = n if nl == -1 else nl + 1
            continue
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[open_brace_idx : i + 1]
        i += 1
    return ""


def _lua_string_field(block: str, key: str) -> str:
    pattern = re.compile(_LUA_STR_FIELD_RE.format(key=re.escape(key)))
    match = pattern.search(block)
    if not match:
        return ""
    raw = match.group(1) if match.group(1) is not None else match.group(2)
    return raw.replace('\\"', '"').replace("\\'", "'").replace("\\\\", "\\")


def _decode_module_table(env: Mapping[str, str]) -> str:
    sz_raw = env.get("_ModuleTable_Sz_", "").strip()
    chunks: List[str] = []
    if sz_raw.isdigit() and int(sz_raw) > 0:
        for idx in range(1, int(sz_raw) + 1):
            chunk = env.get(f"_ModuleTable{idx:03d}_", "")
            if not chunk:
                return ""
            chunks.append(chunk)
    else:
        idx = 1
        while True:
            chunk = env.get(f"_ModuleTable{idx:03d}_", "")
            if not chunk:
                break
            chunks.append(chunk)
            idx += 1
    if not chunks:
        return ""
    encoded = "".join(chunks).strip()
    try:
        raw = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error):
        return ""
    text = raw.decode("utf-8", errors="replace")
    if "_ModuleTable_" not in text or "mT" not in text:
        return ""
    return text


def _parse_module_table(env: Mapping[str, str]) -> Dict[str, Dict[str, object]]:
    """Parse Lmod _ModuleTable001_.. entries keyed by fullName and short name."""
    text = _decode_module_table(env)
    if not text:
        return {}
    mt_match = re.search(r"""(?:\[\s*["']mT["']\s*\]|mT)\s*=\s*\{""", text)
    if not mt_match:
        return {}
    mt_block = _extract_lua_brace_block(text, mt_match.end() - 1)
    if not mt_block:
        return {}

    records: Dict[str, Dict[str, object]] = {}
    i = 1
    n = len(mt_block) - 1
    while i < n:
        ch = mt_block[i]
        if ch.isspace() or ch == ",":
            i += 1
            continue
        key = ""
        if ch == "[":
            end_q = mt_block.find("]", i + 1)
            if end_q == -1:
                break
            key = mt_block[i + 1 : end_q].strip().strip("\"'")
            i = end_q + 1
        elif ch.isalpha() or ch == "_":
            j = i
            while j < n and (mt_block[j].isalnum() or mt_block[j] == "_"):
                j += 1
            key = mt_block[i:j]
            i = j
        else:
            i += 1
            continue
        while i < n and mt_block[i].isspace():
            i += 1
        if i >= n or mt_block[i] != "=":
            continue
        i += 1
        while i < n and mt_block[i].isspace():
            i += 1
        if i >= n or mt_block[i] != "{":
            continue
        entry_block = _extract_lua_brace_block(mt_block, i)
        if not entry_block:
            break
        i += len(entry_block)
        full_name = _lua_string_field(entry_block, "fullName") or key
        fn = _lua_string_field(entry_block, "fn")
        sticky = False
        prop_match = re.search(r"""(?:\[\s*["']propT["']\s*\]|propT)\s*=\s*\{""", entry_block)
        if prop_match:
            prop_block = _extract_lua_brace_block(entry_block, prop_match.end() - 1)
            lmod_match = re.search(r"""(?:\[\s*["']lmod["']\s*\]|lmod)\s*=\s*\{""", prop_block)
            if lmod_match:
                lmod_block = _extract_lua_brace_block(prop_block, lmod_match.end() - 1)
                if re.search(
                    r"""(?:\[\s*["']sticky["']\s*\]|sticky)\s*=\s*(?:1|true)\b""",
                    lmod_block,
                ):
                    sticky = True
        info: Dict[str, object] = {"fullName": full_name, "fn": fn, "sticky": sticky}
        records[full_name] = info
        if key and key not in records:
            records[key] = info
    return records


def _modulefile_is_sticky(path: str) -> bool:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return False
    if path.endswith(".lua"):
        cleaned = re.sub(r"--[^\n]*", "", text)
        return bool(_LUA_STICKY_RE.search(cleaned))
    cleaned = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    return bool(_TCL_STICKY_RE.search(cleaned))


def _source_root_for_module(name: str, fn: str, entries: Sequence[str]) -> str:
    if not fn:
        return ""
    fn_n = _norm(fn)
    for suffix in (f"/{name}.lua", f"/{name}"):
        if fn_n.endswith(suffix):
            return _norm(fn_n[: -len(suffix)])
    for entry in entries:
        if fn_n.startswith(entry + "/"):
            return entry
    parent = os.path.dirname(fn_n)
    if "/" in name:
        return os.path.dirname(parent)
    return parent


def _check_sticky_modules(modulepath: str, env: Mapping[str, str]) -> Tuple[bool, List[str]]:
    """Report loaded sticky modules whose source root is absent."""
    entries = _modulepath_entries(modulepath)
    entry_reals = {os.path.realpath(e) for e in entries if os.path.isdir(e)}
    loaded = [part for part in env.get("LOADEDMODULES", "").split(":") if part]
    lmfiles = [part for part in env.get("_LMFILES_", "").split(":") if part]
    mt_records = _parse_module_table(env)

    candidates: List[Tuple[str, str, bool]] = []
    for idx, name in enumerate(loaded):
        fn = lmfiles[idx] if idx < len(lmfiles) else ""
        short = name.split("/", 1)[0]
        rec = mt_records.get(name) or mt_records.get(short)
        if not fn and rec:
            fn = str(rec.get("fn") or "")
        sticky = bool(rec and rec.get("sticky"))
        if not sticky and fn and os.path.isfile(fn):
            sticky = _modulefile_is_sticky(fn)
        candidates.append((name, fn, sticky))

    if not loaded and mt_records:
        seen_names: Set[str] = set()
        for rec in mt_records.values():
            full_name = str(rec.get("fullName") or "")
            if full_name and full_name not in seen_names:
                seen_names.add(full_name)
                candidates.append((full_name, str(rec.get("fn") or ""), bool(rec.get("sticky"))))

    lines: List[str] = []
    failed = False
    ok_names: List[str] = []

    for name, fn, sticky in candidates:
        if not sticky:
            continue
        source_root = _source_root_for_module(name, fn, entries)
        root_exists = bool(source_root) and os.path.isdir(source_root)
        root_on_mp = bool(source_root) and (
            (source_root in entries)
            or (root_exists and os.path.realpath(source_root) in entry_reals)
        )
        file_exists = bool(fn) and os.path.isfile(fn)
        if not root_exists and not root_on_mp:
            shown = source_root or "<unknown>"
            lines.append(f"FAIL loaded sticky module {name} source root {shown} is absent")
            failed = True
        elif not root_exists:
            lines.append(
                f"FAIL loaded sticky module {name} source root {source_root} directory is absent"
            )
            failed = True
        elif not root_on_mp:
            lines.append(
                f"FAIL loaded sticky module {name} source root {source_root} is absent from "
                "MODULEPATH"
            )
            failed = True
        elif not file_exists:
            lines.append(f"FAIL loaded sticky module {name} modulefile {fn} is absent")
            failed = True
        else:
            ok_names.append(name)

    if not failed:
        if ok_names:
            lines.append(
                f"ok   loaded sticky modules have present source roots ({', '.join(ok_names)})"
            )
        else:
            lines.append("ok   no loaded sticky modules with absent source roots")
    return failed, lines


def _parse_sc_descript(text: str) -> List[Tuple[str, str]]:
    """Extract (dir, timestamp) entries from an lmodrc.lua scDescriptT table."""
    match = re.search(r"\bscDescriptT\s*=\s*\{", text)
    if not match:
        return []
    outer = _extract_lua_brace_block(text, match.end() - 1)
    if not outer:
        return []
    results: List[Tuple[str, str]] = []
    i = 1
    n = len(outer) - 1
    while i < n:
        if outer[i] == "{":
            entry = _extract_lua_brace_block(outer, i)
            if not entry:
                break
            i += len(entry)
            cache_dir = _norm(_lua_string_field(entry, "dir"))
            ts_path = _norm(_lua_string_field(entry, "timestamp"))
            if cache_dir:
                results.append((cache_dir, ts_path))
        else:
            i += 1
    return results


def _cache_files_in_dir(cache_dir: str) -> List[str]:
    found: List[str] = []
    for name in sorted(os.listdir(cache_dir)):
        if (name.startswith("spiderT") or name.startswith("dbT")) and (
            name.endswith(".lua") or ".luac" in name
        ):
            found.append(os.path.join(cache_dir, name))
    return found


def _covered_roots_for_cache(
    cache_dir: str, lua_texts: Sequence[str], mp_entries: Sequence[str]
) -> List[str]:
    existing = [entry for entry in mp_entries if os.path.isdir(entry)]
    if not existing:
        return []
    matched: List[str] = []
    quoted_paths: Set[str] = set()
    for text in lua_texts:
        for m in _LUA_QUOTED_KEY_RE.finditer(text):
            val = m.group(1) if m.group(1) is not None else m.group(2)
            if val.startswith("/"):
                quoted_paths.add(_norm(val))
    for entry in existing:
        if entry in quoted_paths or os.path.realpath(entry) in quoted_paths:
            matched.append(entry)
    if not matched and os.path.basename(cache_dir) == "cache":
        parent = os.path.dirname(cache_dir)
        if os.path.basename(parent) == ".lmod":
            prefix = os.path.dirname(parent)
            for entry in existing:
                if entry == prefix or entry.startswith(prefix + "/"):
                    matched.append(entry)
    return matched if matched else existing


def _newest_tree_entry(root: str) -> Tuple[float, str]:
    newest_mtime = os.stat(root).st_mtime
    newest_path = root
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if not name.startswith("."))
        for dname in dirnames:
            dpath = os.path.join(directory, dname)
            try:
                mt = os.stat(dpath).st_mtime
            except OSError:
                continue
            if mt > newest_mtime:
                newest_mtime = mt
                newest_path = dpath
        for fname in sorted(filenames):
            if fname.startswith("."):
                continue
            fpath = os.path.join(directory, fname)
            try:
                mt = os.stat(fpath).st_mtime
            except OSError:
                continue
            if mt > newest_mtime:
                newest_mtime = mt
                newest_path = fpath
    return newest_mtime, newest_path


def _check_cache_files(modulepath: str, env: Mapping[str, str]) -> Tuple[bool, List[str]]:
    """Report configured or user Lmod cache files that are unreadable or stale."""
    lines: List[str] = []
    failed = False
    mp_entries = _modulepath_entries(modulepath)

    rc_paths = [_norm(p) for p in env.get("LMOD_RC", "").split(":") if _norm(p)]
    targets: List[Tuple[str, str, str]] = []
    seen_dirs: Set[str] = set()

    for rc_path in rc_paths:
        try:
            with open(rc_path, encoding="utf-8") as handle:
                rc_text = handle.read()
        except (OSError, UnicodeDecodeError) as exc:
            detail = exc.strerror if isinstance(exc, OSError) else str(exc)
            lines.append(f"FAIL configured LMOD_RC file {rc_path} is unreadable ({detail})")
            failed = True
            continue
        for cache_dir, ts_path in _parse_sc_descript(rc_text):
            if cache_dir not in seen_dirs:
                seen_dirs.add(cache_dir)
                targets.append(("configured", cache_dir, ts_path))

    xdg_cache = _norm(env.get("XDG_CACHE_HOME", ""))
    home = _norm(env.get("HOME", ""))
    user_candidates: List[str] = []
    if xdg_cache:
        user_candidates.append(os.path.join(xdg_cache, "lmod"))
    elif home:
        user_candidates.append(os.path.join(home, ".cache", "lmod"))
    if home:
        user_candidates.append(os.path.join(home, ".lmod.d", ".cache"))

    for uc_dir in user_candidates:
        uc_n = _norm(uc_dir)
        if not uc_n or uc_n in seen_dirs or not os.path.lexists(uc_n):
            continue
        seen_dirs.add(uc_n)
        try:
            uc_files = _cache_files_in_dir(uc_n)
        except OSError as exc:
            lines.append(f"FAIL user cache directory {uc_n} is unreadable ({exc.strerror})")
            failed = True
            continue
        ts_candidate = os.path.join(uc_n, "timestamp")
        if uc_files or os.path.lexists(ts_candidate):
            ts_path = ts_candidate if os.path.lexists(ts_candidate) else ""
            targets.append(("user", uc_n, ts_path))

    if not targets:
        if not failed:
            lines.append("ok   no configured Lmod cache files")
        return failed, lines

    for kind, cache_dir, ts_path in targets:
        if not os.path.isdir(cache_dir):
            lines.append(f"FAIL {kind} cache directory {cache_dir} is unreadable")
            failed = True
            continue
        try:
            cache_files = _cache_files_in_dir(cache_dir)
        except OSError as exc:
            lines.append(f"FAIL {kind} cache directory {cache_dir} is unreadable ({exc.strerror})")
            failed = True
            continue
        if not cache_files:
            lines.append(f"FAIL {kind} cache directory {cache_dir} has no cache files")
            failed = True
            continue

        dir_failed = False
        mtimes: List[float] = []
        lua_texts: List[str] = []

        if ts_path:
            try:
                with open(ts_path, "rb") as handle:
                    handle.read()
                mtimes.append(os.stat(ts_path).st_mtime)
            except OSError as exc:
                lines.append(
                    f"FAIL {kind} cache timestamp {ts_path} is unreadable ({exc.strerror})"
                )
                failed = True
                dir_failed = True

        for cf in cache_files:
            try:
                with open(cf, "rb") as handle:
                    blob = handle.read()
                mtimes.append(os.stat(cf).st_mtime)
            except OSError as exc:
                lines.append(f"FAIL {kind} cache file {cf} is unreadable ({exc.strerror})")
                failed = True
                dir_failed = True
                continue
            if not blob:
                lines.append(f"FAIL {kind} cache file {cf} is unreadable (empty)")
                failed = True
                dir_failed = True
                continue
            if cf.endswith(".lua"):
                if b"\x00" in blob:
                    lines.append(f"FAIL {kind} cache file {cf} is unreadable (corrupt)")
                    failed = True
                    dir_failed = True
                    continue
                try:
                    lua_texts.append(blob.decode("utf-8"))
                except UnicodeDecodeError:
                    lines.append(f"FAIL {kind} cache file {cf} is unreadable (invalid UTF-8)")
                    failed = True
                    dir_failed = True

        if dir_failed:
            continue

        cache_mtime = min(mtimes)
        covered = _covered_roots_for_cache(cache_dir, lua_texts, mp_entries)
        stale_hit = ""
        for root in covered:
            newest_mt, newest_path = _newest_tree_entry(root)
            if newest_mt > cache_mtime:
                stale_hit = newest_path
                break
        if stale_hit:
            lines.append(
                f"FAIL {kind} cache in {cache_dir} is stale ({stale_hit} is newer than cache)"
            )
            failed = True
        else:
            lines.append(f"ok   {kind} cache in {cache_dir} is readable and current")

    return failed, lines


def _avail_command(env: Mapping[str, str]) -> Optional[Tuple[List[str], str]]:
    module_bin = shutil.which("module", path=env.get("PATH", ""))
    if module_bin:
        return [module_bin, "-t", "avail"], "both"
    lmod_cmd = env.get("LMOD_CMD", "")
    if lmod_cmd and os.path.isfile(lmod_cmd) and os.access(lmod_cmd, os.X_OK):
        return [lmod_cmd, "bash", "-t", "avail"], "stderr"
    if "BASH_FUNC_module%%" in env or "BASH_FUNC_module()" in env:
        return ["bash", "--noprofile", "--norc", "-c", "module -t avail"], "both"
    return None


def _parse_terse_avail(text: str) -> Set[str]:
    items: Set[str] = set()
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.endswith(":"):
            head = line[:-1].strip()
            section = _norm(head) if head.startswith("/") else head
            continue
        if line.endswith("/"):
            continue
        items.add(f"{section}: {line}" if section else line)
    return items


def _check_cache_inventory(modulepath: str, env: Mapping[str, str]) -> Tuple[bool, List[str]]:
    """Compare cached module -t avail output against LMOD_IGNORE_CACHE=yes."""
    resolved = _avail_command(env)
    if resolved is None:
        return True, ["FAIL cannot run module -t avail (neither module nor LMOD_CMD is available)"]
    cmd, stream = resolved

    with tempfile.TemporaryDirectory(prefix="eessi-sitepath-cache-") as tmpdir:
        private_home = os.path.join(tmpdir, "home")
        private_xdg = os.path.join(tmpdir, "xdg_cache")
        private_lmod = os.path.join(tmpdir, "lmod_cache")
        os.makedirs(private_home, exist_ok=True)
        os.makedirs(private_xdg, exist_ok=True)
        os.makedirs(private_lmod, exist_ok=True)

        base_env = dict(env)
        base_env["MODULEPATH"] = modulepath
        base_env["HOME"] = private_home
        base_env["XDG_CACHE_HOME"] = private_xdg
        base_env["LMOD_CACHEDIR"] = private_lmod
        if stream == "stderr":
            base_env.pop("LMOD_REDIRECT", None)

        cached_env = dict(base_env)
        cached_env.pop("LMOD_IGNORE_CACHE", None)

        uncached_env = dict(base_env)
        uncached_env["LMOD_IGNORE_CACHE"] = "yes"

        outputs: List[Set[str]] = []
        for label, run_env in (("cached", cached_env), ("LMOD_IGNORE_CACHE=yes", uncached_env)):
            try:
                proc = subprocess.run(
                    cmd,
                    env=run_env,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=30,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return True, [f"FAIL {label} module -t avail failed ({exc})"]
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
                return True, [f"FAIL {label} module -t avail exited {proc.returncode}: {detail}"]
            if stream == "stderr":
                raw_text = proc.stderr or ""
            else:
                raw_text = (proc.stdout or "") + "\n" + (proc.stderr or "")
            outputs.append(_parse_terse_avail(raw_text))

    cached_set, uncached_set = outputs
    if cached_set == uncached_set:
        return False, [
            f"ok   cached module -t avail inventory matches LMOD_IGNORE_CACHE=yes "
            f"({len(uncached_set)} entries)"
        ]

    only_cached = sorted(cached_set - uncached_set)
    only_uncached = sorted(uncached_set - cached_set)
    parts: List[str] = []
    if only_cached:
        shown = ", ".join(only_cached[:5])
        if len(only_cached) > 5:
            shown += f" (+{len(only_cached) - 5} more)"
        parts.append(f"only cached: {shown}")
    if only_uncached:
        shown = ", ".join(only_uncached[:5])
        if len(only_uncached) > 5:
            shown += f" (+{len(only_uncached) - 5} more)"
        parts.append(f"only uncached: {shown}")
    return True, [
        f"FAIL cached module -t avail inventory differs from LMOD_IGNORE_CACHE=yes "
        f"({'; '.join(parts)})"
    ]


def report(
    mountinfo: str,
    modulepath: str,
    repo: str,
    site: str,
    eessi: str,
    retain: Sequence[str] = (),
    check_cache: bool = False,
    env: Optional[Mapping[str, str]] = None,
):
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

    active_env = os.environ if env is None else env
    if retain:
        ret_failed, ret_lines = _check_retained_and_collisions(modulepath, retain)
        lines.extend(ret_lines)
        failed = failed or ret_failed
        sticky_failed, sticky_lines = _check_sticky_modules(modulepath, active_env)
        lines.extend(sticky_lines)
        failed = failed or sticky_failed
    if check_cache:
        cache_failed, cache_lines = _check_cache_files(modulepath, active_env)
        lines.extend(cache_lines)
        failed = failed or cache_failed
        inv_failed, inv_lines = _check_cache_inventory(modulepath, active_env)
        lines.extend(inv_lines)
        failed = failed or inv_failed

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
    parser.add_argument(
        "--repo",
        default=os.environ.get("EESSI_CVMFS_REPO") or "/cvmfs/software.eessi.io",
    )
    parser.add_argument("--site", default="")
    parser.add_argument("--eessi", default="")
    parser.add_argument("--modulepath", default=os.environ.get("MODULEPATH") or "")
    parser.add_argument("--mountinfo", default="")
    parser.add_argument(
        "--require-input",
        action="store_true",
        help="exit 2 when install paths are missing or expand to /modules/all alone",
    )
    parser.add_argument(
        "--retain",
        action="append",
        default=[],
        metavar="PATH",
        help=(
            "check that PATH stays on MODULEPATH, report duplicate/precedence collisions, "
            "and check loaded sticky modules (repeatable)"
        ),
    )
    parser.add_argument(
        "--check-cache",
        action="store_true",
        help=(
            "check configured and user Lmod cache files and compare module -t avail "
            "with LMOD_IGNORE_CACHE=yes"
        ),
    )
    args = parser.parse_args(argv)
    site = args.site or os.environ.get("EESSI_SITE_SOFTWARE_PATH") or ""
    eessi = args.eessi or os.environ.get("EESSI_SOFTWARE_PATH") or ""
    if not site or not eessi:
        sys.stderr.write(
            "sitepath.py: need --site and --eessi, or EESSI_SITE_SOFTWARE_PATH and "
            "EESSI_SOFTWARE_PATH\n"
        )
        return 2
    if args.require_input and (_placeholder_modules_path(site) or _placeholder_modules_path(eessi)):
        sys.stderr.write(
            "sitepath.py: --require-input: install paths are missing "
            "(unset shell variables expand to /modules/all)\n"
        )
        return 2
    for ret in args.retain:
        if not _norm(ret):
            sys.stderr.write("sitepath.py: --retain requires a non-empty path\n")
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
    code, text = report(
        mountinfo,
        args.modulepath,
        args.repo,
        site,
        eessi,
        retain=args.retain,
        check_cache=args.check_cache,
    )
    sys.stdout.write(text)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
