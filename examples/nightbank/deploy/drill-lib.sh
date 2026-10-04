# drill-lib.sh -- helpers for the service drill (DL-218).
#
# Sourced by drill-steps.sh, which holds the step bodies. The workflow
# .github/workflows/service-drill.yml and drill-local.sh both run those
# steps, on a host with systemd as PID 1 and sudo. The paths are the ones
# dsl41-launch and the units name; the drill installs everything there.
# Bash, not POSIX sh: the drill is not an example to copy, the launcher and
# the units are.
# shellcheck shell=bash
# shellcheck disable=SC2034  # the names below are read by the steps
set -euo pipefail

DSL41=/opt/dsl41/venv/bin/dsl41
ROOT=/srv/dsl41/runs/nightbank-01
ANCHOR=/srv/dsl41/runs/nightbank-01.anchor
SOCK=$ROOT/control.sock
LAUNCH=/opt/dsl41/bin/dsl41-launch
# the launcher's DSL41 runs through this symlink; each install is a venv
# beside it, and an upgrade flips the link (deployment-runbook ss7)
VENV=/opt/dsl41/venv
MAP=/etc/dsl41/nightbank-access.toml
NIGHTBANK=/srv/dsl41/nightbank
ESTATE=$NIGHTBANK/estate/small
PROPERTIES=$NIGHTBANK/night/night.properties
ENGINE=dsl41-engine.service
SUPERVISOR=dsl41-supervisor.service
# the checkout the drill runs from, and a scratch directory for the wheel
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
SCRATCH=${RUNNER_TEMP:-/tmp}/dsl41-drill
# the order dsl41-launch names the files in
FILES=(amer apac calendars emea global infra)
# RUNBOOK exercise 15 step 1: every scheduled top-level job or box with a
# future tick, held so nothing the calendar starts runs under the drill
QUIESCE=(APAC_EOD_B EMEA_EOD_B AMER_EOD_B OPS_HEARTBEAT_C OPS_MONTHLY_ATTRIB_C OPS_QTR_REG_REPORT_C)
# the job the drill starts by hand; the night's incidents make it run long
LONG_JOB=OPS_XINST_DEMO_C

ok() { echo "ok: $*"; }
fail() {
    echo "FAIL: $*" >&2
    exit 1
}

# every client command runs as the service account, as an operator's would
as_dsl41() { (cd / && sudo -u dsl41 -H "$@"); }
cli() { as_dsl41 "$DSL41" "$@"; }

prop() { systemctl show -p "$2" --value "$1"; }
is_state() { [ "$(prop "$1" ActiveState)" = "$2" ]; }

wait_for() { # wait_for SECONDS DESCRIPTION COMMAND...
    local limit=$1 what=$2
    shift 2
    local deadline=$((SECONDS + limit))
    until "$@"; do
        ((SECONDS < deadline)) || fail "$what: not within ${limit}s"
        sleep 1
    done
    ok "$what"
}

answers() { cli query status --brief -S "$SOCK" >/dev/null 2>&1; }
wait_up() { wait_for 90 "the engine answers on $SOCK" answers; }

job_status() {
    cli query status --job "$1" -S "$SOCK" | jq -r --arg job "$1" '.jobs[$job].status'
}
job_is() { [ "$(job_status "$1")" = "$2" ]; }

engine_log_since() { # engine_log_since EPOCH -- the unit's journal since then
    sudo journalctl -u "$ENGINE" --since "@$1" -o cat --no-pager
}

# the command line the launcher ran for the engine's latest start since
# EPOCH. journald can lag the process, so it is looked for for up to ten
# seconds; no such line is a failure, never an empty match
launch_line_since() {
    local line tries
    for tries in 1 2 3 4 5 6 7 8 9 10; do
        line=$(engine_log_since "$1" | { grep '^dsl41-launch: engine: ' || true; } | tail -n 1)
        [ -z "$line" ] || break
        sleep 1
    done
    [ -n "$line" ] || fail "no dsl41-launch line in the $ENGINE journal after $tries tries"
    printf '%s\n' "$line"
}

# The engine unit allows five starts in five minutes, and the drill's own
# starts count against that. Every deliberate start clears the counter
# first, as an operator's `systemctl reset-failed` would. A unit systemd has
# not loaded yet has no counter, and reset-failed refuses it ("not loaded"):
# that refusal is not a failure of the drill.
manual() { # manual start|restart
    sudo systemctl reset-failed "$ENGINE" 2>/dev/null || true
    sudo systemctl "$1" "$ENGINE"
}

# start, and wait until the engine answers. After a refusal, this clears
# the failed state and starts on the repaired setup.
start_engine() {
    manual start
    wait_up
}

# The engine exited CODE and systemd leaves it there: the unit stays
# failed, its invocation and restart count do not move for four RestartSec
# periods, and the journal since EPOCH names FRAGMENT.
assert_stays_stopped() { # assert_stays_stopped CODE FRAGMENT EPOCH
    local code=$1 fragment=$2 mark=$3 invocation restarts
    wait_for 90 "$ENGINE is failed" is_state "$ENGINE" failed
    invocation=$(prop "$ENGINE" InvocationID)
    restarts=$(prop "$ENGINE" NRestarts)
    sleep 20
    is_state "$ENGINE" failed || fail "$ENGINE left failed: $(prop "$ENGINE" ActiveState)"
    [ "$(prop "$ENGINE" InvocationID)" = "$invocation" ] || fail "$ENGINE was started again"
    [ "$(prop "$ENGINE" NRestarts)" = "$restarts" ] || fail "$ENGINE was restarted"
    [ "$(prop "$ENGINE" ExecMainStatus)" = "$code" ] ||
        fail "exit $(prop "$ENGINE" ExecMainStatus), expected $code"
    engine_log_since "$mark" | grep -F -- "$fragment" >/dev/null ||
        fail "the journal does not say '$fragment'"
    ok "exit $code, not restarted, the journal says '$fragment'"
}

# A start that the configuration refuses: exit 2, and no restart loop.
expect_refusal() { # expect_refusal FRAGMENT
    local mark
    mark=$(date +%s)
    manual start || true
    assert_stays_stopped 2 "$1" "$mark"
    launch_line_since "$mark" | grep -q -- ' --resume$' || fail "the refused start was not a resume"
}

install_map() {
    sudo install -o dsl41 -g dsl41 -m 0600 \
        "$REPO/examples/nightbank/deploy/nightbank-access.toml" "$MAP"
}

# the seal's --next list: the launcher's estate files, in its order
next_args() {
    local file
    for file in "${FILES[@]}"; do
        printf -- '--next\n%s\n' "$ESTATE/$file.jil"
    done
}

# install the wheel the install step built, with the locked closure, into
# a venv of its own beside the symlink (deployment-runbook ss1, ss7)
install_build() { # install_build VENV_DIR
    sudo python3 -m venv "$1"
    sudo "$1/bin/pip" install --quiet --require-hashes -r "$SCRATCH/runtime.txt"
    sudo "$1/bin/pip" install --quiet --no-deps "$SCRATCH"/dist/dsl41-*.whl
    sudo "$1/bin/pip" check
    "$1/bin/dsl41" --help >/dev/null || fail "$1: dsl41 --help failed"
}

# point the launcher's symlink at VENV_DIR: the upgrade's one switch
flip() { # flip VENV_DIR
    sudo ln -sfn "$1" "$VENV"
    [ "$(readlink "$VENV")" = "$1" ] || fail "$VENV does not lead to $1"
}

# UNIT's main process runs from VENV_DIR: its interpreter is that venv's
on_build() { # on_build UNIT VENV_DIR
    sudo grep -qF "$2/" "/proc/$(prop "$1" MainPID)/cmdline" || fail "$1 does not run from $2"
}

# the state-machine version a venv implements
state_machine_version() { # state_machine_version VENV_DIR
    "$1/bin/python" -c 'from dsl41.runner_ledger import STATE_MACHINE_VERSION; print(STATE_MACHINE_VERSION)'
}

# An operator's edit of the launcher's CONFIGURATION block and of the
# units' RequiresMountsFor=, the way deployment-runbook ss7 describes it.
# Only the units' copy of the run root moves; nothing else is touched.
point_launcher() { # point_launcher RUN_ROOT ESTATE_ANCHOR
    sudo sed -i -e "s|^RUN_ROOT=.*|RUN_ROOT=$1|" -e "s|^ESTATE_ANCHOR=.*|ESTATE_ANCHOR=$2|" "$LAUNCH"
    sudo sed -i "s|^RequiresMountsFor=.*|RequiresMountsFor=$1|" \
        "/etc/systemd/system/$ENGINE" "/etc/systemd/system/$SUPERVISOR"
    sudo systemctl daemon-reload
    # read back from the file: --print refuses a fresh root beside a lineage
    # until the open trigger exists
    { grep -qxF "RUN_ROOT=$1" "$LAUNCH" && grep -qxF "ESTATE_ANCHOR=$2" "$LAUNCH"; } ||
        fail "the launcher does not name $1 and $2"
}

# Both units stopped, and still stopped once the longest RestartSec of the
# two has passed twice over: a Restart=always supervisor would be back by
# then. A unit that ended failed (a sealed engine exits 3) counts as
# stopped; active and activating (the restart wait) do not.
assert_units_stay_stopped() {
    local unit state wait=0 restart
    declare -A invocation
    for unit in "$ENGINE" "$SUPERVISOR"; do
        restart=$(prop "$unit" RestartUSec)
        [[ $restart =~ ^[0-9]+s$ ]] || fail "$unit: RestartSec $restart is not whole seconds"
        restart=${restart%s}
        ((restart > wait)) && wait=$restart
        invocation[$unit]=$(prop "$unit" InvocationID)
    done
    sleep $((2 * wait + 2))
    for unit in "$ENGINE" "$SUPERVISOR"; do
        state=$(prop "$unit" ActiveState)
        [[ $state == inactive || $state == failed ]] || fail "$unit is $state"
        [ "$(prop "$unit" InvocationID)" = "${invocation[$unit]}" ] || fail "$unit was started again"
    done
    ok "both units stayed stopped for $((2 * wait + 2))s, past RestartSec=${wait}s"
}

# deployment-runbook ss2b's no-writers check, on ROOT: no supervisor files,
# no process holding leader.lock, and no process of the service account
assert_no_writers() { # assert_no_writers ROOT
    ! sudo test -e "$1/supervisor.pid" || fail "$1/supervisor.pid is still there"
    ! sudo test -e "$1/supervisor.sock" || fail "$1/supervisor.sock is still there"
    sudo flock --nonblock "$1/leader.lock" true || fail "a process holds $1/leader.lock"
    ! pgrep -u dsl41 >/dev/null || fail "dsl41 processes remain: $(pgrep -a -u dsl41)"
    ok "no writer is left in $1"
}

# deployment-runbook ss2b, shape 1, steps 1 to 6: stop the engine unit,
# seal offline and audit while the supervisor unit runs, stop the
# supervisor unit, prove both stay stopped and nothing writes
quiesce_shape1() {
    local next
    mapfile -t next < <(next_args)
    sudo systemctl stop "$ENGINE"
    is_state "$ENGINE" inactive || fail "$ENGINE: $(prop "$ENGINE" ActiveState)"
    cli supervise list --run-root "$ROOT" >/dev/null || fail "the supervisor did not outlive $ENGINE"
    ok "the supervisor outlived $ENGINE"
    wait_out_retry_horizon
    cli seal --run-root "$ROOT" --estate-anchor "$ANCHOR" "${next[@]}" -p "$PROPERTIES" \
        --next-detached --next-as-machine localhost --next-machine-policy strict \
        --next-timezone UTC --claimed-actor drill@nightbank
    cli audit --run-root "$ROOT" --estate-anchor "$ANCHOR"
    ok "sealed offline and audited while the supervisor ran"
    sudo systemctl stop "$SUPERVISOR"
    assert_units_stay_stopped
    assert_no_writers "$ROOT"
}

# An operator's request, with its time kept: a seal refuses inside
# the closing period's retry horizon (60 s after the last request) unless
# it is forced (period-model ss9), and an upgrade waits the horizon out.
request() { # request VERB JOB
    cli sendevent "$1" -J "$2" -S "$SOCK"
    date +%s | sudo tee "$SCRATCH/last-request" >/dev/null
}
force_start() { request FORCE_STARTJOB "$1"; }
# RUNBOOK exercise 15 step 1: hold what the calendar would start
hold_scheduled() {
    local job
    for job in "${QUIESCE[@]}"; do
        request ON_HOLD "$job"
    done
}
wait_out_retry_horizon() {
    local last left
    last=$(cat "$SCRATCH/last-request" 2>/dev/null || echo 0)
    left=$((last + 62 - $(date +%s)))
    if ((left > 0)); then
        echo "waiting ${left}s for the retry horizon after the last request"
        sleep "$left"
    fi
}

# no command still alive under the supervisor of ROOT
nothing_live() { # nothing_live ROOT
    cli supervise list --run-root "$1" | jq -e '[.runs[] | select(.wrapper_alive)] | length == 0' >/dev/null
}
