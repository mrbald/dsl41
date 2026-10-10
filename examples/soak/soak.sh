#!/bin/sh
# soak.sh -- run the soak estate on the wall clock, detached, for a while.
#
#   uv run examples/soak/soak.sh DIR [MINUTES]
#
# DIR must not exist, and its path must be short (under 70 bytes or so):
# the run root's sockets live in it. It gets the run root (DIR/engine), the engine's log,
# the sampler's samples.jsonl (one line a minute) and soak.env, which
# stop.sh reads. After MINUTES (default 1440, a day) a timer runs stop.sh,
# which seals the period, audits it and stops the supervisor; its output
# goes to DIR/stop.log. Run stop.sh yourself to stop earlier.
#
# The engine, the sampler and the timer are started with nohup, so the
# shell that ran this script may exit. Under `uv run` the project's
# environment is on PATH; set DSL41 and PYTHON to use another one.
set -eu

usage() {
    echo "usage: soak.sh DIR [MINUTES]" >&2
    exit 2
}
[ $# -ge 1 ] && [ $# -le 2 ] || usage
DIR=$1
MINUTES=${2:-1440}
case $MINUTES in '' | *[!0-9]*) usage ;; esac
if [ -e "$DIR" ]; then
    echo "soak.sh: $DIR exists; name a fresh directory" >&2
    exit 2
fi

HERE=$(cd "$(dirname "$0")" && pwd)
DSL41=${DSL41:-$(command -v dsl41)}
PYTHON=${PYTHON:-$(command -v python)}
mkdir -p "$DIR"
DIR=$(cd "$DIR" && pwd)
RUN_ROOT=$DIR/engine
ESTATE=$HERE/estate/soak.jil
# a unix socket path holds at most 104 bytes on macOS, 108 on Linux
if [ ${#RUN_ROOT} -gt 80 ]; then
    rmdir "$DIR"
    echo "soak.sh: $RUN_ROOT is too long for the run root's sockets; use a shorter DIR" >&2
    exit 2
fi

cat >"$DIR/soak.env" <<EOF
DSL41='$DSL41'
PYTHON='$PYTHON'
RUN_ROOT='$RUN_ROOT'
ESTATE='$ESTATE'
EOF

# the run line: the estate's machine is localhost, every schedule is UTC
nohup "$DSL41" run "$ESTATE" \
    --run-root "$RUN_ROOT" \
    --detached \
    --as-machine localhost \
    --timezone UTC \
    >"$DIR/engine.log" 2>&1 &
echo $! >"$DIR/engine.pid"
ENGINE_PID=$(cat "$DIR/engine.pid")

# the engine is ready when its control socket answers
tries=0
until "$DSL41" query status --brief -S "$RUN_ROOT/control.sock" >/dev/null 2>&1; do
    tries=$((tries + 1))
    if [ "$tries" -gt 60 ] || ! kill -0 "$ENGINE_PID" 2>/dev/null; then
        echo "soak.sh: the engine did not come up; see $DIR/engine.log" >&2
        exit 1
    fi
    sleep 1
done

nohup "$PYTHON" "$HERE/sample.py" --run-root "$RUN_ROOT" --pid "$ENGINE_PID" \
    --out "$DIR/samples.jsonl" >"$DIR/sample.log" 2>&1 &
echo $! >"$DIR/sample.pid"

# the timer's arguments are positional on purpose: nothing expands twice
# shellcheck disable=SC2016
nohup sh -c 'sleep "$1" && exec "$2" "$3"' timer "$((MINUTES * 60))" \
    "$HERE/stop.sh" "$DIR" >"$DIR/stop.log" 2>&1 &
echo $! >"$DIR/timer.pid"

echo "soak.sh: engine $ENGINE_PID on $RUN_ROOT for $MINUTES minutes"
echo "soak.sh: samples in $DIR/samples.jsonl; stop early with $HERE/stop.sh $DIR"
