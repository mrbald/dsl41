#!/usr/bin/env bash
# Export release inputs upstream of target-native manufacturing (DL-396).
set -euo pipefail
if [ "$#" -ne 2 ]; then
    echo "usage: $0 <release.whl> <new-exports-directory>" >&2
    exit 2
fi
repo="$(cd "$(dirname "$0")/.." && pwd)"
python="${PYTHON:-python3}"
wheel="$("$python" -I -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve(strict=True))' "$1")"
exports="$("$python" -I -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).absolute())' "$2")"
"$python" -I "$repo/scripts/offline/bundle.py" check-release "$repo" "$wheel"
# An incomplete export remains evidence; only a new destination is accepted (DL-396).
mkdir "$exports"
for profile in base ui; do
    extra=""
    if [ "$profile" = ui ]; then extra="--extra=ui"; fi
    (cd "$repo" && uv export --locked --no-dev --no-emit-project --no-header ${extra:+"$extra"} \
        -o "$exports/requirements-$profile.txt")
done
"$python" -I "$repo/scripts/offline/bundle.py" release-inputs "$repo" "$wheel" "$exports"
