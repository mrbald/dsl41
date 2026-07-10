"""DSL surface + decompiler tests (phase 10, DL-17): the four
corpus-extracted builders, cond_to_source fidelity, and the round-trip
property -- decompile(catalog), exec it, and the rebuilt catalog's
canonical form equals the original's.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from typer.testing import CliRunner

from dsl41.ast_jil import parse, parse_file
from dsl41.cli import app
from dsl41.conditions import GlobalAtom, JobRef, Lookback, StatusAtom, parse_condition
from dsl41.dsl import CatalogBuilder, DslError, cond_to_source, decompile
from dsl41.equiv import (
    canonical_cond,
    catalog_hash,
    equiv_scripts,
    equivalent_tier_a,
    equivalent_tier_b,
    equivalent_tier_c,
)
from dsl41.ir import CatalogIR, Time, lower_catalog, lower_source

CORPUS_DIR = Path(__file__).parent / "corpus"
EXPECT_LOWER_ERROR = {"sem31_xor.jil"}
LOWERABLE_CORPUS = [p for p in sorted(CORPUS_DIR.glob("*.jil")) if p.name not in EXPECT_LOWER_ERROR]

runner = CliRunner()


def roundtrip(catalog: CatalogIR) -> CatalogIR:
    source = decompile(catalog)
    # __name__ seeded so the module's `if __name__ == "__main__"` JIL-dump
    # footer (DL-37) resolves without firing
    namespace: dict[str, object] = {"__name__": "<decompiled>"}
    exec(compile(source, "<decompiled>", "exec"), namespace)  # noqa: S102
    rebuilt = namespace["catalog"]
    assert isinstance(rebuilt, CatalogIR)
    return rebuilt


# --------------------------------------------------------------- round-trip property


def test_whole_corpus_roundtrips_hash_equal() -> None:
    catalog = lower_catalog([parse_file(p) for p in LOWERABLE_CORPUS])
    rebuilt = roundtrip(catalog)
    assert catalog_hash(rebuilt) == catalog_hash(catalog)
    assert equivalent_tier_a(catalog, rebuilt).equivalent


@pytest.mark.parametrize("path", LOWERABLE_CORPUS, ids=[p.name for p in LOWERABLE_CORPUS])
def test_each_corpus_file_roundtrips_hash_equal(path: Path) -> None:
    catalog = lower_catalog([parse_file(path)])
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(catalog)


def test_roundtrip_preserves_annotations_and_passthrough_verbatim() -> None:
    """canonical_catalog drops annotations from the COMPARE view; the
    decompiler must still carry them (they are part of IR-F)."""
    catalog = lower_catalog([parse_file(CORPUS_DIR / "oneline_form.jil")])
    rebuilt = roundtrip(catalog)
    for name, job in catalog.jobs.items():
        assert rebuilt.jobs[name].annotations == job.annotations
        assert rebuilt.jobs[name].passthrough == job.passthrough


# ------------------------------------------------------------------ cond_to_source


@pytest.mark.parametrize(
    "source",
    [
        "s(JobA)",
        "f(x) & t(y)",
        "d(a) | n(b)",
        "s(a) & (f(b) | t(c)) & d(e)",
        "(s(A) | s(B)) & s(C)",
        r"s(Joba, 01\:00) & f(JobB, 02\:15)",
        "s(x, 00.30)",
        "s(x, 0)",
        "s(x, 9999)",
        "e(j) = 4",
        "e(j, 2.00) >= 3",
        "v(G) = 1",
        'v(NAME) = "spaced value"',
        "s(DB_BACKUP^PRD)",
        r"s(JOB\:WITH\:COLONS)",
        "s(a) | s(b) & s(c)",  # flat-mode left-assoc shape
    ],
    ids=lambda s: s.replace(" ", ""),
)
def test_cond_to_source_roundtrips_through_the_parser(source: str) -> None:
    cond = parse_condition(source)
    reparsed = parse_condition(cond_to_source(cond))
    from dsl41.equiv import canonical_cond

    assert canonical_cond(reparsed) == canonical_cond(cond)  # equal modulo spans


@given(
    atoms=st.lists(
        st.sampled_from(["s(a)", "f(b)", "d(c)", "n(d)", "e(x) = 1", "v(G) != 2"]),
        min_size=1,
        max_size=5,
    ),
    ops=st.lists(st.sampled_from([" & ", " | "]), min_size=4, max_size=4),
)
def test_cond_to_source_roundtrip_property(atoms: list[str], ops: list[str]) -> None:
    source = ""
    for index, atom in enumerate(atoms):
        source += atom if index == 0 else ops[index - 1] + atom
    cond = parse_condition(source)
    from dsl41.equiv import canonical_cond

    assert canonical_cond(parse_condition(cond_to_source(cond))) == canonical_cond(cond)


def test_cond_to_source_preserves_exact_structure_not_just_semantics() -> None:
    cond = parse_condition("(s(a) | s(b)) & s(c)")
    reparsed = parse_condition(cond_to_source(cond))
    # same tree shape: And(Paren(Or(...)), atom) modulo span
    assert type(reparsed) is type(cond)
    assert cond_to_source(reparsed) == cond_to_source(cond)


# ------------------------------------------------------------------------- builder


def test_builder_four_combinators_end_to_end() -> None:
    c = CatalogBuilder()
    c.global_("FLAG", "go")
    c.machine("m1", type="a")
    c.xinst("PRD", xtype="a")
    with c.box("bx"):
        c.job("m_a", command="a", machine="m1")
        c.job("m_b", command="b", machine="m1", condition="s(m_a)")
    c.job("head", command="h", machine="m1")
    c.job("mid", command="m", machine="m1")
    c.job("tail", command="t", machine="m1")
    c.sequence("head", "mid", "tail")
    c.job("fan1", command="f1", machine="m1")
    c.job("fan2", command="f2", machine="m1")
    c.job("join", command="j", machine="m1")
    c.parallel(["fan1", "fan2"], after="tail", then="join")
    catalog = c.build()
    assert catalog.jobs["m_a"].box.box_name == "bx"
    assert cond_to_source(catalog.jobs["mid"].sem.condition) == "s(head)"  # type: ignore[arg-type]
    assert cond_to_source(catalog.jobs["fan1"].sem.condition) == "s(tail)"  # type: ignore[arg-type]
    assert (
        cond_to_source(catalog.jobs["join"].sem.condition)  # type: ignore[arg-type]
        == "s(fan1) & s(fan2)"
    )
    assert catalog.globals_declared == {"FLAG": "go"}


def test_builder_nested_boxes() -> None:
    c = CatalogBuilder()
    with c.box("outer"):
        with c.box("inner"):
            c.job("leaf", command="x", machine="m1")
    catalog = c.build()
    assert catalog.jobs["inner"].box.box_name == "outer"
    assert catalog.jobs["leaf"].box.box_name == "inner"


def test_builder_refuses_silent_condition_merge() -> None:
    c = CatalogBuilder()
    c.job("a", command="x", machine="m1")
    c.job("b", command="y", machine="m1", condition="s(other)")
    with pytest.raises(DslError, match="already has a condition"):
        c.sequence("a", "b")


def test_builder_refuses_undeclared_names_in_wiring() -> None:
    c = CatalogBuilder()
    c.job("a", command="x", machine="m1")
    with pytest.raises(DslError, match="undeclared"):
        c.sequence("a", "ghost")


def test_builder_refuses_duplicate_job() -> None:
    c = CatalogBuilder()
    c.job("a", command="x", machine="m1")
    with pytest.raises(DslError, match="declared twice"):
        c.job("a", command="y", machine="m1")


def test_builder_refuses_newlines_in_values() -> None:
    c = CatalogBuilder()
    with pytest.raises(DslError, match="control"):
        c.job("a", command="evil\ninsert_job: injected", machine="m1")


def test_builder_refuses_non_key_shaped_attrs() -> None:
    c = CatalogBuilder()
    with pytest.raises(DslError, match="not JIL-key-shaped"):
        c.job("a", command="x", machine="m1", **{"bad key": "v"})


def test_builder_goes_through_the_real_pipeline() -> None:
    """DL-17: the builder generates JIL and lowers it through the tested
    pipeline -- unknown attributes hit the DL-07 firewall exactly like any
    other JIL source."""
    from dsl41.ir import LoweringError

    c = CatalogBuilder()
    c.job("a", command="x", machine="m1", frobnicate="1")
    with pytest.raises(LoweringError, match="frobnicate"):
        c.build()
    assert "frobnicate: 1" in c.to_jil()
    assert c.build(permit_unknown=True).jobs["a"].passthrough == {"frobnicate": "1"}


# ----------------------------------------------------------- decompiler specifics


def test_decompiler_emits_sequence_only_for_pure_s_chains() -> None:
    """The corpus's mutex chain (compound condition) must stay an explicit
    job(condition=...); the pure-s() chain becomes sequence() (DL-17)."""
    catalog = lower_catalog([parse_file(p) for p in LOWERABLE_CORPUS])
    source = decompile(catalog)
    assert "c.sequence('upstream_daily', 'consumer_stale')" in source
    assert "'n(mutex_a) & s(mutex_feeder)'" in source  # NOT swallowed by sequence
    assert "c.sequence('mutex_feeder'" not in source


def test_decompiler_emits_boxes_as_with_blocks() -> None:
    catalog = lower_catalog([parse_file(CORPUS_DIR / "sem10_box_basic.jil")])
    source = decompile(catalog)
    assert "with c.box('box_a', box_success='s(job_a)') as b:" in source
    assert "    b.job('job_a'" in source


def test_decompiled_source_is_deterministic() -> None:
    catalog = lower_catalog([parse_file(p) for p in LOWERABLE_CORPUS])
    assert decompile(catalog) == decompile(catalog)


# --------------------------------------------------------------------------- CLI


def test_cli_decompile_stdout_and_roundtrip(tmp_path: Path) -> None:
    result = runner.invoke(app, ["decompile", str(CORPUS_DIR / "sem10_box_basic.jil")])
    assert result.exit_code == 0
    assert result.stdout.startswith("# Decompiled by dsl41")
    namespace: dict[str, object] = {"__name__": "<cli>"}
    exec(compile(result.stdout, "<cli>", "exec"), namespace)  # noqa: S102
    rebuilt = namespace["catalog"]
    assert isinstance(rebuilt, CatalogIR)
    original = lower_catalog([parse_file(CORPUS_DIR / "sem10_box_basic.jil")])
    assert catalog_hash(rebuilt) == catalog_hash(original)


def test_cli_decompile_out_file(tmp_path: Path) -> None:
    target = tmp_path / "rebuilt.py"
    result = runner.invoke(
        app, ["decompile", "--out", str(target), str(CORPUS_DIR / "sem08_globals.jil")]
    )
    assert result.exit_code == 0
    assert target.read_text(encoding="utf-8").startswith("# Decompiled")


def test_cli_decompile_refusal_exits_2() -> None:
    result = runner.invoke(app, ["decompile", str(CORPUS_DIR / "sem31_xor.jil")])
    assert result.exit_code == 2


# =============================================================================
# Phase 10 breadth tests (appended): edge cases beyond the core coverage
# above. Hand-built Cond trees (no raw tokens), global-value quoting edges,
# deep nesting fidelity, builder edge cases, decompiler edge cases, and
# round-trip strengthening (tier b/c + decompiled-source hygiene/purity).
# =============================================================================


# ---------------------------------------------- hand-built Cond trees (no raw)


@pytest.mark.parametrize(
    "minutes,expected_token",
    [(90, "01.30"), (30, "00.30")],  # 30: sub-hour leading-zero pitfall shape
    ids=["90min-as-01.30", "30min-as-00.30"],
)
def test_cond_to_source_hand_built_window_lookback(minutes: int, expected_token: str) -> None:
    """No raw token survives on a hand-built Lookback -- _lookback_token()
    reconstructs the hhhh.mm shape from kind/minutes alone (module
    docstring). 30 minutes must render "00.30", not "0.30" or "30": a bare
    "30" would mean 30 HOURS (SEM-04), the exact pitfall L015 warns about."""
    atom = StatusAtom(
        job=JobRef(name="x"),
        status="SUCCESS",
        lookback=Lookback(kind="window", minutes=minutes, raw=""),
    )
    rendered = cond_to_source(atom)
    assert rendered == f"s(x, {expected_token})"
    assert canonical_cond(parse_condition(rendered)) == canonical_cond(atom)


def test_cond_to_source_hand_built_zero_lookback() -> None:
    atom = StatusAtom(
        job=JobRef(name="x"),
        status="SUCCESS",
        lookback=Lookback(kind="zero", minutes=None, raw=""),
    )
    rendered = cond_to_source(atom)
    assert rendered == "s(x, 0)"
    assert canonical_cond(parse_condition(rendered)) == canonical_cond(atom)


def test_cond_to_source_hand_built_indefinite_lookback_folds_to_bare_atom() -> None:
    """kind="indefinite" renders "9999", but canonical form drops an explicit
    9999 (SEM-04: it means the same thing as no lookback qualifier at all),
    so the reparsed tree must canonically equal the bare, lookback-less atom."""
    atom = StatusAtom(
        job=JobRef(name="x"),
        status="SUCCESS",
        lookback=Lookback(kind="indefinite", minutes=None, raw=""),
    )
    rendered = cond_to_source(atom)
    assert rendered == "s(x, 9999)"
    bare = StatusAtom(job=JobRef(name="x"), status="SUCCESS", lookback=None)
    assert canonical_cond(parse_condition(rendered)) == canonical_cond(bare)


# ------------------------------------------------------- global-value quoting


def test_cond_to_source_quotes_global_value_with_spaces() -> None:
    cond = GlobalAtom(name="G", op="=", value="spaced value")
    rendered = cond_to_source(cond)
    assert rendered == 'v(G) = "spaced value"'
    assert canonical_cond(parse_condition(rendered)) == canonical_cond(cond)


@pytest.mark.parametrize("value", ["a(b)", "a&b"])
def test_cond_to_source_quotes_global_value_with_grammar_metacharacters(value: str) -> None:
    """'(' and '&' are excluded from BARE_VALUE (condition.lark) -- a value
    containing either must be quoted or the grammar could not parse it back."""
    cond = GlobalAtom(name="G", op="=", value=value)
    rendered = cond_to_source(cond)
    assert rendered == f'v(G) = "{value}"'
    assert canonical_cond(parse_condition(rendered)) == canonical_cond(cond)


def test_cond_to_source_refuses_global_value_with_embedded_quote() -> None:
    cond = GlobalAtom(name="G", op="=", value='he said "hi"')
    with pytest.raises(DslError, match="embedded quote"):
        cond_to_source(cond)


def test_cond_to_source_quotes_empty_global_value_and_grammar_accepts_it() -> None:
    """QUOTED.2: /"[^"]*"/ matches a zero-length body, so v(G) = "" is
    grammatically legal -- only BARE_VALUE (one-or-more chars) would have
    rejected an empty comparand."""
    cond = GlobalAtom(name="G", op="=", value="")
    rendered = cond_to_source(cond)
    assert rendered == 'v(G) = ""'
    assert canonical_cond(parse_condition(rendered)) == canonical_cond(cond)


# ---------------------------------------------------- deep nesting fidelity


def test_cond_to_source_deep_alternating_nesting_is_a_fixpoint() -> None:
    """4 levels of Paren-wrapped And/Or/And/Or -- cond_to_source parenthesizes
    every nested group (module docstring), so a second render of the
    reparsed tree is byte-identical to the first (fixpoint) and canonically
    equal to the original."""
    source = "(s(a) & (s(b) | (s(c) & (s(d) | s(e)))))"
    cond = parse_condition(source)
    rendered = cond_to_source(cond)
    reparsed = parse_condition(rendered)
    assert cond_to_source(reparsed) == rendered  # fixpoint
    assert canonical_cond(reparsed) == canonical_cond(cond)


# ------------------------------------------------------------ builder edge cases


def test_parallel_without_after_or_then_wires_nothing() -> None:
    """parallel(names) with neither after= nor then= is accepted: fan-out and
    fan-in are each optional per-call, so with neither given the call is a
    documented no-op rather than an error -- DL-17 only refuses a MERGE onto
    an already-conditioned job, never a plain no-op wiring."""
    c = CatalogBuilder()
    c.job("fan1", command="f1", machine="m1")
    c.job("fan2", command="f2", machine="m1")
    c.parallel(["fan1", "fan2"])  # must not raise
    catalog = c.build()
    assert catalog.jobs["fan1"].sem.condition is None
    assert catalog.jobs["fan2"].sem.condition is None


def test_parallel_with_then_only_fans_in_without_fan_out() -> None:
    c = CatalogBuilder()
    c.job("m1j", command="a", machine="m1", condition="s(other1)")
    c.job("m2j", command="b", machine="m1", condition="s(other2)")
    c.job("join", command="j", machine="m1")
    c.parallel(["m1j", "m2j"], then="join")
    catalog = c.build()
    assert (
        cond_to_source(catalog.jobs["join"].sem.condition)  # type: ignore[arg-type]
        == "s(m1j) & s(m2j)"
    )


def test_sequence_of_exactly_two_is_the_minimum() -> None:
    c = CatalogBuilder()
    c.job("head", command="h", machine="m1")
    c.job("tail", command="t", machine="m1")
    c.sequence("head", "tail")
    catalog = c.build()
    assert (
        cond_to_source(catalog.jobs["tail"].sem.condition)  # type: ignore[arg-type]
        == "s(head)"
    )


def test_box_accepts_a_condition_kwarg() -> None:
    """A box job is itself an insert_job with job_type=b -- its own
    `condition` gates when the box starts, independent of its members."""
    c = CatalogBuilder()
    c.job("gate", command="g", machine="m1")
    with c.box("bx", condition="s(gate)") as b:
        b.job("member", command="m", machine="m1")
    catalog = c.build()
    assert (
        cond_to_source(catalog.jobs["bx"].sem.condition)  # type: ignore[arg-type]
        == "s(gate)"
    )
    assert catalog.jobs["member"].box.box_name == "bx"


def test_job_schedule_list_join_and_quoted_start_times_roundtrip() -> None:
    """List-valued kwargs join with ', ' (days_of_week); start_times needs
    the pre-quoted single-string shape JIL uses so its own internal commas
    are not mistaken for separate attribute tokens (module docstring)."""
    c = CatalogBuilder()
    c.job(
        "sched",
        command="run.sh",
        machine="m1",
        date_conditions=True,
        days_of_week=["mo", "tu"],
        start_times='"10:00, 11:00"',
    )
    assert "days_of_week: mo, tu" in c.to_jil()
    catalog = c.build()
    schedule = catalog.jobs["sched"].schedule
    assert schedule is not None
    assert schedule.start_times == [Time(hour=10, minute=0), Time(hour=11, minute=0)]
    assert schedule.days_of_week == ["mo", "tu"]
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(catalog)


def test_job_bool_false_renders_as_zero_and_lowers_clean() -> None:
    c = CatalogBuilder()
    c.job("boolj", command="x", machine="m1", auto_hold=False)
    assert "auto_hold: 0" in c.to_jil()
    catalog = c.build()
    assert catalog.jobs["boolj"].sem.auto_hold is False
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(catalog)


def test_machine_with_no_attrs() -> None:
    c = CatalogBuilder()
    c.machine("m_bare")
    catalog = c.build()
    assert catalog.machines["m_bare"].machine_type is None
    assert catalog.machines["m_bare"].attrs == {}


def test_resource_builder_populates_resources() -> None:
    c = CatalogBuilder()
    c.resource("lock1", res_type="R", amount="2", description="serializer")
    catalog = c.build()
    resource = catalog.resources["lock1"]
    assert resource.res_type == "R"
    assert resource.attrs == {"amount": "2", "description": "serializer"}
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(catalog)


def test_calendar_builders_populate_catalog_and_roundtrip() -> None:
    """DL-36: the three autocal-export builders feed CatalogIR and survive
    the decompile round-trip hash-equal."""
    c = CatalogBuilder()
    c.calendar("hols", dates=["01/01/2026 00:00", "12/25/2026 00:00"], description="bank")
    c.cycle("q1", start_date="03/28/2026", end_date="04/02/2026")
    c.extended_calendar("eom", workday="mo,tu,we,th,fr", holcal="hols", cyccal="q1", adjust="0")
    catalog = c.build()
    assert catalog.calendars["hols"].dates == ["01/01/2026 00:00", "12/25/2026 00:00"]
    assert catalog.calendars["eom"].kind == "extended"
    assert catalog.cycles["q1"].attrs["start_date"] == "03/28/2026"
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(catalog)


def test_calendar_date_row_that_would_reparse_as_an_attribute_is_refused() -> None:
    with pytest.raises(DslError, match="re-parse as an attribute"):
        CatalogBuilder().calendar("hols", dates=["surprise: 01/01/2026"])


def test_resources_kwarg_round_trips_typed() -> None:
    """DL-21: the decompiler renders resources groups canonically; rebuilding
    yields the same typed refs and an equal canonical hash."""
    c = CatalogBuilder()
    c.job(
        "resj", command="x", machine="m1", resources="(r1, QUANTITY=2, FREE=A) and (r2, QUANTITY=1)"
    )
    catalog = c.build()
    refs = catalog.jobs["resj"].resources
    assert [(r.name, r.quantity, r.free) for r in refs] == [("r1", 2, "A"), ("r2", 1, None)]
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(catalog)


def test_status_kwarg_round_trips_as_initial_status() -> None:
    """SEM-24 (DL-18): definition-time status is a plain job kwarg."""
    c = CatalogBuilder()
    c.job("heldj", command="x", machine="m1", status="ON_HOLD")
    catalog = c.build()
    assert catalog.jobs["heldj"].sem.initial_status == "ON_HOLD"
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(catalog)


def test_xinst_alone_populates_external_instances() -> None:
    c = CatalogBuilder()
    c.xinst("PRD", xtype="a")
    catalog = c.build()
    assert catalog.external_instances["PRD"].xtype == "a"
    assert catalog.external_instances["PRD"].attrs == {}


def test_xinst_carries_plumbing_attrs_and_decompiles() -> None:
    """DL-28: builder kwargs -> JIL -> XinstIR.attrs, and the decompiler
    reproduces the call."""
    c = CatalogBuilder()
    c.xinst("PRD", xtype="a", xmachine="prd.example.com", xport="9000")
    catalog = c.build()
    prd = catalog.external_instances["PRD"]
    assert prd.attrs == {"xmachine": "prd.example.com", "xport": "9000"}
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(catalog)


def test_global_refuses_control_char_value() -> None:
    c = CatalogBuilder()
    with pytest.raises(DslError, match="control"):
        c.global_("G", "bad\nvalue")


def test_box_refuses_name_with_space() -> None:
    c = CatalogBuilder()
    with pytest.raises(DslError, match="not JIL-name-shaped"):
        c.box("bad name")


def test_to_jil_output_is_parseable_with_matching_statement_count() -> None:
    """DL-17: the builder generates real JIL text -- ast_jil.parse must
    accept it byte for byte, one statement per record/job declaration."""
    c = CatalogBuilder()
    c.global_("FLAG", "go")
    c.machine("m1")
    c.xinst("PRD", xtype="a")
    c.job("a", command="x", machine="m1")
    parsed = parse(c.to_jil(), file="<t>")
    assert len(parsed.statements) == 4


# --------------------------------------------------------- decompiler edge cases


def test_decompile_fw_job_roundtrips_via_watch_kwargs() -> None:
    catalog = lower_source(
        "insert_job: watcher\njob_type: f\nmachine: m1\nwatch_file: /tmp/x\nwatch_interval: 30\n"
    )
    source = decompile(catalog)
    assert "job_type='f'" in source
    assert "watch_file=" in source
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(catalog)


def test_decompile_all_extra_job_kwargs_roundtrip() -> None:
    catalog = lower_source(
        "insert_job: bx\njob_type: b\n\n"
        "insert_job: member\nbox_name: bx\njob_type: c\nmachine: m1\ncommand: run.sh\n"
        "max_exit_success: 4\nterm_run_time: 30\nn_retrys: 2\nauto_hold: 1\n"
        "box_terminator: 1\njob_terminator: 1\n"
    )
    source = decompile(catalog)
    for token in (
        "max_exit_success=4",
        "term_run_time=30",
        "n_retrys=2",
        "auto_hold=True",
        "box_terminator=True",
        "job_terminator=True",
    ):
        assert token in source
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(catalog)


def test_decompile_empty_box_emits_pass_and_roundtrips() -> None:
    """Build via lower_source: a box job with no members. The decompiler's
    with-block cannot be empty Python, so an empty box emits a `pass`."""
    catalog = lower_source("insert_job: empty_box\njob_type: b\n")
    source = decompile(catalog)
    assert "with c.box('empty_box') as b:" in source
    assert "    pass" in source
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(catalog)


def test_decompile_job_named_like_a_python_keyword_roundtrips() -> None:
    """Job names are Python STRING LITERALS in the decompiled source (_py()
    is repr()), never identifiers -- 'import' is unremarkable JIL and
    unremarkable Python once it sits inside quotes."""
    catalog = lower_source("insert_job: import\njob_type: c\nmachine: m1\ncommand: echo hi\n")
    source = decompile(catalog)
    assert "c.job('import'" in source
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(catalog)


def test_decompile_condition_with_crossinstance_colons_and_lookback_roundtrips() -> None:
    catalog = lower_source(
        "insert_job: consumer\njob_type: c\nmachine: m1\ncommand: echo hi\n"
        "condition: s(JOB\\:WITH\\:COLONS^PRD, 01\\:30)\n"
    )
    assert (
        cond_to_source(catalog.jobs["consumer"].sem.condition)  # type: ignore[arg-type]
        == r"s(JOB\:WITH\:COLONS^PRD, 01\:30)"
    )
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(catalog)


def test_decompile_annotations_with_embedded_quotes_roundtrip() -> None:
    c = CatalogBuilder()
    c.job("a", command="x", machine="m1", annotations={"description": 'she said "hi" ok'})
    catalog = c.build()
    rebuilt = roundtrip(catalog)
    assert rebuilt.jobs["a"].annotations == catalog.jobs["a"].annotations
    assert catalog_hash(rebuilt) == catalog_hash(catalog)


# ------------------------------------------------------ round-trip strengthening


def test_whole_corpus_roundtrip_is_tier_b_equivalent() -> None:
    """Beyond tier a's structural hash equality: conditions are semantically
    identical over the full state-space enumeration and the derived graph
    (edges/mutex groups/box tree) matches too."""
    catalog = lower_catalog([parse_file(p) for p in LOWERABLE_CORPUS])
    rebuilt = roundtrip(catalog)
    result = equivalent_tier_b(catalog, rebuilt)
    assert result.equivalent


def test_whole_corpus_roundtrip_is_tier_c_equivalent_over_seeded_scripts() -> None:
    """Oracle-level honesty check: no seeded event script tells the original
    and the decompiled-then-rebuilt catalog apart."""
    catalog = lower_catalog([parse_file(p) for p in LOWERABLE_CORPUS])
    rebuilt = roundtrip(catalog)
    scripts = equiv_scripts(catalog, scripts=5)
    result = equivalent_tier_c(catalog, rebuilt, scripts)
    assert result.equivalent


# --------------------------------------------------- decompiled source hygiene


def test_decompiled_source_ends_with_newline_and_has_no_tabs() -> None:
    catalog = lower_catalog([parse_file(p) for p in LOWERABLE_CORPUS])
    source = decompile(catalog)
    assert source.endswith("\n")
    assert "\t" not in source
    # A per-line <=200-char cap does NOT hold corpus-wide: jobs with many
    # kwargs (torture_colon's colon_torture, sem30_schedule's
    # test_must_start_complete) legitimately produce longer lines. DL-17
    # commits to no silent loss, not to a line-length budget, so no such cap
    # is asserted here (verified empirically, not assumed).


def test_decompiled_source_is_pure_across_independent_execs() -> None:
    """exec'ing the decompiled module twice, in two independent fresh
    namespaces (roundtrip() builds a new namespace dict each call), must not
    depend on hidden state -- both rebuilds hash equal."""
    catalog = lower_catalog([parse_file(p) for p in LOWERABLE_CORPUS])
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(roundtrip(catalog))
