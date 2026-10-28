#!/bin/sh
# Run one Codex peer pass over BRIEF and keep its thread id and last message.
# Usage: scripts/peer.sh MODEL BRIEF OUT SECONDS [--resume THREAD] [--effort LEVEL]
#        [--sandbox MODE]
# Writes OUT.jsonl, OUT.err, OUT.thread and OUT.md. Run from inside a git repo.
# Exit: 64 bad arguments or a brief without the no-other-model line, 65 not in a
# git repo, 70 no agent message in the output.
set -u

usage='usage: scripts/peer.sh MODEL BRIEF OUT SECONDS [--resume THREAD] [--effort LEVEL] [--sandbox MODE]'

[ $# -ge 4 ] || { echo "$usage" >&2; exit 64; }
MODEL=$1
BRIEF=$2
OUT=$3
SECONDS_LIMIT=$4
shift 4

RESUME=
EFFORT=
SANDBOX=read-only
while [ $# -gt 0 ]; do
    [ $# -ge 2 ] || { echo "$usage" >&2; exit 64; }
    case "$1" in
    --resume) RESUME=$2 ;;
    --effort) EFFORT=$2 ;;
    --sandbox) SANDBOX=$2 ;;
    *) echo "$usage" >&2; exit 64 ;;
    esac
    shift 2
done

if [ -z "$EFFORT" ]; then
    if [ -n "$RESUME" ]; then EFFORT=high; else EFFORT=xhigh; fi
fi

case "$SECONDS_LIMIT" in
'' | *[!0-9]*) echo "peer.sh: SECONDS must be a whole number." >&2; exit 64 ;;
esac
case "$SANDBOX" in
read-only | workspace-write | danger-full-access) ;;
*) echo "peer.sh: unknown sandbox mode $SANDBOX." >&2; exit 64 ;;
esac
[ -f "$BRIEF" ] || { echo "peer.sh: no brief at $BRIEF." >&2; exit 64; }
grep -F -q 'call no other model' "$BRIEF" || {
    echo "peer.sh: the brief must say \"call no other model\"." >&2
    exit 64
}
git rev-parse --git-dir >/dev/null 2>&1 || {
    echo "peer.sh: run from inside a git repo." >&2
    exit 65
}

here=$(cd "$(dirname "$0")" && pwd)
effort_config="model_reasoning_effort=\"$EFFORT\""

run() {
    if command -v caffeinate >/dev/null 2>&1; then
        caffeinate -i perl -e 'alarm shift; exec @ARGV or die $!' -- "$SECONDS_LIMIT" "$@"
    else
        perl -e 'alarm shift; exec @ARGV or die $!' -- "$SECONDS_LIMIT" "$@"
    fi
}

if [ -n "$RESUME" ]; then
    run codex exec resume "$RESUME" --json -c "$effort_config" \
        -c "sandbox_mode=\"$SANDBOX\"" - <"$BRIEF" >"$OUT.jsonl" 2>"$OUT.err"
else
    run codex exec --sandbox "$SANDBOX" --json -m "$MODEL" -c "$effort_config" \
        - <"$BRIEF" >"$OUT.jsonl" 2>"$OUT.err"
fi
status=$?
[ "$status" -eq 0 ] || echo "peer.sh: codex exited $status; see $OUT.err." >&2

python3 "$here/peer_extract.py" "$OUT.jsonl" "$OUT" ${RESUME:+--thread "$RESUME"}
