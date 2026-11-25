"""The admission of the builds before DL-559 and DL-560, for the tests of an intent an earlier
build wrote.

A new admission restarts no box: an update that changes box membership while a touched tree is
active is refused `tree_active`, and its intent records `restart_boxes: []` (DL-559). A stored
intent the earlier build wrote can still name roots, and a restart resumes it to its end (DL-489).
`legacy_admission` makes the engine admit as that build did, so a test writes such an intent
the way it was written, and drives the code that intent reaches: the old box runs' KILLJOBs,
the application, the holds of marked members and the forced starts (DL-463, DL-487 to DL-490,
DL-527). Its roots are the outermost running boxes the plan edits structurally
(`reload_plan.restart_boxes`), their members in either catalog are named, the running ones are
stopped, and so is every named job with a process the engine owns (DL-525). `install` does the
same in an engine process a test starts, which no monkeypatch reaches.

An admission with nothing to stop now applies at once, its intent, configuration row and
application in one store transaction (DL-560). `separate_writes`, which `legacy_admission`
includes, makes every admission take the chart's STOPPING row as the builds before did: its
intent is a transaction of its own, and the configuration row and the application follow in
transactions of their own, so a test can stop the engine between them."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dsl41 import runner_update
from dsl41.reload_plan import STOPPABLE, Live, box_scope
from dsl41.reload_update import UPDATE, Admitted, filter_of, plan_of
from dsl41.state_machine import Context

if TYPE_CHECKING:
    import pytest


def _patches(run_root: Path) -> list[tuple[Any, str, Callable[..., Any]]]:
    """The three replacements that make the engine on `run_root` admit as before DL-559."""

    def scope_of(plan_doc: Mapping[str, Any], live: Live) -> Any:
        opened = runner_update.open_plan(run_root, str(plan_doc["token"]))
        return box_scope(plan_of(plan_doc, live.catalog), live, opened.estate.catalog)

    def admission_data(plan_doc: Mapping[str, Any], *, live: Live) -> Admitted:
        plan = plan_of(plan_doc, live.catalog)
        scope = scope_of(plan_doc, live)
        names = {change.name for change in plan.changed} | set(plan.removed) | scope.members
        runs = {
            name: live.runs.get(name, 0)
            for name in names
            if name in live.catalog.jobs
            and live.status.get(name) in STOPPABLE
            and live.catalog.jobs[name].job_type != "BOX"
        }
        named = filter_of(plan, scope)
        for job in named & live.owned.keys():
            if job in live.catalog.jobs and live.catalog.jobs[job].job_type != "BOX":
                runs[job] = live.owned[job]
        stops = tuple(sorted(runs))
        return Admitted(
            str(plan_doc["token"]), stops, named, tuple((job, runs[job]) for job in stops)
        )

    def begin(
        self: runner_update.EngineUpdate,
        request: runner_update.UpdateRequest,
        opened: runner_update.OpenedPlan,
        admitted: Admitted,
    ) -> None:
        attempts = {job: self.engine.attempt_of_run(job, run) for job, run in admitted.stop_runs}
        scope = scope_of(opened.plan, self.live())
        self._install(request, opened, admitted, attempts, {}, scope)

    return [
        (runner_update, "admission_data", admission_data),
        (runner_update, "tree_active", lambda *_args: ()),
        (runner_update.EngineUpdate, "_begin", begin),
    ]


#: The update chart's admission guards as before DL-560: every admission stops first.
_GUARDS: dict[str, Callable[[Context], bool]] = {
    "some_to_stop": lambda ctx: isinstance(ctx.event.payload, Admitted),
    "none_to_stop": lambda ctx: False,
}


def separate_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every admission takes the STOPPING row and writes its intent, its configuration row and
    its application in separate transactions, as the builds before DL-560 did."""
    for name, guard in _GUARDS.items():
        monkeypatch.setitem(UPDATE.guards, name, guard)  # type: ignore[arg-type]


def legacy_admission(monkeypatch: pytest.MonkeyPatch, run_root: Path) -> None:
    """Admit every update of the engine on `run_root` as the builds before DL-559 did."""
    for owner, name, value in _patches(run_root):
        monkeypatch.setattr(owner, name, value)
    separate_writes(monkeypatch)


def install(run_root: Path) -> None:
    """`legacy_admission` for the whole life of this process: an engine a test starts as its
    own process calls this before it runs."""
    for owner, name, value in _patches(run_root):
        setattr(owner, name, value)
    UPDATE.guards.update(_GUARDS)  # type: ignore[attr-defined]
