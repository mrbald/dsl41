#!/usr/bin/env bash
# The deployment rehearsal needs a working service cgroup, not just systemctl (DL-390).
set -euo pipefail

refuse() { printf 'capability: REFUSED: %s\n' "$*" >&2; exit 2; }
case ${1-} in
    '') ;;
    --native-x86_64) [ "$(uname -m)" = x86_64 ] || refuse 'native x86_64 is required' ;;
    *) refuse 'usage: capability-check.sh [--native-x86_64]' ;;
esac
[ "$(uname -s)" = Linux ] || refuse 'Linux is required'
[ "$(id -u)" = 0 ] || refuse 'run as root in the disposable rehearsal host'
for tool in systemctl busctl timeout flock python3.12; do
    command -v "$tool" >/dev/null || refuse "missing prerequisite: $tool"
done
[ "$(cat /proc/1/comm)" = systemd ] || refuse 'systemd must be PID 1'
python3.12 -I -c 'import sys, venv, ensurepip; assert sys.version_info[:2] == (3, 12)' \
    || refuse 'system Python 3.12 with venv and ensurepip is required'

unit=dsl41-capability-$$.service
drain=dsl41-capability-drain-$$.service
scratch=$(mktemp -d)
cleanup() {
    systemctl kill --signal=TERM "$unit" "$drain" >/dev/null 2>&1 || true
    rm -f "/run/systemd/system/$unit" "/run/systemd/system/$drain"
    systemctl daemon-reload
    systemctl reset-failed "$unit" "$drain" >/dev/null 2>&1 || true
    rm -rf "$scratch"
}
trap cleanup EXIT
cat >"/run/systemd/system/$unit" <<'UNIT'
[Service]
Type=simple
SuccessExitStatus=8
Restart=always
RestartSec=30
RestartPreventExitStatus=8
ExecStart=/bin/sh -c 'trap "exit 8" TERM; while :; do sleep 0.1; done'
UNIT
# shellcheck disable=SC2016  # systemd escapes the dollar before the shell reads it (DL-390).
printf 'ExecStopPost=/bin/sh -c '\''echo "$$EXIT_STATUS" > %s/status'\''\n' \
    "$scratch" >>"/run/systemd/system/$unit"
systemctl daemon-reload
timeout 15 systemctl start "$unit" || refuse 'cannot start the probe service'
for _ in $(seq 50); do
    pid=$(systemctl show --value --property=MainPID "$unit")
    [ "${pid:-0}" -gt 0 ] && break
    sleep 0.1
done
[ "${pid:-0}" -gt 0 ] || refuse 'probe service has no process'
group=$(systemctl show --value --property=ControlGroup "$unit")
[ -n "$group" ] && [ "$group" != / ] || refuse 'service has no dedicated cgroup'
cat "/proc/$pid/cgroup" >"$scratch/cgroup"
grep -F -- "$group" "$scratch/cgroup" >/dev/null || refuse 'process is outside service cgroup'
kill -TERM "$pid"
for _ in $(seq 50); do
    state=$(systemctl show --value --property=ActiveState "$unit")
    [ "$state" = inactive ] && break
    sleep 0.1
done
[ "$state" = inactive ] || refuse 'maintenance exit did not stay inactive'
[ "$(cat "$scratch/status")" = 8 ] \
    || refuse "maintenance exit status was $(cat "$scratch/status"), expected 8"
# DL-416: the supervisor unit's ExecStop sends SIGTERM to the main process alone and waits
# for it, so KillMode=control-group's SIGTERM reaches only what is left. The probe's main
# process waits on SIGTERM for its child, which has no handler and ends only on release.
# shellcheck disable=SC2016  # systemd escapes the dollar before the shell reads it (DL-390).
{
    printf '[Service]\nType=simple\nTimeoutStopSec=30\n'
    printf 'ExecStart=/bin/sh -c '\''(while [ ! -e %s/release ]; do sleep 0.1; done;' "$scratch"
    printf ' echo done > %s/child) & trap "wait; exit 0" TERM;' "$scratch"
    printf ' while :; do sleep 0.1; done'\''\n'
    printf '%s\n' 'ExecStop=/bin/sh -c '\''kill -TERM "$$MAINPID"; while kill -0 "$$MAINPID" 2>/dev/null; do sleep 0.1; done'\'''
} >"/run/systemd/system/$drain"
systemctl daemon-reload
timeout 15 systemctl start "$drain" || refuse 'cannot start the drain probe'
systemctl stop --no-block "$drain"
for _ in $(seq 50); do
    state=$(systemctl show --value --property=ActiveState "$drain")
    [ "$state" = deactivating ] && break
    sleep 0.1
done
[ "$state" = deactivating ] || refuse "the drain probe did not begin its stop: $state"
touch "$scratch/release"
for _ in $(seq 100); do
    state=$(systemctl show --value --property=ActiveState "$drain")
    [ "$state" = inactive ] && break
    sleep 0.1
done
[ "$state" = inactive ] || refuse 'the drain probe did not stop'
[ "$(cat "$scratch/child" 2>/dev/null)" = "done" ] \
    || refuse 'a unit stop signalled the command before ExecStop let it end'
printf 'capability: PASS architecture=%s systemd=%s cgroup=%s python=%s\n' \
    "$(uname -m)" "$(systemctl --version | head -1)" "$group" "$(python3.12 --version)"
