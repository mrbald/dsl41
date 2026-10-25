"""Experimental Python composition for the file-processing prototype.

This compiler-facing helper is separate from the worker's import path. It adds
two ordinary command jobs; it introduces no compiler folds or runtime wiring.
"""

from collections.abc import Sequence
import shlex

from dsl41.conditions import JobRef, Lookback, StatusAtom
from dsl41.dsl import CatalogBuilder, cond_to_source


def _success(name: str, *, zero: bool = False) -> str:
    return cond_to_source(
        StatusAtom(
            job=JobRef(name=name),
            status="SUCCESS",
            lookback=Lookback(kind="zero", minutes=None, raw="0") if zero else None,
        )
    )


def add_file_processing_jobs(
    builder: CatalogBuilder,
    *,
    inbox_watcher: str,
    archive_argv: Sequence[str],
    process_argv: Sequence[str],
    machine: str,
    archiver: str = "ARCHIVER",
    processor: str = "PROCESSOR",
) -> None:
    """Add exactly two CMD jobs referencing the caller's existing watcher.

    Callers supply trusted argv, including the chosen Python interpreter and
    worker entry point. The builder's normal lowering validates the conditions.
    """
    catalog = builder.build()
    watcher = catalog.jobs.get(inbox_watcher)
    if watcher is None or watcher.job_type != "FW":
        raise ValueError("inbox_watcher must name an existing FW job")
    if not archive_argv or not process_argv:
        raise ValueError("both command argv lists must be nonempty")
    builder.job(
        archiver,
        machine=machine,
        command=shlex.join(archive_argv),
        condition=_success(inbox_watcher),
    )
    builder.job(
        processor,
        machine=machine,
        command=shlex.join(process_argv),
        condition=_success(archiver, zero=True),
    )
