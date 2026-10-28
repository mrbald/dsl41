#!/bin/sh
# PreToolUse hook for Bash: read the tool input as JSON on stdin and print a
# permission decision. No output means the command may go on.
#   deny: commands that discard work, stage everything, force-push without a lease,
#         or record a state that still holds merge-conflict markers.
#   ask:  commands that delete a branch or a worktree by force.
# The marker pattern is the one in tests/test_docs_hygiene.py (DL-237).
set -u

command -v jq >/dev/null 2>&1 || { echo "bash_guard: jq is not on PATH; no guard ran." >&2; exit 1; }

input=$(cat)
cmd=$(printf '%s' "$input" | jq -r 'select(.tool_name == "Bash") | .tool_input.command // empty')
[ -n "$cmd" ] || exit 0

start=$(printf '%s' "$input" | jq -r '.cwd // empty')
[ -n "$start" ] || start=${CLAUDE_PROJECT_DIR:-.}

MARKER='^((<{7}|>{7}|\|{7})( |$)|={7}$)'
NL='
'

emit() {
    jq -n --arg decision "$1" --arg reason "$2" \
        '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: $decision,
          permissionDecisionReason: $reason}}'
}

# One command per line: split at && || ; | and newlines.
split_commands() {
    sed -E "s/&&|\\|\\||;|\\|/\\${NL}/g"
}

# Blank the quoted strings, so that a commit message cannot trigger a rule.
blank_quotes() {
    sed -E "s/\"[^\"]*\"/\"\"/g; s/'[^']*'/''/g"
}

# The first file named on stdin that holds a conflict marker.
first_marked() {
    while IFS= read -r name; do
        [ -n "$name" ] || continue
        if [ -f "$name" ] && grep -I -E -q "$MARKER" "$name" 2>/dev/null; then
            printf '%s\n' "$name"
            return 0
        fi
    done
    return 1
}

# $1 directory: an unmerged file that still holds a marker.
unmerged_marked() {
    top=$(git -C "$1" rev-parse --show-toplevel 2>/dev/null) || return 1
    git -C "$1" diff --name-only --diff-filter=U 2>/dev/null |
        sed "s|^|$top/|" | first_marked
}

# $1 directory, $2 the words after `git add`: a named file that holds a marker.
named_marked() {
    (
        cd "$1" 2>/dev/null || exit 1
        top=$(git rev-parse --show-toplevel 2>/dev/null) || exit 1
        set -f
        # shellcheck disable=SC2086
        set -- $2
        for word in "$@"; do
            case "$word" in -*) continue ;; esac
            word=$(printf '%s' "$word" | tr -d "\"'")
            git ls-files -m -o -u --exclude-standard -- "$word" 2>/dev/null | sed "s|^|$top/|"
        done | first_marked
    )
}

dir=$start
ask_reason=
segments=$(printf '%s\n' "$cmd" | blank_quotes | split_commands)
originals=$(printf '%s\n' "$cmd" | split_commands)

while IFS= read -r seg; do
    seg=$(printf '%s' "$seg" | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//')
    # leading VAR=value words
    while :; do
        case "$seg" in
        [A-Za-z_]*=*\ *) seg=${seg#* } ;;
        *) break ;;
        esac
    done
    case "$seg" in
    cd\ *)
        target=$(printf '%s' "${seg#cd }" | tr -d "\"'")
        case "$target" in
        /*) [ -d "$target" ] && dir=$target ;;
        *) [ -d "$dir/$target" ] && dir="$dir/$target" ;;
        esac
        continue
        ;;
    esac
    case "$seg" in git | git\ * | gh\ pr\ merge*) ;; *) continue ;; esac

    where=$dir
    c=$(printf '%s' "$seg" | sed -nE 's/^git +-C +([^ ]+).*/\1/p')
    if [ -n "$c" ]; then
        case "$c" in
        /*) where=$c ;;
        *) where="$dir/$c" ;;
        esac
    fi
    seg=$(printf '%s' "$seg" |
        sed -E 's/^git( +(-C|-c|--git-dir|--work-tree) +[^ ]+| +--[a-z-]+=[^ ]+)*/git/')

    if printf '%s' "$seg" | grep -E -q '^git (restore|checkout|clean|pull)( |$)'; then
        verb=${seg#git }
        verb=${verb%% *}
        case "$verb" in
        pull) emit deny "git pull is not used here: run git fetch, then git rebase." ;;
        *) emit deny "git $verb can discard work: use git switch, or edit the file by hand." ;;
        esac
        exit 0
    fi
    if printf '%s' "$seg" | grep -E -q '^git reset( .*)? --hard( |$)'; then
        emit deny "git reset --hard discards work: use git switch or a new commit."
        exit 0
    fi
    if printf '%s' "$seg" | grep -E -q '^git add( .*)? (-A|--all|\.)( |$)'; then
        emit deny "Stage explicit paths: git add -A and git add . pick up untracked scratch."
        exit 0
    fi
    if printf '%s' "$seg" | grep -E -q '^git push( |$)'; then
        if printf '%s' "$seg" | grep -E -q '( |^)(--force|-[A-Za-z]*f[A-Za-z]*)( |$)' &&
            ! printf '%s' "$seg" | grep -q -e '--force-with-lease'; then
            emit deny "A forced push needs --force-with-lease."
            exit 0
        fi
        if printf '%s' "$seg" | grep -E -q '( |^)(--delete|-d)( |$)|( |^):[^ ]'; then
            ask_reason="This push deletes a remote branch."
        fi
    fi
    if printf '%s' "$seg" | grep -E -q '^git branch( .*)? -D( |$)'; then
        ask_reason="git branch -D deletes a branch that may be unmerged."
    fi
    if printf '%s' "$seg" | grep -E -q '^git worktree remove( .*)? (--force|-f)( |$)'; then
        ask_reason="git worktree remove --force drops uncommitted work in the worktree."
    fi
    if printf '%s' "$seg" | grep -E -q '^gh pr merge( .*)? --delete-branch'; then
        ask_reason="--delete-branch closes any pull request stacked on this branch."
    fi

    case "$seg" in
    "git commit" | "git commit "* | "git rebase "*--continue* | "git cherry-pick "*--continue* | \
        "git add "*)
        bad=$(unmerged_marked "$where") || bad=
        if [ -z "$bad" ]; then
            case "$seg" in
            "git add "*)
                # the named paths, read from the unblanked command
                bad=$(printf '%s\n' "$originals" |
                    sed -nE 's/^.*git( +(-C|-c) +[^ ]+)* +add +//p' |
                    while IFS= read -r words; do
                        named_marked "$where" "$words" && break
                    done) || bad=
                ;;
            esac
        fi
        if [ -n "$bad" ]; then
            emit deny "$bad still holds a merge-conflict marker; resolve it first."
            exit 0
        fi
        ;;
    esac
done <<EOF2
$segments
EOF2

[ -z "$ask_reason" ] || emit ask "$ask_reason"
exit 0
