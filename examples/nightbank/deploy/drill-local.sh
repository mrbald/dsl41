#!/usr/bin/env bash
# drill-local.sh -- run the service drill in a podman container with systemd as PID 1.
#
#   drill-local.sh [--image ubuntu|el8] [ROW...|all]
#
# It builds this checkout's wheel and a newer one (the same tree as version X.post1, for the
# upgrade row), exports the lock's runtime requirements, starts the wargame host
# (scripts/wargame.sh) on the chosen base, delivers the three, and runs each row of
# drill-steps.sh in the container as `runner`. The named rows run in the drill's order, after
# install and start, which every other row needs; `all`, the default, runs every row. The reboot row restarts the container
# between its two halves. After a failed row it runs the diagnostics, and it stops at the
# first failure. It exits 0 only when every row passed.
#
#   DRILL_KEEP=1 drill-local.sh ...   keep the container afterwards; scripts/wargame.sh
#                                     --base BASE join NAME opens a shell in it
#
# The host needs podman, git and uv. PODMAN names the podman command (`sudo podman` for a
# rootful engine). The container runs the host's architecture.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
tree=$(cd "$here/../../.." && pwd)
base=ubuntu
if [ "${1-}" = --image ]; then
    [ "$#" -ge 2 ] || { echo "usage: $0 [--image ubuntu|el8] [ROW...|all]" >&2; exit 2; }
    base=$2
    shift 2
fi
case $base in ubuntu | el8) ;; *) echo "--image must be ubuntu or el8" >&2; exit 2 ;; esac
# no mapfile: macOS ships bash 3.2
read -r -a all <<<"$(bash "$here/drill-steps.sh" --list | tr '\n' ' ')"
for row in "$@"; do
    [ "$row" = all ] || [[ " ${all[*]} " == *" $row "* ]] ||
        { echo "unknown row: $row (rows: ${all[*]})" >&2; exit 2; }
done
# the named rows in the drill's order, after install and start, which every other row needs
rows=()
for row in "${all[@]}"; do
    case " install start $* " in *" $row "* | *" all "*) rows+=("$row") ;; esac
done
[ "$#" != 0 ] || rows=("${all[@]}")
name=drill-$base-$$
# PODMAN is the podman command, as in scripts/wargame.sh
podman() {
    # shellcheck disable=SC2086  # PODMAN is a command and its arguments, split on purpose
    ${PODMAN:-command podman} "$@"
}
wargame() { "$tree/scripts/wargame.sh" --base "$base" "$@"; }
say() { printf 'drill-local: %s\n' "$*"; }

scratch=$(mktemp -d "${TMPDIR:-/tmp}/dsl41-drill.XXXXXX")
# shellcheck disable=SC2329  # the EXIT trap invokes it
cleanup() {
    if [ "${DRILL_KEEP-}" = 1 ]; then
        say "kept the container; scripts/wargame.sh --base $base join $name"
    else
        wargame dispose "$name" 2>/dev/null || true
    fi
    rm -r -f -- "$scratch"
}
trap cleanup EXIT

# The wheel of this tree, the same tree as the next post release, and the lock's runtime
# requirements. The copy is what wargame.sh copies: tracked and unignored files.
say "building the wheels"
dist=$scratch/dist
newer=$scratch/newer
mkdir -p "$dist" "$newer"
(cd "$tree" && git ls-files -z --cached --others --exclude-standard |
    while IFS= read -r -d '' path; do
        if [ -e "$path" ] || [ -L "$path" ]; then printf '%s\0' "$path"; fi
    done | COPYFILE_DISABLE=1 tar --null -T - -cf -) | tar -C "$newer" -xf -
sed -e 's/^version = "\(.*\)"$/version = "\1.post1"/' "$tree/pyproject.toml" >"$newer/pyproject.toml"
grep -q '^version = ".*\.post1"$' "$newer/pyproject.toml" || { say "cannot bump the version"; exit 1; }
(cd "$tree" && env -u VIRTUAL_ENV uv build --quiet --python 3.12 --wheel --out-dir "$dist")
(cd "$newer" && env -u VIRTUAL_ENV uv build --quiet --python 3.12 --wheel --out-dir "$dist")
(cd "$tree" && env -u VIRTUAL_ENV uv export --quiet --frozen --no-emit-project --no-hashes \
    --python 3.12 -o "$dist/requirements.txt")

say "starting the $base host"
wargame build "$name" >"$scratch/build.log" || { cat "$scratch/build.log"; exit 1; }
wargame start "$name"
wargame deliver "$dist" "$name"
container=dsl41-wargame-$name

step() { # step NAME -- one drill-steps.sh entry in the container, as runner
    podman exec --user runner --workdir /work "$container" \
        bash examples/nightbank/deploy/drill-steps.sh "$1"
}
run_row() { # run_row ROW
    case $1 in
        reboot)
            # each command returns on failure: a function called as an `if` condition runs
            # without set -e
            step reboot-before || return 1
            say "restarting the container: systemd shuts down and boots again"
            wargame stop "$name" || return 1
            wargame start "$name" || return 1
            step reboot-after || return 1
            ;;
        *) step "$1" || return 1 ;;
    esac
}

results=()
status=0
start=$SECONDS
for row in "${rows[@]}"; do
    say "row $row"
    t0=$SECONDS
    if run_row "$row"; then
        results+=("passed  $((SECONDS - t0))s  $row")
    else
        results+=("FAILED  $((SECONDS - t0))s  $row")
        status=1
        say "diagnostics"
        step diagnostics || true
        break
    fi
done
say "summary ($base)"
printf '  %s\n' "${results[@]}"
say "$([ "$status" = 0 ] && echo passed || echo FAILED) on $base in $((SECONDS - start))s"
exit "$status"
