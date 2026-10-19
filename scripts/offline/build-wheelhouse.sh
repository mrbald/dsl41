#!/usr/bin/env bash
# Build a target-native delivery from the exact release wheel and lock (DL-389).
set -euo pipefail
if [ "$#" -ne 2 ]; then
    echo "usage: PYTHON=python3.12 $0 <release.whl> <new-bundle-directory>" >&2
    exit 2
fi
repo="$(cd "$(dirname "$0")/../.." && pwd)"
python="${PYTHON:-python3.12}"
wheel="$("$python" -I -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve(strict=True))' "$1")"
bundle="$("$python" -I -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).absolute())' "$2")"
"$python" -I "$repo/scripts/offline/bundle.py" check-input "$repo" "$wheel"
# An interrupted build remains evidence; only a new destination is accepted (DL-389).
mkdir "$bundle"
mkdir "$bundle/wheels" "$bundle/assets" "$bundle/tools"
cp "$repo/uv.lock" "$repo/pyproject.toml" "$bundle/"
cp "$wheel" "$bundle/wheels/"
cp "$repo"/scripts/offline/{prepare.sh,prepare-locked.sh,bundle.py,smoke.py,qualify.sh} \
    "$bundle/tools/"
cp "$repo"/examples/nightbank/deploy/{dsl41-launch,dsl41-engine.service,dsl41-supervisor.service,nightbank-access.toml} \
    "$bundle/assets/"
for profile in base ui; do
    extra=""
    if [ "$profile" = ui ]; then extra="--extra=ui"; fi
    (cd "$repo" && uv export --locked --no-dev --no-emit-project ${extra:+"$extra"} \
        -o "$bundle/requirements-$profile.txt")
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
