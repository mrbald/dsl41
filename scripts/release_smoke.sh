#!/usr/bin/env bash
# Smoke-test a release candidate as a user installs it (DL-215).
#
#   scripts/release_smoke.sh <dist-dir> <exports-dir>
#
# <dist-dir> holds exactly one wheel and one sdist (`uv build`). <exports-dir>
# holds requirements-base.txt and requirements-ui.txt (`uv export`, hashes
# included). For each profile, base and ui, the script makes a fresh venv,
# installs the locked closure with --require-hashes, then the wheel with
# --no-deps, and drives the installed `dsl41` from a scratch directory with
# PYTHONPATH unset. Inputs come from the checkout's examples; code never does.
# Last, it rebuilds a wheel from the sdist and compares the two payloads.
#
# Environment:
#   PYTHON   interpreter for the venvs (default: python3)
#
# Every check prints one "ok:" line. The first failure prints "FAIL:" and
# exits non-zero; a failed setup step (mktemp, cd) exits non-zero through
# `set -e` without that line. Needs network for pip, and `unzip` and `ps`.
set -euo pipefail

if [ "$#" -ne 2 ]; then
    echo "usage: $0 <dist-dir> <exports-dir>" >&2
    exit 2
fi

repo="$(cd "$(dirname "$0")/.." && pwd)"
dist="$(cd "$1" && pwd)"
exports="$(cd "$2" && pwd)"
python="${PYTHON:-python3}"

fail() {
    echo "FAIL: $*" >&2
    exit 1
}
ok() { echo "ok: $*"; }

# Bound a command in seconds. GNU timeout is on Linux runners; macOS has it
# only with coreutils, so perl's alarm is the fallback there.
bounded() {
    local secs="$1"
    shift
    if command -v timeout >/dev/null 2>&1; then
        timeout "$secs" "$@"
    elif command -v gtimeout >/dev/null 2>&1; then
        gtimeout "$secs" "$@"
    else
        perl -e 'alarm shift; exec @ARGV or die "exec: $!"' "$secs" "$@"
    fi
}

# Poll COMMAND every 0.2s for up to SECS seconds (drill-lib.sh's wait_for
# shape, at finer granularity); fail with DESCRIPTION if it never
# succeeds. Prints no "ok:" line -- the caller's check may cover more than
# this one wait.
wait_until() { # wait_until SECS DESCRIPTION COMMAND...
    local limit="$1" what="$2" tries
    shift 2
    tries=$((limit * 5))
    for _ in $(seq 1 "$tries"); do
        "$@" && return 0
        sleep 0.2
    done
    fail "$what: not within ${limit}s"
}

# SIGINT PID, then wait up to SECS for it to exit. 0 once it is gone; 2 if
# SIGINT itself failed (already gone); 1 if it is still running when the
# wait ends. The two call sites turn 1 and 2 into their own message.
stop_engine() { # stop_engine PID SECS
    local pid="$1" limit="$2" tries
    kill -INT "$pid" 2>/dev/null || return 2
    tries=$((limit * 5))
    for _ in $(seq 1 "$tries"); do
        kill -0 "$pid" 2>/dev/null || return 0
        sleep 0.2
    done
    kill -0 "$pid" 2>/dev/null || return 0
    return 1
}

shopt -s nullglob
wheels=("$dist"/*.whl)
sdists=("$dist"/*.tar.gz)
shopt -u nullglob
[ "${#wheels[@]}" -eq 1 ] || fail "expected one wheel in $dist, found ${#wheels[@]}"
[ "${#sdists[@]}" -eq 1 ] || fail "expected one sdist in $dist, found ${#sdists[@]}"
wheel="${wheels[0]}"
sdist="${sdists[0]}"
ok "candidate: $(basename "$wheel") and $(basename "$sdist")"

# The run root holds the control socket, and AF_UNIX paths stop at 104 bytes
# on macOS; its $TMPDIR alone is about 50. So the scratch lives under /tmp.
work="$(mktemp -d /tmp/dsl41-smoke.XXXXXX)"
engine_pid=""
run_root=""
venv=""
cleanup() {
    if [ -n "$engine_pid" ] && kill -0 "$engine_pid" 2>/dev/null; then
        stop_engine "$engine_pid" 10 || true
        kill -KILL "$engine_pid" 2>/dev/null || true
        wait "$engine_pid" 2>/dev/null || true
    fi
    if [ -n "$run_root" ] && [ -e "$run_root/supervisor.lock" ]; then
        bounded 30 "$venv/bin/dsl41" supervise shutdown --run-root "$run_root" \
            >/dev/null 2>&1 || true
    fi
    rm -rf "$work"
}
trap cleanup EXIT

unset PYTHONPATH VIRTUAL_ENV PYTHONHOME
cd "$work"

# Nightbank's placeholders, fixed values (the launcher computes them per
# night, but it is checkout code and the smoke runs installed code only).
props="$work/night.properties"
cat >"$props" <<EOF
OWNER=smoke
NB_DATA=$work/nb/data
NB_LOGS=$work/nb/logs
NB_PROFILE=$work/nb/profile.env
EOD_APAC=09:05
EOD_EMEA=01:08
EOD_AMER=19:11
HB_WINDOW=00:05-01:35
EOF
estate=("$repo"/examples/nightbank/estate/small/*.jil)

# One CMD job: the smallest estate that still spawns a wrapper process.
cat >"$work/smoke.jil" <<'EOF'
insert_job: smoke_a
job_type: CMD
command: /bin/echo dsl41-smoke
machine: localhost
EOF

check_install() {
    local profile="$1"
    venv="$work/venv-$profile"
    "$python" -m venv "$venv" || fail "[$profile] $python -m venv"
    "$venv/bin/python" -m pip install --quiet --disable-pip-version-check \
        --require-hashes -r "$exports/requirements-$profile.txt" \
        || fail "[$profile] pip install --require-hashes -r requirements-$profile.txt"
    ok "[$profile] locked closure installed with --require-hashes"
    "$venv/bin/python" -m pip install --quiet --disable-pip-version-check --no-deps "$wheel" \
        || fail "[$profile] pip install --no-deps $(basename "$wheel")"
    ok "[$profile] wheel installed with --no-deps"
    "$venv/bin/python" -m pip check >/dev/null || fail "[$profile] pip check"
    ok "[$profile] pip check: no missing or conflicting requirements"

    "$venv/bin/python" - "$venv" "$profile" <<'EOF' || fail "[$profile] installed package layout"
import json
import sys
from importlib.metadata import distribution
from pathlib import Path

import dsl41

venv = Path(sys.argv[1]).resolve()
tag = f"[{sys.argv[2]}]"
where = Path(dsl41.__file__).resolve()
if venv not in where.parents:
    sys.exit(f"dsl41 imported from {where}, not from {venv}")
print(f"ok: {tag} import dsl41 resolves inside the venv ({where})")

dist = distribution("dsl41")
direct = dist.read_text("direct_url.json")
if direct and json.loads(direct).get("dir_info", {}).get("editable"):
    sys.exit("dsl41 is an editable install")
print(f"ok: {tag} dsl41 is not an editable install")

files = {str(f): f for f in dist.files or ()}
if "dsl41/py.typed" not in files or not Path(dist.locate_file(files["dsl41/py.typed"])).is_file():
    sys.exit("dsl41/py.typed missing from the installed package")
print(f"ok: {tag} dsl41/py.typed installed")

expected = {"LICENSE", "LICENSING.md", "COMMERCIAL.md", "THIRD_PARTY_LICENSES"}
declared = set(dist.metadata.get_all("License-File") or ())
if declared != expected:
    sys.exit(f"License-File metadata {sorted(declared)} != {sorted(expected)}")
for name in sorted(expected):
    hits = [f for key, f in files.items() if key.endswith(f".dist-info/licenses/{name}")]
    if not hits or not Path(dist.locate_file(hits[0])).is_file():
        sys.exit(f"license file {name} missing from the dist-info")
print(f"ok: {tag} license files installed: {', '.join(sorted(expected))}")
EOF

    bounded 60 "$venv/bin/dsl41" --help >/dev/null || fail "[$profile] dsl41 --help"
    ok "[$profile] dsl41 --help exits 0"
}

check_pipeline() {
    local profile="$1" out="$work/out-$1"
    mkdir -p "$out"
    bounded 120 "$venv/bin/dsl41" uc "${estate[@]}" -p "$props" -o "$out/bundle.json" \
        2>"$out/uc.err" || { cat "$out/uc.err" >&2; fail "[$profile] dsl41 uc on nightbank"; }
    "$venv/bin/python" - "$out/bundle.json" <<'EOF' || fail "[$profile] uc bundle content"
import json
import sys
from importlib.metadata import version

bundle = json.load(open(sys.argv[1]))
if bundle.get("tool_version") != version("dsl41"):
    sys.exit(f"tool_version {bundle.get('tool_version')!r} is not {version('dsl41')!r}")
if not bundle.get("records"):
    sys.exit("the bundle has no records")
EOF
    ok "[$profile] dsl41 uc compiled examples/nightbank/estate/small" \
        "(records present, tool_version matches)"

    # each page must carry the installed _vendor files byte for byte
    local format bundles
    for format in html html-chart explore; do
        case "$format" in
            explore) bundles="cytoscape-explore.iife.min.js custom-elements.min.js" ;;
            *) bundles="mermaid.min.js mermaid-layout-elk.iife.min.js" ;;
        esac
        bounded 120 "$venv/bin/dsl41" viz --format "$format" "${estate[@]}" -p "$props" \
            -o "$out/$format.html" || fail "[$profile] dsl41 viz --format $format"
        head -c 256 "$out/$format.html" | grep -qi '<!doctype html' \
            || fail "[$profile] viz --format $format did not write an HTML page"
        # shellcheck disable=SC2086  # $bundles is a word list
        "$venv/bin/python" - "$out/$format.html" $bundles <<'EOF' \
            || fail "[$profile] viz --format $format page does not inline its bundles"
import sys
from importlib.resources import files

page = open(sys.argv[1], "rb").read()
for name in sys.argv[2:]:
    data = files("dsl41").joinpath("_vendor", name).read_bytes()
    if len(data) < 1024 or data not in page:
        sys.exit(f"{name} ({len(data)} bytes) is not inlined in the page")
EOF
        ok "[$profile] dsl41 viz --format $format inlines $bundles byte for byte"
    done
}

# SOCK is bound; fails immediately, with LOG's tail, if PID died first --
# this is not part of wait_until's own timeout
_socket_bound() { # _socket_bound SOCK PID LOG PROFILE
    [ -S "$1" ] && return 0
    kill -0 "$2" 2>/dev/null || { cat "$3" >&2; fail "[$4] engine exited early"; }
    return 1
}

_job_succeeded() { # _job_succeeded VENV SOCK
    case "$(bounded 30 "$1/bin/dsl41" query status --brief -S "$2" || true)" in
        *SUCCESS*) return 0 ;;
        *) return 1 ;;
    esac
}

# A detached lifecycle: engine up, supervisor spawned from the installed
# package, one job to SUCCESS, engine stopped, supervisor shut down. The
# ui profile also mounts the TUI while the engine is live.
check_lifecycle() {
    local profile="$1"
    run_root="$work/run-$profile"
    local sock="$run_root/control.sock" log="$work/engine-$profile.log"
    # Not under `bounded`: with GNU timeout in between, the SIGINT below did
    # not end the run before timeout's own deadline (seen with coreutils 9.x;
    # cause not traced). Each wait below has its own bound instead, and the
    # EXIT trap stops a leftover engine.
    "$venv/bin/dsl41" run "$work/smoke.jil" --run-root "$run_root" --detached \
        >"$log" 2>&1 &
    engine_pid=$!

    wait_until 30 "[$profile] control socket is bound" \
        _socket_bound "$sock" "$engine_pid" "$log" "$profile"
    ok "[$profile] dsl41 run --detached is up (control socket bound)"

    bounded 30 "$venv/bin/dsl41" sendevent STARTJOB -J smoke_a -S "$sock" >/dev/null \
        || fail "[$profile] sendevent STARTJOB"
    wait_until 30 "[$profile] smoke_a reaches SUCCESS" _job_succeeded "$venv" "$sock"
    grep -q dsl41-smoke "$run_root/logs/smoke_a.1.out" \
        || fail "[$profile] the job's stdout log lacks its output"
    ok "[$profile] STARTJOB ran smoke_a to SUCCESS; its stdout reached the run's log"

    # the supervisor must run from the installed package, not the checkout
    bounded 30 "$venv/bin/dsl41" supervise list --run-root "$run_root" >"$work/sup.json" \
        || fail "[$profile] supervise list"
    local sup_pid sup_cmd
    sup_pid="$("$venv/bin/python" -c \
        'import json, sys; print(json.load(open(sys.argv[1]))["supervisor_pid"])' \
        "$work/sup.json")" || fail "[$profile] supervise list has no supervisor_pid"
    sup_cmd="$(ps -o command= -p "$sup_pid")" || fail "[$profile] supervisor $sup_pid not running"
    case "$sup_cmd" in
        *"$venv"/*runner_supervisor*) ;;
        *) fail "[$profile] supervisor does not run from the venv: $sup_cmd" ;;
    esac
    ok "[$profile] supervisor (pid $sup_pid) runs from the installed package"

    if [ "$profile" = ui ]; then
        check_tui
    fi

    local stopped=0
    stop_engine "$engine_pid" 30 || stopped=$?
    case "$stopped" in
        0) ;;
        2) fail "[$profile] engine gone before SIGINT" ;;
        *) fail "[$profile] engine still running 30s after SIGINT" ;;
    esac
    local rc=0
    wait "$engine_pid" || rc=$?
    engine_pid=""
    [ "$rc" -eq 0 ] || { cat "$log" >&2; fail "[$profile] engine exited $rc on SIGINT"; }
    ok "[$profile] engine stopped on SIGINT with exit 0"
    bounded 60 "$venv/bin/dsl41" supervise shutdown --run-root "$run_root" >/dev/null \
        || fail "[$profile] supervise shutdown"
    run_root=""
    ok "[$profile] supervisor shut down"
}

# The TUI, headless: construct the app on the live socket, mount it through
# Textual's run_test, wait for the job's row, exit.
check_tui() {
    bounded 90 "$venv/bin/python" - "$run_root/control.sock" <<'EOF' || fail "[ui] TUI mount"
import asyncio
import sys
from pathlib import Path

from textual.widgets import DataTable

from dsl41.runner_tui import RunnerApp


async def main(sock: Path) -> None:
    app = RunnerApp(sock)
    async with app.run_test(size=(120, 40)) as pilot:
        table = app.query_one("#jobs", DataTable)
        for _ in range(300):
            if "smoke_a" in {key.value for key in table.rows}:
                break
            await pilot.pause(0.1)
        else:
            sys.exit("the TUI's jobs table never showed smoke_a")
        cells = [str(cell) for cell in table.get_row("smoke_a")]
        if not any("SUCCESS" in cell for cell in cells):
            sys.exit(f"smoke_a's row does not read SUCCESS: {cells}")
    print("ok: [ui] TUI mounted headless on the live socket; smoke_a's row reads SUCCESS")


asyncio.run(asyncio.wait_for(main(Path(sys.argv[1])), timeout=60))
EOF
}

for profile in base ui; do
    echo "== profile $profile"
    check_install "$profile"
    check_pipeline "$profile"
    if [ "$profile" = base ]; then
        check_lifecycle base
        # cli_common.import_tui_or_exit_2: without the [ui] extra, `dsl41 ui`
        # refuses with exit 2 before it looks for the socket
        rc=0
        "$venv/bin/dsl41" ui -S "$work/no.sock" 2>"$work/ui.err" || rc=$?
        [ "$rc" -eq 2 ] || fail "[base] dsl41 ui exited $rc, expected the exit-2 refusal"
        grep -q "optional \[ui\] extra" "$work/ui.err" \
            || fail "[base] dsl41 ui refusal text: $(cat "$work/ui.err")"
        ok "[base] dsl41 ui refuses with exit 2 and names the [ui] extra"
    else
        check_lifecycle ui
    fi
done

# The sdist must build the same wheel. RECORD is excluded: it lists the
# other files' hashes, so it adds nothing once they are compared, and it is
# the one file a builder may order or format differently.
echo "== sdist rebuild"
# The rebuild uses the hatchling that built the candidate (its WHEEL
# Generator line), so a hatchling release in between cannot change WHEEL.
generator="$(unzip -p "$wheel" '*.dist-info/WHEEL' | sed -n 's/^Generator: hatchling //p')" \
    || fail "read the Generator line of $(basename "$wheel")"
[ -n "$generator" ] || fail "$(basename "$wheel") names no hatchling Generator"
echo "hatchling==$generator" >"$work/build-constraints.txt"
ok "rebuild constrained to hatchling==$generator"
rebuilt="$work/rebuilt"
PIP_CONSTRAINT="$work/build-constraints.txt" bounded 300 "$venv/bin/python" -m pip wheel \
    --quiet --disable-pip-version-check --no-deps "$sdist" -w "$rebuilt" \
    || fail "pip wheel from $(basename "$sdist")"
"$venv/bin/python" - "$wheel" "$rebuilt"/*.whl <<'EOF' || fail "sdist-rebuilt wheel differs"
import hashlib
import sys
import zipfile


def payload(path: str) -> dict[str, str]:
    with zipfile.ZipFile(path) as zf:
        return {
            name: hashlib.sha256(zf.read(name)).hexdigest()
            for name in zf.namelist()
            if not name.endswith(".dist-info/RECORD")
        }


direct, rebuilt = payload(sys.argv[1]), payload(sys.argv[2])
if set(direct) != set(rebuilt):
    only_d = sorted(set(direct) - set(rebuilt))
    only_r = sorted(set(rebuilt) - set(direct))
    sys.exit(f"file lists differ: only direct {only_d}, only rebuilt {only_r}")
changed = sorted(name for name in direct if direct[name] != rebuilt[name])
if changed:
    sys.exit(f"payloads differ in: {changed}")
print(f"ok: sdist-rebuilt wheel matches the direct wheel ({len(direct)} files, RECORD excluded)")
EOF

echo "release smoke: all checks passed"
