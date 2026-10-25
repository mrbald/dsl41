#!/usr/bin/env bash
# Both target lanes use the exact wheel and shared installed scenario bodies (DL-389).
set -euo pipefail
check_no_uv() {
    # Target manufacturing and qualification require an absent uv executable (DL-396).
    local found
    if found=$(command -v uv); then
        printf 'refused: uv found at %s\n' "$found" | tee "$1" >&2
        return 2
    fi
    printf 'uv absent from PATH: %s\n' "$PATH" >"$1"
}
copy_tree() {
    mkdir /work
    git -C /src -c safe.directory=/src ls-files --cached -z |
        tar -C /src --null -T - -cf - | tar -C /work -xf -
}
archive_evidence() {
    local status=$? archive_failed=0
    systemctl status dsl41-engine.service dsl41-supervisor.service --no-pager >evidence/units.log 2>&1 || true
    journalctl -u dsl41-engine.service -u dsl41-supervisor.service --no-pager >evidence/journal.log 2>&1 || true
    if [ -d /srv/dsl41/runs ]; then
        tar -C /srv/dsl41 --exclude='*.sock' -czf evidence/durable-state.tar.gz runs || archive_failed=1
    fi
    shopt -s nullglob
    local faults=() path
    for path in /tmp/dsl41-offline-faults.* /tmp/rpm-lifecycle /tmp/dsl41-drill \
        /srv/dsl41-install-storage /srv/dsl41-storage /srv/dsl41/nightbank/night; do
        [ ! -e "$path" ] || faults+=("$path")
    done
    for path in /opt/dsl41/.venvs/* /srv/dsl41-rpm-check/.venvs/*; do
        if [ -d "$path" ] && [ ! -e "$path/.dsl41/receipt.json" ]; then faults+=("$path"); fi
    done
    if [ "${#faults[@]}" -gt 0 ]; then
        tar -C / --exclude='*.sock' -czf evidence/fault-state.tar.gz "${faults[@]#/}" || archive_failed=1
    fi
    if [ -e evidence/exit-status ]; then
        printf '%s\n' "$status" >>evidence/archive-status
    else
        printf '%s\n' "$status" >evidence/exit-status
    fi
    if [ "$archive_failed" = 1 ]; then
        echo 'archive failed; retain this container' >evidence/archive-failed
        trap - EXIT
        exit 1
    fi
}
case ${1-} in
    build)
        check_no_uv /delivery/uv-absence-build.txt
        copy_tree
        cd /work
        shopt -s nullglob
        wheels=(/delivery/dsl41-*.whl)
        [ "${#wheels[@]}" = 1 ] || { echo 'exactly one release wheel required' >&2; exit 2; }
        PYTHON=python3.12 bash scripts/offline/build-wheelhouse.sh \
            "${wheels[0]}" /delivery/release-inputs /delivery/bundle
        sha256sum /delivery/*.whl >/delivery/release-wheel.sha256
        ;;
    qualify)
        copy_tree
        cd /work
        mkdir evidence
        trap archive_evidence EXIT
        check_no_uv evidence/uv-absence-qualification.txt
        cp /opt/os-packages.txt /etc/os-release evidence/
        uname -a >evidence/kernel.txt
        systemctl --version >evidence/systemd.txt
        python3.12 -VV >evidence/python.txt
        ip -o addr >evidence/network.txt 2>/dev/null || cat /proc/net/route >evidence/network.txt
        capability=()
        if [ "${DSL41_REQUIRE_NATIVE_X86-}" = 1 ]; then capability=(--native-x86_64); fi
        bash examples/nightbank/deploy/capability-check.sh "${capability[@]}" | tee evidence/capability.log
        # RPM lifecycle runs before service activation; it retains generated runtimes (DL-389).
        if [ "${2-}" = el8 ]; then
            PYTHON=/usr/bin/python3.12 bash scripts/offline/rpm-lifecycle.sh /delivery/bundle /tmp/rpm-lifecycle \
                >evidence/rpm-lifecycle.log 2>&1
            mkdir evidence/rpm
            shopt -s nullglob
            packages=(/tmp/rpm-lifecycle/dsl41-base/RPMS/*/dsl41-offline-*-1.*.rpm)
            [ "${#packages[@]}" = 1 ] || { echo 'one qualified default/base revision-1 RPM required' >&2; exit 2; }
            cp "${packages[0]}" evidence/rpm/
            rpm -qpi "${packages[0]}" >evidence/rpm/package-info.txt
            rpm -qpl "${packages[0]}" >evidence/rpm/owned-files.txt
            sha256sum evidence/rpm/*.rpm >evidence/rpm/SHA256SUMS
            # Lifecycle's retained prefix is kept intact; service activation uses its completed base runtime.
        else
            for prefix in /opt/dsl41 /srv/dsl41-portable-check; do
                PYTHON=python3.12 bash scripts/offline/qualify.sh /delivery/bundle "$prefix" \
                    >>evidence/portable.log 2>&1
            done
        fi
        PYTHON=python3.12 bash examples/nightbank/deploy/offline-faults.sh /delivery/bundle \
            >evidence/faults.log 2>&1
        chown -R runner:runner /work
        # Root opens the evidence logs before sudo changes the process identity.
        # shellcheck disable=SC2024
        sudo -u runner -H env DRILL_BUNDLE=/delivery/bundle \
            bash examples/nightbank/deploy/drill-steps.sh install >evidence/install.log 2>&1
        # shellcheck disable=SC2024
        if [ "${3:-all}" = maintenance ]; then
            sudo -u runner -H bash -c 'set -e; bash examples/nightbank/deploy/offline-scenarios.sh bootstrap small; bash examples/nightbank/deploy/offline-scenarios.sh maintenance small' \
                >evidence/scenarios.log 2>&1
        else
            sudo -u runner -H bash examples/nightbank/deploy/offline-scenarios.sh \
                >evidence/scenarios.log 2>&1
        fi
        ;;
    archive) cd /work; archive_evidence;;
    *) echo 'usage: offline-target.sh build | qualify ubuntu|el8 | archive' >&2; exit 2;;
esac
