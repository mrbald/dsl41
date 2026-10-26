"""Flat experimental worker charts. Persistence belongs to the caller.

The executor selects every move. Restoring a durable state runs no actions
(statechart-engine §11). These charts are not runner machine registrations.
"""

from dsl41.state_machine import Event, Executor, Exclusion, Region, Row, State, Statechart

ARCHIVE = Statechart(
    "file_archive",
    State(
        "archive",
        regions=(
            Region(
                "receipt",
                "FILE_RECEIVED",
                (State("FILE_RECEIVED"), State("FILE_ARCHIVED", final=True)),
            ),
        ),
    ),
    (Row("file_archive.01", "FILE_RECEIVED", "archive_committed", "FILE_ARCHIVED"),),
    clock="logical",
)

PROCESS = Statechart(
    "file_process",
    State(
        "process",
        regions=(
            Region(
                "work",
                "READY",
                (
                    State("READY"),
                    State("RUNNING"),
                    State("UNKNOWN"),
                    State("DONE", final=True),
                    State("REJECTED", final=True),
                ),
            ),
        ),
    ),
    (
        Row("file_process.01", "READY", "claim", "RUNNING"),
        Row("file_process.02", "RUNNING", "completed", "DONE"),
        Row("file_process.03", "RUNNING", "retry_safe", "READY"),
        Row("file_process.04", "RUNNING", "uncertain", "UNKNOWN"),
        Row("file_process.05", "UNKNOWN", "completed", "DONE"),
        Row("file_process.06", "UNKNOWN", "retry_safe", "READY"),
        Row("file_process.07", "UNKNOWN", "uncertain", "UNKNOWN"),
        Row("file_process.08", "READY", "rejected", "REJECTED"),
        Row("file_process.09", "UNKNOWN", "rejected", "REJECTED"),
    ),
    clock="logical",
    exclusions=(
        Exclusion("READY", "completed", "Only a claimed or uncertain item can complete."),
        Exclusion("READY", "retry_safe", "READY has no attempt to reconcile."),
        Exclusion("READY", "uncertain", "READY has no attempt to reconcile."),
        Exclusion("RUNNING", "claim", "The process lock forbids a second claim."),
        Exclusion("UNKNOWN", "claim", "Reconciliation must prove retry safety first."),
        Exclusion("RUNNING", "rejected", "A stale claim is converted through .04 first."),
    ),
)


def transition(chart: Statechart, old: str, event: str) -> tuple[str, str]:
    """Return the executor's leaf and the id of the row it fired, for the SQL write.

    A move that fires no row, such as any event in a final state, is a ValueError.
    """
    executor = Executor()
    executor.restore("item", chart, (chart.root.name, old))
    rows = [row for outcome in executor.dispatch("item", Event(event)) for row in outcome.rows]
    if len(rows) != 1:
        raise ValueError(f"{old}: {event} makes no move")
    return _leaf(executor, chart), rows[0]


def state_after(chart: Statechart, old: str | None = None, event: str | None = None) -> str:
    """Return the executor's leaf for the SQL write, never a parallel transition table.

    With no state and no event it is the chart's initial leaf.
    """
    if old is not None and event is not None:
        return transition(chart, old, event)[0]
    if old is not None or event is not None:
        raise ValueError("state_after takes a state and an event, or neither")
    executor = Executor()
    executor.start("item", chart)
    executor.run()
    return _leaf(executor, chart)


def _leaf(executor: Executor, chart: Statechart) -> str:
    return next(state for state in executor.active("item") if state != chart.root.name)
