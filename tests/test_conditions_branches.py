"""Branch tests for src/dsl41/conditions.py: the packaged-grammar layout of
`_grammar_text` and `_span` on a tree that carries no source position."""

from __future__ import annotations

from pathlib import Path

import pytest
from lark import Tree

from dsl41 import conditions


def test_grammar_text_prefers_the_packaged_grammar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wheel layout: `dsl41/grammars/condition.lark` exists beside the code."""
    packaged = tmp_path / "grammars"
    packaged.mkdir()
    (packaged / "condition.lark").write_text('start: "packaged"\n', encoding="utf-8")
    monkeypatch.setattr(conditions.resources, "files", lambda package: tmp_path)
    assert conditions._grammar_text() == 'start: "packaged"\n'


def test_grammar_text_falls_back_to_the_repository_grammar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The checkout layout: nothing packaged, the file at the repo root is read."""
    monkeypatch.setattr(conditions.resources, "files", lambda package: tmp_path)
    repo = Path(conditions.__file__).resolve().parents[2] / "grammars" / "condition.lark"
    assert conditions._grammar_text() == repo.read_text(encoding="utf-8")


def test_span_of_a_tree_without_positions_is_none() -> None:
    assert conditions._span(Tree("anything", [])) is None


def test_span_of_a_positioned_tree_is_its_offsets() -> None:
    tree: Tree = Tree("anything", [])
    tree.meta.empty = False
    tree.meta.start_pos = 3
    tree.meta.end_pos = 9
    assert conditions._span(tree) == conditions.CondSpan(start=3, end=9)
