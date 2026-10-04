#!/usr/bin/env bash
# drill-steps.sh -- the steps of the nightbank service drill (DL-218, DL-266).
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
# and restore of deployment-runbook ss2b, and the upgrade rows of ss7.
# The two access-map refusals prove the same systemd claim as the changed
# estate, and are pinned in tests/test_nightbank_deploy.py instead.
set -euo pipefail
# shellcheck source=drill-lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/drill-lib.sh"

STEPS=(install first-start restart detached refusal sealed quiesce restore
    upgrade-resume-safe upgrade-fresh-root upgrade-coordinated upgrade-old-release
    upgrade-state-machine)
# runs after the steps whatever they did; not part of --list
FINALLY=diagnostics

# this tree's wheel, built once, in two venvs: the resume-safe, fresh-root
# and coordinated rows flip between them. Two installs of one build exercise the
# rows' mechanics; they do not qualify a version pair.
BUILD_A=/opt/dsl41/venv-build-a
BUILD_B=/opt/dsl41/venv-build-b
# the old release of ss7's state-machine row: v1.7.0 from PyPI, which
# implements state-machine version 1
OLD_VERSION=1.7.0
OLD_VENV=/opt/dsl41/venv-$OLD_VERSION
# the roots the upgrade rows move the launcher to
ROLLED_ROOT=/srv/dsl41/runs/nightbank-01b
OLD_ROOT=/srv/dsl41/runs/nightbank-old
NEW_ROOT=/srv/dsl41/runs/nightbank-02
BACKUP=/srv/dsl41/backup
# a short box member: an 8-second fakework, unconditioned
SHORT_JOB=AMER_INV_MACROS_C

step_install() { # install dsl41, the nightbank estate, the launcher, the map and the units
    sudo apt-get update -q
    sudo apt-get install -y -q python3-venv jq
    sudo useradd --system --user-group --create-home --home-dir /var/lib/dsl41 \
        --shell /usr/sbin/nologin dsl41
    # the runtime closure exactly as uv.lock pins it, hash-checked, and then
    # the package alone: a drill failure cannot be dependency drift. The
    # package is a wheel uv builds once (its build backend bounded by
    # pyproject), not a pip build of the workspace.
    mkdir -p "$SCRATCH"
    (cd "$REPO" && uv export --frozen --no-dev --no-emit-project --format requirements-txt \
        >"$SCRATCH/runtime.txt")
    (cd "$REPO" && uv build --wheel --out-dir "$SCRATCH/dist")
    install_build "$BUILD_A"
    flip "$BUILD_A"
    sudo install -D -m 0755 "$REPO/examples/nightbank/deploy/dsl41-launch" "$LAUNCH"
    # the estate is root-owned and read-only to the service, as a checkout
    # of a tag would be; the night's data and the runs are its
    sudo install -d -m 0755 /srv/dsl41
    sudo cp -R "$REPO/examples/nightbank" "$NIGHTBANK"
    sudo chown -R root:root "$NIGHTBANK"
    sudo chmod -R go-w,a+rX "$NIGHTBANK"
    sudo install -d -o dsl41 -g dsl41 -m 0700 "$NIGHTBANK/night" "$(dirname "$ROOT")"
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
    "small",
    anchor_utc=datetime.now(UTC) + timedelta(hours=6),
    incidents=True,
)
PY
    # one scripted delay: the job the drill starts runs long enough to
    # outlive an engine stop and start
    echo "$LONG_JOB late 45" | as_dsl41 tee -a "$NIGHTBANK/night/incidents.conf"
    sudo install -d -m 0755 /etc/dsl41
    install_map
    sudo install -m 0644 "$REPO/examples/nightbank/deploy/dsl41-engine.service" \
        "$REPO/examples/nightbank/deploy/dsl41-supervisor.service" /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo -u dsl41 "$LAUNCH" --print
}

step_first_start() { # first start is a genesis, beside a supervisor in its own unit
    local mark line cgroup pid
    mark=$(date +%s)
    start_engine
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
    sudo cp "$ESTATE/apac.jil" /tmp/apac.jil.orig
    sudo sed -i 's/APAC_EXE_TRADES_C --sleep 12 /APAC_EXE_TRADES_C --sleep 13 /' "$ESTATE/apac.jil"
    ! sudo cmp -s /tmp/apac.jil.orig "$ESTATE/apac.jil" || fail "the edit did not apply"
    expect_refusal "catalog hash mismatch"
    sudo cp /tmp/apac.jil.orig "$ESTATE/apac.jil"
    start_engine
}

step_sealed() { # a sealed engine stays stopped until the next period is opened
    local next mark
    mapfile -t next < <(next_args)
    mark=$(date +%s)
    # the drill's own FORCE_STARTJOB is inside the retry horizon, hence
    # --force-seal; every --next-* restates the launcher's options
    cli seal --run-root "$ROOT" "${next[@]}" -p "$PROPERTIES" \
        --next-detached --next-as-machine localhost --next-machine-policy strict \
        --next-timezone UTC --force-seal --claimed-actor drill@nightbank
    assert_stays_stopped 3 "sealed period 1" "$mark"
    mark=$(date +%s)
    start_engine
    launch_line_since "$mark" | grep -q -- ' --resume$' || fail "the opener was not a resume"
    sudo test -e "$ROOT/wal/000002.jsonl" || fail "period 2 did not open in $ROOT"
    ok "period 2 opened in place"
}

step_quiesce() { # managed quiescence: both units stopped and staying stopped (runbook ss2b)
    quiesce_shape1
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
}

# ss7 row 1, a patch marked resume-safe: stop the engine unit, flip, start
# it again; the supervisor unit and its detached job are kept. Rollback is
# the same with the old venv.
step_upgrade_resume_safe() { # resume-safe row: engine restarted on the flipped venv, supervisor kept
    local sup_pid pids pid mark
    echo "two installs of one build: this exercises the row, it qualifies no version pair"
    install_build "$BUILD_B"
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
# the units' cgroups.
step_upgrade_fresh_root() { # silent-note row: the next period opens in a fresh root on the flipped venv
    local mark line
    echo "two installs of one build: this exercises the row, it qualifies no version pair"
    wait_for 60 "nothing is live under the supervisor" nothing_live "$ROOT"
    quiesce_shape1
    flip "$BUILD_B"
    point_launcher "$ROLLED_ROOT" "$ANCHOR"
    as_dsl41 touch "$ROLLED_ROOT.open-from"
    line=$(sudo -u dsl41 "$LAUNCH" --print)
    [[ $line == *" --open-from $ANCHOR" ]] || fail "the trigger did not make an opener: $line"
    ROOT=$ROLLED_ROOT
    SOCK=$ROOT/control.sock
    mark=$(date +%s)
    start_engine
    is_state "$ENGINE" active || fail "$ENGINE: $(prop "$ENGINE" ActiveState)"
    launch_line_since "$mark" | grep -qF -- " --open-from $ANCHOR" || fail "the unit did not open $ROOT"
    engine_log_since "$mark" | grep -qF "opened period 4 in $ROOT" || fail "no roll into $ROOT"
    ! sudo test -e "$ROOT.open-from" || fail "the open trigger is still there"
    sudo test -e "$ROOT/wal/000004.jsonl" || fail "period 4 did not open in $ROOT"
    on_build "$ENGINE" "$BUILD_B"
    on_build "$SUPERVISOR" "$BUILD_B"
    ok "the engine unit opened period 4 in $ROOT; both units run from $BUILD_B"
    # one shot: the next start resumes the opened root
    mark=$(date +%s)
    manual restart
    wait_up
    launch_line_since "$mark" | grep -q -- ' --resume$' || fail "the restart did not resume $ROOT"
    ok "the unit's next start resumed $ROOT"
}

# ss7 row 3, resume-safe with a wrapper or supervisor protocol change:
# drain, stop both units, flip, start both; the engine resumes the same
# root and the supervisor is replaced by the new venv's.
step_upgrade_coordinated() { # coordinated row: both units replaced, the engine resumes the same root
    local mark sup_pid held
    echo "two installs of one build: this exercises the row, it qualifies no version pair"
    ROOT=$ROLLED_ROOT
    SOCK=$ROOT/control.sock
    wait_for 60 "nothing is live under the supervisor" nothing_live "$ROOT"
    sup_pid=$(prop "$SUPERVISOR" MainPID)
    sudo systemctl stop "$ENGINE" "$SUPERVISOR"
    flip "$BUILD_A"
    mark=$(date +%s)
    start_engine
    is_state "$SUPERVISOR" active || fail "Requires= did not start $SUPERVISOR"
    launch_line_since "$mark" | grep -q -- ' --resume$' || fail "the engine did not resume $ROOT"
    [ "$(prop "$SUPERVISOR" MainPID)" != "$sup_pid" ] || fail "the supervisor was not replaced"
    on_build "$ENGINE" "$BUILD_A"
    on_build "$SUPERVISOR" "$BUILD_A"
    held=$(cli query status --job APAC_EOD_B -S "$SOCK" | jq -r '.jobs.APAC_EOD_B.on_hold')
    [ "$held" = true ] || fail "the hold did not survive the upgrade"
    ok "both units run from $BUILD_A; the engine resumed $ROOT with its holds"
}

# The old estate for ss7's state-machine row: v1.7.0 from PyPI, as runbook
# ss1 installs a release that has no assets, on a new estate of its own.
step_upgrade_old_release() { # an estate on the old release, v1.7.0 from PyPI
    local mark line version
    ROOT=$ROLLED_ROOT
    SOCK=$ROOT/control.sock
    wait_for 60 "nothing is live under the supervisor" nothing_live "$ROOT"
    sudo systemctl stop "$ENGINE" "$SUPERVISOR"
    sudo python3 -m venv "$OLD_VENV"
    sudo "$OLD_VENV/bin/pip" install --quiet "dsl41==$OLD_VERSION"
    sudo "$OLD_VENV/bin/pip" check
    version=$(state_machine_version "$OLD_VENV")
    [ "$version" = 1 ] || fail "$OLD_VENV implements state-machine version $version"
    flip "$OLD_VENV"
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
    local mark line version refused
    ROOT=$OLD_ROOT
    ANCHOR=$OLD_ROOT.anchor
    SOCK=$ROOT/control.sock
    wait_for 60 "nothing is live under the supervisor" nothing_live "$ROOT"
    quiesce_shape1
    flip "$BUILD_A"
    version=$(state_machine_version "$BUILD_A")
    [ "$version" != 1 ] || fail "$BUILD_A implements state-machine version 1 too"
    point_launcher "$NEW_ROOT" "$NEW_ROOT.anchor"
    ROOT=$NEW_ROOT
    SOCK=$ROOT/control.sock
    mark=$(date +%s)
    start_engine
    is_state "$SUPERVISOR" active || fail "Requires= did not start $SUPERVISOR"
    line=$(launch_line_since "$mark")
    [[ $line != *--resume* ]] || fail "the new estate's first start was a resume"
    ok "this build (state-machine version $version) leads the new estate $ROOT"
    # the retained old estate: the kept old venv audits it, the new build
    # refuses it and names the version
    as_dsl41 "$OLD_VENV/bin/dsl41" audit --estate-anchor "$OLD_ROOT.anchor"
    ok "the kept v$OLD_VERSION venv audits the retained estate"
    if refused=$(cli audit --estate-anchor "$OLD_ROOT.anchor" 2>&1); then
        fail "this build audited a state-machine version 1 period: $refused"
    fi
    echo "$refused"
    grep -qi 'state.machine' <<<"$refused" || fail "the refusal does not name the version"
    ok "this build refuses the old estate and names the version"
}

step_diagnostics() { # diagnostics and teardown
    sudo systemctl status "$ENGINE" "$SUPERVISOR" --no-pager || true
    sudo journalctl -u "$ENGINE" -u "$SUPERVISOR" -o short-precise --no-pager || true
    sudo sh -c 'tail -n 100 /srv/dsl41/runs/*/supervisor.log' || true
    sudo systemctl stop "$ENGINE" "$SUPERVISOR" || true
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
