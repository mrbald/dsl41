#!/bin/sh
# PreToolUse hook for Bash: read the tool input as JSON on stdin and print a
# permission decision. No output means the command may go on.
#   deny: commands that discard work, stage everything, force-push without a lease,
#         or record a state that still holds merge-conflict markers.
#   ask:  commands that delete a branch or a worktree by force.
# The marker pattern is the one in tests/test_docs_hygiene.py (DL-237).
# A shell tokenizer (Python's shlex) reads the command: heredoc bodies and quoted
# words that hold whitespace never reach a rule, and quotes around a flag do not hide it.
# --force and a + refspec both override --force-with-lease (git-push(1)).
set -u

command -v jq >/dev/null 2>&1 || { echo "bash_guard: jq is not on PATH; no guard ran." >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "bash_guard: python3 is not on PATH; no guard ran." >&2; exit 1; }

input=$(cat)
cmd=$(printf '%s' "$input" | jq -r 'select(.tool_name == "Bash") | .tool_input.command // empty')
[ -n "$cmd" ] || exit 0

start=$(printf '%s' "$input" | jq -r '.cwd // empty')
[ -n "$start" ] || start=${CLAUDE_PROJECT_DIR:-.}

MARKER='^((<{7}|>{7}|\|{7})( |$)|={7}$)'
TAB=$(printf '\t')

# Print one line per git or gh command: the directory it runs in, a tab, then its
# words. Wrappers (env, sudo, command, ...), VAR=value words and git's global options
# are dropped. A word that holds whitespace prints as "".
NORMALISE='
import os, re, shlex, sys

PUNCT = "();<>|&\n"
ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
HEREDOC = re.compile(r"(?<!<)<<(-?)[ \t]*([\x27\x22]?)([A-Za-z0-9_.-]+)\2")
RESERVED = {"{", "}", "!", "if", "then", "else", "elif", "do", "while", "until", "time", "nohup"}
# wrapper: its options that take a separate argument
WRAPPERS = {
    "env": {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"},
    "command": set(),
    "exec": {"-a"},
    "nice": {"-n", "--adjustment"},
    "caffeinate": {"-t", "-w"},
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-U", "-T", "-R"},
}
GIT_ARG = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env"}


def drop_heredocs(text):
    out, pending = [], []
    for line in text.split("\n"):
        if pending:
            strip_tabs, word = pending[0]
            if (line.lstrip("\t") if strip_tabs else line) == word:
                pending.pop(0)
            continue
        out.append(line)
        pending = [(m.group(1) == "-", m.group(3)) for m in HEREDOC.finditer(line)]
    return "\n".join(out)


def tokens(text):
    for attempt in (text, re.sub(r"[\x27\x22\\]", "", text)):
        lex = shlex.shlex(attempt, posix=True, punctuation_chars=PUNCT)
        lex.whitespace = " \t\r"
        lex.whitespace_split = True
        lex.commenters = ""
        try:
            return list(lex)
        except ValueError:
            continue
    return []


def segments(toks):
    seg, skip = [], False
    for tok in toks:
        if tok and all(c in PUNCT for c in tok):
            if ("<" in tok or ">" in tok) and "(" not in tok and ")" not in tok:
                skip = True
                continue
            yield seg
            seg = []
            continue
        if skip:
            skip = False
            continue
        seg.append(tok)
    yield seg


def unwrap(words):
    while words:
        head = words[0]
        if head in RESERVED or ASSIGN.match(head):
            words = words[1:]
            continue
        if head not in WRAPPERS:
            return words
        takes = WRAPPERS[head]
        words = words[1:]
        while words and (words[0].startswith("-") or (head == "env" and ASSIGN.match(words[0]))):
            opt = words.pop(0)
            if opt == "--":
                break
            if opt in takes and words:
                words.pop(0)
    return words


def strip_git_options(words, where):
    i = 1
    while i < len(words) and words[i].startswith("-"):
        word = words[i]
        if word in GIT_ARG and i + 1 < len(words):
            if word == "-C":
                where = os.path.join(where, os.path.expanduser(words[i + 1]))
            i += 2
            continue
        if word.startswith("-C") and len(word) > 2:
            where = os.path.join(where, os.path.expanduser(word[2:]))
        i += 1
    return ["git"] + words[i:], where


def strip_gh_repo(words):
    out, i = [], 0
    while i < len(words):
        if words[i] in ("-R", "--repo") and i + 1 < len(words):
            i += 2
            continue
        if words[i].startswith("--repo=") or (words[i].startswith("-R") and len(words[i]) > 2):
            i += 1
            continue
        out.append(words[i])
        i += 1
    return out


def shown(word):
    return "\"\"" if word == "" or any(c.isspace() for c in word) else word


def main():
    text = drop_heredocs(sys.stdin.read()).replace("\\\n", "")
    here = sys.argv[1]
    for seg in segments(tokens(text)):
        words = list(seg)
        for at, word in enumerate(words):
            if word.startswith("#"):
                words = words[:at]
                break
        words = unwrap(words)
        if not words:
            continue
        if words[0] == "cd" and len(words) > 1:
            target = os.path.join(here, os.path.expanduser(words[1]))
            if os.path.isdir(target):
                here = target
            continue
        where = here
        if words[0] == "git":
            words, where = strip_git_options(words, here)
        elif words[0] == "gh":
            words = strip_gh_repo(words)
        else:
            continue
        print(where + "\t" + " ".join(shown(w) for w in words))


main()
'

emit() {
    jq -n --arg decision "$1" --arg reason "$2" \
        '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: $decision,
          permissionDecisionReason: $reason}}'
}

# $1 a command, $2 an extended regex: some word of the command matches it whole.
has() {
    printf '%s\n' "$1" | tr ' ' '\n' | grep -E -q -x -e "$2"
}

# A short-option cluster that holds the letter $1, such as -f or -uf.
short() {
    printf -- '-[A-Za-z]*%s[A-Za-z]*' "$1"
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
            case "$word" in -* | '""') continue ;; esac
            git ls-files -m -o -u --exclude-standard -- "$word" 2>/dev/null | sed "s|^|$top/|"
        done | first_marked
    )
}

segments=$(printf '%s' "$cmd" | python3 -c "$NORMALISE" "$start") || {
    echo "bash_guard: the command could not be read; no guard ran." >&2
    exit 1
}

ask_reason=
while IFS="$TAB" read -r where seg; do
    [ -n "$seg" ] || continue

    case "$seg" in
    "git pull" | "git pull "*)
        emit deny "git pull is not used here: run git fetch, then git rebase."
        exit 0
        ;;
    "git checkout" | "git checkout "*)
        emit deny "git checkout can discard work: use git switch, or edit the file by hand."
        exit 0
        ;;
    "git restore" | "git restore "*)
        # --staged alone only unstages; the worktree stays as it is.
        if ! has "$seg" "--staged|$(short S)" || has "$seg" "--worktree|$(short W)"; then
            emit deny "git restore can discard work: use git restore --staged, or edit the file by hand."
            exit 0
        fi
        ;;
    "git clean" | "git clean "*)
        if ! has "$seg" "--dry-run|$(short n)"; then
            emit deny "git clean deletes untracked files: preview them with git clean -n."
            exit 0
        fi
        ;;
    "git switch "*)
        if has "$seg" "--discard-changes|--force|$(short f)"; then
            emit deny "git switch --discard-changes discards work: commit or move the changes first."
            exit 0
        fi
        ;;
    "git reset "*)
        if has "$seg" "--hard"; then
            emit deny "git reset --hard discards work: use git switch or a new commit."
            exit 0
        fi
        ;;
    "git add "*)
        if has "$seg" "--all|$(short A)|\\.|\\./|:/|:/\\.|\\*"; then
            emit deny "Stage explicit paths: git add -A, git add . and git add :/ pick up untracked scratch."
            exit 0
        fi
        ;;
    "git push "*)
        if has "$seg" "--force|$(short f)|\\+.+"; then
            emit deny "A forced push needs --force-with-lease, without --force or a + refspec."
            exit 0
        fi
        if has "$seg" "--delete|$(short d)|:.+"; then
            ask_reason="This push deletes a remote branch."
        fi
        ;;
    "git branch "*)
        if has "$seg" "$(short D)" ||
            { has "$seg" "--delete|$(short d)" && has "$seg" "--force|$(short f)"; }; then
            ask_reason="git branch -D deletes a branch that may be unmerged."
        fi
        ;;
    "git worktree remove "*)
        if has "$seg" "--force|$(short f)"; then
            ask_reason="git worktree remove --force drops uncommitted work in the worktree."
        fi
        ;;
    "gh pr merge"*)
        if has "$seg" "--delete-branch|--delete-branch=true|$(short d)"; then
            ask_reason="--delete-branch closes any pull request stacked on this branch."
        fi
        ;;
    esac

    case "$seg" in
    "git commit" | "git commit "* | "git rebase "*--continue* | "git cherry-pick "*--continue* | \
        "git add "*)
        bad=$(unmerged_marked "$where") || bad=
        if [ -z "$bad" ]; then
            case "$seg" in
            "git add "*) bad=$(named_marked "$where" "${seg#git add }") || bad= ;;
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
