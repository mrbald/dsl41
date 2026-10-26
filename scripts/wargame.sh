#!/usr/bin/env bash
# A disposable EL8 host for exercises: a Rocky Linux 8 container under podman, running
# systemd (239, the RHEL 8 family) as PID 1, with python3.12 and uv. The running
# container has no network. /work holds this checkout's tracked files, owned by
# `runner`, a user with passwordless sudo; /delivery/bundle holds a delivered bundle.
# Run the service drill on the image with
# DRILL_BASE_IMAGE=localhost/dsl41-wargame:NAME examples/nightbank/deploy/drill-local.sh,
# or run service exercises in it by hand.
#
# The image has two layers. The base is examples/nightbank/deploy/offline.Containerfile,
# built as the EL8 rehearsal lane builds it; that file asserts uv is absent, because the
# rehearsal lanes prove an install that needs no uv. scripts/wargame.Containerfile adds
# uv on top, from PYTHON_INDEX (default PyPI).
#
#   scripts/wargame.sh build   [NAME]       build the image
#   scripts/wargame.sh start   [NAME]       run (or restart) the container, wait for
#                                           systemd, and copy the checkout to /work
#   scripts/wargame.sh deliver DIR [NAME]   copy the prepared delivery bundle DIR to
#                                           /delivery/bundle, replacing any earlier one
#   scripts/wargame.sh join    [NAME]       a shell inside it
#   scripts/wargame.sh stop    [NAME]       stop it, keeping its state
#   scripts/wargame.sh dispose [NAME]       remove it
#
# NAME defaults to this checkout's directory name, so two checkouts never share a
# container; images and containers carry the checkout path as a label and the script
# refuses to touch one made from another checkout.
set -euo pipefail

usage() { echo "usage: $0 build|start|join|stop|dispose [NAME] | deliver DIR [NAME]" >&2; exit 2; }
[ "$#" -ge 1 ] || usage
verb=$1
shift
case $verb in
    build|start|join|stop|dispose) [ "$#" -le 1 ] || usage ;;
    deliver)
        [ "$#" -ge 1 ] && [ "$#" -le 2 ] || usage
        bundle=$1
        shift
        [ -f "$bundle/tools/prepare.sh" ] || {
            echo "not a prepared delivery bundle (no tools/prepare.sh): $bundle" >&2
            exit 2
        }
        ;;
    *) usage ;;
esac
here=$(CDPATH='' cd "$(dirname "$0")" && pwd -P)
tree=$(CDPATH='' cd "$here/.." && pwd -P)
deploy=$tree/examples/nightbank/deploy
name=${1:-$(basename "$tree")}
[[ $name =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] || {
    echo 'NAME must start with a letter or digit and contain only letters, digits, _, . or -' >&2
    exit 2
}
container=dsl41-wargame-$name
image=localhost/dsl41-wargame:$name
base_image=localhost/dsl41-wargame-base:$name

podman info >/dev/null

# Refuse unrelated containers even when their names match.
if podman container exists "$container"; then
    owner=$(podman inspect --format '{{index .Config.Labels "dsl41.wargame"}}' "$container")
    [ "$owner" = "$tree" ] || { echo "foreign container: $container" >&2; exit 1; }
fi

if [[ $verb = build || $verb = start ]]; then
    for tag in "$base_image" "$image"; do
        podman image exists "$tag" || continue
        owner=$(podman image inspect --format '{{index .Labels "dsl41.wargame"}}' "$tag")
        [ "$owner" = "$tree" ] || { echo "foreign image: $tag" >&2; exit 1; }
    done
fi

case $verb in
    build)
        podman build --tag "$base_image" --label "dsl41.wargame=$tree" --build-arg TARGET=el8 \
            --build-arg BASE=docker.io/library/rockylinux@sha256:9794037624aaa6212aeada1d28861ef5e0a935adaf93e4ef79837119f2a2d04c \
            --build-arg "RPM_MIRROR=${DSL41_RPM_MIRROR-}" \
            --file "$deploy/offline.Containerfile" "$deploy"
        podman build --tag "$image" --label "dsl41.wargame=$tree" --build-arg "BASE=$base_image" \
            --build-arg "PYTHON_INDEX=${PYTHON_INDEX:-https://pypi.org/simple}" \
            --file "$here/wargame.Containerfile" "$here"
        ;;
    start)
        if podman container exists "$container"; then
            podman start "$container"
        else
            podman run --detach --name "$container" --label "dsl41.wargame=$tree" \
                --network none --systemd=always --cpus 2 --memory 3g "$image" /sbin/init
        fi
        ready=false
        for _ in {1..60}; do
            state=$(podman exec "$container" systemctl is-system-running 2>/dev/null || true)
            case $state in running|degraded) ready=true; break ;; esac
            sleep 1
        done
        [ "$ready" = true ] || { echo "systemd did not become ready: $container" >&2; exit 1; }
        podman cp "$deploy/capability-check.sh" "$container:/tmp/dsl41-capability-check.sh"
        podman exec "$container" bash /tmp/dsl41-capability-check.sh
        # Only tracked files reach /work, as in drill-local.sh: untracked scratch stays out.
        podman exec "$container" bash -c 'rm -rf /work && mkdir /work'
        git -C "$tree" ls-files --cached -z |
            COPYFILE_DISABLE=1 tar -C "$tree" --null -T - -cf - |
            podman exec -i "$container" tar -C /work --warning=no-unknown-keyword -xf -
        podman exec "$container" chown -R runner:runner /work
        ;;
    deliver)
        podman exec "$container" bash -c 'rm -rf /delivery/bundle && mkdir -p /delivery'
        podman cp "$bundle" "$container:/delivery/bundle"
        podman exec "$container" chown -R runner:runner /delivery/bundle
        ;;
    join) exec podman exec -it "$container" bash ;;
    stop) podman stop --time 30 "$container" ;;
    dispose) podman rm --force "$container" ;;
esac
