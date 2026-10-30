"""The reread diff (spec-reload steps 2 to 5): pure functions over two catalogs.

Catalogs come from small JIL strings through `lower_source`, the loader the compiler tests use.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from dsl41.ir import CatalogIR, lower_source
from dsl41.reload_diff import (
    Plan,
    definition_hash,
    diff,
    plan_token,
    running_boxes,
)


def _diff(
    live: CatalogIR,
    candidate: CatalogIR,
    live_settings: dict[str, object] | None = None,
    candidate_settings: dict[str, object] | None = None,
) -> Plan:
    return diff(live, candidate, live_settings or {}, candidate_settings or {})


def _job(name: str, extra: str = "", command: str = "run") -> str:
    return f"insert_job: {name}\njob_type: c\ncommand: {command}\nmachine: m1\n{extra}\n"


def _cal(name: str, date_row: str) -> str:
    return f"calendar: {name}\n{date_row}\n\n"


def _scheduled(name: str, calendar: str) -> str:
    return _job(
        name,
        f'date_conditions: 1\nrun_calendar: {calendar}\nstart_times: "01:00"\n',
    )


def _box(name: str, extra: str = "") -> str:
    return f"insert_job: {name}\njob_type: b\n{extra}\n"


def _cat(*parts: str) -> CatalogIR:
    return lower_source("\n".join(parts))


def test_added_removed_changed_unchanged() -> None:
    live = _cat(_job("keep"), _job("edit", command="a"), _job("gone"))
    candidate = _cat(_job("keep"), _job("edit", command="b"), _job("new"))
    plan = _diff(live, candidate)
    assert plan.added == ("new",)
    assert plan.removed == ("gone",)
    assert plan.unchanged == ("keep",)
    (change,) = plan.changed
    assert change.name == "edit"
    assert change.old_hash != change.new_hash
    assert change.old_hash == definition_hash(live.jobs["edit"], live, {})
    assert change.new_hash == definition_hash(candidate.jobs["edit"], candidate, {})
    assert change.reason is None


def test_every_name_tuple_is_sorted() -> None:
    live = _cat(_job("z_gone"), _job("a_gone"), _job("m_keep"), _job("c_keep"))
    candidate = _cat(_job("y_new"), _job("b_new"), _job("m_keep"), _job("c_keep"))
    plan = _diff(live, candidate)
    assert plan.added == ("b_new", "y_new")
    assert plan.removed == ("a_gone", "z_gone")
    assert plan.unchanged == ("c_keep", "m_keep")


def test_identical_catalogs_are_all_unchanged() -> None:
    live = _cat(_job("a"), _job("b"))
    plan = _diff(live, _cat(_job("a"), _job("b")))
    assert plan == Plan(
        added=(), removed=(), changed=(), unchanged=("a", "b"), prior_boxes=(), edited_boxes=()
    )


def test_calendar_edit_marks_every_referencing_job_and_says_why() -> None:
    live = _cat(
        _cal("hols", "01/01/2026 00:00"),
        _scheduled("uses", "hols"),
        _scheduled("also_uses", "hols"),
        _job("plain"),
    )
    candidate = _cat(
        _cal("hols", "01/02/2026 00:00"),
        _scheduled("uses", "hols"),
        _scheduled("also_uses", "hols"),
        _job("plain"),
    )
    plan = _diff(live, candidate)
    assert [c.name for c in plan.changed] == ["also_uses", "uses"]
    assert {c.reason for c in plan.changed} == {"calendar hols changed"}
    assert plan.unchanged == ("plain",)


def test_a_direct_edit_has_no_indirect_reason_even_beside_a_calendar() -> None:
    live = _cat(_cal("hols", "01/01/2026 00:00"), _scheduled("uses", "hols"))
    candidate = _cat(
        _cal("hols", "01/02/2026 00:00"),
        _scheduled("uses", "hols").replace("command: run", "command: other"),
    )
    (change,) = _diff(live, candidate).changed
    assert change.reason is None


def test_machine_and_resource_edits_are_indirect_changes() -> None:
    live = _cat(
        "insert_machine: m1\ntype: a\nnode_name: h1\n",
        "insert_resource: r1\nres_type: R\namount: 2\n",
        _job("j", "resources: (r1,QUANTITY=1)\n"),
    )
    candidate = _cat(
        "insert_machine: m1\ntype: a\nnode_name: h2\n",
        "insert_resource: r1\nres_type: R\namount: 3\n",
        _job("j", "resources: (r1,QUANTITY=1)\n"),
    )
    (change,) = _diff(live, candidate).changed
    assert change.reason == "machine m1 changed; resource r1 changed"


def test_an_unrelated_calendar_edit_changes_no_job() -> None:
    live = _cat(_cal("hols", "01/01/2026 00:00"), _cal("other", "02/01/2026 00:00"), _job("j"))
    candidate = _cat(_cal("hols", "01/01/2026 00:00"), _cal("other", "03/01/2026 00:00"), _job("j"))
    assert _diff(live, candidate).changed == ()


def test_a_reference_to_a_missing_calendar_hashes_as_absent_until_defined() -> None:
    live = _cat(_scheduled("j", "hols"))
    candidate = _cat(_cal("hols", "01/01/2026 00:00"), _scheduled("j", "hols"))
    (change,) = _diff(live, candidate).changed
    assert change.reason == "calendar hols changed"


def test_holcal_edit_reaches_the_job_through_the_extended_calendar() -> None:
    def build(date_row: str) -> CatalogIR:
        return _cat(
            _cal("hols", date_row),
            "extended_calendar: eom\nholcal: hols\nadjust: 0\n",
            _scheduled("j", "eom"),
        )

    (change,) = _diff(build("01/01/2026 00:00"), build("01/02/2026 00:00")).changed
    assert change.reason == "calendar hols changed"


def test_running_box_with_an_added_member() -> None:
    live = _cat(_box("b"), _job("m1", "box_name: b\n"))
    candidate = _cat(_box("b"), _job("m1", "box_name: b\n"), _job("m2", "box_name: b\n"))
    plan = _diff(live, candidate)
    assert running_boxes(plan, candidate, {"b": "RUNNING"}) == ("b",)
    assert running_boxes(plan, candidate, {"b": "SUCCESS"}) == ()
    assert running_boxes(plan, candidate, {}) == ()


def test_running_box_with_a_removed_member() -> None:
    live = _cat(_box("b"), _job("m1", "box_name: b\n"), _job("m2", "box_name: b\n"))
    candidate = _cat(_box("b"), _job("m1", "box_name: b\n"))
    plan = _diff(live, candidate)
    assert running_boxes(plan, candidate, {"b": "RUNNING"}) == ("b",)


def test_running_box_when_a_member_moves_between_boxes() -> None:
    live = _cat(_box("a"), _box("b"), _job("m", "box_name: a\n"))
    candidate = _cat(_box("a"), _box("b"), _job("m", "box_name: b\n"))
    plan = _diff(live, candidate)
    assert running_boxes(plan, candidate, {"a": "RUNNING", "b": "RUNNING"}) == ("a", "b")
    assert running_boxes(plan, candidate, {"a": "RUNNING"}) == ("a",)


def test_editing_a_member_in_place_does_not_touch_its_running_box() -> None:
    live = _cat(_box("b"), _job("m", "box_name: b\n", command="x"))
    candidate = _cat(_box("b"), _job("m", "box_name: b\n", command="y"))
    plan = _diff(live, candidate)
    assert [c.name for c in plan.changed] == ["m"]
    assert running_boxes(plan, candidate, {"b": "RUNNING"}) == ()


def test_removed_running_box_is_flagged() -> None:
    live = _cat(_box("b"), _job("m", "box_name: b\n"))
    candidate = _cat(_job("other"))
    plan = _diff(live, candidate)
    assert running_boxes(plan, candidate, {"b": "RUNNING"}) == ("b",)


def test_changed_running_box_with_the_same_members_is_not_flagged() -> None:
    live = _cat(_box("b", "box_success: s(m)\n"), _job("m", "box_name: b\n"))
    candidate = _cat(_box("b", "box_success: f(m)\n"), _job("m", "box_name: b\n"))
    plan = _diff(live, candidate)
    assert [c.name for c in plan.changed] == ["b"]
    assert running_boxes(plan, candidate, {"b": "RUNNING"}) == ()


def test_description_edit_on_a_running_box_is_not_flagged() -> None:
    live = _cat(_box("b", "description: old\n"), _job("m", "box_name: b\n"))
    candidate = _cat(_box("b", "description: new\n"), _job("m", "box_name: b\n"))
    plan = _diff(live, candidate)
    assert [c.name for c in plan.changed] == ["b"]
    assert running_boxes(plan, candidate, {"b": "RUNNING"}) == ()


def test_calendar_edit_used_by_a_running_box_is_not_flagged() -> None:
    def build(date_row: str) -> CatalogIR:
        return _cat(
            _cal("hols", date_row),
            _box("b", 'date_conditions: 1\nrun_calendar: hols\nstart_times: "01:00"\n'),
            _job("m", "box_name: b\n"),
        )

    live, candidate = build("01/01/2026 00:00"), build("01/02/2026 00:00")
    plan = _diff(live, candidate)
    assert [c.name for c in plan.changed] == ["b"]
    assert running_boxes(plan, candidate, {"b": "RUNNING"}) == ()


def test_a_running_command_job_that_changes_is_not_a_running_box() -> None:
    live = _cat(_job("j", command="x"))
    candidate = _cat(_job("j", command="y"))
    plan = _diff(live, candidate)
    assert running_boxes(plan, candidate, {"j": "RUNNING"}) == ()


def test_adding_a_job_outside_any_box_touches_no_box() -> None:
    live = _cat(_box("b"))
    candidate = _cat(_box("b"), _job("loose"))
    assert running_boxes(_diff(live, candidate), candidate, {"b": "RUNNING"}) == ()


_BASE_JIL = (
    "insert_job: sched\njob_type: c\ncommand: run it\nmachine: m1\n"
    "condition: s(a) & (s(b) | e(c) = 0)\ndate_conditions: 1\nrun_calendar: hols\n"
    'start_times: "01:00"\n'
    "\n"
    "insert_job: a\njob_type: c\ncommand: x\nmachine: m1\n"
)
_CALENDAR_JIL = "\ncalendar: hols\n01/01/2026 00:00\n"


def _spaced(text: str, pads: list[str], comments: list[str]) -> str:
    """The same JIL with padding before lines and comment lines between them."""
    out: list[str] = []
    for index, line in enumerate(text.split("\n")):
        if comment := comments[index % len(comments)]:
            out.append(comment)
        out.append(pads[index % len(pads)] + line)
    return "\n".join(out)


_PAD = st.text(alphabet=" \t", max_size=4)
_COMMENT = st.one_of(
    st.just(""),
    st.text(alphabet="abc xyz", max_size=12).map(lambda t: f"# {t}"),
    st.text(alphabet="abc xyz", max_size=12).map(lambda t: f"/* {t} */"),
)


@settings(max_examples=60, deadline=None)
@given(
    pads=st.lists(_PAD, min_size=1, max_size=6),
    comments=st.lists(_COMMENT, min_size=1, max_size=6),
    around_ops=st.text(alphabet=" ", max_size=3),
)
def test_formatting_only_edit_is_unchanged(
    pads: list[str], comments: list[str], around_ops: str
) -> None:
    live = lower_source(_BASE_JIL + _CALENDAR_JIL)
    jobs = _BASE_JIL.replace("&", f"{around_ops}&{around_ops}")
    candidate = lower_source(_spaced(jobs, pads, comments) + _CALENDAR_JIL)
    plan = _diff(live, candidate)
    assert plan.changed == ()
    assert plan.added == plan.removed == ()
    assert plan.unchanged == ("a", "sched")


def test_condition_edit_inside_a_formatting_pass_is_still_changed() -> None:
    live = lower_source(_BASE_JIL + _CALENDAR_JIL)
    candidate = lower_source(_BASE_JIL.replace("s(a) &", "f(a) &") + _CALENDAR_JIL)
    assert [c.name for c in _diff(live, candidate).changed] == ["sched"]


def test_token_is_stable_and_differs_when_the_live_revision_moves() -> None:
    parts = [("estate.toml", b"e"), ("a.jil", b"a"), ("non-idempotent.toml", b"")]
    assert plan_token(parts, 7) == plan_token(parts, 7)
    assert plan_token(parts, 7) != plan_token(parts, 8)
    assert plan_token(parts, 7) != plan_token([*parts, ("b.jil", b"x")], 7)
    token = plan_token(parts, 7)
    assert len(token) == 64
    assert set(token) <= set("0123456789abcdef")


def test_token_differs_when_only_non_idempotent_toml_changes() -> None:
    base = [("estate.toml", b"e"), ("a.jil", b"a")]
    assert plan_token([*base, ("non-idempotent.toml", b"")], 1) != plan_token(
        [*base, ("non-idempotent.toml", b'jobs = ["x"]')], 1
    )


def test_token_differs_when_bytes_swap_between_files() -> None:
    assert plan_token([("a.jil", b"one"), ("b.jil", b"two")], 1) != plan_token(
        [("a.jil", b"two"), ("b.jil", b"one")], 1
    )


def test_token_differs_for_inputs_that_concatenate_equal() -> None:
    assert plan_token([("a.jil", b"xy"), ("b.jil", b"z")], 1) != plan_token(
        [("a.jil", b"x"), ("b.jil", b"yz")], 1
    )
    assert plan_token([("a.jil", b"xyz")], 1) != plan_token([("a.jil", b"xyz"), ("b.jil", b"")], 1)


def test_estate_setting_change_marks_every_job_changed_with_the_reason() -> None:
    live = _cat(_job("a"), _job("b"))
    candidate = _cat(_job("a"), _job("b"))
    plan = _diff(live, candidate, {"timezone": "Europe/London"}, {"timezone": "America/New_York"})
    assert [c.name for c in plan.changed] == ["a", "b"]
    assert {c.reason for c in plan.changed} == {"estate setting timezone changed"}
    assert plan.unchanged == ()
    same = _diff(live, candidate, {"timezone": "UTC"}, {"timezone": "UTC"})
    assert same.changed == ()


def test_dropping_a_reference_is_reported_as_no_longer_referenced() -> None:
    def build(holcal: str) -> CatalogIR:
        return _cat(
            _cal("hols", "01/01/2026 00:00"),
            _cal("other", "02/01/2026 00:00"),
            f"extended_calendar: eom\nholcal: {holcal}\nadjust: 0\n",
            _scheduled("j", "eom"),
        )

    (change,) = _diff(build("hols"), build("other")).changed
    assert change.reason == (
        "calendar eom changed; calendar hols no longer referenced; calendar other now referenced"
    )


def test_a_job_whose_type_changes_is_removed_and_added() -> None:
    live = _cat(_job("keep"), _job("turns"), _box("bx"))
    candidate = _cat(_job("keep"), _box("turns"), _job("bx"))
    plan = _diff(live, candidate)
    assert plan.added == plan.removed == ("bx", "turns")
    assert plan.changed == ()
    assert plan.unchanged == ("keep",)
    assert plan.edited_boxes == ("bx",)
    assert running_boxes(plan, candidate, {"bx": "RUNNING"}) == ("bx",)
