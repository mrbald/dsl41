# drill-lib.sh -- helpers for .github/workflows/service-drill.yml (DL-218).
#
# Sourced by each step of the drill, on an ubuntu runner with systemd and
# sudo. The paths are the ones dsl41-launch and the units name; the drill
# installs everything there. Bash, not POSIX sh: the drill is not an
# example to copy, the launcher and the units are.
# shellcheck disable=SC2034  # the names below are read by the steps
set -euo pipefail

DSL41=/opt/dsl41/venv/bin/dsl41
ROOT=/srv/dsl41/runs/nightbank-01
SOCK=$ROOT/control.sock
MAP=/etc/dsl41/nightbank-access.toml
NIGHTBANK=/srv/dsl41/nightbank
ESTATE=$NIGHTBANK/estate/small
PROPERTIES=$NIGHTBANK/night/night.properties
ENGINE=dsl41-engine.service
SUPERVISOR=dsl41-supervisor.service
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
        "$GITHUB_WORKSPACE/examples/nightbank/deploy/nightbank-access.toml" "$MAP"
}
