#!/usr/bin/env bash
# The lock owner invokes this recipe; pip owns installation (DL-389).
set -euo pipefail
bundle="$1"
prefix="$2"
profile="$3"
python="$4"
tools="$(cd "$(dirname "$0")" && pwd)"
candidate="$("$python" -I "$tools/bundle.py" candidate "$bundle" "$prefix" "$profile")"
if [ -e "$candidate" ] || [ -L "$candidate" ]; then
    "$python" -I "$tools/bundle.py" verify "$bundle" "$candidate" "$profile"
    echo "$candidate"
    exit 0
fi
mkdir "$candidate"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
run_clean() {
    env -i PATH="$PATH" HOME="$work" PIP_CONFIG_FILE=/dev/null \
        PYTHONDONTWRITEBYTECODE=1 PYTHONPYCACHEPREFIX="$work/pycache" \
        "$@"
}
# Manufacture at the final path with isolated Python and pip inputs (DL-389).
run_clean "$python" -I -m venv "$candidate"
run_clean "$candidate/bin/python" -I -B -X "pycache_prefix=$work/pycache" -m pip \
    install --disable-pip-version-check --no-cache-dir --no-index \
    --find-links "$bundle/wheels" --only-binary=:all: --require-hashes \
    --no-deps --no-compile -r "$bundle/requirements-$profile.txt"
run_clean "$candidate/bin/python" -I -B -X "pycache_prefix=$work/pycache" -m pip check
run_clean "$candidate/bin/python" -I -B -X "pycache_prefix=$work/pycache" \
    "$tools/smoke.py" "$candidate" "$profile" "$bundle"
"$python" -I "$tools/bundle.py" receipt "$bundle" "$candidate" "$profile"
echo "$candidate"
