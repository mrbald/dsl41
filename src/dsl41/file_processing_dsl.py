"""Add the two file-processing jobs to an estate (DL-404; examples/file_processing/CONTRACT.md).

This compiler-facing helper is separate from the worker's import path. It adds
two ordinary command jobs; it introduces no compiler folds or runtime wiring.
"""

from collections.abc import Mapping, Sequence
import shlex

from dsl41.conditions import JobRef, Lookback, StatusAtom
from dsl41.dsl import CatalogBuilder, cond_to_source

# The helper owns these attributes of both jobs: the contract fixes the two
# conditions and names the watcher and the two jobs as the whole scheduler
# surface (CONTRACT.md "Activation is external").
_OWNED = frozenset({"job_type", "box_name", "machine", "command", "condition"})


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
    machine: str,
    archive_argv: Sequence[str],
    process_argv: Sequence[str],
    archiver: str = "ARCHIVER",
    processor: str = "PROCESSOR",
    archiver_attrs: Mapping[str, object] | None = None,
    processor_attrs: Mapping[str, object] | None = None,
) -> None:
    """Add the archiver on `s(inbox_watcher)` and the processor on `s(archiver, 0)`.

    The watcher must already be an FW job in the builder. Each argv is a trusted
    command line, usually `python -m dsl41.file_processing_worker ROLE ...`;
    `shlex.join` quotes it. The attrs mappings carry other JIL attributes, such
    as `owner`, `std_out_file` or an archiver sweep's `start_mins`. The helper's
    own refusals are ValueErrors raised before it adds a job.
    """
    jobs = builder.build().jobs
    watcher = jobs.get(inbox_watcher)
    if watcher is None or watcher.job_type != "FW":
        raise ValueError(f"inbox_watcher {inbox_watcher!r} must name an existing FW job")
    if not archive_argv or not process_argv:
        raise ValueError("both command argv lists must be nonempty")
    if archiver == processor or archiver in jobs or processor in jobs:
        raise ValueError(f"{archiver!r} and {processor!r} must be two new job names")
    if builder._box_stack:
        raise ValueError("call it outside a box: the contract names no box")
    for attrs in (archiver_attrs or {}, processor_attrs or {}):
        owned = sorted(_OWNED & attrs.keys())
        if owned:
            raise ValueError(f"the helper sets {', '.join(owned)}; pass the other attributes")
    for name, argv, condition, extra in (
        (archiver, archive_argv, _success(inbox_watcher), archiver_attrs),
        (processor, process_argv, _success(archiver, zero=True), processor_attrs),
    ):
        fields: dict[str, object] = {"machine": machine, "command": shlex.join(argv)}
        builder.job(name, job_type="c", condition=condition, **fields, **(extra or {}))
