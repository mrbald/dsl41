"""The statechart core's hit recording, the registry, the session plugin and
the DL-42 boundary."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

import transition_hits_plugin as plugin
from dsl41 import state_machine as core
from dsl41.machines import MACHINES
from dsl41.state_machine import (
    HITS_ENV,
    Event,
    Executor,
    Region,
    Row,
    State,
    Statechart,
    well_formed,
)

LAMP = Statechart(
    "lamp",
    State("lamp", regions=(Region("main", "off", (State("off"), State("on"))),)),
    (Row("lamp.01", "off", "switch", "on"), Row("lamp.02", "on", "switch", "off")),
    clock="logical",
)


def _switch(times: int = 1) -> None:
    """Switch a fresh lamp `times` times."""
    executor = Executor()
    executor.restore("k", LAMP, {"lamp", "off"})
    for _ in range(times):
        executor.dispatch("k", Event("switch"))


def _lines(path: Path) -> list[dict[str, str]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


ON = {"machine": "lamp", "kind": "row", "id": "lamp.01", "detail": ""}
OFF = {"machine": "lamp", "kind": "row", "id": "lamp.02", "detail": ""}


@pytest.fixture
def recording(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Recording into a private directory, so the hits stay out of the session."""
    directory = tmp_path / "hits"
    directory.mkdir()
    monkeypatch.setenv(HITS_ENV, str(directory))
    return directory


# ---- recording


def test_nothing_is_written_when_the_variable_is_unset_or_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(HITS_ENV, raising=False)
    _switch()
    monkeypatch.setenv(HITS_ENV, "")
    _switch()
    assert list(tmp_path.iterdir()) == []


def test_a_hit_is_one_json_line_in_a_per_pid_file(recording: Path) -> None:
    _switch()
    assert [p.name for p in recording.iterdir()] == [f"hits-{os.getpid()}.jsonl"]
    assert _lines(recording / f"hits-{os.getpid()}.jsonl") == [ON]


def test_a_repeated_hit_is_written_once_and_a_new_item_is_a_new_line(recording: Path) -> None:
    _switch(3)
    assert _lines(recording / f"hits-{os.getpid()}.jsonl") == [ON, OFF]


def test_the_dedupe_is_per_directory(
    recording: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch()
    second = tmp_path / "second"
    second.mkdir()
    monkeypatch.setenv(HITS_ENV, str(second))
    _switch()
    assert _lines(second / f"hits-{os.getpid()}.jsonl") == [ON]


def test_a_killed_process_loses_no_hit(recording: Path) -> None:
    """The write is closed before the dispatch returns, so SIGKILL right after
    loses nothing."""
    code = (
        "import os, signal\n"
        "from test_state_machine import _switch\n"
        "_switch()\n"
        "os.kill(os.getpid(), signal.SIGKILL)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent)}
    proc = subprocess.run([sys.executable, "-c", code], env=env, check=False, timeout=60)
    assert proc.returncode == -signal.SIGKILL
    files = [p for p in recording.iterdir() if p.name != f"hits-{os.getpid()}.jsonl"]
    assert len(files) == 1 and files[0].name.startswith("hits-")
    assert _lines(files[0]) == [ON]


def test_a_missing_directory_does_not_break_a_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(HITS_ENV, str(tmp_path / "gone"))
    _switch()
    assert not (tmp_path / "gone").exists()


# ---- the registry


def test_every_registered_machine_is_well_formed() -> None:
    for machine in MACHINES:
        assert well_formed(machine) == [], machine.name


def test_registered_machine_names_are_unique() -> None:
    names = [machine.name for machine in MACHINES]
    assert len(names) == len(set(names))


# ---- the session plugin


@dataclass
class _Config:
    """A stub `pytest.Config`; a pytest-xdist worker's carries `workerinput`."""

    workerinput: dict[str, Any] | None = None


@dataclass
class _Session:
    exitstatus: int = 0
    config: _Config = field(default_factory=_Config)


def test_the_plugin_merges_hits_and_keeps_a_clean_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session starts and finishes around two hits, with a private report file."""
    report = tmp_path / "report.json"
    monkeypatch.setattr(plugin, "HITS_FILE", report)
    monkeypatch.setattr(plugin, "_directory", None)
    monkeypatch.setenv(HITS_ENV, "outer")
    session = _Session()
    plugin.pytest_configure(session.config)  # type: ignore[arg-type]
    directory = os.environ[HITS_ENV]
    assert Path(directory).is_dir()
    _switch(2)
    plugin.pytest_sessionfinish(session, 0)  # type: ignore[arg-type]
    assert HITS_ENV not in os.environ
    assert not Path(directory).exists()
    assert json.loads(report.read_text(encoding="utf-8")) == {"hits": [ON, OFF]}
    assert session.exitstatus == 0


def test_finishing_a_session_that_never_started_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(plugin, "HITS_FILE", tmp_path / "report.json")
    monkeypatch.setattr(plugin, "_directory", None)
    monkeypatch.setenv(HITS_ENV, "outer")
    plugin.pytest_sessionfinish(_Session(), 0)  # type: ignore[arg-type]
    assert not (tmp_path / "report.json").exists()


def test_a_worker_neither_starts_nor_finishes_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under pytest-xdist the controller owns the directory and the report. A
    worker keeps the directory it inherited, leaves the controller's report in
    place, writes none of its own and leaves the session's status alone."""
    report = tmp_path / "report.json"
    report.write_text('{"hits": []}', encoding="utf-8")
    monkeypatch.setattr(plugin, "HITS_FILE", report)
    monkeypatch.setattr(plugin, "_directory", str(tmp_path))
    monkeypatch.setenv(HITS_ENV, str(tmp_path))
    worker = _Session(config=_Config(workerinput={"workerid": "gw0"}))
    plugin.pytest_configure(worker.config)  # type: ignore[arg-type]
    assert os.environ[HITS_ENV] == str(tmp_path) and report.exists()
    _switch()
    plugin.pytest_sessionfinish(worker, 0)  # type: ignore[arg-type]
    plugin.pytest_unconfigure(worker.config)  # type: ignore[arg-type]
    assert os.environ[HITS_ENV] == str(tmp_path)
    assert report.read_text(encoding="utf-8") == '{"hits": []}'
    assert plugin._directory == str(tmp_path) and tmp_path.is_dir()
    assert worker.exitstatus == 0


def test_merge_dedupes_sorts_and_skips_a_torn_or_non_object_line(tmp_path: Path) -> None:
    one = {"machine": "m", "kind": "row", "id": "m.02", "detail": ""}
    two = {"machine": "m", "kind": "row", "id": "m.01", "detail": ""}
    (tmp_path / "hits-1.jsonl").write_text(
        json.dumps(one) + "\n" + json.dumps(two) + "\n" + '{"machine": "m", "id"', encoding="utf-8"
    )
    (tmp_path / "hits-2.jsonl").write_text(json.dumps(one) + "\n[1]\n", encoding="utf-8")
    assert plugin.merge(tmp_path) == [two, one]


# ---- the DL-42 boundary


def test_the_core_imports_only_the_standard_library() -> None:
    from test_runner_lifecycle import module_imports

    assert sorted(module_imports(Path(core.__file__)) - set(sys.stdlib_module_names)) == []


def test_the_core_loads_by_path_without_the_package() -> None:
    """The supervisor reaches canon with its own directory on sys.path; do the same."""
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(Path(core.__file__).parent)!r})\n"
        "import state_machine as sm\n"
        "root = sm.State('c', regions=(sm.Region('r', 'a', (sm.State('a'), sm.State('b'))),))\n"
        "rows = (sm.Row('c.01', 'a', 'go', 'b'), sm.Row('c.02', 'c', 'go', kind='ignored'))\n"
        "chart = sm.Statechart('c', root, rows, clock='monotonic')\n"
        "assert sm.well_formed(chart) == []\n"
        "ex = sm.Executor()\n"
        "ex.restore('k', chart, {'c', 'a'})\n"
        "assert [o.rows for o in ex.dispatch('k', sm.Event('go'))] == [('c.01',)]\n"
        "assert not any(name.split('.')[0] == 'dsl41' for name in sys.modules)\n"
    )
    env = {k: v for k, v in os.environ.items() if k != HITS_ENV}
    proc = subprocess.run(
        [sys.executable, "-I", "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


# ---- plugin hardening


def test_configure_removes_a_stale_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    report = tmp_path / "report.json"
    report.write_text('{"hits": []}', encoding="utf-8")
    monkeypatch.setattr(plugin, "HITS_FILE", report)
    monkeypatch.setattr(plugin, "_directory", None)
    monkeypatch.setenv(HITS_ENV, "outer")
    plugin.pytest_configure(_Config())  # type: ignore[arg-type]
    try:
        assert not report.exists()
    finally:
        plugin.pytest_sessionfinish(_Session(), 0)  # type: ignore[arg-type]


def test_unconfigure_after_no_session_removes_the_directory_and_the_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`pytest --help` configures and starts no session: unconfigure removes
    the directory and clears the env. After a finished session it does nothing."""
    monkeypatch.setattr(plugin, "HITS_FILE", tmp_path / "report.json")
    monkeypatch.setattr(plugin, "_directory", None)
    monkeypatch.setenv(HITS_ENV, "outer")
    plugin.pytest_configure(_Config())  # type: ignore[arg-type]
    directory = os.environ[HITS_ENV]
    assert Path(directory).is_dir()
    plugin.pytest_unconfigure(_Config())  # type: ignore[arg-type]
    assert not Path(directory).exists() and plugin._directory is None
    assert HITS_ENV not in os.environ
    assert not (tmp_path / "report.json").exists()
    plugin.pytest_unconfigure(_Config())  # type: ignore[arg-type]
    assert HITS_ENV not in os.environ


def test_the_report_is_written_whole_and_leaves_no_temp_file(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text("old", encoding="utf-8")
    plugin.write_report(report, [{"a": "1"}])
    assert json.loads(report.read_text(encoding="utf-8")) == {"hits": [{"a": "1"}]}
    assert [p.name for p in tmp_path.iterdir()] == ["report.json"]


def test_a_failed_report_write_leaves_the_old_file_and_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = tmp_path / "report.json"
    report.write_text("old", encoding="utf-8")

    def boom(src: str, dst: Path) -> None:
        raise OSError("no rename")

    monkeypatch.setattr(plugin.os, "replace", boom)
    with pytest.raises(OSError):
        plugin.write_report(report, [])
    assert report.read_text(encoding="utf-8") == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["report.json"]
