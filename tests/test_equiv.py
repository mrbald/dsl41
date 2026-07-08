"""Equivalence validator tests: canonical form, tiers a/b/c, L006/L007, CLI
(phase 8, docs/ir-design.md ss6 + ss8; docs/decision-log.md DL-14).

Normative spec: dsl41.equiv's own module docstring pins every phase-8
decision, each gets a test below -- tier (b) enumerates per-job STATE SPACE
(not independent atom booleans: atoms alone cannot see the s(x)&f(x)
contradiction L006 exists for); the state model DECOUPLES status from last
exit code (conservative: unreachable states can only cause false
INEQUIVALENCE or a missed warn, never a false equivalence claim); state
spaces past STATE_CEILING (2**18) report "too_large" (tier-c only, no BDD
fallback v1); canonicalization drops spans/Lookback.raw/Paren/annotations/
var_sites/meta and normalizes nested-same-op flattening + operand sort +
schedule sort/dedup + duplicate-operand dedup, but KEEPS passthrough
verbatim (the semantic firewall's cargo); rename maps cover job names/box
links/condition refs (globals and external instances stay identity v1);
case_fold collisions raise loudly; tier (c) compares (at, job, transition)
with `cause` excluded and the rename applied to catalog A's trace. Also
dsl41.lint's rule_l006/rule_l007 docstrings (the tier-b lint rules),
dsl41.cli's `equiv` command (exit contract, --tier/--rename/--case-fold/
--scripts, hash short-circuit), and docs/decision-log.md DL-14.

Corpus/lowering conventions mirror test_ir.py/test_lint.py/test_derive.py:
LOWERABLE_CORPUS excludes sem31_xor.jil (a deliberate SEM-31 lowering
failure). The whole-corpus per-code lint counts pinned in test_lint.py are
untouched here and MUST keep holding unmodified; section 5 verifies directly
(rather than assuming) that L006/L007 fire zero times on today's corpus.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from typer.testing import CliRunner

from dsl41.ast_jil import parse, parse_file, render_canonical
from dsl41.cli import app
from dsl41.conditions import And, Or, Precedence, StatusAtom, iter_atoms, parse_condition
from dsl41.equiv import (
    STATE_CEILING,
    RenameError,
    canonical_catalog,
    canonical_cond,
    catalog_hash,
    cond_truth_profile,
    conds_equivalent,
    equiv_scripts,
    equivalent_tier_a,
    equivalent_tier_b,
    equivalent_tier_c,
)
from dsl41.ir import CatalogIR, Time, lower_catalog, lower_source
from dsl41.lint import rule_l006, rule_l007
from dsl41.oracle import Event

CORPUS_DIR = Path(__file__).parent / "corpus"
CORPUS = sorted(CORPUS_DIR.glob("*.jil"))

#: sem31_xor.jil is a deliberate SEM-31 mutual-exclusivity violation; excluded
#: from every whole-corpus pass here, mirroring test_ir.py/test_lint.py/
#: test_derive.py exactly.
EXPECT_LOWER_ERROR = {"sem31_xor.jil"}
LOWERABLE_CORPUS = [p for p in CORPUS if p.name not in EXPECT_LOWER_ERROR]

SEM08_GLOBALS = CORPUS_DIR / "sem08_globals.jil"


def _corpus_catalog() -> CatalogIR:
    return lower_catalog([parse_file(p) for p in LOWERABLE_CORPUS])


# ------------------------------------------------------------------ 1. canonical_cond


def test_paren_erasure_at_the_top_level() -> None:
    assert canonical_cond(parse_condition("(s(a))")) == canonical_cond(parse_condition("s(a)"))


def test_nested_and_and_flattens_once_the_grouping_paren_is_erased() -> None:
    # the parser itself never nests same-op runs, but a parenthesized inner
    # group produces a genuine And(And(...)) once canonical_cond erases Paren
    canon = canonical_cond(parse_condition("(s(a) & s(b)) & s(c)"))
    assert isinstance(canon, And)
    assert len(canon.operands) == 3
    assert all(isinstance(op, StatusAtom) for op in canon.operands)


def test_nested_or_or_flattens_once_the_grouping_paren_is_erased() -> None:
    canon = canonical_cond(parse_condition("(s(a) | s(b)) | s(c)"))
    assert isinstance(canon, Or)
    assert len(canon.operands) == 3


def test_operand_sort_makes_a_reordered_and_chain_equal() -> None:
    assert canonical_cond(parse_condition("s(b)&s(a)")) == canonical_cond(
        parse_condition("s(a)&s(b)")
    )


def test_operand_sort_makes_a_reordered_or_chain_equal() -> None:
    assert canonical_cond(parse_condition("s(b)|s(a)")) == canonical_cond(
        parse_condition("s(a)|s(b)")
    )


def test_duplicate_operand_dedup_collapses_an_and_pair_to_the_bare_atom() -> None:
    deduped = canonical_cond(parse_condition("s(a)&s(a)"))
    assert deduped == canonical_cond(parse_condition("s(a)"))
    assert isinstance(deduped, StatusAtom)


def test_duplicate_operand_dedup_collapses_an_or_pair_to_the_bare_atom() -> None:
    deduped = canonical_cond(parse_condition("s(x)|s(x)"))
    assert isinstance(deduped, StatusAtom)
    assert deduped.job.name == "x"


def test_lookback_raw_dropped_two_spellings_of_the_same_60_minute_window_are_equal() -> None:
    """01\\:00 (escaped-colon form) and 1.00 (dotted form) both parse to a
    60-minute window with a different `raw` token; canonicalization drops
    raw (kind+minutes stay), so they compare equal only after canon."""
    dotted = parse_condition(r"s(x, 1.00)")
    colon = parse_condition(r"s(x, 01\:00)")
    assert isinstance(dotted, StatusAtom) and isinstance(colon, StatusAtom)
    assert dotted.lookback is not None and colon.lookback is not None
    assert dotted.lookback.minutes == colon.lookback.minutes == 60
    assert dotted.lookback != colon.lookback  # raw differs pre-canonicalization
    assert canonical_cond(dotted) == canonical_cond(colon)


def test_indefinite_9999_lookback_folds_to_no_qualifier_at_all() -> None:
    """Explicit s(x, 9999) (SEM-04 legacy indefinite) canonicalizes to the
    same form as a bare s(x): _canon_lookback maps kind=="indefinite" to
    None, same as an absent lookback token."""
    assert canonical_cond(parse_condition("s(x, 9999)")) == canonical_cond(parse_condition("s(x)"))


def test_zero_lookback_survives_canonicalization_as_a_distinct_kind() -> None:
    canon = canonical_cond(parse_condition("s(x, 0)"))
    assert isinstance(canon, StatusAtom)
    assert canon.lookback is not None
    assert canon.lookback.kind == "zero"
    assert canon.lookback.raw == ""  # raw dropped, kind/minutes stay
    no_qualifier = canonical_cond(parse_condition("s(x)"))
    assert isinstance(no_qualifier, StatusAtom)
    assert canon.lookback != no_qualifier.lookback  # zero != "no qualifier at all"


def test_spans_are_stripped_at_every_level_of_the_tree() -> None:
    canon = canonical_cond(parse_condition("(s(a) & f(b)) | e(c) > 1"))
    assert isinstance(canon, Or)
    assert canon.span is None
    for operand in canon.operands:
        assert operand.span is None
    for atom in iter_atoms(canon):
        assert atom.span is None


def test_mixed_and_or_is_not_flattened_across_the_operator_boundary() -> None:
    canon = canonical_cond(parse_condition("(s(a)&s(b))|s(c)"))
    assert isinstance(canon, Or)
    kinds = sorted(type(op).__name__ for op in canon.operands)
    assert kinds == ["And", "StatusAtom"]


def test_mixed_or_and_is_not_flattened_across_the_operator_boundary() -> None:
    canon = canonical_cond(parse_condition("(s(a)|s(b))&s(c)"))
    assert isinstance(canon, And)
    kinds = sorted(type(op).__name__ for op in canon.operands)
    assert kinds == ["Or", "StatusAtom"]


#: Small atom pool for the idempotence property below, in the style of
#: test_conditions.py's chain-flattening generator: fixed, grammar-valid atom
#: strings combined left-to-right with random &/| choices.
_ATOM_STRS = [
    "s(a)",
    "f(b)",
    "d(c)",
    "t(d)",
    "n(e)",
    "e(x) = 1",
    "e(x) > 2",
    "v(G) = 1",
    "s(h, 00.30)",
]


@given(
    parts=st.lists(
        st.tuples(st.sampled_from(_ATOM_STRS), st.sampled_from(["&", "|"])),
        min_size=0,
        max_size=6,
    ),
    last=st.sampled_from(_ATOM_STRS),
    mode=st.sampled_from(["flat", "prec"]),
)
def test_canonical_cond_is_idempotent_property(
    parts: list[tuple[str, str]], last: str, mode: Precedence
) -> None:
    expr = "".join(f"{atom} {op} " for atom, op in parts) + last
    cond = parse_condition(expr, mode)
    once = canonical_cond(cond)
    assert canonical_cond(once) == once


# ------------------------------------------------------- 2. canonical_catalog / catalog_hash


def test_hash_is_stable_across_identical_parses() -> None:
    text = "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"
    assert catalog_hash(lower_source(text)) == catalog_hash(lower_source(text))


def test_hash_ignores_an_annotation_difference() -> None:
    a = lower_source("insert_job: j\njob_type: c\ncommand: x\nmachine: m1\ndescription: foo\n")
    b = lower_source("insert_job: j\njob_type: c\ncommand: x\nmachine: m1\ndescription: bar\n")
    assert catalog_hash(a) == catalog_hash(b)


def test_hash_ignores_attribute_order_because_it_does_not_change_the_ir_at_all() -> None:
    a = lower_source("insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n")
    b = lower_source("insert_job: j\nmachine: m1\ncommand: x\njob_type: c\n")
    assert a.jobs == b.jobs  # attr order never reaches the IR, not just the hash
    assert catalog_hash(a) == catalog_hash(b)


def test_hash_ignores_a_render_canonical_reformat_of_a_corpus_file() -> None:
    """Reformats sem08_globals.jil through ast_jil's canonical renderer
    (stable attr order, normalized spacing, comment placement) and reparses
    it; the resulting catalog hashes the same as the original file."""
    original = parse_file(SEM08_GLOBALS)
    reformatted_text = render_canonical(original)
    assert reformatted_text != Path(SEM08_GLOBALS).read_text()  # genuinely reformatted
    reformatted = parse(reformatted_text, file=str(SEM08_GLOBALS))
    assert catalog_hash(lower_catalog([original])) == catalog_hash(lower_catalog([reformatted]))


def test_hash_changes_on_a_condition_edit() -> None:
    template = (
        "insert_job: p\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: j\njob_type: c\ncommand: y\nmachine: m1\ncondition: {}\n"
    )
    a = lower_source(template.format("s(p)"))
    b = lower_source(template.format("f(p)"))
    assert catalog_hash(a) != catalog_hash(b)


def test_hash_changes_on_a_max_exit_success_edit() -> None:
    template = "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\nmax_exit_success: {}\n"
    assert catalog_hash(lower_source(template.format(2))) != catalog_hash(
        lower_source(template.format(3))
    )


def test_hash_changes_on_a_schedule_edit() -> None:
    template = (
        "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "{}"\n'
    )
    assert catalog_hash(lower_source(template.format("10:00"))) != catalog_hash(
        lower_source(template.format("11:00"))
    )


def test_hash_changes_on_a_passthrough_edit_because_passthrough_is_kept_verbatim() -> None:
    template = "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\nauto_delete: {}\n"
    assert catalog_hash(lower_source(template.format(1))) != catalog_hash(
        lower_source(template.format(0))
    )


def test_schedule_list_normalization_sorts_and_dedups_start_times() -> None:
    reordered = (
        "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\ndate_conditions: 1\n"
        'days_of_week: all\nstart_times: "10:00, 09:00"\n'
    )
    sorted_ = (
        "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\ndate_conditions: 1\n"
        'days_of_week: all\nstart_times: "09:00, 10:00"\n'
    )
    with_dup = (
        "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\ndate_conditions: 1\n"
        'days_of_week: all\nstart_times: "10:00, 09:00, 09:00"\n'
    )
    reference_hash = catalog_hash(lower_source(sorted_))
    assert catalog_hash(lower_source(reordered)) == reference_hash
    assert catalog_hash(lower_source(with_dup)) == reference_hash
    # structural check, not just the hash: canonical_catalog really sorts+dedups
    canon = canonical_catalog(lower_source(with_dup))
    schedule = canon.jobs["j"].schedule
    assert schedule is not None
    assert schedule.start_times == [Time(hour=9, minute=0), Time(hour=10, minute=0)]


def test_rename_collision_raises_rename_error() -> None:
    catalog = lower_source(
        "insert_job: a\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: b\njob_type: c\ncommand: y\nmachine: m1\n"
    )
    with pytest.raises(RenameError, match="collision"):
        canonical_catalog(catalog, rename={"a": "b"})


def test_case_fold_collision_raises_rename_error() -> None:
    catalog = lower_source(
        "insert_job: JobA\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: JOBA\njob_type: c\ncommand: y\nmachine: m1\n"
    )
    with pytest.raises(RenameError, match="collision"):
        canonical_catalog(catalog, case_fold=True)


def test_hash_ignores_catalog_meta_source_files() -> None:
    catalog = lower_catalog([parse_file(SEM08_GLOBALS)])
    touched = catalog.model_copy(
        update={"meta": catalog.meta.model_copy(update={"source_files": ["elsewhere.jil"]})}
    )
    assert touched.meta.source_files != catalog.meta.source_files
    assert catalog_hash(catalog) == catalog_hash(touched)


def test_catalog_hash_is_layout_invariant_for_machine_statements() -> None:
    """Regression pin (was a strict xfail): canonical_catalog originally
    passed `machines` through with MachineIR.span intact, so moving an
    insert_machine statement (blank lines only) changed catalog_hash while
    equivalent_tier_a's machines_equal check (machine_type+attrs only) said
    equivalent -- the ss8 short-circuit and tier (a) disagreed on a pure
    layout diff. Machines now canonicalize span-free like jobs."""
    a = lower_source(
        "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n\ninsert_machine: m1\ntype: a\n"
    )
    b = lower_source(
        "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n\n\n\n\ninsert_machine: m1\ntype: a\n"
    )
    assert catalog_hash(a) == catalog_hash(b)


# ------------------------------------------------------------------------- 3. Tier a


def test_tier_a_self_equivalence_on_the_whole_lowerable_corpus() -> None:
    catalog = _corpus_catalog()
    result = equivalent_tier_a(catalog, catalog)
    assert result.equivalent
    assert result.left_only == result.right_only == result.differing == []


def test_tier_a_rename_map_propagates_into_box_name_links_and_box_success_refs() -> None:
    """Direct canonical_catalog inspection (not just the tier-a boolean): a
    rename of the producer AND of the box itself must update the box_name
    link on the member, the box_success ref, and the plain condition ref, in
    one pass."""
    text = (
        "insert_job: producer\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: consumer\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(producer)\n\n"
        "insert_job: mybox\njob_type: b\nbox_success: s(producer)\n\n"
        "insert_job: member\njob_type: c\ncommand: z\nmachine: m1\nbox_name: mybox\n"
    )
    catalog = lower_source(text)
    renamed = canonical_catalog(catalog, rename={"producer": "prod2", "mybox": "boxB"})
    assert set(renamed.jobs) == {"prod2", "consumer", "boxB", "member"}
    condition = renamed.jobs["consumer"].sem.condition
    assert isinstance(condition, StatusAtom) and condition.job.name == "prod2"
    box_success = renamed.jobs["boxB"].sem.box_success
    assert isinstance(box_success, StatusAtom) and box_success.job.name == "prod2"
    assert renamed.jobs["member"].box.box_name == "boxB"


def test_tier_a_rename_map_end_to_end_makes_a_renamed_catalog_equivalent() -> None:
    a_text = (
        "insert_job: producer\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: consumer\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(producer)\n\n"
        "insert_job: mybox\njob_type: b\nbox_success: s(producer)\n\n"
        "insert_job: member\njob_type: c\ncommand: z\nmachine: m1\nbox_name: mybox\n"
    )
    b_text = (
        "insert_job: prod2\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: consumer\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(prod2)\n\n"
        "insert_job: mybox\njob_type: b\nbox_success: s(prod2)\n\n"
        "insert_job: member\njob_type: c\ncommand: z\nmachine: m1\nbox_name: mybox\n"
    )
    cat_a, cat_b = lower_source(a_text), lower_source(b_text)
    renamed_result = equivalent_tier_a(cat_a, cat_b, rename={"producer": "prod2"})
    assert renamed_result.equivalent
    unrenamed_result = equivalent_tier_a(cat_a, cat_b)
    assert not unrenamed_result.equivalent  # the rename was load-bearing
    assert unrenamed_result.left_only == ["producer"]
    assert unrenamed_result.right_only == ["prod2"]


def test_tier_a_left_only_right_only_differing_and_detail_all_populate_together() -> None:
    a_text = (
        "insert_job: p\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: only_a\njob_type: c\ncommand: q\nmachine: m1\n\n"
        "insert_job: shared\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(p)\n"
    )
    b_text = (
        "insert_job: p\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: only_b\njob_type: c\ncommand: r\nmachine: m1\n\n"
        "insert_job: shared\njob_type: c\ncommand: y\nmachine: m1\ncondition: f(p)\n"
    )
    result = equivalent_tier_a(lower_source(a_text), lower_source(b_text))
    assert not result.equivalent
    assert result.left_only == ["only_a"]
    assert result.right_only == ["only_b"]
    assert result.differing == ["shared"]
    assert result.detail == {"shared": "differs in: sem"}


def test_tier_a_annotations_difference_is_still_equivalent() -> None:
    a = lower_source("insert_job: j\njob_type: c\ncommand: x\nmachine: m1\ndescription: foo\n")
    b = lower_source("insert_job: j\njob_type: c\ncommand: x\nmachine: m1\ndescription: bar\n")
    assert equivalent_tier_a(a, b).equivalent


def test_tier_a_condition_operand_reordering_is_equivalent() -> None:
    template = (
        "insert_job: p1\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: p2\njob_type: c\ncommand: y\nmachine: m1\n\n"
        "insert_job: j\njob_type: c\ncommand: z\nmachine: m1\ncondition: {}\n"
    )
    a = lower_source(template.format("s(p1) & s(p2)"))
    b = lower_source(template.format("s(p2) & s(p1)"))
    assert equivalent_tier_a(a, b).equivalent


def test_tier_a_genuinely_different_condition_reports_differing_with_sem_detail() -> None:
    """`condition` lives inside JobIR.sem, so the field-diff loop (which
    walks top-level JobIR fields) names the differing field "sem", not
    "condition"."""
    template = (
        "insert_job: p\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: j\njob_type: c\ncommand: y\nmachine: m1\ncondition: {}\n"
    )
    a = lower_source(template.format("s(p)"))
    b = lower_source(template.format("f(p)"))
    result = equivalent_tier_a(a, b)
    assert not result.equivalent
    assert result.differing == ["j"]
    assert result.detail == {"j": "differs in: sem"}


# -------------------------------------------------------- 4. Tier b: conds_equivalent


def test_done_is_equivalent_to_success_or_failure_or_terminated() -> None:
    result = conds_equivalent(parse_condition("d(x)"), parse_condition("s(x)|f(x)|t(x)"))
    assert result.verdict == "equivalent"
    assert result.state_count == 10  # one job: 5 statuses x iced flag (DL-14a)


def test_window_nesting_the_wider_window_absorbs_the_narrower_one_in_an_or() -> None:
    wide_or_narrow = parse_condition("s(x, 01.00)|s(x, 02.00)")
    wide_alone = parse_condition("s(x, 02.00)")
    assert conds_equivalent(wide_or_narrow, wide_alone).verdict == "equivalent"


def test_success_vs_failure_diverges_with_a_counterexample_naming_the_job() -> None:
    result = conds_equivalent(parse_condition("s(x)"), parse_condition("f(x)"))
    assert result.verdict == "divergent"
    assert result.counterexample == {"x": "SUCCESS"}


def test_distributivity_and_over_or() -> None:
    left = parse_condition("s(a)&(s(b)|s(c))")
    right = parse_condition("(s(a)&s(b))|(s(a)&s(c))")
    assert conds_equivalent(left, right).verdict == "equivalent"


def test_or_and_and_of_the_same_atoms_are_not_equivalent() -> None:
    """De-morgan-ish sanity check: with no negation node in this algebra,
    Or and And of the same two atoms must NOT collapse to the same truth
    function -- s(a)|s(b) is broader than s(a)&s(b)."""
    result = conds_equivalent(parse_condition("s(a)|s(b)"), parse_condition("s(a)&s(b)"))
    assert result.verdict == "divergent"
    assert result.counterexample is not None


def test_done_is_not_equivalent_to_success_or_failure_alone_missing_terminated() -> None:
    """Another de-morgan-ish non-equivalence: dropping one disjunct of the
    d(x) == s(x)|f(x)|t(x) identity breaks it, with TERMINATED as the
    witness."""
    result = conds_equivalent(parse_condition("d(x)"), parse_condition("s(x)|f(x)"))
    assert result.verdict == "divergent"
    assert result.counterexample == {"x": "TERMINATED"}


def test_none_is_equivalent_to_none() -> None:
    result = conds_equivalent(None, None)
    assert result.verdict == "equivalent"
    assert result.state_count == 1  # empty alphabet: exactly one (trivial) state


def test_none_vs_a_status_atom_diverges_where_the_atom_is_false() -> None:
    result = conds_equivalent(None, parse_condition("s(x)"))
    assert result.verdict == "divergent"
    assert result.counterexample == {"x": "NEVER_RAN"}


def test_exitcode_integer_cutpoints_ge_2_equals_gt_1() -> None:
    result = conds_equivalent(parse_condition("e(x) >= 2"), parse_condition("e(x) > 1"))
    assert result.verdict == "equivalent"


def test_unsatisfiable_exitcode_conjunction_diverges_from_the_solo_atom() -> None:
    unsat = parse_condition("e(x) = 1 & e(x) = 2")
    result = conds_equivalent(unsat, parse_condition("e(x) = 1"))
    assert result.verdict == "divergent"
    assert result.counterexample is not None
    assert "exit=1" in result.counterexample["x"]


def test_global_unset_state_makes_the_complementary_or_diverge_from_none() -> None:
    """v(G)=1 | v(G)!=1 looks tautological if you forget UNSET: both
    comparisons are false when the global was never SET_GLOBAL'd/declared.
    This pins that UNSET reading against the naive "always true" one."""
    result = conds_equivalent(parse_condition("v(G) = 1 | v(G) != 1"), None)
    assert result.verdict == "divergent"
    assert result.counterexample is not None
    assert "unset" in result.counterexample["$G"].lower()


def test_notrunning_or_success_is_not_tautologically_equivalent_to_none() -> None:
    """n(x) is true unless x is RUNNING; even OR'd with s(x) it is still
    falsified by the RUNNING state, so it must NOT collapse to the constant
    TRUE (None)."""
    result = conds_equivalent(parse_condition("n(x)|s(x)"), None)
    assert result.verdict == "divergent"
    assert result.counterexample == {"x": "RUNNING"}


def test_too_large_state_space_reports_too_large_and_exceeds_the_ceiling() -> None:
    """5 jobs x 3 distinct lookback windows each: per job, 5 statuses x 4 age
    buckets (3 windows -> 4 buckets) = 20; 20**5 = 3_200_000 > 2**18."""
    jobs = ["j1", "j2", "j3", "j4", "j5"]
    cond_str = " & ".join(f"s({job}, 01.00) & s({job}, 02.00) & s({job}, 03.00)" for job in jobs)
    big = parse_condition(cond_str)
    result = conds_equivalent(big, big)
    assert result.verdict == "too_large"
    assert result.state_count > STATE_CEILING


# --------------------------------------------------- 5. cond_truth_profile / L006 / L007


def test_truth_profile_s_and_f_is_a_contradiction() -> None:
    assert cond_truth_profile(parse_condition("s(x)&f(x)")) == (False, True)


def test_truth_profile_disjoint_exitcode_range_is_a_contradiction() -> None:
    assert cond_truth_profile(parse_condition("s(x)&e(x)>3&e(x)<2")) == (False, True)


def test_truth_profile_global_unset_or_is_satisfiable_and_falsifiable() -> None:
    assert cond_truth_profile(parse_condition("v(G) = 1 | v(G) != 1")) == (True, True)


def test_truth_profile_fixed_status_pins_notrunning_to_a_tautology() -> None:
    """L007's own model: pinning a sibling to NEVER_RAN (the "at box start"
    moment) makes n(sibling) true in every remaining state -- falsifiable
    goes False, unlike the free (unpinned) model where it is True."""
    unpinned = cond_truth_profile(parse_condition("n(sib)"))
    assert unpinned == (True, True)
    pinned = cond_truth_profile(parse_condition("n(sib)"), fixed_status={"sib": "NEVER_RAN"})
    assert pinned == (True, False)


def test_l006_fires_zero_times_on_the_whole_lowerable_corpus() -> None:
    """test_lint.py's whole-corpus per-code counts (untouched here) list no
    L006; this pins that emptiness directly against dsl41.lint.rule_l006."""
    assert rule_l006(_corpus_catalog()) == []


def test_l007_fires_zero_times_on_the_whole_lowerable_corpus() -> None:
    assert rule_l007(_corpus_catalog()) == []


def test_l006_fires_on_a_contradiction_condition_and_names_it() -> None:
    text = (
        "insert_job: p\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: j\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(p) & f(p)\n"
    )
    (violation,) = rule_l006(lower_source(text))
    assert violation.code == "L006"
    assert violation.severity == "warn"
    assert violation.jobs == ["j"]
    assert "contradiction" in violation.message


def test_l006_fires_on_a_box_success_contradiction_too() -> None:
    """rule_l006 walks all three condition-bearing attrs (module docstring's
    shared `_job_conditions` walker), not just `condition`."""
    text = (
        "insert_job: box1\njob_type: b\nbox_success: s(m) & f(m)\n\n"
        "insert_job: m\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box1\n"
    )
    (violation,) = rule_l006(lower_source(text))
    assert violation.jobs == ["box1"]
    assert violation.detail == "box_success"
    assert "contradiction" in violation.message


def test_l007_fires_when_the_notrunning_gate_names_a_later_sibling() -> None:
    """A LATER sibling is certainly NEVER_RAN when this member is first
    evaluated (oracle starts members in catalog order, DL-14a), so n(later)
    is always true at box start -- the gate does nothing."""
    text = (
        "insert_job: boxq\njob_type: b\n\n"
        "insert_job: mem\njob_type: c\ncommand: y\nmachine: m1\nbox_name: boxq\ncondition: n(sib)\n\n"
        "insert_job: sib\njob_type: c\ncommand: x\nmachine: m1\nbox_name: boxq\n"
    )
    (violation,) = rule_l007(lower_source(text))
    assert violation.code == "L007"
    assert violation.severity == "warn"
    assert violation.jobs == ["mem"]
    assert violation.detail == "boxq"


def test_l007_quiet_when_the_notrunning_gate_names_an_earlier_sibling() -> None:
    """Review fix (DL-14a): an EARLIER unconditioned sibling may already be
    RUNNING when this member is evaluated (catalog-order starts), so n(sib)
    genuinely gates -- the old all-NEVER_RAN pinning was a false positive
    here, contradicting the oracle's own box-start behavior."""
    text = (
        "insert_job: boxq\njob_type: b\n\n"
        "insert_job: sib\njob_type: c\ncommand: x\nmachine: m1\nbox_name: boxq\n\n"
        "insert_job: mem\njob_type: c\ncommand: y\nmachine: m1\nbox_name: boxq\ncondition: n(sib)\n"
    )
    assert rule_l007(lower_source(text)) == []


def test_l007_quiet_for_a_member_gated_by_success_of_a_sibling() -> None:
    text = (
        "insert_job: boxr\njob_type: b\n\n"
        "insert_job: sib2\njob_type: c\ncommand: x\nmachine: m1\nbox_name: boxr\n\n"
        "insert_job: mem2\njob_type: c\ncommand: y\nmachine: m1\nbox_name: boxr\ncondition: s(sib2)\n"
    )
    assert rule_l007(lower_source(text)) == []


def test_l007_quiet_for_a_job_that_is_not_a_box_member() -> None:
    text = (
        "insert_job: outsider_sib\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: standalone\njob_type: c\ncommand: y\nmachine: m1\ncondition: n(outsider_sib)\n"
    )
    assert rule_l007(lower_source(text)) == []


def test_l007_quiet_when_an_outside_job_can_still_falsify_the_condition() -> None:
    """n(sib3) & s(outsidejob) -- unlike a bare n(sib3), this also requires
    outsidejob's success; outsidejob's status is unpinned and can vary, so
    the condition IS falsifiable even with sib3 pinned NEVER_RAN."""
    text = (
        "insert_job: boxs\njob_type: b\n\n"
        "insert_job: sib3\njob_type: c\ncommand: x\nmachine: m1\nbox_name: boxs\n\n"
        "insert_job: outsidejob\njob_type: c\ncommand: z\nmachine: m1\n\n"
        "insert_job: mem3\njob_type: c\ncommand: y\nmachine: m1\nbox_name: boxs\n"
        "condition: n(sib3) & s(outsidejob)\n"
    )
    assert rule_l007(lower_source(text)) == []


# ------------------------------------------------------------------- 6. Tier b catalog


def test_tier_b_self_equivalence_on_the_whole_lowerable_corpus() -> None:
    catalog = _corpus_catalog()
    result = equivalent_tier_b(catalog, catalog)
    assert result.equivalent
    assert result.divergent_jobs == {}
    assert result.too_large_jobs == []
    assert result.graph_equal


def test_tier_b_divergent_condition_names_the_job_and_a_counterexample() -> None:
    template = (
        "insert_job: p\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: j\njob_type: c\ncommand: y\nmachine: m1\ncondition: {}\n"
    )
    a = lower_source(template.format("s(p)"))
    b = lower_source(template.format("f(p)"))
    result = equivalent_tier_b(a, b)
    assert not result.equivalent
    assert "j" in result.divergent_jobs
    assert "condition diverges" in result.divergent_jobs["j"]


def test_tier_b_graph_comparison_catches_a_box_membership_difference_conds_alone_miss() -> None:
    """Job m has no condition/box_success/box_failure in either catalog, so
    the per-job tier-b condition loop finds nothing to compare -- divergent_jobs
    stays empty -- yet A boxes m and B does not, so the derived box_tree
    differs and graph_equal must catch it."""
    a = lower_source(
        "insert_job: boxA\njob_type: b\n\ninsert_job: m\njob_type: c\ncommand: x\nmachine: m1\n"
        "box_name: boxA\n"
    )
    b = lower_source("insert_job: m\njob_type: c\ncommand: x\nmachine: m1\n")
    result = equivalent_tier_b(a, b)
    assert result.divergent_jobs == {}
    assert not result.graph_equal
    assert not result.equivalent
    assert result.graph_detail is not None and "box trees differ" in result.graph_detail


def test_tier_b_graph_detail_reports_a_mutex_group_difference() -> None:
    a = lower_source(
        "insert_job: m1j\njob_type: c\ncommand: x\nmachine: m1\ncondition: n(m2j)\n\n"
        "insert_job: m2j\njob_type: c\ncommand: y\nmachine: m1\n"
    )
    b = lower_source(
        "insert_job: m1j\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: m2j\njob_type: c\ncommand: y\nmachine: m1\n"
    )
    result = equivalent_tier_b(a, b)
    assert not result.graph_equal
    assert result.graph_detail is not None
    assert "mutex A=" in result.graph_detail
    assert "m1j" in result.graph_detail and "m2j" in result.graph_detail


# ---------------------------------------------------------------------- 7. Tier c


def test_tier_c_self_equivalence_on_the_corpus_catalog() -> None:
    catalog = _corpus_catalog()
    scripts = equiv_scripts(catalog, scripts=5)
    result = equivalent_tier_c(catalog, catalog, scripts)
    assert result.equivalent
    assert result.scripts_run == 5
    assert result.first_divergence is None


def test_tier_c_rename_equivalence() -> None:
    a_text = (
        "insert_job: producer\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: consumer\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(producer)\n"
    )
    b_text = (
        "insert_job: prod2\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: consumer\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(prod2)\n"
    )
    cat_a, cat_b = lower_source(a_text), lower_source(b_text)
    scripts = equiv_scripts(cat_a, scripts=8)
    assert equivalent_tier_c(cat_a, cat_b, scripts, rename={"producer": "prod2"}).equivalent
    assert not equivalent_tier_c(cat_a, cat_b, scripts).equivalent  # the rename was load-bearing


def test_tier_c_s_vs_f_divergence_is_caught_with_first_divergence_populated() -> None:
    a = lower_source(
        "insert_job: p\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: j\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(p)\n"
    )
    b = lower_source(
        "insert_job: p\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: j\njob_type: c\ncommand: y\nmachine: m1\ncondition: f(p)\n"
    )
    script = [
        Event(
            at=datetime(2026, 1, 1, 8, 0), kind="STATUS", payload={"job": "p", "status": "SUCCESS"}
        )
    ]
    result = equivalent_tier_c(a, b, [script])
    assert not result.equivalent
    assert result.scripts_run == 1
    assert result.first_divergence is not None
    assert "script 0" in result.first_divergence


def test_equiv_scripts_is_deterministic_for_the_same_seed() -> None:
    catalog = _corpus_catalog()
    first = equiv_scripts(catalog, scripts=3, seed=41)
    second = equiv_scripts(catalog, scripts=3, seed=41)
    assert first == second


def test_equiv_scripts_differs_for_a_different_seed() -> None:
    catalog = _corpus_catalog()
    default_seed = equiv_scripts(catalog, scripts=3, seed=41)
    other_seed = equiv_scripts(catalog, scripts=3, seed=99)
    assert default_seed != other_seed


def test_equiv_scripts_payload_job_names_are_drawn_from_catalog_a() -> None:
    text = (
        "insert_job: producer\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: consumer\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(producer)\n"
    )
    catalog = lower_source(text)
    scripts = equiv_scripts(catalog, scripts=10, events_per_script=6, seed=7)
    job_names_seen = {
        str(ev.payload["job"]) for script in scripts for ev in script if "job" in ev.payload
    }
    assert job_names_seen  # the scripts actually reference jobs
    assert job_names_seen <= set(catalog.jobs)


# ------------------------------------------------------------------------- 8. CLI

runner = CliRunner()


def test_cli_equiv_self_compare_short_circuits_on_a_hash_match(tmp_path: Path) -> None:
    f = tmp_path / "a.jil"
    f.write_text("insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n")
    result = runner.invoke(app, ["equiv", str(f), "--against", str(f)])
    assert result.exit_code == 0
    assert "hashes match" in result.stdout


def test_cli_equiv_annotation_only_difference_also_short_circuits(tmp_path: Path) -> None:
    """Annotations are dropped from canonical form, so their catalog_hash is
    equal too -- the short-circuit fires even though --tier a was asked
    for, and no per-tier output is printed."""
    a = tmp_path / "a.jil"
    a.write_text("insert_job: j\njob_type: c\ncommand: x\nmachine: m1\ndescription: hi\n")
    b = tmp_path / "b.jil"
    b.write_text("insert_job: j\njob_type: c\ncommand: x\nmachine: m1\ndescription: bye\n")
    result = runner.invoke(app, ["equiv", str(a), "--against", str(b), "--tier", "a"])
    assert result.exit_code == 0
    assert "hashes match" in result.stdout
    assert "tier a" not in result.stdout


def test_cli_equiv_rename_bypasses_the_short_circuit_and_reports_tier_a(tmp_path: Path) -> None:
    a = tmp_path / "rename_a.jil"
    a.write_text(
        "insert_job: producer\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: consumer\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(producer)\n"
    )
    b = tmp_path / "rename_b.jil"
    b.write_text(
        "insert_job: prod2\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: consumer\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(prod2)\n"
    )
    result = runner.invoke(
        app,
        ["equiv", str(a), "--against", str(b), "--rename", "producer=prod2", "--tier", "a"],
    )
    assert result.exit_code == 0
    assert "tier a: equivalent" in result.stdout
    assert "hashes match" not in result.stdout


def test_cli_equiv_divergent_pair_exits_1_with_divergent_lines(tmp_path: Path) -> None:
    a = tmp_path / "div_a.jil"
    a.write_text(
        "insert_job: p\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: j\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(p)\n"
    )
    b = tmp_path / "div_b.jil"
    b.write_text(
        "insert_job: p\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: j\njob_type: c\ncommand: y\nmachine: m1\ncondition: f(p)\n"
    )
    result = runner.invoke(app, ["equiv", str(a), "--against", str(b)])
    assert result.exit_code == 1
    assert "tier a: DIVERGENT" in result.stdout
    assert "tier b: DIVERGENT" in result.stdout
    assert "tier c: DIVERGENT" in result.stdout


def test_cli_equiv_bad_tier_exits_2(tmp_path: Path) -> None:
    f = tmp_path / "a.jil"
    f.write_text("insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n")
    result = runner.invoke(app, ["equiv", str(f), "--against", str(f), "--tier", "z"])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert "--tier must be a, b, c, or all" in result.stderr


def test_cli_equiv_bad_rename_format_exits_2(tmp_path: Path) -> None:
    f = tmp_path / "a.jil"
    f.write_text("insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n")
    result = runner.invoke(
        app, ["equiv", str(f), "--against", str(f), "--rename", "no_equals_sign"]
    )
    assert result.exit_code == 2
    assert "--rename expects OLD=NEW" in result.stderr


def test_cli_equiv_rename_collision_exits_2_via_rename_error(tmp_path: Path) -> None:
    f = tmp_path / "coll.jil"
    f.write_text(
        "insert_job: a\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: b\njob_type: c\ncommand: y\nmachine: m1\n"
    )
    result = runner.invoke(app, ["equiv", str(f), "--against", str(f), "--rename", "a=b"])
    assert result.exit_code == 2
    assert "collision" in result.stderr


def test_cli_equiv_lowering_refusal_in_against_exits_2(tmp_path: Path) -> None:
    clean = tmp_path / "clean.jil"
    clean.write_text("insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n")
    bad = tmp_path / "bad.jil"
    bad.write_text("insert_job: j\njob_type: c\nfrobnicate: 1\n")
    result = runner.invoke(app, ["equiv", str(clean), "--against", str(bad)])
    assert result.exit_code == 2
    assert "frobnicate" in result.stderr


# ---------------------------------------------- 9. review-driven regressions (DL-14a)

# Soundness fixes from the phase-8 adversarial review; each test pins the
# corrected behavior so it cannot regress silently.


def test_string_global_ordering_comparisons_are_sound() -> None:
    """Review BLOCKER: the old OTHER token made every ordered string
    comparison vacuously false -- v(G) < "m" and v(G) > "m" (opposites!)
    were declared equivalent. String cutpoints ("", lit+NUL) now represent
    the below/between/above regions."""
    less = parse_condition('v(G) < "m"')
    greater = parse_condition('v(G) > "m"')
    result = conds_equivalent(less, greater)
    assert result.verdict == "divergent"
    result_le_ge = conds_equivalent(parse_condition('v(G) <= "m"'), parse_condition('v(G) >= "m"'))
    assert result_le_ge.verdict == "divergent"


def test_string_global_ordering_tier_c_parity() -> None:
    """The same pair end-to-end: tier c with the widened script generator
    (off-literal SET_GLOBAL values on referenced-only globals) also
    distinguishes them -- the old generator never set G at all."""
    template = (
        "insert_job: p\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: j\njob_type: c\ncommand: y\nmachine: m1\ncondition: {}\n"
    )
    a = lower_source(template.format('v(G) < "m"'))
    b = lower_source(template.format('v(G) > "m"'))
    result = equivalent_tier_c(a, b, equiv_scripts(a, scripts=20))
    assert not result.equivalent


def test_iced_state_distinguishes_contradictions_on_different_jobs() -> None:
    """Review MAJOR: without the ice dimension, s(x)&f(x) and s(y)&f(y)
    were both 'unsatisfiable' hence equivalent -- but icing x (SEM-05 makes
    every atom true) starts one consumer and not the other."""
    result = conds_equivalent(parse_condition("s(x) & f(x)"), parse_condition("s(y) & f(y)"))
    assert result.verdict == "divergent"
    assert result.counterexample is not None
    assert any("ON_ICE" in v for v in result.counterexample.values())


def test_iced_contradiction_matches_oracle_end_to_end() -> None:
    template = (
        "insert_job: x\njob_type: c\ncommand: a\nmachine: m1\n\n"
        "insert_job: y\njob_type: c\ncommand: b\nmachine: m1\n\n"
        "insert_job: consumer\njob_type: c\ncommand: c\nmachine: m1\ncondition: {}\n"
    )
    a = lower_source(template.format("s(x) & f(x)"))
    b = lower_source(template.format("s(y) & f(y)"))
    at = datetime(2026, 1, 1, 8, 0)
    script = [
        Event(at=at, kind="ON_ICE", payload={"job": "x"}),
        Event(at=at, kind="STATUS", payload={"job": "y", "status": "SUCCESS"}),
    ]
    result = equivalent_tier_c(a, b, [script])
    assert not result.equivalent  # a's consumer starts (iced x), b's does not


def test_l006_still_fires_on_the_ice_free_contradiction() -> None:
    """L006 deliberately asks the ice-FREE question (DL-14a): the warn
    survives the soundness fix, and the message names the ice caveat."""
    catalog = lower_source(
        "insert_job: contra\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(up) & f(up)\n"
    )
    (violation,) = rule_l006(catalog)
    assert "short of ON_ICE" in violation.message


def test_conds_equivalent_still_equates_true_equivalences_with_ice() -> None:
    """Ice must not over-refuse: d(x) == s(x)|f(x)|t(x) holds in iced
    states too (both sides true)."""
    assert (
        conds_equivalent(parse_condition("d(x)"), parse_condition("s(x)|f(x)|t(x)")).verdict
        == "equivalent"
    )


def test_zero_lookback_shares_the_oracle_q2_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review NIT: tier b reads ORACLE_ZERO_LOOKBACK_ANCHOR so tier b and
    tier c never disagree on zero-lookback semantics."""
    import dsl41.oracle as oracle_module

    zero = parse_condition("s(x, 0)")
    plain = parse_condition("s(x)")
    assert conds_equivalent(zero, plain).verdict == "divergent"  # midnight default
    monkeypatch.setattr(oracle_module, "ORACLE_ZERO_LOOKBACK_ANCHOR", "last_change")
    assert conds_equivalent(zero, plain).verdict == "equivalent"  # latched reading


def test_too_large_is_inconclusive_not_divergent() -> None:
    """Review MINOR: ss6 says too-large defers to tier c; it must not fail
    tier b. Identical too-large conditions -> equivalent=True with the
    condition listed as deferred."""
    big = " & ".join(f"s(big{i}, 01.00) | s(big{i}, 02.00) | s(big{i}, 03.00)" for i in range(5))
    template = "insert_job: j\njob_type: c\ncommand: y\nmachine: m1\ncondition: {}\n"
    a = lower_source(template.format(big))
    b = lower_source(template.format(big))
    result = equivalent_tier_b(a, b)
    assert result.too_large_jobs == ["j.condition"]
    assert result.equivalent  # deferred, not divergent


def test_equiv_scripts_cover_oob_kinds_and_referenced_globals() -> None:
    """Review MINOR: the generator now emits out-of-band kinds and sets
    globals that are only referenced (never declared), with literal and
    off-literal values."""
    catalog = lower_source(
        "insert_job: j\njob_type: c\ncommand: y\nmachine: m1\ncondition: v(RUNTIME_G) = on\n"
    )
    scripts = equiv_scripts(catalog, scripts=30)
    kinds = {ev.kind for script in scripts for ev in script}
    assert {"ON_ICE", "ON_HOLD", "KILLJOB", "FORCE_STARTJOB"} <= kinds
    set_globals = [ev for script in scripts for ev in script if ev.kind == "SET_GLOBAL"]
    assert any(ev.payload["name"] == "RUNTIME_G" for ev in set_globals)
    values = {str(ev.payload["value"]) for ev in set_globals}
    assert "on" in values  # the referenced literal
    assert "onx" in values  # the off-literal probe


def test_tier_c_rename_collision_raises() -> None:
    template = (
        "insert_job: a1\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: a2\njob_type: c\ncommand: y\nmachine: m1\n"
    )
    catalog = lower_source(template)
    with pytest.raises(RenameError):
        equivalent_tier_c(catalog, catalog, [], rename={"a1": "a2"})
