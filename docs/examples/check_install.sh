#!/bin/bash
# Build the wheel with the binary in it, install it into a virtual environment,
# and run the console script and the module through it. Needs no root and
# changes nothing outside the temporary directory, which is removed on exit.
set -euo pipefail

root=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
binary=${TOOL_BIN:-$root/zig-out/bin/eessi-arch-guard}
bundled=$root/src/eessi_arch_guard/bin/eessi-arch-guard
work=$(mktemp -d /tmp/eessi-arch-guard-wheel.XXXXXX)
cleanup() {
    rm -f "$bundled"
    rmdir "$root/src/eessi_arch_guard/bin" 2>/dev/null || true
    rm -rf "$work"
}
trap cleanup EXIT

echo "+ install -D -m 755 eessi-arch-guard src/eessi_arch_guard/bin/eessi-arch-guard"
install -D -m 755 "$binary" "$bundled"
python=${PYTHON:-python3}
echo "+ $python -m pip wheel --no-deps -w <dir> ."
"$python" -m pip wheel --disable-pip-version-check --quiet --no-deps -w "$work/dist" "$root"
wheel=$(printf '%s\n' "$work"/dist/*.whl)
case "$wheel" in
    *-py3-none-manylinux2014_*.whl) echo "wheel tag: py3-none-manylinux2014" ;;
    *) echo "unexpected wheel name: $wheel" >&2; exit 1 ;;
esac
echo "+ python3 tools/check_wheel.py <wheel>"
"$python" "$root/tools/check_wheel.py" "$wheel"
rm -f "$bundled"

echo "+ $python -m venv <venv>"
"$python" -m venv "$work/venv"
echo "+ <venv>/bin/python -m pip install <wheel>"
"$work/venv/bin/python" -m pip install --disable-pip-version-check --quiet "$wheel"
version=$("$work/venv/bin/python" -c 'import importlib.metadata; print(importlib.metadata.version("eessi-arch-guard"))')
echo "installed version: $version"

console="$work/venv/bin/eessi-arch-guard"
direct=$("$binary" --version)
echo "+ <venv>/bin/eessi-arch-guard --version"
via_console=$("$console" --version)
echo "+ <venv>/bin/python -m eessi_arch_guard --version"
via_module=$("$work/venv/bin/python" -m eessi_arch_guard --version)
if [ "$via_console" != "$direct" ] || [ "$via_module" != "$direct" ]; then
    echo "the console script or the module printed another version than the binary" >&2
    exit 1
fi
echo "console script and module print the version of the binary: ${direct%% *}"
if [ "${direct%% *}" != "$version" ]; then
    echo "the package version $version is not the binary's ${direct%% *}" >&2
    exit 1
fi

echo "+ <venv>/bin/eessi-arch-guard, run as a task prolog in an environment without EESSI"
out=$(env -i PATH=/usr/bin:/bin "$console")
echo "exit $?"
[ -z "$out" ] && echo "output: (empty)"

share=$work/venv/share/eessi-arch-guard
for file in sitepath.py cvmfsrev.py lmod/SitePackage.lua; do
    [ -f "$share/$file" ] || { echo "missing $share/$file" >&2; exit 1; }
done
echo "share files: sitepath.py cvmfsrev.py lmod/SitePackage.lua"

echo "+ <venv>/bin/python tests/free_threading_probe.py"
report=$(cd "$work" && env -u PYTHON_GIL PATH=/usr/bin:/bin "$work/venv/bin/python" "$root/tests/free_threading_probe.py")
"$work/venv/bin/python" - "$report" "$work/venv/lib" <<'PY'
import json, sys
report = json.loads(sys.argv[1])
assert report["errors"] == [], report["errors"]
assert len(report["answers"]) == 1 and report["answers"][0][0].startswith(sys.argv[2]), report["answers"]
assert report["answers"][0][1] == 0, report["answers"]
if report["free_threaded_build"]:
    assert report["gil"] is False, "importing the package turned the GIL on"
print("free-threaded build" if report["free_threaded_build"] else "GIL build",
      "| GIL after import:", report["gil"], "| 128 calls from 16 threads, one answer: the bundled binary")
PY
