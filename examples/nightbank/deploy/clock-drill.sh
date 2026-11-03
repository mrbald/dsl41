#!/usr/bin/env bash
# Installed waiter ordering and timeout checks for docs/deploy.md. No shared clock writes.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=drill-lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/drill-lib.sh"

work=$STATE/clock
mkdir -p "$work"
if [ "${1-}" = --cleanup ]; then
    exec > >(tee "$work/cleanup.log") 2>&1
else
    exec > >(tee "$work/row.log") 2>&1
fi

case $(sed -n 's/^ID=//p' /etc/os-release | tr -d '"') in
    rocky) waiter=chrony-wait.service; expected_timeout=3min ;;
    ubuntu) waiter=systemd-time-wait-sync.service; expected_timeout=infinity ;;
    *) fail 'no clock waiter expectation for this image' ;;
esac
drop_in=/run/systemd/system/$waiter.d/90-drill-clock.conf
timeout_drop_in=/run/systemd/system/$waiter.d/95-drill-clock-timeout.conf
daemon_drop_in=/run/systemd/system/chronyd.service.d/90-drill-clock.conf
source_unit=dsl41-drill-clock-source.service
source_path=/run/systemd/system/$source_unit
shim=/run/dsl41-clock-unsynced.so

waiter_prop() { systemctl show -p "$1" --value "$waiter"; }
waiter_active() { [ "$(waiter_prop ActiveState)" = active ]; }
waiter_timed_out() {
    [ "$(waiter_prop ActiveState)" = failed ] && [ "$(waiter_prop Result)" = timeout ];
}
capture() { # capture PHASE -- retain failures before the cleanup removes the fixture
    local unit
    for unit in "$waiter" time-sync.target "$UNIT"; do
        systemctl show "$unit" >"$work/$1-$unit.show"
        sudo systemctl status "$unit" --no-pager 2>&1 | tee "$work/$1-$unit.status" >/dev/null || true
        sudo journalctl -u "$unit" --no-pager -o short-monotonic |
            tee "$work/$1-$unit.journal" >/dev/null
    done
}
cleanup() {
    local rc=0 path enabled
    sudo timeout -k 5 20 systemctl disable --now "$waiter" || rc=1
    if [ "$waiter" = chrony-wait.service ]; then
        sudo timeout -k 5 20 systemctl stop chronyd.service || rc=1
        sudo timeout -k 5 20 systemctl stop "$source_unit" || rc=1
        sudo timeout -k 2 5 journalctl -u "$source_unit" --no-pager |
            tee "$work/source.journal" >/dev/null || true
        sudo rm -f "$daemon_drop_in" "$source_path"
    else
        sudo rm -f /run/systemd/timesync/synchronized "$shim"
    fi
    sudo rm -f "$drop_in" "$timeout_drop_in"
    sudo timeout -k 5 10 systemctl daemon-reload || rc=1
    for path in "$drop_in" "$timeout_drop_in" "$daemon_drop_in" "$source_path" "$shim"; do
        [ ! -e "$path" ] || { echo "cleanup left $path" >&2; rc=1; }
    done
    enabled=$(timeout -k 2 5 systemctl is-enabled "$waiter" 2>/dev/null || true)
    [ "$enabled" = disabled ] || { echo "cleanup left waiter $enabled" >&2; rc=1; }
    [ "$rc" != 0 ] || ok 'clock cleanup: waiter disabled and drill overrides removed'
    exit "$rc"
}
case ${1-} in
    --cleanup) cleanup ;;
    '') trap 'capture final || true' EXIT ;;
    *) fail 'usage: clock-drill.sh [--cleanup]' ;;
esac

record_synchronized() { # docs/deploy.md: enabled alone does not prove the waiter ran
    local expected
    printf '+ systemctl show -p ActiveState,Result,ConditionResult %s\n' "$waiter"
    systemctl show -p ActiveState,Result,ConditionResult "$waiter" |
        tee "$work/waiter-success.show"
    for expected in ActiveState=active Result=success ConditionResult=yes; do
        grep -qx "$expected" "$work/waiter-success.show" || fail "waiter lacks $expected"
    done
    if [ "$waiter" = chrony-wait.service ]; then
        sudo chronyc tracking | tee "$work/chrony-yes.txt"
        grep -qE '^Leap status *: Normal$' "$work/chrony-yes.txt" ||
            fail 'chrony tracking is not synchronized after the waiter became active'
    fi
}

set_sync() { # set_sync yes|no -- synthetic input to the vendor waiter (docs/deploy.md)
    if [ "$waiter" = chrony-wait.service ]; then
        if [ "$1" = yes ]; then
            sudo timeout -k 5 20 systemctl start "$source_unit"
        else
            sudo timeout -k 5 20 systemctl stop "$source_unit"
            # A fresh client has no retained synchronization after the source disappears.
            sudo timeout -k 5 20 systemctl restart chronyd.service
        fi
        if [ "$1" = no ]; then sudo chronyc tracking | tee "$work/chrony-no.txt"; fi
    elif [ "$1" = yes ]; then
        sudo touch /run/systemd/timesync/synchronized
    else
        sudo rm -f /run/systemd/timesync/synchronized
    fi
}
arm() {
    sudo timeout -k 5 45 systemctl stop "$UNIT" time-sync.target "$waiter"
    set_sync no
    sudo systemctl reset-failed "$UNIT" "$waiter"
    # Both enabled waiters belong to boot targets, not to time-sync.target's Wants.
    # Submit the boot ordering in one transaction without rebooting the shared VM.
    sudo systemctl start --no-block "$waiter" "$UNIT"
    wait_for 10 'the waiter has a process' waiter_started
}
waiter_started() { [ "$(waiter_prop ActiveState)" = activating ] && [ "$(waiter_prop MainPID)" != 0 ]; }
assert_blocked() {
    local i
    for i in 1 2 3; do
        waiter_started || fail 'the waiter finished without synchronization'
        [ "$(prop MainPID)" = 0 ] || fail 'the engine started before the waiter finished'
        [ "$(systemctl show -p ActiveState --value time-sync.target)" = inactive ] ||
            fail 'time-sync.target was reached before the waiter finished'
        systemctl list-jobs --no-pager --no-legend | tee "$work/queued-$i.txt" |
            grep -qE " $UNIT +start +waiting" || fail 'no queued engine start'
        sleep 1
    done
    ok 'engine startup stays queued while the unsynchronized waiter runs'
}
assert_order() { # assert_order WAITER_COMPLETION_MONOTONIC
    local target engine
    target=$(systemctl show -p ActiveEnterTimestampMonotonic --value time-sync.target)
    engine=$(prop ExecMainStartTimestampMonotonic)
    (( $1 > 0 && target >= $1 && engine >= target )) ||
        fail "startup order: waiter=$1 target=$target engine=$engine"
    printf 'waiter_end_us=%s target_active_us=%s engine_start_us=%s\n' "$1" "$target" "$engine"
}
record_python() {
    local pid version command_line
    pid=$(prop MainPID)
    command_line=$(sudo cat "/proc/$pid/cmdline" | tr '\0' '\n')
    printf '%s\n' "$command_line" | head -n 1 | grep -qF "$VENV/bin/python" ||
        fail 'the running engine did not select the installed release interpreter'
    version=$(sudo "/proc/$pid/exe" -VV)
    printf 'pid=%s\nexe=%s\n%s\nargv:\n%s\n' "$pid" \
        "$(sudo readlink "/proc/$pid/exe")" "$version" "$command_line" |
        tee "$work/python.txt"
    case $version in 'Python 3.12.'*) ;; *) fail "engine interpreter is not 3.12: $version" ;; esac
}

systemctl cat "$waiter" | tee "$work/vendor-waiter.unit"
[ "$(waiter_prop Type)" = oneshot ] || fail 'the vendor waiter is not oneshot'
[ "$(waiter_prop TimeoutStartUSec)" = "$expected_timeout" ] ||
    fail "installed waiter timeout differs from docs/deploy.md: $(waiter_prop TimeoutStartUSec)"
systemctl cat "$UNIT" >"$work/engine.unit"
[[ " $(prop After) " == *' time-sync.target '* ]] || fail 'engine lacks After=time-sync.target'
[[ " $(prop Wants) " == *' time-sync.target '* ]] || fail 'engine lacks Wants=time-sync.target'
[[ " $(waiter_prop Before) " == *' time-sync.target '* ]] || fail 'waiter lacks Before=time-sync.target'
sudo systemd-analyze verify "/etc/systemd/system/$UNIT" \
    /etc/systemd/system/dsl41-engine-down.service
record_python

sudo install -d "$(dirname "$drop_in")"
if [ "$waiter" = chrony-wait.service ]; then
    sudo timeout -k 5 20 systemctl stop chronyd.service
    printf 'pidfile /run/chrony/chronyd.pid\nserver 127.0.0.1 port 1123 iburst minpoll 0 maxpoll 0\n' \
        >"$work/chrony.conf"
    cat >"$work/source.conf" <<'CONF'
port 1123
bindaddress 127.0.0.1
bindcmdaddress /run/chrony/drill-source.sock
cmdport 0
pidfile /run/chrony/drill-source.pid
local stratum 8
allow 127.0.0.1
CONF
    printf '[Service]\nType=simple\nExecStart=/usr/sbin/chronyd -d -x -f %s/source.conf\n' \
        "$work" | tee "$work/source.unit" | sudo tee "$source_path" >/dev/null
    sudo install -d "$(dirname "$daemon_drop_in")"
    # -x observes chrony's synchronization state without setting the VM's clock.
    printf '[Unit]\nConditionCapability=\n[Service]\nEnvironmentFile=\nEnvironment="OPTIONS=-x -f %s/chrony.conf"\n' \
        "$work" | sudo tee "$daemon_drop_in" | tee "$work/daemon-drop-in.conf"
    printf '[Unit]\nConditionCapability=\n' | sudo tee "$drop_in" >/dev/null
    sudo systemctl daemon-reload
    sudo timeout -k 5 20 systemctl start chronyd.service
else
    sudo timeout -k 5 20 systemctl stop systemd-timesyncd.service
    sudo install -d /run/systemd/timesync
    sudo gcc -shared -fPIC -Wall -Wextra -Werror -o "$shim" \
        "$REPO/examples/nightbank/deploy/clock-unsynced.c"
    # Container conditions and kernel fallback are local fixtures; ExecStart stays vendor's.
    printf '[Unit]\nConditionCapability=\nConditionVirtualization=\n[Service]\nEnvironment=LD_PRELOAD=%s\n' \
        "$shim" | sudo tee "$drop_in" >/dev/null
    sudo systemctl daemon-reload
fi
sudo cat "$drop_in" | tee "$work/waiter-drop-in.conf"
set_sync yes
printf '+ systemctl enable --now %s\n' "$waiter"
sudo timeout -k 5 30 systemctl enable --now "$waiter"
printf '+ systemctl is-enabled %s\n' "$waiter"
enabled=$(systemctl is-enabled "$waiter")
printf '%s\n' "$enabled"
[ "$enabled" = enabled ] || fail "$waiter is not enabled"
printf '+ systemctl status %s\n' "$waiter"
sudo systemctl status "$waiter" --no-pager
record_synchronized
capture enabled

arm
assert_blocked
capture blocked
set_sync yes
wait_for 15 'the waiter finishes after the synchronization signal' waiter_active
record_synchronized
wait_running
assert_order "$(waiter_prop ActiveEnterTimestampMonotonic)" | tee "$work/success-order.txt"
capture synchronized

# The installed 180 s/infinite bound is shortened only in this drill (docs/deploy.md).
printf '[Service]\nTimeoutStartSec=8s\n' | sudo tee "$timeout_drop_in" | tee "$work/timeout-drop-in.conf"
sudo systemctl daemon-reload
[ "$(waiter_prop TimeoutStartUSec)" = 8s ] || fail 'the drill-only timeout did not take effect'
arm
assert_blocked
wait_for 20 'the unsynchronized waiter fails with Result=timeout' waiter_timed_out
wait_running
start=$(waiter_prop ExecMainStartTimestampMonotonic)
end=$(waiter_prop StateChangeTimestampMonotonic)
(( end - start >= 8000000 && end - start < 20000000 )) ||
    fail "8 s waiter bound took $((end - start)) microseconds"
assert_order "$end" | tee "$work/timeout-order.txt"
printf 'installed_timeout=%s drill_timeout=8s actual_us=%s result=%s\n' \
    "$expected_timeout" "$((end - start))" "$(waiter_prop Result)" | tee "$work/timeout.txt"
set_sync no
record_python
capture timed-out
ok 'vendor waiter gates startup; timeout releases the engine without synchronization; engine uses Python 3.12'
