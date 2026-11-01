#!/bin/sh
# ft.sh START SPEED COMMAND...
#
# Run COMMAND on a fake clock. The clock reads START (a UTC time in ISO form,
# for example 2026-03-29T00:20:00) now and runs SPEED times as fast as the
# real one. Children inherit it and share one timeline. Monotonic time is
# faked too: asyncio needs both clocks to agree. Needs libfaketime and GNU date.
set -eu

if [ $# -lt 3 ]; then
    echo "usage: ft.sh START SPEED COMMAND..." >&2
    exit 2
fi
start=$1
speed=$2
shift 2
lib=$(find /usr/lib -name libfaketime.so.1 | head -n 1)
if [ -z "$lib" ]; then
    echo "ft.sh: libfaketime.so.1 not found under /usr/lib" >&2
    exit 2
fi
offset=$(($(date -u -d "$start" +%s) - $(date -u +%s)))
LD_PRELOAD=$lib
FAKETIME="$(printf '%+d' "$offset") x$speed"
FAKETIME_NO_CACHE=1
FAKETIME_DONT_FAKE_MONOTONIC=0
export LD_PRELOAD FAKETIME FAKETIME_NO_CACHE FAKETIME_DONT_FAKE_MONOTONIC
exec "$@"
