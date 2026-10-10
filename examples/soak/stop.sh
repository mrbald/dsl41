#!/bin/sh
# stop.sh -- stop a soak run cleanly and audit it.
#
#   uv run examples/soak/stop.sh DIR
#
# DIR is the directory soak.sh made. The steps:
#   1. a live seal: the engine closes the period and exits 3;
#   2. audit the sealed period;
#   3. wait for the supervisor's commands to end (at most DRAIN_MINUTES,
#      default 50: the longest soak command sleeps 45 minutes);
#   4. shut the supervisor down.
# Each step's time goes to stdout and to DIR/stop.jsonl. The timer soak.sh
# started runs this script too; a manual stop cancels the timer first.
#
# An engine that has not exited ENGINE_EXIT_SECONDS (default 120) after the
# seal stops the script with exit 1, before the audit: the audit needs the
# lock the engine holds. A failed `supervise list` does not end the drain;
# it is reported and asked again until the drain's deadline.
set -eu

[ $# -eq 1 ] || {
    echo "usage: stop.sh DIR" >&2
    exit 2
}
DIR=$(cd "$1" && pwd)
# shellcheck source=/dev/null
. "$DIR/soak.env"
DRAIN_MINUTES=${DRAIN_MINUTES:-50}
DRAIN_SECONDS=${DRAIN_SECONDS:-$((DRAIN_MINUTES * 60))}
DRAIN_POLL_SECONDS=${DRAIN_POLL_SECONDS:-10}
ENGINE_EXIT_SECONDS=${ENGINE_EXIT_SECONDS:-120}
ENGINE_PID=$(cat "$DIR/engine.pid")

# a manual stop cancels the timer; the timer's own run finds itself here
if [ -f "$DIR/timer.pid" ]; then
    TIMER_PID=$(cat "$DIR/timer.pid")
    rm -f "$DIR/timer.pid"
    if [ "$TIMER_PID" != "$$" ]; then
        pkill -P "$TIMER_PID" 2>/dev/null || true
        kill "$TIMER_PID" 2>/dev/null || true
    fi
fi

now() { "$PYTHON" -c 'import time; print(f"{time.time():.3f}")'; }
note() {
    echo "$1"
    echo "$1" >>"$DIR/stop.jsonl"
}
since() { "$PYTHON" -c 'import sys; print(f"{float(sys.argv[2]) - float(sys.argv[1]):.3f}")' "$1" "$(now)"; }

t0=$(now)
status=0
"$DSL41" seal --run-root "$RUN_ROOT" --next "$ESTATE" \
    --next-timezone UTC --next-as-machine localhost --next-detached || status=$?
note "{\"step\": \"seal\", \"exit\": $status, \"seconds\": $(since "$t0")}"
[ "$status" -eq 0 ] || exit "$status"

# the engine exits 3 once the boundary commits
waited=0
while kill -0 "$ENGINE_PID" 2>/dev/null; do
    if [ "$waited" -ge "$ENGINE_EXIT_SECONDS" ]; then
        note "{\"step\": \"engine_exit\", \"exit\": 1, \"seconds\": $(since "$t0")}"
        echo "stop.sh: engine $ENGINE_PID still runs $ENGINE_EXIT_SECONDS s after the seal" >&2
        exit 1
    fi
    sleep 1
    waited=$((waited + 1))
done
note "{\"step\": \"engine_exit\", \"seconds\": $(since "$t0")}"
# the sampler ends within a second of the engine
SAMPLE_PID=$(cat "$DIR/sample.pid")
waited=0
while kill -0 "$SAMPLE_PID" 2>/dev/null && [ "$waited" -lt 30 ]; do
    sleep 1
    waited=$((waited + 1))
done

t1=$(now)
status=0
"$DSL41" audit --run-root "$RUN_ROOT" || status=$?
note "{\"step\": \"audit\", \"exit\": $status, \"seconds\": $(since "$t1")}"

t2=$(now)
deadline=$(($(date +%s) + DRAIN_SECONDS))
drained=false
while :; do
    code=0
    listing=$("$DSL41" supervise list --run-root "$RUN_ROOT" 2>"$DIR/list.err") || code=$?
    if [ "$code" -eq 0 ]; then
        case $listing in
            *'"wrapper_alive": true'*) ;;
            *) drained=true && break ;;
        esac
    elif [ "$code" -eq 2 ] && grep -q '^no supervisor' "$DIR/list.err"; then
        drained=true && break # no supervisor, so no command left to wait for
    else
        echo "stop.sh: supervise list exited $code: $(cat "$DIR/list.err"); asking again" >&2
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
        echo "stop.sh: commands may still run after $DRAIN_SECONDS s; the shutdown ends them" >&2
        break
    fi
    sleep "$DRAIN_POLL_SECONDS"
done
note "{\"step\": \"drain\", \"drained\": $drained, \"seconds\": $(since "$t2")}"

"$DSL41" supervise shutdown --run-root "$RUN_ROOT" || true
note "{\"step\": \"supervisor_shutdown\"}"
exit "$status"
