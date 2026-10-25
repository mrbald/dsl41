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
                (State("READY"), State("RUNNING"), State("UNKNOWN"), State("DONE", final=True)),
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
    ),
    clock="logical",
    exclusions=(
        Exclusion("READY", "completed", "Only a claimed or uncertain item can complete."),
        Exclusion("READY", "retry_safe", "READY has no attempt to reconcile."),
        Exclusion("READY", "uncertain", "READY has no attempt to reconcile."),
        Exclusion("RUNNING", "claim", "The process lock forbids a second claim."),
        Exclusion("UNKNOWN", "claim", "Reconciliation must prove retry safety first."),
    ),
)


def state_after(chart: Statechart, old: str | None = None, event: str | None = None) -> str:
    """Return the executor's leaf for the SQL write, never a parallel transition table."""
    executor = Executor()
    if old is None:
        executor.start("item", chart)
        executor.run()
    else:
        executor.restore("item", chart, (chart.root.name, old))
    if event is not None:
        outcomes = executor.dispatch("item", Event(event))
        if not any(outcome.rows for outcome in outcomes):
            raise ValueError(f"{old}: {event} makes no move")
    return next(state for state in executor.active("item") if state != chart.root.name)
