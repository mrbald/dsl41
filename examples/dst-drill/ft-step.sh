#!/bin/sh
# ft-step.sh SPEC_FILE COMMAND...
#
# Run COMMAND on a wall clock that SPEC_FILE sets and that can be stepped.
# The file holds one libfaketime spec, for example "-9476505", an offset in
# seconds from the real clock. Rewriting the file steps the wall clock of every
# process started here, at its next read. The monotonic clock is not faked:
# a step of the wall clock leaves it alone, as `date -s` does (DL-435). The
# rate is 1, so the two clocks agree between steps. Needs libfaketime.
set -eu

if [ $# -lt 2 ]; then
    echo "usage: ft-step.sh SPEC_FILE COMMAND..." >&2
    exit 2
fi
spec=$1
shift
lib=$(find /usr/lib -name libfaketime.so.1 | head -n 1)
if [ -z "$lib" ]; then
    echo "ft-step.sh: libfaketime.so.1 not found under /usr/lib" >&2
    exit 2
fi
LD_PRELOAD=$lib
FAKETIME_TIMESTAMP_FILE=$spec
FAKETIME_NO_CACHE=1
FAKETIME_DONT_FAKE_MONOTONIC=1
export LD_PRELOAD FAKETIME_TIMESTAMP_FILE FAKETIME_NO_CACHE FAKETIME_DONT_FAKE_MONOTONIC
exec "$@"
