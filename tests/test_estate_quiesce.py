"""`dsl41 estate quiesce`: the shape 1 estate stop as one verb (DL-398).

The service manager is faked: a reader of `systemctl show` argv. The verb's
clock and sleep are faked too. The sleep fails the test outside the
maintenance stage's exit observation, the verb's one bounded wait. The seal and the audit are the real offline seal and
audit on a real root. The supervisor is faked where maintenance must
complete, because guarded maintenance needs a qualified Linux service cgroup;
the real-process test meets a real supervisor, whose maintenance refuses.

Every refusal has its passing counterpart: a verb that never passed a stage
would refuse the same way.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from dsl41 import cli_estate
from dsl41 import runner_procid as procid
from dsl41.cli import app
from dsl41.period import attestation_path
from dsl41.cli_estate import ENGINE_UNIT, SUPERVISOR_UNIT, QuiesceHost
from dsl41.runner_adapters import SupervisorConn

from test_runner_leadership import cli, engine
from test_runner_supervisor import start_supervisor, wait_for
from test_supervisor_launch import _stop_supervisor

JIL = "insert_job: a\njob_type: c\ncommand: sleep 600\n"
NEXT_JIL = JIL + "\ninsert_job: b\njob_type: c\ncommand: echo two\n"

STAGES = [
    "hold",
    "engine",
    "leadership",
    "supervisor",
    "horizon",
    "seal",
    "audit",
    "maintenance",
    "units",
    "writers",
]


class Units:
    """A fake `systemctl show`: each unit's properties, and every argv read."""

    def __init__(self) -> None:
        self.props = {
            unit: {
                "LoadState": "loaded",
                "UnitFileState": "disabled",
                "ActiveState": "inactive",
                "SubState": "dead",
                "MainPID": "0",
            }
            for unit in (ENGINE_UNIT, SUPERVISOR_UNIT)
        }
        self.reads: list[list[str]] = []

    def __call__(self, argv: list[str]) -> str:
        self.reads.append(argv)
        assert argv[:2] == ["systemctl", "show"]
        unit = argv[2]
        names = argv[4::2]
        return "".join(f"{name}={self.props[unit][name]}\n" for name in names)


def _dead_pid() -> int:
    """The pid of a process that has exited and been reaped."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


class FakeSupervisor:
    """LIST, PING, ACQUIRE, MAINTENANCE_STOP and RELEASE, as a supervisor answers
    them. Maintenance refuses while a run is live. Otherwise it is admitted and
    completes at once: maintenance.json records it completed, naming a process
    that has already exited (DL-401). At MAINTENANCE_STOP it takes `leader.lock`
    as the real server does (DL-390), which proves the verb handed it over."""

    def __init__(self, run_root: Path) -> None:
        self.run_root = run_root
        self.up = True
        self.runs: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []
        self.pid = _dead_pid()
        self.start = "ticks:1"
        self.acquired: dict[str, Any] = {"ok": True, "token": 7, "incarnation": "a" * 32}

    def connect(self, _sock: Path) -> FakeSupervisor:
        if not self.up:
            raise FileNotFoundError("no supervisor.sock")
        return self

    def close(self) -> None:
        pass

    def send(self, request: dict[str, Any]) -> dict[str, Any]:
        self.sent.append(request)
        cmd = request["cmd"]
        state = {"ok": True, "incarnation": "a" * 32, "process_state": "serving"}
        if cmd == "LIST":
            return {**state, "supervisor_pid": self.pid, "runs": self.runs, "lease": None}
        if cmd == "PING":
            return state
        if cmd == "ACQUIRE":
            return self.acquired
        if cmd == "RELEASE":
            return {"ok": True}
        assert cmd == "MAINTENANCE_STOP"
        fd = procid.flock_existing(self.run_root / "leader.lock")
        os.close(fd)
        if any(run["wrapper_alive"] for run in self.runs):
            detail = "active wrappers or residual service processes"
            return {"ok": False, "error": "maintenance_refused", "detail": detail}
        self.up = False
        identity = {
            "incarnation": "a" * 32,
            "supervisor_pid": self.pid,
            "supervisor_start_time": self.start,
            "boot_id": procid.current_boot_id(),
            "force": request["force"],
            "bound_s": 0.0,
            "started_at": datetime.now(UTC).isoformat(),
        }
        record = {"version": 1, "state": "completed", **identity, "finished_at": "now"}
        (self.run_root / "maintenance.json").write_text(json.dumps(record))
        _note(self.run_root, self.pid)
        return {"ok": True, "maintenance": "admitted", **identity}


def _note(run_root: Path, pid: int) -> None:
    """The start note a supervisor of incarnation `"a" * 32` writes in supervisor.lock."""
    note = {"version": 1, "incarnation": "a" * 32, "pid": pid}
    (run_root / "supervisor.lock").write_text(json.dumps(note))


class Sleeps:
    """The verb's sleep. It sleeps only inside maintenance's watch of an
    admitted operation (DL-401), and fails the test anywhere else."""

    def __init__(self) -> None:
        self.inside = False
        self.slept: list[float] = []
        #: the path the first sleep creates, for a process told to exit then
        self.release: Path | None = None

    def __call__(self, seconds: float) -> None:
        assert self.inside, f"the verb slept {seconds}s outside the exit observation"
        self.slept.append(seconds)
        if self.release is not None:
            self.release.touch()
        time.sleep(seconds)


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> Sleeps:
    guard = Sleeps()
    real = cli_estate.guarded_maintenance

    def observed(*args: Any, **kwargs: Any) -> Any:
        guard.inside = True
        try:
            return real(*args, **kwargs)
        finally:
            guard.inside = False

    monkeypatch.setattr(cli_estate, "guarded_maintenance", observed)
    settle = cli_estate.settle_maintenance

    def watched(*args: Any, **kwargs: Any) -> Any:
        guard.inside = True
        try:
            return settle(*args, **kwargs)
        finally:
            guard.inside = False

    monkeypatch.setattr(cli_estate, "settle_maintenance", watched)
    return guard


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch, sleeps: Sleeps) -> Iterator[QuiesceHost]:
    """The verb's host, with every part faked but the supervisor connection."""
    fake = QuiesceHost(
        systemctl=Units(),
        now=lambda: datetime(2000, 1, 1),
        sleep=sleeps,
        processes=lambda: [],
    )
    monkeypatch.setattr(cli_estate, "QUIESCE_HOST", fake)
    yield fake


def _use(monkeypatch: pytest.MonkeyPatch, host: QuiesceHost, **parts: Any) -> QuiesceHost:
    changed = replace(host, **parts)
    monkeypatch.setattr(cli_estate, "QUIESCE_HOST", changed)
    return changed


def _quiesce(run_root: Path, next_jil: Path, *extra: str) -> Any:
    return CliRunner().invoke(
        app,
        ["estate", "quiesce", "--run-root", str(run_root), "--next", str(next_jil), *extra],
        catch_exceptions=False,
    )


def _stages(output: str) -> dict[str, str]:
    """Each stage's outcome word, from its `quiesce: <stage>: <outcome>` line."""
    found: dict[str, str] = {}
    for line in output.splitlines():
        parts = line.split(": ", 3)
        if len(parts) >= 3 and parts[0] == "quiesce" and parts[1] in STAGES:
            found[parts[1]] = parts[2]
    return found


def _lock_is_free(run_root: Path) -> bool:
    try:
        fd = procid.flock_existing(run_root / "leader.lock")
    except OSError:
        return False
    os.close(fd)
    return True


def _stopped_root(base: Path) -> tuple[Path, Path]:
    """A tethered period-1 root whose engine has stopped, and the next estate."""
    with engine(base, jil_text=JIL):
        pass
    next_jil = base / "next.jil"
    next_jil.write_text(NEXT_JIL)
    return base / "run", next_jil


# ------------------------------------------------------- refused, then stopped


def test_live_work_refuses_at_maintenance_then_the_rerun_stops_the_estate(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, host: QuiesceHost
) -> None:
    """A3. A live run: the seal and audit pass, maintenance refuses with exit 2
    and prints the run, the lease goes back and leader.lock is free. Once the
    run ends, the rerun skips the seal and the audit, completes maintenance,
    reads both units down and passes the writers stage, with no sleep. A
    third run, on the stopped estate, skips every stage that acts."""
    run_root, next_jil = _stopped_root(short_root)
    supervisor = FakeSupervisor(run_root)
    supervisor.runs = [
        {"run_id": "r-1", "job": "a", "run_number": 1, "wrapper_pid": 4242, "wrapper_alive": True}
    ]
    _use(monkeypatch, host, connect=supervisor.connect)

    refused = _quiesce(run_root, next_jil)
    assert refused.exit_code == 2, refused.output
    stages = _stages(refused.output)
    assert [stages[name] for name in STAGES[:8]] == ["ok"] * 7 + ["refused"]
    assert "units" not in stages and "writers" not in stages
    assert "quiesce: live run: a run 1 (r-1), wrapper pid 4242" in refused.output
    assert "the jobs go on: rerun once they end, or pass --force terminate" in refused.output
    assert "durable: period 1 sealed, attested through period 1" in refused.output
    assert supervisor.sent[-1] == {"cmd": "RELEASE", "token": 7}
    assert _lock_is_free(run_root)

    supervisor.runs[0]["wrapper_alive"] = False
    stopped = _quiesce(run_root, next_jil)
    assert stopped.exit_code == 0, stopped.output
    assert _stages(stopped.output) == {
        "hold": "ok",
        "engine": "ok",
        "leadership": "ok",
        "supervisor": "ok",
        "horizon": "skipped",
        "seal": "skipped",
        "audit": "skipped",
        "maintenance": "ok",
        "units": "ok",
        "writers": "ok",
    }
    cmds = ["LIST", "PING", "ACQUIRE", "MAINTENANCE_STOP"]
    assert [r["cmd"] for r in supervisor.sent[-4:]] == cmds
    assert supervisor.sent[-2]["controller_id"] == f"estate-quiesce-{os.getpid()}"
    assert _lock_is_free(run_root)

    again = _quiesce(run_root, next_jil)
    assert again.exit_code == 0, again.output
    acts = ("supervisor", "horizon", "seal", "audit", "maintenance")
    assert {name: _stages(again.output)[name] for name in acts} == dict.fromkeys(acts, "skipped")
    assert "quiesce: supervisor: skipped: already gone" in again.output


# ------------------------------------------------------- the root steps


@pytest.mark.parametrize(
    "unit,prop,value,command",
    [
        (
            ENGINE_UNIT,
            "UnitFileState",
            "enabled",
            f"as root: systemctl disable {ENGINE_UNIT} {SUPERVISOR_UNIT}",
        ),
        (
            SUPERVISOR_UNIT,
            "UnitFileState",
            "enabled",
            f"as root: systemctl disable {ENGINE_UNIT} {SUPERVISOR_UNIT}",
        ),
        (ENGINE_UNIT, "ActiveState", "active", f"as root: systemctl stop {ENGINE_UNIT}"),
        (ENGINE_UNIT, "ActiveState", "activating", f"as root: systemctl stop {ENGINE_UNIT}"),
    ],
)
def test_a_root_step_not_taken_refuses_and_names_its_command(
    tmp_path: Path, host: QuiesceHost, unit: str, prop: str, value: str, command: str
) -> None:
    """The reboot hold and the engine stop are root's; the verb checks them
    first, takes nothing and touches nothing in the root."""
    assert isinstance(host.systemctl, Units)
    host.systemctl.props[unit][prop] = value
    run_root = tmp_path / "run"
    run_root.mkdir()
    (run_root / "leader.lock").touch()
    result = _quiesce(run_root, tmp_path / "next.jil")
    assert result.exit_code == 2, result.output
    assert command in result.output
    assert "leadership" not in _stages(result.output)
    assert sorted(path.name for path in run_root.iterdir()) == ["leader.lock"]


def test_a_held_leader_lock_refuses_and_names_its_holder(tmp_path: Path, host: QuiesceHost) -> None:
    """The engine stage has shown the engine unit down, so the holder is some
    other process: the refusal names the lock and what it records."""
    run_root = tmp_path / "run"
    run_root.mkdir()
    note = {"pid": 4242, "host": "nightbank-01", "epoch": 3, "since": "2026-10-25T00:00:00"}
    (run_root / "leader.lock").write_text(json.dumps(note))
    held = procid.flock_exclusive(run_root / "leader.lock")
    try:
        result = _quiesce(run_root, tmp_path / "next.jil")
    finally:
        os.close(held)
    assert result.exit_code == 2, result.output
    stages = _stages(result.output)
    assert stages == {"hold": "ok", "engine": "ok", "leadership": "refused"}
    assert "pid 4242 on nightbank-01" in result.output
    assert f"find the process that holds {run_root / 'leader.lock'}" in result.output
    assert "systemctl stop" not in result.output


def test_masked_units_are_a_reboot_hold(tmp_path: Path, host: QuiesceHost) -> None:
    assert isinstance(host.systemctl, Units)
    for unit in (ENGINE_UNIT, SUPERVISOR_UNIT):
        host.systemctl.props[unit].update(LoadState="masked", UnitFileState="masked")
    run_root = tmp_path / "run"
    run_root.mkdir()
    (run_root / "leader.lock").touch()
    result = _quiesce(run_root, tmp_path / "next.jil")
    stages = _stages(result.output)
    assert stages["hold"] == "ok" and stages["engine"] == "ok", result.output


def test_a_missing_leader_lock_refuses_without_creating_one(
    tmp_path: Path, host: QuiesceHost
) -> None:
    """Maintenance refuses without the lock evidence (DL-390), so the verb
    never makes one."""
    run_root = tmp_path / "run"
    run_root.mkdir()
    result = _quiesce(run_root, tmp_path / "next.jil")
    assert result.exit_code == 2, result.output
    assert _stages(result.output)["leadership"] == "refused"
    assert not (run_root / "leader.lock").exists()


# ------------------------------------------------------- the horizon


def test_the_retry_horizon_refuses_with_its_end_and_never_waits(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, host: QuiesceHost
) -> None:
    """period-model ss9: an operator request inside the horizon. The verb
    names the end time and refuses rather than waiting. Past that time the
    stage passes; the seal's own gate still reads its own clock. Inside it,
    --force-seal passes the stage and the seal records the force."""
    with engine(short_root, jil_text=JIL) as running:
        held = cli("sendevent", "ON_HOLD", "-J", "a", "-S", str(running.run_root / "control.sock"))
        assert held.returncode == 0, held.stderr
    run_root = short_root / "run"
    next_jil = short_root / "next.jil"
    next_jil.write_text(NEXT_JIL)
    supervisor = FakeSupervisor(run_root)
    now = datetime.now(UTC).replace(tzinfo=None)  # naive UTC, as the journal stamps
    _use(monkeypatch, host, connect=supervisor.connect, now=lambda: now)

    early = _quiesce(run_root, next_jil)
    assert early.exit_code == 2, early.output
    assert _stages(early.output)["horizon"] == "refused"
    assert "the retry horizon runs until " in early.output
    assert " UTC; rerun after it, or pass --force-seal" in early.output
    assert "seal" not in _stages(early.output)

    _use(monkeypatch, host, connect=supervisor.connect, now=lambda: now + timedelta(hours=1))
    late = _quiesce(run_root, next_jil)
    assert "quiesce: horizon: ok: ended at " in late.output

    _use(monkeypatch, host, connect=supervisor.connect, now=lambda: now)
    forced = _quiesce(run_root, next_jil, "--force-seal")
    assert forced.exit_code == 0, forced.output
    assert "--force-seal records the force" in forced.output
    assert _stages(forced.output)["seal"] == "ok"


# ------------------------------------------------------- the units


@pytest.mark.parametrize(
    "active,sub,main,code",
    [
        ("activating", "auto-restart", "0", 4),
        ("active", "running", "new", 4),
        ("deactivating", "stop-post", "0", 2),
        ("active", "running", "exited", 2),
        ("failed", "failed", "0", 0),
    ],
)
def test_the_units_are_read_once_after_the_exit(
    short_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    host: QuiesceHost,
    active: str,
    sub: str,
    main: str,
    code: int,
) -> None:
    """A unit its exit policy restarted is uncertain, exit 4. A unit the
    manager is still stopping, or whose recorded main process is the one
    that just exited, is a rerun, exit 2. A failed unit is down."""
    run_root, next_jil = _stopped_root(short_root)
    supervisor = FakeSupervisor(run_root)
    units = Units()
    _use(monkeypatch, host, connect=supervisor.connect, systemctl=units)
    real = cli_estate.guarded_maintenance

    def then_the_unit_reads(*args: Any, **kwargs: Any) -> Any:
        answer = real(*args, **kwargs)
        pid = {"0": "0", "new": str(supervisor.pid + 1), "exited": str(supervisor.pid)}[main]
        units.props[SUPERVISOR_UNIT].update(ActiveState=active, SubState=sub, MainPID=pid)
        return answer

    monkeypatch.setattr(cli_estate, "guarded_maintenance", then_the_unit_reads)
    result = _quiesce(run_root, next_jil)
    assert result.exit_code == code, result.output
    assert _stages(result.output)["units"] == ("ok" if code == 0 else "refused")
    if code:
        assert f"{SUPERVISOR_UNIT} is {active}/{sub}" in result.output


def test_the_one_wait_is_the_exit_observation_after_completion(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, host: QuiesceHost, sleeps: Sleeps
) -> None:
    """The admitted operation names a process that is still alive. It exits
    only once the watch has slept, so the verb waits for its exit, bounded by
    the operation's `bound_s` and the margin, and sleeps nowhere else. No fixed
    delay decides the order (DL-401)."""
    run_root, next_jil = _stopped_root(short_root)
    supervisor = FakeSupervisor(run_root)
    sleeps.release = short_root / "release"
    lingering = subprocess.Popen(
        ["sh", "-c", 'while [ ! -e "$1" ]; do sleep 0.01; done', "sh", str(sleeps.release)]
    )
    try:
        supervisor.pid = lingering.pid
        start = procid.proc_start_token(lingering.pid)
        assert start is not None
        supervisor.start = start
        _use(monkeypatch, host, connect=supervisor.connect)
        result = _quiesce(run_root, next_jil)
    finally:
        sleeps.release.touch()
        lingering.wait(timeout=30)
    assert result.exit_code == 0, result.output
    assert sleeps.slept and set(sleeps.slept) == {0.05}
    assert sum(sleeps.slept) < 5


def test_a_lease_held_by_another_controller_names_it(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, host: QuiesceHost
) -> None:
    run_root, next_jil = _stopped_root(short_root)
    supervisor = FakeSupervisor(run_root)
    supervisor.acquired = {
        "ok": False,
        "error": "lease_held",
        "holder": "engine-1234",
        "expires_at": "2026-10-25T12:00:00+00:00",
    }
    _use(monkeypatch, host, connect=supervisor.connect)
    result = _quiesce(run_root, next_jil)
    assert result.exit_code == 2, result.output
    assert "the supervisor's lease is held by engine-1234 until" in result.output
    assert "MAINTENANCE_STOP" not in [request["cmd"] for request in supervisor.sent]


def test_a_next_file_that_does_not_load_still_ends_the_seal_stage(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, host: QuiesceHost
) -> None:
    run_root, _ = _stopped_root(short_root)
    _use(monkeypatch, host, connect=FakeSupervisor(run_root).connect)
    result = _quiesce(run_root, short_root / "missing.jil")
    assert result.exit_code == 2, result.output
    assert "quiesce: seal: refused: exited 2" in result.output


def _audited_root_with_live_run(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, host: QuiesceHost
) -> tuple[Path, Path]:
    """A root the verb has sealed and attested, whose maintenance refused live work."""
    run_root, next_jil = _stopped_root(short_root)
    supervisor = FakeSupervisor(run_root)
    supervisor.runs = [
        {"run_id": "r-1", "job": "a", "run_number": 1, "wrapper_pid": 4242, "wrapper_alive": True}
    ]
    _use(monkeypatch, host, connect=supervisor.connect)
    first = _quiesce(run_root, next_jil)
    assert _stages(first.output)["audit"] == "ok", first.output
    return run_root, next_jil


def _assert_audit_refusal(result: Any, attestation: Path) -> None:
    assert result.exit_code == 2, result.output
    assert "Traceback" not in result.output
    assert _stages(result.output)["audit"] == "refused"
    assert f"quiesce: audit: refused: {attestation}" in result.output
    assert "maintenance" not in _stages(result.output)
    assert _lock_is_free(attestation.parents[1])


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode 000 file")
def test_an_unreadable_attestation_refuses_at_audit_with_one_sentence(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, host: QuiesceHost
) -> None:
    run_root, next_jil = _audited_root_with_live_run(short_root, monkeypatch, host)
    attestation = attestation_path(run_root, 1)
    attestation.chmod(0o000)
    try:
        result = _quiesce(run_root, next_jil)
    finally:
        attestation.chmod(0o644)
    _assert_audit_refusal(result, attestation)
    assert "unreadable" in result.output


def test_an_attestation_that_does_not_parse_refuses_at_audit_with_one_sentence(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, host: QuiesceHost
) -> None:
    run_root, next_jil = _audited_root_with_live_run(short_root, monkeypatch, host)
    attestation = attestation_path(run_root, 1)
    attestation.write_text("{not json")
    result = _quiesce(run_root, next_jil)
    _assert_audit_refusal(result, attestation)


def test_the_writers_stage_names_a_remaining_process(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, host: QuiesceHost
) -> None:
    run_root, next_jil = _stopped_root(short_root)
    supervisor = FakeSupervisor(run_root)
    _use(monkeypatch, host, connect=supervisor.connect, processes=lambda: ["4243 (sleep)"])
    result = _quiesce(run_root, next_jil)
    assert result.exit_code == 2, result.output
    assert _stages(result.output)["writers"] == "refused"
    assert "remain: 4243 (sleep)" in result.output


def test_a_process_the_scan_cannot_read_stops_the_writers_stage(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, host: QuiesceHost
) -> None:
    run_root, next_jil = _stopped_root(short_root)

    def unreadable() -> list[str]:
        raise OSError("cannot observe pid 777 ([Errno 13] Permission denied)")

    _use(monkeypatch, host, connect=FakeSupervisor(run_root).connect, processes=unreadable)
    result = _quiesce(run_root, next_jil)
    assert result.exit_code == 2, result.output
    assert _stages(result.output)["writers"] == "refused"
    assert "cannot observe pid 777" in result.output


def _fake_proc(
    monkeypatch: pytest.MonkeyPatch, table: dict[str, Any], owners: dict[str, int] | None = None
) -> None:
    """A /proc of `table`: pid to its status fields, or to the error its read raises.
    `owners` gives a `/proc/<pid>` directory's uid; the default is the verb's own, 1."""
    owners = owners or {}
    monkeypatch.setattr(sys, "platform", "linux")
    me = str(os.getpid())
    table = {me: {"PPid": "900", "Uid": "1 1 1 1", "State": "R"}, **table}
    real_iterdir = Path.iterdir

    def iterdir(self: Path) -> Iterator[Path]:
        if str(self) == "/proc":
            return iter(Path(f"/proc/{pid}") for pid in table)
        return real_iterdir(self)

    def status(pid: str) -> dict[str, str]:
        value = table.get(pid, FileNotFoundError(2, "no such process"))
        if isinstance(value, BaseException):
            raise value
        return value

    real_read_bytes = Path.read_bytes

    def read_bytes(self: Path) -> bytes:
        # The fake table owns every name: a pid of the table that a real process on the host
        # also holds must not answer with that process's cmdline (DL-398).
        if self.parent.parent == Path("/proc") and self.name == "cmdline":
            raise FileNotFoundError(2, "no such process")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(cli_estate, "_proc_status", status)
    monkeypatch.setattr(cli_estate, "_proc_owner", lambda entry: owners.get(entry.name, 1))
    monkeypatch.setattr(os, "geteuid", lambda: 1)


def test_the_scan_skips_only_proven_absence_and_the_verbs_ancestors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A process gone between listing and reading is skipped. A permission or
    I/O error is not absence: the scan raises. The verb's parent chain (the
    operator's shell under `sudo -u dsl41 -s`) is not a writer; any other
    process of the uid is, and a zombie is not."""
    _fake_proc(
        monkeypatch,
        {
            "900": {"PPid": "1", "Uid": "1 1 1 1", "State": "S", "Name": "bash"},
            "901": FileNotFoundError(2, "gone"),
            "902": {"PPid": "1", "Uid": "1 1 1 1", "State": "Z", "Name": "zombie"},
            "903": {"PPid": "1", "Uid": "2 2 2 2", "State": "S", "Name": "other-user"},
            "904": {"PPid": "1", "Uid": "1 1 1 1", "State": "S", "Name": "sleep"},
        },
    )
    assert cli_estate.other_processes() == ["904 (sleep)"]
    _fake_proc(monkeypatch, {"905": PermissionError(13, "Permission denied")})
    with pytest.raises(OSError, match="cannot observe pid 905"):
        cli_estate.other_processes()


def test_hidepid_skips_only_another_uids_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/proc` mounted `hidepid`: another uid's directory is listed and its
    read denied. That process is not this account's writer. A denied read of
    a directory this uid owns is still a refusal."""
    denied = PermissionError(13, "Permission denied")
    _fake_proc(monkeypatch, {"906": denied}, owners={"906": 0})
    assert cli_estate.other_processes() == []
    _fake_proc(monkeypatch, {"907": denied}, owners={"907": 1})
    with pytest.raises(OSError, match="cannot observe pid 907"):
        cli_estate.other_processes()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
def test_the_process_scan_finds_a_child_of_this_uid() -> None:
    child = subprocess.Popen(["sleep", "30"])
    try:
        found = cli_estate.other_processes()
        assert found is not None and f"{child.pid} (sleep 30)" in found
        mine = (str(os.getpid()), str(os.getppid()))
        assert not any(entry.split(" ", 1)[0] in mine for entry in found)
    finally:
        child.kill()
        child.wait()


def test_supervise_list_refuses_a_connection_the_supervisor_drops(short_root: Path) -> None:
    """A supervisor that closes the connection mid-request: exit 2 with the
    prefixed line, no traceback."""
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(short_root / "supervisor.sock"))
    listener.listen(1)

    def drop() -> None:
        conn, _ = listener.accept()
        conn.close()

    dropper = threading.Thread(target=drop, daemon=True)
    dropper.start()
    try:
        result = CliRunner().invoke(
            app, ["supervise", "list", "--run-root", str(short_root)], catch_exceptions=False
        )
    finally:
        dropper.join(timeout=10)
        listener.close()
    assert result.exit_code == 2, result.output
    assert f"supervisor {short_root / 'supervisor.sock'}: " in result.output


# ------------------------------------------------------- a real supervisor


def _job_status(run_root: Path) -> str | None:
    answer = cli("query", "status", "--job", "a", "-S", str(run_root / "control.sock"))
    return json.loads(answer.stdout)["jobs"]["a"]["status"] if answer.returncode == 0 else None


def _receipts(run_root: Path) -> list[Path]:
    return sorted((run_root / "runs").rglob("receipt.json"))


def _listing(run_root: Path) -> dict[str, Any]:
    conn = SupervisorConn(run_root / "supervisor.sock")
    try:
        return conn.send({"cmd": "LIST"})
    finally:
        conn.close()


@pytest.fixture
def live_job(short_root: Path) -> Iterator[Path]:
    """A detached period-1 root under a real supervisor, with job `a` running
    and the engine stopped: the root a shape 1 estate stop meets. The engine
    attaches, as the example launcher's does (DL-396)."""
    run_root = short_root / "run"
    run_root.mkdir()
    supervisor = start_supervisor(run_root)
    try:
        with engine(
            short_root, jil_text=JIL, extra=["--detached", "--supervisor", "attach"]
        ) as running:
            started = cli(
                "sendevent",
                "FORCE_STARTJOB",
                "-J",
                "a",
                "-S",
                str(running.run_root / "control.sock"),
            )
            assert started.returncode == 0, started.stderr
            # RUNNING in the engine: the spawn is bound, so the engine's stop detaches it
            wait_for(lambda: _job_status(running.run_root) == "RUNNING", timeout_s=30)
        # the same estate and mode: a period never opens over live work whose closure
        # changed (period-model ss10.1)
        (short_root / "next.jil").write_text(JIL)
        assert [run["wrapper_alive"] for run in _listing(run_root)["runs"]] == [True]
        yield run_root
    finally:
        _stop_supervisor(run_root)
        supervisor.wait(timeout=30)


def test_an_offline_seal_leaves_a_carried_live_run_running(
    short_root: Path, live_job: Path
) -> None:
    """period-model ss8: an in-place detached boundary needs no drain. The
    offline sealer's teardown is a detach-stop, so the run it carries keeps
    running under the supervisor for the next period to reattach."""
    sealed = cli(
        "seal",
        "--run-root",
        str(live_job),
        "--next",
        str(short_root / "next.jil"),
        "--next-detached",
        "--force-seal",
    )
    assert sealed.returncode == 0, sealed.stderr
    assert [run["wrapper_alive"] for run in _listing(live_job)["runs"]] == [True]


def test_a_resume_between_the_seal_and_maintenance_is_refused_by_the_held_lock(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, host: QuiesceHost, live_job: Path
) -> None:
    """A4, against a real supervisor with a live job. The verb holds
    leader.lock from its start: a `run --resume --detached --supervisor
    attach` started after the seal stage and before maintenance exits 2
    and spawns nothing. The supervisor's own maintenance then refuses (this
    host has no qualified service cgroup), so the verb exits 2, prints the
    live run, and the job runs on; the lock and the lease are free, and the
    verb has not exited 0 while LIST answers."""
    run_root = live_job
    before = _receipts(run_root)
    refused_resumes: list[subprocess.CompletedProcess[str]] = []
    real = cli_estate.guarded_maintenance

    def resume_first(*args: Any, **kwargs: Any) -> Any:
        refused_resumes.append(
            cli(
                "run",
                "--resume",
                "--detached",
                "--supervisor",
                "attach",
                "--run-root",
                str(run_root),
                str(short_root / "estate.jil"),
            )
        )
        return real(*args, **kwargs)

    monkeypatch.setattr(cli_estate, "guarded_maintenance", resume_first)
    result = _quiesce(run_root, short_root / "next.jil", "--force-seal", "--next-detached")

    (resume,) = refused_resumes
    assert resume.returncode == 2, resume.stderr
    assert "is held by another engine" in resume.stderr
    assert _receipts(run_root) == before
    assert result.exit_code == 2, result.output
    stages = _stages(result.output)
    assert stages["seal"] == "ok" and stages["maintenance"] == "refused"
    assert "quiesce: live run: a run 1" in result.output, result.output
    assert _lock_is_free(run_root)
    listing = _listing(run_root)
    assert listing["lease"] is None
    assert [run["wrapper_alive"] for run in listing["runs"]] == [True]


def test_a_resume_in_the_handover_gap_makes_maintenance_refuse(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, host: QuiesceHost, live_job: Path
) -> None:
    """A4's gap, against a real supervisor: after the verb lets `leader.lock`
    go and before MAINTENANCE_STOP. A resume run there loses the lease to the
    verb and exits 2 before it opens the next period. A process that still
    holds the lock when the request lands makes the supervisor refuse: the
    verb exits 2, names the lock, keeps the seal and the audit, and gives the
    lease back. The job runs on."""
    run_root = live_job
    resumes: list[subprocess.CompletedProcess[str]] = []
    held: list[int] = []
    real = cli_estate.guarded_maintenance

    def in_the_gap(*args: Any, **kwargs: Any) -> Any:
        let_go = kwargs["before_stop"]

        def gap() -> None:
            let_go()
            resumes.append(
                cli(
                    "run",
                    "--resume",
                    "--detached",
                    "--supervisor",
                    "attach",
                    "--run-root",
                    str(run_root),
                    str(short_root / "estate.jil"),
                )
            )
            held.append(procid.flock_exclusive(run_root / "leader.lock"))

        return real(*args, **{**kwargs, "before_stop": gap})

    monkeypatch.setattr(cli_estate, "guarded_maintenance", in_the_gap)
    try:
        result = _quiesce(run_root, short_root / "next.jil", "--force-seal", "--next-detached")
    finally:
        for fd in held:
            os.close(fd)
    (resume,) = resumes
    assert resume.returncode == 2, resume.stderr
    assert not (run_root / "wal" / "000002.jsonl").exists()
    assert result.exit_code == 2, result.output
    assert _stages(result.output)["maintenance"] == "refused"
    assert f"the supervisor could not take {run_root / 'leader.lock'}" in result.output
    assert "durable: period 1 sealed, attested through period 1" in result.output
    listing = _listing(run_root)
    assert listing["lease"] is None
    assert [run["wrapper_alive"] for run in listing["runs"]] == [True]


@pytest.mark.parametrize("force,code", [("terminate", 0), ("kill", 2)])
def test_force_terminate_is_passed_to_maintenance_and_nothing_else_is_accepted(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, host: QuiesceHost, force: str, code: int
) -> None:
    run_root, next_jil = _stopped_root(short_root)
    supervisor = FakeSupervisor(run_root)
    _use(monkeypatch, host, connect=supervisor.connect)
    result = _quiesce(run_root, next_jil, "--force", force)
    assert result.exit_code == code, result.output
    stops = [request for request in supervisor.sent if request["cmd"] == "MAINTENANCE_STOP"]
    assert [request["force"] for request in stops] == ([force] if code == 0 else [])


# ------------------------------------------------------- a rerun meets an admitted operation

#: The supervisor of an admitted operation, as the rerun meets it: it holds leader.lock, as
#: maintenance does from admission to the exit (DL-390). Told to go, it writes its outcome
#: and exits; told nothing, it stays.
_OPERATING = """
import fcntl, os, sys, time
lock, release, outcome, record = sys.argv[1:]
fd = os.open(lock, os.O_RDWR)
fcntl.flock(fd, fcntl.LOCK_EX)
open(release + ".held", "w").close()
while not os.path.exists(release):
    time.sleep(0.01)
if outcome:
    os.replace(outcome, record)
"""


def _operating(
    run_root: Path, release: Path, state: str, bound_s: float = 30.0, **extra: Any
) -> tuple[Any, dict]:
    """A process holding leader.lock and the record that names it, in `state`. Its outcome,
    completed, replaces the record when `release` appears."""
    outcome = run_root / "maintenance.next"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _OPERATING,
            str(run_root / "leader.lock"),
            str(release),
            str(outcome) if state == "admitted" else "",
            str(run_root / "maintenance.json"),
        ]
    )
    wait_for(lambda: Path(str(release) + ".held").exists())
    start = procid.proc_start_token(proc.pid)
    record = {
        "version": 1,
        "state": state,
        "incarnation": "a" * 32,
        "supervisor_pid": proc.pid,
        "supervisor_start_time": start,
        "boot_id": procid.current_boot_id(),
        "force": "terminate",
        "bound_s": bound_s,
        "started_at": datetime.now(UTC).isoformat(),
        **extra,
    }
    (run_root / "maintenance.json").write_text(json.dumps(record))
    _note(run_root, proc.pid)
    outcome.write_text(json.dumps({**record, "state": "completed", "finished_at": "now"}))
    return proc, record


def _sealed_and_refused(short_root: Path, monkeypatch: pytest.MonkeyPatch, host: QuiesceHost):
    """A root the verb has sealed and audited, whose maintenance refused live work."""
    run_root, next_jil = _stopped_root(short_root)
    supervisor = FakeSupervisor(run_root)
    supervisor.runs = [
        {"run_id": "r-1", "job": "a", "run_number": 1, "wrapper_pid": 4242, "wrapper_alive": True}
    ]
    _use(monkeypatch, host, connect=supervisor.connect)
    assert _quiesce(run_root, next_jil).exit_code == 2
    supervisor.runs = []
    return run_root, next_jil, supervisor


def test_a_rerun_during_an_admitted_operation_watches_it_and_never_requests_again(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, host: QuiesceHost, sleeps: Sleeps
) -> None:
    """A client lost after its request, and the verb rerun (DL-401). The supervisor
    holds leader.lock for its operation, so leadership is skipped, and LIST shows the
    operation. The maintenance stage reads maintenance.json and waits the rest of the
    bound: the supervisor exits and records completed, and the verb ends 0 having sent
    no MAINTENANCE_STOP."""
    run_root, next_jil, supervisor = _sealed_and_refused(short_root, monkeypatch, host)
    sleeps.release = short_root / "release"
    proc, record = _operating(run_root, sleeps.release, "admitted")
    supervisor.pid = proc.pid
    real_send = supervisor.send

    def send(request: dict[str, Any]) -> dict[str, Any]:
        answer = real_send(request)
        if request["cmd"] == "LIST":
            answer["process_state"] = "maintenance_stopping"
        return answer

    supervisor.send = send  # type: ignore[method-assign]
    supervisor.sent.clear()
    try:
        result = _quiesce(run_root, next_jil)
    finally:
        sleeps.release.touch()
        proc.wait(timeout=30)
    assert result.exit_code == 0, result.output
    stages = _stages(result.output)
    assert stages["leadership"] == "skipped" and stages["maintenance"] == "ok"
    assert "the supervisor holds it for maintenance (DL-401)" in result.output
    assert f"completed; the supervisor (pid {proc.pid}) exited" in result.output
    assert [request["cmd"] for request in supervisor.sent] == ["LIST"]
    assert sleeps.slept and set(sleeps.slept) == {0.05}
    assert _lock_is_free(run_root)


def test_a_rerun_after_completion_skips_maintenance(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, host: QuiesceHost
) -> None:
    run_root, next_jil, supervisor = _sealed_and_refused(short_root, monkeypatch, host)
    release = short_root / "release"
    proc, record = _operating(run_root, release, "admitted")
    release.touch()
    proc.wait(timeout=30)
    supervisor.up = False
    supervisor.sent.clear()
    result = _quiesce(run_root, next_jil)
    assert result.exit_code == 0, result.output
    assert f"quiesce: maintenance: skipped: already completed; the supervisor (pid {proc.pid})" in (
        result.output
    )
    assert "MAINTENANCE_STOP" not in [request["cmd"] for request in supervisor.sent]


@pytest.mark.parametrize("case", ["uncertain", "unknown_version", "exited_admitted", "no_record"])
def test_a_rerun_that_cannot_prove_the_outcome_refuses(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, host: QuiesceHost, case: str
) -> None:
    """An uncertain record, a record this reader cannot trust, and a supervisor gone
    with its record still admitted are unknown: exit 4 with the runbook pointer, and no
    MAINTENANCE_STOP (DL-401). A held lock that no record names refuses at leadership,
    exit 2, as for any other holder."""
    run_root, next_jil, supervisor = _sealed_and_refused(short_root, monkeypatch, host)
    release = short_root / "release"
    state = "uncertain" if case == "uncertain" else "admitted"
    extra = {"finished_at": "now", "detail": "work remains"} if state == "uncertain" else {}
    proc, record = _operating(run_root, release, state, **extra)
    real_send = supervisor.send

    def send(request: dict[str, Any]) -> dict[str, Any]:
        answer = real_send(request)
        if request["cmd"] == "LIST":
            answer["process_state"] = "maintenance_uncertain"
        return answer

    if case in ("uncertain", "no_record"):
        supervisor.pid = proc.pid
        supervisor.send = send  # type: ignore[method-assign]
    else:
        release.touch()
        proc.wait(timeout=30)
        supervisor.up = False
        (run_root / "maintenance.json").write_text(json.dumps(record))
    if case == "unknown_version":
        (run_root / "maintenance.json").write_text(json.dumps({**record, "version": 2}))
    if case == "no_record":
        (run_root / "maintenance.json").unlink()
    supervisor.sent.clear()
    try:
        result = _quiesce(run_root, next_jil)
    finally:
        release.touch()
        proc.wait(timeout=30)
    assert result.exit_code == (2 if case == "no_record" else 4), result.output
    if case == "no_record":
        # the lock's holder is named by no record: leadership refuses, as for any holder
        assert _stages(result.output)["leadership"] == "refused"
        return
    assert _stages(result.output)["maintenance"] == "refused"
    assert "deployment-runbook ss3" in result.output
    assert "MAINTENANCE_STOP" not in [request["cmd"] for request in supervisor.sent]
    if case == "uncertain":
        assert "maintenance is uncertain: work remains" in result.output
    if case == "unknown_version":
        assert "has version 2" in result.output


def test_an_operation_that_holds_the_lock_on_an_unsealed_period_refuses_leadership(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, host: QuiesceHost
) -> None:
    """The seal needs leader.lock, and an admitted operation keeps it until its exit: on an
    unsealed period the verb refuses at leadership and names the operation, rather than run
    a seal without the lock (DL-401)."""
    run_root, next_jil = _stopped_root(short_root)
    supervisor = FakeSupervisor(run_root)
    _use(monkeypatch, host, connect=supervisor.connect)
    release = short_root / "release"
    proc, _record = _operating(run_root, release, "admitted")
    try:
        result = _quiesce(run_root, next_jil)
    finally:
        release.touch()
        proc.wait(timeout=30)
    assert result.exit_code == 2, result.output
    assert _stages(result.output) == {"hold": "ok", "engine": "ok", "leadership": "refused"}
    assert f"the supervisor (pid {proc.pid}) holds" in result.output
    assert "the period is not sealed; rerun once it has ended" in result.output
    assert supervisor.sent == []


def test_an_operation_still_running_when_the_wait_ends_is_exit_3(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, host: QuiesceHost
) -> None:
    """The rerun watches an admitted operation for the rest of its bound and the margin.
    One still running then is exit 3: it finishes on its own (DL-401)."""
    run_root, next_jil, supervisor = _sealed_and_refused(short_root, monkeypatch, host)
    release = short_root / "release"
    proc, _record = _operating(run_root, release, "admitted", bound_s=0.0)
    supervisor.pid = proc.pid
    real_send = supervisor.send

    def send(request: dict[str, Any]) -> dict[str, Any]:
        answer = real_send(request)
        if request["cmd"] in ("LIST", "PING"):
            answer["process_state"] = "maintenance_stopping"
        return answer

    supervisor.send = send  # type: ignore[method-assign]
    try:
        result = _quiesce(run_root, next_jil)
    finally:
        release.touch()
        proc.wait(timeout=30)
    assert result.exit_code == 3, result.output
    assert "quiesce: maintenance: refused: in progress; it finishes on its own" in result.output


def test_a_lost_reply_that_was_never_admitted_names_it(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, host: QuiesceHost
) -> None:
    """A lost reply resolved as "not admitted" is a refusal with its own cause (DL-401)."""
    run_root, next_jil = _stopped_root(short_root)
    supervisor = FakeSupervisor(run_root)
    real_send = supervisor.send

    def send(request: dict[str, Any]) -> dict[str, Any]:
        if request["cmd"] == "MAINTENANCE_STOP":
            supervisor.sent.append(request)
            raise OSError("synthetic lost reply")
        return real_send(request)

    supervisor.send = send  # type: ignore[method-assign]
    _use(monkeypatch, host, connect=supervisor.connect)
    result = _quiesce(run_root, next_jil)
    assert result.exit_code == 2, result.output
    assert (
        "quiesce: maintenance: refused: the reply was lost and the request was not admitted;"
        " nothing changed; rerun"
    ) in result.output
    cmds = [request["cmd"] for request in supervisor.sent]
    assert cmds.count("MAINTENANCE_STOP") == 1 and cmds[-2:] == ["ACQUIRE", "PING"]


def test_a_draining_supervisor_refuses_the_verb_before_the_seal(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, host: QuiesceHost
) -> None:
    """DL-416: a host signal is draining the supervisor. The verb refuses at the supervisor
    stage with exit 2 and the one sentence, before the seal and with nothing sent but LIST."""
    run_root, next_jil = _stopped_root(short_root)
    supervisor = FakeSupervisor(run_root)
    real_send = supervisor.send

    def send(request: dict[str, Any]) -> dict[str, Any]:
        return {**real_send(request), "process_state": "draining"}

    supervisor.send = send  # type: ignore[method-assign]
    _use(monkeypatch, host, connect=supervisor.connect)
    result = _quiesce(run_root, next_jil)
    assert result.exit_code == 2, result.output
    assert (
        "quiesce: supervisor: refused: supervisor is draining; wait for the unit to stop"
        in result.output
    )
    assert "seal" not in _stages(result.output)
    assert [request["cmd"] for request in supervisor.sent] == ["LIST"]


@pytest.mark.parametrize("lost", [False, True])
def test_a_drain_that_begins_before_maintenance_is_a_refusal(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, host: QuiesceHost, lost: bool
) -> None:
    """DL-416: the drain begins after the supervisor stage. MAINTENANCE_STOP is refused
    `draining`, or its reply is lost and PING shows `draining`; either is exit 2 with the one
    sentence, since a draining supervisor admits nothing, never 4."""
    run_root, next_jil = _stopped_root(short_root)
    supervisor = FakeSupervisor(run_root)
    real_send = supervisor.send
    draining = False

    def send(request: dict[str, Any]) -> dict[str, Any]:
        nonlocal draining
        if request["cmd"] == "MAINTENANCE_STOP":
            supervisor.sent.append(request)
            draining = True
            if lost:
                raise OSError("synthetic lost reply")
            return {"ok": False, "error": "draining"}
        answer = real_send(request)
        return {**answer, "process_state": "draining"} if draining else answer

    supervisor.send = send  # type: ignore[method-assign]
    _use(monkeypatch, host, connect=supervisor.connect)
    result = _quiesce(run_root, next_jil)
    assert result.exit_code == 2, result.output
    assert (
        "quiesce: maintenance: refused: supervisor is draining; wait for the unit to stop"
        in result.output
    )
    cmds = [request["cmd"] for request in supervisor.sent]
    assert cmds.count("MAINTENANCE_STOP") == 1


def test_a_later_supervisor_ended_by_its_deadman_voids_the_record_for_the_rerun(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, host: QuiesceHost
) -> None:
    """Supervisor A's record says completed. B starts on the root, notes itself, and exits
    by its deadman, which removes its socket and pid record. The rerun meets no supervisor
    and A's record: the owner rule runs before the skip, so it is 4, "not a maintenance
    outcome", never `skipped: already completed` (DL-401)."""
    from dsl41 import runner_supervisor as rs

    run_root, next_jil, supervisor = _sealed_and_refused(short_root, monkeypatch, host)
    release = short_root / "release"
    proc, _record = _operating(run_root, release, "admitted")
    release.touch()
    proc.wait(timeout=30)
    with monkeypatch.context() as patch:
        for name in ("_install_signals", "_set_subreaper", "_reap"):
            patch.setattr(rs.Supervisor, name, lambda self: None)
        later = rs.Supervisor(str(run_root), deadman_s=1e-9)
        assert later.run() == 0
    assert not (run_root / "supervisor.pid").exists()
    supervisor.up = False
    supervisor.sent.clear()
    result = _quiesce(run_root, next_jil)
    assert result.exit_code == 4, result.output
    assert _stages(result.output)["maintenance"] == "refused"
    assert f"names another supervisor ({later.incarnation})" in result.output
    assert "not a maintenance outcome" in result.output
    assert "already completed" not in result.output
