#!/usr/bin/env bash
# Disposable EL8 installation host; prerequisites follow docs/offline-rehearsal.md.
set -euo pipefail

usage() { echo "usage: $0 build|start|join|stop|dispose [NAME]" >&2; exit 2; }
[ "$#" -ge 1 ] && [ "$#" -le 2 ] || usage
verb=$1
case $verb in build|start|join|stop|dispose) ;; *) usage ;; esac
here=$(CDPATH='' cd "$(dirname "$0")" && pwd -P)
tree=$(CDPATH='' cd "$here/../../.." && pwd -P)
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
            --file "$here/offline.Containerfile" "$here"
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
        podman cp "$here/capability-check.sh" "$container:/tmp/dsl41-capability-check.sh"
        podman exec "$container" bash /tmp/dsl41-capability-check.sh
        ;;
    join) exec podman exec -it "$container" bash ;;
    stop) podman stop --time 30 "$container" ;;
    dispose) podman rm --force "$container" ;;
esac
