#!/bin/sh
# PostToolUse hook for Write, Edit and MultiEdit: check the edited file and report
# findings as additional context. It never blocks.
#   Python under src, tests, scripts or examples: ruff check, ruff format --check, mypy.
#   README.md or Markdown under docs/: re-render docs/decision-index.md (DL-NNN
#   citations change it).
# The checkout is the one that holds the file, so an edit in a worktree is checked in
# that worktree. A file outside a dsl41 checkout is not checked.
# mypy runs one-shot: after an edit it takes about 0.17 s here, against 0.10 s for
# dmypy, and it leaves no daemon or .dmypy.json behind.
set -u

command -v jq >/dev/null 2>&1 || { echo "after_edit: jq is not on PATH; no check ran." >&2; exit 1; }

realpath_of() {
    python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$1"
}

input=$(cat)
file=$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty')
[ -n "$file" ] || exit 0
file=$(realpath_of "$file") || exit 0
dir=$(dirname "$file")
[ -d "$dir" ] || exit 0
root=$(git -C "$dir" rev-parse --show-toplevel 2>/dev/null) || exit 0
root=$(realpath_of "$root") || exit 0
[ -f "$root/scripts/hooks/after_edit.sh" ] || exit 0
cd "$root" || exit 0

report=
add() {
    report="$report$1
"
}

# Run a checker; keep its output as a finding when it fails.
check() {
    out=$(env -u VIRTUAL_ENV uv run "$@" 2>&1) || add "$*:
$out"
}

case "$file" in
"$root"/src/*.py | "$root"/tests/*.py | "$root"/scripts/*.py | "$root"/examples/*.py)
    [ -f "$file" ] || exit 0
    check ruff check --quiet "$file"
    check ruff format --check --quiet "$file"
    check mypy "$file"
    ;;
"$root"/README.md | "$root"/docs/*.md)
    check python scripts/render_decision_index.py
    if ! git -C "$root" diff --quiet -- docs/decision-index.md; then
        add "decision index re-rendered: stage docs/decision-index.md"
    fi
    ;;
*) exit 0 ;;
esac

[ -n "$report" ] || exit 0
jq -n --arg context "$report" \
    '{hookSpecificOutput: {hookEventName: "PostToolUse", additionalContext: $context}}'
exit 0
