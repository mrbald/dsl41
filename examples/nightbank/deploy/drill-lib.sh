# drill-lib.sh -- paths and helpers for the service drill (docs/deploy.md).
#
# Sourced by drill-steps.sh, which holds the rows. Each row runs on a host with systemd as
# PID 1, as `runner`, an unprivileged user with passwordless sudo, from a copy of the checkout.
# The paths are the ones dsl41-engine.service names; the install row puts everything there.
# Every client command runs as the service user, as docs/deploy.md tells an operator to, since
# the control socket is owner-only.
# shellcheck shell=bash
# shellcheck disable=SC2034  # the names below are read by the rows
set -euo pipefail

UNIT=dsl41-engine.service
VENV=/opt/dsl41/venv
DSL41=/opt/dsl41/bin/dsl41
NIGHTBANK=/opt/dsl41/nightbank
ESTATE=$NIGHTBANK/estate/small
MANIFEST=$ESTATE/estate.toml
ROOT=/srv/dsl41/runs/nightbank
SOCK=$ROOT/control.sock
# the night's data, logs and profile, the service user's own
NIGHT=/srv/dsl41/nightbank/night
# the drill's own jobs and their command, installed beside the estate
DRILL_JIL=$ESTATE/drill.jil
DRILL_JOB=/opt/dsl41/drill/bin/drill-job
# where the leak row's child writes the signal it got
LEAK_MARK=$NIGHT/leak-child.signal
# the leak row's drill-only drop-in; under /run, so a reboot drops it too
LEAK_DROP_IN=/run/systemd/system/$UNIT.d/90-drill-leak.conf
# what the drill keeps between rows; not /tmp, which a container restart empties
STATE=/var/lib/dsl41-drill
# the wheels and the locked requirements drill-local.sh delivered
DELIVERY=${DRILL_DELIVERY:-/delivery/dist}
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
# every top-level job the calendar starts, held at the first start, so only the drill's
# requests start anything while it runs
SCHEDULED=(APAC_EOD_B EMEA_EOD_B AMER_EOD_B OPS_HEARTBEAT_C OPS_MONTHLY_ATTRIB_C
    OPS_QTR_REG_REPORT_C)

ok() { echo "ok: $*"; }
fail() {
    echo "FAIL: $*" >&2
    exit 1
}

as_dsl41() { (cd / && sudo -u dsl41 -H "$@"); }
cli() { as_dsl41 "$DSL41" "$@"; }

prop() { systemctl show -p "$1" --value "$UNIT"; }
is_state() { [ "$(prop ActiveState)" = "$1" ]; }

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

# the engine's status document, or one job's row of it
status() { cli query status --socket "$SOCK" "$@"; }
job_field() { # job_field JOB JQ_PATH -- one field of the job's row, `null` when absent
    status --job "$1" | jq -c --arg job "$1" ".jobs[\$job]$2"
}
engine_is() { [ "$(status 2>/dev/null | jq -r .engine)" = "$1" ]; }
wait_running() { wait_for 120 "the engine answers RUNNING on $SOCK" engine_is RUNNING; }

job_is() { [ "$(job_field "$1" .status)" = "\"$2\"" ]; }
# JOB has an unsettled attempt numbered N or later in state STATE
attempt_is() { # attempt_is JOB N STATE
    status --job "$1" | jq -e --arg job "$1" --argjson n "$2" --arg state "$3" \
        '.jobs[$job].attempt | . != null and .n >= $n and .state == $state' >/dev/null
}
# JOB's attempt has settled: no attempt block is left on its row
attempt_settled() { [ "$(job_field "$1" .attempt)" = null ]; }

# the pids of the processes whose command line holds TEXT, one per line; none is empty
pids_of() { pgrep -f -- "$1" || true; }
gone() { [ -z "$(pids_of "$1")" ]; }
alive() { [ -n "$(pids_of "$1")" ]; }

request() { # request EVENT JOB
    cli sendevent "$1" --job "$2" --socket "$SOCK" >/dev/null
}

# the unit's journal since the Unix time EPOCH
journal_since() { sudo journalctl -u "$UNIT" --since "@$1" -o cat --no-pager; }
journal_has() { journal_since "$1" | grep -qF -- "$2"; }

# The unit allows five starts in 300 s (StartLimitBurst), and the drill's own starts count.
# Every deliberate start clears the counter first, as docs/deploy.md has an operator do after
# the limit is hit.
manual() { # manual start|restart
    sudo systemctl reset-failed "$UNIT" 2>/dev/null || true
    sudo systemctl "$1" "$UNIT"
}

# the engine's main process has changed from PID and the unit is active
replaced() { # replaced PID
    local now
    now=$(prop MainPID)
    [ "$now" != 0 ] && [ "$now" != "$1" ] && is_state active
}

# the unit is failed, or its main process has changed from PID
moved_on() { # moved_on PID
    local now
    is_state failed && return 0
    now=$(prop MainPID)
    [ "$now" != 0 ] && [ "$now" != "$1" ]
}

# The installed copy of the drill's jobs, with DRILL_LONG_C's command ending in VARIANT. The
# reload row changes the variant; the refusal row changes it without an update.
write_drill_jil() { # write_drill_jil VARIANT
    sudo tee "$DRILL_JIL" >/dev/null <<JIL
/* The service drill's jobs (examples/nightbank/deploy/drill-steps.sh); only the drill's
   FORCE_STARTJOB starts them. The gate on a global nobody sets keeps each one from lint's
   L011 (a dangling job), which reread refuses as a warning. */
insert_global: DRILL_GATE
value: CLOSED

insert_job: DRILL_LONG_C
job_type: c
command: $DRILL_JOB long $1
machine: bank_core
owner: dsl41
condition: v(DRILL_GATE) = OPEN

insert_job: DRILL_STUBBORN_C
job_type: c
command: $DRILL_JOB stubborn
machine: bank_core
owner: dsl41
condition: v(DRILL_GATE) = OPEN

insert_job: DRILL_LEAK_C
job_type: c
command: $DRILL_JOB leak $LEAK_MARK
machine: bank_core
owner: dsl41
condition: v(DRILL_GATE) = OPEN

insert_job: DRILL_LEAK_LIVE_C
job_type: c
command: $DRILL_JOB leak-and-run $LEAK_MARK
machine: bank_core
owner: dsl41
condition: v(DRILL_GATE) = OPEN
JIL
    sudo chmod 0644 "$DRILL_JIL"
}

# the installed dsl41's version, read from the venv's metadata
installed_version() {
    "$VENV/bin/python" -I -c 'from importlib.metadata import version; print(version("dsl41"))'
}
