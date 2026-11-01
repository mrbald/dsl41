#!/usr/bin/env bash
# A disposable Linux host for service exercises: a podman container running systemd as PID 1,
# built from scripts/wargame.Containerfile on Ubuntu 24.04 or Rocky Linux 8. /work holds this
# checkout's files, owned by `runner`, a user with passwordless sudo; /delivery holds what
# `deliver` copied in. The container has the network, so an install inside it can reach PyPI.
# examples/nightbank/deploy/drill-local.sh runs the service drill on it.
#
#   scripts/wargame.sh [--base ubuntu|el8] build   [NAME]       build the image
#   scripts/wargame.sh [--base ubuntu|el8] start   [NAME]       run (or restart) the container,
#                                                               wait for systemd, copy the
#                                                               checkout to /work if absent
#   scripts/wargame.sh [--base ubuntu|el8] deliver PATH [NAME]  copy PATH to /delivery/,
#                                                               replacing an earlier copy
#   scripts/wargame.sh [--base ubuntu|el8] join    [NAME]       a shell inside it
#   scripts/wargame.sh [--base ubuntu|el8] stop    [NAME]       stop it, keeping its state
#   scripts/wargame.sh [--base ubuntu|el8] dispose [NAME]       remove the container
#
# --base defaults to ubuntu. NAME defaults to this checkout's directory name and the base, so
# two checkouts never share a container; the image is named the same way, whatever NAME is.
# Images and containers carry the checkout path as a label, and the script refuses one made
# from another checkout. `stop` keeps state, and `start`
# of a stopped container keeps /work: systemd boots again, as on a host reboot.
set -euo pipefail

usage() {
    echo "usage: $0 [--base ubuntu|el8] build|start|join|stop|dispose [NAME]" \
        "| deliver PATH [NAME]" >&2
    exit 2
}
base=ubuntu
if [ "${1-}" = --base ]; then
    [ "$#" -ge 2 ] || usage
    base=$2
    shift 2
fi
case $base in
    # pinned by the registry's index digest (an OCI image index), so the pin names the same
    # image on arm64 and on amd64
    ubuntu) from=docker.io/library/ubuntu@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55 ;;
    el8) from=docker.io/library/rockylinux@sha256:9794037624aaa6212aeada1d28861ef5e0a935adaf93e4ef79837119f2a2d04c ;;
    *) usage ;;
esac
[ "$#" -ge 1 ] || usage
verb=$1
shift
delivery=
case $verb in
    build | start | join | stop | dispose) [ "$#" -le 1 ] || usage ;;
    deliver)
        [ "$#" -ge 1 ] && [ "$#" -le 2 ] || usage
        delivery=$1
        shift
        [ -e "$delivery" ] || { echo "nothing to deliver at $delivery" >&2; exit 2; }
        ;;
    *) usage ;;
esac
here=$(CDPATH='' cd "$(dirname "$0")" && pwd -P)
tree=$(CDPATH='' cd "$here/.." && pwd -P)
name=${1:-$(basename "$tree")-$base}
[[ $name =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] || {
    echo 'NAME must start with a letter or digit and contain only letters, digits, _, . or -' >&2
    exit 2
}
container=dsl41-wargame-$name
# one image per checkout and base, which every container of that base runs
image=localhost/dsl41-wargame:$(basename "$tree")-$base

# PODMAN is the podman command, `podman` by default; CI sets `sudo podman` for a rootful engine
podman() {
    # shellcheck disable=SC2086  # PODMAN is a command and its arguments, split on purpose
    ${PODMAN:-command podman} "$@"
}
podman info >/dev/null

# Refuse an unrelated container or image even when its name matches.
if podman container exists "$container"; then
    owner=$(podman inspect --format '{{index .Config.Labels "dsl41.wargame"}}' "$container")
    [ "$owner" = "$tree" ] || { echo "foreign container: $container" >&2; exit 1; }
fi
if podman image exists "$image"; then
    owner=$(podman image inspect --format '{{index .Labels "dsl41.wargame"}}' "$image")
    [ "$owner" = "$tree" ] || { echo "foreign image: $image" >&2; exit 1; }
fi

running() {
    podman container exists "$container" &&
        [ "$(podman container inspect --format '{{.State.Running}}' "$container")" = true ]
}

case $verb in
    build)
        podman build --tag "$image" --label "dsl41.wargame=$tree" --build-arg "BASE=$from" \
            --build-arg "TARGET=$base" --file "$here/wargame.Containerfile" "$here"
        ;;
    start)
        if podman container exists "$container"; then
            podman start "$container" >/dev/null
        else
            # 2 CPUs and 3 GB leave room in a shared podman VM. SYS_PTRACE lets root read
            # /proc/PID/environ of another user's process, which rootless podman's root lacks.
            podman run --detach --name "$container" --label "dsl41.wargame=$tree" \
                --systemd=always --cap-add SYS_PTRACE --cpus 2 --memory 3g \
                "$image" >/dev/null
        fi
        state=
        for _ in $(seq 90); do
            state=$(podman exec "$container" systemctl is-system-running 2>/dev/null || true)
            case $state in running | degraded) break ;; esac
            sleep 1
        done
        case $state in
            running | degraded) ;;
            *) echo "systemd did not become ready in $container: ${state:-no answer}" >&2; exit 1 ;;
        esac
        # /work is copied only while absent, so a restart keeps it. The copy is the tracked
        # files and the untracked ones git does not ignore, minus those the working tree
        # deleted; it extracts to /work.new and is renamed, so a failure leaves no /work.
        if ! podman exec "$container" test -d /work; then
            podman exec "$container" rm -r -f -- /work.new
            podman exec "$container" mkdir /work.new
            (cd "$tree" && git ls-files -z --cached --others --exclude-standard |
                while IFS= read -r -d '' path; do
                    if [ -e "$path" ] || [ -L "$path" ]; then printf '%s\0' "$path"; fi
                done | COPYFILE_DISABLE=1 tar --null -T - -cf -) |
                podman exec -i "$container" tar -C /work.new --warning=no-unknown-keyword -xf -
            podman exec "$container" bash -c 'chown -R runner:runner /work.new && mv /work.new /work'
        fi
        ;;
    deliver)
        running || { echo "container $container is not running; run: $0 --base $base start $name" >&2; exit 2; }
        item=$(basename "$delivery")
        case $item in . | .. | /) echo "cannot deliver $delivery" >&2; exit 2 ;; esac
        podman exec "$container" mkdir -p /delivery
        podman exec "$container" rm -r -f -- "/delivery/$item"
        podman cp "$delivery" "$container:/delivery/$item"
        podman exec "$container" chown -R runner:runner "/delivery/$item"
        ;;
    join) exec podman exec -it "$container" bash ;;
    stop) podman stop --time 60 "$container" >/dev/null ;;
    dispose) podman rm --force "$container" >/dev/null ;;
esac
