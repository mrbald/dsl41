"""Cond-model and lookback tests (phase 2, docs/ir-design.md ss3 + SEM-02..08).

Grammar-level acceptance/rejection lives in tests/test_condition_grammar.py;
here we pin model shapes, lookback semantics (SEM-04), L015 pitfall shapes,
span retention, and the Q1 precedence switch.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import TypeAdapter

import dsl41.conditions as conditions
from dsl41.ast_jil import parse_file
from dsl41.conditions import (
    And,
    Cond,
    CondSpan,
    ConditionParseError,
    ExitCodeAtom,
    GlobalAtom,
    JobRef,
    Lookback,
    Or,
    Paren,
    Precedence,
    StatusAtom,
    iter_atoms,
    lookback_pitfalls,
    parse_condition,
    parse_lookback,
)

COND_ADAPTER: TypeAdapter[Cond] = TypeAdapter(Cond)

# --------------------------------------------------------------- lookback (SEM-04)


LOOKBACK_TABLE = [
    ("2", "window", 120),  # doc example: 2 hours
    ("30", "window", 1800),  # the classic pitfall: 30 HOURS
    ("00.30", "window", 30),  # sub-hour needs leading 00
    ("12.0", "window", 720),  # grammar-test VALID example
    ("01\\:00", "window", 60),  # escaped-colon form, Broadcom doc verbatim
    ("02\\:15", "window", 135),
    ("9999.59", "window", 9999 * 60 + 59),  # SEM-04 max ~= 416.58 days
    ("9999.00", "window", 9999 * 60),  # dotted form is a window, not indefinite
    ("0.00", "window", 0),  # zero-minute window, distinct from zero-lookback
    ("0", "zero", None),
    ("00", "zero", None),
    ("9999", "indefinite", None),
]


@pytest.mark.parametrize(
    "raw,kind,minutes", LOOKBACK_TABLE, ids=[raw for raw, _, _ in LOOKBACK_TABLE]
)
def test_parse_lookback_table(raw: str, kind: str, minutes: int | None) -> None:
    lb = parse_lookback(raw)
    assert lb.kind == kind
    assert lb.minutes == minutes
    assert lb.raw == raw  # verbatim, for round-trip + Q2 auditing


@pytest.mark.parametrize("raw", [".30", "1.60", "12.99", "", "abc", "1..2", "1\\:60"])
def test_parse_lookback_rejects_invalid(raw: str) -> None:
    with pytest.raises(ConditionParseError):
        parse_lookback(raw)


@given(
    hours=st.integers(min_value=0, max_value=9999),
    minutes=st.integers(min_value=0, max_value=59),
    sep=st.sampled_from([".", "\\:"]),
)
def test_parse_lookback_window_property(hours: int, minutes: int, sep: str) -> None:
    raw = f"{hours:02d}{sep}{minutes:02d}"
    lb = parse_lookback(raw)
    assert lb.kind == "window"
    assert lb.minutes == hours * 60 + minutes


# ------------------------------------------------------------ L015 pitfall shapes


def test_pitfall_bare_hours() -> None:
    (pitfall,) = lookback_pitfalls(parse_lookback("30"))
    assert "30 hours" in pitfall


def test_pitfall_single_digit_minutes() -> None:
    (pitfall,) = lookback_pitfalls(parse_lookback("12.3"))
    assert "12.03" in pitfall  # states the canonical two-digit reading


@pytest.mark.parametrize("raw", ["00.30", "0", "9999", "02\\:15"])
def test_clean_shapes_have_no_pitfalls(raw: str) -> None:
    assert lookback_pitfalls(parse_lookback(raw)) == []


def test_pitfalls_on_hand_built_unparseable_raw() -> None:
    lb = Lookback(kind="window", minutes=30, raw=".30")
    assert lookback_pitfalls(lb) == ["unparseable lookback token '.30'"]


# ------------------------------------------------------------- atom shapes (SEM-02)


def atom(src: str) -> Cond:
    """Parse a single-atom condition and strip the span for shape comparison."""
    cond = parse_condition(src)
    return cond.model_copy(update={"span": None})


def status_atom(src: str) -> StatusAtom:
    a = atom(src)
    assert isinstance(a, StatusAtom)
    return a


def test_status_atoms_long_and_short_forms() -> None:
    for src in ("s(JobA)", "success(JobA)", "SUCCESS(JobA)", "S(JobA)"):
        assert atom(src) == StatusAtom(job=JobRef(name="JobA"), status="SUCCESS"), src
    assert atom("f(x)") == StatusAtom(job=JobRef(name="x"), status="FAILURE")
    assert atom("done(x)") == StatusAtom(job=JobRef(name="x"), status="DONE")
    assert atom("t(x)") == StatusAtom(job=JobRef(name="x"), status="TERMINATED")
    assert atom("notrunning(other_job)") == StatusAtom(
        job=JobRef(name="other_job"), status="NOTRUNNING"
    )


def test_keyword_named_job_is_a_job_name() -> None:
    assert atom("s(f)") == StatusAtom(job=JobRef(name="f"), status="SUCCESS")


def test_job_name_with_dots_and_hash() -> None:
    assert status_atom("s(BOX.CHILD.JOB#1)").job == JobRef(name="BOX.CHILD.JOB#1")


def test_job_name_escaped_colons_unescaped_semantically() -> None:
    assert status_atom(r"s(JOB\:WITH\:COLONS)").job == JobRef(name="JOB:WITH:COLONS")


def test_cross_instance_ref() -> None:
    assert status_atom("s(DB_BACKUP^PRD)").job == JobRef(name="DB_BACKUP", instance="PRD")


def test_exitcode_atoms() -> None:
    assert atom("e(JobA) = 4") == ExitCodeAtom(job=JobRef(name="JobA"), op="=", value=4)
    cond = parse_condition("exitcode(JobA) > 5 and exitcode(JobB) != 10")
    assert isinstance(cond, And)
    left, right = cond.operands
    assert isinstance(left, ExitCodeAtom) and left.op == ">" and left.value == 5
    assert isinstance(right, ExitCodeAtom) and right.op == "!=" and right.value == 10


def test_exitcode_atom_with_lookback() -> None:
    a = atom("e(JobA, 00.30) >= 2")
    assert isinstance(a, ExitCodeAtom)
    assert a.lookback == Lookback(kind="window", minutes=30, raw="00.30")


def test_global_atoms() -> None:
    assert atom("value(BillID) = 100") == GlobalAtom(name="BillID", op="=", value="100")
    assert atom("v(TEST) = ABC") == GlobalAtom(name="TEST", op="=", value="ABC")


def test_global_quoted_value_is_unquoted_semantically() -> None:
    a = atom('value(NAME) = "spaced value"')
    assert a == GlobalAtom(name="NAME", op="=", value="spaced value")


def test_status_lookback_doc_verbatim() -> None:
    cond = parse_condition(r"success(Joba,01\:00) and failure(JobB,02\:15)")
    assert isinstance(cond, And)
    left, right = cond.operands
    assert isinstance(left, StatusAtom)
    assert left.lookback == Lookback(kind="window", minutes=60, raw=r"01\:00")
    assert isinstance(right, StatusAtom)
    assert right.lookback == Lookback(kind="window", minutes=135, raw=r"02\:15")


def test_no_lookback_token_means_none() -> None:
    assert status_atom("s(JobA)").lookback is None  # None == indefinite w/o explicit token


def test_cond_discriminated_union_round_trips() -> None:
    cond = parse_condition("(s(A) | s(B)) & e(C) > 1 & value(G) != off")
    assert COND_ADAPTER.validate_python(cond.model_dump()) == cond


# ------------------------------------------- structure, precedence (Q1), flattening


def test_q1_precedence_modes_differ_model_level() -> None:
    """Model-level mirror of the grammar Q1 sentinel (PENDING: Q1). When Q1
    resolves, this becomes the pinning test for the surviving interpretation."""
    src = "s(A) | s(B) & s(C)"
    flat = parse_condition(src, "flat")
    assert isinstance(flat, And)  # ((A|B) & C): left-assoc, equal precedence
    assert isinstance(flat.operands[0], Or)
    prec = parse_condition(src, "prec")
    assert isinstance(prec, Or)  # (A | (B&C)): C-style
    assert isinstance(prec.operands[1], And)


def test_default_precedence_is_flat() -> None:
    assert conditions.CONDITION_PRECEDENCE == "flat"
    assert parse_condition("s(A) | s(B) & s(C)") == parse_condition("s(A) | s(B) & s(C)", "flat")


def test_module_setting_switches_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conditions, "CONDITION_PRECEDENCE", "prec")
    assert isinstance(parse_condition("s(A) | s(B) & s(C)"), Or)


@pytest.mark.parametrize("mode", ["flat", "prec"])
def test_same_op_runs_flatten_nary(mode: Precedence) -> None:
    cond = parse_condition("s(a)&s(b)&s(c)", mode)
    assert isinstance(cond, And)
    names = [o.job.name for o in cond.operands if isinstance(o, StatusAtom)]
    assert names == ["a", "b", "c"]
    cond = parse_condition("done(job) | terminated(job) | s(x)", mode)
    assert isinstance(cond, Or)
    assert len(cond.operands) == 3


@pytest.mark.parametrize("mode", ["flat", "prec"])
def test_paren_survives_and_blocks_flattening(mode: Precedence) -> None:
    cond = parse_condition("(s(A) | s(B)) & s(C)", mode)
    assert isinstance(cond, And)
    grouped, tail = cond.operands
    assert isinstance(grouped, Paren)
    assert isinstance(grouped.inner, Or)
    assert isinstance(tail, StatusAtom)


def test_word_operators_equal_symbols() -> None:
    def drop_spans(obj: object) -> object:
        if isinstance(obj, dict):
            return {k: drop_spans(v) for k, v in obj.items() if k != "span"}
        if isinstance(obj, list):
            return [drop_spans(v) for v in obj]
        return obj

    def normalized(src: str) -> object:
        return drop_spans(COND_ADAPTER.dump_python(parse_condition(src)))

    assert normalized("s(A) and f(B) or d(C)") == normalized("s(A) & f(B) | d(C)")


# ------------------------------------------------------------------ atom iteration


def test_iter_atoms_left_to_right_through_nesting() -> None:
    cond = parse_condition("(s(A) | f(B)) & e(C) > 1 & value(G) = on")
    kinds_and_ids = [
        (a.kind, a.job.name if not isinstance(a, GlobalAtom) else a.name) for a in iter_atoms(cond)
    ]
    assert kinds_and_ids == [
        ("status", "A"),
        ("status", "B"),
        ("exitcode", "C"),
        ("global", "G"),
    ]


def test_iter_atoms_single_atom() -> None:
    (atom_,) = iter_atoms(parse_condition("s(only)"))
    assert isinstance(atom_, StatusAtom)
    assert atom_.job.name == "only"


# ------------------------------------------------------------------ span retention


def test_atom_and_operator_spans() -> None:
    cond = parse_condition("s(A) & f(B)")
    assert isinstance(cond, And)
    assert cond.span == CondSpan(start=0, end=11)
    assert cond.operands[0].span == CondSpan(start=0, end=4)
    assert cond.operands[1].span == CondSpan(start=7, end=11)


def test_exitcode_span_covers_comparison() -> None:
    cond = parse_condition("e(J) > 5")
    assert cond.span == CondSpan(start=0, end=8)


# -------------------------------------------------------------------- error paths


def test_syntax_error_is_wrapped_with_position() -> None:
    with pytest.raises(ConditionParseError) as exc_info:
        parse_condition("s(JobA) &")
    assert exc_info.value.pos is not None
    assert "invalid condition expression" in str(exc_info.value)


def test_lookback_on_global_atom_rejected() -> None:
    # SEM-04/L003: lexically excluded by the grammar
    with pytest.raises(ConditionParseError):
        parse_condition("value(G, 2.0) = 1")


def test_semantic_lookback_error_surfaces_from_parse() -> None:
    with pytest.raises(ConditionParseError) as exc_info:
        parse_condition("s(J, 1.60)")
    assert "00-59" in str(exc_info.value)


@pytest.mark.parametrize("src", ["", "s()", "& s(A)", "s(JobA", "unknown(JobA)", "s(J,.30)"])
def test_grammar_rejections_wrapped(src: str) -> None:
    with pytest.raises(ConditionParseError):
        parse_condition(src)


def test_dl20_long_flat_chains_parse_iteratively() -> None:
    """DL-20: a 3000-atom flat `&` chain used to blow the Python stack in the
    left-spine descent (RecursionError leaking as a raw traceback). The spine
    walk is iterative now and the result stays one n-ary And, so downstream
    walkers see a shallow tree."""
    cond = parse_condition(" & ".join(f"s(j{i})" for i in range(3000)))
    assert isinstance(cond, And)
    assert len(cond.operands) == 3000
    # mixed operators: flat mode folds left-associatively (Q1), so only the
    # atom count is mode-independent
    mixed = parse_condition(" | ".join(f"s(j{i}) & f(k{i})" for i in range(1500)))
    assert sum(1 for _ in iter_atoms(mixed)) == 3000


def test_dl20_pathological_grouping_depth_is_a_loud_parse_error() -> None:
    """Grouping depth beyond the v1 walker budget must surface as
    ConditionParseError (lowering -> exit-2 class), never a RecursionError
    traceback masquerading as lint findings."""
    deep = "(" * 5000 + "s(q)" + ")" * 5000
    with pytest.raises(ConditionParseError, match="walker budget"):
        parse_condition(deep)


# --------------------------------------------------------------------- properties


_JOB_NAME = st.from_regex(r"[A-Za-z][A-Za-z0-9_.#]{0,10}", fullmatch=True)


@given(name=_JOB_NAME, instance=st.none() | st.from_regex(r"[A-Z]{1,4}", fullmatch=True))
def test_job_ref_property(name: str, instance: str | None) -> None:
    src = f"s({name}^{instance})" if instance else f"s({name})"
    a = parse_condition(src)
    assert isinstance(a, StatusAtom)
    assert a.job == JobRef(name=name, instance=instance)


@given(
    atoms=st.lists(st.sampled_from(["s(a)", "f(b)", "d(c)", "e(x) = 1"]), min_size=2, max_size=6),
    op=st.sampled_from(["&", "|"]),
    mode=st.sampled_from(["flat", "prec"]),
)
def test_chain_flattening_property(atoms: list[str], op: str, mode: Precedence) -> None:
    cond = parse_condition(f" {op} ".join(atoms), mode)
    assert isinstance(cond, And if op == "&" else Or)
    assert len(cond.operands) == len(atoms)


# ------------------------------------------------- corpus integration (ast_jil -> conditions)

CORPUS = sorted((Path(__file__).parent / "corpus").glob("*.jil"))
CONDITION_ATTRS = {"condition", "box_success", "box_failure"}


def test_every_corpus_condition_attr_parses() -> None:
    """The two layers compose: every condition-bearing RawAttr in the corpus
    (comments already stripped by the scanner) is a valid condition expression
    under both candidate grammars. Calendar-export statements are skipped:
    their `condition:` is a date-condition keyword expression (a different
    vendor language, TechDocs "Date Condition Keywords"), carried opaquely
    (DL-36)."""
    checked = 0
    for path in CORPUS:
        jf = parse_file(path)
        for stmt in jf.statements:
            if stmt.subcommand.lower() in {"calendar", "cycle", "extended_calendar"}:
                continue
            for attr in stmt.attrs:
                if attr.key.lower() in CONDITION_ATTRS:
                    for mode in ("flat", "prec"):
                        parse_condition(attr.raw_value, mode)
                    checked += 1
    assert checked >= 8  # grows with the corpus; keep >= current count


# ----------------------------------------------------- CONDITION_PRECEDENCE env switch


def test_env_var_selects_default_precedence() -> None:
    code = (
        "from dsl41.conditions import CONDITION_PRECEDENCE, Or, parse_condition\n"
        "assert CONDITION_PRECEDENCE == 'prec'\n"
        "assert isinstance(parse_condition('s(A) | s(B) & s(C)'), Or)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "CONDITION_PRECEDENCE": "prec"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_env_var_invalid_value_fails_import_loudly() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import dsl41.conditions"],
        env={**os.environ, "CONDITION_PRECEDENCE": "bogus"},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "CONDITION_PRECEDENCE" in result.stderr
