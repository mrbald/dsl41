"""Boundary classification tests (period-model ss10, DL-131).

Normative spec: `docs/period-model.md` ss10.1 (the three tiers), ss10.2 (the
classification graph and its two directions), ss10.3 (the named cases) and
ss10.4 (armed latches), with obligations PR-37, PR-37a, PR-38 through PR-44
in ss13.7.

Two house rules shape this file.

Every tier row is tested with a CONTRAST -- the same estate and the same
change, one job, two livenesses -- because a classifier that answered R for
everything would pass a file of positives. The contrast is what pins that
the LIVENESS decided the answer.

The profile sweep is DERIVED from `RuntimeProfile.model_fields`, so a field
added later fails this suite until somebody says which jobs it reaches (the
DL-83 discipline). Its negative half is as load-bearing as its positive
half: `retry_horizon_us` must reach NO job.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from dsl41.classify import (
    ARMED_ASSUMPTION,
    BOX,
    CALENDAR,
    INITIAL_STATUS_ASSUMPTION,
    JOB,
    LATENT_ASSUMPTION,
    MACHINE,
    PROFILE,
    PROFILE_CMD,
    PROFILE_FW,
    PROFILE_NO_JOB,
    PROFILE_SCHEDULED,
    RESOURCE,
    RESOURCE_ASSUMPTION,
    TZ_BASIS,
    Baseline,
    CarriedJob,
    CarriedState,
    ClassificationGraph,
    carried_from_oracle,
    classify,
)
from dsl41.capacity import CapacityPool
from dsl41.derive import derive_graph
from dsl41.ir import lower_source
from dsl41.oracle import Oracle
from dsl41.oracle_state import CapacityReservation, Event, JobRuntime
from dsl41.period import RuntimeProfile, job_fingerprints

T0 = datetime(2026, 8, 20, 2, 0)


def _side(text: str, **profile: object) -> Baseline:
    return Baseline(catalog=lower_source(text), profile=RuntimeProfile(**profile))


def _carried(jobs: dict[str, CarriedJob], **rest: object) -> CarriedState:
    return CarriedState(jobs=jobs, now=T0, **rest)  # type: ignore[arg-type]


def _running(**flags: object) -> CarriedJob:
    return CarriedJob(row=JobRuntime(status="RUNNING", status_at=T0), **flags)  # type: ignore[arg-type]


def _inactive(**flags: object) -> CarriedJob:
    return CarriedJob(row=JobRuntime(status="INACTIVE"), **flags)  # type: ignore[arg-type]


#: One CMD job whose command C2 changes -- the smallest estate that has a
#: live job and a moved fingerprint.
_ONE_JOB = "insert_job: j\njob_type: c\nmachine: m1\ncommand: /bin/aaa\n"
_ONE_JOB_C2 = "insert_job: j\njob_type: c\nmachine: m1\ncommand: /bin/bbb\n"


# --------------------------------------------------------- ss10.1 the tiers


@pytest.mark.parametrize(
    ("carried_job", "tier"),
    [
        (CarriedJob(row=JobRuntime(status="RUNNING", status_at=T0)), "executing"),
        (CarriedJob(row=JobRuntime(status="STARTING", status_at=T0)), "executing"),
        (CarriedJob(pending_spawn=True), "executing"),
        (CarriedJob(bound=True), "executing"),
        (CarriedJob(fw_watch=True), "executing"),
    ],
)
def test_ss10_1_every_executing_row_shape_refuses_a_changed_closure(
    carried_job: CarriedJob, tier: str
) -> None:
    """The executing tier, row shape by row shape: RUNNING, STARTING, and
    each of ss3.5's three execution kinds. Changed closure => R."""
    result = classify(
        closing=_side(_ONE_JOB),
        opening=_side(_ONE_JOB_C2),
        carried=_carried({"j": carried_job}),
    )
    got = result.by_job["j"]
    assert (got.tier, got.verdict, got.changed) == (tier, "R", ("job:j",))
    assert got.assumption is None
    assert result.refused == ("j",)


def test_ss10_1_the_same_change_over_a_dead_row_carries() -> None:
    """The contrast the tier table is: one estate, one change, two
    livenesses. Not live => carry, and the job is LISTED as changed rather
    than passed over in silence."""
    result = classify(
        closing=_side(_ONE_JOB),
        opening=_side(_ONE_JOB_C2),
        carried=_carried({"j": _inactive()}),
    )
    got = result.by_job["j"]
    assert (got.tier, got.verdict, got.assumption) == ("not_live", "carry", None)
    assert result.changed_not_live == ("j",)
    assert result.refused == ()


def test_ss10_1_an_executing_row_whose_closure_did_not_move_carries() -> None:
    """R is "live AND changed", never "live". A running job the release does
    not touch crosses the boundary like any other row."""
    other = _ONE_JOB + "\ninsert_job: k\njob_type: c\nmachine: m1\ncommand: /bin/ccc\n"
    other_c2 = _ONE_JOB + "\ninsert_job: k\njob_type: c\nmachine: m1\ncommand: /bin/ddd\n"
    result = classify(
        closing=_side(other),
        opening=_side(other_c2),
        carried=_carried({"j": _running(), "k": _inactive()}),
    )
    assert result.by_job["j"].verdict == "carry"
    assert result.by_job["j"].tier == "executing"
    assert result.by_job["j"].changed == ()
    assert result.refused == ()


@pytest.mark.parametrize(
    "carried_job",
    [
        CarriedJob(row=JobRuntime(armed=True)),
        CarriedJob(row=JobRuntime(status="QUE_WAIT", waiter_seq=1)),
        CarriedJob(timer=True),
    ],
)
def test_ss10_1_every_latent_intent_shape_assumes_and_names_it(carried_job: CarriedJob) -> None:
    """The latent tier, shape by shape: an armed latch, a QUE_WAIT rank, a
    non-stale timer. Changed closure => A, and the A is NAMED -- with the
    general sentence exactly, since this estate has no schedule and no
    condition for a named case to be about."""
    result = classify(
        closing=_side(_ONE_JOB),
        opening=_side(_ONE_JOB_C2),
        carried=_carried({"j": carried_job}),
    )
    got = result.by_job["j"]
    assert (got.tier, got.verdict) == ("latent", "A")
    assert got.assumption == LATENT_ASSUMPTION
    assert result.refused == ()


def test_pr41_inactive_with_a_carried_timer_is_latent_intent() -> None:
    """PR-41: an INACTIVE row is not automatically dead. A carried timer is
    intent the seal is holding, and the contrast is the same row without
    one."""
    with_timer = classify(
        closing=_side(_ONE_JOB),
        opening=_side(_ONE_JOB_C2),
        carried=_carried({"j": CarriedJob(row=JobRuntime(status="INACTIVE"), timer=True)}),
    )
    without = classify(
        closing=_side(_ONE_JOB),
        opening=_side(_ONE_JOB_C2),
        carried=_carried({"j": _inactive()}),
    )
    assert (with_timer.by_job["j"].tier, with_timer.by_job["j"].verdict) == ("latent", "A")
    assert (without.by_job["j"].tier, without.by_job["j"].verdict) == ("not_live", "carry")


def test_pr39a_a_pending_spawn_is_executing_not_latent() -> None:
    """ss10.1's worked scenario, as a classification assertion. C1 starts `j`
    on a passive host and the SPAWN stays pending; C2 changes `j.command`.
    Classified latent, the boundary would open and the C1 effect would
    execute C2's command under C1's run number and reservations -- the
    effect carries no frozen command, `_apply_spawn` reads the CURRENT
    catalog at dispatch. So: R.

    The row is deliberately armed as well, so a reading that let the latent
    rule fire first would answer A and fail here."""
    carried = _carried(
        {
            "j": CarriedJob(
                row=JobRuntime(status="RUNNING", status_at=T0, armed=True), pending_spawn=True
            )
        }
    )
    got = classify(closing=_side(_ONE_JOB), opening=_side(_ONE_JOB_C2), carried=carried).by_job["j"]
    assert (got.tier, got.verdict, got.assumption) == ("executing", "R", None)


def test_pr40_que_wait_removed_in_c2_is_r() -> None:
    """PR-40: a waiter whose job C2 deletes is R, not A -- the latch has
    nothing left to start, and `sorted_waiters` would have to guess a
    priority for a job the catalog no longer has. No KeyError anywhere on
    the path."""
    two = _ONE_JOB + "\ninsert_job: k\njob_type: c\nmachine: m1\ncommand: /bin/ccc\n"
    carried = _carried({"j": CarriedJob(row=JobRuntime(status="QUE_WAIT", waiter_seq=3))})
    opening = _side(_ONE_JOB.replace("insert_job: j", "insert_job: k"))
    got = classify(closing=_side(two), opening=opening, carried=carried)
    assert got.by_job["j"].verdict == "R"
    assert got.by_job["j"].tier == "latent"
    assert got.refused == ("j",)
    # the classifier is the GATE over ss5's documented default, not a
    # replacement for it: the queue still sorts such a waiter last instead
    # of raising, so a classifier bug is a misordered queue and not a crash
    assert CapacityPool(opening.catalog).sorted_waiters(carried.rows) == ["j"]


def test_ss10_1_removed_and_not_live_is_a_retained_ghost() -> None:
    """Removed AND dead is a ghost: retained, listed, and not a refusal. The
    contrast with the test above is liveness alone."""
    two = _ONE_JOB + "\ninsert_job: k\njob_type: c\nmachine: m1\ncommand: /bin/ccc\n"
    only_k = "insert_job: k\njob_type: c\nmachine: m1\ncommand: /bin/ccc\n"
    result = classify(
        closing=_side(two), opening=_side(only_k), carried=_carried({"j": _inactive()})
    )
    assert result.ghosts == ("j",)
    assert result.by_job["j"].verdict == "carry"
    assert "j" not in result.changed_not_live
    assert result.refused == ()


def test_ss10_1_a_ghost_from_an_earlier_period_is_still_listed() -> None:
    """A ghost is RETAINED, so at the next boundary it is in neither
    catalog. "Removed" therefore reads the opening catalog alone: a job C1
    dropped and a job dropped two periods ago are the same fact to a row
    that is still there. A classifier that diffed the two catalogs would
    stop listing the row while it was still carried -- and would answer
    `carry` for one that is somehow live under a job nothing can dispatch or
    kill."""
    estate = "insert_job: k\njob_type: c\nmachine: m1\ncommand: /bin/ccc\n"
    result = classify(
        closing=_side(estate), opening=_side(estate), carried=_carried({"gone": _inactive()})
    )
    assert result.ghosts == ("gone",)
    assert result.by_job["gone"].verdict == "carry"
    live = classify(
        closing=_side(estate), opening=_side(estate), carried=_carried({"gone": _running()})
    )
    assert live.by_job["gone"].verdict == "R"
    assert live.ghosts == ()


def test_ss10_1_removed_and_executing_is_r() -> None:
    """A removed job that is RUNNING refuses: it is not in `dispatchable`,
    so a KILL for it plans no effect and KILLJOB would stop nothing --
    nothing can end the run C1 started."""
    two = _ONE_JOB + "\ninsert_job: k\njob_type: c\nmachine: m1\ncommand: /bin/ccc\n"
    only_k = "insert_job: k\njob_type: c\nmachine: m1\ncommand: /bin/ccc\n"
    result = classify(
        closing=_side(two), opening=_side(only_k), carried=_carried({"j": _running()})
    )
    assert result.by_job["j"].verdict == "R"
    assert result.ghosts == ()


# ------------------------------------------------------------- ss10.2 boxes

_BOX_TWO_DEEP = (
    "insert_job: outer\njob_type: b\n\n"
    "insert_job: inner\njob_type: b\nbox_name: outer\n\n"
    "insert_job: leaf\njob_type: c\nmachine: m1\ncommand: /bin/aaa\nbox_name: inner\n"
)


def test_e19_member_changed_while_its_box_executes_is_r() -> None:
    """E19: the member is INACTIVE, its box is RUNNING, and C2 changes the
    member. A would let the member start under C2 INSIDE the box's C1
    execution -- one box run observing two catalogs. R.

    The contrast is the same estate with the box not running."""
    c2 = _BOX_TWO_DEEP.replace("/bin/aaa", "/bin/bbb")
    live_box = classify(
        closing=_side(_BOX_TWO_DEEP),
        opening=_side(c2),
        carried=_carried({"outer": _running(), "leaf": _inactive()}),
    )
    dead_box = classify(
        closing=_side(_BOX_TWO_DEEP),
        opening=_side(c2),
        carried=_carried({"outer": _inactive(), "leaf": _inactive()}),
    )
    assert (live_box.by_job["leaf"].tier, live_box.by_job["leaf"].verdict) == ("executing", "R")
    assert (dead_box.by_job["leaf"].tier, dead_box.by_job["leaf"].verdict) == ("not_live", "carry")


def test_ss10_2_box_containment_moves_at_any_nesting_depth() -> None:
    """`box_name` moving two levels down moves the containment node of every
    box above it, and the outer box's forward closure reaches the leaf."""
    moved = _BOX_TWO_DEEP.replace("box_name: inner", "box_name: outer")
    graph = ClassificationGraph(_side(_BOX_TWO_DEEP), _side(moved))
    assert "box:outer" in graph.changed
    assert "box:inner" in graph.changed
    assert JOB + "leaf" in graph.forward(JOB + "outer")
    assert BOX + "inner" in graph.forward(JOB + "leaf")


def test_pr42_box_membership_changed_while_the_box_executes_is_r() -> None:
    """PR-42: membership itself is the change -- no job's command moved --
    and the running box refuses. Every member refuses with it: no box run
    observes two versions of anything in its closure."""
    moved = _BOX_TWO_DEEP.replace("box_name: inner", "box_name: outer")
    result = classify(
        closing=_side(_BOX_TWO_DEEP),
        opening=_side(moved),
        carried=_carried({"outer": _running(), "inner": _inactive(), "leaf": _inactive()}),
    )
    assert result.by_job["outer"].verdict == "R"
    assert "box:outer" in result.by_job["outer"].changed
    assert result.by_job["leaf"].verdict == "R"


def test_pr38_two_hop_condition_and_nested_containment_reach_the_closure() -> None:
    """PR-38: the closure is transitive in both shapes the estate has --
    a -> b -> c over conditions, and leaf -> inner -> outer over boxes."""
    chain = (
        "insert_job: a\njob_type: c\nmachine: m1\ncommand: /bin/aaa\n\n"
        "insert_job: b\njob_type: c\nmachine: m1\ncommand: x\ncondition: s(a)\n\n"
        "insert_job: c\njob_type: c\nmachine: m1\ncommand: x\ncondition: s(b)\n"
    )
    graph = ClassificationGraph(_side(chain), _side(chain.replace("/bin/aaa", "/bin/bbb")))
    assert JOB + "a" in graph.forward(JOB + "c")
    result = classify(
        closing=_side(chain),
        opening=_side(chain.replace("/bin/aaa", "/bin/bbb")),
        carried=_carried({"c": _running()}),
    )
    assert result.by_job["c"].verdict == "R"
    box_graph = ClassificationGraph(_side(_BOX_TWO_DEEP), _side(_BOX_TWO_DEEP))
    assert JOB + "outer" in box_graph.forward(JOB + "leaf")


# --------------------------------------------------- ss10.2 the profile edges


_PROFILE_ESTATE = (
    "insert_job: sched\njob_type: c\nmachine: m1\ncommand: x\n"
    'date_conditions: 1\ndays_of_week: mo\nstart_times: "04:10"\n\n'
    "insert_job: plain\njob_type: c\nmachine: m1\ncommand: y\n\n"
    "insert_job: watcher\njob_type: f\nmachine: m1\nwatch_file: /tmp/x\n"
)

#: One changed value per `RuntimeProfile` field. The keys ARE the
#: completeness check: a new field with no case here fails the sweep.
_PROFILE_BUMPS: dict[str, object] = {
    "default_tz": "Europe/Zurich",
    "tz_aliases": {"tokyo": "Asia/Tokyo"},
    "as_machine": ("m1",),
    "machine_policy": "local-eligible",
    "execution_mode": "detached",
    "deadman_us": 30_000_000,
    "fw_default_interval_us": 90_000_000,
    "cmd_grace_us": 20_000_000,
    "reconcile_settle_us": 1_000_000,
    "spawn_window_us": 1_000_000,
    "retry_horizon_us": 120_000_000,
}


def test_pr37a_the_profile_field_map_covers_every_field() -> None:
    """ss10.2's mapping is exhaustive over the model, and its four groups do
    not overlap. A field nobody placed reaches every job or no job by
    accident -- both are wrong, and both are silent."""
    groups = (PROFILE_SCHEDULED, PROFILE_CMD, PROFILE_FW, PROFILE_NO_JOB)
    placed = [field for group in groups for field in group]
    assert sorted(placed) == sorted(RuntimeProfile.model_fields)
    assert len(placed) == len(set(placed))
    assert set(_PROFILE_BUMPS) == set(RuntimeProfile.model_fields)


@pytest.mark.parametrize("field", sorted(_PROFILE_BUMPS))
def test_pr37a_profile_edges_run_from_job_to_field(field: str) -> None:
    """PR-37a, table-driven over every field: the edge runs FROM a job TO
    the field, so the field is in the forward closure of exactly the jobs
    ss10.2 names -- and of no others.

    A reversed-edge implementation ("field -> job") reaches no profile field
    from any job, so it fails every row of this table that names one -- ten
    of the eleven -- while passing every other obligation in ss13.7. That is
    why the direction is asserted on the closure and not only on a verdict."""
    expected = {
        **{f: {"sched"} for f in PROFILE_SCHEDULED},
        **{f: {"sched", "plain"} for f in PROFILE_CMD},
        **{f: {"watcher"} for f in PROFILE_FW},
        **{f: set() for f in PROFILE_NO_JOB},
    }[field]
    graph = ClassificationGraph(_side(_PROFILE_ESTATE), _side(_PROFILE_ESTATE))
    reaching = {
        name
        for name in ("sched", "plain", "watcher")
        if PROFILE + field in graph.forward(JOB + name)
    }
    assert reaching == expected


@pytest.mark.parametrize("field", sorted(_PROFILE_BUMPS))
def test_pr37a_a_changed_profile_field_classifies_exactly_its_own_jobs(field: str) -> None:
    """The same table, one level up: changing one field lists it as changed
    for exactly those jobs. `retry_horizon_us` moves `runtime_hash` and
    classifies NO job -- it is boundary policy, and a horizon tweak that
    reached every job would drain all live work in the estate."""
    expected = {
        **{f: {"sched"} for f in PROFILE_SCHEDULED},
        **{f: {"sched", "plain"} for f in PROFILE_CMD},
        **{f: {"watcher"} for f in PROFILE_FW},
        **{f: set() for f in PROFILE_NO_JOB},
    }[field]
    closing = _side(_PROFILE_ESTATE)
    opening = _side(_PROFILE_ESTATE, **{field: _PROFILE_BUMPS[field]})
    assert closing.profile != opening.profile
    result = classify(
        closing=closing,
        opening=opening,
        carried=_carried({name: _running() for name in ("sched", "plain", "watcher")}),
    )
    named = {v.job for v in result.verdicts if PROFILE + field in v.changed}
    assert named == expected
    assert set(result.refused) == expected
    # the field must be MODELLED, not merely absent: without this a
    # `retry_horizon_us` deleted from the node map passes its row, and
    # "reaches no job" would be indistinguishable from "was never a node"
    assert PROFILE + field in result.changed_nodes


def test_pr37a_a_live_cmd_with_only_the_grace_changed_is_r() -> None:
    """The obligation's own example: nothing in the JIL moved, only
    `cmd_grace_us`. A boundary that committed over this would kill the C1
    run with C2's ladder."""
    closing = _side(_ONE_JOB)
    opening = _side(_ONE_JOB, cmd_grace_us=20_000_000)
    live = classify(closing=closing, opening=opening, carried=_carried({"j": _running()}))
    dead = classify(closing=closing, opening=opening, carried=_carried({"j": _inactive()}))
    assert live.by_job["j"].verdict == "R"
    assert live.by_job["j"].changed == (PROFILE + "cmd_grace_us",)
    assert dead.by_job["j"].verdict == "carry"


def test_pr37a_the_timezone_basis_reaches_scheduled_jobs_only() -> None:
    """The basis node is the pair every tick resolves through, so it hangs
    off jobs with `start_times`, `start_mins` or a calendar -- and off no
    other job, whatever its type."""
    graph = ClassificationGraph(_side(_PROFILE_ESTATE), _side(_PROFILE_ESTATE))
    assert TZ_BASIS in graph.forward(JOB + "sched")
    assert TZ_BASIS not in graph.forward(JOB + "plain")
    assert TZ_BASIS not in graph.forward(JOB + "watcher")
    moved = ClassificationGraph(
        _side(_PROFILE_ESTATE), _side(_PROFILE_ESTATE, default_tz="Europe/Zurich")
    )
    assert TZ_BASIS in moved.changed


# ------------------------------------------- ss10.2 the non-job node kinds


_PR37_ESTATE = (
    "insert_resource: FUEL\nres_type: R\namount: 8\n\n"
    "insert_machine: m1\ntype: a\nnode_name: hostaaa\n\n"
    "insert_xinst: PRD\nxtype: a\nxport: 9000\n\n"
    "insert_global: G\nvalue: 1\n\n"
    "calendar: cal_a\n01/01/2026 00:00\n\n"
    "insert_job: j\njob_type: c\nmachine: m1\ncommand: x\n"
    "resources: (FUEL, QUANTITY=2)\n"
    'date_conditions: 1\nrun_calendar: cal_a\nstart_times: "04:10"\n'
    "condition: s(remote^PRD) & v(G) = 1\n"
)

_PR37_CASES = {
    "resource": ("amount: 8", "amount: 2", RESOURCE + "FUEL"),
    "machine": ("node_name: hostaaa", "node_name: hostbbb", MACHINE + "m1"),
    "xinst": ("xport: 9000", "xport: 9001", "xinst:PRD"),
    "global": ("value: 1", "value: 2", "global:G"),
    "calendar": ("01/01/2026 00:00", "02/01/2026 00:00", CALENDAR + "cal_a"),
}


@pytest.mark.parametrize("case", sorted(_PR37_CASES))
def test_pr37_a_non_job_change_classifies_dependents_without_moving_a_job(case: str) -> None:
    """PR-37: each of a resource amount, a machine field, `insert_xinst`, a
    declared global default and a calendar's date set classifies the
    dependent -- and none of them moves a `JobIR` fingerprint or an IR-G
    edge. This is the whole reason the graph exists: the leaf test and IR-G
    together cannot see any of these."""
    old, new, node = _PR37_CASES[case]
    closing, opening = _side(_PR37_ESTATE), _side(_PR37_ESTATE.replace(old, new))
    assert job_fingerprints(closing.catalog) == job_fingerprints(opening.catalog)
    assert _edge_keys(closing) == _edge_keys(opening)
    result = classify(closing=closing, opening=opening, carried=_carried({"j": _running()}))
    assert node in result.changed_nodes
    assert result.by_job["j"].changed == (node,)
    assert result.by_job["j"].verdict == "R"


def test_pr37_the_timezone_map_classifies_dependents_the_same_way() -> None:
    """The sixth PR-37 row lives in the profile, not the catalog: a
    `tz_aliases` edit moves no JIL byte at all."""
    closing = _side(_PR37_ESTATE)
    opening = _side(_PR37_ESTATE, tz_aliases={"tokyo": "Asia/Tokyo"})
    assert closing.catalog == opening.catalog
    result = classify(closing=closing, opening=opening, carried=_carried({"j": _running()}))
    assert result.by_job["j"].verdict == "R"
    assert TZ_BASIS in result.by_job["j"].changed


def _edge_keys(side: Baseline) -> list[tuple[str, str, str]]:
    return sorted((e.src, e.dst, e.via) for e in derive_graph(side.catalog).edges)


def test_ss10_2_a_machine_pool_member_is_in_the_closure() -> None:
    """ "its `machine:` and that machine's members": a job pinned to a pool
    depends on every component the pool resolves to."""
    pool = (
        "insert_machine: a1\ntype: a\nnode_name: hostaaa\n\n"
        "insert_machine: pool\ntype: v\nmachine: a1\n\n"
        "insert_job: j\njob_type: c\nmachine: pool\ncommand: x\n"
    )
    graph = ClassificationGraph(_side(pool), _side(pool.replace("hostaaa", "hostbbb")))
    assert MACHINE + "a1" in graph.forward(JOB + "j")
    assert MACHINE + "a1" in graph.changed
    result = classify(
        closing=_side(pool),
        opening=_side(pool.replace("hostaaa", "hostbbb")),
        carried=_carried({"j": _running()}),
    )
    assert result.by_job["j"].verdict == "R"


@pytest.mark.parametrize("cyccal", ["cyccal: cyc", 'cyccal: "cyc"'])
def test_ss10_2_an_extended_calendar_reaches_its_cycle(cyccal: str) -> None:
    """A cycle is a date set one hop further out: the job names the
    calendar, the calendar names the cycle, and the closure spans both.

    Both spellings of the reference, because an `autocal_asc` export quotes
    names (SEM-36/37, DL-60) and a reference nobody unquoted would silently
    name a cycle that does not exist -- an edge missing from the graph,
    which is the failure mode with no symptom."""
    estate = (
        "cycle: cyc\nstart_date: 03/28/2026\nend_date: 04/02/2026\n\n"
        f"extended_calendar: cwrk\nworkday: mo,tu,we,th,fr\n{cyccal}\ncondition: CWRK#L\n\n"
        "insert_job: j\njob_type: c\nmachine: m1\ncommand: x\n"
        'date_conditions: 1\nrun_calendar: cwrk\nstart_times: "08:00"\n'
    )
    moved = estate.replace("end_date: 04/02/2026", "end_date: 04/03/2026")
    graph = ClassificationGraph(_side(estate), _side(moved))
    assert "cycle:cyc" in graph.forward(JOB + "j")
    assert "cycle:cyc" in graph.changed
    result = classify(
        closing=_side(estate), opening=_side(moved), carried=_carried({"j": _running()})
    )
    assert result.by_job["j"].verdict == "R"


# -------------------------------------------------------- ss10.3 named cases


def test_pr39_the_armed_a_is_reachable_and_not_shadowed_by_an_r_rule() -> None:
    """PR-39, and ss10.3's first named case with it: draft 2 defined `armed`
    as live and ruled R over A, which made every named A case unreachable.
    The armed row must REACH A -- with the sentence ss10.3 writes, since the
    schedule is what moved -- and the same row RUNNING must reach R.

    One test, not two: an ss10.3 case that asserted the same sentence over
    the same estate without the R arm would be a copy that can only drift."""
    armed = (
        "insert_job: j\njob_type: c\nmachine: m1\ncommand: x\n"
        'date_conditions: 1\ndays_of_week: mo\nstart_times: "04:10"\n'
    )
    moved = armed.replace('"04:10"', '"05:10"')
    latent = classify(
        closing=_side(armed),
        opening=_side(moved),
        carried=_carried({"j": CarriedJob(row=JobRuntime(armed=True))}),
    )
    executing = classify(
        closing=_side(armed),
        opening=_side(moved),
        carried=_carried(
            {"j": CarriedJob(row=JobRuntime(status="RUNNING", status_at=T0, armed=True))}
        ),
    )
    assert latent.by_job["j"].verdict == "A"
    assert latent.by_job["j"].assumption == ARMED_ASSUMPTION
    assert executing.by_job["j"].verdict == "R"


def test_ss10_3_an_armed_row_whose_trigger_did_not_move_gets_the_general_a() -> None:
    """The armed sentence is about the TRIGGER: ss10.3 names a changed
    schedule or condition, and nothing else. An armed job whose command
    changed is still an A -- the latch still crosses -- but telling an
    operator "the C1 trigger survives under C2 gating" when the gating is
    what did not move would be a sentence that misnames the risk."""
    result = classify(
        closing=_side(_ONE_JOB),
        opening=_side(_ONE_JOB_C2),
        carried=_carried({"j": CarriedJob(row=JobRuntime(armed=True))}),
    )
    assert result.by_job["j"].assumption == LATENT_ASSUMPTION


_FUEL = (
    "insert_resource: FUEL\nres_type: R\namount: 8\n\n"
    "insert_job: holder\njob_type: c\nmachine: m1\ncommand: x\nresources: (FUEL, QUANTITY=3)\n\n"
    "insert_job: waiter\njob_type: c\nmachine: m1\ncommand: y\nresources: (FUEL, QUANTITY=3)\n"
)


def test_ss10_3_a_resource_lowered_below_carried_use_is_a() -> None:
    """ss10.3: C2 lowers FUEL below what the carried state has already spent
    plus what live rows hold, and the waiter is told the assumption --
    admission refuses until releases catch up. Nothing is refused: the
    period may open, it will simply not admit."""
    result = classify(
        closing=_side(_FUEL),
        opening=_side(_FUEL.replace("amount: 8", "amount: 2")),
        carried=_carried(
            {"waiter": CarriedJob(row=JobRuntime(status="QUE_WAIT", waiter_seq=1))},
            consumed={"r:FUEL": 4},
        ),
    )
    got = result.by_job["waiter"]
    assert (got.verdict, got.assumption) == ("A", RESOURCE_ASSUMPTION)
    assert result.refused == ()


def test_pr43_a_running_holder_of_the_lowered_resource_is_r() -> None:
    """PR-43, the precedence case ss10.1 names: the executing rule and a
    named A rule both fire on one job, and R wins. Same estate, same lowered
    resource, one row moved from QUE_WAIT to RUNNING-and-holding."""
    held = CapacityReservation(bucket="r:FUEL", units=3, release_policy="completion")
    result = classify(
        closing=_side(_FUEL),
        opening=_side(_FUEL.replace("amount: 8", "amount: 2")),
        carried=_carried(
            {
                "holder": CarriedJob(
                    row=JobRuntime(status="RUNNING", status_at=T0, reservations=(held,))
                ),
                "waiter": CarriedJob(row=JobRuntime(status="QUE_WAIT", waiter_seq=1)),
            }
        ),
    )
    assert result.by_job["holder"].verdict == "R"
    assert result.by_job["holder"].assumption is None
    assert result.by_job["waiter"].assumption == RESOURCE_ASSUMPTION
    assert result.refused == ("holder",)


def test_ss10_3_initial_status_changed_while_the_carried_row_disagrees_is_a() -> None:
    """ss10.3's last named A: genesis seeding writes NEW rows only, so a
    changed `initial_status` never rewrites a carried row -- it is recorded
    as an assumption instead. The contrast is the row that already AGREES
    with C2, which needs no assumption of its own."""
    plain = "insert_job: j\njob_type: c\nmachine: m1\ncommand: x\n"
    iced = "insert_job: j\njob_type: c\nmachine: m1\ncommand: x\nstatus: on_ice\n"
    disagrees = classify(
        closing=_side(plain),
        opening=_side(iced),
        carried=_carried({"j": CarriedJob(row=JobRuntime(armed=True))}),
    )
    agrees = classify(
        closing=_side(plain),
        opening=_side(iced),
        carried=_carried({"j": CarriedJob(row=JobRuntime(armed=True, on_ice=True))}),
    )
    assert disagrees.by_job["j"].assumption == INITIAL_STATUS_ASSUMPTION
    assert agrees.by_job["j"].assumption == LATENT_ASSUMPTION


def test_ss10_3_a_live_fw_whose_watch_parameters_changed_is_r() -> None:
    """ss10.3: an FW run is in-engine -- the poll loop, its interval and its
    progress belong to this process -- so a changed watch refuses while the
    watch is live."""
    fw = "insert_job: w\njob_type: f\nmachine: m1\nwatch_file: /tmp/x\nwatch_interval: 30\n"
    live = classify(
        closing=_side(fw),
        opening=_side(fw.replace("watch_interval: 30", "watch_interval: 60")),
        carried=_carried({"w": CarriedJob(fw_watch=True)}),
    )
    dead = classify(
        closing=_side(fw),
        opening=_side(fw.replace("watch_interval: 30", "watch_interval: 60")),
        carried=_carried({"w": _inactive()}),
    )
    assert live.by_job["w"].verdict == "R"
    assert dead.by_job["w"].verdict == "carry"


def test_ss10_3_a_live_fw_refuses_its_own_default_interval_moving() -> None:
    """The same case through the profile: `fw_default_interval_us` is the
    interval a watch with no `watch_interval:` actually polls at."""
    fw = "insert_job: w\njob_type: f\nmachine: m1\nwatch_file: /tmp/x\n"
    result = classify(
        closing=_side(fw),
        opening=_side(fw, fw_default_interval_us=90_000_000),
        carried=_carried({"w": CarriedJob(fw_watch=True)}),
    )
    assert result.by_job["w"].verdict == "R"


# ------------------------------------------------- ss10.2 the two directions


_TRUTH_ESTATE = (
    "insert_global: G\nvalue: 1\n\n"
    "insert_job: gate\njob_type: c\nmachine: m1\ncommand: x\ncondition: v(G) = 2\n"
)


def test_pr44_the_reverse_closure_produces_the_boundary_truth_diff() -> None:
    """PR-44: C2 changes a global's declared default; `gate`'s condition
    text does not move at all. The reverse closure of `global:G` finds the
    dependent, and evaluating its condition under both catalogs at the one
    carried state shows the readiness flip -- with both values, so a reader
    sees the direction."""
    result = classify(
        closing=_side(_TRUTH_ESTATE),
        opening=_side(_TRUTH_ESTATE.replace("value: 1", "value: 2")),
        carried=_carried({"gate": _inactive()}),
    )
    assert len(result.readiness_flips) == 1
    flip = result.readiness_flips[0]
    assert (flip.job, flip.before, flip.after) == ("gate", False, True)


def test_pr44_a_carried_global_value_beats_the_declared_default() -> None:
    """The diff evaluates at the CARRIED state, not at genesis: a global the
    period already latched keeps its value across the boundary (SEM-06), so
    a changed DEFAULT flips nothing."""
    result = classify(
        closing=_side(_TRUTH_ESTATE),
        opening=_side(_TRUTH_ESTATE.replace("value: 1", "value: 2")),
        carried=_carried({"gate": _inactive()}, globals_={"G": "1"}),
    )
    assert result.readiness_flips == ()


def test_ss10_2_forward_and_reverse_answer_different_questions() -> None:
    """ "Two questions, two directions. Both are computed; neither
    substitutes for the other."

    Arm 1: a live CMD whose only change is `cmd_grace_us`. The forward
    closure refuses the boundary; no condition anywhere mentions a profile
    field, so the truth diff is empty. A classifier built only from the
    reverse direction would have opened over a live run.

    Arm 2: a dead job whose condition C2 rewrites. Nothing is live, so the R
    gate says nothing; the reverse closure plus truth evaluation says the
    job's readiness flipped. A classifier built only from the forward
    direction would have reported nothing to the operator."""
    forward_only = classify(
        closing=_side(_ONE_JOB),
        opening=_side(_ONE_JOB, cmd_grace_us=20_000_000),
        carried=_carried({"j": _running()}),
    )
    assert forward_only.refused == ("j",)
    assert forward_only.readiness_flips == ()

    reverse_only = classify(
        closing=_side(_TRUTH_ESTATE),
        opening=_side(_TRUTH_ESTATE.replace("condition: v(G) = 2", "condition: v(G) = 1")),
        carried=_carried({"gate": _inactive()}, globals_={"G": "1"}),
    )
    assert reverse_only.refused == ()
    assert [(f.job, f.before, f.after) for f in reverse_only.readiness_flips] == [
        ("gate", False, True)
    ]


def test_ss10_2_the_truth_diff_reads_carried_statuses_through_the_interpreter() -> None:
    """The diff borrows the interpreter's own evaluation, so SEM-05 holds
    inside it: an ON_ICE predecessor satisfies its dependent's atom, lookback
    ignored. A second evaluator written here would have to re-derive that,
    and would drift.

    The ice has to do OBSERVABLE work, so the two arms differ only in it and
    the estate is built so that ice decides the C1 side alone: `b` waits on
    `a` under C1 and on the succeeded `c` under C2. Iced, `b` was already
    ready and nothing flips; not iced, `b` becomes ready and the flip is
    reported. Both arms have the same candidate set -- `b`'s own condition
    moved either way -- so an empty first arm cannot be the diff skipping
    it."""
    estate = (
        "insert_job: a\njob_type: c\nmachine: m1\ncommand: x\n\n"
        "insert_job: c\njob_type: c\nmachine: m1\ncommand: z\n\n"
        "insert_job: b\njob_type: c\nmachine: m1\ncommand: y\ncondition: s(a)\n"
    )
    swapped = estate.replace("condition: s(a)", "condition: s(c)")
    done = CarriedJob(row=JobRuntime(status="SUCCESS", status_at=T0, last_end_at=T0))
    iced = classify(
        closing=_side(estate),
        opening=_side(swapped),
        carried=_carried(
            {
                "a": CarriedJob(row=JobRuntime(status="INACTIVE", on_ice=True)),
                "c": done,
                "b": _inactive(),
            }
        ),
    )
    plain = classify(
        closing=_side(estate),
        opening=_side(swapped),
        carried=_carried({"a": _inactive(), "c": done, "b": _inactive()}),
    )
    assert iced.readiness_flips == ()  # SEM-05: the iced `a` already satisfied s(a)
    assert [(f.job, f.before, f.after) for f in plain.readiness_flips] == [("b", False, True)]


# ----------------------------------------------------------- the carried input


def test_carried_from_oracle_reads_rows_globals_consumed_and_live_timers() -> None:
    """The convenience builder: what U6 does from a seal, done from a live
    interpreter. The execution sets are passed in because their evidence is
    the outbox and the spool, not the oracle."""
    estate = (
        "insert_job: j\njob_type: c\nmachine: m1\ncommand: x\n"
        'date_conditions: 1\ndays_of_week: mo\nstart_times: "04:10"\n'
        "term_run_time: 30\n\n"
        "insert_global: G\nvalue: 1\n"
    )
    oracle = Oracle(lower_source(estate))
    oracle.feed(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": "j"}))
    oracle.store.begin_input()
    oracle.store.seed_consumed({"r:FUEL": 2})
    oracle.store.commit_input()
    carried = carried_from_oracle(oracle, now=T0 + timedelta(minutes=1), bound=["j"])
    assert carried.jobs["j"].row.status == "RUNNING"
    assert carried.jobs["j"].bound is True
    assert carried.jobs["j"].timer is True  # the term_run_time deadline is live
    assert carried.globals_["G"] == "1"
    # SEM-16's spent units come across: they are in no row, and a boundary
    # that rebuilt them from the holders would refund every depletable (ss5)
    assert carried.consumed == {"r:FUEL": 2}
    assert carried.now == T0 + timedelta(minutes=1)
    assert carried.rows["j"] is carried.jobs["j"].row


def test_ss10_the_verdict_map_is_total_and_ordered() -> None:
    """Every job of either catalog gets exactly one verdict, in name order.
    Phase 2's output IS the sidecar's `classification` field, and audit
    reproduces it byte for byte -- an unordered or partial map cannot be
    reproduced at all."""
    two = _ONE_JOB + "\ninsert_job: k\njob_type: c\nmachine: m1\ncommand: /bin/ccc\n"
    added = two + "\ninsert_job: b\njob_type: c\nmachine: m1\ncommand: /bin/ddd\n"
    result = classify(closing=_side(two), opening=_side(added), carried=_carried({"j": _running()}))
    assert [v.job for v in result.verdicts] == ["b", "j", "k"]
    assert result.by_job["b"].verdict == "carry"
    again = classify(closing=_side(two), opening=_side(added), carried=_carried({"j": _running()}))
    assert again == result


# ------------------------------------------------ round-1 review pins (DL-131)


def test_a_bare_mutex_n_atom_is_still_a_dependency() -> None:
    """IR-G diverts a local unqualified n() into mutex_groups and keeps no
    edge for it (M07) -- a classifier reading IR-G's edges would carry a
    boundary over `b: condition: n(a)` while b executes and C2 changes a.
    The walk reads the atoms, so the edge exists and the verdict is R."""
    c1 = lower_source(
        "insert_job: a\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: b\njob_type: c\ncommand: x\nmachine: m1\ncondition: n(a)\n"
    )
    c2 = lower_source(
        "insert_job: a\njob_type: c\ncommand: CHANGED\nmachine: m1\n\n"
        "insert_job: b\njob_type: c\ncommand: x\nmachine: m1\ncondition: n(a)\n"
    )
    carried = CarriedState(jobs={"b": CarriedJob(row=JobRuntime(status="RUNNING", run_number=1))})
    result = classify(closing=Baseline(catalog=c1), opening=Baseline(catalog=c2), carried=carried)
    assert result.by_job["b"].verdict == "R"
    assert "job:a" in result.by_job["b"].changed


def test_a_calendar_description_edit_is_not_a_change() -> None:
    """The autocal rule engine reads six attributes; everything else on the
    record is descriptive, and a description edit must not refuse a live
    boundary whose date set is identical."""

    def estate(extra: str) -> str:
        return (
            "extended_calendar: cal\nworkday: mo,tu,we,th,fr\n"
            "condition: WORKD#1\n" + extra + "\n"
            "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"
            "date_conditions: 1\nrun_calendar: cal\nstart_mins: 0\n"
        )

    c1 = lower_source(estate(""))
    c2 = lower_source(estate("description: touched\n"))
    graph = ClassificationGraph(Baseline(catalog=c1), Baseline(catalog=c2))
    assert "calendar:cal" not in graph.changed
    # and the six semantic attributes still move it
    c3 = lower_source(estate("").replace("workday: mo,tu,we,th,fr", "workday: mo"))
    graph2 = ClassificationGraph(Baseline(catalog=c1), Baseline(catalog=c3))
    assert "calendar:cal" in graph2.changed


def test_machine_member_order_is_spelling_not_membership() -> None:
    """Resolution folds the pool any-of, so [a,b] and [b,a] are one pool --
    and dropping a member is still a change."""

    def estate(members: str) -> str:
        return (
            "insert_machine: la\ntype: a\n\ninsert_machine: lb\ntype: a\n\n"
            f"insert_machine: pool\ntype: v\n{members}\n"
            "insert_job: j\njob_type: c\ncommand: x\nmachine: pool\n"
        )

    c1 = lower_source(estate("machine: la\nmachine: lb"))
    c2 = lower_source(estate("machine: lb\nmachine: la"))
    c3 = lower_source(estate("machine: la"))
    assert (
        "machine:pool"
        not in ClassificationGraph(Baseline(catalog=c1), Baseline(catalog=c2)).changed
    )
    assert "machine:pool" in ClassificationGraph(Baseline(catalog=c1), Baseline(catalog=c3)).changed


def test_initial_status_agreement_reads_the_whole_flag_vector() -> None:
    """A row carrying HOLD+ICE against a C2 seed of ICE alone still
    disagrees: the retained HOLD is exactly what the operator is told."""
    from dsl41.classify import _row_agrees

    both = JobRuntime(on_hold=True, on_ice=True)
    assert not _row_agrees(both, "ON_ICE")
    only_ice = JobRuntime(on_ice=True)
    assert _row_agrees(only_ice, "ON_ICE")
    assert not _row_agrees(only_ice, "ON_HOLD")
    clean = JobRuntime()
    assert _row_agrees(clean, None)


# ------------------------------------------------ round-2 review pins (DL-131)


@pytest.mark.parametrize("attribute", ["box_success", "box_failure"])
def test_box_fate_conditions_are_dependencies_too(attribute: str) -> None:
    """A box gated by `box_success: s(a)` or `box_failure: f(a)` depends on
    `a` exactly as a start condition would -- the walker covers every
    condition the job carries (SEM-12), and an implementation dropping
    either one fails its row."""

    def estate(cmd: str) -> str:
        return (
            f"insert_job: a\njob_type: c\ncommand: {cmd}\nmachine: m1\n\n"
            f"insert_job: b\njob_type: b\n{attribute}: s(a)\n\n"
            "insert_job: m\njob_type: c\ncommand: x\nmachine: m1\nbox_name: b\n"
        )

    c1, c2 = lower_source(estate("x")), lower_source(estate("CHANGED"))
    carried = CarriedState(jobs={"b": CarriedJob(row=JobRuntime(status="RUNNING", run_number=1))})
    result = classify(closing=Baseline(catalog=c1), opening=Baseline(catalog=c2), carried=carried)
    assert result.by_job["b"].verdict == "R"
    assert "job:a" in result.by_job["b"].changed


def test_reordered_or_duplicated_standard_rows_are_spelling() -> None:
    """A standard calendar compares as the resolver's rows (day -> ticks),
    so reordering and duplicating rows is not a change -- and moving a
    date is."""

    def estate(rows: str) -> str:
        return (
            f"calendar: hols\n{rows}\n"
            "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"
            "date_conditions: 1\nrun_calendar: hols\nstart_mins: 0\n"
        )

    c1 = lower_source(estate("08/19/2026 00:00\n09/01/2026 00:00"))
    c2 = lower_source(estate("09/01/2026 00:00\n08/19/2026 00:00\n08/19/2026 00:00"))
    c3 = lower_source(estate("08/20/2026 00:00\n09/01/2026 00:00"))
    assert (
        "calendar:hols"
        not in ClassificationGraph(Baseline(catalog=c1), Baseline(catalog=c2)).changed
    )
    assert (
        "calendar:hols" in ClassificationGraph(Baseline(catalog=c1), Baseline(catalog=c3)).changed
    )


def test_machine_respellings_that_resolve_identically_are_not_changes() -> None:
    """Type case-folds, node_name unquotes, max_load parses -- the node
    compares what resolution reads, so `08` vs `8` is spelling and `8` vs
    `9` is not."""

    def estate(mtype: str, node: str, load: str) -> str:
        return (
            f"insert_machine: m\ntype: {mtype}\nnode_name: {node}\nmax_load: {load}\n\n"
            "insert_job: j\njob_type: c\ncommand: x\nmachine: m\n"
        )

    c1 = lower_source(estate("a", "host1", "08"))
    c2 = lower_source(estate("A", '"host1"', "8"))
    c3 = lower_source(estate("a", "host1", "9"))
    assert (
        "machine:m" not in ClassificationGraph(Baseline(catalog=c1), Baseline(catalog=c2)).changed
    )
    assert "machine:m" in ClassificationGraph(Baseline(catalog=c1), Baseline(catalog=c3)).changed


def test_a_cycle_description_edit_is_not_a_change() -> None:
    """autocal walks a cycle's PERIODS and nothing else; the attrs map is
    descriptive."""

    def estate(extra: str) -> str:
        return (
            f"cycle: q\nstart_date: 01/01/2026\nend_date: 03/31/2026\n{extra}\n"
            "extended_calendar: cal\ncyccal: q\ncondition: cycle\n\n"
            "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"
            "date_conditions: 1\nrun_calendar: cal\nstart_mins: 0\n"
        )

    c1 = lower_source(estate(""))
    c2 = lower_source(estate("description: touched"))
    graph = ClassificationGraph(Baseline(catalog=c1), Baseline(catalog=c2))
    assert "cycle:q" not in graph.changed
    c3 = lower_source(estate("").replace("end_date: 03/31/2026", "end_date: 04/30/2026"))
    assert "cycle:q" in ClassificationGraph(Baseline(catalog=c1), Baseline(catalog=c3)).changed


# ------------------------------------------------ round-3 review pins (DL-131)


def test_an_exclusion_only_calendar_still_rides_the_timezone_basis() -> None:
    """ss10.2's "or a calendar" includes the exclusion: membership is a
    LOCAL-day question. The fixture's ONLY calendar-ish field is
    `exclude_calendar` -- no start_times, start_mins or run_calendar -- so
    an `is_scheduled` that ignored the exclusion returns False and this
    fails. The verdict half: the job is live, the timezone moves, R."""
    text = (
        "calendar: hols\n08/19/2026 00:00\n\n"
        "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"
        "date_conditions: 1\ndays_of_week: all\n"
        "exclude_calendar: hols\n"
    )
    catalog = lower_source(text)
    c1 = Baseline(catalog=catalog, profile=RuntimeProfile(default_tz="UTC"))
    c2 = Baseline(catalog=catalog, profile=RuntimeProfile(default_tz="Europe/Zurich"))
    graph = ClassificationGraph(c1, c2)
    assert "tz:basis" in graph.forward("job:j")
    assert "tz:basis" in graph.moved("job:j")
    carried = CarriedState(jobs={"j": CarriedJob(row=JobRuntime(status="RUNNING", run_number=1))})
    result = classify(closing=c1, opening=c2, carried=carried)
    assert result.by_job["j"].verdict == "R"


def test_extended_rule_reorder_and_case_are_spelling() -> None:
    """autocal folds rules into include/exclude sets and lowers every token:
    a reordered, re-cased rule list is the same calendar -- and a changed
    rule is not."""

    def estate(conds: str) -> str:
        return (
            f"extended_calendar: cal\nworkday: mo,tu,we,th,fr\n{conds}"
            "\ninsert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"
            "date_conditions: 1\nrun_calendar: cal\nstart_mins: 0\n"
        )

    c1 = lower_source(estate("condition: WORKD#1, MONTHEND\n"))
    # reordered, re-cased, DUPLICATED (the fold is any-of over a set) and
    # respaced around the operator (the tokenizer skips whitespace)
    c2 = lower_source(
        estate("condition: monthend\ncondition: workd#1, MONTHEND\ncondition: monthend\n")
    )
    c3 = lower_source(estate("condition: WORKD#2, MONTHEND\n"))
    assert (
        "calendar:cal"
        not in ClassificationGraph(Baseline(catalog=c1), Baseline(catalog=c2)).changed
    )
    assert "calendar:cal" in ClassificationGraph(Baseline(catalog=c1), Baseline(catalog=c3)).changed


def test_cycle_date_respelling_is_one_date_and_order_still_matters() -> None:
    """`1/1/2026` parses to the same date as `01/01/2026` (autocal's own
    strptime); swapping two periods is a CHANGE -- position numbers the
    chunks cycle-scoped tokens count."""

    def estate(periods: str) -> str:
        return (
            f"cycle: q\n{periods}"
            "\nextended_calendar: cal\ncyccal: q\ncondition: cycle\n\n"
            "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"
            "date_conditions: 1\nrun_calendar: cal\nstart_mins: 0\n"
        )

    a = "start_date: 01/01/2026\nend_date: 03/31/2026\nstart_date: 07/01/2026\nend_date: 09/30/2026\n"
    b = "start_date: 1/1/2026\nend_date: 3/31/2026\nstart_date: 7/1/2026\nend_date: 9/30/2026\n"
    swapped = "start_date: 07/01/2026\nend_date: 09/30/2026\nstart_date: 01/01/2026\nend_date: 03/31/2026\n"
    assert (
        "cycle:q"
        not in ClassificationGraph(
            Baseline(catalog=lower_source(estate(a))), Baseline(catalog=lower_source(estate(b)))
        ).changed
    )
    assert (
        "cycle:q"
        in ClassificationGraph(
            Baseline(catalog=lower_source(estate(a))),
            Baseline(catalog=lower_source(estate(swapped))),
        ).changed
    )


def test_res_type_case_and_duplicate_pool_members_are_spelling() -> None:
    """`res_type: r` and `R` are one renewable policy (capacity upper-cases);
    `[a]` and `[a,a]` are one pool (resolution folds any-of)."""

    def resources(rt: str) -> str:
        return (
            f"insert_resource: fuel\namount: 5\nres_type: {rt}\n\n"
            "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"
            "resources: (fuel, QUANTITY=1)\n"
        )

    assert (
        "resource:fuel"
        not in ClassificationGraph(
            Baseline(catalog=lower_source(resources("r"))),
            Baseline(catalog=lower_source(resources("R"))),
        ).changed
    )

    def pool(members: str) -> str:
        return (
            "insert_machine: la\ntype: a\n\n"
            f"insert_machine: pool\ntype: v\n{members}\n"
            "insert_job: j\njob_type: c\ncommand: x\nmachine: pool\n"
        )

    assert (
        "machine:pool"
        not in ClassificationGraph(
            Baseline(catalog=lower_source(pool("machine: la"))),
            Baseline(catalog=lower_source(pool("machine: la\nmachine: la"))),
        ).changed
    )


def test_rule_whitespace_and_braces_are_the_tokenizers_business() -> None:
    """`MON|TUE` and `MON | TUE` lex to the same tokens, and `{a|b}` groups
    exactly like `(a|b)` (Q9, DL-60) -- the node value comes from autocal's
    own lexer, so none of these spellings is a change. A different token
    still is."""

    def estate(cond: str) -> str:
        return (
            f"extended_calendar: cal\nworkday: mo,tu,we,th,fr\ncondition: {cond}\n"
            "\ninsert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"
            "date_conditions: 1\nrun_calendar: cal\nstart_mins: 0\n"
        )

    tight = lower_source(estate("(MNTHD#7|MNTHD#21)"))
    spaced = lower_source(estate("( MNTHD#7 | MNTHD#21 )"))
    # braces are paren SYNONYMS -- same grouping, different glyphs. A
    # regrouping (`{a} | {b}` vs `(a|b)`) is structure and stays a change,
    # in the safe direction.
    braced = lower_source(estate("{MNTHD#7|MNTHD#21}"))
    other = lower_source(estate("(MNTHD#7|MNTHD#22)"))
    assert (
        "calendar:cal"
        not in ClassificationGraph(Baseline(catalog=tight), Baseline(catalog=spaced)).changed
    )
    assert (
        "calendar:cal"
        not in ClassificationGraph(Baseline(catalog=tight), Baseline(catalog=braced)).changed
    )
    assert (
        "calendar:cal"
        in ClassificationGraph(Baseline(catalog=tight), Baseline(catalog=other)).changed
    )


def test_the_six_attributes_normalize_through_the_engines_own_parsers() -> None:
    """Day-list order and case, action case, quoted references and numeric
    adjust all canonicalize through `autocal.semantic_key` -- and a moved
    day is still a change."""

    def estate(workday: str, holiday: str, adjust: str) -> str:
        return (
            "calendar: hcal\n08/19/2026 00:00\n\n"
            f"extended_calendar: cal\nworkday: {workday}\nholiday: {holiday}\n"
            f'holcal: "hcal"\nadjust: {adjust}\ncondition: WORKD#1\n'
            "\ninsert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"
            "date_conditions: 1\nrun_calendar: cal\nstart_mins: 0\n"
        )

    c1 = lower_source(estate("mo,tu,we", "O", "0"))
    c2 = lower_source(estate("WE,MO,TU", "o", "00").replace('holcal: "hcal"', "holcal: hcal"))
    c3 = lower_source(estate("mo,tu", "O", "0"))
    assert (
        "calendar:cal"
        not in ClassificationGraph(Baseline(catalog=c1), Baseline(catalog=c2)).changed
    )
    assert "calendar:cal" in ClassificationGraph(Baseline(catalog=c1), Baseline(catalog=c3)).changed


# ------------------------------------------------ round-5 review pins (DL-131)


def test_spelling_out_the_engine_defaults_is_not_a_change() -> None:
    """`compile_calendar` derives identical dates from an omitted workday
    (Mon-Fri), an omitted rule list (DAILY) and `holiday: S` (the keep-as-is
    pass-through `_dispose` treats exactly as no action) -- so spelling any
    of them out is one calendar. A REAL default departure still moves it."""

    def estate(cal_block: str) -> str:
        return (
            f"{cal_block}"
            "\ninsert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"
            "date_conditions: 1\nrun_calendar: cal\nstart_mins: 0\n"
        )

    bare = lower_source(estate("extended_calendar: cal\n"))
    spelled = lower_source(
        estate("extended_calendar: cal\nworkday: mo,tu,we,th,fr\nholiday: S\ncondition: daily\n")
    )
    departed = lower_source(estate("extended_calendar: cal\nworkday: mo\n"))
    assert (
        "calendar:cal"
        not in ClassificationGraph(Baseline(catalog=bare), Baseline(catalog=spelled)).changed
    )
    assert (
        "calendar:cal"
        in ClassificationGraph(Baseline(catalog=bare), Baseline(catalog=departed)).changed
    )


def test_word_operators_are_their_symbols() -> None:
    """AND/OR are `&`/`|` to the parser -- the canonical token map, pinned
    for both pairs."""

    def estate(cond: str) -> str:
        return (
            f"extended_calendar: cal\ncondition: {cond}\n"
            "\ninsert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"
            "date_conditions: 1\nrun_calendar: cal\nstart_mins: 0\n"
        )

    for word, symbol in (("tue AND wed", "tue&wed"), ("tue OR wed", "tue|wed")):
        assert (
            "calendar:cal"
            not in ClassificationGraph(
                Baseline(catalog=lower_source(estate(word))),
                Baseline(catalog=lower_source(estate(symbol))),
            ).changed
        ), word


def test_holiday_s_with_a_holcal_is_not_no_action() -> None:
    """`holiday: S` on a holiday keeps the day and skips PAST the
    non_workday branch -- with a holcal and `non_workday: W` it shields a
    weekend holiday from the walk (DL-58's shielding family). So the S
    collapses to no-action only when no holcal exists to hit."""

    def estate(extra: str) -> str:
        return (
            "calendar: hcal\n08/22/2026 00:00\n\n"  # a Saturday
            "extended_calendar: cal\nnon_workday: W\nholcal: hcal\n"
            f"{extra}condition: daily\n"
            "\ninsert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"
            "date_conditions: 1\nrun_calendar: cal\nstart_mins: 0\n"
        )

    without = lower_source(estate(""))
    shielded = lower_source(estate("holiday: S\n"))
    assert (
        "calendar:cal"
        in ClassificationGraph(Baseline(catalog=without), Baseline(catalog=shielded)).changed
    )
    # and with NO holcal, S still equals no action
    bare = lower_source(estate("").replace("non_workday: W\nholcal: hcal\n", ""))
    with_s = lower_source(estate("holiday: S\n").replace("non_workday: W\nholcal: hcal\n", ""))
    assert (
        "calendar:cal"
        not in ClassificationGraph(Baseline(catalog=bare), Baseline(catalog=with_s)).changed
    )
    # and the THIRD contrast: a holcal with NO non_workday action -- the
    # branch S would skip is a no-op, so S is still no action
    idle = lower_source(estate("").replace("non_workday: W\n", ""))
    idle_s = lower_source(estate("holiday: S\n").replace("non_workday: W\n", ""))
    assert (
        "calendar:cal"
        not in ClassificationGraph(Baseline(catalog=idle), Baseline(catalog=idle_s)).changed
    )
    # FIFTH and SIXTH: a non-empty holiday set OUTSIDE the action's domain.
    # W walks non-workdays, so a Monday-only holcal is untouched; O drops
    # workdays, so a Saturday-only holcal is untouched -- either way the
    # skip shields nothing and S is no action.
    monday = lower_source(
        estate("holiday: S\n").replace("08/22/2026 00:00", "08/24/2026 00:00")  # a Monday
    )
    monday_plain = lower_source(estate("").replace("08/22/2026 00:00", "08/24/2026 00:00"))
    assert (
        "calendar:cal"
        not in ClassificationGraph(Baseline(catalog=monday_plain), Baseline(catalog=monday)).changed
    )
    saturday_o = lower_source(estate("").replace("non_workday: W", "non_workday: O"))
    saturday_o_s = lower_source(estate("holiday: S\n").replace("non_workday: W", "non_workday: O"))
    assert (
        "calendar:cal"
        not in ClassificationGraph(
            Baseline(catalog=saturday_o), Baseline(catalog=saturday_o_s)
        ).changed
    )
    # SEVENTH: a holiday the RULES never admit -- Monday holcal, O (which
    # can alter Mondays), but the rule set includes only Tuesdays: neither
    # side ever produces the Monday as a candidate, so S is no action
    tue_only = lower_source(
        estate("")
        .replace("08/22/2026 00:00", "08/24/2026 00:00")
        .replace("non_workday: W", "non_workday: O")
        .replace("condition: daily", "condition: tue")
    )
    tue_only_s = lower_source(
        estate("holiday: S\n")
        .replace("08/22/2026 00:00", "08/24/2026 00:00")
        .replace("non_workday: W", "non_workday: O")
        .replace("condition: daily", "condition: tue")
    )
    assert (
        "calendar:cal"
        not in ClassificationGraph(Baseline(catalog=tue_only), Baseline(catalog=tue_only_s)).changed
    )
    # and the domains DO reach when they should: Monday-holcal under O
    monday_o = lower_source(
        estate("")
        .replace("08/22/2026 00:00", "08/24/2026 00:00")
        .replace("non_workday: W", "non_workday: O")
    )
    monday_o_s = lower_source(
        estate("holiday: S\n")
        .replace("08/22/2026 00:00", "08/24/2026 00:00")
        .replace("non_workday: W", "non_workday: O")
    )
    assert (
        "calendar:cal"
        in ClassificationGraph(Baseline(catalog=monday_o), Baseline(catalog=monday_o_s)).changed
    )
    # and the FOURTH: the holcal is present but EMPTY -- S has nothing to
    # keep, the compiled dates are identical, so S is still no action
    empty = lower_source(
        estate("").replace("calendar: hcal\n08/22/2026 00:00\n", "calendar: hcal\n")
    )
    empty_s = lower_source(
        estate("holiday: S\n").replace("calendar: hcal\n08/22/2026 00:00\n", "calendar: hcal\n")
    )
    assert (
        "calendar:cal"
        not in ClassificationGraph(Baseline(catalog=empty), Baseline(catalog=empty_s)).changed
    )
