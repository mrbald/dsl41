#!/usr/bin/env bash
# Run all seven DST scenarios on Podman's Linux storage; export evidence before cleanup.
set -euo pipefail
here=$(CDPATH='' cd "$(dirname "$0")" && pwd -P)
tree=$(CDPATH='' cd "$here/.." && pwd -P)
if [ -n "${DST_EVIDENCE-}" ]; then
    evidence=$DST_EVIDENCE
    mkdir -p "$(dirname "$evidence")"
    mkdir "$evidence" || { echo "DST_EVIDENCE must be a new directory: $evidence" >&2; exit 2; }
else
    evidence=$(mktemp -d /tmp/dsl41-dst-evidence.XXXXXX)
fi
name=dsl41-dst-$$-$(date +%s)
image=localhost/dsl41-dst:$name
podman() { ${PODMAN:-command podman} "$@"; }
created=0
image_created=0
# shellcheck disable=SC2329  # the EXIT trap invokes it
cleanup() {
    status=$?
    trap - EXIT
    if [ "$created" = 1 ]; then
        podman cp "$name:/tmp/dst-drill/." "$evidence/runs" || { [ "$status" != 0 ] || status=1; }
        podman rm --force "$name" || { [ "$status" != 0 ] || status=1; }
    fi
    if [ "$image_created" = 1 ]; then
        podman image rm --no-prune "$image" || { [ "$status" != 0 ] || status=1; }
    fi
    echo "DST evidence: $evidence"
    exit "$status"
}
trap cleanup EXIT
build_start=$SECONDS
build_status=0
podman build --layers=true --tag "$image" --file "$tree/examples/dst-drill/Dockerfile" \
    "$tree" >"$evidence/build.log" 2>&1 || build_status=$?
printf '{"duration_s": %s, "exit": %s}\n' "$((SECONDS - build_start))" "$build_status" >"$evidence/build.json"
[ "$build_status" = 0 ] || { cat "$evidence/build.log"; exit "$build_status"; }
image_created=1
podman create --pull never --name "$name" --label "dsl41.dst=$tree" --init "$image" >"$evidence/container-id"
created=1
start=$SECONDS
status=0
python3 - "$name" >"$evidence/scenarios.log" 2>&1 <<'PY' || status=$?
import os
import shlex
import subprocess
import sys

try:
    result = subprocess.run(
        [*shlex.split(os.environ.get("PODMAN", "podman")), "start", "--attach", sys.argv[1]],
        timeout=2400,
    )
except subprocess.TimeoutExpired:
    sys.exit(124)
sys.exit(result.returncode)
PY
printf '{"duration_s": %s, "exit": %s}\n' "$((SECONDS - start))" "$status" >"$evidence/scenarios.json"
cat "$evidence/scenarios.log"
exit "$status"
