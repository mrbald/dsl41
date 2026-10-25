#!/usr/bin/env bash
# A disposable EL8 host for exercises: a Rocky Linux 8 container under podman, running
# systemd (239, the RHEL 8 family), isolated from the network, built from
# examples/nightbank/deploy/offline.Containerfile. Install dsl41 inside it per
# docs/offline-rehearsal.md, then run the shape 1 drills (deployment-runbook ss3)
# or anything that needs a real service manager on the production host class.
#
#   scripts/wargame.sh build   [NAME]   build the image
#   scripts/wargame.sh start   [NAME]   run (or restart) the container, wait for systemd
#   scripts/wargame.sh join    [NAME]   a shell inside it
#   scripts/wargame.sh stop    [NAME]   stop it, keeping its state
#   scripts/wargame.sh dispose [NAME]   remove it
#
# NAME defaults to this checkout's directory name, so two checkouts never share a
# container; images and containers carry the checkout path as a label and the script
# refuses to touch one made from another checkout. PYTHON_INDEX (default PyPI) is the
# index the image build installs uv from; the running container has no network.
set -euo pipefail

usage() { echo "usage: $0 build|start|join|stop|dispose [NAME]" >&2; exit 2; }
[ "$#" -ge 1 ] && [ "$#" -le 2 ] || usage
verb=$1
case $verb in build|start|join|stop|dispose) ;; *) usage ;; esac
here=$(CDPATH='' cd "$(dirname "$0")" && pwd -P)
tree=$(CDPATH='' cd "$here/.." && pwd -P)
deploy=$tree/examples/nightbank/deploy
name=${2:-$(basename "$tree")}
[[ $name =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] || {
    echo 'NAME must start with a letter or digit and contain only letters, digits, _, . or -' >&2
    exit 2
}
container=dsl41-wargame-$name
image=localhost/dsl41-wargame:$name

podman info >/dev/null

# Refuse unrelated containers even when their names match.
if podman container exists "$container"; then
    owner=$(podman inspect --format '{{index .Config.Labels "dsl41.wargame"}}' "$container")
    [ "$owner" = "$tree" ] || { echo "foreign container: $container" >&2; exit 1; }
fi

if [[ $verb = build || $verb = start ]] && podman image exists "$image"; then
    owner=$(podman image inspect --format '{{index .Labels "dsl41.wargame"}}' "$image")
    [ "$owner" = "$tree" ] || { echo "foreign image: $image" >&2; exit 1; }
fi

case $verb in
    build)
        podman build --tag "$image" --label "dsl41.wargame=$tree" --build-arg TARGET=el8 \
            --build-arg BASE=docker.io/library/rockylinux@sha256:9794037624aaa6212aeada1d28861ef5e0a935adaf93e4ef79837119f2a2d04c \
            --build-arg PYTHON_INDEX="${PYTHON_INDEX:-https://pypi.org/simple}" \
            --file "$deploy/offline.Containerfile" "$deploy"
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
        ;;
    join) exec podman exec -it "$container" bash ;;
    stop) podman stop --time 30 "$container" ;;
    dispose) podman rm --force "$container" ;;
esac
