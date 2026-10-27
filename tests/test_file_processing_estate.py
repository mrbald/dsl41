"""Python authoring (DL-404 slice 4): the helper, the deployed worker command, DSL round trips
and SEM-04 backlog limits, without scheduler changes."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from dsl41.cli import app

from dsl41.dsl import CatalogBuilder, cond_to_source, decompile
from dsl41.equiv import catalog_hash, equivalent_tier_a
from dsl41.file_processing import STAGE, Policy, WorkItem, archive, process
from dsl41.file_processing_dsl import add_file_processing_jobs
from dsl41.ir import CatalogIR, ExecSpec, FwSpec, lower_source
from dsl41.oracle import Oracle
from dsl41.oracle_state import Event
from examples.file_processing.estate import estate
from examples.file_processing.worker import CountsHandler


def test_estate_has_exactly_one_fw_and_two_explicit_cmds(tmp_path: Path) -> None:
    builder = estate(tmp_path, tmp_path / "ready.signal", Path(sys.executable), "localhost")
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
    rebuilt = namespace["estate"]().build()  # type: ignore[operator]
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
    builder = estate(tmp_path, tmp_path / "ready", Path(sys.executable), "local")
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


def test_to_jil_lowers_to_the_contract_catalog(tmp_path: Path) -> None:
    """CONTRACT.md "Activation is external": the watcher, `s(watcher)` on the archiver and
    `s(archiver, 0)` on the processor are the whole scheduler surface; no box, no timer."""
    python = Path("/opt/site/venv/bin/python")
    catalog = lower_source(estate(tmp_path, tmp_path / "ready", python, "host1").to_jil())
    assert sorted(catalog.jobs) == ["ARCHIVER", "INBOX_FW", "PROCESSOR"]
    watcher = catalog.jobs["INBOX_FW"]
    assert watcher.job_type == "FW" and isinstance(watcher.exec_, FwSpec)
    assert (watcher.exec_.watch_file, watcher.exec_.watch_interval) == (str(tmp_path / "ready"), 1)
    assert (watcher.exec_.watch_file_min_size, watcher.exec_.machine) == (1, "host1")
    expected = {"ARCHIVER": ("s(INBOX_FW)", "archive"), "PROCESSOR": ("s(ARCHIVER, 0)", "process")}
    for name, (condition, role) in expected.items():
        job = catalog.jobs[name]
        assert job.job_type == "CMD" and job.box.box_name is None and job.schedule is None
        assert job.sem.condition is not None
        assert cond_to_source(job.sem.condition.cond) == condition
        assert isinstance(job.exec_, ExecSpec) and job.exec_.machine == "host1"
        argv = shlex.split(job.exec_.command)
        assert argv[:4] == [str(python), "-m", "dsl41.file_processing_worker", role]
        assert argv[4:8] == ["--root", str(tmp_path), "--processor-id", "counts-v1"]


def test_lint_accepts_the_estate(tmp_path: Path) -> None:
    jil = tmp_path / "estate.jil"
    jil.write_text(estate(tmp_path, tmp_path / "ready", Path(sys.executable), "local").to_jil())
    result = CliRunner().invoke(app, ["lint", str(jil), "--strict"])
    assert result.exit_code == 0, result.output


def test_helper_refuses_what_breaks_the_contract(tmp_path: Path) -> None:
    argv = ["python", "-m", "dsl41.file_processing_worker"]

    def add(builder: CatalogBuilder, **kwargs: object) -> None:
        arguments: dict[str, object] = {
            "inbox_watcher": "FW",
            "machine": "local",
            "archive_argv": argv,
            "process_argv": argv,
        }
        add_file_processing_jobs(builder, **{**arguments, **kwargs})  # type: ignore[arg-type]

    def fresh() -> CatalogBuilder:
        builder = CatalogBuilder().job("FW", job_type="f", watch_file=str(tmp_path / "ready"))
        return builder.job("CMD", command="true")

    refusals = [
        ({"inbox_watcher": "CMD"}, "existing FW job"),
        ({"process_argv": []}, "nonempty"),
        ({"archiver": "CMD"}, "two new job names"),
        ({"archiver": "ONE", "processor": "ONE"}, "two new job names"),
        ({"archiver_attrs": {"condition": "s(CMD)"}}, "sets condition"),
        ({"processor_attrs": {"box_name": "B", "machine": "m"}}, "sets box_name, machine"),
    ]
    for kwargs, message in refusals:
        builder = fresh()
        with pytest.raises(ValueError, match=message):
            add(builder, **kwargs)
        assert sorted(builder.build().jobs) == ["CMD", "FW"]
    boxed = fresh()
    with boxed.box("B"):
        with pytest.raises(ValueError, match="outside a box"):
            add(boxed)
    assert sorted(boxed.build().jobs) == ["B", "CMD", "FW"]
    swept = fresh()
    add(swept, archiver_attrs={"owner": "svc", "date_conditions": 1, "start_mins": [0, 30]})
    archiver = swept.build().jobs["ARCHIVER"]
    assert archiver.schedule is not None and archiver.exec_ is not None
    assert archiver.exec_.owner == "svc"


HANDLER = """
from dsl41.file_processing import Completed


class Handler:
    def process(self, item):
        return Completed(item.content_hash)


def make(root):
    return Handler()


def broken(root):
    return object()
"""


def test_deployed_worker_runs_without_a_checkout(tmp_path: Path) -> None:
    """The host command needs the installed package and the handler module only."""
    site, root = tmp_path / "site", tmp_path / "root"
    site.mkdir()
    (site / "site_handler.py").write_text(HANDLER)
    (root / "inbox").mkdir(parents=True)
    (root / "inbox" / "001-a.txt").write_bytes(b"one\n")
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    environment["PYTHONPATH"] = str(site)

    def worker(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "dsl41.file_processing_worker", *args]
            + ["--root", str(root), "--processor-id", "site-v1"],
            cwd=site,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )

    for spec, message in (("site_handler", "MODULE:FACTORY"), ("site_handler:broken", "process")):
        refused = worker("process", "--handler", spec)
        assert refused.returncode == 2 and message in refused.stderr
    assert not (root / "ledger.sqlite3").exists()
    archived = worker("archive")
    assert archived.returncode == 0, archived.stderr
    assert json.loads(archived.stdout)["selected"] == 1
    processed = worker("process", "--handler", "site_handler:make", "--batch", "one")
    assert processed.returncode == 0, processed.stderr
    assert json.loads(processed.stdout)["completed"] == 1
