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
echo "+ python3 -m pip wheel --no-deps -w <dir> ."
python3 -m pip wheel --disable-pip-version-check --quiet --no-deps -w "$work/dist" "$root"
wheel=$(printf '%s\n' "$work"/dist/*.whl)
case "$wheel" in
    *-py3-none-manylinux2014_*.whl) echo "wheel tag: py3-none-manylinux2014" ;;
    *) echo "unexpected wheel name: $wheel" >&2; exit 1 ;;
esac
rm -f "$bundled"

echo "+ python3 -m venv <venv>"
python3 -m venv "$work/venv"
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
