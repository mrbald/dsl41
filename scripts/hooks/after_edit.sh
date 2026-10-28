#!/bin/sh
# PostToolUse hook for Write and Edit: check the edited file and report findings as
# additional context. It never blocks.
#   Python under src, tests, scripts or examples: ruff check, ruff format --check, mypy.
#   README.md or Markdown under docs/: re-render docs/decision-index.md (DL-NNN
#   citations change it).
# mypy runs one-shot: after an edit it takes about 0.17 s here, against 0.10 s for
# dmypy, and it leaves no daemon or .dmypy.json behind.
set -u

command -v jq >/dev/null 2>&1 || { echo "after_edit: jq is not on PATH; no check ran." >&2; exit 1; }

input=$(cat)
file=$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty')
[ -n "$file" ] || exit 0
root=${CLAUDE_PROJECT_DIR:-}
[ -n "$root" ] || exit 0
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
