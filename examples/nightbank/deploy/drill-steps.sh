#!/usr/bin/env bash
# drill-steps.sh -- the rows of the nightbank service drill (docs/deploy.md).
#
#   drill-steps.sh --list    print the rows, in the order they run
#   drill-steps.sh ROW       run one row; exit 0 only if it passed
#
# drill-local.sh runs each row in a podman container with systemd as PID 1, as `runner`. Each
# row is its own process and sources drill-lib.sh. The rows check what an operator reads:
# `systemctl`, `journalctl -u`, `dsl41 query status` and `dsl41 runs`. A wait polls with a
# deadline; the only fixed waits are the ones that prove systemd did not start the unit again.
#
# The reboot row has two halves, reboot-before and reboot-after; drill-local.sh restarts the
# container between them. diagnostics is not a row: drill-local.sh runs it after a failure.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=drill-lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/drill-lib.sh"

ROWS=(install start restart reboot kill-engine drain reload leak refusal start-limit upgrade)

step_install() { # the wheel, the nightbank estate, the drill's jobs, the unit and its user
    local wheel out version
    sudo install -d -o "$(id -un)" -g "$(id -gn)" "$STATE"
    # the unit's User= and Group=; the drill's own user is `runner`
    getent group dsl41 >/dev/null || sudo groupadd --system dsl41
    id dsl41 >/dev/null 2>&1 || sudo useradd --system --gid dsl41 --create-home \
        --home-dir /var/lib/dsl41 --shell /usr/sbin/nologin dsl41
    # Engine releases occupy separate versioned directories (DL-437).
    wheel=$(find "$DELIVERY" -maxdepth 1 -name 'dsl41-*.whl' ! -name '*.post*' | head -n 1)
    [ -n "$wheel" ] || fail "no dsl41 wheel in $DELIVERY"
    version=$(basename "$wheel")
    version=${version#dsl41-}
    version=${version%%-*}
    VENV=/opt/dsl41/releases/$version
    DSL41=$VENV/bin/dsl41
    sudo python3.12 -m venv "$VENV"
    sudo "$VENV/bin/pip" install --quiet --disable-pip-version-check \
        -r "$DELIVERY/requirements.txt"
    sudo "$VENV/bin/pip" install --quiet --disable-pip-version-check --no-deps "$wheel"
    installed_version | tee "$STATE/version-installed"
    # the estate is root's and read-only to the service, as a release's would be
    sudo install -d -m 0755 "$NIGHTBANK"
    sudo cp -R "$REPO/examples/nightbank/estate/small" "$ESTATE"
    sudo cp -R "$REPO/examples/nightbank/bin" "$NIGHTBANK/bin"
    sudo install -d -m 0755 "$(dirname "$DRILL_JOB")"
    sudo tee "$DRILL_JOB" >/dev/null <<'SH'
#!/bin/sh
# The service drill's one command. long: sleep until stopped. stubborn: ignore TERM, so a
# stop takes its KILL after the grace. leak: leave a child behind in its own session, which
# keeps the attempt's DSL41_COOKIE, and exit 0. leak-and-run: leave the child and keep
# running. The child appends "PID TERM" to the file named by its argument when TERMed. The
# shell never execs its sleep, so its command line names the mode on every sh.
set -eu
leave_child() {
    setsid sh -c 'trap "echo \$\$ TERM >>\"\$1\"; exit 0" TERM; while :; do sleep 1; done' \
        drill-leak-child "$1" </dev/null >/dev/null 2>&1 &
}
case $1 in
    long) sleep 600 ;;
    stubborn) trap '' TERM; sleep 600 ;;
    leak) leave_child "$2"; exit 0 ;;
    leak-and-run) leave_child "$2"; sleep 600 ;;
    *) echo "drill-job: unknown mode $1" >&2; exit 2 ;;
esac
SH
    sudo chmod 0755 "$DRILL_JOB"
    write_drill_jil v1
    # The host config owns the application input glob and properties (DL-437).
    sudo install -d -m 0755 /etc/dsl41
    sudo install -m 0644 "$REPO/examples/nightbank/deploy/dsl41.toml" "$CONFIG"
    # The region anchors are six hours out, so no calendar start falls inside the drill.
    "$VENV/bin/python" -I - "$NIGHT" <<'PY' | sudo tee "$ESTATE/night.properties" >/dev/null
import sys
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

night = sys.argv[1]
anchor = (datetime.now(UTC) + timedelta(hours=6)).replace(second=0, microsecond=0)
props = {
    "OWNER": "dsl41",
    "NB_DATA": f"{night}/data",
    "NB_LOGS": f"{night}/logs",
    "NB_PROFILE": f"{night}/profile.env",
}
for i, (region, tz) in enumerate(
    [("APAC", "Asia/Tokyo"), ("EMEA", "Europe/Zurich"), ("AMER", "America/New_York")]
):
    local = (anchor + timedelta(minutes=3 * i)).astimezone(ZoneInfo(tz))
    props[f"EOD_{region}"] = local.strftime("%H:%M")
end = min(anchor + timedelta(minutes=90), anchor.replace(hour=23, minute=59))
props["HB_WINDOW"] = f"{anchor:%H:%M}-{end:%H:%M}"
print("".join(f"{k}={v}\n" for k, v in props.items()), end="")
PY
    sudo chmod 0644 "$ESTATE/night.properties"
    # the night's data, logs and profile, the service user's own
    sudo install -d -m 0755 /srv/dsl41
    sudo install -d -o dsl41 -g dsl41 -m 0700 /srv/dsl41/nightbank "$NIGHT"
    as_dsl41 mkdir -p "$NIGHT/data/incoming" "$NIGHT/data/pending" "$NIGHT/data/current" \
        "$NIGHT/data/state" "$NIGHT/logs"
    as_dsl41 cp "$ESTATE/incidents.conf" "$NIGHT/incidents.conf"
    as_dsl41 tee "$NIGHT/profile.env" >/dev/null <<ENV
export NIGHTBANK_DATA="$NIGHT/data"
export NIGHTBANK_STATE="$NIGHT/data/state"
export NIGHTBANK_INCIDENTS="$NIGHT/incidents.conf"
export NIGHTBANK_SOCKET="$SOCK"
export PATH="$NIGHTBANK/bin:$VENV/bin:\$PATH"
ENV
    # docs/deploy.md: the run root is the service user's; the engine creates what is inside
    sudo install -d -m 0755 /srv/dsl41/runs
    sudo install -d -o dsl41 -g dsl41 -m 0700 "$ROOT"
    # The main config's declared lint policy determines admission (DL-437).
    out=$(as_dsl41 "$DSL41" lint --config "$CONFIG") || fail "lint refused the estate: $out"
    [ -z "$out" ] || fail "the installed estate does not lint clean: $out"
    sed "s/<version>/$version/g" "$REPO/examples/nightbank/deploy/dsl41-engine.service" | \
        sudo tee "/etc/systemd/system/$UNIT" >/dev/null
    sudo systemd-analyze verify "/etc/systemd/system/$UNIT"
    sudo systemctl daemon-reload
    sudo systemctl enable "$UNIT"
    [ "$(systemctl is-enabled "$UNIT")" = enabled ] || fail "$UNIT is not enabled"
    ok "dsl41 $(cat "$STATE/version-installed"), the estate and $UNIT are installed"
}

step_start() { # the first start creates the store; the scheduled jobs are held
    local mark job
    mark=$(date +%s)
    manual start
    wait_running
    sudo test -f "$ROOT/engine.db" || fail "no $ROOT/engine.db after the first start"
    wait_for 10 "the journal says 'engine up'" journal_has "$mark" "engine up; control socket: $SOCK"
    for job in "${SCHEDULED[@]}"; do
        request ON_HOLD "$job"
    done
    [ "$(job_field APAC_EOD_B .on_hold)" = true ] || fail "APAC_EOD_B is not on hold"
    ok "the engine is up on $ROOT; the calendar's jobs are held"
}

step_restart() { # systemctl restart drains and starts on the same root
    local invocation mark
    invocation=$(prop InvocationID)
    mark=$(date +%s)
    manual restart
    wait_running
    [ "$(prop InvocationID)" != "$invocation" ] || fail "no new invocation"
    wait_for 10 "the stop was a clean drain" journal_has "$mark" "engine stopped: clean_shutdown"
    [ "$(job_field APAC_EOD_B .on_hold)" = true ] || fail "the hold did not survive the restart"
    ok "the restart resumed $ROOT: the operator's holds are still there"
}

step_reboot_before() { # before the host restarts: the unit is enabled and running
    [ "$(systemctl is-enabled "$UNIT")" = enabled ] || fail "$UNIT is not enabled"
    is_state active || fail "$UNIT is $(prop ActiveState)"
    prop InvocationID >"$STATE/invocation-before-reboot"
    ok "$UNIT is enabled and active before the reboot"
}

step_reboot_after() { # after the host restarts: the enabled unit resumed the same root
    wait_for 60 "$UNIT started at boot" is_state active
    wait_running
    [ "$(prop InvocationID)" != "$(cat "$STATE/invocation-before-reboot")" ] ||
        fail "the unit kept its invocation across the reboot"
    [ "$(job_field APAC_EOD_B .on_hold)" = true ] || fail "the hold did not survive the reboot"
    ok "the boot started $UNIT, which resumed $ROOT"
}

step_kill_engine() { # SIGKILL of the engine: the unit restarts it and the run goes on
    local main restarts mark run old after
    request FORCE_STARTJOB DRILL_LONG_C
    wait_for 60 "DRILL_LONG_C is running" attempt_is DRILL_LONG_C 1 RUNNING
    run=$(job_field DRILL_LONG_C .run_number)
    old=$(pids_of "drill-job long")
    [ -n "$old" ] || fail "no DRILL_LONG_C process"
    main=$(prop MainPID)
    restarts=$(prop NRestarts)
    mark=$(date +%s)
    sudo kill -KILL "$main"
    wait_for 60 "systemd restarted the engine" replaced "$main"
    wait_running
    [ "$(prop NRestarts)" = $((restarts + 1)) ] || fail "NRestarts did not rise by one"
    wait_for 10 "the journal says status=9/KILL" journal_has "$mark" "status=9/KILL"
    # DL-433: the engine's death ended the attempt; the interruption rule reruns the job as a
    # new attempt of the same run, after the cause the restart read
    wait_for 60 "DRILL_LONG_C runs a second attempt" attempt_is DRILL_LONG_C 2 RUNNING
    [ "$(job_field DRILL_LONG_C .run_number)" = "$run" ] || fail "the rerun opened another run"
    after=$(job_field DRILL_LONG_C .attempt.after)
    case $after in
        '"engine_death"' | '"lifeline"') ;;
        *) fail "the second attempt follows $after, not the engine's death" ;;
    esac
    for pid in $old; do
        ! sudo kill -0 "$pid" 2>/dev/null || fail "the old command $pid outlived the engine"
    done
    ok "after a SIGKILL the unit resumed $ROOT; attempt 1 ended ($after), attempt 2 runs"
    request KILLJOB DRILL_LONG_C
    wait_for 60 "DRILL_LONG_C settled after KILLJOB" attempt_settled DRILL_LONG_C
}

step_drain() { # systemctl stop with a live job: TERM, the grace, KILL, a clean shutdown
    local mark t0 took after
    request FORCE_STARTJOB DRILL_STUBBORN_C
    wait_for 60 "DRILL_STUBBORN_C is running" attempt_is DRILL_STUBBORN_C 1 RUNNING
    mark=$(date +%s)
    t0=$SECONDS
    sudo systemctl stop "$UNIT"
    took=$((SECONDS - t0))
    is_state inactive || fail "$UNIT is $(prop ActiveState) after the stop"
    # the job ignores TERM, so its stop takes grace_term_s (10 s) before the KILL
    ((took >= 10)) || fail "the stop took ${took}s, less than the TERM grace"
    # systemd 255 prints `status=0`, systemd 239 `status=0/SUCCESS`
    prop ExecStop | grep -qE 'code=exited ; status=0(/SUCCESS)? ' ||
        fail "ExecStop did not exit 0: $(prop ExecStop)"
    wait_for 10 "the drain was clean" journal_has "$mark" "engine stopped: clean_shutdown"
    gone "drill-job stubborn" || fail "the stubborn command outlived the drain"
    ok "the drain took ${took}s, ExecStop exited 0 and the store says clean_shutdown"
    # the next start reads how the attempt ended: a rerun after engine_stop
    manual start
    wait_running
    wait_for 60 "DRILL_STUBBORN_C reran" attempt_is DRILL_STUBBORN_C 2 RUNNING
    after=$(job_field DRILL_STUBBORN_C .attempt.after)
    [ "$after" = '"engine_stop"' ] || fail "the rerun follows $after, not engine_stop"
    ok "the drained attempt is recorded as stopped by engine_stop"
    request KILLJOB DRILL_STUBBORN_C
    wait_for 60 "DRILL_STUBBORN_C settled after KILLJOB" attempt_settled DRILL_STUBBORN_C
}

step_reload() { # reread and update with a live job and a changed definition (DL-432)
    local rev out rc=0 token
    request FORCE_STARTJOB DRILL_LONG_C
    wait_for 60 "DRILL_LONG_C is running" attempt_is DRILL_LONG_C 1 RUNNING
    rev=$(status | jq .config_rev)
    write_drill_jil v2
    out=$(cli reread) || rc=$?
    printf '%s\n' "$out"
    [ "$rc" = 1 ] || fail "reread exited $rc, not 1 (a plan that stops a running job)"
    grep -qx 'changed: DRILL_LONG_C' <<<"$out" || fail "reread does not name DRILL_LONG_C changed"
    grep -qx 'stops running: DRILL_LONG_C' <<<"$out" || fail "reread does not stop DRILL_LONG_C"
    token=$(sed -n 's/^token: //p' <<<"$out")
    [ -n "$token" ] || fail "reread printed no token"
    rc=0
    out=$(cli update "$token" --wait) || rc=$?
    printf '%s\n' "$out"
    [ "$rc" = 2 ] || fail "update without consent exited $rc, not 2"
    out=$(cli update "$token" --stop-running --wait) ||
        fail "update --stop-running --wait failed: $out"
    printf '%s\n' "$out"
    grep -q '^applied: catalog revision' <<<"$out" || fail "update printed no application"
    [ "$(status | jq .config_rev)" = $((rev + 1)) ] || fail "config_rev did not move past $rev"
    wait_for 60 "DRILL_LONG_C reran" attempt_is DRILL_LONG_C 2 RUNNING
    [ "$(job_field DRILL_LONG_C .attempt.after)" = '"update_stop"' ] ||
        fail "the rerun follows $(job_field DRILL_LONG_C .attempt.after), not update_stop"
    wait_for 30 "the rerun runs the new definition" alive "drill-job long v2"
    cli query spec --job DRILL_LONG_C --socket "$SOCK" | grep -qF "long v2" ||
        fail "spec does not serve the new definition"
    ok "the update stopped DRILL_LONG_C (update_stop) and reran it under revision $((rev + 1))"
    request KILLJOB DRILL_LONG_C
    wait_for 60 "DRILL_LONG_C settled after KILLJOB" attempt_settled DRILL_LONG_C
}

# the leak row's child PID wrote that it got TERM, and is gone
termed() { # termed PID
    sudo grep -qx "$1 TERM" "$LEAK_MARK" 2>/dev/null && ! sudo kill -0 "$1" 2>/dev/null
}
# a child wrote that it got TERM, and no child is left
some_termed() {
    sudo grep -qx '[0-9][0-9]* TERM' "$LEAK_MARK" 2>/dev/null && gone drill-leak-child
}

# The image fixes the expected signalling primitive; the log only proves it (DL-436).
os_field() { sed -n "s/^$1=//p" /etc/os-release | tr -d '"'; }
scan_primitive_expected() {
    local id version
    id=$(os_field ID)
    version=$(os_field VERSION_ID)
    case $id:$version in
        ubuntu:24.04) echo "pidfd" ;;
        rocky:8*) echo "os.kill (start-token check)" ;;
        *) fail "no leak expectation for image $id $version" ;;
    esac
}
scan_primitive_is() { # scan_primitive_is EXPECTED -- this serving invocation only (DL-436)
    local actual
    actual=$(sudo journalctl -u "$UNIT" "_SYSTEMD_INVOCATION_ID=$(prop InvocationID)" \
        -o cat --no-pager | sed -n 's/^dsl41: cookie scan signalling: //p')
    [ "$actual" = "$1" ]
}

step_leak() { # a job's leftover child is TERMed: live and at restart (DL-433, DL-436)
    local main child primitive
    primitive=$(scan_primitive_expected)
    wait_for 10 "the startup cookie scan uses $primitive" scan_primitive_is "$primitive"
    # live: the command exits 0 and leaves a child that carries its cookie
    sudo rm -f "$LEAK_MARK"
    request FORCE_STARTJOB DRILL_LEAK_C
    wait_for 60 "DRILL_LEAK_C succeeded" job_is DRILL_LEAK_C SUCCESS
    wait_for 60 "the engine TERMed the child DRILL_LEAK_C left" some_termed
    wait_for 30 "DRILL_LEAK_C settled" attempt_settled DRILL_LEAK_C
    # at a restart: the shipped unit's KillMode=control-group ends every process of the unit
    # when the engine dies, so nothing would be left for the restart to find. A drill-only
    # drop-in, KillMode=process, leaves the child alive, as an engine run outside a unit
    # would. The wrapper's lifeline ends only its command's process group, so the TERM the
    # child records comes from the restarted engine's cookie scan.
    sudo install -d "$(dirname "$LEAK_DROP_IN")"
    printf '[Service]\nKillMode=process\n' | sudo tee "$LEAK_DROP_IN" >/dev/null
    sudo systemctl daemon-reload
    sudo rm -f "$LEAK_MARK"
    request FORCE_STARTJOB DRILL_LEAK_LIVE_C
    wait_for 60 "DRILL_LEAK_LIVE_C is running" attempt_is DRILL_LEAK_LIVE_C 1 RUNNING
    wait_for 30 "the child is there" alive drill-leak-child
    child=$(pids_of drill-leak-child)
    [ "$(wc -w <<<"$child")" = 1 ] || fail "more than one child: $child"
    main=$(prop MainPID)
    sudo kill -KILL "$main"
    wait_for 60 "systemd restarted the engine" replaced "$main"
    wait_for 60 "the restarted engine TERMed the child $child left from before" termed "$child"
    wait_running
    wait_for 10 "the restart cookie scan uses $primitive" scan_primitive_is "$primitive"
    sudo rm -f "$LEAK_DROP_IN"
    sudo systemctl daemon-reload
    ok "the child a command left was TERMed live and at the restart"
    # the rerun left a child of its own; KILLJOB ends the command and the leak rule the child
    request KILLJOB DRILL_LEAK_LIVE_C
    wait_for 60 "DRILL_LEAK_LIVE_C settled" attempt_settled DRILL_LEAK_LIVE_C
    wait_for 30 "no child is left" gone drill-leak-child
}

# The unit failed with exit CODE and systemd left it there: no new invocation and no restart
# for twice RestartSec and more.
assert_stays_failed() { # assert_stays_failed CODE
    local invocation restarts
    wait_for 60 "$UNIT is failed" is_state failed
    invocation=$(prop InvocationID)
    restarts=$(prop NRestarts)
    sleep 12
    is_state failed || fail "$UNIT left failed: $(prop ActiveState)"
    [ "$(prop InvocationID)" = "$invocation" ] || fail "$UNIT was started again"
    [ "$(prop NRestarts)" = "$restarts" ] || fail "$UNIT was restarted"
    [ "$(prop ExecMainStatus)" = "$1" ] || fail "exit $(prop ExecMainStatus), expected $1"
}

step_refusal() { # pending source changes cannot deploy or refuse restart (DL-437)
    local mark rev
    # the estate the row found, restored afterwards whichever rows ran before it
    cp "$DRILL_JIL" "$STATE/drill.jil.before"
    rev=$(status | jq .config_rev)
    write_drill_jil v3
    mark=$(date +%s)
    manual restart
    wait_running
    wait_for 10 "the journal reports pending sources" \
        journal_has "$mark" "1 pending source changes; run dsl41 reread"
    [ "$(status | jq .config_rev)" = "$rev" ] || fail "restart applied an unreviewed source"
    ok "restart retained revision $rev and reported the pending source"
    sudo install -m 0644 "$STATE/drill.jil.before" "$DRILL_JIL"
    # A store version mismatch still refuses startup (DL-433, DL-437).
    sudo systemctl stop "$UNIT"
    sudo "$VENV/bin/python" - "$ROOT/engine.db" <<'PY'
import sqlite3
import sys
with sqlite3.connect(sys.argv[1]) as db:
    db.execute("UPDATE meta SET value = '999' WHERE key = 'state_version'")
PY
    manual start
    assert_stays_failed 2
    sudo "$VENV/bin/python" - "$ROOT/engine.db" <<'PY'
import sqlite3
import sys
from dsl41.runner_ledger import STATE_MACHINE_VERSION
with sqlite3.connect(sys.argv[1]) as db:
    db.execute("UPDATE meta SET value = ? WHERE key = 'state_version'", (str(STATE_MACHINE_VERSION),))
PY
    manual start
    wait_running
    ok "a named state version refusal kept the unit down; restoring the store recovered it"
}

step_start_limit() { # five crashes in 300 s leave the unit failed; reset-failed recovers
    local mark kills=0 pid
    sudo systemctl reset-failed "$UNIT"
    mark=$(date +%s)
    while ((kills < 10)) && ! is_state failed; do
        wait_running
        pid=$(prop MainPID)
        sudo kill -KILL "$pid"
        kills=$((kills + 1))
        wait_for 60 "the unit restarted the engine or gave up" moved_on "$pid"
    done
    is_state failed || fail "$UNIT was still restarted after $kills kills"
    # reset-failed cleared the counter: five restarts fit StartLimitBurst, the sixth does not
    [ "$kills" = 6 ] || fail "the unit gave up after $kills kills, not 6"
    wait_for 10 "the journal names the start limit" journal_has "$mark" "repeated too quickly"
    ok "after $kills kills the unit stays failed (StartLimitBurst=5)"
    manual start
    wait_running
    ok "reset-failed and a start recovered the engine on $ROOT"
}

step_upgrade() { # select a compatible engine release independently of app inputs (DL-437)
    local wheel before after rev version
    wheel=$(find "$DELIVERY" -maxdepth 1 -name 'dsl41-*.post*.whl' | head -n 1)
    [ -n "$wheel" ] || fail "no newer dsl41 wheel in $DELIVERY"
    before=$(installed_version)
    rev=$(status | jq .config_rev)
    sudo systemctl stop "$UNIT"
    version=$(basename "$wheel")
    version=${version#dsl41-}
    version=${version%%-*}
    VENV=/opt/dsl41/releases/$version
    DSL41=$VENV/bin/dsl41
    sudo python3.12 -m venv "$VENV"
    sudo "$VENV/bin/pip" install --quiet --disable-pip-version-check -r "$DELIVERY/requirements.txt"
    sudo "$VENV/bin/pip" install --quiet --disable-pip-version-check --no-deps "$wheel"
    after=$(installed_version)
    [ "$after" != "$before" ] || fail "the venv still has dsl41 $before"
    sed "s/<version>/$version/g" "$REPO/examples/nightbank/deploy/dsl41-engine.service" | \
        sudo tee "/etc/systemd/system/$UNIT" >/dev/null
    sudo systemctl daemon-reload
    installed_version >"$STATE/version-installed"
    manual start
    wait_running
    sudo grep -qF "$VENV/bin/python" "/proc/$(prop MainPID)/cmdline" ||
        fail "the engine does not run from $VENV"
    [ "$(status | jq .config_rev)" = "$rev" ] || fail "config_rev moved across the upgrade"
    [ "$(job_field APAC_EOD_B .on_hold)" = true ] || fail "the hold did not survive the upgrade"
    ok "dsl41 $before -> $after in $VENV; the restart resumed $ROOT at revision $rev"
}

step_diagnostics() { # what an operator reads after a failure; never fails
    sudo systemctl status "$UNIT" --no-pager || true
    sudo journalctl -u "$UNIT" -o short-precise --no-pager | tail -n 300 || true
    cli runs "$ROOT" || true
    status || true
}

case ${1-} in
    --list) printf '%s\n' "${ROWS[@]}" ;;
    reboot-before | reboot-after | diagnostics) "step_${1//-/_}" ;;
    *)
        for row in "${ROWS[@]}"; do
            if [ "$row" = "${1-}" ] && [ "$row" != reboot ]; then
                "step_${row//-/_}"
                exit 0
            fi
        done
        fail "usage: drill-steps.sh --list | ROW (one of: ${ROWS[*]}; reboot runs as" \
            "reboot-before and reboot-after) | diagnostics"
        ;;
esac
