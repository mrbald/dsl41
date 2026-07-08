"""Condition grammar tests. Every example is doc-derived (Broadcom TechDocs /
docs/autosys-semantics.md) or a direct SEM-entry consequence — no invented syntax.

Run: pytest tests/test_condition_grammar.py -q
"""

from pathlib import Path

import pytest
from lark import Lark, Tree

GRAMMAR = (Path(__file__).parent.parent / "grammars" / "condition.lark").read_text()


def make_parser(start: str) -> Lark:
    return Lark(GRAMMAR, start=start, parser="lalr")


FLAT = make_parser("start_flat")
PREC = make_parser("start_prec")

# (source, why / provenance)
VALID = [
    ("s(JobA)", "SEM-02 short form"),
    ("success(JobA)", "SEM-02 long form"),
    ("s(JobA) & s(JobB)", "condition-attribute doc example"),
    ("s(a)&s(b)&s(c)", "autorep spacing example — no spaces"),
    (
        "success(Joba,01\\:00) and failure(JobB,02\\:15)",
        "escaped-colon lookback + word operators, verbatim from Broadcom doc",
    ),
    ("s(JobA, 12.0)", "hhhh.mm lookback"),
    ("s(JobA, 00.30)", "sub-hour lookback, leading 00 (SEM-04)"),
    ("s(JobA, 0)", "zero lookback"),
    ("s(JobA, 9999)", "indefinite lookback sentinel"),
    ("done(job) | terminated(job)", "d/t atoms with pipe"),
    ("notrunning(other_job)", "n() mutual exclusion (M07)"),
    ("exitcode(JobA) > 5 and exitcode(JobB) != 10", "directutor/doc exitcode example"),
    ("e(JobA) = 4", "exitcode short form, doc example value"),
    ("value(BillID) = 100", "global atom (KB 186248 scenario)"),
    ("v(TEST) = ABC", "global short form, box_failure doc example values"),
    ('value(NAME) = "spaced value"', "quoted global value"),
    ("s(DB_BACKUP^PRD)", "cross-instance, verbatim scenario from condition doc"),
    ("s(jobB^PRD) & s(jobA)", "cross-instance mixed, Manage Common Job Properties doc"),
    ("(s(A) | s(B)) & s(C)", "parens force precedence (SEM-03)"),
    ("s(A) & (f(B) | t(C)) & d(E)", "nested mix"),
    ("s(BOX.CHILD.JOB#1)", "dots and # in names (mainframe naming notes)"),
    ("s(JOB\\:WITH\\:COLONS)", "escaped colons in job name (box_failure doc note)"),
]

INVALID = [
    ("s()", "empty ref"),
    ("s(JobA) &", "trailing operator"),
    ("& s(JobA)", "leading operator"),
    ("s(JobA", "unclosed paren"),
    ("unknown(JobA)", "unknown predicate keyword"),
    ("value(G, 2.0) = 1", "lookback on global atom — lexically excluded (SEM-04/L003)"),
    ("s(JobA,)", "empty lookback"),
]


@pytest.mark.parametrize("src,why", VALID, ids=[w for _, w in VALID])
def test_valid_both_precedence_modes(src: str, why: str) -> None:
    FLAT.parse(src)
    PREC.parse(src)


@pytest.mark.parametrize("src,why", INVALID, ids=[w for _, w in INVALID])
def test_invalid_rejected(src: str, why: str) -> None:
    for parser in (FLAT, PREC):
        with pytest.raises(Exception):
            parser.parse(src)


def test_precedence_modes_differ_where_expected() -> None:
    """The Q1 sentinel: `s(A) | s(B) & s(C)` must parse into different shapes
    under the two candidate grammars; equal-precedence left-assoc gives
    ((A|B)&C), C-style gives (A|(B&C)). When Q1 resolves, this test is replaced
    by a trace test pinning the surviving interpretation."""
    src = "s(A) | s(B) & s(C)"
    t_flat = FLAT.parse(src).children[0]  # unwrap start_flat
    t_prec = PREC.parse(src).children[0]  # unwrap start_prec
    # flat, left-assoc: ((A|B) & C) → top binop's op is '&'
    assert isinstance(t_flat, Tree) and t_flat.data == "binop"
    op = t_flat.children[1]
    assert isinstance(op, Tree) and op.children[0] == "&"
    # prec, C-style: (A | (B&C)) → top node is or_
    assert isinstance(t_prec, Tree) and t_prec.data == "or_"
