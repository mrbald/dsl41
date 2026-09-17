"""`dsl41 minify`: the de-identifying estate minifier (dsl41.minify).

Normative spec: the module's own docstring -- the four-class table, the naming
scheme, the condition rewrite, the structural verify and the leak guard. The
CLI half checks the exit-code contract `cli.py` states (0 emitted, 2 the input
never reached the tool, 3 a minify refusal).

Fixtures are built from tests/corpus/ (synthetic, LICENSING.md item 2) or
written inline in the same synthetic style. Nothing here is estate-derived.

Every refusal gets a triggering AND a non-triggering case, per CLAUDE.md: a
guard nobody has watched stay quiet is a guard that may be firing on
everything.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dsl41.ast_jil import parse, parse_file
from dsl41.cli import app
from dsl41.ir import lower_source
from dsl41.minify import (
    MinifyRefusal,
    leak_findings,
    minify_files,
    verify_findings,
)
from dsl41.minify_rules import INERT_COMMAND, Klass, class_counts, classify

runner = CliRunner()

CORPUS = Path(__file__).parent / "corpus"

#: Corpus files minify emits with --verify on. The one it does not is
#: sem31_xor.jil, a deliberate SEM-31 lowering failure with its own test below.
#: Preserve-mode rendering keeps attribute order, so the DL-49 pool and the
#: SEM-39 cycle are in here rather than excluded.
EMITTABLE_CORPUS = sorted(p for p in CORPUS.glob("*.jil") if p.name != "sem31_xor.jil")

#: The corpus writes `timezone: Zurich`, a bare city. `_v_timezone` accepts only
#: an exact IANA key or a POSIX offset on a known abbreviation, so a whole-corpus
#: pass takes the owner's own remedy for a refused zone.
CORPUS_MINIFY = {"scrub_timezones": True}

BOXED = """\
insert_job: NIGHTLY_BOX
job_type: b
date_conditions: 1
days_of_week: mo,tu

insert_job: ACME_EXTRACT
job_type: c
box_name: NIGHTLY_BOX
machine: acmehost01
command: /opt/acme/extract.sh --client ACME
owner: acmebatch@acmehost01
description: "pull the ACME ledger"
avg_runtime: 420
max_run_alarm: 30
term_run_time: 90
n_retrys: 2
priority: 5
job_load: 10

insert_job: ACME_LOAD
job_type: c
box_name: NIGHTLY_BOX
machine: acmehost02
command: /opt/acme/load.sh
condition: s(ACME_EXTRACT, 2.30) & (d(ACME_EXTRACT) | e(ACME_EXTRACT) = 4)

insert_job: ACME_REPORT
job_type: c
machine: acmehost01
command: /opt/acme/report.sh
condition: s(NIGHTLY_BOX)
"""


def minify_text(text: str, *, verify: bool = True) -> str:
    """One synthetic estate in, the rendered minified body out."""
    return minify_files([parse(text, file="<fixture>")], verify=verify).bodies[0]


# ------------------------------------------------------------ the table


def test_every_corpus_attribute_is_classified() -> None:
    """The table covers the corpus: no fixture reaches the refusal branch."""
    unclassified = set()
    for path in sorted(CORPUS.glob("*.jil")):
        for stmt in parse_file(path).statements:
            for attr in stmt.attrs:
                if classify(stmt.subcommand, attr.key) is None:
                    unclassified.add(attr.key.lower())
    assert unclassified == set()


def test_classification_is_case_insensitive_and_context_aware() -> None:
    assert classify("insert_job", "JOB_TYPE") == (Klass.KEEP, "")
    # `condition:` is a job expression on a job and a SEM-36/37 date-condition
    # expression on a calendar.
    # `machine:` is one list-aware rule on both lanes: a job's placement and a
    # DL-49 pool member line are both comma lists that L017 splits the same way.
    assert classify("insert_job", "machine") == (Klass.RENAME, "machine")
    assert classify("insert_machine", "machine") == (Klass.RENAME, "machine")
    assert classify("insert_job", "condition") == (Klass.RENAME, "cond")
    assert classify("extended_calendar", "condition") == (Klass.KEEP, "")
    assert classify("insert_job", "command") == (Klass.REPLACE, INERT_COMMAND)
    assert classify("insert_job", "description") == (Klass.DROP, "")


def test_class_counts_are_disjoint_and_stated() -> None:
    counts = class_counts()
    assert counts["replace"] == 1  # `command` alone, per the module docstring
    assert sum(counts.values()) == len(set(_all_table_keys())), (
        "a key may not appear in two classes"
    )


def _all_table_keys() -> list[str]:
    from dsl41.minify_rules import _DROP, _KEEP, _RENAME

    return [*_KEEP, *_DROP, *_RENAME, "command"]


# ------------------------------------------------------------ naming


def test_box_members_take_the_box_member_form() -> None:
    out = minify_text(BOXED)
    assert "insert_job: b1\n" in out
    assert "insert_job: b1j1\n" in out
    assert "insert_job: b1j2\n" in out
    assert "box_name: b1\n" in out
    # the job outside the box gets the free-job series, not a member name
    assert "insert_job: j1\n" in out


def test_names_are_allocated_in_first_appearance_order() -> None:
    text = (
        "insert_job: ZULU\njob_type: c\nmachine: hostzulu\ncommand: /x\n\n"
        "insert_job: ALPHA\njob_type: c\nmachine: hostalpha\ncommand: /y\n"
    )
    names = minify_files([parse(text, file="<fixture>")]).names
    assert names.jobs == {"ZULU": "j1", "ALPHA": "j2"}
    assert names.machines == {"hostzulu": "m1", "hostalpha": "m2"}


def test_base36_series_rolls_past_nine() -> None:
    text = "".join(f"insert_job: J{n}\njob_type: c\nmachine: h\ncommand: /x\n\n" for n in range(12))
    names = minify_files([parse(text, file="<fixture>")]).names
    assert names.jobs["J9"] == "ja"  # 10 -> base36 'a'
    assert names.jobs["J11"] == "jc"


def test_a_job_that_moves_box_keeps_one_name() -> None:
    text = (
        "insert_job: BOXY\njob_type: b\n\n"
        "insert_job: OTHER\njob_type: b\n\n"
        "insert_job: MOVER\njob_type: c\nbox_name: BOXY\nmachine: h\ncommand: /x\n\n"
        "update_job: MOVER\nbox_name: OTHER\n"
    )
    names = minify_files([parse(text, file="<fixture>")], verify=False).names
    assert names.jobs["MOVER"] == "b1j1"  # one stable name across both statements


def test_determinism_is_byte_identical_across_runs() -> None:
    first = minify_files([parse_file(p) for p in EMITTABLE_CORPUS], **CORPUS_MINIFY)
    second = minify_files([parse_file(p) for p in EMITTABLE_CORPUS], **CORPUS_MINIFY)
    assert first.bodies == second.bodies
    assert first.names.to_json() == second.names.to_json()


# ------------------------------------------------------------ what survives


def test_conditions_keep_their_structure_and_lookbacks() -> None:
    out = minify_text(BOXED)
    assert "condition: s(b1j1, 2.30) & (d(b1j1) | e(b1j1) = 4)\n" in out
    assert "condition: s(b1)\n" in out


def test_a_zero_lookback_survives_byte_identical() -> None:
    # SEM-04: s(X, 0) is a zero lookback and is NOT the same token as an
    # absent one -- the rewrite must not normalize it away.
    text = "insert_job: A\njob_type: c\nmachine: h\ncommand: /x\ncondition: s(B, 0) | s(C)\n"
    assert "condition: s(j2, 0) | s(j3)\n" in minify_text(text, verify=False)


def test_cross_instance_references_rename_both_halves() -> None:
    text = (
        "insert_xinst: PRDBOX\nxtype: a\n\n"
        "insert_job: A\njob_type: c\nmachine: h\ncommand: /x\ncondition: s(REMOTEJOB^PRDBOX)\n"
    )
    assert "condition: s(j2^x1)\n" in minify_text(text, verify=False)


def test_numeric_timing_hints_are_kept_verbatim() -> None:
    out = minify_text(BOXED)
    for line in ("avg_runtime: 420", "max_run_alarm: 30", "term_run_time: 90"):
        assert f"{line}\n" in out


def test_dropped_attributes_do_not_reach_the_output() -> None:
    out = minify_text(BOXED)
    for key in ("owner:", "description:", "std_out_file:", "profile:"):
        assert key not in out
    assert "command: sleep 0\n" in out
    assert "/opt/acme" not in out


def test_comments_never_survive() -> None:
    text = "/* ACMECORP nightly */\ninsert_job: A\njob_type: c\nmachine: h\ncommand: /x\n"
    out = minify_text(text, verify=False)
    assert "/*" not in out
    assert "ACMECORP" not in out.upper()


def test_calendar_date_rows_survive_but_calendar_names_do_not() -> None:
    text = 'calendar: ACMEHOLS\ndescription: "acme holidays"\n01/01/2026 00:00\n'
    out = minify_text(text, verify=False)
    assert "01/01/2026 00:00" in out
    assert "ACMEHOLS" not in out
    assert out.startswith("calendar: c1\n")


def test_global_values_map_but_numbers_stay_numbers() -> None:
    # A numeric comparand must stay numeric: compare_value compares two
    # integers numerically, and a `v#` token would silently make `>` a string
    # comparison (dsl41.minify._Allocator.gvalue).
    text = (
        "insert_global: ACMEFLAG\nvalue: 0\n\n"
        "insert_global: ACMEMODE\nvalue: FULLRELOAD\n\n"
        "insert_job: A\njob_type: c\nmachine: h\ncommand: /x\n"
        'condition: v(ACMEFLAG) > 100 & v(ACMEMODE) = "FULLRELOAD"\n'
    )
    out = minify_text(text, verify=False)
    assert "value: 0\n" in out
    assert "value: v1\n" in out
    assert 'condition: v(g1) > 100 & v(g2) = "v1"\n' in out


def test_var_sites_rename_inside_a_kept_value() -> None:
    # SEM-08: `$$NAME` is a global reference wherever it appears. A KEEP value
    # that IS one substitution site cannot be shape-checked -- the scheduler
    # fills it in at run time -- so it renames instead of refusing.
    text = "insert_job: A\njob_type: c\nmachine: h\ncommand: /x\ntimezone: $$ACMEZONE\n"
    assert "timezone: $$g1\n" in minify_text(text, verify=False)


def test_a_rename_value_that_is_one_var_site_keeps_its_var_site() -> None:
    # Renaming `$$INBOX` to `/f/1` would destroy the var site the IR indexes.
    text = "insert_job: A\njob_type: f\nmachine: h\nwatch_file: $$ACMEINBOX\n"
    assert "watch_file: $$g1\n" in minify_text(text, verify=False)


def test_single_dollar_is_shell_and_stays_untouched() -> None:
    # SEM-08 draws the line at `$$`; `$HOME` is the shell's and must not rename.
    from dsl41.minify import _Allocator, rewrite_var_sites

    alloc = _Allocator(set(), {})
    assert rewrite_var_sites("$$ACMEZONE and $HOME", alloc) == "$$g1 and $HOME"


def test_a_keep_value_outside_its_closed_space_refuses() -> None:
    text = (
        "insert_job: A\njob_type: c\nmachine: h\ncommand: /x\n"
        "timezone: ACMEBANKLONDONDESK5\njob_load: ACMEDESK_UNITS\n"
    )
    with pytest.raises(MinifyRefusal) as exc:
        minify_text(text, verify=False)
    joined = " ".join(exc.value.messages)
    assert "timezone" in joined
    assert "job_load" in joined


def test_a_keep_value_inside_its_closed_space_does_not_refuse() -> None:
    text = (
        "insert_job: A\njob_type: c\nmachine: h\ncommand: /x\n"
        "timezone: Europe/London\njob_load: 10\n"
    )
    out = minify_text(text, verify=False)
    assert "timezone: Europe/London\n" in out
    assert "job_load: 10\n" in out


def test_timezones_are_kept_by_default_and_reported() -> None:
    text = (
        "insert_job: A\njob_type: c\nmachine: h\ncommand: /x\n"
        'date_conditions: 1\nstart_times: "05:00"\ntimezone: Europe/London\n'
    )
    result = minify_files([parse(text, file="<fixture>")])
    assert "timezone: Europe/London\n" in result.bodies[0]
    assert result.kept_timezones() == ["Europe/London"]


def test_scrub_timezones_maps_every_zone_to_utc() -> None:
    text = (
        "insert_job: A\njob_type: c\nmachine: h\ncommand: /x\n"
        'date_conditions: 1\nstart_times: "05:00"\ntimezone: Europe/London\n\n'
        "insert_job: B\njob_type: c\nmachine: h\ncommand: /y\n"
        'date_conditions: 1\nstart_times: "06:00"\ntimezone: Zurich\n'
    )
    result = minify_files([parse(text, file="<fixture>")], scrub_timezones=True)
    body = result.bodies[0]
    assert body.count("timezone: UTC") == 2
    assert "London" not in body and "Zurich" not in body
    assert result.kept_timezones() == ["UTC"]
    assert result.names.timezones == {"Europe/London": "UTC", "Zurich": "UTC"}


def test_every_keep_key_has_a_value_predicate() -> None:
    from dsl41.minify_rules import _KEEP, _KEEP_SHAPES

    assert _KEEP - set(_KEEP_SHAPES) == set()
    # `condition` is KEEP only inside a calendar statement, so it is not in _KEEP
    assert set(_KEEP_SHAPES) - _KEEP == {"condition"}


def test_a_calendar_date_row_that_is_not_a_date_refuses() -> None:
    text = "calendar: ACME_HOLS\n01/01/2026 00:00\nACMEBANK_CLOSURE_DAY\n"
    with pytest.raises(MinifyRefusal) as exc:
        minify_text(text, verify=False)
    assert "ACMEBANK_CLOSURE_DAY" in " ".join(exc.value.messages)


def test_a_calendar_condition_outside_the_sem37_inventory_refuses() -> None:
    bad = "extended_calendar: ACME_FISCAL\ncondition: 1st friday of ACMEBANK_MONTH\nadjust: 0\n"
    with pytest.raises(MinifyRefusal):
        minify_text(bad, verify=False)
    good = "extended_calendar: ACME_FISCAL\ncondition: EOMWORK\nadjust: 0\n"
    assert "condition: EOMWORK\n" in minify_text(good, verify=False)


def test_a_cycle_scoped_calendar_condition_is_accepted() -> None:
    # CWRK#L needs a cyccal to RESOLVE, but the expression itself is legal
    # SEM-37 vocabulary; the validator must not reject it for a missing cyccal.
    from dsl41.minify_rules import validate_keep

    assert validate_keep("condition", "CWRK#L")


def test_a_multi_machine_placement_keeps_every_member() -> None:
    text = (
        "insert_machine: acmehost01\ntype: a\n\n"
        "insert_machine: acmehost02\ntype: a\n\n"
        "insert_job: A\njob_type: c\ncommand: /x\nmachine: acmehost01, acmehost02\n"
    )
    assert "machine: m1, m2\n" in minify_text(text, verify=False)


def test_the_long_box_job_type_spelling_is_a_box() -> None:
    text = (
        "insert_job: NIGHTLY\njob_type: box\n\n"
        "insert_job: CHILD\njob_type: cmd\nbox_name: NIGHTLY\ncommand: /x\nmachine: h\n"
    )
    names = minify_files([parse(text, file="<fixture>")], verify=False).names
    assert names.jobs == {"NIGHTLY": "b1", "CHILD": "b1j1"}


def test_the_minified_corpus_still_lowers_and_keeps_its_shape() -> None:
    result = minify_files([parse_file(p) for p in EMITTABLE_CORPUS], **CORPUS_MINIFY)
    before = lower_source("\n".join(p.read_text(encoding="utf-8") for p in EMITTABLE_CORPUS))
    after = lower_source("\n".join(result.bodies))
    assert len(after.jobs) == len(before.jobs)
    assert verify_findings(before, after, result.names) == []


# ------------------------------------------------------------ refusals


def test_an_unclassified_key_refuses_and_names_every_one() -> None:
    text = (
        "insert_job: A\njob_type: c\nmachine: h\ncommand: /x\n"
        "acme_custom_tag: secret\nacme_other_tag: secret\n"
    )
    with pytest.raises(MinifyRefusal) as exc:
        minify_text(text)
    joined = " ".join(exc.value.messages)
    assert "acme_custom_tag" in joined
    assert "acme_other_tag" in joined


def test_a_classified_key_does_not_refuse() -> None:
    text = "insert_job: A\njob_type: c\nmachine: h\ncommand: /x\npriority: 5\n"
    assert "priority: 5\n" in minify_text(text, verify=False)


def test_an_unclassified_subcommand_refuses() -> None:
    text = "insert_monbro: ACMEMON\ndescription: x\n"
    with pytest.raises(MinifyRefusal) as exc:
        minify_text(text)
    assert "insert_monbro" in " ".join(exc.value.messages)


def test_a_condition_that_will_not_parse_refuses() -> None:
    text = "insert_job: A\njob_type: c\nmachine: h\ncommand: /x\ncondition: s(B) &&& junk(\n"
    with pytest.raises(MinifyRefusal) as exc:
        minify_text(text)
    assert "condition" in " ".join(exc.value.messages)


def test_a_condition_that_parses_does_not_refuse() -> None:
    text = "insert_job: A\njob_type: c\nmachine: h\ncommand: /x\ncondition: s(B) & f(C)\n"
    assert "condition: s(j2) & f(j3)\n" in minify_text(text, verify=False)


def test_a_corrupted_mapping_fails_the_structural_verify() -> None:
    """Deliberately break the map after the fact: verify must notice."""
    result = minify_files([parse(BOXED, file="<fixture>")])
    before = lower_source(BOXED)
    after = lower_source("\n".join(result.bodies))
    assert verify_findings(before, after, result.names) == []
    result.names.jobs["ACME_EXTRACT"] = "b1j2"  # now two jobs claim one name
    findings = verify_findings(before, after, result.names)
    assert findings
    assert any("b1j2" in message for message in findings)


def test_a_permuted_mapping_fails_the_structural_verify() -> None:
    """A bijection is not enough: the permuted names must carry the same props.

    The collapsing case above is caught by the job-set difference. This one
    keeps the set intact and swaps two structurally different jobs, so only the
    per-property comparison can see it.
    """
    result = minify_files([parse(BOXED, file="<fixture>")])
    before = lower_source(BOXED)
    after = lower_source("\n".join(result.bodies))
    names = result.names
    names.jobs["NIGHTLY_BOX"], names.jobs["ACME_REPORT"] = (
        names.jobs["ACME_REPORT"],
        names.jobs["NIGHTLY_BOX"],
    )
    findings = verify_findings(before, after, names)
    assert any("job_type differs" in message for message in findings), findings


def test_verify_sees_the_machine_and_watch_file_lanes() -> None:
    text = "insert_job: A\njob_type: f\nmachine: acmehost\nwatch_file: /acme/inbox/x.done\n"
    result = minify_files([parse(text, file="<fixture>")])
    before = lower_source(text)
    after = lower_source("\n".join(result.bodies))
    assert verify_findings(before, after, result.names) == []
    result.names.machines["acmehost"] = "m99"  # the output says m1
    assert any(
        "exec differs" in message for message in verify_findings(before, after, result.names)
    )


def test_verify_sees_the_global_value_lane() -> None:
    text = "insert_global: ACMEMODE\nvalue: FULLRELOAD\n"
    result = minify_files([parse(text, file="<fixture>")])
    before = lower_source(text)
    after = lower_source("\n".join(result.bodies))
    assert verify_findings(before, after, result.names) == []
    result.names.global_values["FULLRELOAD"] = "v99"
    findings = verify_findings(before, after, result.names)
    assert any("global values differ" in message for message in findings), findings


def test_the_emitted_bytes_must_re_scan_to_what_was_emitted() -> None:
    """Preserve mode reproduces an AST byte for byte, so F1 alone proves
    nothing: the check is that the BYTES parse back to the same statements and
    carry no comment."""
    from dsl41.minify import _check_roundtrip

    jf = parse(BOXED, file="<fixture>")
    emitted = minify_files([jf])
    scrubbed = parse(emitted.bodies[0], file="<fixture>")
    _check_roundtrip(emitted.bodies, [scrubbed])  # non-triggering: it round-trips
    # triggering: the bytes carry a comment the emitted AST does not
    with pytest.raises(MinifyRefusal) as exc:
        _check_roundtrip(["insert_job: j1\ncommand: sleep 0 /* c */\n"], [scrubbed])
    assert "re-scan" in " ".join(exc.value.messages)


def test_a_synthetic_name_series_never_aliases_another_shape() -> None:
    """base36 spells 1981 as `1j1`, so box `b1981` would be `b1j1` -- the same
    name as the first member of box `b1`. The shared pool skips it."""
    from dsl41.minify import _Series

    pool: set[str] = set()
    boxes = _Series("b#", pool)
    members = _Series("b1j#", pool)
    assert members.take() == "b1j1"
    minted = {boxes.take() for _ in range(2100)}
    assert "b1j1" not in minted
    assert len(minted) == 2100  # every name distinct


def test_the_leak_guard_fires_on_a_surviving_token() -> None:
    findings = leak_findings({"ACMECORP"}, set(), set(), "insert_job: acmecorp_1\n")
    assert findings and "acmecorp" in findings[0]


def test_the_leak_guard_stays_quiet_on_what_accounts_for_a_token() -> None:
    # `name` is a substring of the key `box_name` and `quantity` is a resource
    # group keyword: an estate whose comments quote its own JIL must not refuse.
    guard = leak_findings({"(name, QUANTITY=n)"}, set(), set(), "box_name: b1\nresources: (l1)\n")
    assert guard == []
    # a VALIDATED keep value accounts for its own tokens ...
    assert (
        leak_findings({"Europe/London"}, {"Europe/London"}, set(), "timezone: Europe/London\n")
        == []
    )
    # ... and a synthetic name this run minted accounts for itself
    assert leak_findings({"b1j1"}, set(), {"b1j1"}, "insert_job: b1j1\n") == []


def test_an_insert_machine_pool_keeps_its_attribute_order() -> None:
    """DL-49: factor/max_load bind to the `machine:` line ABOVE them, so the
    order of a pool statement is semantics, not layout. Preserve mode keeps it;
    canonical mode floated every `machine:` line to the front and broke it."""
    pool = parse_file(CORPUS / "machines_virtual_pool.jil")
    body = minify_files([pool]).bodies[0]
    again = parse(body, file="<fixture>")
    virtual = [s for s in again.statements if len(s.attrs) > 2][0]
    assert [a.key for a in virtual.attrs] == [
        "type",
        "machine",
        "factor",
        "max_load",
        "machine",
        "factor",
        "max_load",
    ]
    # and the members are two DISTINCT synthetic machines, not one collapsed name
    members = [a.raw_value for a in virtual.attrs if a.key == "machine"]
    assert len(set(members)) == 2


def test_a_cycle_keeps_its_start_end_date_pairing() -> None:
    """SEM-39: periods pair positionally, and canonical mode sorted `end_date`
    before `start_date` -- even one pair inverted."""
    cycle = parse_file(CORPUS / "calendars_autocal.jil")
    body = minify_files([cycle]).bodies[0]
    again = parse(body, file="<fixture>")
    periods = [s for s in again.statements if s.subcommand == "cycle"][0]
    assert [a.key for a in periods.attrs] == [
        "start_date",
        "end_date",
        "start_date",
        "end_date",
    ]
    assert lower_source(body).cycles["c2"].periods == [
        ("03/28/2026", "04/02/2026"),
        ("06/27/2026", "07/02/2026"),
    ]


def test_a_comment_tail_riding_the_subject_cannot_leak() -> None:
    """A `#` tail is VALUE text, not a comment (DL-31), so the scanner leaves
    it INSIDE `stmt.subject` -- no `Comment` object is created and preserve
    mode would emit it byte for byte. It survives only because every subject is
    replaced by a minted name."""
    text = "insert_machine: ACMEHOST01   # jsmith box\ntype: a\n"
    parsed = parse(text, file="<fixture>")
    assert "jsmith" in parsed.statements[0].subject  # it really does ride there
    assert parsed.statements[0].comments == []  # and the scanner sees no comment
    body = minify_files([parsed], verify=False).bodies[0]
    assert body == "insert_machine: m1\ntype: a\n"


def test_an_unminted_subject_is_a_refusal_not_a_guard_question() -> None:
    """The defence behind the test above. If a subject ever reached the
    renderer without being minted, its `#` tail would ship; that is a refusal,
    and the token guard is not trusted for it. Driven with an allocator whose
    mint is stubbed out, because nothing in the real path can produce it."""
    from dsl41.minify import _Allocator, _Transformed, _transform_statement

    stmt = parse("insert_machine: ACMEHOST # jsmith\ntype: a\n", file="<f>").statements[0]
    out = _Transformed(jil=parse("", file="<f>"), needles=set(), kept=set())

    honest = _Allocator(set(), {})
    refusals: list[str] = []
    emitted = _transform_statement(stmt, honest, out, refusals)
    assert refusals == [] and emitted is not None and emitted.subject == "m1"

    stubbed = _Allocator(set(), {})
    stubbed.simple = lambda namespace, name: name  # type: ignore[method-assign]
    refusals = []
    assert _transform_statement(stmt, stubbed, out, refusals) is None
    assert "did not rename" in " ".join(refusals)


def test_an_estate_that_does_not_lower_refuses_unless_verify_is_off() -> None:
    xor = parse_file(CORPUS / "sem31_xor.jil")
    with pytest.raises(MinifyRefusal) as exc:
        minify_files([xor])
    assert "--no-verify" in " ".join(exc.value.messages)
    assert minify_files([xor], verify=False).bodies[0]


# ------------------------------------------------------------ the CLI


def test_cli_writes_the_estate_to_stdout(tmp_path: Path) -> None:
    source = tmp_path / "estate.jil"
    source.write_text(BOXED, encoding="utf-8")
    result = runner.invoke(app, ["minify", str(source)])
    assert result.exit_code == 0
    assert "insert_job: b1j1" in result.stdout
    assert "ACME" not in result.stdout


def test_cli_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    source = tmp_path / "estate.jil"
    source.write_text(BOXED, encoding="utf-8")
    out_dir = tmp_path / "out"
    assert runner.invoke(app, ["minify", "--out", str(out_dir), str(source)]).exit_code == 0
    again = runner.invoke(app, ["minify", "--out", str(out_dir), str(source)])
    assert again.exit_code == 3
    forced = runner.invoke(app, ["minify", "--out", str(out_dir), "--force", str(source)])
    assert forced.exit_code == 0
    assert (out_dir / "estate.jil").read_text(encoding="utf-8").startswith("insert_job: b1")


def test_cli_mapping_is_opt_in_and_warns(tmp_path: Path) -> None:
    source = tmp_path / "estate.jil"
    source.write_text(BOXED, encoding="utf-8")
    plain = runner.invoke(app, ["minify", str(source)])
    assert "ACME_EXTRACT" not in plain.stdout  # never printed to stdout
    assert not list(tmp_path.glob("*.json"))  # never written unasked
    mapping = tmp_path / "map.json"
    asked = runner.invoke(app, ["minify", "--mapping", str(mapping), str(source)])
    assert asked.exit_code == 0
    assert "RE-IDENTIFIES" in asked.stderr
    assert "ACME_EXTRACT" in mapping.read_text(encoding="utf-8")
    assert "ACME_EXTRACT" not in asked.stdout


def test_cli_exit_two_when_the_input_never_reached_the_tool(tmp_path: Path) -> None:
    missing = runner.invoke(app, ["minify", str(tmp_path / "nope.jil")])
    assert missing.exit_code == 2
    bad = tmp_path / "bad.jil"
    bad.write_text("insert_job: A\ninsert_nothing: B\n", encoding="utf-8")
    assert runner.invoke(app, ["minify", str(bad)]).exit_code == 2


def test_cli_no_verify_still_emits_and_still_guards(tmp_path: Path) -> None:
    # sem31_xor.jil does not lower, so --verify refuses and --no-verify emits.
    source = CORPUS / "sem31_xor.jil"
    assert runner.invoke(app, ["minify", str(source)]).exit_code == 3
    relaxed = runner.invoke(app, ["minify", "--no-verify", str(source)])
    assert relaxed.exit_code == 0
    assert "insert_job: j1" in relaxed.stdout


def test_the_guard_catches_a_transform_that_forgot_to_rename(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard is a BACKSTOP: with the table closed and every RENAME lane
    rebuilt, nothing a well-formed estate contains can reach it -- its firing
    now means a bug in the transform, so that is what this drives. A RENAME
    rewrite stubbed to the identity is exactly that bug."""
    import dsl41.minify as minify_mod

    monkeypatch.setattr(minify_mod, "_rewrite_value", lambda rule, value, alloc: value.strip())
    text = "insert_job: A\njob_type: c\nmachine: ACMEBANKHOST\ncommand: /x\n"
    with pytest.raises(MinifyRefusal) as exc:
        minify_files([parse(text, file="<fixture>")], verify=False)
    joined = " ".join(exc.value.messages)
    assert "leak guard" in joined
    assert "acmebankhost" in joined


def test_cli_refusals_are_labelled_as_estate_derived(tmp_path: Path) -> None:
    """A refusal has to quote the value to be actionable, and that quote is
    estate text. The block says so before it says anything else."""
    source = tmp_path / "estate.jil"
    source.write_text(
        "insert_job: A\njob_type: c\nmachine: h\ncommand: /x\nresources: ACMEBANK_LICENSE\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["minify", "--no-verify", str(source)])
    assert result.exit_code == 3
    lines = [line for line in result.stderr.splitlines() if line.strip()]
    assert "QUOTE THE ESTATE" in lines[0]
    assert "ACMEBANK_LICENSE" in " ".join(lines[1:])  # still actionable


def test_help_does_not_describe_the_guard_as_total() -> None:
    text = runner.invoke(app, ["minify", "--help"]).stdout.lower()
    assert "backstop" in text or "not a total check" in text


def test_cli_refuses_an_out_that_is_not_a_directory(tmp_path: Path) -> None:
    source = tmp_path / "estate.jil"
    source.write_text(BOXED, encoding="utf-8")
    not_a_dir = tmp_path / "file.txt"
    not_a_dir.write_text("x", encoding="utf-8")
    result = runner.invoke(app, ["minify", "--out", str(not_a_dir), str(source)])
    assert result.exit_code == 3
    assert "--out" in result.stderr


def test_cli_refuses_to_overwrite_a_mapping_without_force(tmp_path: Path) -> None:
    source = tmp_path / "estate.jil"
    source.write_text(BOXED, encoding="utf-8")
    mapping = tmp_path / "map.json"
    assert runner.invoke(app, ["minify", "--mapping", str(mapping), str(source)]).exit_code == 0
    again = runner.invoke(app, ["minify", "--mapping", str(mapping), str(source)])
    assert again.exit_code == 3
    forced = runner.invoke(app, ["minify", "--mapping", str(mapping), "--force", str(source)])
    assert forced.exit_code == 0


def test_cli_exit_three_on_a_minify_refusal(tmp_path: Path) -> None:
    source = tmp_path / "estate.jil"
    source.write_text(
        "insert_job: A\njob_type: c\nmachine: h\ncommand: /x\nacme_tag: v\n", encoding="utf-8"
    )
    result = runner.invoke(app, ["minify", str(source)])
    assert result.exit_code == 3
    assert "acme_tag" in result.stderr


def test_cli_refuses_two_inputs_with_one_basename(tmp_path: Path) -> None:
    first = tmp_path / "a" / "estate.jil"
    second = tmp_path / "b" / "estate.jil"
    for path in (first, second):
        path.parent.mkdir(parents=True)
        path.write_text(BOXED if path is first else BOXED.replace("ACME", "BETA"), encoding="utf-8")
    result = runner.invoke(app, ["minify", "--out", str(tmp_path / "out"), str(first), str(second)])
    assert result.exit_code == 3
    assert "basename" in result.stderr


def test_cli_properties_resolve_placeholders_before_the_lanes(tmp_path: Path) -> None:
    source = tmp_path / "estate.jil"
    source.write_text(
        "insert_job: A\njob_type: c\nmachine: h\ncommand: /x\n"
        "max_run_alarm: ~{$ALARM}~\ntimezone: ~{$TZ}~\n",
        encoding="utf-8",
    )
    unresolved = runner.invoke(app, ["minify", str(source)])
    assert unresolved.exit_code == 3
    assert "outside the closed value space" in unresolved.stderr

    props = tmp_path / "estate.properties"
    props.write_text("ALARM=30\nTZ=Europe/London\n", encoding="utf-8")
    resolved = runner.invoke(app, ["minify", "-p", str(props), str(source)])
    assert resolved.exit_code == 0
    assert "max_run_alarm: 30" in resolved.stdout
    assert "~{" not in resolved.stdout


def test_cli_properties_a_bound_value_is_still_checked_against_its_space(tmp_path: Path) -> None:
    source = tmp_path / "estate.jil"
    source.write_text(
        "insert_job: A\njob_type: c\nmachine: h\ncommand: /x\n"
        "max_run_alarm: ~{$ALARM}~\ntimezone: ~{$TZ}~\n",
        encoding="utf-8",
    )
    props = tmp_path / "estate.properties"
    props.write_text("ALARM=thirty\nTZ=Europe/London\n", encoding="utf-8")
    result = runner.invoke(app, ["minify", "-p", str(props), str(source)])
    assert result.exit_code == 3
    assert "max_run_alarm" in result.stderr


def test_cli_properties_failure_is_exit_two_not_three(tmp_path: Path) -> None:
    source = tmp_path / "estate.jil"
    source.write_text(
        "insert_job: A\njob_type: c\nmachine: h\ncommand: /x\n"
        "max_run_alarm: ~{$ALARM}~\ntimezone: ~{$TZ}~\n",
        encoding="utf-8",
    )
    props = tmp_path / "estate.properties"
    props.write_text("ALARM=30\n", encoding="utf-8")
    incomplete = runner.invoke(app, ["minify", "-p", str(props), str(source)])
    assert incomplete.exit_code == 2
    assert "unresolved placeholder" in incomplete.stderr

    missing = runner.invoke(app, ["minify", "-p", str(tmp_path / "nope.properties"), str(source)])
    assert missing.exit_code == 2
    assert "nope.properties" in missing.stderr


def test_cli_help_lists_properties() -> None:
    result = runner.invoke(app, ["minify", "--help"])
    assert result.exit_code == 0
    assert "--properties" in result.stdout
    assert "-p" in result.stdout.replace("--properties", "")


# ------------------------------------------------- the KEEP closed-space audit

#: Values a site could choose that no KEEP predicate may accept. The POSIX
#: shapes are the ones that got through: `timezones._POSIX_FIXED` has an
#: UNBOUNDED letter run, so `ACMEBANKLONDONDESK5` resolved, passed as a KEEP
#: value, entered the guard's safe pool and shipped verbatim at exit 0.
CLIENT_LABELS = (
    "ACMEBANKSECRET",
    "ACMEBANKSECRET5",
    "ACMEBANKLONDONDESK5",
    "MYCO1",
    "JPMC7",
    "ACMEPROD12",
    "HSBCDESK9:30",
    "acmebanksecret",
    "ACMEBANKSECRET/DESK",
    "0ACMEBANKSECRET",
)


def test_no_keep_predicate_accepts_a_client_chosen_label() -> None:
    """The audit, executable. A KEEP value is exempt from the leak guard by
    design, so a predicate that accepts an arbitrary caller-chosen substring is
    a silent leak channel -- whatever else it also accepts."""
    from dsl41.minify_rules import _KEEP_SHAPES, validate_keep

    accepted = {
        (key, label) for key in _KEEP_SHAPES for label in CLIENT_LABELS if validate_keep(key, label)
    }
    assert accepted == set()


def test_the_timezone_acceptance_space_is_enumerable() -> None:
    """The audit's precise question for the one delegating predicate: every
    accepted value is either an exact IANA key or an allow-listed abbreviation
    plus an offset. Neither carries a caller-chosen substring."""
    import random
    import string

    from dsl41.minify_rules import TZ_ABBREVIATIONS, _iana_keys, validate_keep

    random.seed(3)
    alphabet = string.ascii_letters + string.digits + "+-:/_"
    for _ in range(20000):
        value = "".join(random.choice(alphabet) for _ in range(random.randint(2, 10)))
        if not validate_keep("timezone", value):
            continue
        if value in _iana_keys():
            continue
        runs = re.findall(r"[A-Za-z]+", value)
        assert len(runs) == 1 and runs[0].upper() in TZ_ABBREVIATIONS, value


def test_the_timezone_predicate_enumerates_instead_of_resolving() -> None:
    """`resolve_timezone` answers "can this be interpreted", which is not the
    same question as "is this closed vocabulary" -- the POSIX fallback resolves
    any 3+ letter run followed by digits."""
    from dsl41.timezones import resolve_timezone

    from dsl41.minify_rules import validate_keep

    assert resolve_timezone("ACMEBANKLONDONDESK5") is not None  # it DOES resolve
    assert not validate_keep("timezone", "ACMEBANKLONDONDESK5")  # and is refused
    for good in ("Europe/London", "UTC", "GMT+5", "IST-5:30", "America/New_York"):
        assert validate_keep("timezone", good), good
    for bad in ("Zurich", "europe/london", "ACME/Trading_Desk_7", "ACMEBANK"):
        assert not validate_keep("timezone", bad), bad


def test_a_posix_shaped_label_refuses_and_scrub_is_the_remedy() -> None:
    text = (
        "insert_job: EOD_LOAD\njob_type: c\nmachine: h\ncommand: /x\n"
        "timezone: ACMEBANKLONDONDESK5\n"
    )
    with pytest.raises(MinifyRefusal) as exc:
        minify_text(text, verify=False)
    assert "--scrub-timezones" in " ".join(exc.value.messages)
    # the remedy does not refuse, and the label reaches neither output nor map
    scrubbed = minify_files([parse(text, file="<fixture>")], verify=False, scrub_timezones=True)
    assert "timezone: UTC\n" in scrubbed.bodies[0]
    assert "ACMEBANK" not in scrubbed.bodies[0]
    assert scrubbed.names.timezones == {"ACMEBANKLONDONDESK5": "UTC"}


def test_the_mapping_carries_the_timezone_lane() -> None:
    """The warning promises the map re-identifies the estate; a scrubbed zone
    is only recoverable if the lane is in it."""
    import json

    text = (
        "insert_job: A\njob_type: c\nmachine: h\ncommand: /x\n"
        'date_conditions: 1\nstart_times: "05:00"\ntimezone: Europe/London\n'
    )
    result = minify_files([parse(text, file="<fixture>")], verify=False, scrub_timezones=True)
    written = json.loads(result.names.to_json())
    assert written["timezones"] == {"Europe/London": "UTC"}


def test_a_keep_value_cannot_hide_a_needle() -> None:
    """A KEEP value's tokens are subtracted from the guard's needles, so the
    only thing stopping a KEEP value being a hiding place is the predicate. Try
    to hide the same label in every KEEP key and in a comment at once."""
    for key in ("timezone", "job_load", "priority", "days_of_week", "status"):
        text = (
            "/* ACMEBANKSECRET is the desk */\n"
            "insert_job: A\njob_type: c\nmachine: h\ncommand: /x\n"
            f"{key}: ACMEBANKSECRET\n"
        )
        with pytest.raises(MinifyRefusal):
            minify_text(text, verify=False)


# ------------------------------------------------------ whole-value rewrites


def test_resources_is_rebuilt_not_patched() -> None:
    """Regex substitution left the text outside the parens, the tail after the
    first comma, and a paren-less value untouched."""
    good = (
        "insert_resource: ACMEDB\nres_type: R\namount: 4\n\n"
        "insert_job: A\njob_type: c\nmachine: h\ncommand: /x\n"
        "resources: (ACMEDB, QUANTITY=1, FREE=A) AND (ACMEMQ, QUANTITY=2)\n"
    )
    out = minify_text(good, verify=False)
    assert "resources: (l1, QUANTITY=1, FREE=A) AND (l2, QUANTITY=2)\n" in out
    for leaky in (
        "resources: (ACMEDB, QUANTITY=1, FREE=ACM) AND (R2, QUANTITY=2)\n",
        "resources: DB1\n",
        "resources: (ACMEDB, QUANTITY=1) ACMETRAILER (R2, QUANTITY=2)\n",
        "resources: (ACMEDB, ACMEKEY=1)\n",
    ):
        text = "insert_job: A\njob_type: c\nmachine: h\ncommand: /x\n" + leaky
        with pytest.raises(MinifyRefusal):
            minify_text(text, verify=False)


def test_a_multi_line_run_calendar_renames_every_member() -> None:
    """rule 6 folds continuation lines into one value; mapping the whole thing
    as ONE name left the second calendar unrenamed behind the guard's floor."""
    text = (
        "insert_job: A\njob_type: c\nmachine: h\ncommand: /x\n"
        "date_conditions: 1\nrun_calendar: ACMEHOLS,\n  ACMEQUARTERS\n"
    )
    out = minify_text(text, verify=False)
    assert "run_calendar: c1, c2\n" in out
    assert "ACME" not in out


def test_the_calendar_condition_predicate_cannot_escape() -> None:
    """A predicate answers yes or no; anything it lets escape becomes an exit-1
    traceback on the one path whose job is to fail closed."""
    from dsl41.minify_rules import validate_keep

    for hostile in ("EOMWORK" * 400, "(" * 300 + "EOMWORK" + ")" * 300, "\x00", "&&&"):
        assert validate_keep("condition", hostile) in (True, False)
