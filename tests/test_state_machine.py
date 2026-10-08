"""The shared state-machine core: take, hit recording, well-formedness, the session plugin."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any

import pytest

import transition_hits_plugin as plugin
from dsl41 import state_machine as core
from dsl41.machines import MACHINES
from dsl41.state_machine import (
    HITS_ENV,
    STRICT_ENV,
    StateMachine,
    Transition,
    TransitionError,
    well_formed,
)

T_AB = Transition(
    "demo.01", frozenset({"a"}), "go", "b", guard="ready", effect="start", cite="DL-0"
)
T_TO_C = Transition("demo.02", frozenset({"a", "b"}), "stop", "c")
T_SET = Transition("demo.03", frozenset({"b"}), "status", frozenset({"a", "c"}))
MACHINE = StateMachine(
    name="demo",
    states=frozenset({"a", "b", "c"}),
    initial="a",
    finals=frozenset({"c"}),
    transitions=(T_AB, T_TO_C, T_SET),
)


def _lines(path: Path) -> list[dict[str, str]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production: neither variable set."""
    monkeypatch.delenv(HITS_ENV, raising=False)
    monkeypatch.delenv(STRICT_ENV, raising=False)


@pytest.fixture
def recording(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Recording into a private directory, not strict, so a violation stays out of the session."""
    directory = tmp_path / "hits"
    directory.mkdir()
    monkeypatch.setenv(HITS_ENV, str(directory))
    monkeypatch.delenv(STRICT_ENV, raising=False)
    return directory


# ---- take: the check


def test_a_declared_move_passes(off: None) -> None:
    assert MACHINE.take(T_AB, "a", "b") is None
    assert MACHINE.take(T_TO_C, "b", "c") is None


def test_a_set_target_accepts_each_member(off: None) -> None:
    assert MACHINE.take(T_SET, "b", "a") is None
    assert MACHINE.take(T_SET, "b", "c") is None


def test_a_set_target_refuses_a_state_outside_the_set(off: None) -> None:
    violation = MACHINE.take(T_SET, "b", "b")
    assert violation is not None
    assert "b is not one of the targets of demo.03" in violation.reason


def test_a_transition_of_another_machine_is_a_violation(off: None) -> None:
    other = replace(T_AB, id="demo.09")
    violation = MACHINE.take(other, "a", "b")
    assert violation is not None
    assert (violation.machine, violation.transition) == ("demo", "demo.09")
    assert "not declared" in violation.reason


def test_a_wrong_source_is_a_violation(off: None) -> None:
    violation = MACHINE.take(T_AB, "b", "b")
    assert violation is not None
    assert (violation.old, violation.new) == ("b", "b")
    assert "not a source" in violation.reason


def test_a_wrong_fixed_target_is_a_violation(off: None) -> None:
    violation = MACHINE.take(T_AB, "a", "c")
    assert violation is not None
    assert "targets b, not c" in violation.reason


def test_take_returns_the_violation_and_does_not_raise_in_production(off: None) -> None:
    assert MACHINE.take(T_AB, "c", "a") is not None


# ---- take: recording


def test_nothing_is_written_when_the_variable_is_unset(
    off: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    MACHINE.take(T_AB, "a", "b")
    MACHINE.take(T_AB, "b", "b")
    assert list(tmp_path.iterdir()) == []


def test_an_empty_variable_counts_as_unset(
    off: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(HITS_ENV, "")
    monkeypatch.chdir(tmp_path)
    MACHINE.take(T_AB, "a", "b")
    assert list(tmp_path.iterdir()) == []


def test_a_hit_is_one_json_line_in_a_per_pid_file(recording: Path) -> None:
    assert MACHINE.take(T_AB, "a", "b") is None
    assert [p.name for p in recording.iterdir()] == [f"hits-{os.getpid()}.jsonl"]
    assert _lines(recording / f"hits-{os.getpid()}.jsonl") == [
        {"machine": "demo", "id": "demo.01", "old": "a", "new": "b"}
    ]


def test_a_repeated_hit_is_written_once(recording: Path) -> None:
    for _ in range(3):
        MACHINE.take(T_AB, "a", "b")
    assert len(_lines(recording / f"hits-{os.getpid()}.jsonl")) == 1


def test_a_new_tuple_is_a_new_line(recording: Path) -> None:
    MACHINE.take(T_TO_C, "a", "c")
    MACHINE.take(T_TO_C, "b", "c")
    MACHINE.take(T_SET, "b", "a")
    MACHINE.take(T_SET, "b", "c")
    olds_news = [
        (h["id"], h["old"], h["new"]) for h in _lines(recording / f"hits-{os.getpid()}.jsonl")
    ]
    assert olds_news == [
        ("demo.02", "a", "c"),
        ("demo.02", "b", "c"),
        ("demo.03", "b", "a"),
        ("demo.03", "b", "c"),
    ]


def test_the_dedupe_is_per_directory(
    recording: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    MACHINE.take(T_AB, "a", "b")
    second = tmp_path / "second"
    second.mkdir()
    monkeypatch.setenv(HITS_ENV, str(second))
    MACHINE.take(T_AB, "a", "b")
    assert (second / f"hits-{os.getpid()}.jsonl").is_file()


def test_a_violation_is_written_every_time_and_is_not_a_hit(recording: Path) -> None:
    MACHINE.take(T_AB, "b", "b")
    MACHINE.take(T_AB, "b", "b")
    lines = _lines(recording / f"violations-{os.getpid()}.jsonl")
    assert len(lines) == 2
    assert lines[0]["machine"] == "demo" and lines[0]["id"] == "demo.01"
    assert (lines[0]["old"], lines[0]["new"]) == ("b", "b")
    assert "not a source" in lines[0]["reason"]
    assert not (recording / f"hits-{os.getpid()}.jsonl").exists()


def test_a_killed_process_loses_no_hit(recording: Path) -> None:
    """The write is closed before take returns, so SIGKILL right after loses nothing."""
    code = (
        "import os, signal\n"
        "from test_state_machine import MACHINE, T_AB\n"
        "MACHINE.take(T_AB, 'a', 'b')\n"
        "os.kill(os.getpid(), signal.SIGKILL)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent)}
    proc = subprocess.run([sys.executable, "-c", code], env=env, check=False, timeout=60)
    assert proc.returncode == -signal.SIGKILL
    files = [p for p in recording.iterdir() if p.name != f"hits-{os.getpid()}.jsonl"]
    assert len(files) == 1 and files[0].name.startswith("hits-")
    assert _lines(files[0]) == [{"machine": "demo", "id": "demo.01", "old": "a", "new": "b"}]


# ---- take: strict


def test_strict_raises_on_a_violation(off: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(STRICT_ENV, "1")
    with pytest.raises(TransitionError, match="demo demo.01"):
        MACHINE.take(T_AB, "b", "b")


def test_strict_does_not_raise_on_a_good_move(off: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(STRICT_ENV, "1")
    assert MACHINE.take(T_AB, "a", "b") is None


def test_strict_records_the_violation_before_it_raises(
    recording: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(STRICT_ENV, "1")
    with pytest.raises(TransitionError):
        MACHINE.take(T_AB, "b", "b")
    assert len(_lines(recording / f"violations-{os.getpid()}.jsonl")) == 1


def test_recording_without_strict_does_not_raise(recording: Path) -> None:
    assert MACHINE.take(T_AB, "b", "b") is not None


# ---- well_formed


def _machine(**changes: Any) -> StateMachine[str]:
    return replace(MACHINE, **changes)


def test_a_well_formed_machine_has_no_problems() -> None:
    assert well_formed(MACHINE) == []


def test_a_duplicate_id_is_reported() -> None:
    problems = well_formed(_machine(transitions=(T_AB, replace(T_TO_C, id="demo.01"), T_SET)))
    assert problems == ["duplicate transition id demo.01"]


def test_a_bad_machine_name_is_reported() -> None:
    for name in ("Demo", "demo1", "1demo", "de-mo", "", "_x"):
        renamed = [replace(t, id=f"{name}.01") for t in MACHINE.transitions[:1]]
        machine = _machine(name=name, transitions=(*renamed, *MACHINE.transitions[1:]))
        assert f"machine name {name!r} does not match [a-z][a-z_]*" in well_formed(machine)
    assert well_formed(_machine(name="job_status", transitions=_ids("job_status"))) == []


def _ids(name: str) -> tuple[Transition[str], ...]:
    return tuple(replace(t, id=f"{name}.{i:02d}") for i, t in enumerate(MACHINE.transitions, 1))


def test_an_id_must_carry_its_own_machine_name_and_two_or_three_digits() -> None:
    good = _machine(transitions=(replace(T_AB, id="demo.123"), T_TO_C, T_SET))
    assert well_formed(good) == []
    for bad in ("T1", "demo.1", "demo.1234", "demo.0a", "other.01", "demo01", "demo.", "demo.01x"):
        problems = well_formed(_machine(transitions=(replace(T_AB, id=bad), T_TO_C, T_SET)))
        assert problems == [f"transition id {bad!r} is not demo.<two or three digits>"], bad


def test_an_unknown_source_is_reported() -> None:
    bad = replace(T_AB, source=frozenset({"a", "z"}))
    assert well_formed(_machine(transitions=(bad, T_TO_C, T_SET))) == [
        "demo.01: source z is not a declared state"
    ]


def test_an_unknown_target_is_reported() -> None:
    bad = replace(T_AB, target="z")
    problems = well_formed(_machine(transitions=(bad, T_TO_C, T_SET)))
    assert "demo.01: target z is not a declared state" in problems


def test_an_unknown_initial_is_reported() -> None:
    assert "initial state z is not a declared state" in well_formed(_machine(initial="z"))


def test_an_unknown_final_is_reported() -> None:
    problems = well_formed(_machine(finals=frozenset({"c", "z"})))
    assert problems == ["final state z is not a declared state"]


def test_an_exit_from_a_final_state_is_reported() -> None:
    leaving = Transition("demo.04", frozenset({"c"}), "again", "a")
    problems = well_formed(_machine(transitions=(*MACHINE.transitions, leaving)))
    assert problems == ["demo.04: final state c has an outgoing transition"]


def test_an_unreachable_state_is_reported() -> None:
    problems = well_formed(_machine(states=frozenset({"a", "b", "c", "d"})))
    assert problems == ["state d is not reachable from a"]


def test_a_set_target_reaches_every_member() -> None:
    via_set = _machine(transitions=(T_AB, T_SET))
    assert well_formed(via_set) == []


def test_a_state_outside_every_target_is_unreachable() -> None:
    narrow = replace(T_SET, target=frozenset({"a"}))
    assert well_formed(_machine(transitions=(T_AB, narrow))) == ["state c is not reachable from a"]


def test_an_empty_target_set_is_reported() -> None:
    empty = replace(T_SET, target=frozenset())
    problems = well_formed(_machine(transitions=(T_AB, T_TO_C, empty)))
    assert problems == ["demo.03: the target set is empty"]


def test_a_set_member_that_is_not_declared_is_reported() -> None:
    bad = replace(T_SET, target=frozenset({"a", "z"}))
    problems = well_formed(_machine(transitions=(T_AB, T_TO_C, bad)))
    assert problems == ["demo.03: target z is not a declared state"]


def test_reachability_is_skipped_without_an_initial_state() -> None:
    assert well_formed(_machine(initial=None, states=frozenset({"a", "b", "c", "d"}))) == []


def test_every_registered_machine_is_well_formed() -> None:
    for machine in MACHINES:
        assert well_formed(machine) == [], machine.name


def test_registered_machine_names_are_unique() -> None:
    names = [machine.name for machine in MACHINES]
    assert len(names) == len(set(names))


# ---- the session plugin


@dataclass
class _Session:
    exitstatus: int = 0


def _run_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, take: Any, status: int = 0
) -> tuple[_Session, dict[str, Any]]:
    """Start and finish a plugin session around `take`, with a private report file."""
    report = tmp_path / "report.json"
    monkeypatch.setattr(plugin, "HITS_FILE", report)
    monkeypatch.setattr(plugin, "_directory", None)
    monkeypatch.setenv(HITS_ENV, "outer")
    monkeypatch.setenv(STRICT_ENV, "outer")
    session = _Session(status)
    plugin.pytest_sessionstart(session)  # type: ignore[arg-type]
    directory = os.environ[HITS_ENV]
    assert Path(directory).is_dir() and os.environ[STRICT_ENV] == "1"
    take()
    plugin.pytest_sessionfinish(session, status)  # type: ignore[arg-type]
    assert HITS_ENV not in os.environ and STRICT_ENV not in os.environ
    assert not Path(directory).exists()
    return session, json.loads(report.read_text(encoding="utf-8"))


def test_the_plugin_merges_hits_and_keeps_a_clean_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def take() -> None:
        MACHINE.take(T_TO_C, "b", "c")
        MACHINE.take(T_AB, "a", "b")

    session, report = _run_session(tmp_path, monkeypatch, take)
    assert report == {
        "hits": [
            {"id": "demo.01", "machine": "demo", "new": "b", "old": "a"},
            {"id": "demo.02", "machine": "demo", "new": "c", "old": "b"},
        ],
        "violations": [],
    }
    assert session.exitstatus == 0


def test_the_plugin_fails_the_session_on_a_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def take() -> None:
        with pytest.raises(TransitionError):
            MACHINE.take(T_AB, "b", "b")

    session, report = _run_session(tmp_path, monkeypatch, take)
    assert [v["id"] for v in report["violations"]] == ["demo.01"]
    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED
    assert "demo demo.01: b -> b" in capsys.readouterr().err


def test_the_plugin_keeps_an_existing_failure_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def take() -> None:
        with pytest.raises(TransitionError):
            MACHINE.take(T_AB, "b", "b")

    session, _ = _run_session(tmp_path, monkeypatch, take, status=2)
    assert session.exitstatus == 2


def test_finishing_a_session_that_never_started_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(plugin, "HITS_FILE", tmp_path / "report.json")
    monkeypatch.setattr(plugin, "_directory", None)
    monkeypatch.setenv(HITS_ENV, "outer")
    monkeypatch.setenv(STRICT_ENV, "outer")
    plugin.pytest_sessionfinish(_Session(), 0)  # type: ignore[arg-type]
    assert not (tmp_path / "report.json").exists()


def test_merge_dedupes_sorts_and_skips_a_torn_line(tmp_path: Path) -> None:
    one = {"machine": "m", "id": "demo.02", "old": "a", "new": "b"}
    two = {"machine": "m", "id": "demo.01", "old": "a", "new": "b"}
    (tmp_path / "hits-1.jsonl").write_text(
        json.dumps(one) + "\n" + json.dumps(two) + "\n" + '{"machine": "m", "id"', encoding="utf-8"
    )
    (tmp_path / "hits-2.jsonl").write_text(json.dumps(one) + "\n", encoding="utf-8")
    hits, violations = plugin.merge(tmp_path)
    assert hits == [two, one]
    assert violations == []


# ---- recording never raises


def test_a_missing_directory_does_not_break_a_good_take(
    off: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(HITS_ENV, str(tmp_path / "gone"))
    assert MACHINE.take(T_AB, "a", "b") is None


def test_a_missing_directory_does_not_break_a_violation(
    off: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(HITS_ENV, str(tmp_path / "gone"))
    assert MACHINE.take(T_AB, "b", "b") is not None


def test_strict_still_raises_the_violation_when_its_write_fails(
    off: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(HITS_ENV, str(tmp_path / "gone"))
    monkeypatch.setenv(STRICT_ENV, "1")
    with pytest.raises(TransitionError, match="demo demo.01"):
        MACHINE.take(T_AB, "b", "b")


def test_an_unwritable_file_is_dropped_and_the_hit_is_retried(recording: Path) -> None:
    """A directory stands where the file belongs, so the open fails. The failed hit is
    not remembered, and the next take writes it."""
    blocked = recording / f"hits-{os.getpid()}.jsonl"
    blocked.mkdir()
    assert MACHINE.take(T_AB, "a", "b") is None
    blocked.rmdir()
    assert MACHINE.take(T_AB, "a", "b") is None
    assert _lines(blocked) == [{"machine": "demo", "id": "demo.01", "old": "a", "new": "b"}]


def test_an_unwritable_violation_file_does_not_raise(recording: Path) -> None:
    (recording / f"violations-{os.getpid()}.jsonl").mkdir()
    assert MACHINE.take(T_AB, "b", "b") is not None


# ---- Enum states


class Phase(str, Enum):
    a = "a"
    b = "b"


E_AB = Transition("enumed.01", frozenset({Phase.a}), "go", Phase.b)
E_MACHINE = StateMachine(
    name="enumed",
    states=frozenset(Phase),
    initial=Phase.a,
    finals=frozenset({Phase.b}),
    transitions=(E_AB,),
)


def test_an_enum_state_is_recorded_by_its_value(recording: Path) -> None:
    assert E_MACHINE.take(E_AB, Phase.a, Phase.b) is None
    assert _lines(recording / f"hits-{os.getpid()}.jsonl") == [
        {"machine": "enumed", "id": "enumed.01", "old": "a", "new": "b"}
    ]


def test_an_enum_violation_names_its_values(recording: Path) -> None:
    violation = E_MACHINE.take(E_AB, Phase.b, Phase.b)
    assert violation is not None
    assert (violation.old, violation.new) == ("b", "b")
    assert violation.reason == "b is not a source of enumed.01"


def test_state_name_uses_value_for_an_enum_and_str_otherwise() -> None:
    assert core.state_name(Phase.a) == "a"
    assert core.state_name("plain") == "plain"
    assert well_formed(E_MACHINE) == []


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
        "t = sm.Transition('m.01', frozenset({'a'}), 'go', 'b')\n"
        "m = sm.StateMachine('m', frozenset({'a', 'b'}), 'a', frozenset(), (t,))\n"
        "assert m.take(t, 'a', 'b') is None and sm.well_formed(m) == []\n"
        "assert m.take(t, 'b', 'b') is not None\n"
        "assert not any(name.split('.')[0] == 'dsl41' for name in sys.modules)\n"
    )
    env = {k: v for k, v in os.environ.items() if k not in (HITS_ENV, STRICT_ENV)}
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


def test_a_torn_violation_line_counts_as_a_violation(tmp_path: Path) -> None:
    (tmp_path / "violations-7.jsonl").write_text('{"machine": "m", "id"\n', encoding="utf-8")
    hits, violations = plugin.merge(tmp_path)
    assert hits == []
    assert [v["reason"] for v in violations] == ["unparsable line in violations-7.jsonl"]


def test_a_non_object_violation_line_counts_as_a_violation(tmp_path: Path) -> None:
    (tmp_path / "violations-7.jsonl").write_text("[1]\n", encoding="utf-8")
    assert len(plugin.merge(tmp_path)[1]) == 1


def test_session_start_removes_a_stale_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = tmp_path / "report.json"
    report.write_text('{"hits": [], "violations": []}', encoding="utf-8")
    monkeypatch.setattr(plugin, "HITS_FILE", report)
    monkeypatch.setattr(plugin, "_directory", None)
    monkeypatch.setenv(HITS_ENV, "outer")
    monkeypatch.setenv(STRICT_ENV, "outer")
    plugin.pytest_sessionstart(_Session())  # type: ignore[arg-type]
    try:
        assert not report.exists()
    finally:
        plugin.pytest_sessionfinish(_Session(), 0)  # type: ignore[arg-type]


def test_the_report_is_written_whole_and_leaves_no_temp_file(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text("old", encoding="utf-8")
    plugin.write_report(report, [{"a": "1"}], [])
    assert json.loads(report.read_text(encoding="utf-8")) == {
        "hits": [{"a": "1"}],
        "violations": [],
    }
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
        plugin.write_report(report, [], [])
    assert report.read_text(encoding="utf-8") == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["report.json"]
