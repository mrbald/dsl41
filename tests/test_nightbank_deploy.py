"""DL-218: the nightbank deployment example -- `dsl41-launch` and the two
systemd units beside it (examples/nightbank/deploy/).

The launcher is the one reviewed place that holds the run root and the
whole ordered `dsl41 run` line. These tests run the shipped script. Where a
test needs a root it can write, it copies the script and replaces only the
values in its CONFIGURATION block, so the logic under test is the shipped
logic. The service-level claims -- systemd starting, restarting and
refusing -- are the drill's (`.github/workflows/service-drill.yml`), not
these tests'.
"""

from __future__ import annotations

import os
import pwd
import re
import shlex
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dsl41.period import CMD_GRACE_S

from test_nightbank_example import NB, _launcher

DEPLOY = NB / "deploy"
LAUNCH = DEPLOY / "dsl41-launch"
DRILL_LIB = DEPLOY / "drill-lib.sh"
DSL41 = Path(sys.executable).with_name("dsl41")
ME = pwd.getpwuid(os.geteuid()).pw_name
#: the order the launcher names the files in; it is part of the catalog
#: address, so a reordering is a new estate and this list pins it
ORDER = ("amer.jil", "apac.jil", "calendars.jil", "emea.jil", "global.jil", "infra.jil")
SHIPPED_ROOT = "/srv/dsl41/runs/nightbank-01"


def _sh(launcher: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["sh", str(launcher), *args], capture_output=True, text=True, timeout=120)


def _site(base: Path) -> dict[str, str]:
    return {
        "DSL41": str(DSL41),
        "RUN_ROOT": str(base / "runs" / "nb"),
        "ESTATE_ANCHOR": str(base / "runs" / "nb.anchor"),
        "ACCESS_MAP": str(base / "etc" / "access.toml"),
        "ESTATE": str(NB / "estate" / "small"),
        "PROPERTIES": str(base / "night" / "night.properties"),
    }


def _configured(base: Path, **values: str) -> Path:
    """The shipped launcher with its CONFIGURATION values replaced, one
    `NAME=` line each, and nothing else touched."""
    text = LAUNCH.read_text()
    for name, value in values.items():
        lines = [line for line in text.splitlines() if line.startswith(f"{name}=")]
        assert len(lines) == 1, f"{name}= must be one line in the CONFIGURATION block"
        text = text.replace(lines[0] + "\n", f"{name}={shlex.quote(value)}\n")
    path = base / "dsl41-launch"
    path.write_text(text)
    path.chmod(0o755)
    return path


def _night(base: Path) -> Path:
    """A night's properties, built by nightbank's own `prepare_night`, the
    anchors six hours out so nothing fires under the test."""
    _launcher().prepare_night(
        base / "night",
        "small",
        anchor_utc=datetime.now(UTC) + timedelta(hours=6),
        incidents=False,
    )
    return base / "night" / "night.properties"


def _write_map(path: Path, body: str) -> None:
    path.parent.mkdir(mode=0o700, exist_ok=True)
    os.chmod(path.parent, 0o700)  # umask-proof: the loader checks the parent
    path.write_text(body)
    os.chmod(path, 0o600)


def _grant_me(path: Path) -> None:
    """The shipped example map with its service account swapped for the
    user running the test, so this engine's owner is mapped."""
    body = (DEPLOY / "nightbank-access.toml").read_text()
    assert body.count('"user:os/dsl41"') == 1
    _write_map(path, body.replace('"user:os/dsl41"', f'"user:os/{ME}"'))


# ------------------------------------------------------------ the command


def test_the_shipped_launcher_prints_the_whole_ordered_line() -> None:
    if Path(SHIPPED_ROOT, "journal.jsonl").exists():  # pragma: no cover -- a real host
        pytest.skip("this host holds the example's run root")
    printed = _sh(LAUNCH, "--print")
    assert printed.returncode == 0, printed.stderr
    estate = "/srv/dsl41/nightbank/estate/small"
    assert shlex.split(printed.stdout) == [
        "/opt/dsl41/venv/bin/dsl41",
        "run",
        *(f"{estate}/{name}" for name in ORDER),
        "-p",
        "/srv/dsl41/nightbank/night/night.properties",
        "--run-root",
        SHIPPED_ROOT,
        "--estate-anchor",
        f"{SHIPPED_ROOT}.anchor",
        "--detached",
        "--as-machine",
        "localhost",
        "--machine-policy",
        "strict",
        "--timezone",
        "UTC",
        "--access-map",
        "/etc/dsl41/nightbank-access.toml",
    ]
    assert printed.stdout.count("\n") == 1  # one line, nothing else on stdout
    # the order pin above is the estate's own file set, not a subset of it
    assert sorted(ORDER) == sorted(p.name for p in (NB / "estate" / "small").glob("*.jil"))


def test_the_shipped_launcher_prints_the_supervisor_line() -> None:
    printed = _sh(LAUNCH, "--print", "supervisor")
    assert printed.returncode == 0, printed.stderr
    assert shlex.split(printed.stdout) == [
        "/opt/dsl41/venv/bin/dsl41",
        "supervise",
        "start",
        "--run-root",
        SHIPPED_ROOT,
    ]


def test_the_printed_line_is_shell_quoted(short_root: Path) -> None:
    launcher = _configured(short_root, RUN_ROOT=str(short_root / "it's a root"))
    printed = _sh(launcher, "--print")
    assert printed.returncode == 0, printed.stderr
    words = shlex.split(printed.stdout)
    assert words[words.index("--run-root") + 1] == str(short_root / "it's a root")
    # every other word needs no quoting and is printed bare, as shlex.quote
    # would leave it
    bare = [word for word in words if word != str(short_root / "it's a root")]
    assert all(f" {word}" in f" {printed.stdout}" for word in bare)
    assert "'" + str(short_root / "it") + "'" in printed.stdout


def test_the_launcher_resumes_if_and_only_if_the_root_holds_its_sentinel(short_root: Path) -> None:
    """deployment-runbook ss3: `--resume` iff `<root>/journal.jsonl`. A
    genesis is chosen for a root that does not exist, is empty, or holds
    only what the supervisor unit put there before the first start."""
    launcher = _configured(short_root, **_site(short_root))
    root = short_root / "runs" / "nb"
    assert "--resume" not in shlex.split(_sh(launcher, "--print").stdout)
    root.mkdir(parents=True)
    assert "--resume" not in shlex.split(_sh(launcher, "--print").stdout)
    for name in ("supervisor.lock", "supervisor.log", "supervisor.pid", "supervisor.sock", ".s.42"):
        (root / name).write_text("")
    genesis = _sh(launcher, "--print")
    assert genesis.returncode == 0, genesis.stderr
    assert "--resume" not in shlex.split(genesis.stdout)
    (root / "journal.jsonl").write_text("")
    words = shlex.split(_sh(launcher, "--print").stdout)
    assert words.count("--resume") == 1 and words[-1] == "--resume"


@pytest.mark.parametrize("mode", [("--print",), ("engine",)])
def test_a_root_with_contents_and_no_sentinel_is_never_a_genesis(
    short_root: Path, mode: tuple[str, ...]
) -> None:
    """A root that lost its sentinel, or holds something else, is neither
    empty nor resumable: the launcher refuses rather than start a fresh
    estate beside whatever is there."""
    launcher = _configured(short_root, **_site(short_root))
    root = short_root / "runs" / "nb"
    (root / "wal").mkdir(parents=True)
    refused = _sh(launcher, *mode)
    assert refused.returncode == 2
    assert "holds wal but no journal.jsonl" in refused.stderr
    assert refused.stdout == ""


def test_a_run_root_that_is_not_a_directory_is_refused(short_root: Path) -> None:
    launcher = _configured(short_root, **_site(short_root))
    (short_root / "runs").mkdir()
    (short_root / "runs" / "nb").write_text("")
    refused = _sh(launcher, "--print")
    assert refused.returncode == 2
    assert "is not a directory" in refused.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 directory")
@pytest.mark.parametrize("mode", [("--print",), ("engine",)])
def test_an_unreadable_run_root_is_refused_not_read_as_empty(
    short_root: Path, mode: tuple[str, ...]
) -> None:
    """A root this user cannot search hides its sentinel, and a missing
    sentinel would otherwise mean a genesis. Exit 2, with or without
    --print, and nothing started."""
    launcher = _configured(short_root, **_site(short_root))
    root = short_root / "runs" / "nb"
    root.mkdir(parents=True)
    (root / "journal.jsonl").write_text("")
    root.chmod(0o000)
    try:
        refused = _sh(launcher, *mode)
    finally:
        root.chmod(0o700)
    assert refused.returncode == 2
    assert "is not readable and searchable" in refused.stderr
    assert refused.stdout == ""


@pytest.mark.parametrize(
    "args",
    [("bogus",), ("engine", "extra"), ("--print", "supervisor-ready"), ("--print", "--print")],
)
def test_a_usage_error_is_a_configuration_refusal(args: tuple[str, ...]) -> None:
    refused = _sh(LAUNCH, *args)
    assert refused.returncode == 2
    assert "usage: dsl41-launch" in refused.stderr
    assert refused.stdout == ""


@pytest.mark.parametrize("mode", ["engine", "supervisor", "supervisor-ready"])
def test_a_missing_dsl41_is_a_configuration_refusal_not_a_crash(
    short_root: Path, mode: str
) -> None:
    """Exit 2, which the units never restart; a failed `exec` would exit
    127 and restart-loop an engine unit."""
    launcher = _configured(
        short_root, **{**_site(short_root), "DSL41": str(short_root / "absent" / "dsl41")}
    )
    refused = _sh(launcher, mode)
    assert refused.returncode == 2
    assert "is not executable" in refused.stderr


# ------------------------------------------------- the refusals it relies on


@pytest.mark.parametrize("damage", ["missing", "malformed"])
def test_a_configured_access_map_that_does_not_load_refuses_the_resume(
    short_root: Path, damage: str
) -> None:
    """The launcher passes `--access-map` on every start and relies on
    `dsl41 run` refusing a map that does not load (access-model ss4). This
    is what the CLI does today on the RESUME path the launcher takes after
    the first start: exit 2, naming the map, before the root is read or
    written. The genesis half is `test_access.py`'s."""
    _night(short_root)
    launcher = _configured(short_root, **_site(short_root))
    root = short_root / "runs" / "nb"
    root.mkdir(parents=True)
    (root / "journal.jsonl").write_text("")
    (short_root / "etc").mkdir(mode=0o700)  # the map's directory stands; the map does not
    if damage == "malformed":
        _write_map(short_root / "etc" / "access.toml", "format_version = \n")
    before = sorted(p.name for p in root.iterdir())
    refused = _sh(launcher, "engine")
    assert refused.returncode == 2, refused.stdout + refused.stderr
    assert "--resume" in refused.stderr  # the launcher's own line: it chose the resume
    assert "access map" in refused.stderr
    assert ("cannot open" if damage == "missing" else "not valid TOML") in refused.stderr
    assert sorted(p.name for p in root.iterdir()) == before
    assert (root / "journal.jsonl").read_text() == ""
    assert not (short_root / "runs" / "nb.anchor").exists()


# ------------------------------------------------------------- end to end


def _wait_up(socket: Path, proc: subprocess.Popen[str], timeout_s: float = 60.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        assert proc.poll() is None, f"engine exited {proc.returncode}"
        answered = subprocess.run(
            [str(DSL41), "query", "status", "--brief", "-S", str(socket)],
            capture_output=True,
            timeout=30,
        )
        if answered.returncode == 0:
            return
        time.sleep(0.3)
    raise AssertionError("the engine did not answer its control socket")


def _stop(proc: subprocess.Popen[str]) -> int:
    proc.send_signal(signal.SIGTERM)
    return proc.wait(timeout=60)


def test_the_launcher_starts_restarts_and_its_reattach_line_resumes(short_root: Path) -> None:
    """The launcher's line is one `dsl41 run` accepts; its second start is
    a resume of the same root; and the reattach line the detached engine
    prints on the way out is itself a working resume, access map included."""
    _night(short_root)
    _grant_me(short_root / "etc" / "access.toml")
    launcher = _configured(short_root, **_site(short_root))
    root = short_root / "runs" / "nb"
    socket = root / "control.sock"
    procs: list[subprocess.Popen[str]] = []

    def start(argv: list[str], log: str) -> subprocess.Popen[str]:
        with open(short_root / log, "w") as out:
            proc = subprocess.Popen(argv, stdout=out, stderr=subprocess.STDOUT, text=True)
        procs.append(proc)
        _wait_up(socket, proc)
        return proc

    try:
        first = start(["sh", str(launcher), "engine"], "first.log")
        assert _stop(first) == 0
        first_log = (short_root / "first.log").read_text()
        assert re.search(r"^dsl41-launch: engine: .*--access-map \S+$", first_log, re.M)
        assert "--resume" not in first_log.splitlines()[0]  # genesis

        second = start(["sh", str(launcher), "engine"], "second.log")
        assert _stop(second) == 0
        second_log = (short_root / "second.log").read_text()
        assert second_log.splitlines()[0].endswith(" --resume")

        reattach = re.search(r"^detached: reattach with `(.*)`$", second_log, re.M)
        assert reattach is not None, second_log
        words = shlex.split(reattach.group(1))
        # the second start already carried --resume, so the line is its own
        assert words == shlex.split(second_log.splitlines()[0].split(": ", 2)[2])
        assert words[words.index("--access-map") + 1] == str(short_root / "etc" / "access.toml")
        third = start(words, "third.log")
        assert _stop(third) == 0
    finally:
        for proc in procs:
            if proc.poll() is None:  # pragma: no cover -- only a failed assertion
                proc.kill()
                proc.wait(timeout=30)
        subprocess.run(
            [str(DSL41), "supervise", "shutdown", "--run-root", str(root)],
            capture_output=True,
            timeout=120,
        )


# ------------------------------------------------------------- the units


def _unit(name: str) -> dict[str, dict[str, list[str]]]:
    """A systemd unit file, read to the grammar it uses here: comments,
    blank lines, `[Section]` headers and `Key=Value` lines, nothing else."""
    sections: dict[str, dict[str, list[str]]] = {}
    current: dict[str, list[str]] | None = None
    for number, line in enumerate((DEPLOY / name).read_text().splitlines(), 1):
        if not line.strip() or line.startswith(("#", ";")):
            continue
        header = re.fullmatch(r"\[([A-Za-z]+)\]", line)
        if header:
            current = sections.setdefault(header.group(1), {})
            continue
        entry = re.fullmatch(r"([A-Za-z]+)=(.*)", line)
        assert entry and current is not None, f"{name}:{number}: {line!r}"
        current.setdefault(entry.group(1), []).append(entry.group(2))
    return sections


def test_the_units_take_the_documented_separate_service_shape() -> None:
    """deployment-runbook ss3, shape 1: the supervisor in its own unit, the
    engine requiring it, and restart policies that never retry exit 2 (a
    configuration refusal) or, for the engine, exit 3 (a sealed period)."""
    engine = _unit("dsl41-engine.service")
    supervisor = _unit("dsl41-supervisor.service")
    assert set(engine) == set(supervisor) == {"Unit", "Service", "Install"}

    assert engine["Unit"]["Requires"] == ["dsl41-supervisor.service"]
    assert engine["Unit"]["After"] == ["dsl41-supervisor.service"]
    assert "PartOf" not in supervisor["Unit"] and "BindsTo" not in supervisor["Unit"]
    assert engine["Service"]["ExecStart"] == ["/opt/dsl41/bin/dsl41-launch engine"]
    assert engine["Service"]["Restart"] == ["on-failure"]
    assert engine["Service"]["RestartPreventExitStatus"][0].split() == ["2", "3"]
    # shape 2's KillMode=process is exactly what shape 1 does not need
    assert engine["Service"].get("KillMode", ["control-group"]) == ["control-group"]

    # five starts in five minutes bound a repeating crash and a launcher
    # that cannot be executed; the supervisor keeps DL-210's unlimited
    # retry of an ownership refusal
    assert engine["Unit"]["StartLimitIntervalSec"] == ["300"]
    assert engine["Unit"]["StartLimitBurst"] == ["5"]
    assert supervisor["Unit"]["StartLimitIntervalSec"] == ["0"]
    assert supervisor["Service"]["ExecStart"] == ["/opt/dsl41/bin/dsl41-launch supervisor"]
    assert supervisor["Service"]["ExecStartPost"] == [
        "/opt/dsl41/bin/dsl41-launch supervisor-ready"
    ]
    assert supervisor["Service"]["Restart"] == ["always"]
    assert supervisor["Service"]["RestartPreventExitStatus"] == ["2"]
    assert supervisor["Service"]["KillMode"] == ["control-group"]
    # supervisor-protocol ss5's shutdown waits: the missing-spawn.json wait,
    # the longest grace plus 2 s, and the 2 s output drain
    (stop,) = supervisor["Service"]["TimeoutStopSec"]
    assert int(stop) >= 5 + CMD_GRACE_S + 2 + 2


def test_the_units_repeat_the_run_root_only_for_its_mount() -> None:
    """The launcher is the one place that names the command. Each unit
    repeats the run root once, in `RequiresMountsFor=`, so an unmounted
    file system is never started on and read as an empty root; this test
    holds that copy equal to the launcher's."""
    (configured,) = re.findall(r"^RUN_ROOT=(\S+)$", LAUNCH.read_text(), re.M)
    for name in ("dsl41-engine.service", "dsl41-supervisor.service"):
        unit = _unit(name)
        assert unit["Unit"]["RequiresMountsFor"] == [configured], name
        for section in unit.values():
            for key, values in section.items():
                if key != "RequiresMountsFor":
                    assert not any("/srv/" in value for value in values), (name, key)


def _drill_vars(text: str) -> dict[str, str]:
    """The simple `NAME=value` assignments at the top of drill-lib.sh,
    with `$NAME` references (e.g. `ESTATE=$NIGHTBANK/estate/small`)
    resolved against earlier assignments in the same dict."""
    values: dict[str, str] = {}
    for name, raw in re.findall(r"^(\w+)=(\S+)$", text, re.M):
        for ref, resolved in values.items():
            raw = raw.replace(f"${ref}", resolved)
        values[name] = raw
    return values


def test_the_drill_names_the_same_paths_and_file_order_as_the_launcher() -> None:
    """drill-lib.sh installs at the paths dsl41-launch runs on and repeats
    the estate file order to feed the seal step's `--next` list; both must
    name the same values, the way the units' `RequiresMountsFor=` is held
    equal to the launcher's `RUN_ROOT`."""
    launcher_text = LAUNCH.read_text()
    drill_text = DRILL_LIB.read_text()
    drill_values = _drill_vars(drill_text)
    for launcher_name, drill_name in (
        ("DSL41", "DSL41"),
        ("RUN_ROOT", "ROOT"),
        ("ACCESS_MAP", "MAP"),
        ("ESTATE", "ESTATE"),
        ("PROPERTIES", "PROPERTIES"),
    ):
        (launcher_value,) = re.findall(rf"^{launcher_name}=(\S+)$", launcher_text, re.M)
        assert drill_values[drill_name] == launcher_value, (launcher_name, drill_name)

    launcher_order = re.findall(r'"\$ESTATE/(\w+)\.jil"', launcher_text)
    (files_literal,) = re.findall(r"^FILES=\(([^)]*)\)$", drill_text, re.M)
    assert files_literal.split() == launcher_order
