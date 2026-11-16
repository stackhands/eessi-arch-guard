#!/usr/bin/env bash
# The Zig build's --version starts with the version build.zig.zon declares
# and ends with the commit given with -Dcommit: a copy of the Zig source with
# another version, built with -Dcommit.
#
#   bash tests/build_version.sh
set -euo pipefail
repo_root=$(cd "$(dirname "$0")/.." && pwd)
work=$(mktemp -d "${TMPDIR:-/tmp}/eag-version.XXXXXX")
trap 'rm -rf -- "${work}"' EXIT
cp -R "${repo_root}/build.zig" "${repo_root}/build.zig.zon" "${repo_root}/zig" "${work}/"
sed -i -E 's/^( *\.version = ")[^"]+(".*)$/\10.0.0-check\2/' "${work}/build.zig.zon"
(cd "${work}" && zig build -Dcommit=0123abc --prefix "${work}/out")
got=$("${work}/out/bin/eessi-arch-guard" --version)
case "${got}" in
    "0.0.0-check ("*", 0123abc)") echo "--version: ${got}" ;;
    *) echo "--version '${got}' does not carry build.zig.zon's version and -Dcommit" >&2; exit 1 ;;
esac
