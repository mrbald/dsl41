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
scratch=$(mktemp -d)
cleanup() {
    systemctl kill --signal=TERM "$unit" >/dev/null 2>&1 || true
    rm -f "/run/systemd/system/$unit"
    systemctl daemon-reload
    systemctl reset-failed "$unit" >/dev/null 2>&1 || true
    rm -rf "$scratch"
}
trap cleanup EXIT
cat >"/run/systemd/system/$unit" <<'UNIT'
[Unit]
RefuseManualStop=yes
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
if timeout 5 systemctl stop "$unit" 2>"$scratch/refusal"; then
    refuse 'RefuseManualStop did not reject a manager stop'
fi
systemctl is-active --quiet "$unit" || refuse 'refused stop changed service state'
kill -TERM "$pid"
for _ in $(seq 50); do
    state=$(systemctl show --value --property=ActiveState "$unit")
    [ "$state" = inactive ] && break
    sleep 0.1
done
[ "$state" = inactive ] || refuse 'maintenance exit did not stay inactive'
[ "$(cat "$scratch/status")" = 8 ] \
    || refuse "maintenance exit status was $(cat "$scratch/status"), expected 8"
printf 'capability: PASS architecture=%s systemd=%s cgroup=%s python=%s\n' \
    "$(uname -m)" "$(systemctl --version | head -1)" "$group" "$(python3.12 --version)"
