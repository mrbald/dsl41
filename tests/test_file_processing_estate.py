"""Python DSL round trips and SEM-04 backlog limits, without scheduler changes."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import shlex
import sys

import pytest

from dsl41.dsl import CatalogBuilder, cond_to_source, decompile
from dsl41.equiv import catalog_hash, equivalent_tier_a
from dsl41.file_processing import STAGE, Policy, WorkItem, archive, process
from dsl41.file_processing_dsl import add_file_processing_jobs
from dsl41.ir import CatalogIR, ExecSpec
from dsl41.oracle import Oracle
from dsl41.oracle_state import Event
from examples.file_processing.estate import build_estate
from examples.file_processing.worker import CountsHandler


def test_estate_has_exactly_one_fw_and_two_explicit_cmds(tmp_path: Path) -> None:
    builder = build_estate(tmp_path, tmp_path / "ready.signal", Path(sys.executable), "localhost")
    catalog = builder.build()
    assert {name: job.job_type for name, job in catalog.jobs.items()} == {
        "INBOX_FW": "FW",
        "ARCHIVER": "CMD",
        "PROCESSOR": "CMD",
    }
    condition = catalog.jobs["PROCESSOR"].sem.condition
    assert condition is not None and cond_to_source(condition.cond) == "s(ARCHIVER, 0)"
    emitted = decompile(catalog)
    assert all("PROCESSOR" not in line for line in emitted.splitlines() if ".sequence(" in line)
    namespace: dict[str, object] = {"__name__": "<file-processing-roundtrip>"}
    exec(compile(emitted, "<file-processing-roundtrip>", "exec"), namespace)
    rebuilt = namespace["catalog"]
    assert isinstance(rebuilt, CatalogIR)
    assert catalog_hash(rebuilt) == catalog_hash(catalog)
    assert equivalent_tier_a(catalog, rebuilt).equivalent


def test_helper_adds_two_jobs_and_quotes_trusted_argv(tmp_path: Path) -> None:
    builder = CatalogBuilder().job("existing:FW", job_type="f", watch_file=str(tmp_path / "ready"))
    argv = ["/python with space", "worker.py", "--root", "/synthetic root/$literal"]
    add_file_processing_jobs(
        builder, inbox_watcher="existing:FW", archive_argv=argv, process_argv=argv, machine="local"
    )
    catalog = builder.build()
    assert len(catalog.jobs) == 3
    command = catalog.jobs["ARCHIVER"].exec_
    assert isinstance(command, ExecSpec) and command.command is not None
    assert shlex.split(command.command) == argv
    missing = CatalogBuilder()
    with pytest.raises(ValueError, match="existing FW"):
        add_file_processing_jobs(
            missing, inbox_watcher="missing", archive_argv=argv, process_argv=argv, machine="local"
        )
    assert not missing.build().jobs


def test_sem04_overlap_leaves_backlog_until_explicit_later_invocation(tmp_path: Path) -> None:
    """SEM-04: an archiver END during a processor run predates its next anchor."""
    identity = "counts-v1"
    builder = build_estate(tmp_path, tmp_path / "ready", Path(sys.executable), "local")
    oracle = Oracle(builder.build())
    start = datetime(2026, 1, 1, 8)

    def status(job: str, value: str, minute: int) -> None:
        oracle.feed(
            Event(
                at=start + timedelta(minutes=minute),
                kind="STATUS",
                payload={"job": job, "status": value},
            )
        )

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a").write_bytes(b"first\n")
    archive(tmp_path, identity)
    status("INBOX_FW", "SUCCESS", 0)
    status("ARCHIVER", "SUCCESS", 1)
    assert oracle.store.runtime("PROCESSOR").status == "RUNNING"

    class Overlap(CountsHandler):
        def process(self, item: WorkItem):
            (inbox / "b").write_bytes(b"second\n")
            archive(tmp_path, identity)
            status("ARCHIVER", "SUCCESS", 2)
            return super().process(item)

    report = process(tmp_path, identity, Overlap(tmp_path / "output"), Policy(batch="one"))
    status("PROCESSOR", "SUCCESS", 3)
    oracle.feed(
        Event(at=start + timedelta(minutes=4), kind="ON_HOLD", payload={"job": "PROCESSOR"})
    )
    oracle.feed(
        Event(at=start + timedelta(minutes=5), kind="OFF_HOLD", payload={"job": "PROCESSOR"})
    )
    transitions = [row.transition for row in oracle.trace() if row.job == "PROCESSOR"]
    assert transitions == [
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
        "RUNNING->SUCCESS",
        "ON_HOLD",
        "OFF_HOLD",
    ]
    assert report.remaining == 1 and oracle.store.runtime("PROCESSOR").status == "SUCCESS"
    assert len(list((tmp_path / "todo").glob("*.json"))) == 1
    assert process(tmp_path, identity, CountsHandler(tmp_path / "output")).completed == 1
    assert len(list((tmp_path / "output").glob("*.json"))) == 2
    assert [path.name for path in (tmp_path / "todo").iterdir()] == [STAGE]
