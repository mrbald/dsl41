"""AST fidelity tests F1-F4 (docs/jil-statement-syntax.md) + scanner structure
and error-path checks.

Whitespace-sensitive inputs (trailing spaces, CRLF, missing final newline) live
here as inline strings rather than corpus files, where editors/VCS could
silently mangle the exact bytes the test exists to protect.
"""

from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dsl41.ast_jil import (
    SUBCOMMANDS,
    JilParseError,
    parse,
    parse_file,
    render,
    render_canonical,
)

CORPUS_DIR = Path(__file__).parent / "corpus"
CORPUS = sorted(CORPUS_DIR.glob("*.jil"))


def corpus_text(path: Path) -> str:
    # bypass universal-newline translation
    return path.read_bytes().decode("utf-8")


# ------------------------------------------------------------------ F1: preserve mode


@pytest.mark.parametrize("path", CORPUS, ids=[p.name for p in CORPUS])
def test_f1_preserve_identity(path: Path) -> None:
    text = corpus_text(path)
    assert render(parse(text, file=path.name)) == text


def test_f1_via_parse_file() -> None:
    path = CORPUS_DIR / "oneline_form.jil"
    assert render(parse_file(path)) == corpus_text(path)


# ----------------------------------------------------------------- F2: canonical mode


@pytest.mark.parametrize("path", CORPUS, ids=[p.name for p in CORPUS])
def test_f2_canonical_fixpoint(path: Path) -> None:
    canonical = render_canonical(parse(corpus_text(path), file=path.name))
    assert render_canonical(parse(canonical, file=path.name)) == canonical


def test_canonical_splits_oneline_form_and_orders_attrs() -> None:
    jf = parse("insert_job: j   job_type: c\ncondition: s(a)\nzz_unknown: 1\ncommand: x\n")
    assert render_canonical(jf) == (
        "insert_job: j\njob_type: c\ncommand: x\ncondition: s(a)\nzz_unknown: 1\n"
    )


def test_canonical_normalizes_spacing_and_blank_lines() -> None:
    text = "insert_job:j\n\n\n   command:   echo hi   \nmachine: m1\n"
    assert render_canonical(parse(text)) == "insert_job: j\ncommand: echo hi\nmachine: m1\n"


def test_canonical_separates_statements_with_one_blank_line() -> None:
    text = "insert_job: a\ncommand: x\ninsert_job: b\ncommand: y\n"
    assert render_canonical(parse(text)) == (
        "insert_job: a\ncommand: x\n\ninsert_job: b\ncommand: y\n"
    )


def test_canonical_uses_lf_and_keeps_comments() -> None:
    text = "# lead\r\ninsert_job: j\r\ncommand: echo hi /* tail */\r\n"
    assert render_canonical(parse(text)) == ("# lead\ninsert_job: j\ncommand: echo hi /* tail */\n")


# ------------------------------------------------------------------ scanner structure


def test_oneline_form_structure() -> None:
    jf = parse_file(CORPUS_DIR / "oneline_form.jil")
    (stmt,) = jf.statements
    assert stmt.subcommand == "insert_job"
    assert stmt.subject == "template"
    assert stmt.job_type_inline == "c"
    assert [a.key for a in stmt.attrs] == ["owner", "command", "machine", "condition"]
    cmd = stmt.attrs[1]
    assert cmd.raw_value == "ls -l /tmp"
    (tc,) = cmd.comments
    assert tc.attachment == "trailing"
    assert tc.text == "/* colon-free value with comment */"
    # leading '#' comment attached to the statement, not floating
    assert stmt.comments[0].attachment == "leading"
    assert stmt.comments[0].text.startswith("# one-line")


def test_escaped_colon_stays_in_value() -> None:
    jf = parse_file(CORPUS_DIR / "sem04_lookback.jil")
    cond = next(a for a in jf.statements[0].attrs if a.key == "condition")
    assert cond.raw_value == r"success(Joba,01\:00) and failure(JobB,02\:15)"


def test_subcommand_recognized_case_insensitively_stored_as_written() -> None:
    jf = parse("INSERT_JOB: j\ncommand: echo hi\n")
    (stmt,) = jf.statements
    assert stmt.subcommand == "INSERT_JOB"
    assert stmt.subject == "j"
    assert stmt.attrs[0].key == "command"


def test_unknown_key_is_attribute_never_boundary() -> None:
    jf = parse("insert_job: j\nfrobnicate_mode: 7\n")
    assert [a.key for a in jf.statements[0].attrs] == ["frobnicate_mode"]


def test_trailing_hash_comment_split() -> None:
    jf = parse("insert_job: j\ncommand: echo hi # note\n")
    (attr,) = jf.statements[0].attrs
    assert attr.raw_value == "echo hi"
    (tc,) = attr.comments
    assert tc.attachment == "trailing"
    assert tc.text == "# note"
    assert tc.indent == " "


def test_glob_is_not_a_comment() -> None:
    jf = parse("insert_job: j\ncommand: ls -l /tmp/*\n")
    (attr,) = jf.statements[0].attrs
    assert attr.raw_value == "ls -l /tmp/*"
    assert attr.comments == []


def test_quoted_markers_stay_in_value() -> None:
    jf = parse('insert_job: j\ndescription: "hash # and /* stay */ put"\n')
    (attr,) = jf.statements[0].attrs
    assert attr.raw_value == '"hash # and /* stay */ put"'
    assert attr.comments == []


def test_continuation_lines_merge_into_value() -> None:
    text = 'insert_job: j\nstart_times: "08:00,\n09:00"\ncommand: echo hi\n'
    jf = parse(text)
    st_attr, cmd = jf.statements[0].attrs
    assert st_attr.raw_value == '"08:00,\n09:00"'
    assert st_attr.span.line_start == 2 and st_attr.span.line_end == 3
    assert cmd.raw_value == "echo hi"
    assert render(parse(text)) == text


def test_floating_comments_at_eof() -> None:
    text = "insert_job: j\ncommand: echo hi\n\n/* the end */\n"
    jf = parse(text)
    (fc,) = jf.trailing_comments
    assert fc.attachment == "floating"
    assert fc.pre_blank_lines == [""]
    assert render(jf) == text


def test_spans_are_utf8_byte_offsets() -> None:
    text = "insert_job: jé\ncommand: echo hi\n"
    jf = parse(text)
    (stmt,) = jf.statements
    assert stmt.span.line_start == 1 and stmt.span.line_end == 2
    assert stmt.span.byte_start == 0
    header_bytes = len("insert_job: jé".encode())
    assert stmt.attrs[0].span.byte_start == header_bytes + 1
    assert stmt.span.byte_end == header_bytes + 1 + len("command: echo hi")


# ----------------------------------------------------------------------- F3: fuzzing


_IDENT = st.from_regex(r"[a-z][a-z0-9_]{0,7}", fullmatch=True)
_ATTR_KEY = _IDENT.filter(lambda k: k not in SUBCOMMANDS)
_VALUE = st.text(st.characters(min_codepoint=32, max_codepoint=126), max_size=24)
_LAYOUT_LINE = st.sampled_from(["", "  ", "# lead comment", "/* lead */", "\t/* x */"])


@st.composite
def jil_source(draw: st.DrawFn) -> str:
    nl = draw(st.sampled_from(["\n", "\r\n"]))
    lines: list[str] = []
    for _ in range(draw(st.integers(min_value=1, max_value=3))):
        for _ in range(draw(st.integers(min_value=0, max_value=2))):
            lines.append(draw(_LAYOUT_LINE))
        header = f"insert_job: {draw(_IDENT)}"
        if draw(st.booleans()):
            header += "   job_type: c"
        lines.append(header)
        for _ in range(draw(st.integers(min_value=0, max_value=4))):
            lines.append(f"{draw(_ATTR_KEY)}: {draw(_VALUE)}")
        if draw(st.booleans()):
            lines.append("start_mins: 0, 15,")
            lines.append("30, 45")
    text = nl.join(lines)
    if draw(st.booleans()):
        text += nl
    return text


@given(jil_source())
def test_f3_fuzz_preserve_identity(text: str) -> None:
    try:
        jf = parse(text)
    except JilParseError:
        return  # F3: fidelity is asserted wherever parse succeeds
    assert render(jf) == text


@given(jil_source())
def test_f3_fuzz_canonical_fixpoint(text: str) -> None:
    try:
        jf = parse(text)
    except JilParseError:
        return
    canonical = render_canonical(jf)
    assert render_canonical(parse(canonical)) == canonical


@given(st.text(alphabet=st.sampled_from(list('abz_:#/*\\" \t\n')), max_size=60))
def test_f3_soup_preserve_identity(text: str) -> None:
    """Raw character soup: anything the scanner accepts must round-trip."""
    try:
        jf = parse(text)
    except JilParseError:
        return
    assert render(jf) == text


# ----------------------------------------------------- F4: inline whitespace/EOL cases


F4_CASES = [
    ("escaped-colon-in-value", "insert_job: j\ncommand: echo C\\:\\\\TEMP\n"),
    ("quoted-and-escaped-colons", 'insert_job: j\ncommand: echo "a : b" and \\: bare\n'),
    ("hash-inside-quotes", 'insert_job: j\ndescription: "hash # inside quotes"\n'),
    ("glob-not-comment", "insert_job: j\ncommand: ls -l /tmp/*\n"),
    ("unclosed-block-stays-in-value", "insert_job: j\ncommand: ls /tmp /*\n"),
    ("embedded-closed-block-in-value", "insert_job: j\ncommand: a /* closed */ b\n"),
    ("no-space-after-colon", "insert_job: j\ncommand:no_space\n"),
    ("empty-value", "insert_job: j\nempty_attr:\n"),
    ("trailing-spaces-in-value", "insert_job: j\ncommand: trailing spaces   \n"),
    ("oneline-plus-trailing-comment", "insert_job: j   job_type: c   /* trailing */\n"),
    ("crlf-endings", "insert_job: j\r\ncommand: echo hi\r\n"),
    ("no-final-newline", "insert_job: j\ncommand: echo hi"),
    ("indented-attr", "insert_job: j\n   command: indented\n"),
    ("empty-subject", "insert_job:\n"),
    ("blank-and-ws-only-lines", "insert_job: j\n   \ncommand: echo hi\n\n"),
    ("empty-file", ""),
    ("comment-only-file", "# just a comment\n"),
]


@pytest.mark.parametrize("text", [t for _, t in F4_CASES], ids=[i for i, _ in F4_CASES])
def test_f4_preserve_identity(text: str) -> None:
    assert render(parse(text)) == text


@pytest.mark.parametrize("text", [t for _, t in F4_CASES], ids=[i for i, _ in F4_CASES])
def test_f4_canonical_fixpoint(text: str) -> None:
    canonical = render_canonical(parse(text))
    assert render_canonical(parse(canonical)) == canonical


# ------------------------------------------------------------------------ error paths


ERROR_CASES = [
    ("attr-before-statement", "command: echo hi\n"),
    ("unrecognized-line", "insert_job: j\n???not an attr\n"),
    ("mixed-line-endings", "insert_job: a\ncommand: x\r\nmachine: m\n"),
    ("bare-cr-endings", "insert_job: a\rcommand: x\r"),
    ("inline-key-not-job-type", "insert_job: j owner: bob\n"),
    ("multiple-inline-pairs", "insert_job: j job_type: c owner: bob\n"),
    ("unterminated-block-comment", "insert_job: j\n/* never closed\n"),
    ("content-after-block-close", "insert_job: j\n/* closed */ command: x\n"),
    ("continuation-without-list-attr", "insert_job: j\ncommand: echo\n0, 15, 30\n"),
]


@pytest.mark.parametrize("text", [t for _, t in ERROR_CASES], ids=[i for i, _ in ERROR_CASES])
def test_scanner_errors_are_loud(text: str) -> None:
    with pytest.raises(JilParseError):
        parse(text)
