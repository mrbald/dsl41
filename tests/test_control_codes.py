"""The stable codes of control-protocol ss2: one registry, one table, one
helper.

The registry (`runner_codes.CODES`) and the code table in
docs/control-protocol.md must hold the same set, or a client reading the
contract branches on a code the engine never sends. Every `ok: false`
answer the server builds goes through `_failure`, which requires a code;
any other way of writing `ok: false` or `refused` in the server module
would be a door that could answer without one.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

import dsl41.runner_control as control
from dsl41.runner_clock import EngineError
from dsl41.runner_codes import CODES, STORED_CODES, UNKNOWN_OUTCOME_CODES
from dsl41.runner_control import ControlServer, _failure
from test_boundary import C2_JIL, _close, _genesis, _seal_request_wire, _stage

ROOT = Path(__file__).resolve().parent.parent
TABLE_HEADER = "| code | outcome class | reasons | client action |"


def _table_codes() -> list[str]:
    lines = (ROOT / "docs" / "control-protocol.md").read_text(encoding="utf-8").splitlines()
    start = lines.index(TABLE_HEADER)
    codes: list[str] = []
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        first = line.split("|")[1].strip()
        assert first.startswith("`") and first.endswith("`"), line
        codes.append(first.strip("`"))
    return codes


def test_the_code_table_and_the_registry_hold_the_same_set() -> None:
    codes = _table_codes()
    assert len(codes) == len(set(codes)), "a code listed twice"
    assert set(codes) == CODES


def test_the_stored_codes_are_registry_codes_and_never_an_unknown_outcome() -> None:
    """The append-only subset a rejected `decision` may store: the
    gates' verdicts, all inside the registry, none of them `unknown`."""
    assert STORED_CODES <= CODES
    assert not STORED_CODES & UNKNOWN_OUTCOME_CODES
    assert len(STORED_CODES) == 11  # precondition_failed, nine host codes, stale_completion


def _is_true(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def test_every_failing_answer_in_the_server_module_is_built_by_the_helper() -> None:
    """`_failure` is the one place an answer becomes `ok: false` or gains
    `refused`, so its code requirement and its unknown-outcome guard run on
    every one. Outside it, the server module may write `"ok"` only as a
    literal `True`, and `_decision` may compute it for the rejected
    decision answer, whose code is the stored one. Any other form -- a
    `False` or computed `"ok"` value, an `ok=` keyword, a `refused=` keyword
    outside `_failure`, a `"refused"` key, a `setdefault` of either key, or a
    subscript assignment to either key -- is a door that could answer
    without a code."""
    tree = ast.parse(Path(control.__file__).read_text(encoding="utf-8"))
    offences: list[tuple[str, int, str]] = []

    def check(node: ast.AST, owner: str) -> None:
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if not isinstance(key, ast.Constant):
                    continue
                if key.value == "refused":
                    offences.append((owner, node.lineno, "refused key"))
                if key.value == "ok" and not _is_true(value):
                    offences.append((owner, node.lineno, "ok key"))
        if isinstance(node, ast.Call):
            to_helper = isinstance(node.func, ast.Name) and node.func.id == "_failure"
            for keyword in node.keywords:
                if keyword.arg == "ok" and not _is_true(keyword.value):
                    offences.append((owner, node.lineno, "ok= keyword"))
                if keyword.arg == "refused" and not to_helper:
                    offences.append((owner, node.lineno, "refused= keyword"))
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "setdefault"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value in ("ok", "refused")
            ):
                offences.append((owner, node.lineno, f"{node.args[0].value} setdefault"))
        # a subscript whose key is not a literal (`answer[k] = False`) cannot be caught here
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        for target in targets:
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.slice, ast.Constant)
                and target.slice.value in ("ok", "refused")
            ):
                offences.append((owner, target.lineno, f"{target.slice.value} assignment"))

    def visit(node: ast.AST, owner: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            owner = node.name
        check(node, owner)
        for child in ast.iter_child_nodes(node):
            visit(child, owner)

    visit(tree, "<module>")
    allowed = {
        ("_failure", "ok key"),
        ("_failure", "refused assignment"),
        ("_decision", "ok key"),
    }
    assert [o for o in offences if (o[0], o[2]) not in allowed] == []
    # and each allowance is used exactly once, so none outlives its reason
    assert sorted((o[0], o[2]) for o in offences) == sorted(allowed)


def test_an_unknown_outcome_code_never_carries_a_refusal() -> None:
    """The code is independent of the marker, except here: `unknown` means
    admission is uncertain, and `refused` says nothing was admitted."""
    for code in sorted(UNKNOWN_OUTCOME_CODES):
        assert _failure(code, "e") == {"ok": False, "code": code, "error": "e"}  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="unknown outcome"):
            _failure(code, "e", refused=True)  # type: ignore[arg-type]
    assert _failure("unknown_cmd", "e", refused=True) == {
        "ok": False,
        "code": "unknown_cmd",
        "error": "e",
        "refused": True,
    }


def test_a_code_less_engine_error_at_the_seal_door_answers_engine_error(tmp_path: Path) -> None:
    """The seal catch reads the raise site's code and never the prose. A
    raise site that named none answers the neutral `engine_error`, not
    `seal_refused`: the catch cannot tell a boundary check from an engine
    fault, so it claims neither."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    server = ControlServer(engine, run_root / "control.sock")
    wire = _seal_request_wire(engine, _stage(run_root, C2_JIL))

    async def code_less(_request: object) -> None:
        raise EngineError("an engine fault with no code")

    async def coded(_request: object) -> None:
        raise EngineError("a boundary check", code="seal_refused")

    try:
        engine.submit_seal = code_less  # type: ignore[method-assign]
        assert asyncio.run(server._seal(wire)) == {
            "ok": False,
            "code": "engine_error",
            "refused": True,
            "error": "an engine fault with no code",
        }
        engine.submit_seal = coded  # type: ignore[method-assign]
        assert asyncio.run(server._seal(wire))["code"] == "seal_refused"
    finally:
        _close(engine)
