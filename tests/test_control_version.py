"""Control-row dispatch and writer rules (protocol-evolution ss7, DL-498)."""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

import pytest
from test_preconditions import _serve, _teardown

from dsl41.runner_control import command


@pytest.mark.parametrize("cmd", ["status", "sendevent", "subscribe"])
@pytest.mark.parametrize(
    "version,entry",
    [
        (1, "DL-90"),
        (2, "DL-118"),
        (3, "DL-498"),
        (4, "DL-498"),
        (6, None),
        ("5", None),
        (True, None),
        (None, None),
        (5.0, None),
    ],
)
def test_control_version_refusal_keeps_connection(
    short_root: Path, cmd: str, version: object, entry: str | None
) -> None:
    """Each refusal is headerless; the next v5 query on that socket works (DL-498)."""

    async def scenario() -> None:
        engine, server, task = await _serve(short_root / "run")
        try:
            reader, writer = await asyncio.open_unix_connection(str(server.path))
            try:
                request = (
                    command(
                        "ON_HOLD",
                        {"job": "j"},
                        key="job:j",
                        revision=0,
                        baseline_id=engine.baseline_id,
                    )
                    if cmd == "sendevent"
                    else {"cmd": cmd}
                )
                if version is not None:
                    request["v"] = version
                writer.write(json.dumps(request).encode() + b"\n")
                await writer.drain()
                answer = json.loads(await asyncio.wait_for(reader.readline(), 5))
                error = (
                    f"control protocol v{version} is retired ({entry}); this engine speaks v5"
                    if entry
                    else f"protocol version {version!r}: this engine speaks v5"
                )
                assert answer == {
                    "ok": False,
                    "refused": True,
                    "code": "unsupported_version",
                    "error": error,
                }
                writer.write(b'{"v":5,"cmd":"status"}\n')
                await writer.drain()
                assert json.loads(await asyncio.wait_for(reader.readline(), 5))["ok"]
                assert not engine.oracle.store.job["j"].on_hold
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            await _teardown(engine, server, task)

    asyncio.run(scenario())


def test_control_field_tolerance_is_separate_from_version(short_root: Path) -> None:
    """Unknown fields pass queries and refuse strict reload envelopes (DL-498)."""

    async def scenario() -> None:
        engine, server, task = await _serve(short_root / "run")
        try:
            reader, writer = await asyncio.open_unix_connection(str(server.path))
            try:
                for cmd in ("status", "reread", "update"):
                    request = {"v": 5, "cmd": cmd, "future_field": True}
                    if cmd == "update":
                        request.update(baseline_id=engine.baseline_id, request_id="extra-field")
                    writer.write(json.dumps(request).encode() + b"\n")
                    await writer.drain()
                    answer = json.loads(await asyncio.wait_for(reader.readline(), 5))
                    if cmd == "status":
                        assert answer["ok"]
                    else:
                        assert answer["code"] == "invalid_argument" and answer["refused"]
                        assert "accepts" in answer["error"]
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            await _teardown(engine, server, task)

    asyncio.run(scenario())


def _literal_version_lines(text: str) -> list[int]:
    lines = set()
    for node in ast.walk(ast.parse(text)):
        values: list[ast.expr] = []
        if isinstance(node, ast.Dict):
            values += [
                value
                for key, value in zip(node.keys, node.values)
                if isinstance(key, ast.Constant) and key.value == "v"
            ]
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if node.value is not None:
                values += [
                    node.value
                    for target in targets
                    if isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value == "v"
                ]
        elif isinstance(node, ast.Call):
            values += [keyword.value for keyword in node.keywords if keyword.arg == "v"]
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "setdefault"
                and len(node.args) >= 2
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "v"
            ):
                values.append(node.args[1])
            if (
                isinstance(node.func, ast.Name)
                and node.func.id == "dict"
                and node.args
                and isinstance(node.args[0], (ast.List, ast.Tuple))
            ):
                values += [
                    pair.elts[1]
                    for pair in node.args[0].elts
                    if isinstance(pair, (ast.List, ast.Tuple))
                    and len(pair.elts) == 2
                    and isinstance(pair.elts[0], ast.Constant)
                    and pair.elts[0].value == "v"
                ]
        elif isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
            for line in [node.value, *node.value.splitlines()]:
                try:
                    frame = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(frame, dict) and "v" in frame:
                    lines.add(node.lineno)
        if any(isinstance(value, ast.Constant) for value in values):
            lines.add(node.lineno)
    return sorted(lines)


@pytest.mark.parametrize(
    "source",
    [
        'request = {"v": 3}',
        'request["v"] = 4',
        'request["v"]: int = 3',
        "dict(v=4)",
        'dict([("v", 3)])',
        'request.setdefault("v", 4)',
        'frame = \'{"v":3,"cmd":"status"}\\n\'',
        'frame = b\'{"v":4,"cmd":"status"}\\n\'',
    ],
)
def test_literal_writer_check_detects_literal_forms(source: str) -> None:
    assert _literal_version_lines(source) == [1]


@pytest.mark.parametrize(
    "source",
    [
        'request = {"v": PROTOCOL_VERSION}',
        'request["v"]: int = PROTOCOL_VERSION',
        'dict([("v", PROTOCOL_VERSION)])',
        'request.setdefault("v", PROTOCOL_VERSION)',
        'request = {"v": PROTOCOL_VERSION - 1}',
    ],
)
def test_literal_writer_check_leaves_computed_values_to_review(source: str) -> None:
    assert _literal_version_lines(source) == []


def test_bundled_control_writers_have_one_version_constant() -> None:
    """Reject literal control versions (DL-498); computed values still need source review."""
    source = Path(__file__).resolve().parents[1] / "src" / "dsl41"
    violations = []
    for path in sorted(source.rglob("*.py")):
        if "_vendor" in path.parts or path.name == "status_hook.py":
            continue  # The hook payload belongs to another protocol row (DL-446).
        violations += [
            f"{path.relative_to(source)}:{line}"
            for line in _literal_version_lines(path.read_text())
        ]
    assert violations == []
