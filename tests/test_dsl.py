"""DSL surface + decompiler tests (phase 10, DL-17): the four
corpus-extracted builders, cond_to_source fidelity, and the round-trip
property -- decompile(catalog), exec it, and the rebuilt catalog's
canonical form equals the original's.
"""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from typer.testing import CliRunner

from dsl41.ast_jil import parse, parse_file
from dsl41.cli import app
from dsl41.conditions import GlobalAtom, JobRef, Lookback, StatusAtom, parse_condition
from dsl41.dsl import FOLDS, CatalogBuilder, DslError, cond_to_source, decompile
from dsl41.equiv import (
    canonical_cond,
    catalog_hash,
    equiv_scripts,
    equivalent_tier_a,
    equivalent_tier_b,
    equivalent_tier_c,
)
from dsl41.ir import (
    CatalogIR,
    ExecSpec,
    FwSpec,
    ScheduleBlock,
    Semantics,
    Time,
    lower_catalog,
    lower_source,
)

CORPUS_DIR = Path(__file__).parent / "corpus"
EXPECT_LOWER_ERROR = {"sem31_xor.jil"}
LOWERABLE_CORPUS = [p for p in sorted(CORPUS_DIR.glob("*.jil")) if p.name not in EXPECT_LOWER_ERROR]

runner = CliRunner()


def roundtrip(catalog: CatalogIR, *, disable: Collection[str] = ()) -> CatalogIR:
    source = decompile(catalog, disable=disable)
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


def test_decompiler_folds_compose_on_the_corpus_mutex_chain() -> None:
    """DL-38: detection runs on mutex-stripped residuals, so the corpus's
    mutex chain -- `n(mutex_a) & s(mutex_feeder)` on mutex_b -- folds as
    sequence() + mutex(), and the emitted module re-conjoins them (wiring
    order: sequences before mutex). The self-excluding mutex_serial stays
    an explicit condition (one-way/self n() never folds)."""
    catalog = lower_catalog([parse_file(p) for p in LOWERABLE_CORPUS])
    source = decompile(catalog)
    assert "c.sequence('upstream_daily', 'consumer_stale')" in source
    assert "c.sequence('mutex_feeder', 'mutex_b')" in source
    assert "c.mutex('mutex_a', 'mutex_b')" in source
    assert "'n(mutex_a) & s(mutex_feeder)'" not in source  # folded, not verbatim
    assert "condition='n(mutex_serial)'" in source  # self-mutex stays explicit
    assert source.index("c.sequence('mutex_feeder'") < source.index("c.mutex(")


def test_decompiler_keeps_mutex_chain_explicit_when_folds_disabled() -> None:
    """--no-fold T-005 (and T-001) restores the DL-17 pinning: the compound
    condition stays an explicit job(condition=...), never swallowed."""
    catalog = lower_catalog([parse_file(p) for p in LOWERABLE_CORPUS])
    source = decompile(catalog, disable=("T-005",))
    assert "'n(mutex_a) & s(mutex_feeder)'" in source  # NOT swallowed by sequence
    assert "c.sequence('mutex_feeder'" not in source
    assert "c.mutex(" not in source


def test_decompiler_emits_boxes_as_with_blocks() -> None:
    catalog = lower_catalog([parse_file(CORPUS_DIR / "sem10_box_basic.jil")])
    source = decompile(catalog)
    assert "with c.box('box_a', box_success='s(job_a)') as b:" in source
    assert "    b.job('job_a'" in source


def test_decompiled_source_is_deterministic() -> None:
    catalog = lower_catalog([parse_file(p) for p in LOWERABLE_CORPUS])
    assert decompile(catalog) == decompile(catalog)


# --------------------------------------------------------- parallel() sugar (DL-37)


def _job_line(source: str, name: str) -> str:
    """The exact `c.job(...)`/`b.job(...)`/`c.box(...)` call line naming
    `name` as its subject -- used to check that condition= is suppressed on
    parallel()/sequence() members (their condition lives in the wiring line
    instead, module docstring)."""
    needle = f"({name!r}"
    matches = [
        line
        for line in source.splitlines()
        if (".job(" in line or ".box(" in line) and needle in line
    ]
    assert len(matches) == 1, f"expected exactly one call line for {name!r}, got {matches}"
    return matches[0]


def test_decompile_emits_parallel_for_fanout_without_join() -> None:
    """DL-37 fan-out: >= 2 jobs whose entire condition is exactly s(seed) for
    an in-catalog producer become c.parallel([...], after=...) with no
    then=; each member's own condition= is suppressed."""
    c = CatalogBuilder()
    c.job("seed", command="s", machine="m1")
    c.job("a", command="a", machine="m1", condition="s(seed)")
    c.job("b", command="b", machine="m1", condition="s(seed)")
    c.job("c", command="cc", machine="m1", condition="s(seed)")
    catalog = c.build()
    source = decompile(catalog)
    assert "c.parallel(['a', 'b', 'c'], after='seed')" in source
    for member in ("a", "b", "c"):
        assert "condition=" not in _job_line(source, member)
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(catalog)


def test_decompile_emits_parallel_with_unique_join() -> None:
    """Adding the unique job whose condition is exactly the conjunction of
    the members' plain successes upgrades the call to then=...; the join's
    own condition= is suppressed too."""
    c = CatalogBuilder()
    c.job("seed", command="s", machine="m1")
    c.job("a", command="a", machine="m1", condition="s(seed)")
    c.job("b", command="b", machine="m1", condition="s(seed)")
    c.job("join", command="j", machine="m1", condition="s(a) & s(b)")
    catalog = c.build()
    source = decompile(catalog)
    assert "c.parallel(['a', 'b'], after='seed', then='join')" in source
    for member in ("a", "b", "join"):
        assert "condition=" not in _job_line(source, member)
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(catalog)


def test_decompile_parallel_disqualified_by_member_lookback() -> None:
    """A lookback on a member's condition is a looser incoming shape than
    plain s(p) -- the whole group stays explicit (here: group size drops to
    1, below the >= 2 fan-out threshold)."""
    c = CatalogBuilder()
    c.job("seed", command="s", machine="m1")
    c.job("a", command="a", machine="m1", condition="s(seed)")
    c.job("b", command="b", machine="m1", condition="s(seed, 12.00)")
    catalog = c.build()
    source = decompile(catalog)
    assert "c.parallel(" not in source
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(catalog)


def test_decompile_parallel_disqualified_by_member_instance_qualifier() -> None:
    """s(seed^PRD) fails _plain_s_condition's instance check even though the
    base name 'seed' matches an in-catalog producer -- the fanout dict's
    catalog-membership test looks at the base name, but the per-member
    plain-shape test still requires cond.job.instance is None, so the
    instance-qualified member never joins the group."""
    c = CatalogBuilder()
    c.job("seed", command="s", machine="m1")
    c.job("a", command="a", machine="m1", condition="s(seed)")
    c.job("b", command="b", machine="m1", condition="s(seed^PRD)")
    catalog = c.build()
    source = decompile(catalog)
    assert "c.parallel(" not in source
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(catalog)


def test_decompile_parallel_disqualified_by_extra_conjunct() -> None:
    """A member ANDing in an extra atom is not exactly s(p) -- stays explicit."""
    c = CatalogBuilder()
    c.job("seed", command="s", machine="m1")
    c.job("a", command="a", machine="m1", condition="s(seed)")
    c.job("b", command="b", machine="m1", condition="s(seed) & s(other)")
    catalog = c.build()
    source = decompile(catalog)
    assert "c.parallel(" not in source
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(catalog)


def test_decompile_parallel_disqualified_by_undefined_producer() -> None:
    """Two consumers share exactly s(seed), but no insert_job defines 'seed'
    -- the fanout grouping requires the producer to be IN the catalog, so
    both consumers stay explicit job(condition=...) calls."""
    c = CatalogBuilder()
    c.job("a", command="a", machine="m1", condition="s(seed)")
    c.job("b", command="b", machine="m1", condition="s(seed)")
    catalog = c.build()
    source = decompile(catalog)
    assert "c.parallel(" not in source
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(catalog)


def test_decompile_parallel_join_ambiguity_stays_explicit() -> None:
    """Two DIFFERENT jobs each carry exactly the conjunction of the member
    set -- the join is ambiguous, so parallel() is emitted WITHOUT then=,
    and both candidate joins keep their own explicit condition=."""
    c = CatalogBuilder()
    c.job("seed", command="s", machine="m1")
    c.job("a", command="a", machine="m1", condition="s(seed)")
    c.job("b", command="b", machine="m1", condition="s(seed)")
    c.job("join1", command="j1", machine="m1", condition="s(a) & s(b)")
    c.job("join2", command="j2", machine="m1", condition="s(b) & s(a)")
    catalog = c.build()
    source = decompile(catalog)
    assert "c.parallel(['a', 'b'], after='seed')" in source
    assert "then=" not in source
    assert "condition='s(a) & s(b)'" in _job_line(source, "join1")
    assert "condition='s(b) & s(a)'" in _job_line(source, "join2")
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(catalog)


def test_decompile_parallel_join_over_subset_of_members_stays_explicit() -> None:
    """A conjunction over a strict SUBSET of the fan-out members has a
    different operand count than the member set, so it never qualifies as
    the join (same length-mismatch path covers a superset) -- no then=, and
    the near-miss job keeps its own explicit condition=."""
    c = CatalogBuilder()
    c.job("seed", command="s", machine="m1")
    c.job("a", command="a", machine="m1", condition="s(seed)")
    c.job("b", command="b", machine="m1", condition="s(seed)")
    c.job("c", command="cc", machine="m1", condition="s(seed)")
    c.job("subjoin", command="sj", machine="m1", condition="s(a) & s(b)")
    catalog = c.build()
    source = decompile(catalog)
    assert "c.parallel(['a', 'b', 'c'], after='seed')" in source
    assert "then=" not in source
    assert "condition='s(a) & s(b)'" in _job_line(source, "subjoin")
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(catalog)


def test_decompile_emits_parallel_for_box_members() -> None:
    """DL-37's field requirement: parallel boxes with > 10 same-producer
    members exist at least twice in the target estate. Built via lower_source
    with raw box_name attrs (not the builder) to mirror that shape and prove
    fan-out grouping does not care whether members sit inside a box -- the
    wiring line is still emitted at module level (c.parallel(...), not
    b.parallel(...)) since parallel() only exists on CatalogBuilder."""
    members = [f"member{i}" for i in range(1, 13)]
    text = "insert_job: seed\njob_type: c\nmachine: m1\ncommand: s\n\n"
    text += "insert_job: bx\njob_type: b\n\n"
    for name in members:
        text += (
            f"insert_job: {name}\nbox_name: bx\njob_type: c\nmachine: m1\n"
            f"command: run.sh\ncondition: s(seed)\n\n"
        )
    catalog = lower_source(text)
    source = decompile(catalog)
    expected_args = "[" + ", ".join(repr(n) for n in members) + "]"
    assert f"c.parallel({expected_args}, after='seed')" in source
    for name in members:
        assert "condition=" not in _job_line(source, name)
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(catalog)


def test_decompile_emits_both_sequence_and_parallel_without_overlap() -> None:
    """A catalog with both a pure s-chain and a fan-out emits both wirings;
    disjointness is structural (module docstring / derive._chains: a
    producer with >= 2 successors can never be a chain link), so no
    c.sequence(...) line ever names a fan-out member."""
    c = CatalogBuilder()
    c.job("seed", command="s", machine="m1")
    c.job("a", command="a", machine="m1", condition="s(seed)")
    c.job("b", command="b", machine="m1", condition="s(seed)")
    c.job("head", command="h", machine="m1")
    c.job("mid", command="m", machine="m1")
    c.job("tail", command="t", machine="m1")
    c.sequence("head", "mid", "tail")
    catalog = c.build()
    source = decompile(catalog)
    assert "c.sequence('head', 'mid', 'tail')" in source
    assert "c.parallel(['a', 'b'], after='seed')" in source
    sequence_lines = [
        line for line in source.splitlines() if line.strip().startswith("c.sequence(")
    ]
    assert sequence_lines
    for line in sequence_lines:
        assert "'a'" not in line
        assert "'b'" not in line
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(catalog)


# ------------------------------------------- decompiler completeness (DL-37)


def _typed_field_names(model: type) -> list[str]:
    """model_fields minus provenance/derived lanes: `*_span` pointers carry
    no decompiler-visible value, and `kind` is a Literal discriminator, not
    a typed attribute (DL-37 finding 1's blind spot was exactly a typed
    attribute with no corpus fixture -- this enumerates every candidate)."""
    return [name for name in model.model_fields if name != "kind" and not name.endswith("_span")]


_SEMANTICS_FIELDS = [("Semantics", name) for name in _typed_field_names(Semantics)]
_EXEC_FIELDS = [("ExecSpec", name) for name in _typed_field_names(ExecSpec)]
_FW_FIELDS = [("FwSpec", name) for name in _typed_field_names(FwSpec)]
_SCHEDULE_FIELDS = [("ScheduleBlock", name) for name in _typed_field_names(ScheduleBlock)]
#: Non-model-field lanes the decompiler also renders (module docstring /
#: _job_kwargs): box linkage, box membership, and the FW job_type itself.
_EXTRA_FIELDS = [
    ("Extra", "resources"),
    ("Extra", "annotations"),
    ("Extra", "passthrough"),
    ("Extra", "box.box_name"),
    ("Extra", "box_terminator"),
    ("Extra", "job_terminator"),
    ("Extra", "job_type==FW"),
]
_FIELD_CASES = _SEMANTICS_FIELDS + _EXEC_FIELDS + _FW_FIELDS + _SCHEDULE_FIELDS + _EXTRA_FIELDS

#: Fields this sweep found with NO corpus witness today. Reported upstream
#: (final message) rather than papered over by extending the corpus (corpus
#: hygiene / CLAUDE.md): ExecSpecBase fields are shared by CMD and FW jobs,
#: but the corpus's only FW job (kitchen_sink's sink_fw) sets just
#: machine/watch_* -- no FW job anywhere sets owner/profile/std_out_file/
#: std_err_file. Box terminators are not witnessed by any corpus job at all.
#: Fields the sweep found unwitnessed get a corpus fixture, not an entry
#: here -- the set is expected to stay empty (DL-37a closed the six gaps
#: the first run of this sweep reported: FwSpec owner/profile/std_out_file/
#: std_err_file and the box_terminator/job_terminator flags, all now in
#: kitchen_sink.jil).
_NO_CORPUS_WITNESS: set[tuple[str, str]] = set()


@pytest.mark.parametrize(
    "model_name,field",
    _FIELD_CASES,
    ids=[f"{model_name}.{field}" for model_name, field in _FIELD_CASES],
)
def test_corpus_witnesses_every_decompiler_visible_field(model_name: str, field: str) -> None:
    """DL-37 finding (1): _job_kwargs predated the DL-32/DL-33 doc sweep and
    silently dropped success_codes/fail_codes/std_in_file/envvars because no
    corpus fixture carried a non-default value for them -- the whole-corpus
    round-trip test was blind to the gap (it can only catch what the corpus
    actually exercises). This is the structural guard against a repeat: every
    typed field of Semantics/ExecSpec/FwSpec/ScheduleBlock, plus the handful
    of non-model-field lanes the decompiler also renders, must have at least
    one non-default witness somewhere in the corpus. A future typed field
    with no fixture FAILS here instead of silently round-tripping as None."""
    if (model_name, field) in _NO_CORPUS_WITNESS:
        pytest.skip(f"{model_name}.{field} has no corpus witness today (reported, not padded)")
    catalog = lower_catalog([parse_file(p) for p in LOWERABLE_CORPUS])
    jobs = catalog.jobs.values()
    if model_name == "Semantics":
        default = Semantics.model_fields[field].get_default(call_default_factory=True)
        witnessed = any(getattr(job.sem, field) != default for job in jobs)
    elif model_name in ("ExecSpec", "FwSpec"):
        kind = "cmd" if model_name == "ExecSpec" else "fw"
        model = ExecSpec if model_name == "ExecSpec" else FwSpec
        default = model.model_fields[field].get_default(call_default_factory=True)
        witnessed = any(
            job.exec_ is not None
            and job.exec_.kind == kind
            and getattr(job.exec_, field) != default
            for job in jobs
        )
    elif model_name == "ScheduleBlock":
        default = ScheduleBlock.model_fields[field].get_default(call_default_factory=True)
        witnessed = any(
            job.schedule is not None and getattr(job.schedule, field) != default for job in jobs
        )
    elif field == "resources":
        witnessed = any(job.resources for job in jobs)
    elif field == "annotations":
        witnessed = any(job.annotations for job in jobs)
    elif field == "passthrough":
        witnessed = any(job.passthrough for job in jobs)
    elif field == "box.box_name":
        witnessed = any(job.box.box_name for job in jobs)
    elif field == "box_terminator":
        witnessed = any(job.box.box_terminator for job in jobs)
    elif field == "job_terminator":
        witnessed = any(job.box.job_terminator for job in jobs)
    elif field == "job_type==FW":
        witnessed = any(job.job_type == "FW" for job in jobs)
    else:  # pragma: no cover -- exhaustive by construction of _FIELD_CASES
        raise AssertionError(f"unhandled case {model_name}.{field}")
    assert witnessed, f"no corpus job carries a non-default {model_name}.{field}"


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


def test_cli_decompile_no_check_matches_check_output() -> None:
    """--check defaults on and already round-trips clean on the corpus
    (test_cli_decompile_stdout_and_roundtrip); --no-check must skip the exec
    verification but emit byte-identical stdout and still exit 0 (DL-37)."""
    target = str(CORPUS_DIR / "sem10_box_basic.jil")
    checked = runner.invoke(app, ["decompile", target])
    unchecked = runner.invoke(app, ["decompile", "--no-check", target])
    assert checked.exit_code == 0
    assert unchecked.exit_code == 0
    assert unchecked.stdout == checked.stdout


def test_cli_decompile_check_failure_exits_1_but_still_writes_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-37 finding (3): --check executes the emitted module and diffs
    canonical hashes; a genuine decompiler gap must still emit the module
    for inspection but exit 1 with a loud stderr message. cli.py imports
    decompile INSIDE the command function (`from dsl41.dsl import decompile
    as decompile_catalog`), so patching the module attribute dsl41.dsl.decompile
    is what the deferred import actually picks up."""
    broken_source = (
        "from dsl41.dsl import CatalogBuilder\n\n"
        "c = CatalogBuilder()\n"
        "c.job('only_one_job', command='x', machine='m1')\n"
        "catalog = c.build()\n"
    )
    monkeypatch.setattr("dsl41.dsl.decompile", lambda catalog, **kwargs: broken_source)
    target = tmp_path / "rebuilt.py"
    result = runner.invoke(
        app, ["decompile", "--out", str(target), str(CORPUS_DIR / "sem10_box_basic.jil")]
    )
    assert result.exit_code == 1
    assert "round-trip check FAILED" in result.stderr
    assert target.read_text(encoding="utf-8") == broken_source


def test_cli_decompile_check_survives_a_module_that_raises(tmp_path: Path) -> None:
    """DL-37a review finding 1 (MAJOR): a module the builder refuses to
    execute (here: a calendar name with outer spaces, legal in IR but not
    calendar-name-shaped for the builder) must still be emitted, with a
    clean stderr message and exit 1 -- not an uncaught traceback that eats
    the module."""
    source_jil = tmp_path / "spaced.jil"
    source_jil.write_text('calendar: " padded "\n01/01/2026 00:00\n', encoding="utf-8")
    target = tmp_path / "rebuilt.py"
    result = runner.invoke(app, ["decompile", "--out", str(target), str(source_jil)])
    assert result.exit_code == 1
    assert "round-trip check FAILED" in result.stderr
    assert "the emitted module raised DslError" in result.stderr
    assert "Traceback" not in result.stderr
    assert target.exists()  # emitted BEFORE the check ran


def test_cli_decompile_refusal_is_a_clean_exit_2(tmp_path: Path) -> None:
    """DL-37a review finding 1, decompile-time half: a standard calendar
    carrying an attr literally named `dates` makes decompile() itself
    refuse (nothing emittable) -- a clean exit-2 refusal, not a traceback."""
    source_jil = tmp_path / "datesattr.jil"
    source_jil.write_text("calendar: c1\ndates: bogus\n01/01/2026 00:00\n", encoding="utf-8")
    result = runner.invoke(app, ["decompile", str(source_jil)])
    assert result.exit_code == 2
    assert "decompile refused" in result.stderr
    assert "Traceback" not in result.stderr


def test_decompile_keeps_empty_string_machine_and_resource_types() -> None:
    """DL-37a review finding 3: `type:`/`res_type:` with an empty value are
    legal opaque records; truthiness guards used to silently drop them."""
    catalog = lower_source(
        "insert_machine: m_empty\ntype:\n\ninsert_resource: r_empty\nres_type:\n"
    )
    assert catalog.machines["m_empty"].machine_type == ""
    assert catalog.resources["r_empty"].res_type == ""
    rebuilt = roundtrip(catalog)
    assert rebuilt.machines["m_empty"].machine_type == ""
    assert rebuilt.resources["r_empty"].res_type == ""
    assert catalog_hash(rebuilt) == catalog_hash(catalog)


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


def test_calendar_name_with_spaces_is_quoted_and_roundtrips() -> None:
    """DL-37 finding (5): calendar names may carry spaces (TechDocs' own
    example is "shopping days") -- quoted on emission, unquoted at lowering,
    and the round trip survives decompile+exec hash-equal."""
    c = CatalogBuilder()
    c.calendar("shopping days", dates=["01/01/2026 00:00"])
    assert 'calendar: "shopping days"' in c.to_jil()
    catalog = c.build()
    assert catalog.calendars["shopping days"].dates == ["01/01/2026 00:00"]
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(catalog)


@pytest.mark.parametrize(
    "name",
    ["bad\tname", 'bad"name', " leading space", "trailing space "],
    ids=["tab", "embedded-quote", "leading-space", "trailing-space"],
)
def test_calendar_name_shape_refusals(name: str) -> None:
    """Tabs, embedded double quotes, and non-stripped names would not
    survive the quote/unquote round trip a spaced name depends on."""
    with pytest.raises(DslError, match="not calendar-name-shaped"):
        CatalogBuilder().calendar(name, dates=["01/01/2026 00:00"])


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


def test_decompile_splats_record_attrs_colliding_with_keywords_or_name() -> None:
    """DL-37 finding (5) / DL-34a: opaque insert_machine attrs literally
    named `class` (a Python keyword) or `name` (the builder's positional-only
    param) would otherwise produce a module that fails to compile/collides;
    _record_kwargs routes them through a **{} splat instead (machine(),
    resource(), xinst() attrs are all opaque, DL-18 -- machine exercises it
    here)."""
    catalog = lower_source("insert_machine: m1\ntype: r\nclass: heavy\nname: alt\n")
    source = decompile(catalog)
    assert "**{'class':" in source
    assert "'name':" in source
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(catalog)


def test_decompile_code_ranges_single_code_and_full_span_roundtrip() -> None:
    """_code_ranges surface forms (DL-37 finding 1): a single exit code
    (lo == hi) renders bare, and a wide explicit range renders lo-hi."""
    catalog = lower_source(
        "insert_job: coded\njob_type: c\nmachine: m1\ncommand: run.sh\n"
        "success_codes: 4\nfail_codes: 0-9999\n"
    )
    source = decompile(catalog)
    assert "success_codes='4'" in source
    assert "fail_codes='0-9999'" in source
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(catalog)


def test_decompiled_module_footer_prints_jil_when_run_as_main(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """DL-37 finding (4): the emitted module ends with an
    `if __name__ == "__main__":` footer that prints c.to_jil() -- running
    the module as a script is the documented iterate-and-diff loop. Exec
    with __name__ == "__main__" fires the footer; the captured stdout must
    itself be valid JIL that lowers to a hash-equal catalog."""
    catalog = lower_catalog([parse_file(p) for p in LOWERABLE_CORPUS])
    source = decompile(catalog)
    assert source.endswith("    sys.stdout.write(c.to_jil())\n")
    assert '\nif __name__ == "__main__":\n' in source
    namespace: dict[str, object] = {"__name__": "__main__"}
    exec(compile(source, "<decompiled-main>", "exec"), namespace)  # noqa: S102
    printed = capsys.readouterr().out
    rebuilt = lower_source(printed)
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


# =============================================================================
# DL-38 fold registry: closed set T-001..T-007 (dsl.FOLDS), the builder
# surface it rests on (sequence(link=), parallel(on=, then_any=), mutex(),
# contend()), the --no-fold opt-out, the fold report, and the CLI surface.
# =============================================================================


# ----------------------------------------------------------- builder: sequence(link=)


def test_sequence_link_accepts_f_d_t_and_wires_the_named_letter() -> None:
    for link in ("f", "d", "t"):
        c = CatalogBuilder()
        c.job("head", command="h", machine="m1")
        c.job("tail", command="t", machine="m1")
        c.sequence("head", "tail", link=link)
        catalog = c.build()
        assert (
            cond_to_source(catalog.jobs["tail"].sem.condition)  # type: ignore[arg-type]
            == f"{link}(head)"
        )


def test_sequence_refuses_an_unknown_link_letter() -> None:
    c = CatalogBuilder()
    c.job("head", command="h", machine="m1")
    c.job("tail", command="t", machine="m1")
    with pytest.raises(DslError, match="not one of s/f/d/t"):
        c.sequence("head", "tail", link="n")


# ------------------------------------------------- builder: parallel(on=, then_any=)


def test_parallel_on_accepts_f_d_t_and_wires_the_named_letter() -> None:
    for on in ("f", "d", "t"):
        c = CatalogBuilder()
        c.job("seed", command="s", machine="m1")
        c.job("m1j", command="a", machine="m1")
        c.job("m2j", command="b", machine="m1")
        c.parallel(["m1j", "m2j"], after="seed", on=on)
        catalog = c.build()
        assert (
            cond_to_source(catalog.jobs["m1j"].sem.condition)  # type: ignore[arg-type]
            == f"{on}(seed)"
        )


def test_parallel_refuses_an_unknown_on_letter() -> None:
    c = CatalogBuilder()
    c.job("seed", command="s", machine="m1")
    c.job("m1j", command="a", machine="m1")
    c.job("m2j", command="b", machine="m1")
    with pytest.raises(DslError, match="not one of s/f/d/t"):
        c.parallel(["m1j", "m2j"], after="seed", on="n")


def test_parallel_then_any_fans_in_with_or() -> None:
    c = CatalogBuilder()
    c.job("m1j", command="a", machine="m1", condition="s(other1)")
    c.job("m2j", command="b", machine="m1", condition="s(other2)")
    c.job("join", command="j", machine="m1")
    c.parallel(["m1j", "m2j"], then_any="join")
    catalog = c.build()
    assert (
        cond_to_source(catalog.jobs["join"].sem.condition)  # type: ignore[arg-type]
        == "s(m1j) | s(m2j)"
    )


def test_parallel_then_any_refuses_a_join_that_already_has_a_condition() -> None:
    c = CatalogBuilder()
    c.job("m1j", command="a", machine="m1")
    c.job("m2j", command="b", machine="m1")
    c.job("join", command="j", machine="m1", condition="s(other)")
    with pytest.raises(DslError, match="already has a condition"):
        c.parallel(["m1j", "m2j"], then_any="join")


# ------------------------------------------------------------------ builder: mutex()


def test_mutex_pairwise_conjoins_bare_n_onto_each_job() -> None:
    c = CatalogBuilder()
    c.job("a", command="a", machine="m1")
    c.job("b", command="b", machine="m1")
    c.mutex("a", "b")
    catalog = c.build()
    assert cond_to_source(catalog.jobs["a"].sem.condition) == "n(b)"  # type: ignore[arg-type]
    assert cond_to_source(catalog.jobs["b"].sem.condition) == "n(a)"  # type: ignore[arg-type]


def test_mutex_composes_with_an_existing_condition_and_canonicalizes() -> None:
    """DL-38: mutex() conjoins onto whatever the job already carries --
    build() must succeed, and the lowered condition canonicalizes equal to
    the hand-parsed 'n(other) & s(x)' regardless of conjoin order (canonical
    form sorts conjuncts, module docstring)."""
    c = CatalogBuilder()
    c.job("x", command="cx", machine="m1")
    c.job("a", command="ca", machine="m1", condition="s(x)")
    c.job("b", command="cb", machine="m1")
    c.mutex("a", "b")
    catalog = c.build()
    assert cond_to_source(catalog.jobs["a"].sem.condition) == "(s(x)) & n(b)"  # type: ignore[arg-type]
    expected = parse_condition("n(b) & s(x)")
    assert canonical_cond(catalog.jobs["a"].sem.condition) == canonical_cond(expected)  # type: ignore[arg-type]
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(catalog)


def test_mutex_of_three_conjoins_every_pair() -> None:
    c = CatalogBuilder()
    c.job("a", command="a", machine="m1")
    c.job("b", command="b", machine="m1")
    c.job("c", command="c", machine="m1")
    c.mutex("a", "b", "c")
    catalog = c.build()
    assert canonical_cond(catalog.jobs["a"].sem.condition) == canonical_cond(  # type: ignore[arg-type]
        parse_condition("n(b) & n(c)")
    )
    assert canonical_cond(catalog.jobs["b"].sem.condition) == canonical_cond(  # type: ignore[arg-type]
        parse_condition("n(a) & n(c)")
    )
    assert canonical_cond(catalog.jobs["c"].sem.condition) == canonical_cond(  # type: ignore[arg-type]
        parse_condition("n(a) & n(b)")
    )


def test_mutex_refuses_fewer_than_two_names() -> None:
    c = CatalogBuilder()
    c.job("a", command="a", machine="m1")
    with pytest.raises(DslError, match="at least two"):
        c.mutex("a")


def test_mutex_refuses_duplicate_names_in_one_call() -> None:
    c = CatalogBuilder()
    c.job("a", command="a", machine="m1")
    with pytest.raises(DslError, match="must be distinct"):
        c.mutex("a", "a")


def test_mutex_refuses_an_undeclared_job() -> None:
    c = CatalogBuilder()
    c.job("a", command="a", machine="m1")
    with pytest.raises(DslError, match="undeclared"):
        c.mutex("a", "ghost")


def test_mutex_then_sequence_refuses_the_conditioned_follower() -> None:
    """mutex() marks its jobs conditioned -- chain builders must refuse them
    afterward (DL-38: wire chains BEFORE mutex())."""
    c = CatalogBuilder()
    c.job("a", command="a", machine="m1")
    c.job("b", command="b", machine="m1")
    c.mutex("a", "b")
    with pytest.raises(DslError, match="already has a condition"):
        c.sequence("a", "b")


def test_mutex_then_parallel_refuses_the_conditioned_member() -> None:
    c = CatalogBuilder()
    c.job("seed", command="s", machine="m1")
    c.job("a", command="a", machine="m1")
    c.job("b", command="b", machine="m1")
    c.mutex("a", "b")
    with pytest.raises(DslError, match="already has a condition"):
        c.parallel(["a", "b"], after="seed")


def test_decompile_never_folds_a_lookback_qualified_n_atom_into_mutex() -> None:
    """DL-38: T-005 detection is STRICTER than derive's M07 -- a
    lookback-qualified n() is a real edge (M03), never a mutex candidate
    (module docstring's standing exclusions). Otherwise-symmetric pair (a
    references other, other references a back) but a's side carries a
    lookback: mutex() can only emit a BARE n(), so folding this pair would
    silently drop the lookback -- it must stay fully explicit on both
    sides."""
    catalog = lower_source(
        "insert_job: a\njob_type: c\nmachine: m1\ncommand: ca\n"
        "condition: n(other, 01.00)\n\n"
        "insert_job: other\njob_type: c\nmachine: m1\ncommand: co\n"
        "condition: n(a)\n"
    )
    source = decompile(catalog)
    assert "c.mutex(" not in source
    assert "condition='n(other, 01.00)'" in _job_line(source, "a")
    assert "condition='n(a)'" in _job_line(source, "other")
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(catalog)


# ---------------------------------------------------------------- builder: contend()


def test_contend_declares_one_shared_resource_group_across_jobs() -> None:
    c = CatalogBuilder()
    c.job("a", command="a", machine="m1")
    c.job("b", command="b", machine="m1")
    c.contend(["a", "b"], resource="LOCK1", quantity=2, free="A")
    assert "resources: (LOCK1, QUANTITY=2, FREE=A)" in c.to_jil()
    catalog = c.build()
    for name in ("a", "b"):
        refs = catalog.jobs[name].resources
        assert [(r.name, r.quantity, r.free) for r in refs] == [("LOCK1", 2, "A")]
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(catalog)


def test_contend_free_is_optional() -> None:
    c = CatalogBuilder()
    c.job("a", command="a", machine="m1")
    c.job("b", command="b", machine="m1")
    c.contend(["a", "b"], resource="LOCK1")
    assert "resources: (LOCK1, QUANTITY=1)" in c.to_jil()


def test_contend_refuses_fewer_than_two_jobs() -> None:
    c = CatalogBuilder()
    c.job("a", command="a", machine="m1")
    with pytest.raises(DslError, match="at least two"):
        c.contend(["a"], resource="LOCK1")


def test_contend_refuses_an_undeclared_job() -> None:
    c = CatalogBuilder()
    c.job("a", command="a", machine="m1")
    with pytest.raises(DslError, match="undeclared"):
        c.contend(["a", "ghost"], resource="LOCK1")


def test_contend_refuses_a_job_that_already_carries_resources() -> None:
    c = CatalogBuilder()
    c.job("a", command="a", machine="m1", resources="(OTHER, QUANTITY=1)")
    c.job("b", command="b", machine="m1")
    with pytest.raises(DslError, match="already carries resources"):
        c.contend(["a", "b"], resource="LOCK1")


def test_contend_refuses_quantity_below_one() -> None:
    c = CatalogBuilder()
    c.job("a", command="a", machine="m1")
    c.job("b", command="b", machine="m1")
    with pytest.raises(DslError, match="quantity must be >= 1"):
        c.contend(["a", "b"], resource="LOCK1", quantity=0)


def test_contend_refuses_an_invalid_free_value() -> None:
    c = CatalogBuilder()
    c.job("a", command="a", machine="m1")
    c.job("b", command="b", machine="m1")
    with pytest.raises(DslError, match="not one of Y/N/A"):
        c.contend(["a", "b"], resource="LOCK1", free="Q")


def test_contend_refuses_a_resource_name_with_a_space() -> None:
    """A space is caught by the shared _check_name helper first (DL-17's
    generic name-shape guard), before contend()'s own paren/comma check."""
    c = CatalogBuilder()
    c.job("a", command="a", machine="m1")
    c.job("b", command="b", machine="m1")
    with pytest.raises(DslError, match="not JIL-name-shaped"):
        c.contend(["a", "b"], resource="bad name")


@pytest.mark.parametrize("resource", ["bad(name)", "bad,name"], ids=["paren", "comma"])
def test_contend_refuses_a_resource_name_with_parens_or_commas(resource: str) -> None:
    c = CatalogBuilder()
    c.job("a", command="a", machine="m1")
    c.job("b", command="b", machine="m1")
    with pytest.raises(DslError, match="not resource-name-shaped"):
        c.contend(["a", "b"], resource=resource)


# ------------------------------------------------------------- decompiler: T-002


def _t002_chain_catalog() -> CatalogIR:
    """h1(scheduled) -> h2 s(h1) -> h3 e(h2)=0 [disqualified: exit-code atom]
    -> h4 s(h3): one derive chain, split by the middle link into two runs."""
    c = CatalogBuilder()
    c.job(
        "h1",
        command="x",
        machine="m1",
        date_conditions=True,
        days_of_week=["all"],
        start_times='"06:00"',
    )
    c.job("h2", command="x", machine="m1", condition="s(h1)")
    c.job("h3", command="x", machine="m1", condition="e(h2) = 0")
    c.job("h4", command="x", machine="m1", condition="s(h3)")
    return c.build()


def test_decompile_splits_a_chain_with_a_disqualified_middle_link_into_two_runs() -> None:
    catalog = _t002_chain_catalog()
    report: list[str] = []
    source = decompile(catalog, report=report)
    assert "c.sequence('h1', 'h2')" in source
    assert "c.sequence('h3', 'h4')" in source
    assert "condition='e(h2) = 0'" in _job_line(source, "h3")
    assert any("T-002: 1 chain(s) folded as sub-runs" == line for line in report)
    assert any("exit-code atom" in line for line in report)
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(catalog)


def test_decompile_leaves_the_disqualified_chain_whole_when_t002_disabled() -> None:
    catalog = _t002_chain_catalog()
    report: list[str] = []
    source = decompile(catalog, disable=("T-002",), report=report)
    assert "c.sequence(" not in source
    for name, cond in (("h2", "s(h1)"), ("h3", "e(h2) = 0"), ("h4", "s(h3)")):
        assert f"condition={cond!r}" in _job_line(source, name)
    assert any("left whole (T-002 disabled)" in line for line in report)
    assert catalog_hash(roundtrip(catalog, disable=("T-002",))) == catalog_hash(catalog)


# ------------------------------------------------------------- decompiler: T-003


def _t003_or_join_catalog() -> CatalogIR:
    c = CatalogBuilder()
    c.job("seed", command="s", machine="m1")
    c.job("a", command="a", machine="m1", condition="s(seed)")
    c.job("b", command="b", machine="m1", condition="s(seed)")
    c.job("join", command="j", machine="m1", condition="s(a) | s(b)")
    return c.build()


def test_decompile_emits_parallel_then_any_for_a_unique_or_join() -> None:
    catalog = _t003_or_join_catalog()
    source = decompile(catalog)
    assert "c.parallel(['a', 'b'], after='seed', then_any='join')" in source
    for member in ("a", "b", "join"):
        assert "condition=" not in _job_line(source, member)
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(catalog)


def test_decompile_keeps_the_or_join_explicit_when_t003_disabled() -> None:
    catalog = _t003_or_join_catalog()
    source = decompile(catalog, disable=("T-003",))
    assert "c.parallel(['a', 'b'], after='seed')" in source
    assert "then_any=" not in source
    assert "condition='s(a) | s(b)'" in _job_line(source, "join")
    assert catalog_hash(roundtrip(catalog, disable=("T-003",))) == catalog_hash(catalog)


def test_decompile_two_or_joins_over_the_same_members_is_ambiguous_and_stays_explicit() -> None:
    c = CatalogBuilder()
    c.job("seed", command="s", machine="m1")
    c.job("a", command="a", machine="m1", condition="s(seed)")
    c.job("b", command="b", machine="m1", condition="s(seed)")
    c.job("joina", command="ja", machine="m1", condition="s(a) | s(b)")
    c.job("joinb", command="jb", machine="m1", condition="s(b) | s(a)")
    catalog = c.build()
    source = decompile(catalog)
    assert "c.parallel(['a', 'b'], after='seed')" in source
    assert "then_any=" not in source
    assert "condition='s(a) | s(b)'" in _job_line(source, "joina")
    assert "condition='s(b) | s(a)'" in _job_line(source, "joinb")
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(catalog)


# ------------------------------------------------------------- decompiler: T-004


def test_decompile_emits_sequence_link_for_a_uniform_f_chain() -> None:
    c = CatalogBuilder()
    c.job("f1", command="a", machine="m1")
    c.job("f2", command="b", machine="m1", condition="f(f1)")
    c.job("f3", command="c", machine="m1", condition="f(f2)")
    catalog = c.build()
    source = decompile(catalog)
    assert "c.sequence('f1', 'f2', 'f3', link='f')" in source
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(catalog)


def test_decompile_keeps_the_f_chain_explicit_when_t004_disabled() -> None:
    c = CatalogBuilder()
    c.job("f1", command="a", machine="m1")
    c.job("f2", command="b", machine="m1", condition="f(f1)")
    c.job("f3", command="c", machine="m1", condition="f(f2)")
    catalog = c.build()
    source = decompile(catalog, disable=("T-004",))
    assert "c.sequence(" not in source
    assert "condition='f(f1)'" in _job_line(source, "f2")
    assert "condition='f(f2)'" in _job_line(source, "f3")
    assert catalog_hash(roundtrip(catalog, disable=("T-004",))) == catalog_hash(catalog)


def test_decompile_emits_parallel_on_for_a_uniform_d_fanout() -> None:
    c = CatalogBuilder()
    c.job("seed", command="s", machine="m1")
    c.job("a", command="a", machine="m1", condition="d(seed)")
    c.job("b", command="b", machine="m1", condition="d(seed)")
    catalog = c.build()
    source = decompile(catalog)
    assert "c.parallel(['a', 'b'], after='seed', on='d')" in source
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(catalog)


def test_decompile_keeps_the_d_fanout_explicit_when_t004_disabled() -> None:
    c = CatalogBuilder()
    c.job("seed", command="s", machine="m1")
    c.job("a", command="a", machine="m1", condition="d(seed)")
    c.job("b", command="b", machine="m1", condition="d(seed)")
    catalog = c.build()
    source = decompile(catalog, disable=("T-004",))
    assert "c.parallel(" not in source
    assert "condition='d(seed)'" in _job_line(source, "a")
    assert "condition='d(seed)'" in _job_line(source, "b")
    assert catalog_hash(roundtrip(catalog, disable=("T-004",))) == catalog_hash(catalog)


# ------------------------------------------------------------- decompiler: T-006


def _t006_resources_catalog() -> CatalogIR:
    c = CatalogBuilder()
    c.job("ra", command="a", machine="m1", resources="(LOCK1, QUANTITY=1, FREE=A)")
    c.job("rb", command="b", machine="m1", resources="(LOCK1, QUANTITY=1, FREE=A)")
    c.job(
        "multi",
        command="m",
        machine="m1",
        resources="(LOCK1, QUANTITY=1, FREE=A) and (POOL2, QUANTITY=2)",
    )
    return c.build()


def test_decompile_emits_contend_for_identical_single_group_resources() -> None:
    catalog = _t006_resources_catalog()
    source = decompile(catalog)
    assert "c.contend(['ra', 'rb'], resource='LOCK1', quantity=1, free='A')" in source
    assert "resources=" not in _job_line(source, "ra")
    assert "resources=" not in _job_line(source, "rb")
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(catalog)


def test_decompile_leaves_a_multi_group_resources_job_explicit() -> None:
    catalog = _t006_resources_catalog()
    source = decompile(catalog)
    assert "resources=" in _job_line(source, "multi")
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(catalog)


def test_decompile_keeps_resources_explicit_when_t006_disabled() -> None:
    catalog = _t006_resources_catalog()
    source = decompile(catalog, disable=("T-006",))
    assert "c.contend(" not in source
    assert "resources=" in _job_line(source, "ra")
    assert "resources=" in _job_line(source, "rb")
    assert catalog_hash(roundtrip(catalog, disable=("T-006",))) == catalog_hash(catalog)


def test_decompile_reports_a_non_name_shaped_resource_as_staying_explicit() -> None:
    """A resource NAME can carry a space at the IR level (the `resources:`
    grammar's group is `\\(([^()]*)\\)`, DL-21) even though contend() itself
    would refuse to build such a name -- T-006 must skip it with a report
    note rather than emit an uncallable contend()."""
    catalog = lower_source(
        "insert_job: j1\njob_type: c\nmachine: m1\ncommand: x\n"
        "resources: (BAD NAME, QUANTITY=1)\n\n"
        "insert_job: j2\njob_type: c\nmachine: m1\ncommand: y\n"
        "resources: (BAD NAME, QUANTITY=1)\n"
    )
    report: list[str] = []
    source = decompile(catalog, report=report)
    assert "c.contend(" not in source
    assert any(
        "not resource-name-shaped for contend()" in line and "'BAD NAME'" in line
        for line in report
    )
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(catalog)


# ------------------------------------------------------------- decompiler: T-007


def _t007_schedules_catalog() -> CatalogIR:
    c = CatalogBuilder()
    c.job(
        "sa",
        command="a",
        machine="m1",
        date_conditions=True,
        days_of_week=["mo", "tu"],
        start_times='"05:00"',
    )
    c.job(
        "sb",
        command="b",
        machine="m1",
        date_conditions=True,
        days_of_week=["mo", "tu"],
        start_times='"05:00"',
    )
    c.job(
        "solo",
        command="c",
        machine="m1",
        date_conditions=True,
        days_of_week=["we"],
        start_times='"09:00"',
    )
    return c.build()


def test_decompile_factors_identical_schedules_into_a_shared_dict() -> None:
    catalog = _t007_schedules_catalog()
    source = decompile(catalog)
    assert "SCHED_0500_MO_TU = dict(" in source
    assert "**SCHED_0500_MO_TU" in _job_line(source, "sa")
    assert "**SCHED_0500_MO_TU" in _job_line(source, "sb")
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(catalog)


def test_decompile_leaves_a_singleton_schedule_inline() -> None:
    catalog = _t007_schedules_catalog()
    source = decompile(catalog)
    assert "date_conditions=True" in _job_line(source, "solo")
    assert "days_of_week='we'" in _job_line(source, "solo")
    assert "**SCHED_" not in _job_line(source, "solo")
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(catalog)


def test_decompile_keeps_schedules_inline_when_t007_disabled() -> None:
    catalog = _t007_schedules_catalog()
    source = decompile(catalog, disable=("T-007",))
    assert "dict(" not in source
    assert "days_of_week='mo, tu'" in _job_line(source, "sa")
    assert "days_of_week='mo, tu'" in _job_line(source, "sb")
    assert catalog_hash(roundtrip(catalog, disable=("T-007",))) == catalog_hash(catalog)


def test_decompile_schedule_name_collision_gets_a_deterministic_suffix() -> None:
    """Two DIFFERENT groups (the second also sets timezone=, so its emitted
    kwargs differ) whose _schedule_var_name STEM is the same (stem is built
    from start_times/start_mins + run_calendar/days_of_week only) collide;
    the second gets a deterministic _2 suffix, first-seen order."""
    c = CatalogBuilder()
    c.job(
        "g1a", command="a", machine="m1", date_conditions=True, run_calendar="CAL",
        start_times='"05:00"',
    )
    c.job(
        "g1b", command="b", machine="m1", date_conditions=True, run_calendar="CAL",
        start_times='"05:00"',
    )
    c.job(
        "g2a", command="c", machine="m1", date_conditions=True, run_calendar="CAL",
        start_times='"05:00"', timezone="UTC",
    )
    c.job(
        "g2b", command="d", machine="m1", date_conditions=True, run_calendar="CAL",
        start_times='"05:00"', timezone="UTC",
    )
    catalog = c.build()
    source = decompile(catalog)
    assert "SCHED_0500_CAL = dict(" in source
    assert "SCHED_0500_CAL_2 = dict(" in source
    assert "**SCHED_0500_CAL\n" in source.replace(")", "\n")  # g1a/g1b splat the base name
    assert "**SCHED_0500_CAL_2" in _job_line(source, "g2a")
    assert "**SCHED_0500_CAL_2" in _job_line(source, "g2b")
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(catalog)


# ---------------------------------------------------- decompiler: corpus fixtures


def test_decompiler_folds_the_t003_corpus_or_join_and_leaves_the_ambiguous_pair_explicit() -> None:
    catalog = lower_catalog([parse_file(p) for p in LOWERABLE_CORPUS])
    source = decompile(catalog)
    assert (
        "c.parallel(['fold_or_m1', 'fold_or_m2'], after='fold_or_seed', then_any='fold_or_join')"
        in source
    )
    assert "c.parallel(['fold_or2_m1', 'fold_or2_m2'], after='fold_or2_seed')" in source
    assert "condition='s(fold_or2_m1) | s(fold_or2_m2)'" in _job_line(source, "fold_or2_joina")
    assert "condition='s(fold_or2_m2) | s(fold_or2_m1)'" in _job_line(source, "fold_or2_joinb")
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(catalog)


def test_decompiler_folds_the_t004_corpus_typed_links_and_splits_the_mixed_chain() -> None:
    catalog = lower_catalog([parse_file(p) for p in LOWERABLE_CORPUS])
    source = decompile(catalog)
    assert "c.sequence('fold_f_seed', 'fold_f_b', 'fold_f_c', link='f')" in source
    assert "c.parallel(['fold_d_m1', 'fold_d_m2'], after='fold_d_seed', on='d')" in source
    assert "c.sequence('fold_mixed_a', 'fold_mixed_b')" in source
    assert "c.sequence('fold_mixed_b', 'fold_mixed_c', 'fold_mixed_d', link='f')" in source
    assert "c.sequence('fold_mixed_d', 'fold_mixed_e', link='d')" in source
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(catalog)


def test_decompiler_folds_the_t006_corpus_resources_and_leaves_the_multi_group_job_explicit() -> (
    None
):
    catalog = lower_catalog([parse_file(p) for p in LOWERABLE_CORPUS])
    source = decompile(catalog)
    assert (
        "c.contend(['fold_res_a', 'fold_res_b'], resource='FOLD_LOCK', quantity=1, free='A')"
        in source
    )
    assert "resources=" in _job_line(source, "fold_res_multi")
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(catalog)


def test_decompiler_folds_the_t007_corpus_schedules_and_leaves_near_identical_ones_inline() -> (
    None
):
    catalog = lower_catalog([parse_file(p) for p in LOWERABLE_CORPUS])
    source = decompile(catalog)
    assert "**SCHED_0500_MO_TU_WE_TH_FR" in _job_line(source, "fold_sched_a")
    assert "**SCHED_0500_MO_TU_WE_TH_FR" in _job_line(source, "fold_sched_b")
    assert "start_times='\"05:01\"'" in _job_line(source, "fold_sched_c")
    assert "start_times='\"05:02\"'" in _job_line(source, "fold_sched_d")
    assert catalog_hash(roundtrip(catalog)) == catalog_hash(catalog)


# ------------------------------------------------------------------- fold report


def test_report_lists_every_fold_code_that_fired_on_the_whole_corpus() -> None:
    catalog = lower_catalog([parse_file(p) for p in LOWERABLE_CORPUS])
    report: list[str] = []
    decompile(catalog, report=report)
    for code in ("T-001", "T-002", "T-003", "T-004", "T-006", "T-007"):
        assert any(line.startswith(f"{code}:") for line in report), report
    # T-005 fires on the corpus's mutex chain (m07_mutex.jil, pre-existing).
    assert any(line.startswith("T-005:") for line in report), report


def test_report_omits_a_code_that_finds_nothing() -> None:
    """A catalog with no resources anywhere must not mention T-006 at all."""
    c = CatalogBuilder()
    c.job("a", command="a", machine="m1")
    catalog = c.build()
    report: list[str] = []
    decompile(catalog, report=report)
    assert not any(line.startswith("T-006:") for line in report)


def test_report_explains_why_a_lookback_qualified_link_stays_explicit() -> None:
    c = CatalogBuilder()
    c.job("p1", command="x", machine="m1")
    c.job("p2", command="x", machine="m1", condition="s(p1, 01.00)")
    catalog = c.build()
    report: list[str] = []
    decompile(catalog, report=report)
    assert any(
        "explicit: link 'p1'->'p2' -- lookback-qualified (Q2)" in line for line in report
    )


# --------------------------------------------------------------- CLI: folds/--no-fold


def test_cli_folds_lists_the_whole_registry() -> None:
    result = runner.invoke(app, ["folds"])
    assert result.exit_code == 0
    assert len(FOLDS) == 7
    for code, description in FOLDS.items():
        assert f"{code}  {description}" in result.stdout


def test_cli_decompile_no_fold_changes_the_emitted_source() -> None:
    target = str(CORPUS_DIR / "m07_mutex.jil")
    with_folds = runner.invoke(app, ["decompile", target])
    without = runner.invoke(app, ["decompile", "--no-fold", "T-005", target])
    assert with_folds.exit_code == 0
    assert without.exit_code == 0
    assert with_folds.stdout != without.stdout
    assert "c.mutex(" in with_folds.stdout
    assert "c.mutex(" not in without.stdout


def test_cli_decompile_no_fold_accepts_a_comma_list() -> None:
    target = str(CORPUS_DIR / "m07_mutex.jil")
    result = runner.invoke(app, ["decompile", "--no-fold", "T-001,T-005", target])
    assert result.exit_code == 0
    assert "c.sequence(" not in result.stdout
    assert "c.mutex(" not in result.stdout


def test_cli_decompile_no_fold_unknown_code_exits_2() -> None:
    result = runner.invoke(
        app, ["decompile", "--no-fold", "T-999", str(CORPUS_DIR / "sem10_box_basic.jil")]
    )
    assert result.exit_code == 2
    assert "unknown fold code" in result.stderr


def test_cli_decompile_fold_report_lines_land_on_stderr_prefixed_fold() -> None:
    result = runner.invoke(app, ["decompile", str(CORPUS_DIR / "m07_mutex.jil")])
    assert result.exit_code == 0
    fold_lines = [line for line in result.stderr.splitlines() if line.startswith("fold: ")]
    assert fold_lines
    assert any(line.startswith("fold: T-005:") for line in fold_lines)
    assert "fold:" not in result.stdout


# ------------------------------------------------- the fold-independence property


_ALL_FOLD_SUBSETS: list[tuple[str, ...]] = [(), *[(code,) for code in FOLDS], tuple(FOLDS)]


@pytest.mark.parametrize(
    "disable",
    _ALL_FOLD_SUBSETS,
    ids=["none", *FOLDS, "all"],
)
def test_whole_corpus_decompile_roundtrip_is_fold_independent(disable: tuple[str, ...]) -> None:
    """DL-38's central guarantee: every fold is hash-neutral by construction
    (canonical form sorts/dedups conjuncts, so e.g. mutex()'s conjoin order
    cannot matter) -- the whole-corpus round-trip holds whether individual
    folds are on, off, or the entire registry is disabled at once."""
    catalog = lower_catalog([parse_file(p) for p in LOWERABLE_CORPUS])
    rebuilt = roundtrip(catalog, disable=disable)
    assert catalog_hash(rebuilt) == catalog_hash(catalog)
