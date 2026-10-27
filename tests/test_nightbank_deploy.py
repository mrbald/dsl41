"""DL-218: the nightbank deployment example -- `dsl41-launch` and the two
systemd units beside it (examples/nightbank/deploy/).

The launcher is the one reviewed place that holds the run root and the
whole `dsl41 run` line; the estate's words in it come from the active
release's manifest (DL-407). These tests run the shipped script. Where a
test needs a root or a release it can write, it copies the script and
replaces only the values in its CONFIGURATION block, so the logic under
test is the shipped logic. The service-level claims -- systemd starting, restarting and
refusing -- are the drill's (`.github/workflows/service-drill.yml`), not
these tests'.
"""

from __future__ import annotations

import os
import pwd
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dsl41.estate_manifest import load_manifest
from dsl41.period import CMD_GRACE_S

from test_nightbank_example import NB, _launcher

DEPLOY = NB / "deploy"
LAUNCH = DEPLOY / "dsl41-launch"
DRILL_LIB = DEPLOY / "drill-lib.sh"
DRILL_STEPS = DEPLOY / "drill-steps.sh"
DRILL_WORKFLOW = NB.parents[1] / ".github" / "workflows" / "service-drill.yml"
DRILL_LOCAL = DEPLOY / "drill-local.sh"
WARGAME = NB.parents[1] / "scripts" / "wargame.sh"
DSL41 = Path(sys.executable).with_name("dsl41")
ME = pwd.getpwuid(os.geteuid()).pw_name
#: the order the estate's manifest names the files in; it is part of the
#: catalog address, so a reordering is a new estate and this list pins it
ORDER = ("amer.jil", "apac.jil", "calendars.jil", "emea.jil", "global.jil", "infra.jil")
SHIPPED_ROOT = "/srv/dsl41/runs/nightbank-01"
#: the release name the tests' releases take
RELEASE = "r1"


def _sh(launcher: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["sh", str(launcher), *args], capture_output=True, text=True, timeout=120)


def _releases(base: Path, estate: Path = NB / "estate" / "small") -> dict[str, str]:
    """A releases directory under `base` holding a copy of `estate` as release r1,
    and the ACTIVE file naming it: the launcher's values for both."""
    releases = base / "releases"
    shutil.copytree(estate, releases / RELEASE)
    (base / "active").write_text(f"{RELEASE}\n")
    return {"ESTATE_RELEASES": str(releases), "ACTIVE": str(base / "active")}


def _site(base: Path) -> dict[str, str]:
    return {
        "DSL41": str(DSL41),
        "RUN_ROOT": str(base / "runs" / "nb"),
        "ESTATE_ANCHOR": str(base / "runs" / "nb.anchor"),
        "ACCESS_MAP": str(base / "etc" / "access.toml"),
        **_releases(base),
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


def test_the_shipped_launcher_prints_the_whole_ordered_line(tmp_path: Path) -> None:
    if Path(SHIPPED_ROOT, "journal.jsonl").exists():  # pragma: no cover -- a real host
        pytest.skip("this host holds the example's run root")
    # this build's dsl41 in place of the shipped path, so the --supervisor probe finds it;
    # a release of the small estate in place of the shipped releases directory
    releases = _releases(tmp_path)
    printed = _sh(_configured(tmp_path, DSL41=str(DSL41), **releases), "--print")
    assert printed.returncode == 0, printed.stderr
    estate = f"{releases['ESTATE_RELEASES']}/{RELEASE}"
    assert shlex.split(printed.stdout) == [
        str(DSL41),
        "run",
        *(f"{estate}/{name}" for name in ORDER),
        # the manifest's timezone; the site's properties follow the manifest's words
        "--timezone",
        "UTC",
        "-p",
        "/srv/dsl41/nightbank/night/night.properties",
        "--run-root",
        SHIPPED_ROOT,
        "--estate-anchor",
        f"{SHIPPED_ROOT}.anchor",
        "--detached",
        # shape 1: the engine attaches to the supervisor unit and never forks its own (DL-396)
        "--supervisor",
        "attach",
        "--as-machine",
        "localhost",
        "--machine-policy",
        "strict",
        "--access-map",
        "/etc/dsl41/nightbank-access.toml",
    ]
    assert printed.stdout.count("\n") == 1  # one line, nothing else on stdout


@pytest.mark.parametrize("estate", ["small", "bank"])
def test_each_estates_manifest_names_its_whole_file_set_in_the_pinned_order(estate: str) -> None:
    """DL-407: the manifest is the one list. It names every JIL file of the estate, in
    the order the catalog address pins, with the launcher's environment declared."""
    directory = NB / "estate" / estate
    manifest = load_manifest(directory / "estate.toml")
    assert tuple(manifest.files) == ORDER
    assert sorted(ORDER) == sorted(p.name for p in directory.glob("*.jil"))
    assert manifest.timezone == "UTC" and manifest.properties == []
    (environment,) = re.findall(r"^ENVIRONMENT=(\S+)$", LAUNCH.read_text(), re.M)
    assert environment in manifest.environments


def _release(base: Path, manifest: str, layers: tuple[str, ...] = ()) -> dict[str, str]:
    """`_releases` with the release's estate.toml replaced by `manifest`, and an empty
    properties file for each name in `layers`."""
    releases = _releases(base)
    release = Path(releases["ESTATE_RELEASES"], RELEASE)
    (release / "estate.toml").write_text(manifest)
    for name in layers:
        (release / name).write_text("")
    return releases


def test_the_printed_line_takes_the_manifests_order_and_ends_with_the_sites_layer(
    tmp_path: Path,
) -> None:
    """DL-407: --print shows the manifest's files in the manifest's order, whatever it
    is, then its layers and the chosen environment's, then the site's PROPERTIES as the
    last -p, so the site's values win."""
    reverse = list(reversed(ORDER))
    releases = _release(
        tmp_path,
        f"name = 'x'\nfiles = {reverse!r}\nproperties = ['app.properties']\n"
        "timezone = 'Europe/Zurich'\n[environments.wargame]\nproperties = ['host.properties']\n"
        "[environments.other]\nproperties = []\n",
        ("app.properties", "host.properties"),
    )
    launcher = _configured(tmp_path, DSL41=str(DSL41), RUN_ROOT=str(tmp_path / "nb"), **releases)
    printed = _sh(launcher, "--print")
    assert printed.returncode == 0, printed.stderr
    words = shlex.split(printed.stdout)
    release = f"{releases['ESTATE_RELEASES']}/{RELEASE}"
    assert words[2 : words.index("--run-root")] == [
        *(f"{release}/{name}" for name in reverse),
        "-p",
        f"{release}/app.properties",
        "-p",
        f"{release}/host.properties",
        "--timezone",
        "Europe/Zurich",
        "-p",
        "/srv/dsl41/nightbank/night/night.properties",
    ]


@pytest.mark.parametrize(
    ("active", "says"),
    [
        (None, "is missing: no release is active"),
        ("", "is empty: no release is active"),
        ("\n", "is empty: no release is active"),
        ("r1\nr2\n", "holds more than one line: name one release"),
        ("../r1\n", "which is not one directory name"),
        ("..\n", "which is not one directory name"),
        ("r2\n", "holds no estate.toml: not a release"),
        ("symlinked ACTIVE", "is a symlink: name the release in a plain file"),
        ("symlinked release", "is a symlink: the opener must name the paths the seal named"),
    ],
)
def test_the_launcher_refuses_an_active_file_that_names_no_one_release(
    tmp_path: Path, active: str | None, says: str
) -> None:
    """DL-407: ACTIVE is a plain file holding one line, one directory name under
    ESTATE_RELEASES that is no symlink and holds estate.toml. Anything else is the
    launcher's exit 2, before dsl41 runs."""
    releases = _releases(tmp_path)
    path = Path(releases["ACTIVE"])
    if active is None:
        path.unlink()
    elif active == "symlinked ACTIVE":
        (tmp_path / "selector").write_text(f"{RELEASE}\n")
        path.unlink()
        path.symlink_to(tmp_path / "selector")
    elif active == "symlinked release":
        Path(releases["ESTATE_RELEASES"], "current").symlink_to(RELEASE)
        path.write_text("current\n")
    else:
        path.write_text(active)
    launcher = _configured(tmp_path, DSL41=str(DSL41), RUN_ROOT=str(tmp_path / "nb"), **releases)
    for mode in ("--print", "engine"):
        refused = _sh(launcher, mode)
        assert refused.returncode == 2, refused.stdout + refused.stderr
        assert says in refused.stderr and refused.stdout == ""
        assert not (tmp_path / "nb").exists()


def test_a_manifest_estate_args_refuses_is_the_launchers_refusal(tmp_path: Path) -> None:
    """DL-407: `estate args` exit 2 (here: the environment the launcher names is not
    declared) refuses the start with exit 2, and the verb's sentence is in the log."""
    releases = _release(tmp_path, "name = 'x'\nfiles = ['amer.jil']\n[environments.prod]\n")
    launcher = _configured(tmp_path, DSL41=str(DSL41), RUN_ROOT=str(tmp_path / "nb"), **releases)
    refused = _sh(launcher, "--print")
    assert refused.returncode == 2 and refused.stdout == ""
    assert "no environment 'wargame'; the manifest declares prod" in refused.stderr
    assert "estate args exited 2 for " in refused.stderr


def test_the_declared_workload_ceiling_fits_the_supervisor_units_stop_timeout() -> None:
    """The launcher declares the workload the units were sized for (DL-401's bound):
    15 + 5 + grace + 2 + 8 per live run + 10 seconds fits TimeoutStopSec, the grace is
    at least the default, and the small estate the drill runs is inside the ceiling."""
    text = LAUNCH.read_text()
    (jobs,) = map(int, re.findall(r"^MAX_JOBS=(\d+)$", text, re.M))
    (grace,) = map(int, re.findall(r"^MAX_GRACE_S=(\d+)$", text, re.M))
    (stop,) = _unit("dsl41-supervisor.service")["Service"]["TimeoutStopSec"]
    assert 15 + 5 + grace + 2 + 8 * jobs + 10 <= int(stop)
    assert grace >= CMD_GRACE_S
    small = NB / "estate" / "small"
    assert sum(path.read_text().count("insert_job:") for path in small.glob("*.jil")) <= jobs


def _fake_dsl41(base: Path, body: str) -> Path:
    """A dsl41 that hands `estate args` to this build's and runs `body` for
    anything else."""
    fake = base / "fake-dsl41"
    fake.write_text(
        "#!/bin/sh\n"
        f'if [ "$1 $2" = "estate args" ]; then exec {shlex.quote(str(DSL41))} "$@"; fi\n' + body
    )
    fake.chmod(0o755)
    return fake


@pytest.mark.parametrize(
    ("probe", "says"),
    [
        (
            2,
            "this launcher needs dsl41 1.13.0 or later;"
            " a rollback below it reinstalls the site package qualified with that release",
        ),
        (1, "estate args --help exited 1: cannot tell whether it reads a manifest"),
    ],
)
def test_the_launcher_refuses_a_dsl41_without_estate_args(
    tmp_path: Path, probe: int, says: str
) -> None:
    """DL-407: the launcher reads the manifest through `estate args`. A dsl41 whose
    parse of `estate args --help` exits 2 has no such verb, and any other failure
    cannot tell: both refuse with exit 2, before the release is read (DL-402's probe).
    ACTIVE is missing here, so a refusal that read the release would say so instead."""
    fake = tmp_path / "fake-dsl41"
    fake.write_text(
        f'#!/bin/sh\n[ "$*" = "estate args --help" ] && exit {probe}\n'
        '[ "$*" = "run --supervisor attach --help" ] && exit 2\nexit 9\n'
    )
    fake.chmod(0o755)
    releases = _releases(tmp_path)
    Path(releases["ACTIVE"]).unlink()
    launcher = _configured(tmp_path, DSL41=str(fake), RUN_ROOT=str(tmp_path / "nb"), **releases)
    for mode in ("--print", "engine"):
        refused = _sh(launcher, mode)
        assert refused.returncode == 2 and refused.stdout == ""
        assert refused.stderr.endswith(f"{says}\n"), refused.stderr


@pytest.mark.parametrize("probe", [0, 2, 1])
def test_the_launcher_passes_supervisor_attach_only_to_a_dsl41_that_accepts_it(
    tmp_path: Path, probe: int
) -> None:
    """DL-402: the parse of `run --supervisor attach --help` decides by exit
    code. 0 passes the option, 2 (a release before DL-396: no such option)
    omits it, and any other code refuses instead of falling back to spawn."""
    fake = _fake_dsl41(
        tmp_path,
        'echo "probe: $*"\n'
        'echo "probe: $*" >&2\n'
        '[ "$*" = "run --supervisor attach --help" ] || exit 9\n'
        f"exit {probe}\n",
    )
    launcher = _configured(
        tmp_path, DSL41=str(fake), RUN_ROOT=str(tmp_path / "nb"), **_releases(tmp_path)
    )
    printed = _sh(launcher, "--print")
    if probe == 1:
        assert printed.returncode == 2 and printed.stdout == ""
        assert printed.stderr.splitlines() == [
            f"dsl41-launch: {fake} run --supervisor attach --help exited 1:"
            " cannot tell attach from spawn"
        ]
        return
    assert printed.returncode == 0, printed.stderr
    assert printed.stderr == ""  # the probe's output is discarded
    words = shlex.split(printed.stdout)
    if probe == 0:
        at = words.index("--supervisor")
        assert words[at - 1 : at + 2] == ["--detached", "--supervisor", "attach"]
    else:
        assert "--supervisor" not in words and "attach" not in words
        assert words[words.index("--detached") + 1] == "--as-machine"


def test_the_shipped_launcher_prints_the_supervisor_line() -> None:
    printed = _sh(LAUNCH, "--print", "supervisor")
    assert printed.returncode == 0, printed.stderr
    assert shlex.split(printed.stdout) == [
        "/opt/dsl41/.venv/bin/dsl41",
        "supervise",
        "start",
        "--run-root",
        SHIPPED_ROOT,
    ]


def test_the_printed_line_is_shell_quoted(short_root: Path) -> None:
    # this build's dsl41, so the --supervisor probe has a binary to run (DL-402)
    launcher = _configured(
        short_root,
        DSL41=str(DSL41),
        RUN_ROOT=str(short_root / "it's a root"),
        **_releases(short_root),
    )
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
    for name in (
        "leader.lock",
        "supervisor.lock",
        "supervisor.log",
        "supervisor.pid",
        "supervisor.sock",
        ".s.42",
    ):
        (root / name).write_text("")
    genesis = _sh(launcher, "--print")
    assert genesis.returncode == 0, genesis.stderr
    assert "--resume" not in shlex.split(genesis.stdout)
    (root / "journal.jsonl").write_text("")
    words = shlex.split(_sh(launcher, "--print").stdout)
    assert words.count("--resume") == 1 and words[-1] == "--resume"


def test_the_launcher_reads_dsl41s_genesis_staging_as_residue(short_root: Path) -> None:
    """period-model ss1.1: dsl41 stages `catalogs/` and `periods/.staging/`
    before its genesis and tolerates both there, so a start refused before
    the sentinel (DL-396: no supervisor answered) is followed by a genesis,
    not by the launcher's exit 2. Anything else under `periods/` refuses."""
    launcher = _configured(short_root, **_site(short_root))
    root = short_root / "runs" / "nb"
    (root / "catalogs" / "sha256-x").mkdir(parents=True)
    (root / "periods" / ".staging" / "sha256-y").mkdir(parents=True)
    (root / "leader.lock").write_text("")
    genesis = _sh(launcher, "--print")
    assert genesis.returncode == 0, genesis.stderr
    assert "--resume" not in shlex.split(genesis.stdout)
    (root / "periods" / "000001").mkdir()
    refused = _sh(launcher, "--print")
    assert refused.returncode == 2
    assert "holds periods/000001 but no journal.jsonl" in refused.stderr


@pytest.mark.parametrize("kind", ["nonempty", "symlink", "directory"])
def test_the_launcher_refuses_ambiguous_bootstrap_lock(short_root: Path, kind: str) -> None:
    """DL-390: only a cold empty regular lock can precede genesis."""
    launcher = _configured(short_root, **_site(short_root))
    root = short_root / "runs" / "nb"
    root.mkdir(parents=True)
    lock = root / "leader.lock"
    if kind == "nonempty":
        lock.write_text('{"pid": 123}')
    elif kind == "directory":
        lock.mkdir()
    else:
        target = short_root / "lock-target"
        target.touch()
        lock.symlink_to(target)
    refused = _sh(launcher, "--print")
    assert refused.returncode == 2
    assert "not an empty provisioned lock" in refused.stderr
    assert not (root / "journal.jsonl").exists()


def test_the_open_trigger_makes_the_engine_mode_open_from_the_anchor(short_root: Path) -> None:
    """DL-266: `<RUN_ROOT>.open-from` makes the engine mode pass
    `--open-from ESTATE_ANCHOR` instead of choosing from the root, for a
    fresh root and for a root that already holds its sentinel (the
    identical retry of a roll). --print leaves the trigger in place."""
    site = _site(short_root)
    launcher = _configured(short_root, **site)
    root = short_root / "runs" / "nb"
    root.mkdir(parents=True)
    (root / "supervisor.sock").write_text("")
    trigger = Path(site["RUN_ROOT"] + ".open-from")
    trigger.write_text("")
    for sentinel in (False, True):
        if sentinel:
            (root / "journal.jsonl").write_text("")
        printed = _sh(launcher, "--print")
        assert printed.returncode == 0, printed.stderr
        words = shlex.split(printed.stdout)
        assert words[-2:] == ["--open-from", site["ESTATE_ANCHOR"]]
        assert "--resume" not in words
        assert trigger.exists()


@pytest.mark.parametrize("mode", [("--print",), ("engine",)])
def test_a_genesis_against_an_existing_anchor_is_refused_and_names_the_trigger(
    short_root: Path, mode: tuple[str, ...]
) -> None:
    """DL-266: with no trigger and no sentinel, an anchor that already holds
    a lineage refuses the start with exit 2, before dsl41 runs. A genesis
    there would write a foreign sentinel into the root first, and that
    blocks the identical retry of a roll whose opener died early."""
    site = _site(short_root)
    launcher = _configured(short_root, **site)
    root = Path(site["RUN_ROOT"])
    root.mkdir(parents=True)
    (root / "supervisor.sock").write_text("")
    anchor = Path(site["ESTATE_ANCHOR"])
    anchor.mkdir()
    (anchor / "anchor.json").write_text("{}")
    refused = _sh(launcher, *mode)
    assert refused.returncode == 2
    assert "a genesis would fork it" in refused.stderr
    assert f"create {site['RUN_ROOT']}.open-from" in refused.stderr
    assert refused.stdout == ""
    assert sorted(path.name for path in root.iterdir()) == ["supervisor.sock"]


def test_a_genesis_with_an_anchor_directory_but_no_anchor_proceeds(short_root: Path) -> None:
    """The non-triggering twin: no anchor.json in ESTATE_ANCHOR, so the
    launcher still chooses a genesis."""
    site = _site(short_root)
    launcher = _configured(short_root, **site)
    Path(site["ESTATE_ANCHOR"]).mkdir(parents=True)
    printed = _sh(launcher, "--print")
    assert printed.returncode == 0, printed.stderr
    words = shlex.split(printed.stdout)
    assert "--resume" not in words and "--open-from" not in words


def test_the_engine_mode_removes_the_open_trigger_before_it_runs_dsl41(
    short_root: Path,
) -> None:
    """DL-266: one shot. The engine mode removes the trigger before it execs
    dsl41, so the unit's next start resumes the opened root instead of
    opening it again."""
    seen = short_root / "argv"
    fake = _fake_dsl41(short_root, f"printf '%s\\n' \"$@\" >{shlex.quote(str(seen))}\n")
    site = {**_site(short_root), "DSL41": str(fake)}
    launcher = _configured(short_root, **site)
    trigger = Path(site["RUN_ROOT"] + ".open-from")
    (short_root / "runs").mkdir()
    trigger.write_text("")
    opened = _sh(launcher, "engine")
    assert opened.returncode == 0, opened.stderr
    argv = seen.read_text().splitlines()
    assert argv[-2:] == ["--open-from", site["ESTATE_ANCHOR"]]
    assert not trigger.exists()
    # what the open leaves behind: a root with its sentinel
    Path(site["RUN_ROOT"]).mkdir()
    Path(site["RUN_ROOT"], "journal.jsonl").write_text("")
    restarted = _sh(launcher, "engine")
    assert restarted.returncode == 0, restarted.stderr
    argv = seen.read_text().splitlines()
    assert argv[-1] == "--resume" and "--open-from" not in argv


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


def test_an_engine_start_with_no_supervisor_exits_1_and_the_retry_is_a_genesis(
    short_root: Path,
) -> None:
    """DL-396 under the shipped launcher: the engine mode attaches, so a start
    while no supervisor answers forks nothing and exits 1, which the engine
    unit retries. Once the supervisor mode is up, the next start is the same
    genesis and serves its control socket."""
    _night(short_root)
    _grant_me(short_root / "etc" / "access.toml")
    launcher = _configured(short_root, **_site(short_root))
    root = short_root / "runs" / "nb"
    socket = root / "control.sock"
    first = _sh(launcher, "engine")
    assert first.returncode == 1, first.stdout + first.stderr
    assert f"no supervisor answers on {root}" in first.stderr
    assert not (root / "supervisor.pid").exists() and not (root / "supervisor.sock").exists()
    assert not (root / "journal.jsonl").exists()
    supervisor: subprocess.Popen[bytes] | None = None
    engine: subprocess.Popen[str] | None = None
    try:
        supervisor = subprocess.Popen(
            ["sh", str(launcher), "supervisor"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        ready = _sh(launcher, "supervisor-ready")
        assert ready.returncode == 0, ready.stderr
        with open(short_root / "retry.log", "w") as out:
            engine = subprocess.Popen(
                ["sh", str(launcher), "engine"], stdout=out, stderr=subprocess.STDOUT, text=True
            )
        _wait_up(socket, engine)
        line = (short_root / "retry.log").read_text().splitlines()[0]
        assert line.startswith("dsl41-launch: engine: ") and "--resume" not in line, line
        assert _stop(engine) == 0
    finally:
        if engine is not None and engine.poll() is None:  # pragma: no cover -- a failure
            engine.kill()
            engine.wait(timeout=30)
        if supervisor is not None:
            from test_runner_supervisor import shutdown_unmanaged_supervisor

            try:
                shutdown_unmanaged_supervisor(
                    root, controller_id="retry-fixture", process=supervisor
                )
            finally:
                if supervisor.poll() is None:  # pragma: no cover -- a failure
                    supervisor.kill()
                    supervisor.wait(timeout=30)


def test_the_launcher_starts_restarts_and_its_reattach_line_resumes(short_root: Path) -> None:
    """The launcher's line is one `dsl41 run` accepts; its second start is
    a resume of the same root; and the reattach line the detached engine
    prints on the way out is itself a working resume, access map included.
    The supervisor mode runs first, as shape 1's supervisor unit does: the
    engine mode attaches to it (DL-396)."""
    _night(short_root)
    _grant_me(short_root / "etc" / "access.toml")
    launcher = _configured(short_root, **_site(short_root))
    root = short_root / "runs" / "nb"
    socket = root / "control.sock"
    procs: list[subprocess.Popen[str]] = []
    supervisor = subprocess.Popen(
        ["sh", str(launcher), "supervisor"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def start(argv: list[str], log: str) -> subprocess.Popen[str]:
        with open(short_root / log, "w") as out:
            proc = subprocess.Popen(argv, stdout=out, stderr=subprocess.STDOUT, text=True)
        procs.append(proc)
        _wait_up(socket, proc)
        return proc

    try:
        ready = _sh(launcher, "supervisor-ready")
        assert ready.returncode == 0, ready.stderr
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
        from test_runner_supervisor import shutdown_unmanaged_supervisor

        try:
            shutdown_unmanaged_supervisor(
                root, controller_id="launcher-fixture", process=supervisor
            )
        finally:
            if supervisor.poll() is None:  # pragma: no cover -- only a failed assertion
                supervisor.kill()
                supervisor.wait(timeout=30)


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
    # 6: a time-half stop and 7: a statechart error stay down until a fixed
    # build is deployed
    assert engine["Service"]["RestartPreventExitStatus"][0].split() == ["2", "3", "6", "7"]
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
    # 7: a statechart error stops the supervisor too, until a fixed build
    assert supervisor["Service"]["RestartPreventExitStatus"][0].split() == ["2", "7", "8"]
    assert supervisor["Service"]["SuccessExitStatus"] == ["8"]
    assert supervisor["Unit"]["RefuseManualStop"] == ["yes"]
    assert supervisor["Service"]["KillMode"] == ["control-group"]
    # supervisor-protocol ss5's shutdown waits: the missing-spawn.json wait,
    # the longest grace plus 2 s, and the 2 s output drain
    (stop,) = supervisor["Service"]["TimeoutStopSec"]
    assert int(stop) >= 5 + CMD_GRACE_S + 2 + 2
    # DL-401: a host stop is held through forced maintenance, whose bound_s for the small
    # estate with every job live is 15 + 5 + grace + 2 + 8 per run + 10
    small = Path(__file__).resolve().parent.parent / "examples/nightbank/estate/small"
    jobs = sum(path.read_text().count("insert_job:") for path in small.glob("*.jil"))
    assert int(stop) >= 15 + 5 + CMD_GRACE_S + 2 + 8 * jobs + 10


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
    """The simple `NAME=value` assignments at the top of drill-lib.sh or
    the launcher, with `$NAME` references (e.g. `DSL41=$PREFIX/.venv/bin/dsl41`)
    resolved against earlier assignments in the same dict."""
    values: dict[str, str] = {}
    for name, raw in re.findall(r"^(\w+)=(\S+)$", text, re.M):
        for ref, resolved in values.items():
            raw = raw.replace(f"${ref}", resolved)
        values[name] = raw
    return values


def test_the_drill_names_the_same_paths_and_file_order_as_the_launcher() -> None:
    """drill-lib.sh installs at the paths dsl41-launch runs on, and both
    must name the same values, the way the units' `RequiresMountsFor=` is
    held equal to the launcher's `RUN_ROOT`. Neither names an estate file:
    the file order of the seal's `--next` list, like the launcher's line,
    is the release manifest's (DL-407)."""
    launcher_text = LAUNCH.read_text()
    drill_text = DRILL_LIB.read_text()
    launcher_values = _drill_vars(launcher_text)
    drill_values = _drill_vars(drill_text)
    for launcher_name, drill_name in (
        ("PREFIX", "PREFIX"),
        ("DSL41", "DSL41"),
        ("RUN_ROOT", "ROOT"),
        ("ESTATE_ANCHOR", "ANCHOR"),
        ("ACCESS_MAP", "MAP"),
        ("ESTATE_RELEASES", "ESTATE_RELEASES"),
        ("ACTIVE", "ACTIVE"),
        ("ENVIRONMENT", "ENVIRONMENT"),
        ("PROPERTIES", "PROPERTIES"),
    ):
        assert drill_values[drill_name] == launcher_values[launcher_name], launcher_name

    # the upgrade steps flip the symlink the launcher's dsl41 runs through
    assert drill_values["DSL41"] == drill_values["VENV"] + "/bin/dsl41"
    # the drill's release is one directory under the launcher's releases
    assert drill_values["ESTATE"] == f"{drill_values['ESTATE_RELEASES']}/{drill_values['RELEASE']}"
    for text in (launcher_text, drill_text):
        assert ".jil" not in text and not re.search(r"^FILES=", text, re.M)
    assert tuple(load_manifest(NB / "estate" / "small" / "estate.toml").files) == ORDER


COMPAT = DEPLOY / "compat" / "dsl41-launch-pre-manifest"


def test_the_drill_runs_v1_7_0_under_the_pre_manifest_launcher_and_restores_the_current() -> None:
    """DL-407: the current launcher refuses a dsl41 before 1.13.0, and a site that rolls
    back below it reinstalls that generation's launcher. The fixture is that launcher:
    it reads no manifest and names one release directory. The drill installs it for the
    v1.7.0 steps only, and the steps after them run the current launcher again."""
    fixture = COMPAT.read_text()
    assert "estate args --help" not in fixture and '"$DSL41" estate args' not in fixture
    assert "DL-407" in fixture.split("\n\n", 1)[0]
    assert re.findall(r"^ESTATE=(\S+)$", fixture, re.M) == [
        f"{_drill_vars(DRILL_LIB.read_text())['ESTATE_RELEASES']}/small-r1"
    ]
    assert os.access(COMPAT, os.X_OK)
    assert "estate args --help" in LAUNCH.read_text()
    steps = subprocess.run(
        ["bash", str(DRILL_STEPS), "--list"], capture_output=True, text=True, timeout=60, check=True
    ).stdout.split()
    text = DRILL_STEPS.read_text()
    calls: list[tuple[str, str]] = []
    for step in steps:
        body = re.search(rf"^step_{step.replace('-', '_')}\(\) \{{.*?^\}}$", text, re.M | re.S)
        assert body is not None, step
        calls += [
            (step, call)
            for call in re.findall(
                r"^\s+(install_old_launcher|restore_launcher)$", body.group(), re.M
            )
        ]
    assert calls == [
        ("upgrade-old-release", "install_old_launcher"),
        ("upgrade-state-machine", "restore_launcher"),
        ("upgrade-state-machine-rollback", "install_old_launcher"),
        ("upgrade-state-machine-rollback", "restore_launcher"),
    ]
    # each install is for a flip to the old venv, each restore for a flip to this build
    for step, call in calls:
        body = re.search(rf"^step_{step.replace('-', '_')}\(\) \{{.*?^\}}$", text, re.M | re.S)
        assert body is not None
        before = body.group()[: body.group().index(call)]
        flips = re.findall(r'flip "\$(\w+)"', before)
        assert flips[-1] == ("OLD_VENV" if call == "install_old_launcher" else "BUILD_A"), step


def test_the_drill_writes_active_as_a_plain_file_and_never_links_it() -> None:
    """DL-407: a symlink selector would change the hashed path strings. The
    drill writes ACTIVE only through `activate`, which replaces a plain file
    by a rename and checks it is no symlink; no drill script links it."""
    texts = {
        path.name: path.read_text()
        for path in (DRILL_LIB, DRILL_STEPS, DEPLOY / "offline-scenarios.sh")
    }
    body = re.search(r"^activate\(\) \{.*?^\}$", texts["drill-lib.sh"], re.M | re.S)
    assert body is not None
    assert 'sudo mv -T "$ACTIVE.next" "$ACTIVE"' in body.group()
    assert 'sudo test -L "$ACTIVE"' in body.group()
    for name, text in texts.items():
        assert not re.search(r"\bln\b[^\n]*\$ACTIVE", text), name
        outside = text.replace(body.group(), "") if name == "drill-lib.sh" else text
        assert not re.search(r"(?:>|tee|mv|cp)[^\n]*\$ACTIVE\b", outside), name
    install = re.search(r"^step_install\(\).*?^\}$", texts["drill-steps.sh"], re.M | re.S)
    assert install is not None and 'activate "$RELEASE"' in install.group()


def _workflow_runs(text: str) -> list[str]:
    """Every step's `run:` value in a job's steps, in order, whether the key
    opens the step (`- run: ...`) or follows its `name:`. A block scalar
    is returned as its indicator (`|` or `>`), so a step body shows up as
    a value that is not a one-line call."""
    steps = text[text.index("steps:") :]
    return re.findall(r"^[ \t]+(?:-[ \t]+)?run:[ \t]*(.*?)[ \t]*$", steps, re.M)


def test_the_workflow_run_reader_sees_every_step_form() -> None:
    text = (
        "    steps:\n"
        "      - name: a\n"
        "        run: bash one.sh\n"
        "      - run: rm -rf /srv/dsl41\n"
        "      - name: b\n"
        "        run: |\n"
        "          echo body\n"
    )
    assert _workflow_runs(text) == ["bash one.sh", "rm -rf /srv/dsl41", "|"]


def test_the_workflow_runs_the_drill_steps_in_their_order() -> None:
    """DL-266: the workflow and drill-local.sh run the same step bodies,
    which live in drill-steps.sh. The workflow names each step once, in the
    order `drill-steps.sh --list` gives, then the diagnostics step, which
    runs whatever came before; it holds no step body of its own."""
    listed = subprocess.run(
        ["bash", str(DRILL_STEPS), "--list"], capture_output=True, text=True, timeout=60, check=True
    ).stdout.split()
    workflow = DRILL_WORKFLOW.read_text().split("\n  offline:", 1)[0]
    call = "bash examples/nightbank/deploy/drill-steps.sh"
    assert _workflow_runs(workflow) == [f"{call} {step}" for step in listed] + [
        f"{call} diagnostics"
    ]
    assert re.search(rf"^\s+if: always\(\)\n\s+run: {call} diagnostics$", workflow, re.M)


def _drill_local_step_users(text: str) -> list[str | None]:
    """The `--user` of each `podman exec` in drill-local.sh that runs
    drill-steps.sh, with line continuations joined; None for an exec with
    no `--user`, which runs as the container's root."""
    joined = text.replace("\\\n", " ")
    users: list[str | None] = []
    for line in joined.splitlines():
        if "podman exec" in line and "drill-steps.sh" in line:
            found = re.search(r"--user[ =]+(\S+)", line)
            users.append(found.group(1) if found else None)
    return users


def test_drill_local_runs_every_step_as_an_unprivileged_sudoer() -> None:
    """DL-271: drill-local.sh runs each step and the diagnostics as an
    unprivileged user with passwordless sudo, as GitHub's runner does, so a
    step that reads a root-only path without sudo fails locally too."""
    text = DRILL_LOCAL.read_text()
    assignments = re.findall(r"^\s*user=(\S+)$", text, re.M)
    assert len(assignments) == 1 and assignments[0] != "root"
    assert "useradd --create-home --shell /bin/bash $user" in text
    assert "echo '$user ALL=(ALL) NOPASSWD:ALL' >/etc/sudoers.d/$user" in text
    # the user's own group: its name on the built image, its primary group on a named one
    assert 'chown -R "$user:$group" /work' in text
    assert re.findall(r"^\s*group=(.+)$", text, re.M) == [
        "$user",
        '$(podman exec "$name" id -gn "$user")',
    ]
    # the step loop and the diagnostics, both as the user
    assert _drill_local_step_users(text) == ['"$user"', '"$user"']
    # and nothing inside those execs climbs back to root
    joined = text.replace("\\\n", " ")
    for line in joined.splitlines():
        if "podman exec" in line and "drill-steps.sh" in line:
            assert not re.search(r"\bsudo\b|\bsu\b|runuser", line), line


def test_the_wargame_image_is_the_offline_image_with_the_drills_uv() -> None:
    """wargame.sh builds the EL8 offline image, then a second build on it that adds
    the uv drill-local.sh pins, so the result serves as DRILL_BASE_IMAGE.
    The offline image keeps its no-uv assertion for the rehearsal lanes."""
    script = WARGAME.read_text().replace("\\\n", " ")
    builds = [line for line in script.splitlines() if "podman build" in line]
    assert [re.findall(r"--file (\S+)", line) for line in builds] == [
        ['"$deploy/offline.Containerfile"'],
        ['"$here/wargame.Containerfile"'],
    ]
    # the second build starts from the image the first one tags
    (first_tag,) = re.findall(r'--tag "\$(\w+)"', builds[0])
    assert f'--build-arg "BASE=${first_tag}"' in builds[1]
    (pinned,) = re.findall(r"^uv_version=(\S+)$", DRILL_LOCAL.read_text(), re.M)
    layer = WARGAME.with_name("wargame.Containerfile").read_text()
    assert re.findall(r"\buv==(\S+)", layer) == [pinned]
    assert "&& ! command -v uv" in (DEPLOY / "offline.Containerfile").read_text()


def test_wargame_verbs_refuse_early_keep_work_and_tag_the_documented_image(
    tmp_path: Path,
) -> None:
    """A stub podman logs its calls. `deliver` refuses before podman on a bad
    bundle and, naming `start`, on a missing container; `start` of an existing
    container extracts nothing into /work; `build` tags the image the docs name."""
    stub = tmp_path / "bin" / "podman"
    stub.parent.mkdir()
    stub.write_text(
        '#!/bin/sh\necho "$*" >> "$STUB_LOG"\ncase $* in\n'
        '"container exists "*) [ "$STUB_EXISTS" = 1 ] ;;\n'
        '*dsl41.wargame*) echo "$STUB_OWNER" ;;\n'
        "*inspect*) echo true ;;\n*is-system-running*) echo running ;;\nesac\n"
    )
    stub.chmod(0o755)
    log = tmp_path / "log"
    bundle = tmp_path / "bundle" / "tools"
    bundle.mkdir(parents=True)
    (bundle / "prepare.sh").write_text("")
    env = {
        **os.environ,
        "PATH": f"{stub.parent}:{os.environ['PATH']}",
        "STUB_LOG": str(log),
        "STUB_OWNER": str(WARGAME.parents[1]),
    }

    def run(exists: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(WARGAME), *args],
            env={**env, "STUB_EXISTS": exists},
            capture_output=True,
            text=True,
            timeout=60,
        )

    refused = run("0", "deliver", str(tmp_path / "nonexistent"), "t")
    assert refused.returncode == 2 and not log.exists()
    missing = run("0", "deliver", str(bundle.parent), "t")
    assert missing.returncode == 2 and "start" in missing.stderr
    log.unlink()
    assert run("1", "start", "t").returncode == 0
    assert "/work.new" not in log.read_text()
    log.unlink()
    assert run("1", "build", "t").returncode == 0
    assert "--tag localhost/dsl41-wargame:t " in log.read_text()


# ---------------------------------------------------------- the release helper

RELEASE_HELPER = DEPLOY / "dsl41-release"
SUDOERS = DEPLOY / "dsl41-release.sudoers"
#: the drill's release steps (DL-407)
RELEASE_STEPS = (
    "release",
    "release-rollback",
    "release-properties-only",
    "release-calendar-only",
    "release-concurrent",
    "release-interrupted",
)


def test_the_sudoers_example_grants_the_helpers_service_mode_and_the_engine_start_only() -> None:
    """DL-407: the deployment identity runs the helper's service-account part as the
    units' user and starts the engine unit; the file grants those two commands and no
    other, at the paths the launcher and the units name."""
    lines = [line for line in SUDOERS.read_text().splitlines() if line and not line.startswith("#")]
    (prefix,) = re.findall(r"^PREFIX=(\S+)$", LAUNCH.read_text(), re.M)
    (user,) = _unit("dsl41-engine.service")["Service"]["User"]
    assert _unit("dsl41-supervisor.service")["Service"]["User"] == [user]
    assert lines == [
        f"nbdeploy ALL=({user}) NOPASSWD: {prefix}/bin/dsl41-release service *",
        "nbdeploy ALL=(root) NOPASSWD: /usr/bin/systemctl start dsl41-engine.service",
    ]
    text = RELEASE_HELPER.read_text()
    assert f'SERVICE_ACCOUNT = "{user}"' in text
    assert 'ENGINE_UNIT = "dsl41-engine.service"' in text
    # the helper runs on the runtime the launcher's link selects
    assert text.splitlines()[0] == f"#!{prefix}/.venv/bin/python -I"
    assert os.access(RELEASE_HELPER, os.X_OK)


def test_the_helpers_exit_codes_are_the_table_its_header_prints() -> None:
    """Each code the helper exits with is one row of its header's table, and each row
    is a code it defines."""
    text = RELEASE_HELPER.read_text()
    header = text.split('"""', 2)[1]
    table = header.split("Exit codes:\n", 1)[1]
    rows = {int(code) for code in re.findall(r"^  (\d)  \S", table, re.M)}
    defined = {int(code) for code in re.findall(r"^EXIT_[A-Z]+ = (\d)$", text, re.M)}
    assert rows == defined == {0, 2, 3, 4}
    exits = re.findall(r"(?:Stop\(|return |sys\.exit\()\s*(EXIT_[A-Z]+|\d)\b", text)
    assert {name for name in exits if not name.isdigit()} <= {
        name for name in re.findall(r"^(EXIT_[A-Z]+) = ", text, re.M)
    }
    assert {name for name in exits if name.isdigit()} == set()


def test_the_launchers_vars_restate_the_engines_deployment_options_for_a_seal(
    tmp_path: Path,
) -> None:
    """DL-407: every seal restates the deployment half of the profile as --next-*. The
    launcher prints it from the same function the engine line uses, so the two agree."""
    releases = _releases(tmp_path)
    launcher = _configured(tmp_path, DSL41=str(DSL41), **releases)
    printed = shlex.split(_sh(launcher, "--print").stdout)
    done = _sh(launcher, "vars")
    assert done.returncode == 0, done.stderr
    values = dict(shlex.split(line)[0].split("=", 1) for line in done.stdout.splitlines())
    deployment = {"--detached", "--deadman", "--as-machine", "--machine-policy"}
    run_form: list[str] = []
    for index, word in enumerate(printed):
        if word in deployment:
            run_form += [word] if word == "--detached" else [word, printed[index + 1]]
    restated = [word.replace("--next-", "--", 1) for word in shlex.split(values["NEXT_PROFILE"])]
    assert restated == run_form
    assert {key: values[key] for key in releases} == releases
    assert set(values) == {
        "DSL41",
        "RUN_ROOT",
        "ESTATE_ANCHOR",
        "ESTATE_RELEASES",
        "ACTIVE",
        "ENVIRONMENT",
        "PROPERTIES",
        "MAX_JOBS",
        "MAX_GRACE_S",
        "NEXT_PROFILE",
    }


def test_the_drill_runs_each_release_step_once_through_the_helper() -> None:
    """The drill names each release step once, in --list and in the workflow, and each
    step's body runs the installed helper; definition_probe goes through it too."""
    listed = subprocess.run(
        ["bash", str(DRILL_STEPS), "--list"], capture_output=True, text=True, timeout=60, check=True
    ).stdout.split()
    assert [step for step in listed if step.startswith("release")] == list(RELEASE_STEPS)
    text = DRILL_STEPS.read_text()
    for step in RELEASE_STEPS:
        body = re.search(rf"^step_{step.replace('-', '_')}\(\) \{{.*?^\}}$", text, re.M | re.S)
        assert body is not None, step
        assert re.search(r"\b(deploy|release_apply|release_prepare)\b", body.group()), step
    lib = DRILL_LIB.read_text()
    assert '"$REPO/examples/nightbank/deploy/dsl41-release"' in lib
    assert '"$REPO/examples/nightbank/deploy/dsl41-release.sudoers"' in lib
    probe = re.search(
        r"^definition_probe\(\) \{.*?^\}$",
        (DEPLOY / "offline-scenarios.sh").read_text(),
        re.M | re.S,
    )
    assert probe is not None
    assert "release_apply" in probe.group() and "seal_next" not in probe.group()
