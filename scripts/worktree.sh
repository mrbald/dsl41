#!/bin/sh
# Create a synced worktree for BRANCH and check its venv imports its own source.
# The shell's VIRTUAL_ENV points at the main checkout; a sync that follows it
# repoints the main editable install (worktree trap).
# The synced .venv is an ignored file, so plain `wt remove` refuses the worktree.
set -eu

usage='usage: scripts/worktree.sh BRANCH [--base REF]
Remove the worktree later with: wt remove --discard-ignored BRANCH'

BASE=origin/main
BRANCH=
while [ $# -gt 0 ]; do
    case "$1" in
    --base)
        [ $# -ge 2 ] || { echo "$usage" >&2; exit 64; }
        BASE=$2
        shift 2
        ;;
    -*)
        echo "$usage" >&2
        exit 64
        ;;
    *)
        [ -z "$BRANCH" ] || { echo "$usage" >&2; exit 64; }
        BRANCH=$1
        shift
        ;;
    esac
done
[ -n "$BRANCH" ] || { echo "$usage" >&2; exit 64; }

command -v wt >/dev/null 2>&1 || { echo "worktree.sh: wt is not on PATH." >&2; exit 2; }

git fetch origin -q
wt add "$BRANCH" --base "$BASE" >&2
p=$(wt path "$BRANCH")
cd "$p"
env -u VIRTUAL_ENV -u UV_PROJECT_ENVIRONMENT -u UV_PROJECT uv sync --frozen --extra dev >&2
where=$(env -u VIRTUAL_ENV uv run python -c 'import dsl41, sys; print(dsl41.__file__)')
case "$where" in
"$p"/*) ;;
*)
    echo "worktree.sh: dsl41 imports from $where, not from $p." >&2
    exit 3
    ;;
esac
echo "$p"
