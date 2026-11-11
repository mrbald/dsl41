"""Count scheduler refusals served by the trace (runner-design §§9 and 11)."""

from collections.abc import Iterable, Mapping
from typing import Any

type RefusalKey = tuple[str, int, str]


def scheduler_refusal_counts(entries: Iterable[Mapping[str, Any]]) -> dict[RefusalKey, int]:
    """Count by job, run and refused-status class; STARTING and RUNNING share a class.

    Missing provenance or run facts cannot contribute. QUE_WAIT belongs to its own class:
    queue admission has not yet incremented the job's run number.
    """
    counts: dict[RefusalKey, int] = {}
    for entry in entries:
        if entry.get("transition") != "START_REFUSED" or entry.get("start_source") != "scheduler":
            continue
        job, run, status = entry.get("job"), entry.get("run_number"), entry.get("refused_status")
        if not isinstance(job, str) or type(run) is not int or not isinstance(status, str):
            continue
        status_class = "RUNNING" if status in ("STARTING", "RUNNING") else status
        key = (job, run, status_class)
        counts[key] = counts.get(key, 0) + 1
    return counts
