"""How a detached launch gets its supervisor: `attach` or `spawn` (DL-396).

`SupervisorClient.connect` attaches and forks nothing; `provision` is the one
method that may fork. `dsl41 run --detached` spawns by default and attaches
with `--supervisor attach`. The offline `dsl41 seal` attaches by default and
spawns only with `--supervisor spawn`. Under attach, absence and a transport
lost before the lease is held are retryable (run exits 1); a supervisor that
answers and refuses is not (exit 2).

Every refusal below has its passing counterpart beside it: an attach that
refuses proves nothing on its own, because a build where attach never worked
would refuse the same way.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from dsl41.cli import app
from dsl41.ir import lower_source
from dsl41.period import RuntimeProfile, runtime_profile_from_cli
from dsl41.runner_adapters import SupervisorClient
from dsl41.runner_startup import wire_from_profile

from test_runner_leadership import engine
from test_runner_supervisor import shutdown_unmanaged_supervisor, start_supervisor

C1_JIL = "insert_job: a\njob_type: c\ncommand: sleep 600\n"
C2_JIL = C1_JIL + "\ninsert_job: b\njob_type: c\ncommand: echo two\n"

runner = CliRunner()


def _invoke(*args: str) -> Any:
    return runner.invoke(app, list(args), catch_exceptions=False)


def _dsl41(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "dsl41", *args], capture_output=True, text=True, timeout=60
    )


def _stop_supervisor(run_root: Path) -> None:
    """Best effort: the legacy SHUTDOWN, then a kill of whatever pid the root
    still records. A teardown, so a failed assertion leaks no supervisor."""
    try:
        shutdown_unmanaged_supervisor(run_root, controller_id="launch-teardown")
    except Exception:  # noqa: BLE001 -- the fallback below, then the test's own failure
        with contextlib.suppress(OSError, ValueError, KeyError):
            pid = json.loads((run_root / "supervisor.pid").read_text())["pid"]
            os.kill(pid, signal.SIGKILL)


class _Supervised(list[Path]):
    """The run roots whose supervisors a test owns, and the handles of those
    it started itself."""

    def __init__(self) -> None:
        super().__init__()
        self.procs: list[subprocess.Popen[bytes]] = []

    def start(self, run_root: Path, deadman_s: float | None = None) -> int:
        """Register `run_root`, then start a supervisor there; its pid."""
        self.append(run_root)
        self.procs.append(start_supervisor(run_root, deadman_s=deadman_s))
        return self.procs[-1].pid


@pytest.fixture
def supervised() -> Iterator[_Supervised]:
    """Every supervisor the test owns is stopped and reaped at teardown,
    whatever the test did."""
    owned = _Supervised()
    yield owned
    for root in owned:
        _stop_supervisor(root)
    for proc in owned.procs:
        if proc.poll() is None:  # pragma: no cover -- only a failed shutdown
            proc.kill()
            proc.wait(timeout=30)


@pytest.fixture
def spawns(monkeypatch: pytest.MonkeyPatch, supervised: _Supervised) -> Iterator[list[Path]]:
    """Every supervisor fork this process makes, by run root. The real spawn
    still runs, so a `spawn` launch works as it does in production, and each
    fork's root is registered for teardown before it is made."""
    forked: list[Path] = []
    real = SupervisorClient._spawn_supervisor

    def recording(self: SupervisorClient) -> subprocess.Popen[bytes]:
        forked.append(self.run_root)
        supervised.append(self.run_root)
        return real(self)

    monkeypatch.setattr(SupervisorClient, "_spawn_supervisor", recording)
    yield forked


def _detached_period_1(base: Path, supervised: _Supervised) -> Path:
    """A stopped period-1 root whose pin says detached, deadman 600 s, with
    no supervisor: the root an offline seal meets after the supervisor unit
    went down. A real `dsl41 run --detached` makes it, so the pin is the one
    a launch writes; the test then stops the engine and its supervisor."""
    run_root = base / "run"
    supervised.append(run_root)
    with engine(base, jil_text=C1_JIL, extra=["--detached", "--deadman", "600"]):
        pass
    shutdown_unmanaged_supervisor(run_root, controller_id="period-1-fixture")
    assert _no_supervisor_files(run_root)
    return run_root


def _next_estate(base: Path) -> Path:
    c2 = base / "c2.jil"
    c2.write_text(C2_JIL)
    return c2


def _no_supervisor_files(run_root: Path) -> bool:
    return (
        not (run_root / "supervisor.pid").exists() and not (run_root / "supervisor.sock").exists()
    )


def _started_lines(run_root: Path) -> list[str]:
    log = run_root / "supervisor.log"
    lines = log.read_text().splitlines() if log.exists() else []
    return [line for line in lines if line.startswith("supervisor: started")]


# ------------------------------------------------------- the offline seal


def test_offline_seal_attaches_by_default_and_refuses_with_no_supervisor(
    short_root: Path, spawns: list[Path], supervised: _Supervised
) -> None:
    """A detached root with no supervisor: the seal exits 2, names a remedy
    that starts no unmanaged supervisor, forks nothing and leaves no
    supervisor file behind."""
    c2 = _next_estate(short_root)
    run_root = _detached_period_1(short_root, supervised)

    refused = _invoke("seal", "--run-root", str(run_root), "--next", str(c2))

    assert refused.exit_code == 2, refused.output
    assert (
        f"no supervisor answers on {run_root}; start the supervisor service, or pass"
        " --supervisor spawn to start one in this session"
    ) in refused.output
    assert "supervise start" not in refused.output
    assert spawns == []
    assert _no_supervisor_files(run_root)
    assert not (run_root / "seals").exists() or not any((run_root / "seals").iterdir())


def test_offline_seal_with_supervisor_spawn_forks_one_with_the_pinned_deadman(
    short_root: Path, spawns: list[Path], supervised: _Supervised
) -> None:
    """The counterpart: `--supervisor spawn` is the development path. It
    forks one supervisor, which runs the deadman the period pinned, and the
    boundary commits."""
    c2 = _next_estate(short_root)
    run_root = _detached_period_1(short_root, supervised)
    sealed = _invoke(
        "seal", "--run-root", str(run_root), "--next", str(c2), "--supervisor", "spawn"
    )
    assert sealed.exit_code == 0, sealed.output
    assert spawns == [run_root]
    listing = _dsl41("supervise", "list", "--run-root", str(run_root))
    assert listing.returncode == 0, listing.stderr
    assert json.loads(listing.stdout)["deadman_s"] == 600.0


def test_offline_exact_retry_of_a_committed_seal_needs_no_supervisor(
    short_root: Path, spawns: list[Path], supervised: _Supervised
) -> None:
    """period-model ss2.2 under attach: the boundary committed, then the
    supervisor went down. The exact retry is answered from the lineage on
    disk, exit 0, with nothing forked; it never reaches the wiring."""
    c2 = _next_estate(short_root)
    run_root = _detached_period_1(short_root, supervised)
    request = ["--request-id", "retry-after-commit"]
    first = _invoke(
        "seal", "--run-root", str(run_root), "--next", str(c2), "--supervisor", "spawn", *request
    )
    assert first.exit_code == 0, first.output
    shutdown_unmanaged_supervisor(run_root, controller_id="retry-fixture")
    assert _no_supervisor_files(run_root)

    retried = _invoke("seal", "--run-root", str(run_root), "--next", str(c2), *request)

    assert retried.exit_code == 0, retried.output
    assert "was already closed by request_id retry-after-commit" in retried.output
    # no recovery ran on this path, so the answer does not claim period 2 is open
    assert "period 2 is ready to open" in retried.output
    assert "has opened" not in retried.output
    assert spawns == [run_root]  # the first seal's fork only
    assert _no_supervisor_files(run_root)


# ------------------------------------------------------- dsl41 run


def test_run_supervisor_attach_with_no_supervisor_exits_1_and_forks_nothing(
    short_root: Path, spawns: list[Path]
) -> None:
    """Exit 1, not 2: the engine unit lists 2 in `RestartPreventExitStatus`,
    and an attach that found no supervisor is the one the unit retries."""
    jil = short_root / "estate.jil"
    jil.write_text(C1_JIL)
    run_root = short_root / "run"

    result = _invoke(
        "run", "--run-root", str(run_root), "--detached", "--supervisor", "attach", str(jil)
    )

    assert result.exit_code == 1, result.output
    assert f"no supervisor answers on {run_root}" in result.output
    assert "start the supervisor service" in result.output
    assert spawns == []
    assert _no_supervisor_files(run_root)


def test_run_supervisor_attach_reads_a_stale_socket_file_as_absence(
    short_root: Path, spawns: list[Path]
) -> None:
    """A `supervisor.sock` left by a killed supervisor refuses the connect.
    That is absence, exit 1, and the file stays: only a supervisor unlinks it."""
    jil = short_root / "estate.jil"
    jil.write_text(C1_JIL)
    run_root = short_root / "run"
    run_root.mkdir(mode=0o700)
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(run_root / "supervisor.sock"))
    stale.close()

    result = _invoke(
        "run", "--run-root", str(run_root), "--detached", "--supervisor", "attach", str(jil)
    )

    assert result.exit_code == 1, result.output
    assert f"no supervisor answers on {run_root}" in result.output
    assert spawns == []
    assert (run_root / "supervisor.sock").exists()


def test_run_supervisor_needs_detached(short_root: Path, spawns: list[Path]) -> None:
    jil = short_root / "estate.jil"
    jil.write_text(C1_JIL)
    run_root = short_root / "run"
    for choice in ("attach", "spawn"):
        refused = _invoke("run", "--run-root", str(run_root), "--supervisor", choice, str(jil))
        assert refused.exit_code == 2, refused.output
        assert "--supervisor needs --detached" in refused.output
    assert spawns == []
    assert not run_root.exists()


def test_run_supervisor_attach_connects_to_the_running_supervisor(
    short_root: Path, supervised: _Supervised
) -> None:
    """The counterpart: with a supervisor up, the engine attaches to it and
    takes its lease. The root's log records one supervisor start and no
    second start refused against its lock."""
    run_root = short_root / "run"
    pid = supervised.start(run_root)
    with engine(short_root, extra=["--detached", "--supervisor", "attach"]) as up:
        listing = _dsl41("supervise", "list", "--run-root", str(run_root))
        assert listing.returncode == 0, listing.stderr
        answer = json.loads(listing.stdout)
        assert answer["supervisor_pid"] == pid
        assert answer["lease"] is not None
        assert str(run_root.resolve()) in answer["lease"]["holder"]
        assert up.proc.poll() is None
    assert json.loads((run_root / "supervisor.pid").read_text())["pid"] == pid
    assert len(_started_lines(run_root)) == 1
    assert "another supervisor owns this root" not in (run_root / "supervisor.log").read_text()


@pytest.mark.parametrize("mode", ["attach", "spawn"])
def test_only_spawn_reaches_provision(
    short_root: Path, supervised: _Supervised, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """With a supervisor up, both modes fork nothing, so a fork count cannot
    tell them apart. The method can: attach wires through `connect` and never
    calls `provision`, the client's one forking method; spawn calls it."""
    run_root = short_root / "run"
    supervised.start(run_root)
    provisioned: list[Path] = []
    real = SupervisorClient.provision

    async def recording(self: SupervisorClient) -> None:
        provisioned.append(self.run_root)
        await real(self)

    monkeypatch.setattr(SupervisorClient, "provision", recording)
    catalog = lower_source(C1_JIL)
    profile = RuntimeProfile(execution_mode="detached")

    async def wire() -> None:
        wiring = await wire_from_profile(
            run_root,
            catalog,
            profile,
            start=datetime.now(UTC).replace(tzinfo=None),
            supervisor=mode,  # type: ignore[arg-type]
        )
        await wiring.close()

    asyncio.run(wire())
    assert provisioned == ([] if mode == "attach" else [run_root])
    assert len(_started_lines(run_root)) == 1


def test_resume_supervisor_attach_waits_for_the_supervisor_and_then_resumes(
    short_root: Path, spawns: list[Path], supervised: _Supervised
) -> None:
    """`--resume` honours the option. With no supervisor the resume exits 1
    and forks nothing; once one runs at the pinned deadman, the identical
    command resumes and attaches to it."""
    run_root = _detached_period_1(short_root, supervised)
    jil = short_root / "estate.jil"
    argv = ["run", "--resume", "--run-root", str(run_root), "--detached"]

    refused = _invoke(*argv, "--supervisor", "attach", str(jil))
    assert refused.exit_code == 1, refused.output
    assert f"no supervisor answers on {run_root}" in refused.output
    assert spawns == []
    assert _no_supervisor_files(run_root)

    pid = supervised.start(run_root, deadman_s=600)
    with engine(short_root, resume=True, extra=["--detached", "--supervisor", "attach"]) as up:
        listing = _dsl41("supervise", "list", "--run-root", str(run_root))
        assert listing.returncode == 0, listing.stderr
        answer = json.loads(listing.stdout)
        assert answer["supervisor_pid"] == pid
        assert answer["lease"] is not None
        assert up.proc.poll() is None
    assert spawns == []


# ------------------------------------------- attach: lost versus refused


def _peer(
    on_acquire: Callable[[asyncio.StreamWriter], Awaitable[None]],
    *,
    ping: dict[str, Any] | None = None,
) -> Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]:
    """A socket peer that answers PING with `ping` and hands ACQUIRE to
    `on_acquire`; it answers RELEASE and anything else with ok."""
    answer = ping or {"ok": True, "version": 1, "incarnation": "inc-1", "deadman_s": None}

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while line := await reader.readline():
                cmd = json.loads(line).get("cmd")
                if cmd == "ACQUIRE":
                    await on_acquire(writer)
                    if writer.is_closing():
                        return
                    continue
                reply = answer if cmd == "PING" else {"ok": True}
                writer.write(json.dumps(reply).encode() + b"\n")
                await writer.drain()
        except OSError:
            pass
        finally:
            writer.close()

    return handle


async def _hang_up(writer: asyncio.StreamWriter) -> None:
    writer.close()


async def _lease_held(writer: asyncio.StreamWriter) -> None:
    refusal = {"ok": False, "error": "lease_held", "holder": "engine:other", "expires_at": "t"}
    writer.write(json.dumps(refusal).encode() + b"\n")
    await writer.drain()


def _serve_against(
    run_root: Path,
    handle: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]],
) -> int:
    """`dsl41 run --detached --supervisor attach`'s body against `handle`."""
    from dsl41.cli_run import _serve_run

    run_root.mkdir(mode=0o700)

    async def scenario() -> int:
        server = await asyncio.start_unix_server(handle, path=str(run_root / "supervisor.sock"))
        try:
            return await _serve_run(
                lower_source(C1_JIL),
                run_root,
                False,
                [],
                profile=runtime_profile_from_cli(detached=True),
                supervisor="attach",
            )
        finally:
            server.close()

    return asyncio.run(scenario())


def test_attach_reads_a_connection_lost_during_acquire_as_absence(
    short_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A supervisor that answers PING and drops the connection on ACQUIRE is
    gone before the lease is held: the same retryable exit 1 as absence."""
    code = _serve_against(short_root / "run", _peer(_hang_up))
    assert code == 1
    assert "no supervisor answers on" in capsys.readouterr().err


def test_attach_refuses_a_lease_another_controller_holds_with_2(
    short_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The counterpart: a supervisor that answers ACQUIRE with a refusal is
    present. A retry cannot fix that, so it stays exit 2."""
    code = _serve_against(short_root / "run", _peer(_lease_held))
    assert code == 2
    err = capsys.readouterr().err
    assert "lease acquire refused" in err and "lease_held" in err


def test_attach_refuses_an_incompatible_supervisor_with_2(
    short_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A supervisor that answers PING with `unsupported_version` is present
    and incompatible, a configuration error: exit 2, naming the version."""
    code = _serve_against(
        short_root / "run",
        _peer(_lease_held, ping={"ok": False, "error": "unsupported_version"}),
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "refused PING" in err and "protocol version 1" in err
    assert "no supervisor answers" not in err
