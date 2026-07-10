"""UC twin trace tests: the P-Mxx expected-divergence pairs (phase 9+).

Normative spec: docs/stonebranch-semantics.md UCS-01/02/03/09/13 + Part IV
(the P-Mxx pair definitions -- "the honest core of the whole project");
src/dsl41/uc_oracle.py's module docstring pins every interpreter decision
(recorded as DL-16); src/dsl41/backend_uc.py's compile_twin docstring pins
the lowering choices exercised in section 2. docs/decision-log.md DL-16
(this suite) + DL-13 (edge-triggered re-evaluation and the schedule double
gate that P-M07's AutoSys side leans on).

Every expected outcome here was verified empirically against BOTH engines
before the assertion was written (CLAUDE.md: fidelity is tested, not
asserted). Two recurring, DOCUMENTED (not buggy) wrinkles surfaced during
that verification and are cited inline wherever they matter instead of
being folded silently into the interesting assertions:

- STARTJOB/TIMER on a task launches its whole CONTAINING WORKFLOW (every
  task resets to Waiting, then every source task starts) -- per
  uc_oracle.py's own docstring. A script that names only ONE task can
  therefore restart or auto-start sibling source tasks the script never
  mentions. Noted where it changes what a job_outcomes() comparison shows.
- A box's own name is never one of its own compiled workflow's tasks
  (compile_twin names the WORKFLOW after the box; the box itself is not a
  member of it), so STARTJOB(the box name) is a NO_WORKFLOW no-op on the UC
  side. A fair box-vs-workflow script fires the box name for AutoSys and a
  member name for UC; see the section-4 box-fold test.

No source bugs were found; every divergence pinned below matches a mapping-
table A/R row or a UCS entry the module docstrings already call out.

Section map:
  1. UCS unit tests: the twin interpreter alone (hand-built UcModel).
  2. compile_twin lowering: JIL -> UcModel shape assertions.
  3. The P-Mxx pairs: run_both + first_divergence (the point of the file).
  4. Convergence sanity: the comparator does not cry wolf on faithful shapes.
  5. Comparator unit tests: normalize_transition / job_outcomes / first_divergence.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from dsl41.ast_jil import parse_file
from dsl41.backend_uc import UcEdge, UcModel, UcVarCondition, UcWorkflow, compile_twin
from dsl41.ir import lower_catalog, lower_source
from dsl41.oracle import Event, EventKind, Oracle, TraceEntry
from dsl41.uc_oracle import (
    Divergence,
    UcOracle,
    UcOracleError,
    first_divergence,
    job_outcomes,
    normalize_transition,
)

CORPUS_DIR = Path(__file__).parent / "corpus"

T0 = datetime(2026, 7, 1, 8, 0)


def ev(kind: EventKind, minutes: float = 0.0, **payload: object) -> Event:
    return Event(at=T0 + timedelta(minutes=minutes), kind=kind, payload=payload)


def transitions(trace: list[TraceEntry], job: str) -> list[str]:
    return [t.transition for t in trace if t.job == job]


def run_both(jil: str, script: list[Event]) -> tuple[list[TraceEntry], list[TraceEntry]]:
    """Lower `jil` once, run `script` through the AutoSys oracle and the UC
    twin (Oracle(catalog) / UcOracle(compile_twin(catalog))), and return
    (autosys_trace, uc_trace)."""
    catalog = lower_source(jil)
    autosys_trace = Oracle(catalog).run_script(script)
    uc_trace = UcOracle(compile_twin(catalog)).run_script(script)
    return autosys_trace, uc_trace


def make_model(
    tasks: list[str],
    edges: list[UcEdge] | None = None,
    mutex_groups: list[list[str]] | None = None,
    max_exit_success: dict[str, int] | None = None,
    success_codes: dict[str, list[tuple[int, int]]] | None = None,
    fail_codes: dict[str, list[tuple[int, int]]] | None = None,
    name: str = "wf",
) -> UcModel:
    """One-workflow UcModel builder for section-1 unit tests of the twin
    interpreter ALONE -- bypassing compile_twin entirely (lowering is
    section 2's concern)."""
    return UcModel(
        workflows=[UcWorkflow(name=name, tasks=list(tasks), edges=list(edges or []))],
        mutex_groups=[list(g) for g in (mutex_groups or [])],
        max_exit_success=dict(max_exit_success or {}),
        success_codes=dict(success_codes or {}),
        fail_codes=dict(fail_codes or {}),
    )


# ===================================================== 1. UCS unit tests: the twin alone


def test_instance_launch_starts_every_source_task_not_just_the_named_one() -> None:
    """DL-16: STARTJOB/TIMER on a task launches its CONTAINING WORKFLOW, not
    just that task -- every source task (no incoming edges) starts, even
    when the fed job is itself a non-source. STARTJOB targets 'b' (a
    non-source with an incoming edge from 'a'); 'a' and 'x' (both sources)
    start anyway, and 'b' stays Waiting until 'a' completes."""
    model = make_model(
        ["a", "x", "b"], [UcEdge(src="a", dst="b", condition="success", mapping_row="TEST")]
    )
    o = UcOracle(model)
    o.feed(ev("STARTJOB", 0, job="b"))
    assert transitions(o.trace(), "a") == ["Waiting->Running"]
    assert transitions(o.trace(), "x") == ["Waiting->Running"]
    assert transitions(o.trace(), "b") == []


@pytest.mark.parametrize(
    ("edge_condition", "producer_status", "should_run"),
    [
        ("success", "SUCCESS", True),
        ("success", "FAILURE", False),
        ("success", "TERMINATED", False),
        ("failure", "SUCCESS", False),
        ("failure", "FAILURE", True),
        ("failure", "TERMINATED", False),  # review M-1: Cancelled is NOT Failed
        ("cancelled", "SUCCESS", False),
        ("cancelled", "FAILURE", False),
        ("cancelled", "TERMINATED", True),
        ("done", "SUCCESS", True),
        ("done", "FAILURE", True),
        ("done", "TERMINATED", True),
    ],
)
def test_ucs01_edge_condition_truth_table(
    edge_condition: str, producer_status: str, should_run: bool
) -> None:
    """UCS-01/M06 (review M-1): an edge's condition is matched against the
    predecessor's terminal status at completion -- done matches all three,
    but UC separates Cancelled from Failed: a failure edge stays unmatched
    on Cancelled (so M04's f() keeps its EXACT class), and the `cancelled`
    condition carries the t() mapping."""
    model = make_model(
        ["p", "c"],
        [UcEdge(src="p", dst="c", condition=edge_condition, mapping_row="TEST")],  # type: ignore[arg-type]
    )
    o = UcOracle(model)
    o.feed(ev("STARTJOB", 0, job="p"))
    o.feed(ev("STATUS", 1, job="p", status=producer_status))
    expected = ["Waiting->Running"] if should_run else ["Waiting->Skipped"]
    assert transitions(o.trace(), "c") == expected


def test_ucs02_skip_cascades_through_a_chain() -> None:
    """UCS-02: a failed edge match on the sole incoming edge Skips the
    successor, and that Skip propagates as a further all-skipped cascade to
    ITS successor too."""
    model = make_model(
        ["p", "c1", "c2"],
        [
            UcEdge(src="p", dst="c1", condition="success", mapping_row="TEST"),
            UcEdge(src="c1", dst="c2", condition="success", mapping_row="TEST"),
        ],
    )
    o = UcOracle(model)
    o.feed(ev("STARTJOB", 0, job="p"))
    o.feed(ev("STATUS", 1, job="p", status="FAILURE"))
    assert transitions(o.trace(), "c1") == ["Waiting->Skipped"]
    assert transitions(o.trace(), "c2") == ["Waiting->Skipped"]


def test_ucs02_all_predecessors_skipped_skips_the_task() -> None:
    """UCS-02 verbatim: 'If ALL immediate predecessors ... are Skipped -> the
    task is Skipped.' Two producers both mismatch their edge; the consumer
    stays unresolved (Waiting, no trace entry) until BOTH have resolved."""
    model = make_model(
        ["p1", "p2", "c"],
        [
            UcEdge(src="p1", dst="c", condition="success", mapping_row="TEST"),
            UcEdge(src="p2", dst="c", condition="success", mapping_row="TEST"),
        ],
    )
    o = UcOracle(model)
    o.feed(ev("STARTJOB", 0, job="p1"))
    o.feed(ev("STATUS", 1, job="p1", status="FAILURE"))
    assert transitions(o.trace(), "c") == []  # p2's edge still pending
    o.feed(ev("STATUS", 2, job="p2", status="FAILURE"))
    assert transitions(o.trace(), "c") == ["Waiting->Skipped"]


def test_ucs02_satisfied_predecessor_completing_after_the_skipped_one_still_runs() -> None:
    """UCS-02 verbatim: '>= 1 satisfied edge and none pending -> it runs --
    skipped predecessors do not block.' p2's edge skips first; p1's edge
    satisfies afterward and the consumer runs anyway."""
    model = make_model(
        ["p1", "p2", "c"],
        [
            UcEdge(src="p1", dst="c", condition="success", mapping_row="TEST"),
            UcEdge(src="p2", dst="c", condition="success", mapping_row="TEST"),
        ],
    )
    o = UcOracle(model)
    o.feed(ev("STARTJOB", 0, job="p1"))  # p1 and p2 are both sources: both start
    o.feed(ev("STATUS", 1, job="p2", status="FAILURE"))
    assert transitions(o.trace(), "c") == []
    o.feed(ev("STATUS", 2, job="p1", status="SUCCESS"))
    assert transitions(o.trace(), "c") == ["Waiting->Running"]


def test_ucs02_satisfied_predecessor_completing_before_the_skipped_one_still_runs() -> None:
    """Same claim, opposite completion order: the satisfied edge resolves
    first, the skipped one second -- order does not matter to UCS-02."""
    model = make_model(
        ["p1", "p2", "c"],
        [
            UcEdge(src="p1", dst="c", condition="success", mapping_row="TEST"),
            UcEdge(src="p2", dst="c", condition="success", mapping_row="TEST"),
        ],
    )
    o = UcOracle(model)
    o.feed(ev("STARTJOB", 0, job="p1"))
    o.feed(ev("STATUS", 1, job="p1", status="SUCCESS"))
    assert transitions(o.trace(), "c") == []  # p2 still pending
    o.feed(ev("STATUS", 2, job="p2", status="FAILURE"))
    assert transitions(o.trace(), "c") == ["Waiting->Running"]


def test_var_condition_false_at_completion_skips_and_set_global_after_does_not_revive() -> None:
    """UCS-01 timing (the M09 divergence, DL-16): a variable condition is
    read WHEN THE PREDECESSOR COMPLETES, never re-checked on a later
    SET_GLOBAL. G is unset at p's completion -> even though the edge's own
    success/failure condition matches, the WHOLE edge is unsatisfied ->
    Skipped; setting G afterward does not revive the already-Skipped path."""
    var_cond = UcVarCondition(name="G", op="=", value="go")
    model = make_model(
        ["p", "c"],
        [UcEdge(src="p", dst="c", condition="success", var_condition=var_cond, mapping_row="TEST")],
    )
    o = UcOracle(model)
    o.feed(ev("STARTJOB", 0, job="p"))
    o.feed(ev("STATUS", 1, job="p", status="SUCCESS"))
    assert transitions(o.trace(), "c") == ["Waiting->Skipped"]
    o.feed(ev("SET_GLOBAL", 2, name="G", value="go"))
    assert transitions(o.trace(), "c") == ["Waiting->Skipped"]  # unchanged: no revival


def test_var_condition_true_when_set_before_predecessor_completes() -> None:
    """Contrast: G is set before p completes, so it IS true at the
    completion-time evaluation and the edge is satisfied."""
    var_cond = UcVarCondition(name="G", op="=", value="go")
    model = make_model(
        ["p", "c"],
        [UcEdge(src="p", dst="c", condition="success", var_condition=var_cond, mapping_row="TEST")],
    )
    o = UcOracle(model)
    o.feed(ev("SET_GLOBAL", 0, name="G", value="go"))
    o.feed(ev("STARTJOB", 1, job="p"))
    o.feed(ev("STATUS", 2, job="p", status="SUCCESS"))
    assert transitions(o.trace(), "c") == ["Waiting->Running"]


def test_ucs09_mutex_exclusive_wait_and_fifo_release() -> None:
    """UCS-09: a 3-way Mutually Exclusive Tasks group -- the second and
    third eligible tasks queue in Exclusive Wait; releases are FIFO, one at
    a time, as the running partner completes (never two at once, since the
    freshly-released task is itself a blocker for the next)."""
    model = make_model(["a", "b", "cc"], mutex_groups=[["a", "b", "cc"]])
    o = UcOracle(model)
    o.feed(ev("STARTJOB", 0, job="a"))
    assert transitions(o.trace(), "a") == ["Waiting->Running"]
    assert transitions(o.trace(), "b") == ["Waiting->ExclusiveWait"]
    assert transitions(o.trace(), "cc") == ["Waiting->ExclusiveWait"]
    o.feed(ev("STATUS", 1, job="a", status="SUCCESS"))
    assert transitions(o.trace(), "b") == ["Waiting->ExclusiveWait", "ExclusiveWait->Running"]
    assert transitions(o.trace(), "cc") == ["Waiting->ExclusiveWait"]  # still queued
    o.feed(ev("STATUS", 2, job="b", status="SUCCESS"))
    assert transitions(o.trace(), "cc") == ["Waiting->ExclusiveWait", "ExclusiveWait->Running"]


def test_ucs09_self_exclusion_never_manifests_given_one_instance_per_workflow_v1() -> None:
    """A singleton mutex group ['s'] makes 's' its own partner
    (backend_uc's constructor: partners = set(group) - {task} or {task}).
    Empirically pinning the ACTUAL v1 behavior rather than assuming: self-
    exclusion never shows up as an ExclusiveWait, because the coarser 'one
    open instance per workflow' gate (UCS-09 Instance Wait) always
    intercepts a concurrent launch attempt FIRST -- a second STARTJOB is
    ignored (INSTANCE_OPEN) before the mutex check is ever reached again.
    Not a bug: the self-exclusion bookkeeping is harmless scaffolding for a
    future multi-instance model."""
    model = make_model(["s"], mutex_groups=[["s"]])
    o = UcOracle(model)
    o.feed(ev("STARTJOB", 0, job="s"))
    assert transitions(o.trace(), "s") == ["Waiting->Running"]
    o.feed(ev("STARTJOB", 1, job="s"))
    assert transitions(o.trace(), "s") == ["Waiting->Running"]  # unchanged: never ExclusiveWait
    assert transitions(o.trace(), "wf")[-1] == "INSTANCE_OPEN"


def test_m19_ice_before_launch_is_skip_at_start_and_cascades() -> None:
    """M19/UCS-02: ON_ICE marks a task Skip-AT-START; when the workflow
    launches, the iced source Skips immediately and the skip cascades to
    its successor's successor too."""
    model = make_model(
        ["p", "c1", "c2"],
        [
            UcEdge(src="p", dst="c1", condition="success", mapping_row="TEST"),
            UcEdge(src="c1", dst="c2", condition="success", mapping_row="TEST"),
        ],
    )
    o = UcOracle(model)
    o.feed(ev("ON_ICE", 0, job="p"))
    o.feed(ev("STARTJOB", 1, job="p"))
    assert transitions(o.trace(), "p")[-1] == "Waiting->Skipped"
    assert transitions(o.trace(), "c1") == ["Waiting->Skipped"]
    assert transitions(o.trace(), "c2") == ["Waiting->Skipped"]


def test_m19_contrast_one_iced_predecessor_does_not_block_the_other() -> None:
    """Contrast case (M19 mapping-table note): skipped predecessors do not
    block (UCS-02) -- one iced source Skips, the other REAL source runs and
    satisfies its edge, and the consumer runs."""
    model = make_model(
        ["p1", "p2", "c"],
        [
            UcEdge(src="p1", dst="c", condition="success", mapping_row="TEST"),
            UcEdge(src="p2", dst="c", condition="success", mapping_row="TEST"),
        ],
    )
    o = UcOracle(model)
    o.feed(ev("ON_ICE", 0, job="p1"))
    o.feed(ev("STARTJOB", 1, job="p2"))
    assert transitions(o.trace(), "p1")[-1] == "Waiting->Skipped"
    assert transitions(o.trace(), "p2") == ["Waiting->Running"]
    o.feed(ev("STATUS", 2, job="p2", status="SUCCESS"))
    assert transitions(o.trace(), "c") == ["Waiting->Running"]


def test_m20_hold_blocks_instance_completion_off_hold_starts_and_closes() -> None:
    """M20: a Held task blocks the instance from completing even once every
    OTHER task is terminal; OFF_HOLD starts it (it is a source, so its
    'edges' were trivially already eligible) and the instance then closes."""
    model = make_model(["h", "other"])
    o = UcOracle(model)
    o.feed(ev("ON_HOLD", 0, job="h"))
    o.feed(ev("STARTJOB", 1, job="other"))
    assert transitions(o.trace(), "h") == ["HELD", "Waiting->Held"]
    assert transitions(o.trace(), "other") == ["Waiting->Running"]
    o.feed(ev("STATUS", 2, job="other", status="SUCCESS"))
    assert transitions(o.trace(), "wf") == ["INSTANCE->Running"]  # not closed: h still Held
    o.feed(ev("OFF_HOLD", 3, job="h"))
    assert transitions(o.trace(), "h")[-1] == "Waiting->Running"
    o.feed(ev("STATUS", 4, job="h", status="SUCCESS"))
    assert transitions(o.trace(), "wf") == ["INSTANCE->Running", "INSTANCE->Success"]


def test_m23_killjob_cancels_and_only_the_cancelled_edge_matches() -> None:
    """M23 + review M-1: KILLJOB completes a Running task as Cancelled; a
    failure-condition edge does NOT match Cancelled (UC separates them,
    UCS-01/M06) -- only a `cancelled` edge does."""
    model = make_model(
        ["p", "cf", "cc"],
        [
            UcEdge(src="p", dst="cf", condition="failure", mapping_row="TEST"),
            UcEdge(src="p", dst="cc", condition="cancelled", mapping_row="TEST"),
        ],
    )
    o = UcOracle(model)
    o.feed(ev("STARTJOB", 0, job="p"))
    o.feed(ev("KILLJOB", 1, job="p"))
    assert transitions(o.trace(), "p") == ["Waiting->Running", "Running->Cancelled"]
    assert transitions(o.trace(), "cf") == ["Waiting->Skipped"]  # failure != cancelled
    assert transitions(o.trace(), "cc") == ["Waiting->Running"]


def test_m22_force_startjob_launches_then_forces_and_forces_within_open_instance() -> None:
    """M22 + review E-1: FORCE_STARTJOB with no open instance launches the
    containing workflow first (UC's Launch-task analog), then clears the
    task's dependencies; inside an already-open instance it starts the task
    regardless of its unmet edge."""
    model = make_model(
        ["p", "q", "h"], [UcEdge(src="q", dst="h", condition="success", mapping_row="TEST")]
    )
    o = UcOracle(model)
    o.feed(ev("FORCE_STARTJOB", 0, job="h"))
    # instance opened by the force; h started despite its unmet q-edge
    assert transitions(o.trace(), "h") == ["FORCED->Running"]
    assert transitions(o.trace(), "wf")[0] == "INSTANCE->Running"
    o2 = UcOracle(model)
    o2.feed(ev("STARTJOB", 1, job="p"))
    assert transitions(o2.trace(), "h") == []  # waiting on q
    o2.feed(ev("FORCE_STARTJOB", 2, job="h"))
    assert transitions(o2.trace(), "h") == ["FORCED->Running"]


def test_status_on_non_running_task_is_ignored_with_a_marker() -> None:
    """STATUS is ignored -- with a STATUS_IGNORED trace marker, never an
    exception -- both when no instance is open at all and when the task is
    merely still Waiting inside an open one."""
    model = make_model(
        ["p", "c"], [UcEdge(src="p", dst="c", condition="success", mapping_row="TEST")]
    )
    o = UcOracle(model)
    o.feed(ev("STATUS", 0, job="c", status="SUCCESS"))  # no instance exists yet
    assert transitions(o.trace(), "c") == ["STATUS_IGNORED"]
    o.feed(ev("STARTJOB", 1, job="p"))
    o.feed(ev("STATUS", 2, job="c", status="SUCCESS"))  # c still Waiting: p hasn't completed
    assert transitions(o.trace(), "c") == ["STATUS_IGNORED", "STATUS_IGNORED"]


def test_second_startjob_while_instance_open_is_ignored() -> None:
    """A STARTJOB while the workflow's instance is still open is ignored
    (UCS-09 Instance Wait territory) and recorded as an INSTANCE_OPEN
    marker; the already-running task is untouched (no reset, no second
    Waiting->Running)."""
    model = make_model(["p"])
    o = UcOracle(model)
    o.feed(ev("STARTJOB", 0, job="p"))
    o.feed(ev("STARTJOB", 1, job="p"))
    assert transitions(o.trace(), "p") == ["Waiting->Running"]
    assert transitions(o.trace(), "wf") == ["INSTANCE->Running", "INSTANCE_OPEN"]


def test_workflow_instance_success_and_failed_trace_entries() -> None:
    """UCS-04 approximation (DL-16): the workflow itself gets exactly one
    trace entry once every task is terminal -- Success iff none
    Failed/Cancelled, else Failed."""
    ok_model = make_model(
        ["p", "c"],
        [UcEdge(src="p", dst="c", condition="success", mapping_row="TEST")],
        name="wf_ok",
    )
    o = UcOracle(ok_model)
    o.feed(ev("STARTJOB", 0, job="p"))
    o.feed(ev("STATUS", 1, job="p", status="SUCCESS"))
    o.feed(ev("STATUS", 2, job="c", status="SUCCESS"))
    assert transitions(o.trace(), "wf_ok")[-1] == "INSTANCE->Success"

    bad_model = make_model(
        ["p2", "c2"],
        [UcEdge(src="p2", dst="c2", condition="failure", mapping_row="TEST")],
        name="wf_bad",
    )
    o2 = UcOracle(bad_model)
    o2.feed(ev("STARTJOB", 0, job="p2"))
    o2.feed(ev("STATUS", 1, job="p2", status="FAILURE"))
    o2.feed(ev("STATUS", 2, job="c2", status="SUCCESS"))
    assert transitions(o2.trace(), "wf_bad")[-1] == "INSTANCE->Failed"


@pytest.mark.parametrize(
    ("exit_code", "ceiling", "expected"),
    [
        (0, None, "Success"),
        (1, None, "Failed"),
        (2, 2, "Success"),
        (3, 2, "Failed"),
        (5, 2, "Failed"),
    ],
    ids=["default-ceiling-0-ok", "default-ceiling-0-over", "at-ceiling", "ceiling+1", "well-over"],
)
def test_m31_exit_code_boundary_via_max_exit_success(
    exit_code: int, ceiling: int | None, expected: str
) -> None:
    """M31/U4 default: an integer exit_code (no explicit status) is judged
    against max_exit_success (default 0 when the task has no entry) -- the
    same boundary reading as the AutoSys oracle."""
    model = make_model(["p"], max_exit_success=({"p": ceiling} if ceiling is not None else {}))
    o = UcOracle(model)
    o.feed(ev("STARTJOB", 0, job="p"))
    o.feed(ev("STATUS", 1, job="p", exit_code=exit_code))
    assert transitions(o.trace(), "p") == ["Waiting->Running", f"Running->{expected}"]


@pytest.mark.parametrize(
    ("exit_code", "expected"),
    [(5, "Failed"), (25, "Success"), (0, "Failed"), (31, "Failed")],
    ids=["fail-code-wins", "in-success-range", "zero-not-listed", "outside-range"],
)
def test_m31_explicit_code_sets_share_the_autosys_verdict(exit_code: int, expected: str) -> None:
    """M31/DL-33: success_codes/fail_codes ride the same same-boundary
    assumption (U4) -- the twin judges through ir.exit_is_success, so the
    Q7 corner defaults (fail-wins, replacement, threshold-ignored) match
    the AutoSys oracle by construction."""
    model = make_model(
        ["p"],
        max_exit_success={"p": 2},
        success_codes={"p": [(20, 30)]},
        fail_codes={"p": [(5, 5)]},
    )
    o = UcOracle(model)
    o.feed(ev("STARTJOB", 0, job="p"))
    o.feed(ev("STATUS", 1, job="p", exit_code=exit_code))
    assert transitions(o.trace(), "p") == ["Waiting->Running", f"Running->{expected}"]


def test_error_feed_time_going_backwards_raises() -> None:
    o = UcOracle(make_model(["p"]))
    o.feed(ev("STATUS", 5, job="p", status="SUCCESS"))
    with pytest.raises(UcOracleError, match="backwards"):
        o.feed(ev("STATUS", 0, job="p", status="SUCCESS"))


def test_error_set_global_without_name_raises() -> None:
    o = UcOracle(make_model(["p"]))
    with pytest.raises(UcOracleError, match="SET_GLOBAL requires payload.name"):
        o.feed(Event(at=T0, kind="SET_GLOBAL", payload={"value": "x"}))


def test_error_unmappable_status_raises() -> None:
    o = UcOracle(make_model(["p"]))
    o.feed(ev("STARTJOB", 0, job="p"))
    with pytest.raises(UcOracleError, match="unmappable status"):
        o.feed(ev("STATUS", 1, job="p", status="BOGUS"))


def test_error_uninjectable_event_kind_raises() -> None:
    """MUST_START_ALARM is a valid shared EventKind (the AutoSys oracle
    emits it) but the UC twin has no handling for it at all."""
    o = UcOracle(make_model(["p"]))
    with pytest.raises(UcOracleError, match="uninjectable"):
        o.feed(Event(at=T0, kind="MUST_START_ALARM", payload={}))


def test_error_status_without_job_raises() -> None:
    o = UcOracle(make_model(["p"]))
    with pytest.raises(UcOracleError, match="requires payload.job"):
        o.feed(Event(at=T0, kind="STATUS", payload={"status": "SUCCESS"}))


# ===================================================== 2. compile_twin lowering


def test_boxes_become_a_workflow_with_nested_boxes_flattened() -> None:
    """M13/M18 (compile_twin docstring): nested boxes flatten into the
    top-level box's task set; neither box job name appears as a task."""
    text = (
        "insert_job: outer_box\njob_type: b\n\n"
        "insert_job: inner_box\njob_type: b\nbox_name: outer_box\n\n"
        "insert_job: leaf1\njob_type: c\ncommand: x\nmachine: m1\nbox_name: inner_box\n\n"
        "insert_job: leaf2\njob_type: c\ncommand: y\nmachine: m1\nbox_name: outer_box\n"
    )
    model = compile_twin(lower_source(text))
    (wf,) = model.workflows
    assert wf.name == "outer_box"
    assert set(wf.tasks) == {"leaf1", "leaf2"}
    assert "inner_box" not in wf.tasks
    assert "outer_box" not in wf.tasks
    assert wf.edges == []


def test_standalone_connected_jobs_form_one_component_workflow() -> None:
    """Unboxed jobs connected by a compiled edge group into one workflow
    named 'wf_<first-task-in-catalog-order>'."""
    text = (
        "insert_job: p2b\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: c2b\njob_type: c\ncommand: y\nmachine: m1\ncondition: f(p2b)\n"
    )
    model = compile_twin(lower_source(text))
    (wf,) = model.workflows
    assert wf.name == "wf_p2b"
    assert wf.tasks == ["p2b", "c2b"]
    assert [(e.src, e.dst) for e in wf.edges] == [("p2b", "c2b")]


def test_isolated_job_becomes_a_singleton_workflow() -> None:
    text = "insert_job: lonely\njob_type: c\ncommand: z\nmachine: m1\n"
    model = compile_twin(lower_source(text))
    (wf,) = model.workflows
    assert wf.name == "wf_lonely"
    assert wf.tasks == ["lonely"]
    assert wf.edges == []


def test_r_classified_edge_is_excluded_and_ledgered() -> None:
    """Part II requirement 1 (compile_twin docstring): R rows never
    compile silently; they are excluded and recorded. Cross-instance
    s(prod^PRD) is M33/R (SEM-07: consolidating instances is a migration
    design decision, not a translation)."""
    text = "insert_job: cons_r\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(prod^PRD)\n"
    model = compile_twin(lower_source(text))
    assert model.excluded == ["M33 edge prod^PRD -> cons_r (R-class)"]
    assert all(not wf.edges for wf in model.workflows)


def test_run_window_job_is_excluded_with_an_m27_ledger_entry() -> None:
    """M27: run_window has no UC analog at all (compile_twin docstring); the
    job is still modeled (it can still run), just without the window."""
    text = (
        "insert_job: rw_job\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "02:00"\nrun_window: "02:00-04:00"\n'
    )
    model = compile_twin(lower_source(text))
    assert any(e.startswith("M27 rw_job:") for e in model.excluded)
    (wf,) = model.workflows
    assert wf.tasks == ["rw_job"]


def test_lookback_qualified_notrunning_edge_is_excluded() -> None:
    """n() WITH a lookback stays an edge at the derive layer (M03, DL-12,
    distinct from the bare-n() mutex reading) but compile_twin excludes it:
    no UC edge condition reads 'not running'."""
    text = (
        "insert_job: nr_prod\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: nr_cons\njob_type: c\ncommand: y\nmachine: m1\ncondition: n(nr_prod, 01.00)\n"
    )
    model = compile_twin(lower_source(text))
    assert model.excluded == ["M03 edge nr_prod -> nr_cons (notrunning has no UC edge condition)"]
    assert all(not wf.edges for wf in model.workflows)


def test_terminated_via_folds_to_a_cancelled_condition_edge() -> None:
    """M06 + review M-1: t() (TERMINATED) compiles to the `cancelled` edge
    condition -- UC separates Cancelled from Failed, so folding t() into
    `failure` would make f() fire on kills and break M04's EXACT class."""
    text = (
        "insert_job: t_prod\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: t_cons\njob_type: c\ncommand: y\nmachine: m1\ncondition: t(t_prod)\n"
    )
    model = compile_twin(lower_source(text))
    (wf,) = model.workflows
    (edge,) = wf.edges
    assert (edge.src, edge.dst, edge.condition) == ("t_prod", "t_cons", "cancelled")


def test_exitcode_atom_becomes_a_var_condition_on_exit_pseudo_variable() -> None:
    """M08/U4 default: e(prod) op k becomes a 'done' edge carrying a
    var_condition on the 'exit:<task>' pseudo-variable."""
    text = (
        "insert_job: e_prod\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: e_cons\njob_type: c\ncommand: y\nmachine: m1\ncondition: e(e_prod) = 5\n"
    )
    model = compile_twin(lower_source(text))
    (wf,) = model.workflows
    (edge,) = wf.edges
    assert edge.condition == "done"
    assert edge.var_condition == UcVarCondition(name="exit:e_prod", op="=", value="5")


def test_global_atom_attaches_var_condition_to_the_predecessor_edge() -> None:
    """M09: a global atom alongside a real predecessor in the SAME
    condition attaches as a var_condition on that predecessor's compiled
    edge -- a UC edge cannot exist without a predecessor vertex (UCS-01),
    which is exactly why bare async global gates are M09/R-adjacent."""
    text = (
        "insert_job: g_prod\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: g_cons\njob_type: c\ncommand: y\nmachine: m1\n"
        "condition: s(g_prod) & v(GATE) = go\n"
    )
    model = compile_twin(lower_source(text))
    (wf,) = model.workflows
    (edge,) = wf.edges
    assert (edge.src, edge.dst, edge.condition) == ("g_prod", "g_cons", "success")
    assert edge.var_condition == UcVarCondition(name="GATE", op="=", value="go")
    assert model.excluded == []


def test_global_gate_with_no_predecessor_edge_is_excluded() -> None:
    """A consumer whose condition is JUST a global atom has no compiled
    predecessor edge to attach the var_condition to -- excluded, ledgered
    (UCS-01)."""
    text = "insert_job: g_cons2\njob_type: c\ncommand: y\nmachine: m1\ncondition: v(GATE2) = go\n"
    model = compile_twin(lower_source(text))
    assert model.excluded == [
        "M09 global gate $GATE2 -> g_cons2 (consumer has no compiled predecessor edge;"
        " async global gates need a redesign, UCS-01)"
    ]
    assert all(not wf.edges for wf in model.workflows)


def test_mutex_groups_pass_through_unchanged() -> None:
    """M07 (UCS-09): derive's mutex-pair detector output passes straight
    through to UcModel.mutex_groups, reusing the m07_mutex.jil corpus
    fixture (also exercised by test_derive.py's own M07 assertions)."""
    catalog = lower_catalog([parse_file(CORPUS_DIR / "m07_mutex.jil")])
    model = compile_twin(catalog)
    assert model.mutex_groups == [["mutex_a", "mutex_b"], ["mutex_serial"]]


def test_or_shape_presence_adds_a_u1_gated_ledger_note() -> None:
    """M12: whenever an Or node is present anywhere in the catalog, a single
    U1-gated ledger note is added (irrespective of how many OR shapes)."""
    text = (
        "insert_job: or_p1\njob_type: c\ncommand: a\nmachine: m1\n\n"
        "insert_job: or_p2\njob_type: c\ncommand: b\nmachine: m1\n\n"
        "insert_job: or_cons\njob_type: c\ncommand: c\nmachine: m1\ncondition: s(or_p1) | s(or_p2)\n"
    )
    model = compile_twin(lower_source(text))
    assert model.excluded == [
        "M12 OR shapes present: duplicate-successor join semantics apply"
        " (UCS-03); alternative lowerings are U1-gated"
    ]


def test_cross_workflow_edge_member_to_outside_task_is_excluded() -> None:
    """A box MEMBER's edge to a task OUTSIDE the box spans two different
    compiled workflows (the box's workflow + the outside task's own
    component) -- excluded, ledgered as Task Monitor territory, not modeled
    v1."""
    text = (
        "insert_job: box_x\njob_type: b\n\n"
        "insert_job: member_in\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box_x\n\n"
        "insert_job: outside_job\njob_type: c\ncommand: y\nmachine: m1\ncondition: f(member_in)\n"
    )
    model = compile_twin(lower_source(text))
    assert model.excluded == [
        "M04 edge member_in -> outside_job spans workflows"
        " (Task Monitor territory, M02/M03; not modeled v1)"
    ]
    for wf in model.workflows:
        assert ("member_in", "outside_job") not in [(e.src, e.dst) for e in wf.edges]


# ===================================================== 3. the P-Mxx pairs (the point of the file)


def test_pM01_staleness_latch_vs_within_run() -> None:
    """P-M01 (stonebranch Part IV; M01 mapping-table assumption: 'no cross-
    run staleness is relied upon'). Producer and consumer share one cadence
    (M01 same-cycle, DL-12): 'prod' is scheduled, 'cons' inherits its
    cadence by being its sole condition predecessor. Day 1: prod succeeds,
    cons auto-starts and succeeds in BOTH engines -- the assumption holds
    this far. Day 2: STARTJOB targets 'cons' ONLY. AutoSys's SEM-01 latch
    means s(prod) is STILL true (prod's SUCCESS never expires) -> cons
    re-runs. UC (UCS-13: no cross-run latching) launches a FRESH instance
    in which 'cons' is not a source -- it has no way to re-satisfy its
    predecessor edge, so it waits forever. This is exactly the divergence
    the M01 A-row assumption exists to flag.

    Side note (DL-16, not a bug): STARTJOB on ANY task launches its WHOLE
    containing workflow -- so day 2's 'STARTJOB cons' ALSO resets and
    restarts 'prod' as a source task of the fresh UC instance, even though
    the script never touches prod again. That is its OWN small divergence
    (prod gets an extra RUNNING in UC that AutoSys never sees), asserted
    separately below rather than folded into the primary claim.
    """
    text = (
        "insert_job: pm01_prod\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\n\n'
        "insert_job: pm01_cons\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(pm01_prod)\n"
    )
    script = [
        ev("STARTJOB", 0, job="pm01_prod"),
        ev("STATUS", 1, job="pm01_prod", status="SUCCESS"),
        ev("STATUS", 2, job="pm01_cons", status="SUCCESS"),
        ev("STARTJOB", 24 * 60, job="pm01_cons"),  # day 2, targets the consumer only
    ]
    autosys_trace, uc_trace = run_both(text, script)
    assert transitions(autosys_trace, "pm01_cons") == [
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
        "RUNNING->SUCCESS",
        "SUCCESS->STARTING",  # the SEM-01 latch re-fires on day 2
        "STARTING->RUNNING",
    ]
    assert transitions(uc_trace, "pm01_cons") == ["Waiting->Running", "Running->Success"]

    divergence = first_divergence(autosys_trace, uc_trace, ["pm01_cons"])
    assert divergence is not None
    assert divergence.job == "pm01_cons"
    assert divergence.autosys == ["RUNNING", "SUCCESS", "RUNNING"]
    assert divergence.uc == ["RUNNING", "SUCCESS"]

    # side note: prod, untouched by the script on day 2, still gets a second
    # RUNNING in UC because STARTJOB(cons) restarts the whole shared instance
    assert job_outcomes(autosys_trace)["pm01_prod"] == ["RUNNING", "SUCCESS"]
    assert job_outcomes(uc_trace)["pm01_prod"] == ["RUNNING", "SUCCESS", "RUNNING"]


def test_pM07_mutex_overlap_abandon_vs_queue() -> None:
    """P-M07 (Part IV; M07 mapping-table assumption for n()). 'ja' is
    date_conditions-scheduled with condition n(jb) (bare, local -> a mutex
    PAIR at the derive layer, DL-12 -- never an edge); 'jb' is plain.
    Script: jb starts; ja's scheduled tick finds jb RUNNING -> n(jb) false
    -> AutoSys ABANDONS the attempt (SEM-32/Q3 default); DL-13's schedule
    double gate then blocks the LATER wake too -- a scheduled job only
    starts on its own tick, never via edge-triggered re-evaluation -- so ja
    is permanently abandoned in AutoSys. The closing STATUS SUCCESS is a
    script-artifact latch onto a job that never ran (oracle.py: injected
    STATUS is unconditional); that is precisely why the assertion below
    reads the FULL milestone SEQUENCE (['SUCCESS'], no RUNNING) rather than
    just the final status. UC has no schedule concept at all (UCS-09 mutex
    is a runtime resource, not a trigger gate): ja goes to ExclusiveWait and
    is released, FIFO, the instant jb completes -- a real run.
    """
    text = (
        "insert_job: pm07_ja\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\ncondition: n(pm07_jb)\n\n'
        "insert_job: pm07_jb\njob_type: c\ncommand: y\nmachine: m1\n"
    )
    script = [
        ev("STARTJOB", 0, job="pm07_jb"),
        ev("STARTJOB", 1, job="pm07_ja"),
        ev("STATUS", 2, job="pm07_jb", status="SUCCESS"),
        ev("STATUS", 3, job="pm07_ja", status="SUCCESS"),
    ]
    autosys_trace, uc_trace = run_both(text, script)
    assert transitions(autosys_trace, "pm07_ja") == ["INACTIVE->SUCCESS"]  # never RUNNING
    assert transitions(uc_trace, "pm07_ja") == [
        "Waiting->ExclusiveWait",
        "ExclusiveWait->Running",
        "Running->Success",
    ]

    divergence = first_divergence(autosys_trace, uc_trace, ["pm07_ja"])
    assert divergence is not None
    assert divergence.job == "pm07_ja"
    assert divergence.autosys == ["SUCCESS"]  # the script-artifact latch only
    assert divergence.uc == ["RUNNING", "SUCCESS"]  # UC actually queued and ran it


def test_pM09_set_global_mid_run() -> None:
    """P-M09 (Part IV; M09 mapping-table 'Re-eval-on-set' note). Consumer's
    condition is s(p) & v(GATE)=go. p succeeds while GATE is unset: UC
    evaluates the var_condition AT COMPLETION (UCS-01) -> false -> the
    WHOLE edge is unsatisfied -> consumer Skips. AutoSys's SET_GLOBAL is
    ITSELF a re-evaluation trigger (SEM-08): once GATE is set, s(p) (still
    latched) AND v(GATE)=go are both true -> consumer runs. UC's edge state
    is frozen at completion; SET_GLOBAL deliberately does not revive it."""
    text = (
        "insert_job: pm09_p\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: pm09_cons\njob_type: c\ncommand: y\nmachine: m1\n"
        "condition: s(pm09_p) & v(GATE) = go\n"
    )
    script = [
        ev("STARTJOB", 0, job="pm09_p"),
        ev("STATUS", 1, job="pm09_p", status="SUCCESS"),
        ev("SET_GLOBAL", 2, name="GATE", value="go"),
    ]
    autosys_trace, uc_trace = run_both(text, script)
    divergence = first_divergence(autosys_trace, uc_trace, ["pm09_cons"])
    assert divergence is not None
    assert divergence.job == "pm09_cons"
    assert divergence.autosys == ["RUNNING"]  # SET_GLOBAL woke it; both conjuncts now true
    assert divergence.uc == ["SKIPPED"]  # frozen at p's completion moment, GATE was unset then


def test_pM12_independent_or_first_branch_fires_vs_and_join() -> None:
    """P-M12 (Part IV; the hard compiler problem). Independent OR:
    consumer's condition is s(a)|s(b) with a, b sharing no common ancestor
    (OrShape.kind == 'independent'). AutoSys fires the moment EITHER branch
    is true. compile_twin's NAIVE M12 lowering (DL-16) attaches BOTH
    branches' edges to the consumer and lets UC's default conjunctive-over-
    non-skipped join (UCS-02/03) apply -- with 'b' never even attempted,
    its edge stays pending forever, so the consumer never resolves at all
    (not even Skipped: genuinely stuck)."""
    text = (
        "insert_job: pm12i_a\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: pm12i_b\njob_type: c\ncommand: y\nmachine: m1\n\n"
        "insert_job: pm12i_cons\njob_type: c\ncommand: z\nmachine: m1\n"
        "condition: s(pm12i_a) | s(pm12i_b)\n"
    )
    catalog = lower_source(text)
    model = compile_twin(catalog)
    (wf,) = model.workflows
    assert {(e.src, e.dst) for e in wf.edges} == {
        ("pm12i_a", "pm12i_cons"),
        ("pm12i_b", "pm12i_cons"),
    }  # naive lowering: BOTH OR branches became edges into the consumer
    assert any("M12 OR shapes present" in e and "U1-gated" in e for e in model.excluded)

    script = [ev("STARTJOB", 0, job="pm12i_a"), ev("STATUS", 1, job="pm12i_a", status="SUCCESS")]
    autosys_trace, uc_trace = run_both(text, script)
    divergence = first_divergence(autosys_trace, uc_trace, ["pm12i_cons"])
    assert divergence is not None
    assert divergence.job == "pm12i_cons"
    assert divergence.autosys == ["RUNNING"]  # fired on the first satisfied branch
    assert divergence.uc == []  # never even attempted: b's edge is still pending


def test_pM12_common_ancestor_diamond_converges() -> None:
    """The honest contrast (Part IV): a common-ancestor OR (OrShape.kind ==
    'common_ancestor') is exactly the UCS-03 diamond pattern the mapping
    table calls out as a WORKING lowering. root succeeds; b1's edge fires;
    b2's edge (from root's FAILURE) is skipped; 'skipped predecessors do
    not block' (UCS-02) lets the consumer run off b1 alone. AutoSys reaches
    the same result via s(b1) alone (Or short-circuits). first_divergence
    is None for the path that matters.

    b2 itself is EXCLUDED from the compared job list: it never ran in
    EITHER engine (root never reached FAILURE), but UC labels that
    'Skipped' while AutoSys leaves it silently untouched/INACTIVE --
    SKIPPED has no AutoSys analog by design (uc_oracle.py module
    docstring), so an unrun branch's bookkeeping label is a universal,
    uninteresting wrinkle, not part of the OR-convergence claim under test
    (demonstrated, not asserted as part of the None claim, below).
    """
    text = (
        "insert_job: pm12d_root\njob_type: c\ncommand: r\nmachine: m1\n\n"
        "insert_job: pm12d_b1\njob_type: c\ncommand: b1\nmachine: m1\ncondition: s(pm12d_root)\n\n"
        "insert_job: pm12d_b2\njob_type: c\ncommand: b2\nmachine: m1\ncondition: f(pm12d_root)\n\n"
        "insert_job: pm12d_cons\njob_type: c\ncommand: cc\nmachine: m1\n"
        "condition: s(pm12d_b1) | s(pm12d_b2)\n"
    )
    script = [
        ev("STARTJOB", 0, job="pm12d_root"),
        ev("STATUS", 1, job="pm12d_root", status="SUCCESS"),
        ev("STATUS", 2, job="pm12d_b1", status="SUCCESS"),
        ev("STATUS", 3, job="pm12d_cons", status="SUCCESS"),
    ]
    autosys_trace, uc_trace = run_both(text, script)
    jobs = ["pm12d_root", "pm12d_b1", "pm12d_cons"]
    assert first_divergence(autosys_trace, uc_trace, jobs) is None

    # the excluded branch's label difference, demonstrated for the record:
    assert job_outcomes(autosys_trace).get("pm12d_b2", []) == []
    assert job_outcomes(uc_trace)["pm12d_b2"] == ["SKIPPED"]


def test_pM19_all_iced_predecessors_runs_vs_skip_cascade() -> None:
    """P-M19 (Part IV; M19 mapping-table all-skipped-cascade caveat).
    Consumer's condition is s(i1)&s(i2), both predecessors ON_ICE before
    the workflow ever launches. AutoSys: SEM-05/SEM-20 (DL-13) -- ice
    satisfies EVERY atom kind, so both conjuncts are true the instant
    'cons' is (manually) attempted -> it runs. UC: ON_ICE is Skip-AT-START
    (M19); at instance launch both iced sources Skip immediately, and 'ALL
    predecessors Skipped -> Skip' (UCS-02) cascades onto the consumer too
    -- it never runs at all."""
    text = (
        "insert_job: pm19_i1\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: pm19_i2\njob_type: c\ncommand: y\nmachine: m1\n\n"
        "insert_job: pm19_cons\njob_type: c\ncommand: z\nmachine: m1\n"
        "condition: s(pm19_i1) & s(pm19_i2)\n"
    )
    script = [
        ev("ON_ICE", 0, job="pm19_i1"),
        ev("ON_ICE", 0, job="pm19_i2"),
        ev("STARTJOB", 1, job="pm19_cons"),
    ]
    autosys_trace, uc_trace = run_both(text, script)
    divergence = first_divergence(autosys_trace, uc_trace, ["pm19_cons"])
    assert divergence is not None
    assert divergence.job == "pm19_cons"
    assert divergence.autosys == ["RUNNING"]
    assert divergence.uc == ["SKIPPED"]


def test_pM19_contrast_one_iced_one_real_predecessor_converges() -> None:
    """Contrast (M19 mapping-table note: 'skipped predecessor doesn't block
    successors (UCS-02) -- downstream-satisfied matches'). Only i1 is
    iced; i2 is real and actually succeeds. Both engines run the consumer:
    UC via UCS-02 (i1's edge Skipped, i2's edge satisfied -> runs anyway);
    AutoSys because ice satisfies s(i1) while i2's real SUCCESS satisfies
    s(i2). i2 gets its own explicit STARTJOB so AutoSys drives it through a
    REAL STARTING->RUNNING (not a bare STATUS overwrite artifact) -- the
    same real run i2 gets in UC as a source task of the shared instance.

    i1 itself is excluded from the compared list for the same reason as
    the M12 diamond's b2: SKIPPED has no AutoSys analog, so an iced job's
    own bookkeeping label always looks like a 'divergence' even when the
    consumer path it feeds genuinely converges -- demonstrated, not folded
    into the None claim, below.
    """
    text = (
        "insert_job: pm19c_i1\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: pm19c_i2\njob_type: c\ncommand: y\nmachine: m1\n\n"
        "insert_job: pm19c_cons\njob_type: c\ncommand: z\nmachine: m1\n"
        "condition: s(pm19c_i1) & s(pm19c_i2)\n"
    )
    script = [
        ev("ON_ICE", 0, job="pm19c_i1"),
        ev("STARTJOB", 1, job="pm19c_cons"),
        ev("STARTJOB", 1, job="pm19c_i2"),
        ev("STATUS", 2, job="pm19c_i2", status="SUCCESS"),
        ev("STATUS", 3, job="pm19c_cons", status="SUCCESS"),
    ]
    autosys_trace, uc_trace = run_both(text, script)
    assert first_divergence(autosys_trace, uc_trace, ["pm19c_i2", "pm19c_cons"]) is None

    assert job_outcomes(autosys_trace).get("pm19c_i1", []) == []
    assert job_outcomes(uc_trace)["pm19c_i1"] == ["SKIPPED"]


def test_pM27_run_window_skip_vs_plain_run() -> None:
    """P-M27 (Part IV; M27 is a flat R in the mapping table -- 'no direct
    analog' -- so compile_twin excludes run_window entirely, DL-16). 04:30
    is 30 minutes past the 02:00-04:00 window's close and 21.5h from its
    next opening -- closer to the previous close -> AutoSys's closer-edge
    rule (SEM-33) SKIPs it permanently (no timer queued, unlike the DEFER
    branch). UC has no window concept at all: the job just runs. The
    trailing STATUS legitimately completes the UC run; on the AutoSys side
    it lands on a job that never started (INACTIVE), the same script-
    artifact hazard documented in P-M07 -- noted, not hidden."""
    text = (
        "insert_job: pm27_job\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "02:00"\nrun_window: "02:00-04:00"\n'
    )
    catalog = lower_source(text)
    script = [
        Event(at=datetime(2026, 7, 1, 4, 30), kind="STARTJOB", payload={"job": "pm27_job"}),
        Event(
            at=datetime(2026, 7, 1, 4, 40),
            kind="STATUS",
            payload={"job": "pm27_job", "status": "SUCCESS"},
        ),
    ]
    autosys_trace = Oracle(catalog).run_script(script)
    uc_trace = UcOracle(compile_twin(catalog)).run_script(script)
    assert transitions(autosys_trace, "pm27_job") == ["RUN_WINDOW_SKIP", "INACTIVE->SUCCESS"]

    divergence = first_divergence(autosys_trace, uc_trace, ["pm27_job"])
    assert divergence is not None
    assert divergence.job == "pm27_job"
    assert "RUNNING" in divergence.uc
    assert "RUNNING" not in divergence.autosys
    # the spurious latch: both eventually show SUCCESS despite autosys never running it
    assert divergence.autosys == ["SUCCESS"]
    assert divergence.uc == ["RUNNING", "SUCCESS"]


# ===================================================== 4. convergence sanity


def test_convergence_plain_success_chain() -> None:
    text = (
        "insert_job: conv_p\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: conv_c\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(conv_p)\n"
    )
    script = [
        ev("STARTJOB", 0, job="conv_p"),
        ev("STATUS", 1, job="conv_p", status="SUCCESS"),
        ev("STATUS", 2, job="conv_c", status="SUCCESS"),
    ]
    autosys_trace, uc_trace = run_both(text, script)
    assert first_divergence(autosys_trace, uc_trace, ["conv_p", "conv_c"]) is None


def test_convergence_failure_edge() -> None:
    """f(p): p FAILS -> both engines run the consumer."""
    text = (
        "insert_job: convf_p\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: convf_c\njob_type: c\ncommand: y\nmachine: m1\ncondition: f(convf_p)\n"
    )
    script = [
        ev("STARTJOB", 0, job="convf_p"),
        ev("STATUS", 1, job="convf_p", status="FAILURE"),
        ev("STATUS", 2, job="convf_c", status="SUCCESS"),
    ]
    autosys_trace, uc_trace = run_both(text, script)
    assert first_divergence(autosys_trace, uc_trace, ["convf_p", "convf_c"]) is None


def test_convergence_done_edge() -> None:
    """d(p): matches any terminal status, in both engines."""
    text = (
        "insert_job: convd_p\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: convd_c\njob_type: c\ncommand: y\nmachine: m1\ncondition: d(convd_p)\n"
    )
    script = [
        ev("STARTJOB", 0, job="convd_p"),
        ev("STATUS", 1, job="convd_p", status="FAILURE"),
        ev("STATUS", 2, job="convd_c", status="SUCCESS"),
    ]
    autosys_trace, uc_trace = run_both(text, script)
    assert first_divergence(autosys_trace, uc_trace, ["convd_p", "convd_c"]) is None


def test_convergence_box_fold_excludes_the_box_workflow_name_marker() -> None:
    """An all-success box vs. its UC workflow fold. The box's OWN name is
    NOT one of its own compiled workflow's tasks (compile_twin names the
    workflow after the box; the box itself is never a member) --
    STARTJOB(box name) is a NO_WORKFLOW no-op on the UC side, so a fair
    launch now works with ONE script fired at the box name on both sides
    (review M-2: the workflow is addressable by its own box name, UCS-0
    "workflows are themselves tasks"), and the box name itself converges
    (review C-1: INSTANCE->Running gives the workflow the same
    RUNNING+terminal shape as the AutoSys box)."""
    text = (
        "insert_job: fold_box\njob_type: b\n\n"
        "insert_job: fold_m1\njob_type: c\ncommand: x\nmachine: m1\nbox_name: fold_box\n\n"
        "insert_job: fold_m2\njob_type: c\ncommand: y\nmachine: m1\nbox_name: fold_box\n"
    )
    catalog = lower_source(text)
    script = [
        ev("STARTJOB", 0, job="fold_box"),  # one script, both engines
        ev("STATUS", 1, job="fold_m1", status="SUCCESS"),
        ev("STATUS", 2, job="fold_m2", status="SUCCESS"),
    ]
    autosys_trace = Oracle(catalog).run_script(script)
    uc_trace = UcOracle(compile_twin(catalog)).run_script(script)

    # box name AND members: genuine, full convergence
    assert job_outcomes(autosys_trace)["fold_box"] == ["RUNNING", "SUCCESS"]
    assert job_outcomes(uc_trace)["fold_box"] == ["RUNNING", "SUCCESS"]
    assert first_divergence(autosys_trace, uc_trace, ["fold_box", "fold_m1", "fold_m2"]) is None


# ===================================================== 5. comparator unit tests


@pytest.mark.parametrize(
    ("transition", "expected"),
    [
        ("Waiting->Running", "RUNNING"),
        ("Running->Success", "SUCCESS"),
        ("Running->Failed", "FAILURE"),
        ("Running->Cancelled", "TERMINATED"),
        ("Waiting->Skipped", "SKIPPED"),
        ("Waiting->Held", None),  # unrecognized UC target
        ("Waiting->ExclusiveWait", None),
        ("ON_ICE", None),  # no "->" at all
        ("INACTIVE->STARTING", None),  # AutoSys-shaped: no fallback in this function
        ("STARTING->RUNNING", None),  # ditto -- "RUNNING" is not a _TO_AUTOSYS key
    ],
)
def test_normalize_transition_mapping_table(transition: str, expected: str | None) -> None:
    """normalize_transition is a UC-only mapping (target -> _TO_AUTOSYS
    lookup, no fallback): it recognizes the 5 UC terminal/running targets
    and returns None for anything else, INCLUDING already-AutoSys-shaped
    transitions -- job_outcomes (below) is the bilateral function that
    handles both vocabularies; this one does not."""
    assert normalize_transition(transition) == expected


def test_job_outcomes_drops_starting_and_uc_internal_markers() -> None:
    """job_outcomes keeps only the shared RUNNING/terminal/SKIPPED
    milestones: AutoSys's STARTING is dropped (its own docstring), and UC's
    internal markers (no '->', or an unrecognized target like Held/
    ExclusiveWait) are dropped too."""
    trace = [
        TraceEntry(at=T0, job="x", transition="INACTIVE->STARTING", cause="c"),
        TraceEntry(at=T0, job="x", transition="STARTING->RUNNING", cause="c"),
        TraceEntry(at=T0, job="x", transition="RUNNING->SUCCESS", cause="c"),
        TraceEntry(at=T0, job="y", transition="INSTANCE_LAUNCHED", cause="c"),
        TraceEntry(at=T0, job="y", transition="Waiting->Held", cause="c"),
        TraceEntry(at=T0, job="y", transition="Waiting->ExclusiveWait", cause="c"),
        TraceEntry(at=T0, job="y", transition="ExclusiveWait->Running", cause="c"),
        TraceEntry(at=T0, job="z", transition="RUN_WINDOW_SKIP", cause="c"),
    ]
    outcomes = job_outcomes(trace)
    assert outcomes["x"] == ["RUNNING", "SUCCESS"]
    assert outcomes["y"] == ["RUNNING"]
    assert "z" not in outcomes


def test_first_divergence_none_on_identical_traces() -> None:
    trace = [
        TraceEntry(at=T0, job="a", transition="Waiting->Running", cause="c"),
        TraceEntry(at=T0, job="b", transition="INACTIVE->STARTING", cause="c"),
    ]
    assert first_divergence(trace, [t.model_copy() for t in trace], ["a", "b"]) is None


def test_first_divergence_returns_the_first_differing_job_in_given_order() -> None:
    """Two jobs differ; the function returns whichever comes FIRST in the
    caller-supplied `jobs` order, not e.g. trace/catalog order."""
    autosys_trace = [
        TraceEntry(at=T0, job="a", transition="INACTIVE->STARTING", cause="c"),
        TraceEntry(at=T0, job="a", transition="STARTING->RUNNING", cause="c"),
        TraceEntry(at=T0, job="b", transition="INACTIVE->STARTING", cause="c"),
        TraceEntry(at=T0, job="b", transition="STARTING->RUNNING", cause="c"),
    ]
    uc_trace = [
        TraceEntry(at=T0, job="a", transition="Waiting->Skipped", cause="c"),
        TraceEntry(at=T0, job="b", transition="Waiting->Skipped", cause="c"),
    ]
    first = first_divergence(autosys_trace, uc_trace, ["a", "b"])
    assert first is not None
    assert first.job == "a"
    second = first_divergence(autosys_trace, uc_trace, ["b", "a"])
    assert second is not None
    assert second.job == "b"


def test_divergence_model_fields() -> None:
    d = Divergence(job="x", autosys=["RUNNING"], uc=["SKIPPED"])
    assert d.job == "x"
    assert d.autosys == ["RUNNING"]
    assert d.uc == ["SKIPPED"]


# ============================================ 6. review-driven regressions (DL-16a)

# Fixes from the UC-twin adversarial review; each test pins the corrected
# behavior so it cannot regress silently.


def test_review_m1_f_edge_stays_exact_on_killed_producer_end_to_end() -> None:
    """Review M-1: f(a) is an EXACT M04 row -- killing the producer must
    leave the consumer unstarted in BOTH engines (the failure edge no
    longer fires on Cancelled)."""
    text = (
        "insert_job: k_prod\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: k_cons\njob_type: c\ncommand: y\nmachine: m1\ncondition: f(k_prod)\n"
    )
    catalog = lower_source(text)
    script = [
        ev("STARTJOB", 0, job="k_prod"),
        ev("KILLJOB", 1, job="k_prod"),
    ]
    autosys_trace = Oracle(catalog).run_script(script)
    uc_trace = UcOracle(compile_twin(catalog)).run_script(script)
    assert first_divergence(autosys_trace, uc_trace, ["k_cons"]) is None
    assert job_outcomes(uc_trace)["k_cons"] == ["SKIPPED"]  # recorded non-run == absence


def test_review_m2_startjob_on_the_box_name_launches_the_workflow() -> None:
    """Review M-2: the canonical AutoSys box trigger STARTJOB(box) must
    launch the box-named workflow (UCS-0: workflows are themselves tasks)."""
    text = (
        "insert_job: mbox\njob_type: b\n\n"
        "insert_job: mm1\njob_type: c\ncommand: x\nmachine: m1\nbox_name: mbox\n"
    )
    o = UcOracle(compile_twin(lower_source(text)))
    o.feed(ev("STARTJOB", 0, job="mbox"))
    assert transitions(o.trace(), "mbox") == ["INSTANCE->Running"]
    assert transitions(o.trace(), "mm1") == ["Waiting->Running"]


def test_review_m2_nested_box_name_aliases_to_the_top_workflow() -> None:
    text = (
        "insert_job: outer_bx\njob_type: b\n\n"
        "insert_job: inner_bx\njob_type: b\nbox_name: outer_bx\n\n"
        "insert_job: leaf_t\njob_type: c\ncommand: x\nmachine: m1\nbox_name: inner_bx\n"
    )
    model = compile_twin(lower_source(text))
    (wf,) = model.workflows
    assert wf.name == "outer_bx"
    assert wf.aliases == ["inner_bx"]
    o = UcOracle(model)
    o.feed(ev("STARTJOB", 0, job="inner_bx"))  # alias launches the top workflow
    assert transitions(o.trace(), "outer_bx") == ["INSTANCE->Running"]
    assert transitions(o.trace(), "leaf_t") == ["Waiting->Running"]


def test_review_m3_gate_on_exitcode_only_consumer_lands_in_the_ledger() -> None:
    """Review M-3: a v(G) gate whose consumer's only edges already carry M08
    exitcode var_conditions cannot attach -- it must be RECORDED, never
    silently dropped (no-silent-loss constitution)."""
    text = (
        "insert_job: g_prod\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: g_cons\njob_type: c\ncommand: y\nmachine: m1\n"
        "condition: e(g_prod) = 3 & v(GATE) = 1\n"
    )
    model = compile_twin(lower_source(text))
    (wf,) = model.workflows
    (edge,) = wf.edges
    assert edge.var_condition is not None
    assert edge.var_condition.name == "exit:g_prod"  # M08 kept its slot
    assert any("M09 global gate $GATE -> g_cons" in entry for entry in model.excluded)


def test_review_m3_gate_not_on_every_path_is_recorded() -> None:
    """Review M-3 (related finding): when the gate attaches to some edges
    but others already carry M08 var_conditions, the >=1-satisfied join can
    bypass the gate -- recorded in the ledger."""
    text = (
        "insert_job: p_s\njob_type: c\ncommand: a\nmachine: m1\n\n"
        "insert_job: p_e\njob_type: c\ncommand: b\nmachine: m1\n\n"
        "insert_job: mix_cons\njob_type: c\ncommand: y\nmachine: m1\n"
        "condition: s(p_s) & e(p_e) = 3 & v(GATE) = 1\n"
    )
    model = compile_twin(lower_source(text))
    (wf,) = model.workflows
    gated = [e for e in wf.edges if e.var_condition and e.var_condition.name == "GATE"]
    assert len(gated) == 1  # attached to the status edge
    assert any("not on every path" in entry for entry in model.excluded)


def test_review_e1_force_first_event_converges_with_autosys() -> None:
    """Review E-1: FORCE_STARTJOB as the first event force-starts in both
    engines (the twin launches the containing workflow, then forces)."""
    text = "insert_job: solo_f\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(ghost_f)\n"
    catalog = lower_source(text)
    script = [
        ev("FORCE_STARTJOB", 0, job="solo_f"),
        ev("STATUS", 1, job="solo_f", status="SUCCESS"),
    ]
    autosys_trace = Oracle(catalog).run_script(script)
    uc_trace = UcOracle(compile_twin(catalog)).run_script(script)
    assert first_divergence(autosys_trace, uc_trace, ["solo_f"]) is None
