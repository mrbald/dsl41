#!/usr/bin/env bash
# The small and bank lanes share the installed service and boundary probes (DL-389).
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
source "$here/drill-lib.sh"
BUILD_A=$(cat "$SCRATCH/build-a")
BUILD_B=$(cat "$SCRATCH/build-b")
expect_exit() { # expect_exit CODE COMMAND...
    local expected=$1 code=0
    shift
    "$@" || code=$?
    [ "$code" = "$expected" ] || fail "exit $code, expected $expected: $*"
}
# The numeric outcome and the server's structured reason must both match (DL-390).
maintenance_outcome() { # maintenance_outcome EXPECTED [EXTRA_ARGS...]
    local expected=$1 code=0
    shift
    MAINTENANCE_REPLY=$(cli supervise shutdown --run-root "$ROOT" "$@" 2>&1) || code=$?
    printf '%s\n' "$MAINTENANCE_REPLY"
    [ "$code" = "$expected" ] || fail "maintenance exit $code, expected $expected"
    MAINTENANCE_JSON=$(printf '%s\n' "$MAINTENANCE_REPLY" | sed -n 's/^[^{]*\({.*}\)$/\1/p')
}
seal_next() {
    next_args
    cli seal --run-root "$ROOT" --estate-anchor "$ANCHOR" "${NEXT[@]}" -p "$PROPERTIES" \
        --next-detached --next-as-machine localhost --next-machine-policy strict \
        --force-seal --claimed-actor offline-drill
    wait_for 30 'the sealed engine exits' is_state "$ENGINE" failed
    start_engine
}
maintenance_probe() {
    local sup pid attempts run
    force_start "$LONG_JOB"
    wait_for 30 "$LONG_JOB is RUNNING" job_is "$LONG_JOB" RUNNING
    sup=$(prop "$SUPERVISOR" MainPID)
    pid=$(pgrep -f "fakework $LONG_JOB" | head -1)
    run=$(job_field "$LONG_JOB" run_number)
    attempts=$(sudo cat "$NIGHTBANK/night/data/state/$LONG_JOB.attempts")
    if sudo systemctl stop "$SUPERVISOR"; then fail 'manager stop ignored RefuseManualStop'; fi
    if sudo systemctl restart "$SUPERVISOR"; then fail 'manager restart ignored RefuseManualStop'; fi
    expect_exit 2 cli supervise shutdown --run-root "$ROOT"
    sudo systemctl stop "$ENGINE"
    expect_exit 2 cli supervise shutdown --run-root "$ROOT"
    sudo kill -0 "$pid"
    [ "$(prop "$SUPERVISOR" MainPID)" = "$sup" ] || fail 'refusal replaced supervisor'
    # Preparing and activating a runtime is independent of existing processes (DL-389).
    install_build ui
    flip "$BUILD_B"
    start_engine
    [ "$(prop "$SUPERVISOR" MainPID)" = "$sup" ] || fail 'runtime flip replaced supervisor'
    sudo kill -0 "$pid"
    [ "$(job_field "$LONG_JOB" run_number)" = "$run" ] || fail 'resume relaunched the command'
    [ "$(sudo cat "$NIGHTBANK/night/data/state/$LONG_JOB.attempts")" = "$attempts" ] ||
        fail 'resume repeated an external attempt'
    sudo systemctl stop "$ENGINE"
    maintenance_stop --force=terminate
    assert_no_writers "$ROOT"
    flip "$BUILD_A"
    start_engine
    wait_for 60 'the force-terminated command is reconciled' job_not_running "$LONG_JOB"
    [ "$(job_field "$LONG_JOB" run_number)" = "$run" ] || fail 'force recovery relaunched work'
    [ "$(sudo cat "$NIGHTBANK/night/data/state/$LONG_JOB.attempts")" = "$attempts" ] ||
        fail 'force recovery repeated an external attempt'
    ok 'manager guard, live-work refusal, runtime flip survival, explicit force and reconciliation'
}
# pidfds bind every fixture signal to the process identity we observed (DL-390).
process_token() {
    sudo "$VENV/bin/python" -I -c 'from dsl41.runner_procid import proc_start_token; import sys; print(proc_start_token(int(sys.argv[1])))' "$1"
}
verified_signal() { # verified_signal PID START_TOKEN SIGNAL
    sudo "$VENV/bin/python" -I - "$@" <<'PYTHON'
import os
import ctypes
import platform
import signal
import sys
from dsl41.runner_procid import verify_alive
pid = int(sys.argv[1])
if not hasattr(signal, "pidfd_send_signal"):
    raise RuntimeError("fixture requires pidfd_send_signal support")
if hasattr(os, "pidfd_open"):
    fd = os.pidfd_open(pid)
else:
    # Linux's admitted 64-bit ABIs assign pidfd_open syscall 434 (DL-390).
    if platform.machine() not in {"x86_64", "aarch64"} or sys.maxsize <= 2**32:
        raise RuntimeError("fixture has no qualified pidfd_open ABI")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    fd = libc.syscall(ctypes.c_long(434), ctypes.c_int(pid), ctypes.c_uint(0))
    if fd < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
try:
    if not verify_alive(pid, sys.argv[2]):
        raise RuntimeError("fixture process identity changed")
    signal.pidfd_send_signal(fd, getattr(signal, sys.argv[3]))
finally:
    os.close(fd)
PYTHON
}
concurrent_engine_probe() {
    local pid token before after
    wait_for 30 'no command is live before concurrent startup' nothing_live "$ROOT"
    pid=$(prop "$ENGINE" MainPID)
    token=$(process_token "$pid")
    before=$(sudo sh -c 'sha256sum "$1"/wal/*.jsonl' sh "$ROOT")
    expect_exit 2 as_dsl41 timeout 20 "$LAUNCH" engine
    after=$(sudo sh -c 'sha256sum "$1"/wal/*.jsonl' sh "$ROOT")
    [ "$before" = "$after" ] || fail 'concurrent start changed the WAL'
    [ "$(prop "$ENGINE" MainPID)" = "$pid" ] || fail 'concurrent start replaced the engine'
    [ "$(process_token "$pid")" = "$token" ] || fail 'engine identity changed'
    ok 'concurrent installed startup refused without WAL changes or replacing the engine'
}
frozen_engine_probe() {
    local sup
    FROZEN_PID=$(prop "$ENGINE" MainPID)
    FROZEN_TOKEN=$(process_token "$FROZEN_PID")
    FROZEN_HOLDER=$(lease_field holder)
    case $FROZEN_HOLDER in "engine:$ROOT#"*) ;; *) fail 'the engine does not own its expected lease';; esac
    sup=$(prop "$SUPERVISOR" MainPID)
    cli supervise list --run-root "$ROOT" | jq -e '.deadman_s == null' >/dev/null ||
        fail 'the frozen-engine fixture requires no supervisor deadman'
    trap 'verified_signal "$FROZEN_PID" "$FROZEN_TOKEN" SIGCONT || true' EXIT
    verified_signal "$FROZEN_PID" "$FROZEN_TOKEN" SIGSTOP
    # Expiry removes lease authority; the frozen engine still owns the file lock (DL-390).
    lease_expired() { [ -z "$(lease_field holder)" ]; }
    wait_for 75 'the frozen engine no longer has a live supervisor lease' lease_expired
    maintenance_outcome 2
    jq -e -s 'length == 1 and .[0].ok == false and .[0].error == "maintenance_refused"
        and (.[0].detail | startswith("[Errno 11]"))' <<<"$MAINTENANCE_JSON" >/dev/null ||
        fail 'the frozen engine was not refused for its held file lock'
    [ "$(prop "$SUPERVISOR" MainPID)" = "$sup" ] || fail 'lock refusal replaced supervisor'
    [ "$(process_token "$FROZEN_PID")" = "$FROZEN_TOKEN" ] || fail 'frozen engine identity changed'
    verified_signal "$FROZEN_PID" "$FROZEN_TOKEN" SIGCONT
    trap - EXIT
    wait_up
    engine_lease_returned() { [ "$(lease_field holder)" = "$FROZEN_HOLDER" ]; }
    wait_for 30 'the same resumed engine reacquired its lease' engine_lease_returned
    ok 'expired engine lease does not bypass its held leader.lock; identity-checked CONT recovers'
}
residual_cgroup_probe() {
    local fixture=dsl41-residual-fixture.service child token group sup
    sudo systemctl stop "$ENGINE"
    sup=$(prop "$SUPERVISOR" MainPID)
    sudo systemd-run --unit="$fixture" --property=User=dsl41 --property=KillMode=process /bin/sleep 300
    child_ready() { [ "$(prop "$fixture" MainPID)" -gt 0 ]; }
    wait_for 10 'the explicit residual fixture started' child_ready
    child=$(prop "$fixture" MainPID)
    token=$(process_token "$child")
    group=$(prop "$SUPERVISOR" ControlGroup)
    [ -n "$group" ] && [ "$group" != / ] || fail 'supervisor has no dedicated group'
    sudo "$VENV/bin/python" -I - "$sup" "$group" "$child" "$token" <<'PYTHON'
import os
import sys
from pathlib import Path
from dsl41.runner_procid import ServiceContainment, verify_alive
supervisor, group, child, token = int(sys.argv[1]), sys.argv[2], int(sys.argv[3]), sys.argv[4]
entries = [line.split(":", 2) for line in Path(f"/proc/{supervisor}/cgroup").read_text().splitlines()]
rows = [row for row in entries if "name=systemd" in row[1].split(",")]
if not rows:
    rows = [row for row in entries if row[1] == ""]
if len(rows) != 1 or rows[0][2] != group:
    raise RuntimeError("fixture cannot reconcile supervisor hierarchy")
kind = "cgroup" if rows[0][1] else "cgroup2"
mounts = []
for line in Path("/proc/self/mountinfo").read_text().splitlines():
    left, right = line.split(" - ", 1)
    fields, fs = left.split(), right.split()
    if fs[0] == kind and (kind == "cgroup2" or "name=systemd" in fs[2].split(",")):
        if fields[3] != "/" or "\\" in fields[4]:
            raise RuntimeError("fixture requires a complete unescaped cgroup mount")
        mounts.append(Path(fields[4]))
if len(mounts) != 1 or not verify_alive(child, token):
    raise RuntimeError("fixture identity or mount is indeterminate")
proof = ServiceContainment(mounts[0] / group.lstrip("/"), group, group.rsplit("/", 1)[-1], supervisor)
if proof.members() != {supervisor}:
    raise RuntimeError("fixture requires an otherwise idle supervisor cgroup")
(proof.directory / "cgroup.procs").write_text(str(child) + "\n")
if child not in proof.members() or not verify_alive(child, token):
    raise RuntimeError("fixture migration did not preserve process identity")
print(f"injected residual: hierarchy={kind} pid={child} token={token} group={group}")
PYTHON
    sudo grep -F -- "$group" "/proc/$child/cgroup" >/dev/null || fail 'fixture did not enter the supervisor cgroup'
    maintenance_outcome 2
    jq -e -s 'length == 1 and .[0].ok == false and .[0].error == "maintenance_refused"
        and .[0].detail == "active wrappers or residual service processes"' <<<"$MAINTENANCE_JSON" >/dev/null ||
        fail 'the default request was not refused for residual work'
    [ "$(process_token "$child")" = "$token" ] || fail 'default refusal signalled the residual'
    maintenance_outcome 4 --force=terminate
    # the admitted operation's outcome is its record (DL-401)
    sudo cat "$ROOT/maintenance.json" | jq -e '.state == "uncertain" and .force == "terminate"
        and .detail == "work remains after the termination bound"' >/dev/null ||
        fail 'the force result did not record retained residual work'
    [ "$(prop "$SUPERVISOR" MainPID)" = "$sup" ] || fail 'uncertainty dropped supervisor custody'
    [ "$(process_token "$child")" = "$token" ] || fail 'force signalled an unknown residual'
    manual start || true
    wait_for 30 'the uncertain supervisor excludes engine startup' is_state "$ENGINE" failed
    [ "$(prop "$ENGINE" ExecMainStatus)" = 2 ] || fail 'excluded engine did not refuse with 2'
    maintenance_outcome 4
    # the rerun reads the uncertain operation from PING and its record and sends nothing
    grep -qF 'maintenance is uncertain: work remains after the termination bound' \
        <<<"$MAINTENANCE_REPLY" || fail 'the repeated request lost the uncertain state'
    # This named fixture deliberately accepts containment, never an automatic fallback (DL-390).
    incident_stop_supervisor 'injected unknown residual; explicit fixture containment after outcome 4'
    if [ "$(prop "$fixture" LoadState)" != not-found ]; then sudo systemctl stop "$fixture"; fi
    sudo systemctl reset-failed "$fixture" 2>/dev/null || true
    start_engine
    ok 'residual refusal, uncertain force, retained engine exclusion and explicit incident containment'
}
# Definitions change through `dsl41 release` (DL-407, DL-412): an added job, a
# changed one, a removal that leaves a ghost, and a rollback by the same path.
# The last release has the inputs the lane started with.
definition_probe() {
    local original=$ESTATE effect=$NIGHTBANK/night/effects-$size first src
    release_setup
    src=$(release_source "$size-p1" "$original")
    cat >>"$src/global.jil" <<JIL

insert_job: OFFLINE_EFFECT_C
job_type: c
command: printf 'v1\\n' >> $effect
machine: localhost
owner: dsl41
JIL
    deploy prepare "$size-p1" "$src"
    release_apply "$size-p1"
    spec_has OFFLINE_EFFECT_C v1 || fail 'the added definition is not v1'
    send_keeping_retry FORCE_STARTJOB OFFLINE_EFFECT_C
    first=$ANSWER
    wait_for 30 'added definition produced one effect' job_is OFFLINE_EFFECT_C SUCCESS
    # The exact request is replayed; its external effect count must stay one (DL-216).
    # shellcheck disable=SC2086
    ANSWER=$(cli sendevent FORCE_STARTJOB -J OFFLINE_EFFECT_C -S "$SOCK" $RETRY_LINE)
    [ "$(jq -c '{request_id,decision,index}' <<<"$ANSWER")" = \
        "$(jq -c '{request_id,decision,index}' <<<"$first")" ] || fail 'request replay changed its decision'
    [ "$(sudo cat "$effect" | wc -l | tr -d ' ')" = 1 ] || fail 'request replay repeated external effect'
    src=$(release_source "$size-p2" "$ESTATE")
    sed -i "s/printf 'v1/printf 'v2/" "$src/global.jil"
    deploy prepare "$size-p2" "$src"
    release_apply "$size-p2"
    spec_has OFFLINE_EFFECT_C v2 || fail 'the changed definition is not v2'
    force_start OFFLINE_EFFECT_C
    wait_for 30 'changed definition ran' ran_to_success OFFLINE_EFFECT_C 2
    src=$(release_source "$size-p3" "$original")
    deploy prepare "$size-p3" "$src"
    release_apply "$size-p3"
    expect_exit 2 cli query spec -J OFFLINE_EFFECT_C -S "$SOCK"
    [ "$(job_field OFFLINE_EFFECT_C job_type)" = null ] || fail 'removed definition is not a ghost'
    release_apply rollback
    [ "$RELEASE" = "$size-p2" ] || fail "the rollback activated $RELEASE"
    force_start OFFLINE_EFFECT_C
    wait_for 30 'definition rollback ran' ran_to_success OFFLINE_EFFECT_C 3
    [ "$(sudo cat "$effect")" = $'v1\nv2\nv2' ] || fail 'definition effects differ'
    release_apply rollback
    [ "$RELEASE" = "$size-p3" ] || fail "the second rollback activated $RELEASE"
    release_teardown
    cli audit --estate-anchor "$ANCHOR"
    ok 'definition add/change/remove/rollback through dsl41 release; exact replay produced no duplicate effect'
}
# Each phase can be resumed explicitly after a diagnosed harness failure (DL-389).
use_estate() { # use_estate small|bank
    size=$1
    case $size in
        small) ROOT=/srv/dsl41/runs/nightbank-01;;
        bank) ROOT=/srv/dsl41/runs/nightbank-bank;;
        *) fail 'estate must be small or bank';;
    esac
    ANCHOR=$ROOT.anchor
    SOCK=$ROOT/control.sock
    RELEASE=$size-r1
    ESTATE=$ESTATE_RELEASES/$RELEASE
    export DRILL_ROOT=$ROOT DRILL_ESTATE=$size
}
bootstrap_estate() {
    if [ "$size" = bank ]; then
        ROOT=/srv/dsl41/runs/nightbank-bank
        ANCHOR=$ROOT.anchor
        SOCK=$ROOT/control.sock
        RELEASE=bank-r1
        ESTATE=$ESTATE_RELEASES/$RELEASE
        provision_root "$ROOT"
        point_launcher "$ROOT" "$ANCHOR"
        # The bank estate is above the example's ceiling, so this lane sizes the
        # ceiling and the supervisor unit's stop timeout for it, together, by
        # DL-401's line: the estate and the one job definition_probe adds.
        size_ceiling $(($(cat "$NIGHTBANK/estate/bank"/*.jil | grep -c '^insert_job:') + 1))
        deploy prepare "$RELEASE" "$NIGHTBANK/estate/bank"
        activate "$RELEASE"
        # The shared install preparation also supplies the bank estate's incident table (DL-389).
        sudo cp "$ESTATE/incidents.conf" "$NIGHTBANK/night/incidents.conf"
        echo "$LONG_JOB late 45" | as_dsl41 tee -a "$NIGHTBANK/night/incidents.conf"
    fi
    bash "$here/drill-steps.sh" first-start
    bash "$here/drill-steps.sh" restart
}
backup_probe() {
    wait_for 60 'all work finished before backup' nothing_live "$ROOT"
    take_hold
    quiesce_shape1
    local backup=$SCRATCH/backup-$size
    sudo mkdir "$backup"
    sudo cp -a "$ROOT" "$backup/root"
    sudo cp -a "$ANCHOR" "$backup/anchor"
    sudo mv "$ROOT" "$ROOT.loss"
    sudo mv "$ANCHOR" "$ANCHOR.loss"
    sudo cp -a "$backup/root" "$ROOT"
    sudo cp -a "$backup/anchor" "$ANCHOR"
    cli audit --estate-anchor "$ANCHOR"
    start_engine
    [ "$(job_field APAC_EOD_B on_hold)" = true ] || fail 'restore lost the hold'
    cli audit --estate-anchor "$ANCHOR"
    sudo systemctl stop "$ENGINE"
    maintenance_stop
    assert_no_writers "$ROOT"
    release_hold
    ok "$size: preserved root, periods, audit, operator holds and effects across maintenance and restore"
}
access_probe() {
    bash "$here/drill-steps.sh" configure
}
accept_estate() {
    maintenance_probe
    concurrent_engine_probe
    frozen_engine_probe
    residual_cgroup_probe
    definition_probe
    if [ "$size" = small ]; then access_probe; fi
    bash "$here/drill-steps.sh" refusal
    backup_probe
}
storage_probe() {
# A bounded tmpfs exercises ENOSPC without consuming the VM's backing disk (DL-389).
local storage_type storage_bytes
read -r storage_type storage_bytes <<<"$(findmnt -n -b -o FSTYPE,SIZE --mountpoint /srv/dsl41-storage)"
[ "$storage_type" = tmpfs ] && [ "$storage_bytes" = 8388608 ] ||
    fail 'a distinct 8 MiB runtime-fault tmpfs is required'
ROOT=/srv/dsl41-storage/run-$(date +%s)-$$
ANCHOR=$ROOT.anchor
SOCK=$ROOT/control.sock
RELEASE=small-r1
ESTATE=$ESTATE_RELEASES/$RELEASE
sudo chown dsl41:dsl41 /srv/dsl41-storage
provision_root "$ROOT"
point_launcher "$ROOT" "$ANCHOR"
activate "$RELEASE"
start_engine
hold_scheduled
# The existing WAL page must have too little room for the launch request (DL-389).
local page wal_bytes aligned=false probe
page=$(getconf PAGESIZE)
for probe in $(seq 128); do
    request ON_HOLD OPS_SPOOL_C
    wal_bytes=$(sudo stat -c %s "$ROOT/wal/000001.jsonl")
    if [ "$((page - wal_bytes % page))" -lt 256 ]; then aligned=true; break; fi
done
[ "$aligned" = true ] || fail "could not bound the WAL tail after $probe probes"
local attempts_before=0
if sudo test -e "$NIGHTBANK/night/data/state/OPS_SPOOL_C.attempts"; then
    attempts_before=$(sudo cat "$NIGHTBANK/night/data/state/OPS_SPOOL_C.attempts")
fi
as_dsl41 "$VENV/bin/python" - "$ROOT/storage-fill" <<'PYTHON'
import errno
import sys
with open(sys.argv[1], "wb", buffering=0) as stream:
    for chunk in (65536, 4096, 1):
        try:
            while True:
                stream.write(b"x" * chunk)
        except OSError as error:
            if error.errno != errno.ENOSPC:
                raise
PYTHON
expect_exit 4 cli sendevent FORCE_STARTJOB -J OPS_SPOOL_C -S "$SOCK"
sudo systemctl stop "$ENGINE"
sudo rm "$ROOT/storage-fill"
maintenance_stop
start_engine
[ "$(job_field OPS_SPOOL_C run_number)" = 0 ] || fail 'storage failure admitted a launch'
local attempts_after=0
if sudo test -e "$NIGHTBANK/night/data/state/OPS_SPOOL_C.attempts"; then
    attempts_after=$(sudo cat "$NIGHTBANK/night/data/state/OPS_SPOOL_C.attempts")
fi
[ "$attempts_after" = "$attempts_before" ] || fail 'storage failure caused an external attempt'
seal_next
sudo systemctl stop "$ENGINE"
cli audit --estate-anchor "$ANCHOR"
maintenance_stop
assert_no_writers "$ROOT"
ok 'bounded ENOSPC before FORCE_STARTJOB produces UNKNOWN; recovery preserves run and external attempt counts'
}

case ${1:-all} in
    all)
        for size in small bank; do
            use_estate "$size"
            bootstrap_estate
            accept_estate
        done
        storage_probe
        ;;
    bootstrap|accept|access|backup|maintenance)
        [ "$#" = 2 ] || fail 'phase requires small or bank'
        use_estate "$2"
        case $1 in
            bootstrap) bootstrap_estate;;
            accept) accept_estate;;
            access) access_probe;;
            backup) backup_probe;;
            maintenance)
                maintenance_probe
                concurrent_engine_probe
                frozen_engine_probe
                residual_cgroup_probe
                sudo systemctl stop "$ENGINE"
                maintenance_stop
                assert_no_writers "$ROOT"
                ;;
        esac
        ;;
    storage) storage_probe;;
    *) fail 'usage: offline-scenarios.sh all | bootstrap|accept|access|backup|maintenance small|bank | storage';;
esac
