"""`dsl41 run` on a run root (cli_run._serve_run): a root that holds `engine.db` resumes
whether or not `--resume` is given, so a supervisor rerunning the same command line restarts
the engine; `--resume` is an accepted no-op; a missing root is created and starts a new run."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from genesis_harness import C1_JIL, _catalog

from dsl41 import runner_startup
from dsl41.cli_run import _serve_run
from dsl41.runner_clock import EngineError
from dsl41.runner_config import RuntimeProfile


def _serve_until_built(
    run_root: Path, resume: bool, monkeypatch: pytest.MonkeyPatch
) -> tuple[int, list[str]]:
    """Run `_serve_run` up to the engine it builds; answer the exit code and which path it
    took. The built engine is closed and the run refused, so nothing serves."""
    taken: list[str] = []
    real_start, real_resume = runner_startup.start_run, runner_startup.resume_run

    def start(*args: Any, **kwargs: Any) -> Any:
        taken.append("start")
        engine = real_start(*args, **kwargs)
        engine.journal.close()
        raise EngineError("test: stop once genesis is done")

    async def resume(*args: Any, **kwargs: Any) -> Any:
        taken.append("resume")
        engine = await real_resume(*args, **kwargs)
        await engine.shutdown()
        engine.journal.close()
        raise EngineError("test: stop once the restart is done")

    monkeypatch.setattr(runner_startup, "start_run", start)
    monkeypatch.setattr(runner_startup, "resume_run", resume)
    catalog, _ = _catalog(C1_JIL)
    code = asyncio.run(_serve_run(catalog, run_root, resume, [], profile=RuntimeProfile()))
    return code, taken


def test_an_existing_root_resumes_without_the_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_root = tmp_path / "run"
    assert _serve_until_built(run_root, False, monkeypatch)[1] == ["start"]  # the first run
    code, taken = _serve_until_built(run_root, False, monkeypatch)
    assert (code, taken) == (2, ["resume"])


def test_the_resume_flag_is_still_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_root = tmp_path / "run"
    assert _serve_until_built(run_root, False, monkeypatch)[1] == ["start"]  # the first run
    capsys.readouterr()
    code, taken = _serve_until_built(run_root, True, monkeypatch)
    assert (code, taken) == (2, ["resume"])
    assert "stop once the restart is done" in capsys.readouterr().err
    fresh = tmp_path / "fresh"  # the flag on a root with no store starts a new run
    assert _serve_until_built(fresh, True, monkeypatch)[1] == ["start"]


def test_a_missing_root_is_created_and_starts_a_new_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_root = tmp_path / "not" / "yet"
    code, taken = _serve_until_built(run_root, False, monkeypatch)
    assert (code, taken) == (2, ["start"])
    assert (run_root / "engine.db").exists()


def test_a_refused_root_gains_no_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """spec-wiring 1b: `engine.toml` is written only once the root's refusals have passed. A 1.x
    root and a store of another state version are refused, and neither gains the file."""
    import sqlite3
    from contextlib import closing

    catalog, _ = _catalog(C1_JIL)
    old = tmp_path / "old"
    old.mkdir()
    (old / "journal.jsonl").write_text("")
    code = asyncio.run(_serve_run(catalog, old, False, [], profile=RuntimeProfile()))
    assert code == 2
    assert sorted(path.name for path in old.iterdir()) == ["journal.jsonl", "leader.lock"]

    run_root = tmp_path / "run"
    assert _serve_until_built(run_root, False, monkeypatch)[1] == ["start"]
    assert not (run_root / "engine.toml").exists()  # the stand-in refusal came after genesis
    with closing(sqlite3.connect(run_root / "engine.db", isolation_level=None)) as db:
        db.execute("UPDATE meta SET value = '999' WHERE key = 'state_version'")
    monkeypatch.undo()
    code = asyncio.run(_serve_run(catalog, run_root, False, [], profile=RuntimeProfile()))
    assert code == 2
    assert not (run_root / "engine.toml").exists()


def _engine_records(run_root: Path) -> list[str]:
    from store_surgery import rows

    return [body["event"] for _, kind, body in rows(run_root) if kind == "engine"]


def test_an_engine_that_never_reaches_running_records_shutdown_during_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """spec-wiring 1b: an engine stopped before it reached RUNNING records
    `shutdown_during_startup` and takes no snapshot; one that reached it records its shutdown
    and snapshots."""
    import sqlite3
    from contextlib import closing

    from dsl41 import cli_run

    async def never(*_args: Any) -> bool:
        return False

    catalog, _ = _catalog(C1_JIL)
    run_root = tmp_path / "run"
    monkeypatch.setattr(cli_run, "_await_started", never)
    assert asyncio.run(_serve_run(catalog, run_root, False, [], profile=RuntimeProfile())) == 0
    assert _engine_records(run_root) == ["started", "shutdown_during_startup"]
    with closing(sqlite3.connect(run_root / "engine.db")) as db:
        assert db.execute("SELECT COUNT(*) FROM snapshots").fetchone() == (0,)
    assert (run_root / "engine.toml").exists()  # the refusals had passed


def test_the_socket_is_bound_only_after_the_restart_records_engine_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """spec-wiring 1b step 8: `engine started` is recorded once steps 6 and 7 took effect, and
    the control socket is bound after it. A bind that fails leaves an engine that never
    reached RUNNING: `shutdown_during_startup`, no snapshot."""
    from dsl41.runner_control import ControlServer

    seen: list[list[str]] = []

    async def start(self: ControlServer) -> None:
        seen.append(_engine_records(run_root))
        raise EngineError("test: the bind fails")

    catalog, _ = _catalog(C1_JIL)
    run_root = tmp_path / "run"
    monkeypatch.setattr(ControlServer, "start", start)
    assert asyncio.run(_serve_run(catalog, run_root, False, [], profile=RuntimeProfile())) == 2
    assert asyncio.run(_serve_run(catalog, run_root, False, [], profile=RuntimeProfile())) == 2
    # genesis and the restart each bound only once `engine started` was durable
    assert seen == [["started"], ["started", "shutdown_during_startup", "started"]]
    assert _engine_records(run_root)[-1] == "shutdown_during_startup"
