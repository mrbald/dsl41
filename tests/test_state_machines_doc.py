"""The generated state-machine doc, scripts/render_state_machines.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from dsl41.state_machine import (
    COMPLETION,
    Branch,
    Choice,
    Region,
    Row,
    State,
    Statechart,
    TimeEvent,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "render_state_machines.py"


def _load() -> ModuleType:
    """scripts/ is not a package, so the test loads the script by path."""
    spec = importlib.util.spec_from_file_location("render_state_machines", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["render_state_machines"] = module
    spec.loader.exec_module(module)
    return module


rsm = _load()


def test_the_committed_doc_is_the_rendering() -> None:
    """Breaks when a machine changed and nobody ran
    `uv run python scripts/render_state_machines.py`."""
    assert rsm.is_current(REPO_ROOT), (
        "docs/state-machines.md is stale; run `uv run python scripts/render_state_machines.py`"
    )


# ---- statecharts

CHART = Statechart(
    "door",
    State(
        "door",
        regions=(
            Region(
                "main",
                "closed",
                (
                    State("closed", entry="light"),
                    State(
                        "opened",
                        regions=(
                            Region("r1", "wide", (State("wide"), State("shut", final=True))),
                            Region(
                                "r2",
                                "alarm",
                                (State("alarm", wait=frozenset({"t"}), stays="waits"),),
                            ),
                        ),
                        defer=frozenset({"lock", "bell"}),
                    ),
                ),
            ),
        ),
    ),
    (
        Row("door.01", "closed", "open", "which", guard="badge", action="log", cite="DL-1"),
        Row("door.02", "opened", "close", "closed"),
        Row("door.03", "door", "lock", kind="ignored"),
        Row("door.04", "door", "bell", kind="refusal", code="quiet"),
        Row("door.05", "wide", COMPLETION, "shut"),
    ),
    choices=(
        Choice(
            "which",
            (Branch("door.11", "opened", guard="ok", action="ring"), Branch("door.12", "closed")),
        ),
    ),
    time_events=(TimeEvent("t", generation=True), TimeEvent("u")),
    clock="logical",
    defer_bound=3,
    overflow="drop",
)


def test_a_statechart_renders_regions_choices_and_deferrals() -> None:
    lines = rsm.chart_diagram(CHART)
    # sorted names: alarm closed door opened shut wide -> s0..s5
    assert lines[:4] == [
        "```mermaid",
        "stateDiagram-v2",
        '    state "door" as s2',
        "    state s2 {",
    ]
    assert "        [*] --> s1" in lines
    assert "        state s3 {" in lines
    assert "            --" in lines
    assert "            s4 --> [*]" in lines
    assert "    state c0 <<choice>>" in lines
    assert "    s1 --> c0 : door.01 open [badge] / log" in lines
    assert "    c0 --> s3 : door.11 [ok] / ring" in lines
    assert "    c0 --> s1 : door.12 [else]" in lines
    assert "    s5 --> s4 : door.05" in lines
    assert "    note right of s3 : defer bell, lock" in lines
    assert not any("door.03" in line or "door.04" in line for line in lines)


def test_a_statechart_table_lists_rows_branches_and_states() -> None:
    rows = rsm.chart_table(CHART)
    assert rows[2] == "| door.01 | closed | open | badge | log | which | external | DL-1 |"
    assert "| door.04 | door | bell |  |  |  | refusal quiet |  |" in rows
    assert "| door.05 | wide | (completion) |  |  | shut | external |  |" in rows
    assert "| door.12 | which | else |  | closed |" in rows
    assert "| opened | door/main |  |  | bell, lock |  |" in rows
    assert "| shut (final) | opened/r1 |  |  |  |  |" in rows
    assert "| alarm | opened/r2 |  |  |  | t (stays: waits) |" in rows
    assert "| closed | door/main | light |  |  |  |" in rows


def test_a_statechart_gets_a_summary_line_and_its_own_section() -> None:
    assert rsm.chart_summary(CHART) == (
        "Clock: logical. Deferred queue: 3, overflow drop. Time events: t (generation), u."
    )
    bare = Statechart("bare", State("bare"), (), clock="")
    assert rsm.chart_summary(bare) == "Clock: undeclared."
    text = rsm.render([CHART, bare])
    assert "No state machines are declared yet." not in text
    assert "\n## door\n\nClock: logical." in text and "\n## bare\n" in text
    assert text.count("```mermaid\nstateDiagram-v2\n") == 2
    assert rsm.chart_table(bare)[-1] == "| bare |  |  |  |  |  |"


def _ready(_ctx: object) -> bool:
    """The door is ready;
    a second line."""
    return True


def test_a_statechart_lists_its_guards_and_actions_with_their_docstrings() -> None:
    documented = Statechart(
        "lock",
        State("lock"),
        (Row("lock.01", "lock", "go", kind="internal", guard="ready", action="note"),),
        guards={"ready": _ready},
        actions={"note": lambda _ctx: None},
        clock="logical",
    )
    assert rsm.chart_table(documented)[-4:] == [
        "| Name | Kind | Prose |",
        "| --- | --- | --- |",
        "| ready | guard | The door is ready, a second line. |",
        "| note | action |  |",
    ]
    assert rsm.chart_table(CHART)[-1] == "| alarm | opened/r2 |  |  |  | t (stays: waits) |"


# ---- staleness


def test_a_matching_doc_is_current(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "state-machines.md").write_text(rsm.render([CHART]), encoding="utf-8")
    assert rsm.is_current(tmp_path, [CHART])


def test_a_changed_machine_makes_the_doc_stale(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "state-machines.md").write_text(rsm.render([CHART]), encoding="utf-8")
    assert not rsm.is_current(tmp_path, [])


def test_a_missing_doc_is_not_current(tmp_path: Path) -> None:
    assert not rsm.is_current(tmp_path, [CHART])


def test_an_empty_registry_says_so() -> None:
    text = rsm.render([])
    assert text.startswith("# State machines\n")
    assert "No state machines are declared yet." in text
    assert "```mermaid" not in text


def test_a_state_name_is_never_a_mermaid_id_and_a_quote_is_softened() -> None:
    """`end` and `note` are keywords and `a-b` does not parse, so names are
    labels; a double quote in a name would end its label."""
    odd = Statechart(
        "odd",
        State(
            "odd",
            regions=(Region("main", "end", (State("end"), State("a-b"), State('q"b'))),),
        ),
        (Row("odd.01", "end", "x", "a-b"), Row("odd.02", "a-b", "y", 'q"b')),
        clock="logical",
    )
    lines = rsm.chart_diagram(odd)
    # sorted names: a-b end odd q"b -> s0..s3
    assert '        state "a-b" as s0' in lines and '        state "q\'b" as s3' in lines
    assert "    s1 --> s0 : odd.01 x" in lines and "    s0 --> s3 : odd.02 y" in lines
    for raw in ("end -->", "--> end", "a-b -->", "--> a-b"):
        assert not any(raw in line for line in lines)
