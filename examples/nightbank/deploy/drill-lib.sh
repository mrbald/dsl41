# drill-lib.sh -- helpers for the service drill (DL-218, DL-266, DL-268).
#
# Sourced by drill-steps.sh, which holds the step bodies. The workflow
# .github/workflows/service-drill.yml and drill-local.sh both run those
# steps, on a host with systemd as PID 1, as an unprivileged user with
# passwordless sudo: every read of a path the service account or root owns
# goes through sudo (DL-271). The paths are the ones
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
# the operator recipes the drill runs as written (deployment-runbook ss0)
RUNBOOK=$REPO/docs/deployment-runbook.md

ok() { echo "ok: $*"; }
fail() {
    echo "FAIL: $*" >&2
    exit 1
}

# every client command runs as the service account, as an operator's would
as_dsl41() { (cd / && sudo -u dsl41 -H "$@"); }
cli() { as_dsl41 "$DSL41" "$@"; }

# The commands of the runbook block marked `<!-- recipe: NAME -->`, the
# `sh` fence on the line after the marker (DL-268). The drill runs the text
# an operator reads, so the two cannot drift; a missing block fails.
recipe() { # recipe NAME
    local body
    body=$(awk -v mark="<!-- recipe: $1 -->" '
        want == 2 && /^```$/ { exit }
        want == 2 { print; next }
        want == 1 { if ($0 == "```sh") { want = 2; next } exit }
        $0 == mark { want = 1 }' "$RUNBOOK")
    [ -n "$body" ] || fail "no recipe '$1' in $RUNBOOK"
    printf '%s\n' "$body"
}

# Run one recipe in bash, as root from the checkout (the service recipe
# copies files out of it), or as the service account from /, with the
# variables named. The block goes to stderr first, so the step log shows
# what ran. --no-errexit is for a block whose commands report a state in
# their exit codes (systemctl is-active); the caller reads its stdout.
run_recipe() { # run_recipe [--as-dsl41] [--no-errexit] NAME [VAR=VALUE...]
    local as=root errexit=-e name body
    while :; do
        case $1 in
            --as-dsl41) as=dsl41 ;;
            --no-errexit) errexit=+e ;;
            *) break ;;
        esac
        shift
    done
    name=$1
    shift
    body=$(recipe "$name")
    printf 'recipe %s:\n  | %s\n' "$name" "${body//$'\n'/$'\n  | '}" >&2
    if [ "$as" = root ]; then
        (cd "$REPO" && sudo env "$@" bash "$errexit" -u -o pipefail -c "$body")
    else
        as_dsl41 env "$@" bash "$errexit" -u -o pipefail -c "$body"
    fi
}

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

# Each unit's own `is-enabled` answer is STATE: `systemctl is-enabled A B`
# exits 0 when either one is enabled, so the units are asked one by one.
assert_enablement() { # assert_enablement enabled|disabled
    local unit state
    for unit in "$ENGINE" "$SUPERVISOR"; do
        state=$(systemctl is-enabled "$unit" 2>/dev/null || true)
        [ "$state" = "$1" ] || fail "$unit is $state, not $1"
    done
    ok "both units are $1"
}

# deployment-runbook ss2b's reboot hold, as the runbook prints it: taken
# before a window a reboot must not end, released at its end
take_hold() {
    run_recipe hold-down
    assert_enablement disabled
}
release_hold() {
    run_recipe hold-release
    assert_enablement enabled
}

# A boot's start of multi-user.target, in the drill's own container only:
# it starts every enabled unit the target wants, on the whole host, so the
# runbook never tells an operator to run it.
boot_target() {
    sudo systemctl start multi-user.target
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

# deployment-runbook ss2b, shape 1, steps 1 to 6 (the caller takes the
# reboot hold first): stop the engine unit,
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
stamp_request() { date +%s | sudo tee "$SCRATCH/last-request" >/dev/null; }
request() { # request VERB JOB
    cli sendevent "$1" -J "$2" -S "$SOCK"
    stamp_request
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
    local last=0 left
    # no file is no request yet; a file the step cannot read fails (DL-271)
    [ ! -e "$SCRATCH/last-request" ] || last=$(cat "$SCRATCH/last-request")
    left=$((last + 62 - $(date +%s)))
    if ((left > 0)); then
        echo "waiting ${left}s for the retry horizon after the last request"
        sleep "$left"
    fi
}

# The estate anchors in DIR, one per line, listed as root (DL-271). The
# steps run as an unprivileged user with sudo, as GitHub's runner does, and
# the run roots' directory is the service account's alone: a glob there
# matches nothing and stays literal. An empty list is a failure.
anchors_in() { # anchors_in DIR
    local list
    list=$(sudo find "$1" -mindepth 1 -maxdepth 1 -name '*.anchor' | sort)
    [ -n "$list" ] || fail "no *.anchor in $1"
    printf '%s\n' "$list"
}

# no command still alive under the supervisor of ROOT
nothing_live() { # nothing_live ROOT
    cli supervise list --run-root "$1" | jq -e '[.runs[] | select(.wrapper_alive)] | length == 0' >/dev/null
}

# `sendevent`, with the retry line it prints before its first write kept
# (control-protocol: --request-id, --expect, --epoch and --baseline). Sets
# RETRY_LINE to those arguments and ANSWER to the engine's JSON. The time of
# the request is kept like request().
send_keeping_retry() { # send_keeping_retry VERB JOB
    local err
    err=$(mktemp "$SCRATCH/send.XXXXXX")
    ANSWER=$(cli sendevent "$1" -J "$2" -S "$SOCK" 2>"$err") || {
        cat "$err" >&2
        fail "sendevent $1 -J $2 failed"
    }
    stamp_request
    RETRY_LINE=$(sed -n 's/^sending: //p' "$err")
    rm -f "$err"
    [ -n "$RETRY_LINE" ] || fail "sendevent printed no retry line"
}

job_field() { # job_field JOB FIELD -- one field of the job's status row
    cli query status --job "$1" -S "$SOCK" | jq -r --arg job "$1" --arg f "$2" '.jobs[$job][$f]'
}

# the engine's state, taken from its supervisor connection: the lease
# holder and its expiry, from the supervisor's LIST
lease_field() { # lease_field holder|expires_at
    cli supervise list --run-root "$ROOT" | jq -r --arg f "$1" '.lease[$f] // empty'
}
lease_is_held() { [ -n "$(lease_field holder)" ]; }
# the lease's expiry moved past WAS: one renewal (ISO times compare as text)
lease_renewed_past() { # lease_renewed_past WAS
    local now
    now=$(lease_field expires_at)
    [[ -n $now && $now > $1 ]]
}

# The engine unit's journal lines since EPOCH that match the fixed string
journal_count() { # journal_count EPOCH FRAGMENT
    engine_log_since "$1" | { grep -cF -- "$2" || true; }
}
journal_has() { # journal_has UNIT EPOCH FRAGMENT
    sudo journalctl -u "$1" --since "@$2" -o cat --no-pager | grep -qF -- "$3"
}

# A fault injected into the engine unit, the way tests/test_engine_machines.py
# injects one: a sitecustomize in a directory the unit's PYTHONPATH names
# replaces one machine's transition with a violating one. effect.01 is the
# outbox's record move (an engine-tier machine: the violation is one journal
# line); host.07 is a host's reinstatement (an oracle-tier machine: the
# violation is a trace line). No source file changes, and remove_faults
# takes the drop-in and the directory away. Restarts the engine, which
# resumes the same root.
FAULT_DIR=/opt/dsl41/drill-inject
FAULT_DROPIN=/etc/systemd/system/$ENGINE.d/drill-fault.conf
inject_faults() { # inject_faults effect.01 host.07 ...
    local names
    names=$(IFS=,; echo "$*")
    sudo install -d "$FAULT_DIR" "$(dirname "$FAULT_DROPIN")"
    sudo tee "$FAULT_DIR/sitecustomize.py" >/dev/null <<'PY'
import os

from dsl41 import runner_effects, runner_hosts
from dsl41.state_machine import StateMachine, Violation

faults = set(os.environ["DRILL_FAULTS"].split(","))
effect_real = runner_effects.EFFECT


class BrokenEffect:
    def __getattr__(self, name):
        return getattr(effect_real, name)

    def take(self, transition, old, new):
        if transition.id in faults:
            return Violation("effect", transition.id, old, new, "injected by the service drill")
        return effect_real.take(transition, old, new)


runner_effects.EFFECT = BrokenEffect()
host = runner_hosts.HOST
runner_hosts.HOST = StateMachine(
    name=host.name,
    states=host.states,
    initial=host.initial,
    finals=host.finals,
    transitions=tuple(t for t in host.transitions if t.id not in faults),
)
PY
    printf '[Service]\nEnvironment=PYTHONPATH=%s\nEnvironment=DRILL_FAULTS=%s\n' \
        "$FAULT_DIR" "$names" | sudo tee "$FAULT_DROPIN" >/dev/null
    sudo systemctl daemon-reload
    manual restart
    wait_up
}
remove_faults() {
    sudo rm -rf "$FAULT_DIR" "$(dirname "$FAULT_DROPIN")"
    sudo systemctl daemon-reload
}

# a client command run as another login, with the venv the launcher uses
as_user() { # as_user USER ARGS... -- dsl41 ARGS as USER
    local user=$1
    shift
    (cd / && sudo -u "$user" -H "$VENV/bin/dsl41" "$@")
}

# JOB's run number RUN or a later one ended in SUCCESS
ran_to_success() { # ran_to_success JOB RUN
    [ "$(job_field "$1" run_number)" -ge "$2" ] && job_is "$1" SUCCESS
}

# a unit's main process is not PID: systemd started a new one, and it is active
unit_replaced() { # unit_replaced UNIT PID
    local now
    now=$(prop "$1" MainPID)
    [ "$now" != 0 ] && [ "$now" != "$2" ] && is_state "$1" active
}
# the engine unit began a new invocation since INVOCATION and is active
engine_reinvoked() { # engine_reinvoked INVOCATION
    [ "$(prop "$ENGINE" InvocationID)" != "$1" ] && is_state "$ENGINE" active
}
# the engine unit has a new main process since PID, or gave up
engine_moved_on() { # engine_moved_on PID
    local now
    is_state "$ENGINE" failed && return 0
    now=$(prop "$ENGINE" MainPID)
    [ "$now" != 0 ] && [ "$now" != "$1" ]
}

# Kill the engine's main process again and again until the unit's start
# limit stops it (StartLimitBurst=5 in five minutes). Sets KILLS. At most ten
# kills; a unit that never gives up is a failure.
kill_engine_until_failed() {
    local pid
    KILLS=0
    sudo systemctl reset-failed "$ENGINE"
    while ((KILLS < 10)) && ! is_state "$ENGINE" failed; do
        pid=$(prop "$ENGINE" MainPID)
        if [ "$pid" != 0 ]; then
            sudo kill -KILL "$pid"
            KILLS=$((KILLS + 1))
        fi
        wait_for 30 "the unit restarted the engine, or gave up" engine_moved_on "$pid"
    done
    is_state "$ENGINE" failed || fail "$ENGINE was still restarted after $KILLS kills"
}

# the subscribe stream in FILE carries the adapter's STATUS input for JOB, with
# a nonzero exit code: the run's end, as the runbook's alarm row describes it
stream_shows_failure() { # stream_shows_failure FILE JOB
    jq -e -n --arg job "$2" '[inputs | select(.rec == "input" and .kind == "STATUS"
        and .source == "adapter" and .payload.job == $job and ((.payload.exit_code // 0) != 0))]
        | length >= 1' "$1" >/dev/null 2>&1
}

# put the units back the way the install left them: the engine's
# Requires= and no drop-ins, then reload. The caller restarts what it needs.
restore_units() {
    sudo sed -i 's/^Wants=dsl41-supervisor.service/Requires=dsl41-supervisor.service/' \
        "/etc/systemd/system/$ENGINE"
    sudo rm -rf "$FAULT_DIR" "/etc/systemd/system/$ENGINE.d" "/etc/systemd/system/$SUPERVISOR.d"
    sudo systemctl daemon-reload
}

# supervise list shows a run of JOB, and no run of it has a live wrapper
list_shows_finished_run() { # list_shows_finished_run JOB
    cli supervise list --run-root "$ROOT" | jq -e --arg job "$1" \
        '.ok == true and ([.runs[] | select(.job == $job)] | length >= 1)
         and ([.runs[] | select(.job == $job)] | all(.wrapper_alive == false))' >/dev/null
}
# more transition violation lines since EPOCH than BEFORE
violation_lines_exceed() { # violation_lines_exceed EPOCH BEFORE
    [ "$(journal_count "$1" 'dsl41: transition violation')" -gt "$2" ]
}
# the run root's perimeter journal holds a record of this kind
perimeter_has() { # perimeter_has REC
    sudo jq -e -s --arg rec "$1" 'any(.[]; .rec == $rec)' "$ROOT/perimeter.jsonl" >/dev/null
}
# records of kind REC in the perimeter journal whose text holds FRAGMENT
perimeter_count() { # perimeter_count REC FRAGMENT
    sudo jq -s --arg rec "$1" --arg frag "$2" \
        '[.[] | select(.rec == $rec and (tojson | contains($frag)))] | length' "$ROOT/perimeter.jsonl"
}
perimeter_count_exceeds() { # perimeter_count_exceeds REC FRAGMENT BEFORE
    [ "$(perimeter_count "$1" "$2")" -gt "$3" ]
}
job_not_running() { [ "$(job_status "$1")" != RUNNING ]; }
