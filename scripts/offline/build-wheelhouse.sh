#!/usr/bin/env bash
# Build a target-native delivery from the exact release wheel and lock (DL-389).
set -euo pipefail
if [ "$#" -ne 3 ]; then
    echo "usage: PYTHON=python3.12 $0 <release.whl> <exports-directory> <new-bundle-directory>" >&2
    exit 2
fi
repo="$(cd "$(dirname "$0")/../.." && pwd)"
python="${PYTHON:-python3.12}"
wheel="$("$python" -I -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve(strict=True))' "$1")"
exports="$("$python" -I -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve(strict=True))' "$2")"
bundle="$("$python" -I -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).absolute())' "$3")"
"$python" -I "$repo/scripts/offline/bundle.py" check-input "$repo" "$wheel"
"$python" -I "$repo/scripts/offline/bundle.py" verify-inputs "$repo" "$wheel" "$exports"
# An interrupted build remains evidence; only a new destination is accepted (DL-389).
mkdir "$bundle"
mkdir "$bundle/wheels" "$bundle/assets" "$bundle/tools" "$bundle/release-inputs"
cp "$exports"/{requirements-base.txt,requirements-ui.txt,release-inputs.json} "$bundle/release-inputs/"
cp "$repo/uv.lock" "$repo/pyproject.toml" "$bundle/"
cp "$wheel" "$bundle/wheels/"
cp "$repo"/scripts/offline/{prepare.sh,prepare-locked.sh,bundle.py,smoke.py,qualify.sh} \
    "$bundle/tools/"
for profile in base ui; do
    cp "$bundle/release-inputs/requirements-$profile.txt" "$bundle/requirements-$profile.txt"
    "$python" -I "$repo/scripts/offline/bundle.py" add-release "$bundle" "$wheel" "$profile"
    # Download only wheels; hashes and every transitive pin come from uv.lock (DL-389).
    env -i PATH="$PATH" HOME="${HOME:-/tmp}" PIP_CONFIG_FILE=/dev/null \
        "$python" -I -m pip download --disable-pip-version-check --no-cache-dir \
        --index-url "${DSL41_PYTHON_INDEX:-https://pypi.org/simple}" \
        --only-binary=:all: --require-hashes --no-deps \
        --find-links "$bundle/wheels" -r "$bundle/requirements-$profile.txt" \
        --dest "$bundle/wheels"
done
"$python" -I "$repo/scripts/offline/bundle.py" manifest "$bundle" "$wheel"
echo "Built $bundle; qualify both profiles on this target before delivery."
