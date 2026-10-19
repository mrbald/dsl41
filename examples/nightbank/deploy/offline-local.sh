#!/usr/bin/env bash
# Connected delivery construction precedes network-disabled qualification (DL-389).
set -euo pipefail
if [ "$#" -ne 2 ]; then
    echo 'usage: offline-local.sh ubuntu|el8 NEW_EVIDENCE_DIRECTORY' >&2
    exit 2
fi
target=$1
scope=${DSL41_REHEARSAL_SCOPE:-all}
case $scope in all|maintenance) ;; *) echo 'scope must be all or maintenance' >&2; exit 2;; esac
case $target in
    ubuntu) base=docker.io/library/ubuntu@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55 ;;
    el8) base=docker.io/library/rockylinux@sha256:3c2d0ce12bf79fc5ff05e43b1000e30ff062dc89405525f3307cbff71661f1a0 ;;
    *) echo 'target must be ubuntu or el8' >&2; exit 2;;
esac
here=$(cd "$(dirname "$0")" && pwd)
tree=$(cd "$here/../../.." && pwd)
mkdir "$2"
evidence=$(cd "$2" && pwd -P)
printf '%s\n' "$scope" >"$evidence/scope"
image=${DSL41_REHEARSAL_IMAGE:-localhost/dsl41-offline-$target:local}
name=dsl41-offline-$target-$$
container_started=false
cleanup() {
    local status=$? archive_failed=0
    trap - EXIT
    if [ "$container_started" = true ]; then
        podman inspect "$name" >"$evidence/container.json" 2>"$evidence/inspect-error.log" || archive_failed=1
        podman logs "$name" >"$evidence/container.log" 2>&1 || archive_failed=1
        podman cp "$name:/work/evidence/." "$evidence/" 2>"$evidence/archive-error.log" || archive_failed=1
        [ ! -e "$evidence/archive-failed" ] || archive_failed=1
        if [ "$archive_failed" = 1 ]; then
            echo "evidence archival failed; kept disposable container $name" >&2
            status=1
        elif [ "${DRILL_KEEP-}" = 1 ]; then
            echo "kept disposable rehearsal container $name"
        else
            podman rm --force "$name" >/dev/null || status=1
        fi
    fi
    exit "$status"
}
trap cleanup EXIT
if [ -z "${DSL41_REHEARSAL_IMAGE-}" ]; then
    podman build --tag "$image" --build-arg "BASE=${DSL41_OCI_IMAGE:-$base}" \
        --build-arg "TARGET=$target" --build-arg "APT_MIRROR=${DSL41_APT_MIRROR-}" \
        --build-arg "RPM_MIRROR=${DSL41_RPM_MIRROR-}" \
        --build-arg "PYTHON_INDEX=${DSL41_PYTHON_INDEX:-https://pypi.org/simple}" \
        --file "$here/offline.Containerfile" "$here" >"$evidence/image-build.log" 2>&1
fi
podman image inspect "$image" >"$evidence/image.json"
podman info --format json >"$evidence/podman-info.json"
vm_arch=$(podman info --format '{{.Host.Arch}}')
printf '%s\n' "$vm_arch" >"$evidence/podman-architecture.txt"
if [ "${DSL41_REQUIRE_NATIVE_X86-}" = 1 ]; then
    case $vm_arch in amd64|x86_64) ;; *) echo "native x86-64 Podman host required: $vm_arch" >&2; exit 2;; esac
fi
mkdir "$evidence/delivery"
if [ -n "${DSL41_REUSE_DELIVERY-}" ]; then
    cp -a "$DSL41_REUSE_DELIVERY/." "$evidence/delivery/"
    printf 'reused delivery: %s\n' "$DSL41_REUSE_DELIVERY" >"$evidence/delivery-build.log"
else
    if [ -n "${DSL41_RELEASE_WHEEL-}" ]; then cp "$DSL41_RELEASE_WHEEL" "$evidence/delivery/"; fi
    podman run --rm --volume "$tree:/src:ro" --volume "$evidence/delivery:/delivery" \
        --env "DSL41_PYTHON_INDEX=${DSL41_PYTHON_INDEX:-https://pypi.org/simple}" \
        "$image" bash /src/examples/nightbank/deploy/offline-target.sh build \
        >"$evidence/delivery-build.log" 2>&1
fi
# This fresh container has no external interface during manufacturing or service probes (DL-389).
podman run --detach --name "$name" --network none --systemd=always --cpus 2 --memory 3g \
    --tmpfs /srv/dsl41-storage:rw,size=8m,mode=0700 \
    --tmpfs /srv/dsl41-install-storage:rw,size=16m,mode=0700 \
    --volume "$tree:/src:ro" --volume "$evidence/delivery:/delivery:ro" \
    --env "DSL41_REQUIRE_NATIVE_X86=${DSL41_REQUIRE_NATIVE_X86-}" "$image" /sbin/init >/dev/null
container_started=true
for _ in $(seq 60); do
    state=$(podman exec "$name" systemctl is-system-running 2>/dev/null || true)
    case $state in running|degraded) break;; esac
    sleep 1
done
podman exec "$name" bash /src/examples/nightbank/deploy/offline-target.sh qualify "$target" "$scope" \
    >"$evidence/qualification.log" 2>&1
printf 'passed: %s %s scope=%s; evidence %s\n' "$target" "$(podman exec "$name" uname -m)" "$scope" "$evidence"
