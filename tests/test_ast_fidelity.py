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


def test_resource_subcommands_are_statement_boundaries() -> None:
    """Rule 3 (amended 2026-07-09, DL-18): insert_resource must start a new
    statement, never fold into the preceding insert_machine -- the exact
    silent-loss failure the first estate-shaped dry run exposed."""
    text = "insert_machine: m1\ntype: a\n\ninsert_resource: lock1\nres_type: R\namount: 1\n"
    jf = parse(text)
    assert [s.subcommand for s in jf.statements] == ["insert_machine", "insert_resource"]
    assert [a.key for a in jf.statements[1].attrs] == ["res_type", "amount"]
    assert render(jf) == text


def test_subcommand_shaped_unknown_key_is_a_loud_error() -> None:
    """Rule 3 guard (DL-18): an attribute-position key shaped like a
    subcommand but not in the recognized set is a scanner error, not an
    attribute -- a missed statement boundary is silent structural loss.
    (insert_monbro moved to the recognized set in DL-29; the guard example
    is now a fictional object class.)"""
    text = "insert_job: j   job_type: c\ncommand: x\ninsert_frobnicator: f1\ncurrency: y\n"
    with pytest.raises(JilParseError, match="shaped like a subcommand"):
        parse(text)


def test_full_12x_subcommand_inventory_scans_as_boundaries() -> None:
    """DL-29: every subcommand on the TechDocs 12.1 JIL Subcommands page is
    a statement boundary, so F1 fidelity holds over any valid estate file;
    lowering (not the scanner) refuses the out-of-scope object classes."""
    text = (
        "insert_monbro: mon1\nmode: b\n\n"
        "delete_glob: g1\n\n"
        "insert_job_type: mytype\n\n"
        "update_connectionprofile: hadoop1\n\n"
        "delete_blob: j1\n"
    )
    jf = parse(text)
    assert [s.subcommand for s in jf.statements] == [
        "insert_monbro",
        "delete_glob",
        "insert_job_type",
        "update_connectionprofile",
        "delete_blob",
    ]
    assert render(jf) == text


def test_subcommand_shaped_guard_fires_before_any_statement_too() -> None:
    with pytest.raises(JilParseError, match="shaped like a subcommand"):
        parse("delete_frobnicator: f1\n")


# --------------------------------------------- rule 11: calendar exports (DL-36)


def test_calendar_export_statements_are_boundaries() -> None:
    """Rule 11 (DL-36): the autocal_asc export verbs start statements -- the
    field failure was `extended_calendar` at file start dying as an
    'attribute line before any statement'."""
    text = (
        "extended_calendar: eom\nworkday: mo,tu,we,th,fr\nadjust: 0\n\n"
        "cycle: q1\nstart_date: 03/28/2026\nend_date: 04/02/2026\n"
    )
    jf = parse(text)
    assert [s.subcommand for s in jf.statements] == ["extended_calendar", "cycle"]
    assert render(jf) == text


def test_standard_calendar_date_rows_are_verbatim_statement_body() -> None:
    text = (
        "calendar: hols\ndescription: x\n"
        "01/01/2026 00:00\n12/25/2026 00:00\n\n"
        "insert_job: j   job_type: c\ncommand: x\n"
    )
    jf = parse(text)
    calendar, job = jf.statements
    assert calendar.date_lines == ["01/01/2026 00:00", "12/25/2026 00:00"]
    assert [a.key for a in calendar.attrs] == ["description"]
    assert job.subcommand == "insert_job"
    assert render(jf) == text


def test_calendar_date_rows_allowed_without_attrs() -> None:
    text = "calendar: hols\n01/01/2026 00:00\n"
    jf = parse(text)
    assert jf.statements[0].date_lines == ["01/01/2026 00:00"]
    assert render(jf) == text


def test_date_row_with_key_shaped_tail_still_carries_verbatim() -> None:
    """Rule 11 does not validate the row shape, so the rule-4b continuation
    guard (DL-160) stays out of date rows. autocal refuses the row loudly at
    consumption (tests/test_autocal.py)."""
    text = "calendar: hols\n01/01/2026 owner: bob\n"
    jf = parse(text)
    assert jf.statements[0].date_lines == ["01/01/2026 owner: bob"]
    assert render(jf) == text


def test_attribute_after_date_rows_is_a_loud_error() -> None:
    """Rule 11 (DL-36): the export format puts attributes before the date
    list; re-rendering an interleaved shape would silently reorder it."""
    with pytest.raises(JilParseError, match="after the date rows"):
        parse("calendar: hols\n01/01/2026 00:00\ndescription: x\n")


def test_date_row_outside_a_calendar_statement_is_still_an_error() -> None:
    with pytest.raises(JilParseError, match="unrecognized line"):
        parse("insert_job: j   job_type: c\ncommand: x\n01/01/2026 00:00\n")
    with pytest.raises(JilParseError, match="unrecognized line"):
        parse("extended_calendar: eom\nadjust: 0\n01/01/2026 00:00\n")


def test_date_rows_must_be_contiguous_with_their_statement() -> None:
    with pytest.raises(JilParseError, match="unrecognized line"):
        parse("calendar: hols\n01/01/2026 00:00\n\n12/25/2026 00:00\n")


def test_canonical_keeps_date_rows_after_attrs() -> None:
    canonical = render_canonical(parse("calendar: hols\ndescription: x\n  01/01/2026 00:00  \n"))
    assert canonical == "calendar: hols\ndescription: x\n01/01/2026 00:00\n"
    assert render_canonical(parse(canonical)) == canonical


def test_rename_job_is_a_statement_boundary() -> None:
    """Rule 3 (amended 2026-07-10, DL-27): rename_job is a documented 12.x
    subcommand whose rename_ verb sat outside the DL-18 guard shape -- before
    the amendment it folded silently into the preceding statement, the exact
    loss class the guard exists to stop."""
    text = "insert_job: j   job_type: c\ncommand: x\n\nrename_job: j\nnew_name: k\n"
    jf = parse(text)
    assert [s.subcommand for s in jf.statements] == ["insert_job", "rename_job"]
    assert [a.key for a in jf.statements[1].attrs] == ["new_name"]
    assert render(jf) == text


def test_rename_shaped_unknown_key_is_a_loud_error() -> None:
    """DL-27 extends the DL-18 guard verbs with rename_."""
    with pytest.raises(JilParseError, match="shaped like a subcommand"):
        parse("insert_job: j   job_type: c\nrename_frobnicator: f1\n")


def test_second_attr_pair_on_attribute_line_is_a_loud_error() -> None:
    """Rule 4b (DL-30): JIL permits several attribute statements per line;
    swallowing the second pair into the first value is silent loss the
    DL-07 firewall can never see."""
    with pytest.raises(JilParseError, match="rule 4b"):
        parse("insert_job: j\nmachine: prod priority: 5\n")


@pytest.mark.parametrize(
    ("label", "line"),
    [
        # No whitespace before the colon-bearing token: not a pair shape.
        ("path-colon", "std_out_file: /tmp/out:file.err"),
        # Escaped colon is never a delimiter (rule 2).
        ("escaped-colon", "command: run C\\:\\\\TEMP now"),
        # Quoted colons are shadowed (rule 7).
        ("quoted-pair-lookalike", 'description: "see doc: here"'),
        # Digit-led tokens are not key-shaped.
        ("bare-time-list", "run_window: 02:00-04:00"),
        # A key: shape inside a retained closed block comment is comment
        # prose, not a pair (rule 5 opaque retention) -- the DL-30 loss
        # class, pinned here so a detector change cannot reopen it.
        ("pair-inside-inline-comment", "command: run /* see key: x */ more"),
        # The marker is taken at the value start too (rule 5, DL-151).
        ("pair-inside-value-start-comment", "command:/* see key: x */ more"),
        # A token glued to the closing `*/` is not whitespace-preceded, so it
        # is not a pair shape; the mask must not invent that whitespace
        # (rule 4b, DL-151).
        ("token-glued-to-block-close", "command: a /* c */b: x"),
    ],
)
def test_rule_4b_guard_leaves_valid_colon_values_alone(label: str, line: str) -> None:
    jf = parse(f"insert_job: j\n{line}\n")
    assert len(jf.statements[0].attrs) == 1, label


def test_rule_4b_guard_sees_through_a_quoted_block_marker() -> None:
    """Rule 4b + rule 7 (DL-151): the closed-block mask is quote-aware. A `/*`
    inside quotes opens no comment. The quote-blind mask blanked the span over
    the CLOSING quote, the guard read the parity as odd, and the second pair
    folded into raw_value -- the DL-30 silent-loss class."""
    with pytest.raises(JilParseError, match="rule 4b"):
        parse('insert_job: j\ndescription: "/* " */ key: value\n')


def test_rule_4b_guard_fires_inside_a_glued_block_marker() -> None:
    """Rule 5 opens a block only at the value start or after whitespace, so a
    marker glued to the text before it is not a comment and the pair inside it
    is a real pair (DL-151). The mask now opens spans where rule 5 does."""
    with pytest.raises(JilParseError, match="rule 4b"):
        parse("insert_job: j\ncommand: a/* key: x */b\n")


def test_rule_4b_guard_fires_on_a_pair_after_a_closed_block() -> None:
    """The mask hides only the span. A whitespace-preceded pair AFTER the
    closing `*/` is a real second attribute and still errors."""
    with pytest.raises(JilParseError, match="rule 4b"):
        parse("insert_job: j\ncommand: a /* c */ b: x\n")


def test_rule_4b_guard_covers_continuation_lines() -> None:
    """Rule 4b covers the joined value (DL-160): a bare `key:` token on a
    rule-6 continuation line folded silently into raw_value -- the DL-30
    loss class."""
    with pytest.raises(JilParseError, match="rule 4b"):
        parse("insert_job: j\nstart_times: 10:00,\n 11:00 owner: bob\n")


def test_rule_4b_continuation_guard_closes_the_run_calendar_lane() -> None:
    """The one fold that reached the backend with no downstream check: a
    run_calendar continuation in a calendar-free set. L018 stays quiet when
    the set defines no calendars, so the folded pair was invisible end to
    end (DL-160)."""
    with pytest.raises(JilParseError, match="rule 4b"):
        parse("insert_job: j\nrun_calendar: cal\n plus owner: bob\n")


def test_rule_4b_continuation_seeds_quote_state_from_the_joined_value() -> None:
    """A quote opened on the attribute line is still open on the continuation
    line (DL-160): a pair lookalike inside it is quoted value text, and the
    joined value round-trips -- the tests/corpus/continuation_multiline.jil
    shape."""
    text = 'insert_job: j\nstart_times: "08:00,\n09:00 note: x"\n'
    jf = parse(text)
    (attr,) = jf.statements[0].attrs
    assert attr.raw_value == '"08:00,\n09:00 note: x"'
    assert render(jf) == text


def test_rule_4b_continuation_pair_after_the_closing_quote_fires() -> None:
    """The seeded parity flips back when the spanning quote closes: a
    whitespace-preceded pair after the close is a real pair (DL-160)."""
    with pytest.raises(JilParseError, match="rule 4b"):
        parse('insert_job: j\nstart_times: "08:00,\n09:00" owner: bob\n')


def test_quoted_block_marker_without_a_second_pair_still_parses() -> None:
    """The same mask must not invent a pair: with no `key:` after the quoted
    marker the value stays verbatim and round-trips."""
    text = 'insert_job: j\ndescription: "/* " */ plain tail\n'
    jf = parse(text)
    (attr,) = jf.statements[0].attrs
    assert attr.raw_value == '"/* " */ plain tail'
    assert render(jf) == text


def test_closed_block_on_a_subcommand_line_is_opaque_value_text() -> None:
    """Rule 4 runs the rule-4b detector, so it masks closed blocks too
    (DL-151): rule 5 keeps `/* see owner: bob */` with text after it as opaque
    value text, and the scanner used to reject the line as an inline `owner`
    pair."""
    text = "insert_job: j /* see owner: bob */ tail\n"
    jf = parse(text)
    (stmt,) = jf.statements
    assert stmt.subject == "j /* see owner: bob */ tail"
    assert stmt.job_type_inline is None
    assert render(jf) == text


def test_closed_block_before_the_inline_job_type_keeps_its_bytes() -> None:
    """The mask fill must not be whitespace: `m.group(1)` is rendered back as
    the gap before the inline key, so a space fill put blanks where the comment
    was and broke F1 (DL-151)."""
    text = "insert_job: j /* c */ job_type: c\n"
    jf = parse(text)
    (stmt,) = jf.statements
    assert stmt.subject == "j /* c */"
    assert stmt.job_type_inline == "c"
    assert render(jf) == text


def test_block_marker_glued_to_the_inline_key_colon_is_not_a_comment() -> None:
    """The tail check reads the line-level mask, not a fresh mask of the
    inline value: rule 5 opens no span at `job_type:/*`, so `key: y` inside it
    is a real second pair and still errors (DL-151)."""
    with pytest.raises(JilParseError, match="multiple inline attributes"):
        parse("insert_job: j job_type:/* key: y */ z\n")


def test_closed_block_after_the_inline_job_type_is_opaque_value_text() -> None:
    text = "insert_job: j   job_type: c /* see owner: bob */ tail\n"
    jf = parse(text)
    (stmt,) = jf.statements
    assert stmt.subject == "j"
    assert stmt.job_type_inline == "c /* see owner: bob */ tail"
    assert render(jf) == text


def test_block_marker_at_the_value_start_is_a_trailing_comment() -> None:
    """Rule 5: the trailing marker is taken at the value start as well as after
    whitespace, so `command:/* c */` is an empty value plus a comment."""
    text = "insert_job: j\ncommand:/* c */\n"
    jf = parse(text)
    (attr,) = jf.statements[0].attrs
    assert attr.raw_value == ""
    assert [c.text for c in attr.comments] == ["/* c */"]
    assert render(jf) == text


def test_mid_line_hash_stays_in_value() -> None:
    """Rule 5 (amended 2026-07-10, DL-31): Broadcom defines `#` comments in
    the first column only and `#` is a legal name/value character, so a
    whitespace-preceded `#`-tail is VALUE text, never a trailing comment --
    stripping it silently changed the value vs. the engine's parse."""
    text = "insert_job: j\ncommand: echo hi # note\n"
    jf = parse(text)
    (attr,) = jf.statements[0].attrs
    assert attr.raw_value == "echo hi # note"
    assert attr.comments == []
    assert render(jf) == text


def test_hash_full_line_comment_still_recognized() -> None:
    jf = parse("# lead\ninsert_job: j\ncommand: x\n")
    (stmt,) = jf.statements
    assert [c.text for c in stmt.comments] == ["# lead"]


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


# ---------------------------- rule 5 opener: multi-line trailing comments (DL-161)


def test_trailing_opener_spans_lines_as_comment() -> None:
    """Rule 5 (DL-161): a whitespace-preceded `/*` with no `*/` on its line
    OPENS a block comment. Structural, not just F1: before the amendment the
    marker stayed value text and the body lines scanned as ATTRIBUTES --
    `owner` below silently became an attribute of j. F1 alone cannot prove
    the classification: both ASTs render the same bytes."""
    text = "insert_job: j\ncommand: echo hi /* watch\nowner: prose about bob\nend: */\n"
    jf = parse(text)
    (attr,) = jf.statements[0].attrs  # owner/end are NOT attributes
    assert attr.key == "command"
    assert attr.raw_value == "echo hi"
    (tc,) = attr.comments
    assert tc.attachment == "trailing"
    assert tc.text == "/* watch\nowner: prose about bob\nend: */"
    assert tc.span.line_start == 2 and tc.span.line_end == 4
    assert attr.span.line_end == 4 and jf.statements[0].span.line_end == 4
    assert render(jf) == text
    canonical = render_canonical(jf)
    assert render_canonical(parse(canonical)) == canonical


def test_trailing_opener_round_trips_crlf() -> None:
    """The renderers emit the comment body line-wise: `Comment.text` stores
    '\\n', so a suffix-string emit would render the body LF inside a CRLF
    file and break F1."""
    text = "insert_job: j\r\ncommand: a /* w\r\nkey: in body\r\nb */\r\n"
    jf = parse(text)
    (attr,) = jf.statements[0].attrs
    assert attr.raw_value == "a"
    assert attr.comments[0].text == "/* w\nkey: in body\nb */"
    assert render(jf) == text


def test_trailing_opener_unterminated_at_eof_is_loud() -> None:
    """A comment still open at EOF refuses with the OPENER's line (rule 5,
    DL-161). Before the amendment this input parsed silently with the
    marker left in the value."""
    with pytest.raises(JilParseError, match="unterminated block comment") as exc:
        parse("insert_job: j\ncommand: ls /tmp /*\n", file="f.jil")
    assert exc.value.line == 2
    assert str(exc.value).startswith("f.jil:2:")


def test_trailing_opener_close_line_tail_is_loud() -> None:
    """The multi-line close rule is the full-line one: non-whitespace after
    `*/` on the closing line is an error."""
    with pytest.raises(JilParseError, match="content after"):
        parse("insert_job: j\ncommand: a /* w\nb */ tail\n")


def test_quoted_marker_is_the_glob_escape() -> None:
    """Rule 5 (DL-161): quoting is the ONLY complete escape for a
    whitespace-preceded `/*` value -- the quoted marker opens nothing
    (rule 7). Unquoted, this exact value now opens a comment
    (torture_colon.jil pins the quoted spelling corpus-wide)."""
    text = 'insert_job: j\nstd_err_file: "/tmp/log /*unclosed-glob-lookalike"\n'
    jf = parse(text)
    (attr,) = jf.statements[0].attrs
    assert attr.raw_value == '"/tmp/log /*unclosed-glob-lookalike"'
    assert attr.comments == []
    assert render(jf) == text


def test_trailing_opener_on_the_statement_header() -> None:
    """The opener runs on subcommand lines too, and the inline job_type
    relocation still reaches an F2 fixpoint with the multi-line comment on
    the header (the design-consult pin)."""
    text = "insert_job: j /* c1 */ job_type: cmd /* c2\nowner: comment text\n*/\n"
    jf = parse(text)
    (stmt,) = jf.statements
    assert stmt.subject == "j /* c1 */"
    assert stmt.job_type_inline == "cmd"
    assert stmt.attrs == []  # `owner:` in the body is comment prose
    tc = next(c for c in stmt.comments if c.attachment == "trailing")
    assert tc.text == "/* c2\nowner: comment text\n*/"
    assert render(jf) == text
    canonical = render_canonical(jf)
    assert render_canonical(parse(canonical)) == canonical


def test_trailing_block_opener_carries_the_fact_on_the_model() -> None:
    """The rule-5 opener fact lives on `Comment.trailing_block` (arch-review
    F4): the scanner stamps it at scan time, so a renderer never has to
    re-sniff `"\\n" in c.text` to tell a multi-line trailing comment from a
    closed one. Checked directly on the model, not just via round-trip
    bytes, and on both places the scanner can stamp it: the attribute line
    and a continuation line."""
    header_text = "insert_job: j /* c1 */ job_type: cmd /* c2\nowner: comment text\n*/\n"
    jf = parse(header_text)
    (stmt,) = jf.statements
    tc = next(c for c in stmt.comments if c.attachment == "trailing")
    assert tc.trailing_block is True

    cont_text = "insert_job: j\nstart_times: 10:00,\n11:00 /* note\nowner: bob\n*/\n"
    jf2 = parse(cont_text)
    (attr,) = jf2.statements[0].attrs
    (tc2,) = attr.comments
    assert tc2.trailing_block is True

    inline_text = "insert_job: j\ncommand: echo hi /* c */\n"
    jf3 = parse(inline_text)
    (attr3,) = jf3.statements[0].attrs
    (tc3,) = attr3.comments
    assert tc3.attachment == "trailing"
    assert tc3.trailing_block is False


def test_trailing_opener_on_a_continuation_line_gates_the_4b_detector() -> None:
    """Rule 6 amended (DL-161): a continuation line can open a block comment,
    with the quote state seeded from the joined value. The body is comment,
    so the DL-160 detector does not fire on `owner:` there and the body does
    not fold into raw_value -- only the pre-opener prefix is value."""
    text = "insert_job: j\nstart_times: 10:00,\n11:00 /* note\nowner: bob\n*/\n"
    jf = parse(text)
    (attr,) = jf.statements[0].attrs
    assert attr.raw_value == "10:00,\n11:00"
    (tc,) = attr.comments
    assert tc.attachment == "trailing" and tc.text == "/* note\nowner: bob\n*/"
    assert render(jf) == text


def test_rule_4b_fires_on_the_value_prefix_of_a_continuation_opener() -> None:
    """Only the comment body is exempt: a bare pair in the pre-opener value
    prefix of the opening continuation line is still the DL-160 error."""
    with pytest.raises(JilParseError, match="rule 4b"):
        parse("insert_job: j\nstart_times: 10:00,\n11:00 owner: bob /* note\nbody */\n")


def test_multiline_trailing_comment_closes_the_continuation() -> None:
    """Rule 6: a comment line closes the open continuation, and the body
    lines of a multi-line trailing comment are comment lines. The line
    after the closer is an error, not a resumed continuation."""
    with pytest.raises(JilParseError, match="unrecognized line"):
        parse("insert_job: j\nstart_times: 10:00, /* note\nbody */\n11:00\n")


def test_singleline_trailing_comment_leaves_the_continuation_open() -> None:
    """The other half of the rule above: a trailing comment that closes on
    its own line is not a comment LINE, so the continuation stays armed --
    pre-DL-161 behavior, now pinned."""
    text = "insert_job: j\nstart_times: 10:00, /* c */\n11:00\n"
    jf = parse(text)
    (attr,) = jf.statements[0].attrs
    assert attr.raw_value == "10:00,\n11:00"
    assert render(jf) == text


def test_quoted_marker_on_a_continuation_line_opens_nothing() -> None:
    """The opener walk shares rule 4b's seeded quote state (DL-160): inside
    a quote opened on an earlier line, the marker is value text."""
    text = 'insert_job: j\nstart_times: "08:00, /* x\n09:00"\n'
    jf = parse(text)
    (attr,) = jf.statements[0].attrs
    assert attr.raw_value == '"08:00, /* x\n09:00"'
    assert render(jf) == text


def test_closed_trailing_plus_continuation_opener_both_render() -> None:
    """One attr can carry both trailing shapes: a closed comment on the
    attribute line (continuation stays open) and a multi-line comment opened
    on a continuation line (which closes it). The closed one rides the first
    value line, the multi-line one the last."""
    text = "insert_job: j\nstart_times: 1, /* c */\n2 /* open\nbody */\n"
    jf = parse(text)
    (attr,) = jf.statements[0].attrs
    assert attr.raw_value == "1,\n2"
    assert [c.text for c in attr.comments] == ["/* c */", "/* open\nbody */"]
    assert render(jf) == text
    canonical = render_canonical(jf)
    assert render_canonical(parse(canonical)) == canonical


def test_calendar_date_body_survives_a_header_trailing_opener() -> None:
    """Rule 11 (DL-161): the atomic scan consumes the comment body with its
    statement line, so a multi-line trailing comment on `calendar:` is
    header trivia, not a comment line between rows -- the date body still
    opens contiguously after it."""
    text = "calendar: hols /* note\nbody */\n01/01/2026 00:00\n"
    jf = parse(text)
    (stmt,) = jf.statements
    assert stmt.date_lines == ["01/01/2026 00:00"]
    tc = next(c for c in stmt.comments if c.attachment == "trailing")
    assert tc.text == "/* note\nbody */"
    assert render(jf) == text


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
#: DL-159: gap widths between a subject and a closed block comment, or between
#: that comment and the inline `job_type` key. A single-space gap hides the
#: F2 bug (the buggy verbatim dump and the normalized re-parse agree by
#: accident), so the generator must be able to draw a wider one.
_SUBJECT_GAP = st.sampled_from([" ", "  ", "    "])


@st.composite
def jil_source(draw: st.DrawFn) -> str:
    nl = draw(st.sampled_from(["\n", "\r\n"]))
    lines: list[str] = []
    for _ in range(draw(st.integers(min_value=1, max_value=3))):
        for _ in range(draw(st.integers(min_value=0, max_value=2))):
            lines.append(draw(_LAYOUT_LINE))
        header = f"insert_job: {draw(_IDENT)}"
        if draw(st.booleans()):
            # DL-159: a closed block comment split off with the inline
            # job_type pair -- the subject alphabet must cover this shape,
            # not just a plain subject.
            header += f"{draw(_SUBJECT_GAP)}/* c */"
        if draw(st.booleans()):
            header += f"{draw(_SUBJECT_GAP)}job_type: c"
        lines.append(header)
        for _ in range(draw(st.integers(min_value=0, max_value=4))):
            lines.append(f"{draw(_ATTR_KEY)}: {draw(_VALUE)}")
        if draw(st.booleans()):
            lines.append("start_mins: 0, 15,")
            lines.append("30, 45")
        if draw(st.booleans()):
            # DL-161: a trailing opener whose comment closes on a later line;
            # the key-shaped body line must stay comment prose.
            lines.append(f"{draw(_ATTR_KEY)}: v /* {draw(_IDENT)}")
            lines.append(f"{draw(_IDENT)}: body")
            lines.append("*/")
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
    # DL-161: the unclosed marker OPENS a multi-line comment now; the
    # unclosed-at-EOF spelling moved to the error cases below. These pin the
    # valid closing shapes -- LF, CRLF, and opened on a continuation line.
    (
        "trailing-opener-multiline",
        "insert_job: j\ncommand: echo hi /* watch\nowner: body prose\nend: */\n",
    ),
    (
        "trailing-opener-multiline-crlf",
        "insert_job: j\r\ncommand: a /* w\r\nkey: in body\r\nb */\r\n",
    ),
    (
        "trailing-opener-on-continuation-line",
        "insert_job: j\nstart_times: 1,\n2 /* note\nkey: body\n*/\n",
    ),
    ("trailing-opener-blank-body-line", "insert_job: j\ncommand: a /* w\n\nb */\n"),
    ("quoted-unclosed-marker-glob-escape", 'insert_job: j\nstd_err_file: "/tmp/log /*glob"\n'),
    ("embedded-closed-block-in-value", "insert_job: j\ncommand: a /* closed */ b\n"),
    ("block-marker-at-value-start", "insert_job: j\ncommand:/* c */\n"),
    ("quoted-block-marker-in-value", 'insert_job: j\ndescription: "/* " */ plain tail\n'),
    ("closed-block-in-subject", "insert_job: j /* see owner: bob */ tail\n"),
    ("closed-block-before-inline-job-type", "insert_job: j /* c */ job_type: c\n"),
    # DL-159: a wider gap before the split-off comment exposes the F2 breach
    # the single-space case above hides by accident (the verbatim first-pass
    # subject dump and the normalized second-pass trailing comment agree
    # only when the original gap already happens to be one space).
    (
        "closed-block-before-inline-job-type-wide-gap",
        "insert_job: j    /* c */ job_type: cmd\n",
    ),
    ("closed-block-after-inline-job-type", "insert_job: j   job_type: c /* x: 1 */ tail\n"),
    # DL-159: two closed blocks in the subject, the second split off with
    # job_type -- the fix re-splits on the whole subject, not just a single
    # marker, so this multi-block shape must land on the same fixpoint.
    (
        "multiblock-subject-before-inline-job-type",
        "insert_job: j /* c1 */ x    /* c2 */ job_type: cmd\n",
    ),
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
    # DL-161: the rule-5 opener's refusal shapes. The first parsed silently
    # before the amendment (marker stayed in the value).
    ("trailing-opener-unterminated-at-eof", "insert_job: j\ncommand: ls /tmp /*\n"),
    ("trailing-opener-close-line-tail", "insert_job: j\ncommand: a /* w\nb */ tail\n"),
    (
        "continuation-after-multiline-trailing-comment",
        "insert_job: j\nstart_times: 1, /* c\nb */\n2\n",
    ),
]


@pytest.mark.parametrize("text", [t for _, t in ERROR_CASES], ids=[i for i, _ in ERROR_CASES])
def test_scanner_errors_are_loud(text: str) -> None:
    with pytest.raises(JilParseError):
        parse(text)
