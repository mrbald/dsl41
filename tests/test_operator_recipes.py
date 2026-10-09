"""DL-268: deployment-runbook ss0's recipes, run as the runbook prints them.

A block in docs/deployment-runbook.md whose fence follows a
`<!-- recipe: NAME -->` line is a recipe. The job recipe, the readiness
wait, the sealed check, the torn-opening recipe and the retirement's
audit and list run here, as
the runbook prints them, against a synthetic estate under the shipped
launcher. The service, removal and stop-check recipes need systemd: the
service drill runs them (drill-steps.sh's `run_recipe`), and the tests
below check their content, since CI never dispatches the drill. Every
marked recipe is run here, run by the drill, or compared, and a test
holds that partition.

Where the runbook says `systemctl start dsl41-engine.service`, these tests
run what that unit's `ExecStart=` runs: the launcher's engine mode, which
returns at once as a `Type=simple` start does. It runs as the invoking
user, with no supervisor unit, so the detached engine starts its own
supervisor (shape 2, not shape 1). The claim that systemd starts the unit,
and does not restart exit 2 or 3, is the drill's.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import typer

from dsl41.cli import app
from test_nightbank_deploy import (
    DEPLOY,
    DRILL_LIB,
    DRILL_STEPS,
    DSL41,
    LAUNCH,
    ORDER,
    _configured,
    _grant_me,
    _stop,
    _unit,
)
from test_docs_hygiene import ROOT
from test_runner_supervisor import wait_for

RUNBOOK = ROOT / "docs" / "deployment-runbook.md"
MARKER = re.compile(r"^<!-- (recipe|diagram): ([a-z-]+) -->$")

#: the synthetic estate: one job that stays, in the launcher's last file
PING = (
    "insert_job: OPS_PING_C\njob_type: CMD\ncommand: /bin/echo ~{$GREETING}~\nmachine: localhost\n"
)
NEW = "OPS_REPORT_C"


def _job(command: str) -> str:
    return f"\ninsert_job: {NEW}\njob_type: CMD\ncommand: {command}\nmachine: localhost\n"


def _blocks() -> dict[tuple[str, str], str]:
    """Every marked block of the runbook: (kind, name) -> its body. The
    fence must open on the line after the marker; a name is used once."""
    lines = RUNBOOK.read_text(encoding="utf-8").splitlines()
    found: dict[tuple[str, str], str] = {}
    for number, line in enumerate(lines):
        match = MARKER.match(line)
        if match is None:
            continue
        kind, name = match.groups()
        fence = "```sh" if kind == "recipe" else "```text"
        assert lines[number + 1] == fence, f"{RUNBOOK.name}:{number + 1}: no {fence} after it"
        end = lines.index("```", number + 2)
        assert (kind, name) not in found, f"{kind} {name} is marked twice"
        found[kind, name] = "\n".join(lines[number + 2 : end])
    return found


def recipe(name: str) -> str:
    return _blocks()["recipe", name]


def _env(site: dict[str, str], **extra: str) -> dict[str, str]:
    """The shell an operator has after ss0's variables, with this test's
    values, and the `dsl41` under test first on PATH."""
    return {
        **os.environ,
        "PATH": f"{DSL41.parent}{os.pathsep}{os.environ.get('PATH', '')}",
        "RUN_ROOT": site["RUN_ROOT"],
        "ESTATE_ANCHOR": site["ESTATE_ANCHOR"],
        "ESTATE": site["ESTATE"],
        "PROPERTIES": site["PROPERTIES"],
        "S": f"{site['RUN_ROOT']}/control.sock",
        **extra,
    }


def _run(name: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """One recipe in a POSIX shell that stops at the first failing command."""
    return subprocess.run(
        ["sh", "-euc", recipe(name)], env=env, capture_output=True, text=True, timeout=120
    )


def _ok(name: str, env: dict[str, str]) -> str:
    done = _run(name, env)
    assert done.returncode == 0, f"recipe {name}: {done.stdout}{done.stderr}"
    return done.stdout


def _write_estate(directory: Path, extra: str = "") -> None:
    """The launcher's six files, in its names: comments, PING, and `extra`."""
    directory.mkdir(exist_ok=True)
    for name in ORDER:
        body = f"/* {name}: synthetic */\n"
        if name == ORDER[-1]:
            body += PING + extra
        (directory / name).write_text(body)


def _site(base: Path) -> dict[str, str]:
    (base / "night").mkdir()
    (base / "night" / "night.properties").write_text("GREETING=hello\n")
    return {
        "DSL41": str(DSL41),
        "RUN_ROOT": str(base / "runs" / "nb"),
        "ESTATE_ANCHOR": str(base / "runs" / "nb.anchor"),
        "ACCESS_MAP": str(base / "etc" / "access.toml"),
        "ESTATE": str(base / "estate"),
        "PROPERTIES": str(base / "night" / "night.properties"),
    }


class _Engine:
    """The engine unit's ExecStart, run by hand: the configured launcher's
    engine mode, one process per start."""

    def __init__(self, base: Path, site: dict[str, str]) -> None:
        self.base = base
        self.site = site
        _grant_me(Path(site["ACCESS_MAP"]))
        self.launcher = _configured(base, **site)
        self.proc: subprocess.Popen[str] | None = None
        self.starts = 0

    def start(self, env: dict[str, str]) -> str:
        """Start, as `systemctl start` does, without waiting; then the
        runbook's own readiness wait. Returns the launcher's line: the
        command this start ran."""
        self.starts += 1
        log = self.base / f"engine-{self.starts}.log"
        with open(log, "w") as out:
            self.proc = subprocess.Popen(
                ["sh", str(self.launcher), "engine"],
                stdout=out,
                stderr=subprocess.STDOUT,
                text=True,
            )
        _ok("wait-answers", env)
        return log.read_text().splitlines()[0]

    def exit_code(self) -> int:
        assert self.proc is not None
        return self.proc.wait(timeout=120)

    def teardown(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            _stop(self.proc)
        subprocess.run(
            [str(DSL41), "supervise", "shutdown", "--run-root", self.site["RUN_ROOT"]],
            capture_output=True,
            timeout=120,
        )


def _transition(engine: _Engine, env: dict[str, str]) -> None:
    """ss0's job recipe, steps 2 to 5, on whatever the estate now holds."""
    _ok("job-check", env)
    _ok("job-seal", env)
    assert engine.exit_code() == 3  # sealed: the unit stays failed until opened
    line = engine.start(env)  # the recipe's job-open, then its wait
    assert line.startswith("dsl41-launch: engine: ") and line.endswith(" --resume"), line


# ------------------------------------------------------------- the markers


def test_every_marker_opens_one_block_and_names_it_once() -> None:
    found = _blocks()
    assert {name for kind, name in found if kind == "diagram"} == {"processes", "decisions"}
    assert all(body.strip() for body in found.values())


def test_the_drill_runs_only_recipes_the_runbook_has() -> None:
    """The drill is manual, so a renamed or deleted block would surface only
    when someone next dispatches it; this catches it in CI."""
    drill = DRILL_STEPS.read_text() + DRILL_LIB.read_text()
    used = set(re.findall(r"run_recipe(?: --[a-z0-9-]+)* ([a-z-]+)", drill))
    assert used == RUN_BY_DRILL
    assert used <= {name for kind, name in _blocks() if kind == "recipe"}


#: the recipes the tests below run as written
RUN_HERE = {
    "job-check",
    "job-seal",
    "job-verify",
    "job-verify-removed",
    "wait-answers",
    "watch-sealed",
    "retire-audit",
    "retire-list",
    "torn-look",
    "torn-check",
    "torn-reopen",
    "torn-reclaim",
    "torn-open",
}
#: the recipes drill-steps.sh runs as written
RUN_BY_DRILL = {
    "service-install",
    "service-start",
    "wait-answers",
    "watch-sealed",
    "hold-down",
    "hold-release",
    "retire-remove",
    "retire-check",
    "retire-audit",
    "retire-list",
}
#: compared, not run: the variables against the launcher, job-open against
#: the engine unit's ExecStart (the stand-in runs that instead)
COMPARED = {"operator-env", "job-open"}


def test_every_recipe_is_run_or_compared() -> None:
    """The runbook says which blocks run where; a new marked block that
    nothing runs fails here instead of sitting unexecuted."""
    recipes = {name for kind, name in _blocks() if kind == "recipe"}
    assert recipes == RUN_HERE | RUN_BY_DRILL | COMPARED


def test_every_recipe_parses_as_shell() -> None:
    for (kind, name), body in _blocks().items():
        if kind == "recipe":
            parsed = subprocess.run(["sh", "-n", "-c", body], capture_output=True, timeout=30)
            assert parsed.returncode == 0, name


def _chunks() -> list[str]:
    """The runbook cut at every heading and every bold upgrade row: one
    procedure per chunk."""
    text = RUNBOOK.read_text(encoding="utf-8")
    return re.split(r"^(?=#{2,3} |\*\*Row \d)", text, flags=re.M)


def _fenced() -> list[tuple[int, str | None, list[str]]]:
    """Every fenced block of the runbook, marked or not: its opening line
    number, the recipe or diagram name marked above it, and its lines."""
    lines = RUNBOOK.read_text(encoding="utf-8").splitlines()
    found: list[tuple[int, str | None, list[str]]] = []
    start: int | None = None
    for number, line in enumerate(lines):
        if start is None and line.startswith("```"):
            start = number
        elif start is not None and line == "```":
            marker = MARKER.match(lines[start - 1])
            name = None if marker is None else marker.group(2)
            found.append((start + 1, name, lines[start + 1 : number]))
            start = None
    assert start is None, f"{RUNBOOK.name}:{start}: the fence never closes"
    return found


def test_every_reboot_hold_is_released_where_it_is_taken() -> None:
    """DL-268, DL-270: the reboot hold (both units disabled) is one named
    step. Across every fenced block of the runbook, marked or not, only
    hold-down disables a unit. A unit is enabled only by the install and by
    hold-release's command, which a procedure that releases the hold inline
    marks with a comment. Every procedure that takes the hold says where it
    releases it, except a retirement, which says it never does."""
    down, up = recipe("hold-down").strip(), recipe("hold-release").strip()
    assert down.startswith("systemctl disable ") and up.startswith("systemctl enable ")
    assert up in recipe("service-install").splitlines()
    for start, name, body in _fenced():
        for line in body:
            command = line.split("#", 1)[0].strip()
            where = f"{RUNBOOK.name}:{start}"
            if "systemctl disable" in line:
                assert name == "hold-down" and command == down, where
            if "systemctl enable" in line:
                assert command == up, where
                if name not in ("hold-release", "service-install"):
                    assert "# release the reboot hold" in line, where
    taking = [chunk for chunk in _chunks() if re.search(r"[Tt]akes? .{0,10}reboot hold", chunk)]
    assert len(taking) >= 6  # ss0 x3, ss2b, ss7 rows 2 and 4
    for chunk in taking:
        heading = chunk.splitlines()[0]
        assert re.search(r"releas", chunk), heading
        if "Retiring an estate" in heading:
            assert "never releases it" in chunk


def test_no_recipe_starts_a_target() -> None:
    """DL-268: a target start starts every enabled unit it wants on the host,
    another estate's included. Only the drill's own container does it."""
    for (kind, name), body in _blocks().items():
        assert ".target" not in body, f"{kind} {name}"


def _directive(unit: str, section: str, key: str) -> str:
    (value,) = _unit(unit)[section][key]
    return value


def _launcher_value(name: str) -> str:
    (value,) = re.findall(rf"^{name}=(\S+)$", LAUNCH.read_text(), re.M)
    return value


UNITS = ("dsl41-engine.service", "dsl41-supervisor.service")


def _lines(name: str) -> list[str]:
    """A recipe's commands, continuation lines joined, spaces collapsed."""
    return [" ".join(line.split()) for line in recipe(name).replace("\\\n", " ").splitlines()]


def test_the_service_install_recipe_installs_what_the_units_and_launcher_name() -> None:
    """The drill-only service recipe, checked by content: the account the
    units run as, the launcher at their ExecStart path, the access map at
    the launcher's path and owned by the account, the run roots' directory
    the account's, both shipped units installed and enabled."""
    lines = _lines("service-install")
    user = _directive(UNITS[0], "Service", "User")
    assert {_directive(unit, "Service", "User") for unit in UNITS} == {user}
    assert any(line.startswith("useradd ") and line.endswith(f" {user}") for line in lines)
    launch_path = _directive(UNITS[0], "Service", "ExecStart").split()[0]
    assert f"install -D -m 0755 examples/nightbank/deploy/dsl41-launch {launch_path}" in lines
    runs = str(Path(_launcher_value("RUN_ROOT")).parent)
    assert f"install -d -o {user} -g {user} -m 0700 {runs}" in lines
    assert str(Path(_launcher_value("ESTATE_ANCHOR")).parent) == runs
    map_path = _launcher_value("ACCESS_MAP")
    assert (
        f"install -o {user} -g {user} -m 0600 examples/nightbank/deploy/{Path(map_path).name}"
        f" {map_path}"
    ) in lines
    shipped = sorted(path.name for path in DEPLOY.glob("*.service"))
    assert sorted(UNITS) == shipped
    assert (
        "install -m 0644 "
        + " ".join(f"examples/nightbank/deploy/{u}" for u in UNITS)
        + " /etc/systemd/system/"
    ) in lines
    enable = [line for line in lines if line.startswith("systemctl enable ")]
    assert len(enable) == 1 and sorted(enable[0].split()[2:]) == shipped
    assert lines.index("systemctl daemon-reload") < lines.index(enable[0])
    assert lines[-1] == f"sudo -u {user} {launch_path} --print"


def test_the_service_start_recipe_starts_the_engine_unit_and_reads_both() -> None:
    assert recipe("service-start").splitlines() == [
        f"systemctl start {UNITS[0]}",
        f"systemctl is-active {UNITS[1]} {UNITS[0]}",
    ]


def test_the_retirement_unit_recipes_name_each_shipped_unit() -> None:
    """The reboot hold disables both units and its release enables both;
    retire-remove keeps copies of both units, the launcher and the map,
    clears a failed state, and removes only the two unit files;
    retire-check reloads and then only reads, each unit on its own line."""
    for name, verb in (("hold-down", "disable"), ("hold-release", "enable")):
        words = recipe(name).split()
        assert words[:2] == ["systemctl", verb] and sorted(words[2:]) == sorted(UNITS), name

    remove = _lines("retire-remove")
    (copy,) = [line for line in remove if line.startswith("cp -p ")]
    assert sorted(copy.split()[2:-1]) == sorted(
        [
            *(f"/etc/systemd/system/{u}" for u in UNITS),
            _directive(UNITS[0], "Service", "ExecStart").split()[0],
            _launcher_value("ACCESS_MAP"),
        ]
    )
    (rm,) = [line for line in remove if line.startswith("rm ")]
    assert sorted(rm.split()[1:]) == sorted(f"/etc/systemd/system/{u}" for u in UNITS)
    # one unit per line: a unit systemd has unloaded refuses, and must not
    # keep the other's failed state from being cleared
    resets = [line for line in remove if line.startswith("systemctl reset-failed ")]
    assert sorted(resets) == sorted(f"systemctl reset-failed {u} || :" for u in UNITS)
    assert remove.index(copy) < min(map(remove.index, resets))
    assert max(map(remove.index, resets)) < remove.index(rm)

    assert recipe("retire-check").splitlines() == [
        "systemctl daemon-reload",
        *(f"systemctl show -p LoadState -p ActiveState {u}" for u in UNITS),
    ]


def test_the_operator_variables_are_the_launchers_values() -> None:
    text = LAUNCH.read_text()
    launcher = dict(re.findall(r"^(RUN_ROOT|ESTATE_ANCHOR|ESTATE|PROPERTIES)=(\S+)$", text, re.M))
    assigned = dict(re.findall(r"^(\w+)=(\S+)$", recipe("operator-env"), re.M))
    assert assigned == {**launcher, "S": "$RUN_ROOT/control.sock"}


def test_the_job_recipe_opens_through_the_engine_units_exec_start() -> None:
    """The stand-in above is honest only while `job-open` starts the unit
    whose ExecStart is the launcher's engine mode."""
    assert recipe("job-open") == "systemctl start dsl41-engine.service"
    assert _unit("dsl41-engine.service")["Service"]["ExecStart"] == [
        "/opt/dsl41/bin/dsl41-launch engine"
    ]


def test_the_seal_recipe_restates_the_launchers_order_and_options() -> None:
    """`--next` names the launcher's files in its order, and the `--next-*`
    options are the launcher's run options (a seal does not inherit them)."""
    seal = recipe("job-seal")
    assert re.findall(r'--next "\$ESTATE/([\w.]+)"', seal) == list(ORDER)
    # job-check lints, then rehearses, the same files in the same order
    assert re.findall(r'"\$ESTATE/([\w.]+)"', recipe("job-check")) == list(ORDER) * 2
    launch = LAUNCH.read_text()
    for option in ("--detached", "--as-machine", "--machine-policy", "--timezone"):
        (value,) = re.findall(rf"^\s+{option}(?: (\S+))? \\$", launch, re.M)
        spelled = f"--next-{option[2:]}" + (f" {value}" if value else "")
        assert spelled in seal, spelled


# ------------------------------------------------------------- the diagrams


def _cli_verbs() -> set[str]:
    return set(typer.main.get_command(app).commands)  # type: ignore[attr-defined]


def test_the_process_diagram_names_what_the_units_launcher_and_cli_name() -> None:
    """Diagram 1's labels are the unit files' own directives, the
    launcher's modes and the CLI's verbs, and it names every one of them
    that a running estate has."""
    diagram = _blocks()["diagram", "processes"]
    directives = {
        f"{key}={value}"
        for unit in ("dsl41-engine.service", "dsl41-supervisor.service")
        for section in _unit(unit).values()
        for key, values in section.items()
        for value in values
    }
    shown = re.findall(r"\b([A-Z][A-Za-z]+=.+?)(?=\s{2,}|$)", diagram, re.M)
    assert shown and set(shown) <= directives, set(shown) - directives
    for wanted in (
        "Requires=dsl41-supervisor.service",
        "Restart=on-failure",
        "RestartPreventExitStatus=2 3 5",
        "Restart=always",
        "ExecStart=/opt/dsl41/bin/dsl41-launch engine",
        "ExecStart=/opt/dsl41/bin/dsl41-launch supervisor",
        "ExecStartPost=/opt/dsl41/bin/dsl41-launch supervisor-ready",
    ):
        assert wanted in shown, wanted

    (usage,) = re.findall(r"usage: dsl41-launch \[--print\] \[([a-z|-]+)\]", LAUNCH.read_text())
    modes = set(usage.split("|"))
    assert set(re.findall(r"dsl41-launch ([a-z-]+)", diagram)) == modes
    assert set(re.findall(r"\bdsl41 ([a-z][a-z-]*)", diagram)) <= _cli_verbs()
    assert {"run", "supervise"} <= set(re.findall(r"\bdsl41 ([a-z][a-z-]*)", diagram))


def test_both_diagrams_name_only_units_the_example_ships() -> None:
    """Diagram 1 draws both units; diagram 2 names no other."""
    shipped = {path.name for path in DEPLOY.glob("*.service")}
    named = {
        name: set(re.findall(r"\b(dsl41-[a-z]+\.service)\b", _blocks()["diagram", name]))
        for name in ("processes", "decisions")
    }
    assert named["processes"] == shipped
    assert named["decisions"] and named["decisions"] <= shipped


# ------------------------------------------------------------- end to end


def test_the_job_recipe_adds_changes_and_removes_a_job(short_root: Path) -> None:
    """ss0's job recipe, three times over one lineage: add a job, change its
    command, remove it. Each pass is a complete catalog, a live seal that
    stops the engine with exit 3, and an in-place open; each verifies with
    the recipe's own query. The removed job stays as a ghost."""
    site = _site(short_root)
    _write_estate(Path(site["ESTATE"]))
    engine = _Engine(short_root, site)
    env = _env(site, JOB=NEW)
    try:
        genesis = engine.start(env)
        assert "--resume" not in genesis and "--open-from" not in genesis

        _write_estate(Path(site["ESTATE"]), _job("/bin/echo first"))
        _transition(engine, env)
        spec = json.loads(_ok("job-verify", env))
        assert spec["job_type"] == "CMD" and "/bin/echo first" in spec["jil"]

        _write_estate(Path(site["ESTATE"]), _job("/bin/echo second"))
        _transition(engine, env)
        spec = json.loads(_ok("job-verify", env))
        assert "/bin/echo second" in spec["jil"] and "/bin/echo first" not in spec["jil"]

        _write_estate(Path(site["ESTATE"]))
        _transition(engine, env)
        row = json.loads(_ok("job-verify-removed", env))["jobs"][NEW]
        assert row["job_type"] is None and row["status"] == "INACTIVE"
        refused = subprocess.run(
            [str(DSL41), "query", "spec", "-J", NEW, "-S", env["S"]],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert refused.returncode == 2 and f"unknown job '{NEW}'" in refused.stdout
        # three in-place boundaries: four periods, one root
        root = Path(site["RUN_ROOT"])
        assert sorted(p.name for p in (root / "wal").iterdir()) == [
            f"{n:06d}.jsonl" for n in (1, 2, 3, 4)
        ]

        # the retirement's read-only steps, on the stopped estate: the
        # retained history audits, and the list names the one root
        assert engine.proc is not None and _stop(engine.proc) == 0
        audited = _ok("retire-audit", env)
        assert [line.split()[1] for line in audited.splitlines()[:3]] == ["1", "2", "3"]
        assert "attested" in audited
        listed = _ok("retire-list", env).splitlines()
        assert listed[0] == "roots planned (1):"
        assert listed[1].startswith(f"  {os.path.realpath(root)}: period 4,")
        assert listed[2].startswith("would remove ")
    finally:
        engine.teardown()


def test_the_job_recipe_refuses_a_delta_file(short_root: Path) -> None:
    """ss0: AutoSys delta forms are refused, and the recipe's own check is
    where the maintainer meets the refusal, before any window."""
    site = _site(short_root)
    _write_estate(Path(site["ESTATE"]), "\nupdate_job: OPS_PING_C\ncommand: /bin/echo x\n")
    done = _run("job-check", _env(site))
    assert done.returncode == 2
    assert "subcommand 'update_job' is not supported" in done.stdout + done.stderr


@pytest.mark.skipif(shutil.which("jq") is None, reason="the runbook's check reads with jq")
def test_a_sealed_period_not_yet_opened_shows_on_the_anchor(short_root: Path) -> None:
    """The sealed-not-opened signal (DL-268): after a seal, and until an operator
    opens the next period, the lineage head reads `closed` and no engine
    answers; once opened it reads `open` again."""
    site = _site(short_root)
    _write_estate(Path(site["ESTATE"]))
    engine = _Engine(short_root, site)
    env = _env(site)
    try:
        engine.start(env)
        assert _ok("watch-sealed", env).strip() == "open"
        _ok("job-seal", env)
        assert engine.exit_code() == 3
        assert _ok("watch-sealed", env).strip() == "closed"
        answered = subprocess.run(
            [str(DSL41), "query", "status", "--brief", "-S", env["S"]],
            capture_output=True,
            timeout=60,
        )
        assert answered.returncode == 2  # nothing runs while the period waits
        engine.start(env)
        assert _ok("watch-sealed", env).strip() == "open"
    finally:
        engine.teardown()


def _launch_once(launcher: Path) -> subprocess.CompletedProcess[str]:
    """One start of the engine unit that is expected to refuse: the
    launcher's engine mode, run to its exit."""
    return subprocess.run(
        ["sh", str(launcher), "engine"], capture_output=True, text=True, timeout=120
    )


def _shutdown_supervisor(root: str) -> None:
    """Shape 2's supervisor stop; exit 2 means none was running."""
    done = subprocess.run(
        [str(DSL41), "supervise", "shutdown", "--run-root", root],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode in (0, 2), done.stdout + done.stderr


def _stopped(root: Path) -> None:
    """Shape 2's stop, then ss2b's no-writers check: no supervisor files."""
    _shutdown_supervisor(str(root))
    wait_for(lambda: not (root / "supervisor.pid").exists(), timeout_s=60)
    wait_for(lambda: not (root / "supervisor.sock").exists(), timeout_s=60)


def _check_stops(env: dict[str, str], why: str) -> None:
    done = _run("torn-check", env)
    assert done.returncode != 0 and f"stop: {why}" in done.stderr, done.stdout + done.stderr


def _each_guard_stops_the_check(root: Path, anchor: Path, env: dict[str, str]) -> None:
    """Every guard of torn-check the recipe relies on, one at a time: the
    evidence goes in, the check stops naming it, the evidence comes out."""
    from dsl41.period import wal_path

    segment = wal_path(root, 2)
    torn = segment.read_bytes()
    segment.write_bytes(torn + b"\n")
    _check_stops(env, "the segment holds a complete line")
    segment.write_bytes(torn)
    (root / "wal" / "000003.jsonl").write_bytes(b"")
    _check_stops(env, "wal/ holds something other than one segment")
    (root / "wal" / "000003.jsonl").unlink()
    (root / "runs" / "OPS_PING_C").mkdir(parents=True)
    _check_stops(env, "runs/ holds run evidence")
    (root / "runs" / "OPS_PING_C").rmdir()
    (root / "seals").mkdir(exist_ok=True)
    (root / "seals" / "000002.json").write_text("{}")
    _check_stops(env, "the period has a seal")
    (root / "seals" / "000002.json").unlink()
    (root / "supervisor.pid").write_text("1\n")
    _check_stops(env, "a supervisor may still run")
    (root / "supervisor.pid").unlink()
    sentinel = root / "journal.jsonl"
    owned = sentinel.read_bytes()
    record = json.loads(owned)
    sentinel.write_text(json.dumps({**record, "claim_id": None}) + "\n")
    _check_stops(env, "the head's claim did not open this root")
    sentinel.write_bytes(owned)
    (claim,) = (anchor / "claims").iterdir()
    claim.rename(claim.with_suffix(".aside"))
    _check_stops(env, "the claim file is missing")
    claim.with_suffix(".aside").rename(claim)


@pytest.mark.skipif(shutil.which("jq") is None, reason="the runbook's check reads with jq")
@pytest.mark.parametrize("cause", ["crash", "crash-fallback", "damage"])
def test_a_torn_sole_opening_of_a_rolled_root_is_recovered_by_the_recipe(
    short_root: Path, cause: str
) -> None:
    """ss0's torn-opening recipe, as the runbook prints it. Root A seals
    and is audited; the roll into B leaves B's only segment as part of
    its first line and nothing after, which resume refuses with `missing
    segment record`.

    `crash` stops the roll between the segment and the head move: the
    head stays `claimed` by B. Every guard of the check stops it when
    its evidence is there; then the segment goes, the identical opener
    reopens period 2 in B with no reclaim, and B resumes.
    `crash-fallback` walks the fallback blocks instead: reclaim, then a
    fresh root C. `damage` finishes the roll, stops B and cuts the
    durable segment to part of its first line, so every later record is
    gone too; the head reads `open`, the check finds no run evidence,
    the anchor comes back from a copy taken before the roll, and C opens
    the period."""
    from dsl41.boundary import load_bundle_catalog
    from dsl41.estate import roll_into_root
    from dsl41.period import wal_path
    from dsl41.runner_journal import read_journal

    site = _site(short_root)
    _write_estate(Path(site["ESTATE"]))
    root_a, anchor = Path(site["RUN_ROOT"]), Path(site["ESTATE_ANCHOR"])
    site_b = {**site, "RUN_ROOT": str(short_root / "runs" / "nb-b")}
    site_c = {**site, "RUN_ROOT": str(short_root / "runs" / "nb-c")}
    root_b, root_c = Path(site_b["RUN_ROOT"]), Path(site_c["RUN_ROOT"])
    (short_root / "b").mkdir()
    (short_root / "c").mkdir()
    engine = _Engine(short_root, site)
    engine_b = _Engine(short_root / "b", site_b)
    engine_c = _Engine(short_root / "c", site_c)
    env, env_b, env_c = _env(site), _env(site_b), _env(site_c)

    def head() -> dict[str, object]:
        stored = json.loads((anchor / "anchor.json").read_text())["head"]
        assert isinstance(stored, dict)
        return stored

    try:
        # period 1 in A, sealed live, audited, its supervisor stopped
        engine.start(env)
        _ok("job-seal", env)
        assert engine.exit_code() == 3
        audited = subprocess.run(
            [str(DSL41), "audit", "--run-root", str(root_a), "--estate-anchor", str(anchor)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert audited.returncode == 0, audited.stdout + audited.stderr
        _stopped(root_a)
        copy = short_root / "copy"
        shutil.copytree(anchor, copy / "anchor")

        # the roll into B, and B's only segment cut inside its first line
        if cause == "damage":
            Path(f"{root_b}.open-from").touch()
            engine_b.start(env_b)
            assert engine_b.proc is not None and _stop(engine_b.proc) == 0
            _stopped(root_b)
        else:

            class Stopped(Exception):
                pass

            def crash_point(stage: str) -> None:
                if stage == "after_opening_segment":
                    raise Stopped(stage)

            with pytest.raises(Stopped):
                roll_into_root(
                    root_b,
                    anchor_dir=anchor,
                    catalog_of=lambda root, m: load_bundle_catalog(root, m.source_bundle_hash),
                    crash_point=crash_point,
                )
        segment = wal_path(root_b, 2)
        first = segment.read_bytes().split(b"\n", 1)[0]
        segment.write_bytes(first[: len(first) // 2])

        # the unit's start refuses, and so does the identical retry
        refused = _launch_once(engine_b.launcher)
        assert refused.returncode == 2
        assert "missing segment record" in refused.stdout + refused.stderr
        if cause != "damage":
            Path(f"{root_b}.open-from").touch()
            retried = _launch_once(engine_b.launcher)
            assert retried.returncode == 2
            assert "missing segment record" in retried.stdout + retried.stderr
            assert head()["state"] == "claimed"

        # steps 1 to 3: look, stop, check. The refused detached starts
        # left B's supervisor up, so the look asks it for its runs
        assert (root_b / "supervisor.sock").exists()
        looked = _ok("torn-look", env_b).splitlines()
        assert json.loads(looked[0])["state"] == ("open" if cause == "damage" else "claimed")
        _stopped(root_b)
        if cause == "crash":
            _each_guard_stops_the_check(root_b, anchor, env_b)
        if cause == "damage":
            sentinel = root_b / "journal.jsonl"
            owned = sentinel.read_bytes()
            sentinel.write_text(json.dumps({**json.loads(owned), "claim_id": None}) + "\n")
            _check_stops(env_b, "no roll created this root")
            sentinel.write_bytes(owned)
        state = "open" if cause == "damage" else "claimed"
        assert _ok("torn-check", env_b).split() == [state, "000002.jsonl"]

        if cause == "crash":
            # steps 4 and 5: the identical opener reopens B in place
            _ok("torn-reopen", env_b)
            assert sorted(p.name for p in (root_b / "wal").iterdir()) == []
            line = engine_b.start(env_b)
            assert line.endswith(f" --open-from {anchor}"), line
            assert head() == {"state": "open", "period_id": 2, "root": os.path.realpath(root_b)}
            assert read_journal(segment)[0]["reclaimed"] is None
            assert engine_b.proc is not None and _stop(engine_b.proc) == 0
            line = engine_b.start(env_b)
            assert line.endswith(" --resume"), line
            return

        if cause == "crash-fallback":
            _ok("torn-reclaim", env_b)
            _check_stops(env_b, "the head is closed")
        else:
            shutil.rmtree(anchor)
            shutil.copytree(copy / "anchor", anchor)
        assert head()["state"] == "closed"

        # step 7: a fresh root C
        _ok("torn-open", env_c)
        line = engine_c.start(env_c)
        assert line.endswith(f" --open-from {anchor}"), line
        assert head() == {"state": "open", "period_id": 2, "root": os.path.realpath(root_c)}
        opening = read_journal(wal_path(root_c, 2))[0]
        assert (opening["reclaimed"] is not None) == (cause == "crash-fallback")
        # a rerun of the check on B now meets a lineage open elsewhere
        _check_stops(env_b, "the head names another root")

        # B can no longer resume: the anchor does not name it. C stops
        # first, or B meets C's anchor lock before the rule
        assert engine_c.proc is not None and _stop(engine_c.proc) == 0
        stale = _launch_once(engine_b.launcher)
        assert stale.returncode == 2
        assert "this anchor does not name" in stale.stdout + stale.stderr
        line = engine_c.start(env_c)
        assert line.endswith(" --resume"), line
    finally:
        engine_c.teardown()
        engine_b.teardown()
        engine.teardown()
