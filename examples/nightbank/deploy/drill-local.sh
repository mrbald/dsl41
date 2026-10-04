#!/usr/bin/env bash
# drill-local.sh -- run the service drill on this machine (DL-266).
#
# It runs the steps of drill-steps.sh, the same step bodies
# .github/workflows/service-drill.yml runs, in a podman container with
# systemd as PID 1 (`--systemd=always`) on Ubuntu 24.04, the runner image's
# distribution. The checkout is mounted read-only and copied into the
# container. Each step runs as `runner`, an unprivileged user with
# passwordless sudo that owns the copy, as GitHub's runner user owns its
# checkout (DL-271): a step that reads a root-only path without sudo fails
# here as it fails there. It exits 0 only when every step passed; the
# diagnostics step runs after the steps either way.
#
#   drill-local.sh               run every step
#   DRILL_KEEP=1 drill-local.sh  keep the container afterwards, to inspect it
#
# The container runs the host's architecture: arm64 on Apple silicon,
# where GitHub's runner is x86_64. Both images come from one index digest.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
tree=$(cd "$here/../../.." && pwd)
# ubuntu:24.04, pinned by the registry's index digest (an OCI image index),
# so the pin names the same image on arm64 and on amd64
base=docker.io/library/ubuntu@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55
# the uv the steps call, as setup-uv provides one on the runner
uv_version=0.9.7
image=localhost/dsl41-service-drill:local
# the steps' user: unprivileged, with passwordless sudo, as on the runner
user=runner
name=dsl41-service-drill-$$

say() { printf 'drill-local: %s\n' "$*"; }

podman build --quiet --tag "$image" --file - "$here" <<EOF >/dev/null
FROM $base
RUN apt-get update -q \\
    && DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends \\
        systemd systemd-sysv dbus sudo python3-venv jq procps util-linux ca-certificates \\
    && rm -rf /var/lib/apt/lists/* \\
    && python3 -m venv /opt/uv && /opt/uv/bin/pip install --quiet uv==$uv_version \\
    && ln -s /opt/uv/bin/uv /usr/local/bin/uv \\
    && useradd --create-home --shell /bin/bash $user \\
    && echo '$user ALL=(ALL) NOPASSWD:ALL' >/etc/sudoers.d/$user \\
    && chmod 0440 /etc/sudoers.d/$user
CMD ["/sbin/init"]
EOF

# shellcheck disable=SC2329  # the EXIT trap invokes it
cleanup() {
    if [ "${DRILL_KEEP-}" = 1 ]; then
        say "kept container $name"
    else
        podman rm --force "$name" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

# 2 CPUs and 3 GB bound the container, so it leaves room in the podman VM
podman run --detach --name "$name" --systemd=always --cpus 2 --memory 3g \
    --volume "$tree:/src:ro" "$image" >/dev/null
for _ in $(seq 60); do
    state=$(podman exec "$name" systemctl is-system-running 2>/dev/null || true)
    case $state in running | degraded) break ;; esac
    sleep 1
done
say "systemd is $state in $name"
podman exec "$name" bash -c 'mkdir /work && tar -C /src -cf - \
    --exclude=./.venv --exclude=./.git --exclude="*/__pycache__" --exclude=./.mypy_cache \
    --exclude=./.ruff_cache --exclude=./.pytest_cache --exclude="./.coverage*" --exclude=./dist . |
    tar -C /work -xf -'
podman exec "$name" chown -R "$user:$user" /work

steps=$(bash "$here/drill-steps.sh" --list)
results=()
status=0
start=$SECONDS
for step in $steps; do
    say "step $step"
    t0=$SECONDS
    if podman exec --user "$user" --workdir /work "$name" \
        bash examples/nightbank/deploy/drill-steps.sh "$step"; then
        results+=("passed  $((SECONDS - t0))s  $step")
    else
        results+=("FAILED  $((SECONDS - t0))s  $step")
        status=1
        break
    fi
done
say "step diagnostics"
podman exec --user "$user" --workdir /work "$name" \
    bash examples/nightbank/deploy/drill-steps.sh diagnostics || true

say "summary"
printf '  %s\n' "${results[@]}"
for step in $steps; do
    printf '%s\n' "${results[@]}" | grep -q " $step\$" || printf '  not run     %s\n' "$step"
done
say "$([ "$status" = 0 ] && echo passed || echo FAILED) as $user in $((SECONDS - start))s"
exit "$status"
