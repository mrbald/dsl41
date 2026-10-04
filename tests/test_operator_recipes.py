"""DL-268: deployment-runbook ss0's recipes, run as the runbook prints them.

A block in docs/deployment-runbook.md whose fence follows a
`<!-- recipe: NAME -->` line is a recipe. The job recipe, the readiness
wait, the sealed check and the retirement's audit and list run here, as
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
}
#: the recipes drill-steps.sh runs as written
RUN_BY_DRILL = {
    "service-install",
    "service-start",
    "wait-answers",
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


def test_every_reboot_hold_is_released_where_it_is_taken() -> None:
    """S4-R14, R17: the reboot hold (both units disabled) is one named step.
    Only hold-down disables a unit and only hold-release (and the install)
    enables one; every procedure that takes the hold says where it releases
    it, except a retirement, which says it never does."""
    for (kind, name), body in _blocks().items():
        if kind != "recipe":
            continue
        assert ("systemctl disable" in body) == (name == "hold-down"), name
        assert ("systemctl enable" in body) == (name in ("hold-release", "service-install")), name
    taking = [chunk for chunk in _chunks() if re.search(r"[Tt]akes? .{0,10}reboot hold", chunk)]
    assert len(taking) >= 6  # ss0 x3, ss2b, ss7 rows 2 and 4
    for chunk in taking:
        heading = chunk.splitlines()[0]
        assert re.search(r"releas", chunk), heading
        if "Retiring an estate" in heading:
            assert "never releases it" in chunk


def test_no_recipe_starts_a_target() -> None:
    """S4-R2: a target start starts every enabled unit it wants on the host,
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
        "RestartPreventExitStatus=2 3",
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
    """F9's sealed-not-opened signal: after a seal, and until an operator
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
