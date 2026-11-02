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
image_created=0
# shellcheck disable=SC2329  # the EXIT trap invokes it
cleanup() {
    status=$?
    trap - EXIT
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
# DL-441: each scenario owns its container and /dev/shm; all lane results are collected.
run_scenario() (
    scenario=$1
    name=$name-$scenario
    target=$evidence/$scenario
    mkdir "$target"
    created=0
    start=$SECONDS
    # shellcheck disable=SC2329  # the EXIT trap invokes it
    finish_scenario() {
        status=$?
        trap - EXIT
        if [ "$created" = 1 ]; then
            podman cp "$name:/tmp/dst-drill/." "$target/runs" || { [ "$status" != 0 ] || status=1; }
            podman rm --force "$name" || { [ "$status" != 0 ] || status=1; }
        fi
        printf '{"duration_s": %s, "exit": %s}\n' "$((SECONDS - start))" "$status" >"$target/scenario.json"
        exit "$status"
    }
    trap finish_scenario EXIT
    podman create --pull never --name "$name" --label "dsl41.dst=$tree" --init "$image" \
        python examples/dst-drill/drill.py "$scenario" >"$target/container-id"
    created=1
    python3 - "$name" <<'PYTHON'
import os
import shlex
import subprocess
import sys

try:
    result = subprocess.run(
        [*shlex.split(os.environ.get("PODMAN", "podman")), "start", "--attach", sys.argv[1]],
        timeout=600,
    )
except subprocess.TimeoutExpired:
    sys.exit(124)
sys.exit(result.returncode)
PYTHON
)
run_lane() (
    lane=$1
    shift
    start=$SECONDS
    status=0
    for scenario in "$@"; do
        result=0
        # A separate shell retains errexit inside the scenario's EXIT trap.
        set +e
        (set -e; run_scenario "$scenario") >"$evidence/$scenario.log" 2>&1
        result=$?
        set -e
        if [ "$status" = 0 ] && [ "$result" != 0 ]; then status=$result; fi
    done
    printf '{"duration_s": %s, "exit": %s}\n' "$((SECONDS - start))" "$status" >"$evidence/$lane.json"
    exit "$status"
)
start=$SECONDS
run_lane falls london-fall newyork-fall &
falls=$!
run_lane restart london-fall-restart london-spring london-midnight &
restart=$!
run_lane clock clock-step newyork-spring &
clock=$!
status=0
for pid in "$falls" "$restart" "$clock"; do
    result=0
    wait "$pid" || result=$?
    if [ "$status" = 0 ] && [ "$result" != 0 ]; then status=$result; fi
done
printf '{"duration_s": %s, "exit": %s}\n' "$((SECONDS - start))" "$status" >"$evidence/scenarios.json"
cat "$evidence/falls.json" "$evidence/restart.json" "$evidence/clock.json"
python3 - "$evidence" <<'PYTHON'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
for scenario in (
    "london-fall", "newyork-fall", "london-fall-restart", "london-spring",
    "london-midnight", "clock-step", "newyork-spring",
):
    record = json.loads((root / scenario / "scenario.json").read_text())
    verdict = "PASS" if record["exit"] == 0 else "FAIL"
    print(f"{verdict} {scenario}: exit {record['exit']}, {record['duration_s']}s")
PYTHON
exit "$status"
