#!/usr/bin/env bash
# Archive verified delivery bytes without manufacturing a runtime (DL-389).
set -euo pipefail
if [ "$#" -ne 2 ]; then
    echo "usage: PYTHON=python3.12 $0 <bundle> <new-output-directory>" >&2
    exit 2
fi
tools="$(cd "$(dirname "$0")" && pwd)"
bundle="$(cd "$1" && pwd)"
python="${PYTHON:-python3.12}"
"$python" -I "$tools/bundle.py" candidate "$bundle" /tmp/dsl41-rpm-source-check base >/dev/null
version="$("$python" -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$bundle/manifest.json")"
mkdir "$2"
output="$(cd "$2" && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
cp -a "$bundle" "$work/dsl41-offline-$version"
tar -czf "$output/dsl41-offline-$version.tar.gz" -C "$work" "dsl41-offline-$version"
cp "$tools/dsl41-offline.spec" "$output/"
echo "$output/dsl41-offline-$version.tar.gz"
