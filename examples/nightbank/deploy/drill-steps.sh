#!/usr/bin/env bash
# drill-steps.sh -- the steps of the nightbank service drill (DL-218, DL-266,
# DL-268).
#
#   drill-steps.sh --list    print the step names, in the order they run
#   drill-steps.sh STEP      run one step; exit 0 only if it passed
#
# .github/workflows/service-drill.yml runs one step per workflow step, and
# drill-local.sh runs the same steps in a podman container with systemd as
# PID 1. Each step is its own process and sources drill-lib.sh for the
# paths and the waits, so a step that points the launcher elsewhere sets
# the paths it uses itself. The drill needs a host with systemd, sudo, uv,
# jq and python3-venv; the install step installs what it needs.
#
# The drill checks the claims the example makes: the first start, a
# same-root restart, a detached job that survives an engine stop, a changed
# estate refused without a restart loop, a sealed engine that stays
# stopped until an operator opens the next period, the managed quiescence
# and restore of deployment-runbook ss2b, the signals of "What to watch",
# the configure recipe,
# the stop-and-recover recipe, a SIGKILL of the engine and of the
# supervisor, the application's release train through `dsl41 release`, every
# upgrade row of ss7 with its rollback, and ss0's retirement. The install,
# the first start and the retirement run ss0's recipe blocks as the runbook
# prints them (run_recipe).
# The two access-map refusals prove the same systemd claim as the changed
# estate, and are pinned in tests/test_nightbank_deploy.py instead.
set -euo pipefail
# shellcheck source=drill-lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/drill-lib.sh"

STEPS=(install first-start restart detached refusal sealed quiesce restore reboot
    watch configure kill-engine kill-supervisor stop-recover
    release release-rollback release-properties-only release-calendar-only
    release-concurrent release-interrupted
    upgrade-resume-safe upgrade-fresh-root upgrade-fresh-root-rollback
    upgrade-coordinated upgrade-coordinated-rollback
    upgrade-old-release upgrade-state-machine upgrade-state-machine-rollback retire)
# runs after the steps whatever they did; not part of --list
FINALLY=diagnostics

# this tree's wheel, built once, in two venvs: the resume-safe, fresh-root
# and coordinated rows flip between them. Two installs of one build exercise the
# rows' mechanics; they do not qualify a version pair.
BUILD_A=$(cat "$SCRATCH/build-a" 2>/dev/null || true)
BUILD_B=$(cat "$SCRATCH/build-b" 2>/dev/null || true)
# the old release of ss7's state-machine row: v1.7.0 from PyPI, which
# implements state-machine version 1
OLD_VERSION=1.7.0
OLD_VENV=/opt/dsl41/.venvs/historical-$OLD_VERSION
# the roots the upgrade rows move the launcher to: row 2's roll, and the
# fresh root of its rollback
ROLLED_ROOT=/srv/dsl41/runs/nightbank-01b
ROLLBACK_ROOT=/srv/dsl41/runs/nightbank-01c
OLD_ROOT=/srv/dsl41/runs/nightbank-old
NEW_ROOT=/srv/dsl41/runs/nightbank-02
BACKUP=/srv/dsl41/backup
# a short box member: an 8-second fakework, unconditioned
SHORT_JOB=AMER_INV_MACROS_C
# a job whose first run fails (the night's fail_once incident), and a job
# nothing depends on, for an operator's probes
FAIL_JOB=AMER_MKT_FX_C
PROBE_JOB=OPS_SPOOL_C

step_install() { # install dsl41, the nightbank estate, the launcher, the map and the units
    # Connected preparation happens before the network-disabled service phase (DL-389).
    mkdir -p "$SCRATCH"
    if [ -n "${DRILL_BUNDLE-}" ]; then
        cp -a "$DRILL_BUNDLE" "$SCRATCH/bundle"
    else
        (cd "$REPO" && uv build --wheel --out-dir "$SCRATCH/dist")
        PYTHON=python3.12 bash "$REPO/scripts/export-release-inputs.sh" \
            "$SCRATCH"/dist/dsl41-*.whl "$SCRATCH/exports"
        PYTHON=python3.12 bash "$REPO/scripts/offline/build-wheelhouse.sh" \
            "$SCRATCH"/dist/dsl41-*.whl "$SCRATCH/exports" "$SCRATCH/bundle"
        python3.12 -m pip download --only-binary=:all: "dsl41==$OLD_VERSION" \
            --dest "$SCRATCH/historical-wheels"
    fi
    install_build base
    BUILD_A=$(tail -n 1 "$SCRATCH/prepare-base.log")
    printf '%s\n' "$BUILD_A" >"$SCRATCH/build-a"
    install_build ui
    BUILD_B=$(tail -n 1 "$SCRATCH/prepare-ui.log")
    printf '%s\n' "$BUILD_B" >"$SCRATCH/build-b"
    flip "$BUILD_A"
    # the example is root-owned and read-only to the service, as a checkout
    # of a tag would be
    sudo install -d -m 0755 /srv/dsl41
    sudo cp -R "$REPO/examples/nightbank" "$NIGHTBANK"
    sudo chown -R root:root "$NIGHTBANK"
    sudo chmod -R go-w,a+rX "$NIGHTBANK"
    run_recipe service-install
    record_supervisor_exit
    assert_enablement enabled
    # ss1's link, through the symlink, so the recipes' bare dsl41 follows a flip
    sudo ln -sfn "$VENV/bin/dsl41" /usr/local/bin/dsl41
    # the night's data is the service's
    sudo install -d -o dsl41 -g dsl41 -m 0700 "$NIGHTBANK/night"
    # nightbank's own prepare_night: data directories, properties and
    # profile, the region anchors six hours out so no calendar fires. The
    # heredoc is deliberately unquoted so $NIGHTBANK expands; it must hold
    # no other $ or backtick
    as_dsl41 "$VENV/bin/python" - <<PY
import importlib.util
from datetime import UTC, datetime, timedelta
from importlib.machinery import SourceFileLoader
from pathlib import Path

loader = SourceFileLoader("nightbank", "$NIGHTBANK/bin/nightbank")
spec = importlib.util.spec_from_loader(loader.name, loader)
module = importlib.util.module_from_spec(spec)
loader.exec_module(module)
module.prepare_night(
    Path("$NIGHTBANK/night"),
    "${DRILL_ESTATE:-small}",
    anchor_utc=datetime.now(UTC) + timedelta(hours=6),
    incidents=True,
)
PY
    # one scripted delay: the job the drill starts runs long enough to
    # outlive an engine stop and start
    echo "$LONG_JOB late 45" | as_dsl41 tee -a "$NIGHTBANK/night/incidents.conf"
    # Every release on the host is prepared, the first one included: the
    # deployment identity prepares it with the site's properties in place,
    # and ACTIVE names it for the genesis (DL-407). The launcher's printed
    # line reads that release's manifest.
    release_identity
    deploy prepare "$RELEASE" "$NIGHTBANK/estate/${DRILL_ESTATE:-small}"
    activate "$RELEASE"
    run_recipe service-print
}

step_first_start() { # first start is a genesis, beside a supervisor in its own unit
    local mark line cgroup pid active
    mark=$(date +%s)
    # ss0's service recipe, step 5
    active=$(run_recipe service-start)
    [ "$active" = $'active\nactive' ] || fail "service-start printed: $active"
    run_recipe --as-dsl41 wait-answers S="$SOCK"
    wait_up
    is_state "$SUPERVISOR" active || fail "Requires= did not start $SUPERVISOR"
    line=$(launch_line_since "$mark")
    echo "$line"
    [[ $line != *--resume* ]] || fail "the first start was a resume"
    [[ $line == *"--access-map $MAP"* ]] || fail "no --access-map on the line"
    sudo test -e "$ROOT/journal.jsonl" || fail "no sentinel after the genesis"
    # separate cgroups: no supervisor process in the engine's
    cgroup=/sys/fs/cgroup$(prop "$ENGINE" ControlGroup)
    while read -r pid; do
        ! sudo grep -q runner_supervisor "/proc/$pid/cmdline" ||
            fail "a supervisor runs in the engine's cgroup"
    done <"$cgroup/cgroup.procs"
    ok "the engine's cgroup holds no supervisor"
    hold_scheduled
}

step_restart() { # same-root restart resumes
    local invocation mark line held
    invocation=$(prop "$ENGINE" InvocationID)
    mark=$(date +%s)
    manual restart
    wait_up
    [ "$(prop "$ENGINE" InvocationID)" != "$invocation" ] || fail "no new invocation"
    line=$(launch_line_since "$mark")
    echo "$line"
    [[ $line == *" --run-root $ROOT "* && $line == *" --resume" ]] ||
        fail "the restart was not a resume of $ROOT"
    held=$(cli query status --job APAC_EOD_B -S "$SOCK" | jq -r '.jobs.APAC_EOD_B.on_hold')
    [ "$held" = true ] || fail "the hold did not survive the restart"
    ok "the operator's holds survived the restart"
}

step_detached() { # a detached job survives an engine stop
    local pids pid
    force_start "$LONG_JOB"
    wait_for 60 "$LONG_JOB is RUNNING" job_is "$LONG_JOB" RUNNING
    pids=$(pgrep -f "fakework $LONG_JOB") || fail "no $LONG_JOB process"
    for pid in $pids; do
        grep -q "$SUPERVISOR" "/proc/$pid/cgroup" || fail "$pid is not in $SUPERVISOR"
    done
    ok "$LONG_JOB runs in the supervisor's cgroup"
    sudo systemctl stop "$ENGINE"
    is_state "$ENGINE" inactive || fail "$ENGINE: $(prop "$ENGINE" ActiveState)"
    for pid in $pids; do
        sudo kill -0 "$pid" || fail "$pid died with the engine"
    done
    cli supervise list --run-root "$ROOT" |
        jq -e --arg job "$LONG_JOB" '[.runs[].job] | index($job) != null' >/dev/null ||
        fail "the supervisor does not list $LONG_JOB"
    ok "$LONG_JOB outlived the engine"
    start_engine
    job_is "$LONG_JOB" RUNNING || fail "after reattach: $(job_status "$LONG_JOB")"
    ok "the restarted engine reattached to $LONG_JOB"
    wait_for 180 "$LONG_JOB ended in SUCCESS" job_is "$LONG_JOB" SUCCESS
}

step_refusal() { # a changed estate is refused without a restart loop
    sudo systemctl stop "$ENGINE"
    sudo cp "$ESTATE/global.jil" "$SCRATCH/global-refusal.orig"
    sudo sed -i 's/OPS_XINST_DEMO_C --sleep [0-9][0-9]*/OPS_XINST_DEMO_C --sleep 999/' "$ESTATE/global.jil"
    ! sudo cmp -s "$SCRATCH/global-refusal.orig" "$ESTATE/global.jil" || fail "the edit did not apply"
    expect_refusal "catalog hash mismatch"
    sudo cp "$SCRATCH/global-refusal.orig" "$ESTATE/global.jil"
    start_engine
}

step_sealed() { # a sealed engine stays stopped until the next period is opened
    local mark
    next_args
    mark=$(date +%s)
    # the drill's own FORCE_STARTJOB is inside the retry horizon, hence
    # --force-seal; the manifest gives the estate's words, and every
    # --next-* after them restates the launcher's options (DL-407)
    cli seal --run-root "$ROOT" "${NEXT[@]}" -p "$PROPERTIES" \
        --next-detached --next-as-machine localhost --next-machine-policy strict \
        --force-seal --claimed-actor drill@nightbank
    assert_stays_stopped 3 "sealed period 1" "$mark"
    # "What to watch", the sealed-not-opened row: closed until the opener runs
    [ "$(run_recipe --as-dsl41 watch-sealed ESTATE_ANCHOR="$ANCHOR")" = closed ] ||
        fail "the sealed check did not read closed"
    ok "the sealed check reads closed"
    mark=$(date +%s)
    start_engine
    launch_line_since "$mark" | grep -q -- ' --resume$' || fail "the opener was not a resume"
    sudo test -e "$ROOT/wal/000002.jsonl" || fail "period 2 did not open in $ROOT"
    [ "$(run_recipe --as-dsl41 watch-sealed ESTATE_ANCHOR="$ANCHOR")" = open ] ||
        fail "the sealed check did not read open"
    ok "period 2 opened in place; the sealed check reads open"
}

step_quiesce() { # managed quiescence: both units stopped and staying stopped (runbook ss2b)
    local log_size
    take_hold
    log_size=$(supervisor_log_size "$ROOT")
    quiesce_shape1
    assert_policy_read_logged "$ROOT" "$log_size"
    # the disabled units stay down through a boot's target, too
    boot_target
    assert_units_stay_stopped
}

step_restore() { # a copy of the root and the anchor, restored at the recorded paths
    local mark held
    # the copy a backup tool takes once the writers are gone; the
    # deployment inputs (estate, properties, map) stay where they are
    sudo install -d -m 0700 "$BACKUP"
    sudo cp -a "$ROOT" "$BACKUP/root"
    sudo cp -a "$ANCHOR" "$BACKUP/anchor"
    # the loss, then the restore at the same absolute paths
    sudo rm -rf "$ROOT" "$ANCHOR"
    sudo cp -a "$BACKUP/root" "$ROOT"
    sudo cp -a "$BACKUP/anchor" "$ANCHOR"
    cli audit --estate-anchor "$ANCHOR"
    ok "the restored lineage audits from its anchor"
    mark=$(date +%s)
    start_engine
    is_state "$SUPERVISOR" active || fail "Requires= did not start $SUPERVISOR"
    launch_line_since "$mark" | grep -q -- ' --resume$' || fail "the opener was not a resume"
    sudo test -e "$ROOT/wal/000003.jsonl" || fail "period 3 did not open in the restored root"
    held=$(cli query status --job APAC_EOD_B -S "$SOCK" | jq -r '.jobs.APAC_EOD_B.on_hold')
    [ "$held" = true ] || fail "the hold did not survive the restore"
    ok "period 3 opened in the restored root with the operator's holds"
    release_hold
}

# deployment-runbook ss0's host reboot, as far as a container can stage
# one: no seal and no reboot hold; both units stop, as a shutdown stops
# them, and a boot's target starts the enabled units, which resume the
# same root in the same period.
step_reboot() { # a host reboot: the enabled units resume the same root at boot
    local invocation mark
    wait_for 60 "nothing is live under the supervisor" nothing_live "$ROOT"
    assert_enablement enabled
    invocation=$(prop "$ENGINE" InvocationID)
    sudo systemctl stop "$ENGINE"
    maintenance_stop
    is_state "$ENGINE" inactive || fail "$ENGINE: $(prop "$ENGINE" ActiveState)"
    mark=$(date +%s)
    boot_target
    wait_up
    is_state "$SUPERVISOR" active || fail "$SUPERVISOR did not start at boot"
    [ "$(prop "$ENGINE" InvocationID)" != "$invocation" ] || fail "$ENGINE did not start again"
    launch_line_since "$mark" | grep -q -- ' --resume$' || fail "the boot start was not a resume"
    ! sudo test -e "$ROOT/wal/000004.jsonl" || fail "the boot opened a period"
    ok "the boot resumed period 3 in $ROOT"
}

# deployment-runbook ss0's "What to watch": each signal read with the command
# the table prints, against the running estate. A subscribe line over the
# budget is not reached in a live unit, and the runbook does not say it is
# drilled.
watch_cleanup() {
    sudo pkill -f '[d]sl41 query subscribe' 2>/dev/null || true
}
step_watch() { # "What to watch": every signal
    local out cursor baseline sub rc err run lines leader
    trap watch_cleanup EXIT
    # the engine unit
    out=$(systemctl show -p ActiveState -p ExecMainStatus -p NRestarts "$ENGINE")
    grep -qx 'ActiveState=active' <<<"$out" || fail "engine unit: $out"
    grep -qx 'ExecMainStatus=0' <<<"$out" || fail "engine unit: $out"
    grep -qE '^NRestarts=[0-9]+$' <<<"$out" || fail "engine unit: $out"
    ok "the engine unit row reads active, status 0, a restart count"
    # sealed, not opened: open is normal (closed is drilled in the sealed step)
    [ "$(run_recipe --as-dsl41 watch-sealed ESTATE_ANCHOR="$ANCHOR")" = open ] ||
        fail "the sealed check did not read open"
    # the supervisor unit and its list, with one finished run in it
    out=$(systemctl show -p ActiveState -p NRestarts "$SUPERVISOR")
    grep -qx 'ActiveState=active' <<<"$out" || fail "supervisor unit: $out"
    grep -qE '^NRestarts=[0-9]+$' <<<"$out" || fail "supervisor unit: $out"
    run=$(job_field "$SHORT_JOB" run_number)
    force_start "$SHORT_JOB"
    wait_for 60 "$SHORT_JOB ended in SUCCESS" ran_to_success "$SHORT_JOB" $((run + 1))
    wait_for 30 "supervise list shows $SHORT_JOB with wrapper_alive false" \
        list_shows_finished_run "$SHORT_JOB"
    # the supervisor log (the unit's journal is read in kill-supervisor, where a start is new)
    sudo test -s "$ROOT/supervisor.log" || fail "$ROOT/supervisor.log is empty or missing"
    ok "the supervisor log has content"
    # the leader note names the engine now running
    out=$(sudo cat "$ROOT/leader.lock")
    jq -e --argjson pid "$(prop "$ENGINE" MainPID)" \
        '.pid == $pid and (.host | type == "string") and (.epoch | type == "number")
         and (.since | type == "string")' <<<"$out" >/dev/null ||
        fail "leader.lock does not name the running engine: $out"
    ok "leader.lock names the engine's pid, host, epoch and since"
    # the control socket answers
    cli query status --brief -S "$SOCK" >/dev/null || fail "the control socket did not answer"
    # the perimeter's arming receipt
    perimeter_has policy_loaded || fail "no policy_loaded receipt in perimeter.jsonl"
    ok "the perimeter journal holds the map's policy_loaded receipt"
    # free space: both paths report
    lines=$(sudo df -P "$ROOT" "$ANCHOR" | wc -l)
    [ "$lines" = 3 ] || fail "df -P printed $lines lines for the run root and the anchor"
    # failures and alarms: the stream wakes the reader, the trace is the record
    out=$(cli query trace --since 0 -S "$SOCK")
    cursor=$(jq -r .last_seq <<<"$out")
    baseline=$(jq -r .baseline_id <<<"$out")
    rm -f "$SCRATCH/subscribe.out" "$SCRATCH/subscribe.err"
    as_dsl41 timeout 180 "$DSL41" query subscribe -S "$SOCK" \
        >"$SCRATCH/subscribe.out" 2>"$SCRATCH/subscribe.err" &
    sub=$!
    wait_for 30 "the subscriber is acknowledged" grep -q '"subscribed": true' "$SCRATCH/subscribe.out"
    request FORCE_STARTJOB "$FAIL_JOB"
    wait_for 60 "$FAIL_JOB ended in FAILURE" job_is "$FAIL_JOB" FAILURE
    wait_for 30 "the stream carries the run's end as an adapter STATUS input" \
        stream_shows_failure "$SCRATCH/subscribe.out" "$FAIL_JOB"
    out=$(cli query trace --since "$cursor" -S "$SOCK")
    [ "$(jq -r .baseline_id <<<"$out")" = "$baseline" ] || fail "the baseline_id moved"
    jq -e --arg job "$FAIL_JOB" \
        '[.entries[] | select(.job == $job and .transition == "RUNNING->FAILURE")] | length == 1' \
        <<<"$out" >/dev/null || fail "the trace since $cursor has no RUNNING->FAILURE for $FAIL_JOB"
    ok "the trace since the cursor names the transition to FAILURE"
    # a subscriber the engine closes exits 2 and names the cursor to resume at
    leader=$(prop "$ENGINE" MainPID)
    sudo systemctl stop "$ENGINE"
    rc=0
    wait "$sub" || rc=$?
    [ "$rc" = 2 ] || fail "subscribe exited $rc after the engine stopped, not 2"
    grep -q 'resubscribe with --since [0-9]' "$SCRATCH/subscribe.err" ||
        fail "subscribe did not name a --since: $(cat "$SCRATCH/subscribe.err")"
    ok "subscribe exits 2 and names the --since to resume with"
    # the leader note says who led; the control socket says no engine answers
    [ "$(sudo jq -r .pid "$ROOT/leader.lock")" = "$leader" ] ||
        fail "leader.lock does not keep the stopped engine's pid $leader"
    [ "$(prop "$ENGINE" MainPID)" = 0 ] || fail "$ENGINE still has a main process"
    rc=0
    cli query status --brief -S "$SOCK" >/dev/null 2>&1 || rc=$?
    [ "$rc" = 2 ] || fail "status --brief exited $rc with no engine, not 2"
    rc=0
    err=$(cli query subscribe -S "$SOCK" 2>&1) || rc=$?
    [ "$rc" = 2 ] || fail "subscribe exited $rc with no engine, not 2"
    ! grep -q -- '--since' <<<"$err" || fail "a subscribe refused before its ack named a --since"
    ok "with no engine: status --brief and subscribe exit 2, and subscribe names no --since"
    start_engine
}

# deployment-runbook ss0's "Recipe: configure an estate": the account that
# runs both units and owns the root, the socket group, the tiers a binding
# gives, the perimeter's receipts, a reload that names another group, and a
# changed execution profile refused on resume. The service account must be in
# the socket group (the engine hands it the run root), and the group needs
# execute on the run roots' directory.
SOCKET_GROUP=nightbank-socket
configure_restore() {
    local user
    [ ! -e "$SCRATCH/map.orig" ] || sudo cp "$SCRATCH/map.orig" "$MAP"
    [ ! -e "$SCRATCH/manifest.orig" ] || sudo cp "$SCRATCH/manifest.orig" "$ESTATE/estate.toml"
    sudo chgrp dsl41 "$(dirname "$ROOT")" "$ROOT" 2>/dev/null || true
    sudo chmod 0700 "$(dirname "$ROOT")" 2>/dev/null || true
    sudo gpasswd -d dsl41 "$SOCKET_GROUP" >/dev/null 2>&1 || true
    for user in dopsu dreadu dnobindu dstranger; do
        sudo userdel "$user" 2>/dev/null || true
    done
    sudo groupdel "$SOCKET_GROUP" 2>/dev/null || true
    sudo groupdel nightbank-ops 2>/dev/null || true
    sudo groupdel nightbank-observers 2>/dev/null || true
}
# CMD (a dsl41 command line) as USER exits CODE; the output is in $out
exits_as() { # exits_as USER CODE ARGS...
    local user=$1 code=$2 rc=0
    shift 2
    out=$(as_user "$user" "$@" 2>&1) || rc=$?
    [ "$rc" = "$code" ] || fail "$user: dsl41 $* exited $rc, not $code: $out"
}
step_configure() { # configure recipe: identity, socket group, tiers, receipts, profile gate
    local unit path denied before
    out=
    trap configure_restore EXIT
    # identities: one service account runs both units and owns what it writes
    for unit in "$ENGINE" "$SUPERVISOR"; do
        [ "$(prop "$unit" User)" = dsl41 ] || fail "$unit does not run as dsl41"
        [ "$(ps -o user= -p "$(prop "$unit" MainPID)")" = dsl41 ] || fail "$unit's process is not dsl41's"
    done
    for path in "$ROOT" "$ANCHOR" "$MAP"; do
        [ "$(sudo stat -c %U "$path")" = dsl41 ] || fail "$path is not owned by dsl41"
    done
    [ "$(sudo stat -c %a "$ROOT")" = 700 ] || fail "$ROOT is not 0700"
    [ "$(sudo stat -c %a "$MAP")" = 600 ] || fail "$MAP is not 0600"
    sudo groupadd "$SOCKET_GROUP"
    sudo groupadd nightbank-ops
    sudo groupadd nightbank-observers
    sudo useradd -M -s /usr/sbin/nologin -G "$SOCKET_GROUP,nightbank-ops" dopsu
    sudo useradd -M -s /usr/sbin/nologin -G "$SOCKET_GROUP,nightbank-observers" dreadu
    sudo useradd -M -s /usr/sbin/nologin -G "$SOCKET_GROUP" dnobindu
    sudo useradd -M -s /usr/sbin/nologin dstranger
    sudo cp -p "$MAP" "$SCRATCH/map.orig"
    sudo cp -p "$ESTATE/estate.toml" "$SCRATCH/manifest.orig"
    # with no socket_group, only the service account reaches the socket
    exits_as dopsu 2 query status --brief -S "$SOCK"
    grep -q 'Permission denied' <<<"$out" || fail "an ops member without the group: $out"
    ok "no socket_group: a person with an ops binding cannot reach the socket"
    # socket exposure: name the group in the map and restart. The service
    # account is not yet a member, so the engine cannot hand the root over
    sudo systemctl stop "$ENGINE"
    sudo sed -i "s/^# socket_group = .*/socket_group = \"$SOCKET_GROUP\"/" "$MAP"
    expect_refusal "cannot open the run root to group '$SOCKET_GROUP'"
    sudo usermod -aG "$SOCKET_GROUP" dsl41
    start_engine
    [ "$(sudo stat -c '%a %G' "$ROOT")" = "710 $SOCKET_GROUP" ] || fail "run root: $(sudo stat -c '%a %G' "$ROOT")"
    [ "$(sudo stat -c '%a %G' "$SOCK")" = "660 $SOCKET_GROUP" ] || fail "socket: $(sudo stat -c '%a %G' "$SOCK")"
    ok "the engine opened the run root to 0710 and the socket to 0660 for $SOCKET_GROUP"
    # the run roots' directory is 0700 for the account: no member reaches the socket yet
    exits_as dopsu 2 query status --brief -S "$SOCK"
    grep -q 'Permission denied' <<<"$out" || fail "before the directory grant: $out"
    sudo chgrp "$SOCKET_GROUP" "$(dirname "$ROOT")"
    sudo chmod 0710 "$(dirname "$ROOT")"
    # the bindings decide each member's tier
    exits_as dopsu 0 query status --brief -S "$SOCK"
    exits_as dopsu 0 sendevent ON_HOLD -J "$PROBE_JOB" -S "$SOCK"
    exits_as dopsu 0 sendevent OFF_HOLD -J "$PROBE_JOB" -S "$SOCK"
    stamp_request
    exits_as dreadu 0 query status --brief -S "$SOCK"
    exits_as dreadu 2 sendevent ON_HOLD -J "$PROBE_JOB" -S "$SOCK"
    grep -q 'access_denied' <<<"$out" || fail "the read tier's send: $out"
    exits_as dnobindu 2 query status --brief -S "$SOCK"
    grep -q 'access_denied' <<<"$out" || fail "a member with no binding: $out"
    exits_as dstranger 2 query status --brief -S "$SOCK"
    grep -q 'Permission denied' <<<"$out" || fail "a login outside the group: $out"
    [ "$(job_field "$PROBE_JOB" on_hold)" = false ] || fail "a refused ON_HOLD was applied"
    ok "ops sends, read only reads, an unbound member is denied, a stranger cannot connect"
    # the receipts: denials are access_denied records naming the principal
    denied=$(sudo jq -r -s '[.[] | select(.rec == "access_denied") | .principal] | unique | join(",")' \
        "$ROOT/perimeter.jsonl")
    [ "$denied" = dnobindu,dreadu ] || fail "access_denied receipts name: $denied"
    # each start's policy_loaded record names the map's digest
    [ "$(sudo jq -r -s '[.[] | select(.rec == "policy_loaded")] | last | .digest' "$ROOT/perimeter.jsonl")" = \
        "sha256:$(sudo sha256sum "$MAP" | cut -d' ' -f1)" ] || fail "policy_loaded names another digest"
    ok "perimeter.jsonl has access_denied receipts, and policy_loaded names the map's digest"
    # a reload that names another group is refused whole
    sudo sed -i "s/^socket_group = .*/socket_group = \"nightbank-ops\"/" "$MAP"
    before=$(perimeter_count policy_reload_failed 'socket_group is fixed at arming')
    sudo systemctl kill -s HUP --kill-who=main "$ENGINE"
    wait_for 30 "the reload that names another group is refused (policy_reload_failed)" \
        perimeter_count_exceeds policy_reload_failed 'socket_group is fixed at arming' "$before"
    is_state "$ENGINE" active || fail "the refused reload stopped the engine"
    exits_as dopsu 0 query status --brief -S "$SOCK"
    # execution profile: a changed option is a refusal on resume, exit 2.
    # The timezone is the release manifest's (DL-407), edited in place as
    # the refusal step edits a release's JIL
    sudo systemctl stop "$ENGINE"
    sudo cp "$SCRATCH/map.orig" "$MAP"
    sudo sed -i 's|^timezone = "UTC"$|timezone = "America/New_York"|' "$ESTATE/estate.toml"
    sudo grep -q 'America/New_York' "$ESTATE/estate.toml" || fail "the manifest edit did not apply"
    expect_refusal "runtime-profile mismatch"
    # put it all back
    sudo cp "$SCRATCH/manifest.orig" "$ESTATE/estate.toml"
    configure_restore
    start_engine
    [ "$(sudo stat -c '%a' "$SOCK")" = 600 ] || fail "the socket is not owner-only again"
    ok "the example's map and manifest are back; the socket is owner-only"
}

# deployment-runbook ss0's "Recipe: stop, restart, seal and recover", the
# parts no other step runs: an engine stop reads as no answer, a crash loop
# that the start limit ends and reset-failed clears, a lost answer replayed
# with the printed arguments, and a rerun as a different act. The refusal,
# the seal, the reboot and the estate stop are the refusal, sealed, reboot
# and quiesce steps'; the supervisor unit's restart is kill-supervisor's.
step_stop_recover() { # stop and recover: a crash loop, a replayed request, a new rerun
    local mark run first rc applied
    # engine stop and start
    sudo systemctl stop "$ENGINE"
    is_state "$ENGINE" inactive || fail "$ENGINE: $(prop "$ENGINE" ActiveState)"
    rc=0
    cli query status --brief -S "$SOCK" >/dev/null 2>&1 || rc=$?
    [ "$rc" = 2 ] || fail "status --brief exited $rc with the engine stopped, not 2"
    cli supervise list --run-root "$ROOT" | jq -e '.ok == true' >/dev/null ||
        fail "the supervisor did not outlive the engine stop"
    mark=$(date +%s)
    start_engine
    launch_line_since "$mark" | grep -q -- ' --resume$' || fail "the start was not a resume"
    ok "an engine stop is exit 2 on the socket; the start resumes the same root"
    # recover from a crash: the unit restarts the engine until the start limit
    mark=$(date +%s)
    kill_engine_until_failed
    ((KILLS >= 5 && KILLS <= 6)) || fail "the unit gave up after $KILLS kills, not 5 or 6"
    journal_has "$ENGINE" "$mark" 'repeated too quickly' || fail "the journal does not name the start limit"
    [ "$(prop "$ENGINE" ExecMainStatus)" = 9 ] || fail "exit $(prop "$ENGINE" ExecMainStatus), not a SIGKILL"
    ok "after $KILLS kills the unit stays failed (StartLimitBurst=5)"
    # fix the cause, reset-failed, start
    mark=$(date +%s)
    start_engine
    launch_line_since "$mark" | grep -q -- ' --resume$' || fail "the recovery was not a resume"
    ok "reset-failed and a start recover the engine on the same root"
    # a lost answer: the same arguments, with the printed pins, answer from the first decision
    run=$(job_field "$FAIL_JOB" run_number)
    send_keeping_retry FORCE_STARTJOB "$FAIL_JOB"
    first=$ANSWER
    wait_for 60 "$FAIL_JOB ended in SUCCESS on its rerun" ran_to_success "$FAIL_JOB" $((run + 1))
    applied=$(cli query trace --since 0 -S "$SOCK" | jq -r .applied_index)
    # shellcheck disable=SC2086  # RETRY_LINE is the printed arguments, split on purpose
    ANSWER=$(cli sendevent FORCE_STARTJOB -J "$FAIL_JOB" -S "$SOCK" $RETRY_LINE 2>/dev/null) ||
        fail "the replayed request was refused: $ANSWER"
    [ "$(jq -S -c '{request_id, decision, index}' <<<"$ANSWER")" = \
        "$(jq -S -c '{request_id, decision, index}' <<<"$first")" ] ||
        fail "the replay was not answered from the first decision: $ANSWER"
    [ "$(cli query trace --since 0 -S "$SOCK" | jq -r .applied_index)" = "$applied" ] ||
        fail "the replay applied something"
    [ "$(job_field "$FAIL_JOB" run_number)" = $((run + 1)) ] || fail "the replay started another run"
    ok "the replayed request got the first answer and applied nothing twice"
    # a new request id is a different act: the next run number
    request FORCE_STARTJOB "$FAIL_JOB"
    wait_for 60 "$FAIL_JOB ran again" ran_to_success "$FAIL_JOB" $((run + 2))
    ok "a fresh request id started run $((run + 2))"
}

# deployment-runbook ss0 and ss3: a SIGKILL of the engine's main process.
# systemd restarts the unit, the launcher resumes the root, and a detached
# command keeps running under the supervisor through it.
step_kill_engine() { # SIGKILL of the engine: systemd restarts it and the run resumes
    local pids pid main restarts mark run
    force_start "$LONG_JOB"
    wait_for 60 "$LONG_JOB is RUNNING" job_is "$LONG_JOB" RUNNING
    run=$(job_field "$LONG_JOB" run_number)
    pids=$(pgrep -f "fakework $LONG_JOB") || fail "no $LONG_JOB process"
    main=$(prop "$ENGINE" MainPID)
    restarts=$(prop "$ENGINE" NRestarts)
    mark=$(date +%s)
    sudo kill -KILL "$main"
    wait_for 60 "systemd restarted the engine" unit_replaced "$ENGINE" "$main"
    wait_up
    [ "$(prop "$ENGINE" NRestarts)" = $((restarts + 1)) ] || fail "NRestarts did not rise by one"
    journal_has "$ENGINE" "$mark" 'status=9/KILL' || fail "the journal does not say status=9/KILL"
    launch_line_since "$mark" | grep -q -- ' --resume$' || fail "the restart was not a resume"
    for pid in $pids; do
        sudo kill -0 "$pid" || fail "$pid died with the engine"
    done
    job_is "$LONG_JOB" RUNNING || fail "after the restart: $(job_status "$LONG_JOB")"
    [ "$(job_field "$LONG_JOB" run_number)" = "$run" ] || fail "the restart started another run"
    wait_for 180 "$LONG_JOB ended in SUCCESS" job_is "$LONG_JOB" SUCCESS
    [ "$(cli query status --job APAC_EOD_B -S "$SOCK" | jq -r '.jobs.APAC_EOD_B.on_hold')" = true ] ||
        fail "the hold did not survive the kill"
    ok "after a SIGKILL the unit resumed the root; $LONG_JOB kept running to SUCCESS"
}

# deployment-runbook ss0 and ss3: a SIGKILL of the supervisor's main
# process. Part 1 runs the shipped units: systemd restarts the supervisor and
# Requires= carries the restart to the engine, which resumes and takes a lease
# that renews. Part 2 is a drill-only edit of the installed units, not a
# supported configuration (the runbook requires Requires=): Wants= for
# Requires= in the engine unit, and RestartSec=40 in the supervisor unit.
# It keeps the engine up through the outage to drive the engine's own
# client: it reports the host unreachable after five failed renewals and
# renews again when the supervisor returns (the renewal loop never ends
# while the client is open). The shipped units never reach that path.
kill_supervisor_cleanup() {
    restore_units 2>/dev/null || true
}
step_kill_supervisor() { # SIGKILL of the supervisor: restart, resume and lease renewal
    local sup eng_inv eng_pid holder was mark run restarts
    trap kill_supervisor_cleanup EXIT
    # part 1: the shipped units
    force_start "$LONG_JOB"
    wait_for 60 "$LONG_JOB is RUNNING" job_is "$LONG_JOB" RUNNING
    run=$(job_field "$LONG_JOB" run_number)
    sup=$(prop "$SUPERVISOR" MainPID)
    eng_inv=$(prop "$ENGINE" InvocationID)
    restarts=$(prop "$ENGINE" NRestarts)
    mark=$(date +%s)
    sudo kill -KILL "$sup"
    wait_for 60 "systemd restarted the supervisor" unit_replaced "$SUPERVISOR" "$sup"
    journal_has "$SUPERVISOR" "$mark" 'status=9/KILL' || fail "the journal does not say status=9/KILL"
    wait_for 30 "the supervisor unit's journal records the new start" \
        journal_job "$SUPERVISOR" "$mark" Started
    wait_for 90 "the engine unit began a new invocation" engine_reinvoked "$eng_inv"
    # a stop job from Requires=, not the unit's own on-failure restart
    journal_job "$ENGINE" "$mark" Stopping || fail "no stop of the engine unit in its journal"
    [ "$(prop "$ENGINE" NRestarts)" -le "$restarts" ] || fail "the engine unit restarted itself"
    wait_up
    launch_line_since "$mark" | grep -q -- ' --resume$' || fail "the engine did not resume"
    wait_for 60 "the engine holds the lease" lease_is_held
    was=$(lease_field expires_at)
    wait_for 60 "the lease renews" lease_renewed_past "$was"
    wait_for 60 "$LONG_JOB's command is recorded as ended" job_not_running "$LONG_JOB"
    [ "$(job_field "$LONG_JOB" run_number)" = "$run" ] || fail "the restart brought the command back"
    run=$(job_field "$SHORT_JOB" run_number)
    force_start "$SHORT_JOB"
    wait_for 60 "$SHORT_JOB ran on the new supervisor" ran_to_success "$SHORT_JOB" $((run + 1))
    ok "the supervisor restarted and ended its command; the engine resumed and its lease renews"
    # part 2: the engine outlives the supervisor's outage
    sudo sed -i 's/^Requires=dsl41-supervisor.service/Wants=dsl41-supervisor.service/' \
        "/etc/systemd/system/$ENGINE"
    sudo install -d "/etc/systemd/system/$SUPERVISOR.d"
    printf '[Service]\nRestartSec=40\n' | sudo tee "/etc/systemd/system/$SUPERVISOR.d/drill.conf" >/dev/null
    sudo systemctl daemon-reload
    manual restart
    wait_up
    wait_for 60 "the engine holds the lease" lease_is_held
    sup=$(prop "$SUPERVISOR" MainPID)
    eng_pid=$(prop "$ENGINE" MainPID)
    eng_inv=$(prop "$ENGINE" InvocationID)
    holder=$(lease_field holder)
    mark=$(date +%s)
    sudo kill -KILL "$sup"
    wait_for 60 "the engine reports the host unreachable" \
        journal_has "$ENGINE" "$mark" 'supervisor lease renewal failed 5 times'
    wait_for 90 "systemd restarted the supervisor after RestartSec" unit_replaced "$SUPERVISOR" "$sup"
    wait_for 60 "the engine renewed its lease after the outage" \
        journal_has "$ENGINE" "$mark" 'supervisor lease renewed after'
    [ "$(prop "$ENGINE" MainPID)" = "$eng_pid" ] && [ "$(prop "$ENGINE" InvocationID)" = "$eng_inv" ] ||
        fail "the engine was restarted"
    [ "$(lease_field holder)" = "$holder" ] || fail "the lease changed hands"
    was=$(lease_field expires_at)
    wait_for 60 "the lease keeps renewing" lease_renewed_past "$was"
    ok "the engine outlived the outage: five failed renewals, then the lease renewed"
    run=$(job_field "$SHORT_JOB" run_number)
    force_start "$SHORT_JOB"
    wait_for 60 "$SHORT_JOB ran after the outage" ran_to_success "$SHORT_JOB" $((run + 1))
    restore_units
    manual restart
    wait_up
}

# The application's release train (DL-407): the deployment identity prepares,
# applies and rolls back releases with `dsl41 release`, through the window of
# deployment-runbook ss6a, and changes no file the infrastructure owns. The
# steps run in order: each starts from the release the last one left active.

# a release's receipt, one field; the active release's by default
receipt() { # receipt FIELD [RELEASE]
    local dir=$ESTATE_RELEASES/${2:-$RELEASE}/.release
    sudo jq -r --arg f "$1" '.[$f]' "$dir/$(sudo cat "$dir/latest")"
}
# exactly FILE differs between the releases' inputs
only_differs() { # only_differs FROM TO FILE
    local out
    out=$(sudo diff -rq -x .release "$1" "$2" || true)
    [ "$out" = "Files $1/$3 and $2/$3 differ" ] || fail "the release changes more than $3: $out"
}

step_release() { # a prepared release that adds a job file and changes a job, applied by dsl41 release
    local before src
    release_setup
    before=$(infra_state)
    # v1: the active release's inputs, a properties layer of the release's own
    # and a job that reads it
    src=$(release_source small-v1 "$ESTATE")
    cat >"$src/probe.jil" <<'JIL'
insert_job: RELEASE_PROBE_C
job_type: c
command: echo probe ~{$PROBE_WORD}~
machine: localhost
owner: dsl41
JIL
    echo 'PROBE_WORD=v1' >"$src/release.properties"
    sed -i -e 's/^files = \[\(.*\)\]$/files = [\1, "probe.jil"]/' \
        -e '/^timezone = /i properties = ["release.properties"]' "$src/estate.toml"
    deploy prepare small-v1 "$src"
    release_apply small-v1
    spec_has RELEASE_PROBE_C "echo probe v1" || fail "the probe job does not read the layer"
    # v2: one more job file, and the probe job changed
    src=$(release_source small-v2 "$ESTATE")
    cat >"$src/added.jil" <<'JIL'
insert_job: RELEASE_ADDED_C
job_type: c
command: echo added
machine: localhost
owner: dsl41
JIL
    sed -i 's/^command: echo probe /command: echo changed /' "$src/probe.jil"
    sed -i 's/"probe.jil"\]$/"probe.jil", "added.jil"]/' "$src/estate.toml"
    grep -qF '"added.jil"]' "$src/estate.toml" || fail "the manifest edit did not apply"
    deploy prepare small-v2 "$src"
    # one scheduled job the apply must hold and release itself; the request
    # also makes the seal wait out the retry horizon
    request OFF_HOLD EMEA_EOD_B
    release_apply small-v2
    spec_has RELEASE_ADDED_C "echo added" || fail "the added job has no definition"
    spec_has RELEASE_PROBE_C "echo changed v1" || fail "the changed job is not the new definition"
    [ "$(receipt previous)" = small-v1 ] || fail "the receipt does not name small-v1"
    [ "$(receipt holds | jq -c '[.[] | select(.state == "released") | .job]')" = \
        "$(receipt released | jq -c .)" ] ||
        fail "the apply's holds: $(receipt holds), released: $(receipt released)"
    receipt released | jq -e 'index("EMEA_EOD_B") != null and index("APAC_EOD_B") == null' \
        >/dev/null || fail "the apply released $(receipt released)"
    [ "$(job_field EMEA_EOD_B on_hold)" = false ] || fail "the apply kept its hold"
    [ "$(job_field APAC_EOD_B on_hold)" = true ] || fail "the apply released a hold it did not take"
    request ON_HOLD EMEA_EOD_B
    # one run, so the job has history to keep when a release removes it
    force_start RELEASE_ADDED_C
    wait_for 60 "RELEASE_ADDED_C ended in SUCCESS" job_is RELEASE_ADDED_C SUCCESS
    [ "$(infra_state)" = "$before" ] || fail "the release changed a file the infrastructure owns"
    ok "small-v2 is active: the added job answers, the changed job is new, the launcher, units and runtime link are unchanged"
}

step_release_rollback() { # rollback to v1 by the same path; the removed job stays a ghost
    local before rc
    before=$(infra_state)
    release_apply rollback
    [ "$RELEASE" = small-v1 ] || fail "the rollback activated $RELEASE"
    [ "$(receipt previous)" = small-v2 ] || fail "the receipt does not name small-v2"
    rc=0
    cli query spec -J RELEASE_ADDED_C -S "$SOCK" >/dev/null 2>&1 || rc=$?
    [ "$rc" = 2 ] || fail "query spec of the removed job exited $rc, not 2"
    [ "$(job_field RELEASE_ADDED_C job_type)" = null ] || fail "the removed job is not a ghost"
    [ "$(job_field RELEASE_ADDED_C run_number)" = 1 ] || fail "the ghost lost its history"
    spec_has RELEASE_PROBE_C "echo probe v1" || fail "the probe job is not v1's again"
    [ "$(infra_state)" = "$before" ] || fail "the rollback changed a file the infrastructure owns"
    ok "rolled back to small-v1: RELEASE_ADDED_C is a ghost with its run, the launcher, units and runtime link are unchanged"
}

step_release_properties_only() { # a release that changes only its properties layer
    local src from=$ESTATE
    src=$(release_source small-v3 "$ESTATE")
    echo 'PROBE_WORD=v3' >"$src/release.properties"
    only_differs "$from" "$src" release.properties
    deploy prepare small-v3 "$src"
    release_apply small-v3
    spec_has RELEASE_PROBE_C "echo probe v3" || fail "the probe job does not read the new layer"
    [ "$(receipt source_bundle_hash)" != "$(receipt source_bundle_hash small-v1)" ] ||
        fail "the bundle hash did not move"
    [ "$(receipt period)" -gt "$(receipt period small-v1)" ] || fail "no new period opened"
    ok "a properties-only release moved the bundle hash and opened period $(receipt period)"
}

step_release_calendar_only() { # a release that changes only a calendar
    local src from=$ESTATE hash
    src=$(release_source small-v4 "$ESTATE")
    sed -i 's|^01/01/2027$|01/01/2027\n01/01/2028|' "$src/calendars.jil"
    only_differs "$from" "$src" calendars.jil
    deploy prepare small-v4 "$src"
    release_apply small-v4
    hash=$(receipt source_bundle_hash)
    [ "$hash" != "$(receipt source_bundle_hash small-v3)" ] || fail "the bundle hash did not move"
    sudo grep -rqF 01/01/2028 "$ROOT/catalogs/${hash#sha256:}" ||
        fail "the opened bundle does not hold the new holiday"
    ok "a calendar-only release moved the bundle hash and opened period $(receipt period)"
}

step_release_concurrent() { # a second apply is refused while the first holds the lock
    local src first rc out
    src=$(release_source small-v5 "$ESTATE")
    deploy prepare small-v5 "$src"
    # the first apply holds EMEA_EOD_B and waits out the retry horizon, holding the lock
    request OFF_HOLD EMEA_EOD_B
    sudo systemctl reset-failed "$ENGINE" 2>/dev/null || true
    deploy apply small-v5 >"$SCRATCH/release-first.log" 2>&1 &
    first=$!
    wait_for 60 "the first apply recorded itself" sudo test -e "$APPLY_STATE/apply.json"
    rc=0
    out=$(deploy rollback 2>&1) || rc=$?
    echo "$out"
    [ "$rc" = 2 ] || fail "the second apply exited $rc, not 2"
    grep -qE "another apply holds $APPLY_STATE/apply.lock: pid [0-9]+, $DEPLOY_USER@" <<<"$out" ||
        fail "the refusal does not name the holder"
    rc=0
    wait "$first" || rc=$?
    cat "$SCRATCH/release-first.log"
    [ "$rc" = 0 ] || fail "the first apply exited $rc"
    follow_active
    [ "$RELEASE" = small-v5 ] || fail "ACTIVE names $RELEASE, not small-v5"
    wait_up
    request ON_HOLD EMEA_EOD_B
    ok "the second apply was refused naming the first, which completed"
}

# the committed seals in ROOT
seal_count() { sudo find "$ROOT/seals" -name '[0-9][0-9][0-9][0-9][0-9][0-9].json' | wc -l; }
# One apply of a copy of the estate's own inputs, killed at STOP: the record
# keeps STEP, ACTIVE still names FROM, the seal committed once, and the
# engine sealed and exited 3; `status` says what resume does, and the resume
# completes with no second seal.
interrupted_apply() { # interrupted_apply NAME FROM STOP STEP SAYS
    local src rc seals out
    src=$(release_source "$1" "$NIGHTBANK/estate/${DRILL_ESTATE:-small}")
    deploy prepare "$1" "$src"
    seals=$(seal_count)
    sudo systemctl reset-failed "$ENGINE" 2>/dev/null || true
    rc=0
    deploy_stopped "$3" apply "$1" || rc=$?
    [ "$rc" != 0 ] || fail "the apply was not stopped at $3"
    [ "$(sudo jq -r .step "$APPLY_STATE/apply.json")" = "$4" ] || fail "the record is not at $4"
    [ "$(cat "$ACTIVE")" = "$2" ] || fail "ACTIVE moved before the apply wrote it"
    [ "$(seal_count)" = $((seals + 1)) ] || fail "the seal did not commit"
    wait_for 90 "$ENGINE is failed" is_state "$ENGINE" failed
    [ "$(prop "$ENGINE" ExecMainStatus)" = 3 ] || fail "exit $(prop "$ENGINE" ExecMainStatus), not 3"
    out=$(deploy status)
    echo "$out"
    grep -qF "recorded apply: $1 over $2, started " <<<"$out" || fail "status does not show the apply"
    grep -qF "$5" <<<"$out" || fail "status does not say what resume does"
    sudo systemctl reset-failed "$ENGINE" 2>/dev/null || true
    deploy resume
    follow_active
    [ "$RELEASE" = "$1" ] || fail "ACTIVE names $RELEASE, not $1"
    [ "$(seal_count)" = $((seals + 1)) ] || fail "the resume sealed again"
    ! sudo test -e "$APPLY_STATE/apply.json" || fail "the record is still there"
    wait_up
    ok "the apply killed at $3 resumed from its record with no second seal; $1 is active"
}

step_release_interrupted() { # the apply killed around the seal: status shows it, resume completes it
    # the estate's own inputs, so the release the steps leave active has the
    # file set of the release the drill installed
    interrupted_apply small-v6 small-v5 seal-reply sealing \
        'will ask the estate whether the seal under request id'
    interrupted_apply small-v7 small-v6 seal sealed \
        'will name small-v7 in ACTIVE, start the engine, check the new period, and release the holds'
    release_teardown
}

# ss7 row 1, a patch marked resume-safe: stop the engine unit, flip, start
# it again; the supervisor unit and its detached job are kept. Rollback is
# the same with the old venv.
step_upgrade_resume_safe() { # resume-safe row: engine restarted on the flipped venv, supervisor kept
    local sup_pid pids pid mark
    echo "two installs of one build: this exercises the row, it qualifies no version pair"
    install_build ui
    force_start "$LONG_JOB"
    wait_for 60 "$LONG_JOB is RUNNING" job_is "$LONG_JOB" RUNNING
    pids=$(pgrep -f "fakework $LONG_JOB") || fail "no $LONG_JOB process"
    sup_pid=$(prop "$SUPERVISOR" MainPID)
    sudo systemctl stop "$ENGINE"
    flip "$BUILD_B"
    mark=$(date +%s)
    start_engine
    launch_line_since "$mark" | grep -q -- ' --resume$' || fail "the upgrade was not a resume"
    on_build "$ENGINE" "$BUILD_B"
    [ "$(prop "$SUPERVISOR" MainPID)" = "$sup_pid" ] || fail "the supervisor was replaced"
    for pid in $pids; do
        sudo kill -0 "$pid" || fail "$pid died across the upgrade"
    done
    job_is "$LONG_JOB" RUNNING || fail "after the upgrade: $(job_status "$LONG_JOB")"
    ok "the engine resumed on $BUILD_B; the supervisor and $LONG_JOB were kept"
    # rollback: flip back, resume
    sudo systemctl stop "$ENGINE"
    flip "$BUILD_A"
    mark=$(date +%s)
    start_engine
    launch_line_since "$mark" | grep -q -- ' --resume$' || fail "the rollback was not a resume"
    [ "$(prop "$SUPERVISOR" MainPID)" = "$sup_pid" ] || fail "the supervisor was replaced"
    wait_for 180 "$LONG_JOB ended in SUCCESS" job_is "$LONG_JOB" SUCCESS
    ok "rolled back to $BUILD_A by the same three commands"
}

# ss7 row 2, a silent or unclear note: at a boundary, the next period opens
# in a fresh run root (a physical roll) on the new venv; the lineage and
# its anchor are kept. The launcher's one-shot open trigger makes the
# engine unit run the opener, so the new root's supervisor and jobs live in
# the units' cgroups. The rollback is the same procedure with the old venv
# and another fresh root.
# the period a roll of ROOT opens: the one after its newest segment, which the
# roll's seal closes
next_period() { # next_period ROOT
    local newest
    newest=$(sudo find "$1/wal" -name '[0-9]*.jsonl' | sort | tail -n 1)
    [ -n "$newest" ] || fail "$1 holds no segment"
    newest=${newest##*/}
    echo $((10#${newest%.jsonl} + 1))
}
roll_into_fresh_root() { # roll_into_fresh_root NEW_ROOT BUILD PERIOD
    local new=$1 build=$2 period=$3 mark line
    wait_for 60 "nothing is live under the supervisor" nothing_live "$ROOT"
    take_hold
    quiesce_shape1
    flip "$build"
    provision_root "$new"
    point_launcher "$new" "$ANCHOR"
    as_dsl41 touch "$new.open-from"
    line=$(sudo -u dsl41 "$LAUNCH" --print)
    [[ $line == *" --open-from $ANCHOR" ]] || fail "the trigger did not make an opener: $line"
    ROOT=$new
    SOCK=$ROOT/control.sock
    mark=$(date +%s)
    start_engine
    is_state "$ENGINE" active || fail "$ENGINE: $(prop "$ENGINE" ActiveState)"
    launch_line_since "$mark" | grep -qF -- " --open-from $ANCHOR" || fail "the unit did not open $ROOT"
    engine_log_since "$mark" | grep -qF "opened period $period in $ROOT" || fail "no roll into $ROOT"
    ! sudo test -e "$ROOT.open-from" || fail "the open trigger is still there"
    sudo test -e "$ROOT/wal/$(printf %06d "$period").jsonl" || fail "period $period did not open in $ROOT"
    on_build "$ENGINE" "$build"
    on_build "$SUPERVISOR" "$build"
    ok "the engine unit opened period $period in $ROOT; both units run from $build"
    # one shot: the next start resumes the opened root
    mark=$(date +%s)
    manual restart
    wait_up
    launch_line_since "$mark" | grep -q -- ' --resume$' || fail "the restart did not resume $ROOT"
    ok "the unit's next start resumed $ROOT"
    release_hold
}

step_upgrade_fresh_root() { # silent-note row: the next period opens in a fresh root on the flipped venv
    echo "two installs of one build: this exercises the row, it qualifies no version pair"
    roll_into_fresh_root "$ROLLED_ROOT" "$BUILD_B" "$(next_period "$ROOT")"
}

step_upgrade_fresh_root_rollback() { # silent-note row's rollback: the old venv, another fresh root
    echo "two installs of one build: this exercises the row, it qualifies no version pair"
    ROOT=$ROLLED_ROOT
    SOCK=$ROOT/control.sock
    roll_into_fresh_root "$ROLLBACK_ROOT" "$BUILD_A" "$(next_period "$ROOT")"
}

# ss7 row 3, resume-safe with a wrapper or supervisor protocol change:
# drain, stop both units, flip, start both; the engine resumes the same
# root and the supervisor is replaced by the new venv's. The rollback is the
# same with the old venv.
flip_both_units() { # flip_both_units BUILD
    local mark sup_pid held
    wait_for 60 "nothing is live under the supervisor" nothing_live "$ROOT"
    sup_pid=$(prop "$SUPERVISOR" MainPID)
    sudo systemctl stop "$ENGINE"
    maintenance_stop
    flip "$1"
    mark=$(date +%s)
    start_engine
    is_state "$SUPERVISOR" active || fail "Requires= did not start $SUPERVISOR"
    launch_line_since "$mark" | grep -q -- ' --resume$' || fail "the engine did not resume $ROOT"
    [ "$(prop "$SUPERVISOR" MainPID)" != "$sup_pid" ] || fail "the supervisor was not replaced"
    on_build "$ENGINE" "$1"
    on_build "$SUPERVISOR" "$1"
    held=$(cli query status --job APAC_EOD_B -S "$SOCK" | jq -r '.jobs.APAC_EOD_B.on_hold')
    [ "$held" = true ] || fail "the hold did not survive the flip"
    ok "both units run from $1; the engine resumed $ROOT with its holds"
}

step_upgrade_coordinated() { # coordinated row: both units replaced, the engine resumes the same root
    echo "two installs of one build: this exercises the row, it qualifies no version pair"
    ROOT=$ROLLBACK_ROOT
    SOCK=$ROOT/control.sock
    flip_both_units "$BUILD_B"
}

step_upgrade_coordinated_rollback() { # coordinated row's rollback: flip back, replace again
    echo "two installs of one build: this exercises the row, it qualifies no version pair"
    ROOT=$ROLLBACK_ROOT
    SOCK=$ROOT/control.sock
    flip_both_units "$BUILD_A"
}

# The old estate for ss7's state-machine row: v1.7.0 from PyPI, as runbook
# ss1 installs a release that has no assets, on a new estate of its own.
step_upgrade_old_release() { # an estate on the old release, v1.7.0 from PyPI
    local mark line version rc refused
    ROOT=$ROLLBACK_ROOT
    SOCK=$ROOT/control.sock
    wait_for 60 "nothing is live under the supervisor" nothing_live "$ROOT"
    sudo systemctl stop "$ENGINE"
    maintenance_stop
    sudo python3.12 -m venv "$OLD_VENV"
    sudo "$OLD_VENV/bin/pip" install --quiet --no-index --find-links "$SCRATCH/historical-wheels" "dsl41==$OLD_VERSION"
    sudo "$OLD_VENV/bin/pip" check
    version=$(state_machine_version "$OLD_VENV")
    [ "$version" = 1 ] || fail "$OLD_VENV implements state-machine version $version"
    flip "$OLD_VENV"
    # the current launcher refuses a runtime without estate args (DL-407)
    rc=0
    refused=$(sudo -u dsl41 "$LAUNCH" --print 2>&1) || rc=$?
    if [ "$rc" != 2 ] || ! grep -qF 'this launcher needs dsl41 1.13.0 or later' <<<"$refused"; then
        fail "the current launcher on v$OLD_VERSION exited $rc: $refused"
    fi
    ok "the current launcher refuses v$OLD_VERSION and names the release it needs"
    install_old_launcher
    provision_root "$OLD_ROOT"
    point_launcher "$OLD_ROOT" "$OLD_ROOT.anchor"
    ROOT=$OLD_ROOT
    SOCK=$ROOT/control.sock
    mark=$(date +%s)
    start_engine
    line=$(launch_line_since "$mark")
    [[ $line != *--resume* ]] || fail "the old release's first start was a resume"
    hold_scheduled
    # one detached command under the old release's supervisor, for the
    # drain and the audit to have work in them
    force_start "$SHORT_JOB"
    wait_for 60 "$SHORT_JOB ended in SUCCESS" job_is "$SHORT_JOB" SUCCESS
    ok "v$OLD_VERSION (state-machine version 1) leads $ROOT under both units"
}

# ss7 row 4, a state-machine version change: drain, stop both units, flip,
# start a new estate on the new build, and keep the old venv to audit the
# retained old estate.
step_upgrade_state_machine() { # state-machine row: v1.7.0 to this build, a new estate
    local mark line version refused rc
    ROOT=$OLD_ROOT
    ANCHOR=$OLD_ROOT.anchor
    SOCK=$ROOT/control.sock
    wait_for 60 "nothing is live under the supervisor" nothing_live "$ROOT"
    take_hold
    quiesce_shape1 legacy-incident
    flip "$BUILD_A"
    version=$(state_machine_version "$BUILD_A")
    [ "$version" != 1 ] || fail "$BUILD_A implements state-machine version 1 too"
    restore_launcher
    provision_root "$NEW_ROOT"
    point_launcher "$NEW_ROOT" "$NEW_ROOT.anchor"
    ROOT=$NEW_ROOT
    SOCK=$ROOT/control.sock
    mark=$(date +%s)
    start_engine
    is_state "$SUPERVISOR" active || fail "Requires= did not start $SUPERVISOR"
    line=$(launch_line_since "$mark")
    [[ $line != *--resume* ]] || fail "the new estate's first start was a resume"
    ok "this build (state-machine version $version) leads the new estate $ROOT"
    release_hold
    # the retained old estate: the kept old venv audits it, the new build
    # refuses it and names the version
    as_dsl41 "$OLD_VENV/bin/dsl41" audit --estate-anchor "$OLD_ROOT.anchor"
    ok "the kept v$OLD_VERSION venv audits the retained estate"
    rc=0
    refused=$(cli audit --estate-anchor "$OLD_ROOT.anchor" 2>&1) || rc=$?
    echo "$refused"
    [ "$rc" = 2 ] || fail "this build's audit of a state-machine version 1 period exited $rc, not 2: $refused"
    # The refusal is the seal artifact's "not in ss3.2 canonical form (corrupt
    # bytes, or a seal written by a build with a different record shape)".
    # It names neither the version nor the shape: this build reads the seal
    # before the period's state-machine version. The check that the old venv
    # is the one to use is the audit just above, which passes on the same
    # anchor.
    grep -qF 'not in ss3.2 canonical form' <<<"$refused" ||
        fail "the refusal is not the seal artifact's canonical-form message: $refused"
    ok "this build refuses the old estate (exit 2); the kept v$OLD_VERSION venv audits it"
}

# ss7 row 4's rollback: stop both units, flip back to the old venv, point the
# launcher at the old estate, start; the engine opens the old estate's next
# period on the old build, with the old estate's holds. The drill then goes
# forward again, to the new estate the retirement works on.
step_upgrade_state_machine_rollback() { # state-machine row's rollback: the old venv on the old estate
    local mark line held
    ROOT=$NEW_ROOT
    wait_for 60 "nothing is live under the supervisor" nothing_live "$ROOT"
    sudo systemctl stop "$ENGINE"
    maintenance_stop
    flip "$OLD_VENV"
    install_old_launcher
    point_launcher "$OLD_ROOT" "$OLD_ROOT.anchor"
    ROOT=$OLD_ROOT
    ANCHOR=$OLD_ROOT.anchor
    SOCK=$ROOT/control.sock
    mark=$(date +%s)
    start_engine
    is_state "$SUPERVISOR" active || fail "Requires= did not start $SUPERVISOR"
    line=$(launch_line_since "$mark")
    echo "$line"
    [[ $line == *" --run-root $ROOT "* && $line == *" --resume" ]] ||
        fail "the rollback was not a resume of $ROOT"
    on_build "$ENGINE" "$OLD_VENV"
    on_build "$SUPERVISOR" "$OLD_VENV"
    sudo test -e "$ROOT/wal/000002.jsonl" || fail "the old estate's period 2 did not open in $ROOT"
    held=$(cli query status --job APAC_EOD_B -S "$SOCK" | jq -r '.jobs.APAC_EOD_B.on_hold')
    [ "$held" = true ] || fail "the old estate's hold did not survive"
    ok "the old estate answers on v$OLD_VERSION with period 2 open and its holds"
    # forward again: the new estate resumes where the upgrade left it
    sudo systemctl stop "$ENGINE"
    incident_stop_supervisor "historical rollback complete; replace the old supervisor deliberately"
    flip "$BUILD_A"
    restore_launcher
    point_launcher "$NEW_ROOT" "$NEW_ROOT.anchor"
    ROOT=$NEW_ROOT
    ANCHOR=$NEW_ROOT.anchor
    SOCK=$ROOT/control.sock
    mark=$(date +%s)
    start_engine
    line=$(launch_line_since "$mark")
    [[ $line == *" --run-root $ROOT "* && $line == *" --resume" ]] ||
        fail "the return was not a resume of $ROOT"
    on_build "$ENGINE" "$BUILD_A"
    ok "the new estate resumed on $BUILD_A"
}

# deployment-runbook ss0's retirement, on the estate row 4 started: take
# the reboot hold, quiesce through ss2b's live-seal branch (the engine
# ends failed with exit 3), remove the units with copies
# kept, and check that a reload, a boot's target and a stray start start
# nothing, and that the retained history audits. Then the site's deletion
# of one whole set, the v1.7.0 estate: no kept anchor names its root, and
# afterwards no kept anchor names a root that is gone.
step_retire() { # retirement: the estate stays stopped after its units go, the history audits
    local out unit anchor root named retained list anchors
    ROOT=$NEW_ROOT
    ANCHOR=$NEW_ROOT.anchor
    SOCK=$ROOT/control.sock
    assert_enablement enabled
    take_hold
    for unit in "$ENGINE" "$SUPERVISOR"; do
        is_state "$unit" active || fail "disabling stopped $unit"
    done
    ok "both units disabled and still running"
    wait_for 60 "nothing is live under the supervisor" nothing_live "$ROOT"
    # ss2b's live-seal branch this time: the engine ends failed with exit 3,
    # which retire-remove's reset-failed must clear
    next_args
    wait_out_retry_horizon
    cli seal --run-root "$ROOT" --estate-anchor "$ANCHOR" "${NEXT[@]}" -p "$PROPERTIES" \
        --next-detached --next-as-machine localhost --next-machine-policy strict \
        --claimed-actor drill@nightbank
    wait_for 90 "$ENGINE is failed" is_state "$ENGINE" failed
    [ "$(prop "$ENGINE" ExecMainStatus)" = 3 ] || fail "exit $(prop "$ENGINE" ExecMainStatus), not 3"
    cli audit --run-root "$ROOT" --estate-anchor "$ANCHOR"
    maintenance_stop
    assert_units_stay_stopped
    is_state "$ENGINE" failed || fail "$ENGINE: $(prop "$ENGINE" ActiveState)"
    run_recipe retire-remove RUN_ROOT="$ROOT"
    retained=/srv/dsl41/retained/${ROOT##*/}
    for unit in "$ENGINE" "$SUPERVISOR" dsl41-launch nightbank-access.toml; do
        sudo test -f "$retained/$unit" || fail "no copy of $unit in $retained"
    done
    out=$(run_recipe retire-check)
    echo "$out"
    [ "$out" = $'LoadState=not-found\nActiveState=inactive\nLoadState=not-found\nActiveState=inactive' ] ||
        fail "retire-check printed: $out"
    # container only: a boot's target and a stray start find no unit
    boot_target
    ! sudo systemctl start "$ENGINE" 2>/dev/null || fail "$ENGINE started after its removal"
    for unit in "$ENGINE" "$SUPERVISOR"; do
        [ "$(prop "$unit" ActiveState)" = inactive ] || fail "$unit is $(prop "$unit" ActiveState)"
    done
    assert_no_writers "$ROOT"
    ok "the retired estate stays stopped: no unit, nothing running"
    run_recipe --as-dsl41 retire-audit ESTATE_ANCHOR="$ANCHOR"
    ok "the retired estate's history audits"
    # the site deletes the v1.7.0 set: its anchor and the one root it names
    [ "$(roots_named "$OLD_ROOT.anchor" "$OLD_VENV")" = "$OLD_ROOT" ] ||
        fail "$OLD_ROOT.anchor does not name $OLD_ROOT alone"
    # an array, not a read loop: the recipes the loops run read stdin
    list=$(anchors_in "$(dirname "$ROOT")")
    mapfile -t anchors <<<"$list"
    for anchor in "${anchors[@]}"; do
        [ "$anchor" = "$OLD_ROOT.anchor" ] && continue
        named=$(roots_named "$anchor")
        echo "$anchor names: $named"
        ! grep -qxF -- "$OLD_ROOT" <<<"$named" || fail "$anchor names $OLD_ROOT"
    done
    sudo rm -rf "$OLD_ROOT" "$OLD_ROOT.anchor"
    list=$(anchors_in "$(dirname "$ROOT")")
    mapfile -t anchors <<<"$list"
    for anchor in "${anchors[@]}"; do
        named=$(roots_named "$anchor")
        [ -n "$named" ] || fail "$anchor names no root"
        while read -r root; do
            sudo test -d "$root" || fail "$anchor names $root, which is gone"
        done <<<"$named"
        run_recipe --as-dsl41 retire-audit ESTATE_ANCHOR="$anchor"
    done
    ok "every kept anchor names roots that exist, and each kept lineage audits"
}

# the roots ANCHOR's registry names, one per line, from the retire-list
# recipe run with VENV's dsl41 (the lineage's state-machine version)
roots_named() { # roots_named ANCHOR [VENV]
    run_recipe --as-dsl41 retire-list ESTATE_ANCHOR="$1" PATH="${2:-$VENV}/bin:/usr/bin:/bin" |
        sed -n 's/^  \(\/[^:]*\): .*/\1/p'
}

step_diagnostics() { # diagnostics and teardown
    local load active
    sudo systemctl status "$ENGINE" "$SUPERVISOR" --no-pager || true
    sudo journalctl -u "$ENGINE" -u "$SUPERVISOR" -o short-precise --no-pager || true
    sudo sh -c 'tail -n 100 /srv/dsl41/runs/*/supervisor.log' || true
    # Removed units must be inactive; loaded units retain strict shutdown errors (DL-390).
    load=$(prop "$ENGINE" LoadState)
    case $load in
        not-found)
            active=$(prop "$ENGINE" ActiveState)
            [ "$active" = inactive ] || fail "$ENGINE: not-found unit has ActiveState=$active"
            ;;
        loaded) sudo systemctl stop "$ENGINE";;
        *) fail "$ENGINE: unexpected LoadState=$load during diagnostics";;
    esac
    load=$(prop "$SUPERVISOR" LoadState)
    case $load in
        not-found)
            active=$(prop "$SUPERVISOR" ActiveState)
            [ "$active" = inactive ] || fail "$SUPERVISOR: not-found unit has ActiveState=$active"
            ;;
        loaded) maintenance_stop;;
        *) fail "$SUPERVISOR: unexpected LoadState=$load during diagnostics";;
    esac
}

case ${1-} in
    --list) printf '%s\n' "${STEPS[@]}" ;;
    "$FINALLY") "step_$FINALLY" ;;
    *)
        for step in "${STEPS[@]}"; do
            if [ "$step" = "${1-}" ]; then
                "step_${step//-/_}"
                exit 0
            fi
        done
        fail "usage: drill-steps.sh --list | STEP (one of: ${STEPS[*]} $FINALLY)"
        ;;
esac
