"""The generated state-machine doc, scripts/render_state_machines.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from dsl41.state_machine import StateMachine, Transition

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

MACHINE = StateMachine(
    name="demo",
    states=frozenset({"a", "b", "c"}),
    initial="a",
    finals=frozenset({"c"}),
    transitions=(
        Transition(
            "demo.01",
            frozenset({"b", "a"}),
            "go",
            "c",
            guard="ready",
            effect="start; log",
            cite="DL-1",
        ),
        Transition(
            "demo.02",
            frozenset({"a"}),
            "status",
            frozenset({"b", "c"}),
            effect="a | b",
            mark="spec-only",
        ),
    ),
)


def test_the_committed_doc_is_the_rendering() -> None:
    """Breaks when a machine changed and nobody ran
    `uv run python scripts/render_state_machines.py`."""
    assert rsm.is_current(REPO_ROOT), (
        "docs/state-machines.md is stale; run `uv run python scripts/render_state_machines.py`"
    )


def test_a_matching_doc_is_current(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "state-machines.md").write_text(rsm.render([MACHINE]), encoding="utf-8")
    assert rsm.is_current(tmp_path, [MACHINE])


def test_a_changed_machine_makes_the_doc_stale(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "state-machines.md").write_text(rsm.render([MACHINE]), encoding="utf-8")
    assert not rsm.is_current(tmp_path, [])


def test_a_missing_doc_is_not_current(tmp_path: Path) -> None:
    assert not rsm.is_current(tmp_path, [MACHINE])


def test_an_empty_registry_says_so() -> None:
    text = rsm.render([])
    assert text.startswith("# State machines\n")
    assert "No state machines are declared yet." in text
    assert "```mermaid" not in text


def test_a_machine_has_a_heading_a_diagram_and_a_table() -> None:
    text = rsm.render([MACHINE])
    assert "No state machines are declared yet." not in text
    assert "\n## demo\n" in text
    assert text.count("```mermaid\nstateDiagram-v2\n") == 1
    assert "| Id | Source | Trigger | Guard | Effect | Target | Cite | Mark |" in text


def test_the_diagram_has_initial_final_and_one_edge_per_source() -> None:
    lines = rsm.diagram(MACHINE)  # sorted names: a, b, c -> s0, s1, s2
    assert "    [*] --> s0" in lines
    assert "    s2 --> [*]" in lines
    assert "    s0 --> s2 : demo.01 go [ready] / start, log" in lines
    assert "    s1 --> s2 : demo.01 go [ready] / start, log" in lines


def test_a_set_target_goes_through_its_own_choice_node() -> None:
    lines = rsm.diagram(MACHINE)
    assert "    state c1 <<choice>>" in lines
    assert "    s0 --> c1 : demo.02 status / a | b" in lines
    assert "    c1 --> s1" in lines and "    c1 --> s2" in lines
    assert not any("<<choice>>" in line for line in rsm.diagram(_bare()))


def _bare() -> StateMachine[str]:
    return StateMachine("plain", frozenset({"a"}), None, frozenset(), ())


def test_a_machine_without_initial_or_finals_draws_neither() -> None:
    lines = rsm.diagram(_bare())
    assert lines == ["```mermaid", "stateDiagram-v2", '    state "a" as s0', "```"]


def test_the_edge_label_omits_an_empty_guard_and_effect() -> None:
    assert rsm.edge_label(Transition("demo.09", frozenset({"a"}), "tick", "a")) == "demo.09 tick"


def test_the_table_row_lists_every_column() -> None:
    rows = rsm.table(MACHINE)
    assert rows[2] == "| demo.01 | a, b | go | ready | start, log | c | DL-1 |  |"
    assert rows[3] == "| demo.02 | a | status |  | a \\| b | b, c |  | spec-only |"


LAMP = StateMachine(
    name="lamp",
    states=frozenset({"off", "on", "broken"}),
    initial="off",
    finals=frozenset({"broken"}),
    transitions=(
        Transition("lamp.01", frozenset({"off"}), "switch", "on", effect="light"),
        Transition("lamp.02", frozenset({"on"}), "check", "on", guard="bulb ok"),
        Transition("lamp.03", frozenset({"on", "off"}), "surge", frozenset({"off", "broken"})),
    ),
)


def test_the_mermaid_text_is_exact() -> None:
    assert "\n".join(rsm.diagram(LAMP)) == "\n".join(
        [
            "```mermaid",
            "stateDiagram-v2",
            '    state "broken" as s0',
            '    state "off" as s1',
            '    state "on" as s2',
            "    state c2 <<choice>>",
            "    [*] --> s1",
            "    s1 --> s2 : lamp.01 switch / light",
            "    s2 --> s2 : lamp.02 check [bulb ok]",
            "    s1 --> c2 : lamp.03 surge",
            "    s2 --> c2 : lamp.03 surge",
            "    c2 --> s0",
            "    c2 --> s1",
            "    s0 --> [*]",
            "```",
        ]
    )
    assert rsm.table(LAMP)[4] == "| lamp.03 | off, on | surge |  |  | broken, off |  |  |"


def test_a_state_name_is_never_a_mermaid_id() -> None:
    """`end` and `note` are keywords and `a-b` does not parse, so names are labels."""
    machine = StateMachine(
        name="odd",
        states=frozenset({"end", "a-b", "note", "state"}),
        initial="end",
        finals=frozenset({"note"}),
        transitions=(
            Transition("odd.01", frozenset({"end"}), "x", "a-b"),
            Transition("odd.02", frozenset({"a-b"}), "y", frozenset({"note", "state"})),
        ),
    )
    lines = rsm.diagram(machine)
    assert lines[2:6] == [
        '    state "a-b" as s0',
        '    state "end" as s1',
        '    state "note" as s2',
        '    state "state" as s3',
    ]
    assert "    s1 --> s0 : odd.01 x" in lines
    assert "    state c1 <<choice>>" in lines
    assert "    s0 --> c1 : odd.02 y" in lines
    for raw in ("end -->", "--> end", "a-b -->", "--> a-b", "note -->", "--> note"):
        assert not any(raw in line for line in lines)


def test_each_set_target_gets_its_own_choice_node() -> None:
    machine = StateMachine(
        name="twin",
        states=frozenset({"a", "b", "c"}),
        initial="a",
        finals=frozenset(),
        transitions=(
            Transition("twin.01", frozenset({"a"}), "x", frozenset({"b", "c"})),
            Transition("twin.02", frozenset({"a"}), "y", frozenset({"b", "c"})),
        ),
    )
    nodes = [line for line in rsm.diagram(machine) if "<<choice>>" in line]
    assert nodes == ["    state c0 <<choice>>", "    state c1 <<choice>>"]


def test_a_state_named_like_a_choice_node_does_not_collide() -> None:
    machine = StateMachine(
        name="clash",
        states=frozenset({"c0", "a", "b"}),
        initial="a",
        finals=frozenset(),
        transitions=(Transition("clash.01", frozenset({"a"}), "x", frozenset({"b", "c0"})),),
    )
    lines = rsm.diagram(machine)
    assert '    state "c0" as s2' in lines
    assert "    state c0 <<choice>>" in lines


def test_a_quote_in_a_state_name_is_softened() -> None:
    machine = StateMachine("q", frozenset({'a"b'}), None, frozenset(), ())
    assert '    state "a\'b" as s0' in rsm.diagram(machine)


def test_an_enum_state_renders_by_its_value() -> None:
    from enum import Enum

    class Phase(str, Enum):
        a = "a"
        b = "b"

    machine = StateMachine(
        "e",
        frozenset(Phase),
        Phase.a,
        frozenset({Phase.b}),
        (Transition("e.01", frozenset({Phase.a}), "go", Phase.b),),
    )
    lines = rsm.diagram(machine)
    assert "    s0 --> s1 : e.01 go" in lines and '    state "a" as s0' in lines
    assert rsm.table(machine)[2] == "| e.01 | a | go |  |  | b |  |  |"
