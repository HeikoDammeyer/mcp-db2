#!/usr/bin/env bash
# The bundled Db2 clidriver (v11.5.9, the only one IBM ships for macOS x86_64) links against
# GNU libstdc++ at /usr/local/lib/gcc/8/libstdc++.6.dylib. That path does not exist on a modern
# machine, so dyld falls back to Apple's stub /usr/lib/libstdc++.6.dylib, which lacks the C++11
# symbols libdb2 needs and `import ibm_db` dies with "Symbol not found: __ZNKSt8...".
#
# This repoints the dependency at Homebrew's GNU libstdc++ inside the venv only — no system change.
# Re-run it after every `uv sync` that reinstalls ibm_db.
set -euo pipefail

[[ "$(uname -s)" == "Darwin" ]] || { echo "macOS only; nothing to do."; exit 0; }

VENV="${1:-$(cd "$(dirname "$0")/.." && pwd)/.venv}"
LIBDIR="$VENV/lib/python3.11/site-packages/clidriver/lib"
OLD="/usr/local/lib/gcc/8/libstdc++.6.dylib"
NEW="$(brew --prefix gcc 2>/dev/null)/lib/gcc/current/libstdc++.6.dylib"

[[ -f "$NEW" ]] || { echo "GNU libstdc++ not found at $NEW — run: brew install gcc" >&2; exit 1; }
[[ -d "$LIBDIR" ]] || { echo "clidriver not found at $LIBDIR — run: uv sync" >&2; exit 1; }

for lib in libdb2.dylib libdb2clixml4c.dylib; do
    path="$LIBDIR/$lib"
    [[ -f "$path" ]] || continue
    if otool -L "$path" | grep -q "$OLD"; then
        install_name_tool -change "$OLD" "$NEW" "$path"
        codesign --force --sign - "$path" 2>/dev/null || true
        echo "patched $lib"
    else
        echo "$lib already patched"
    fi
done
