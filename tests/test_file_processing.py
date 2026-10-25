"""Synthetic local worker protocol probes; all effects live under tmp_path."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from dsl41 import file_processing as fp
from dsl41.file_processing_charts import ARCHIVE, PROCESS, state_after
from dsl41.state_machine import StatechartError, well_formed
from examples.file_processing.demo import run
from examples.file_processing.worker import CountsHandler

IDENTITY = "counts-v1"


def query(root: Path, sql: str) -> list[sqlite3.Row]:
    db = sqlite3.connect(root / "ledger.sqlite3")
    db.row_factory = sqlite3.Row
    try:
        return db.execute(sql).fetchall()
    finally:
        db.close()


def deliver(root: Path, name: str, data: bytes) -> None:
    (root / "inbox").mkdir(parents=True, exist_ok=True)
    (root / "inbox" / name).write_bytes(data)


def handler(root: Path) -> CountsHandler:
    return CountsHandler(root / "output")


def published(directory: Path) -> list[Path]:
    """Published entries, without the reserved staging directory."""
    return sorted(path for path in directory.iterdir() if path.name != fp.STAGE)


def test_duplicate_deliveries_and_distinct_original_names(tmp_path: Path) -> None:
    deliver(tmp_path, "001-report.txt", b"first\n")
    deliver(tmp_path, "002-report.txt", b"first\n")
    deliver(tmp_path, "003-report.txt", b"second\nthird\n")
    report = fp.archive(tmp_path, IDENTITY)
    receipts = query(tmp_path, "SELECT * FROM receipts ORDER BY seq")
    assert [row["name"] for row in receipts] == [
        "001-report.txt",
        "002-report.txt",
        "003-report.txt",
    ]
    assert all(row["state"] == "FILE_ARCHIVED" for row in receipts)
    assert report.completed == 3 and report.duplicates == 1
    assert len(published(tmp_path / "objects")) == 2
    assert len(query(tmp_path, "SELECT * FROM work")) == 2
    assert fp.process(tmp_path, IDENTITY, handler(tmp_path)).completed == 2
    assert len(list((tmp_path / "output").glob("*.json"))) == 2
    assert query(tmp_path, "SELECT count(*) FROM work WHERE evidence IS NOT NULL")[0][0] == 2


def test_before_archive_and_reruns_are_noops(tmp_path: Path) -> None:
    assert fp.process(tmp_path, IDENTITY, handler(tmp_path)).selected == 0
    deliver(tmp_path, "delivery-1", b"one\n")
    fp.archive(tmp_path, IDENTITY)
    fp.process(tmp_path, IDENTITY, handler(tmp_path))
    original = tuple(query(tmp_path, "SELECT * FROM work")[0])
    deliver(tmp_path, "delivery-2", b"one\n")
    assert fp.archive(tmp_path, IDENTITY).duplicates == 1
    assert not published(tmp_path / "todo")
    assert fp.process(tmp_path, IDENTITY, handler(tmp_path)).selected == 0
    assert fp.archive(tmp_path, IDENTITY).selected == 0
    assert tuple(query(tmp_path, "SELECT * FROM work")[0]) == original


@pytest.mark.parametrize("role", ["archive", "process"])
def test_missing_ledger_refuses_without_recreating_or_repeating_effect(
    tmp_path: Path, role: str
) -> None:
    effect = tmp_path / "effects.txt"
    calls = []

    class External:
        def process(self, item: fp.WorkItem) -> fp.Result:
            calls.append(item.processing_key)
            with effect.open("a") as stream:
                stream.write("synthetic effect\n")
            return fp.Completed("synthetic append committed")

    external = External()
    deliver(tmp_path, "first", b"same content")
    fp.archive(tmp_path, IDENTITY)
    assert fp.process(tmp_path, IDENTITY, external).completed == 1
    assert query(tmp_path, "SELECT state FROM work")[0][0] == "DONE"
    ledger = tmp_path / "ledger.sqlite3"
    ledger.unlink()
    deliver(tmp_path, "duplicate", b"same content")
    with pytest.raises(fp.ProtocolError, match="missing ledger"):
        if role == "archive":
            fp.archive(tmp_path, IDENTITY)
        else:
            fp.process(tmp_path, IDENTITY, external)
    assert not ledger.exists()
    assert len(calls) == 1 and effect.read_text() == "synthetic effect\n"
    assert (tmp_path / "inbox" / "duplicate").read_bytes() == b"same content"


def test_inbox_only_initial_root_remains_valid(tmp_path: Path) -> None:
    deliver(tmp_path, "first", b"input\n")
    assert {path.name for path in tmp_path.iterdir()} == {"inbox"}
    assert fp.process(tmp_path, IDENTITY, handler(tmp_path)).selected == 0
    assert fp.archive(tmp_path, IDENTITY).completed == 1
    assert fp.process(tmp_path, IDENTITY, handler(tmp_path)).completed == 1


@pytest.mark.parametrize("order,first", [("oldest", "a"), ("newest", "c")])
def test_archive_order_admits_every_delivery_before_selection(
    tmp_path: Path, order: str, first: str
) -> None:
    for name in ("c", "a", "b"):
        deliver(tmp_path, name, name.encode())
        os.utime(tmp_path / "inbox" / name, (1000 - ord(name), 1000 - ord(name)))
    report = fp.archive(tmp_path, IDENTITY, fp.Policy(order, "one"))
    receipts = query(tmp_path, "SELECT * FROM receipts ORDER BY seq")
    assert [row["name"] for row in receipts] == ["a", "b", "c"]
    assert [row["name"] for row in receipts if row["state"] == "FILE_ARCHIVED"] == [first]
    assert report.selected == 1 and report.remaining == 2
    assert fp.archive(tmp_path, IDENTITY, fp.Policy(order, "all")).completed == 2


@pytest.mark.parametrize("order,first", [("oldest", 1), ("newest", 3)])
def test_process_order_and_duplicate_age(tmp_path: Path, order: str, first: int) -> None:
    for name in ("a", "b", "c"):
        deliver(tmp_path, name, name.encode())
    fp.archive(tmp_path, IDENTITY)
    before = [tuple(row) for row in query(tmp_path, "SELECT * FROM work ORDER BY receipt_seq")]
    deliver(tmp_path, "z-duplicate-a", b"a")
    fp.archive(tmp_path, IDENTITY)
    assert [
        tuple(row) for row in query(tmp_path, "SELECT * FROM work ORDER BY receipt_seq")
    ] == before
    seen = []

    class Record:
        def process(self, item: fp.WorkItem) -> fp.Result:
            seen.append(item.receipt_sequence)
            return fp.Completed("synthetic receipt")

    report = fp.process(tmp_path, IDENTITY, Record(), fp.Policy(order, "one"))
    assert seen == [first] and report.remaining == 2
    report = fp.process(tmp_path, IDENTITY, Record(), fp.Policy(order, "all"))
    assert report.completed == 2
    assert seen == ([1, 2, 3] if order == "oldest" else [3, 2, 1])


def test_mixed_archive_order_keeps_creating_duplicate_receipt_sequence(tmp_path: Path) -> None:
    for name, data in (("a", b"duplicate"), ("b", b"other"), ("c", b"duplicate")):
        deliver(tmp_path, name, data)
    fp.archive(tmp_path, IDENTITY, fp.Policy("newest", "one"))
    created = tuple(query(tmp_path, "SELECT * FROM work")[0])
    assert fp.archive(tmp_path, IDENTITY, fp.Policy("oldest", "all")).duplicates == 1
    rows = query(tmp_path, "SELECT * FROM work ORDER BY receipt_seq")
    assert tuple(rows[1]) == created
    seen = []

    class Record:
        def process(self, item: fp.WorkItem) -> fp.Result:
            seen.append((item.receipt_sequence, item.metadata["delivery_name"]))
            return fp.Completed("synthetic evidence")

    assert fp.process(tmp_path, IDENTITY, Record(), fp.Policy("oldest", "all")).completed == 2
    assert seen == [(2, "b"), (3, "c")]


@pytest.mark.parametrize("stage", ["object_published", "archive_committed", "todo_published"])
def test_archive_crash_boundaries_converge(tmp_path: Path, stage: str) -> None:
    deliver(tmp_path, "delivery", b"bytes\n")
    script = (
        "import os,sys; from pathlib import Path; from dsl41.file_processing import archive; "
        "archive(Path(sys.argv[1]),'counts-v1', "
        "failpoint=lambda name: os._exit(31) if name == sys.argv[2] else None)"
    )
    child = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), stage],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert child.returncode == 31, child.stderr
    assert (tmp_path / "inbox" / "delivery").exists()
    assert len(published(tmp_path / "objects")) == 1
    receipt = query(tmp_path, "SELECT * FROM receipts")[0]
    assert receipt["state"] == ("FILE_RECEIVED" if stage == "object_published" else "FILE_ARCHIVED")
    assert bool(published(tmp_path / "todo")) == (stage == "todo_published")
    fp.archive(tmp_path, IDENTITY)
    assert not list((tmp_path / "inbox").iterdir())
    assert len(query(tmp_path, "SELECT * FROM receipts")) == 1
    assert fp.process(tmp_path, IDENTITY, handler(tmp_path)).completed == 1
    assert len(list((tmp_path / "output").glob("*.json"))) == 1


def test_archive_repairs_missing_outbox_without_arrivals(tmp_path: Path) -> None:
    deliver(tmp_path, "delivery", b"data")
    fp.archive(tmp_path, IDENTITY)
    published(tmp_path / "todo")[0].unlink()
    (tmp_path / "objects" / fp.STAGE / "object-orphan").write_bytes(b"incomplete")
    (tmp_path / "todo" / fp.STAGE / "todo-orphan").write_bytes(b"incomplete")
    assert fp.archive(tmp_path, IDENTITY).selected == 0
    assert not list((tmp_path / "objects" / fp.STAGE).iterdir())
    assert not list((tmp_path / "todo" / fp.STAGE).iterdir())
    assert fp.process(tmp_path, IDENTITY, handler(tmp_path)).completed == 1


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_recovery_retains_source_when_committed_object_is_inconsistent(
    tmp_path: Path, damage: str
) -> None:
    data = b"last correct copy\n"
    deliver(tmp_path, "delivery", data)

    def crash(stage: str) -> None:
        if stage == "archive_committed":
            raise SystemExit("receipt committed; TODO not published")

    with pytest.raises(SystemExit):
        fp.archive(tmp_path, IDENTITY, failpoint=crash)
    obj = tmp_path / "objects" / hashlib.sha256(data).hexdigest()
    assert obj.read_bytes() == data
    if damage == "missing":
        obj.unlink()
    else:
        obj.chmod(0o644)
        obj.write_bytes(b"corrupt")
    calls = []

    class Record:
        def process(self, item: fp.WorkItem) -> fp.Result:
            calls.append(item.processing_key)
            return fp.Completed("must not execute")

    with pytest.raises(fp.ProtocolError, match="archived object"):
        fp.archive(tmp_path, IDENTITY)
    assert (tmp_path / "inbox" / "delivery").read_bytes() == data
    assert query(tmp_path, "SELECT state,cleaned FROM receipts")[0][:] == ("FILE_ARCHIVED", 0)
    with pytest.raises(fp.ProtocolError, match="archived object"):
        fp.process(tmp_path, IDENTITY, Record())
    assert calls == []
    if damage == "missing":
        assert not obj.exists()
    else:
        assert obj.read_bytes() == b"corrupt"


def test_processor_can_finish_before_crashed_archiver_cleans_source(tmp_path: Path) -> None:
    deliver(tmp_path, "delivery", b"data\n")

    def crash(stage: str) -> None:
        if stage == "todo_published":
            raise SystemExit("handover published; source retained")

    with pytest.raises(SystemExit):
        fp.archive(tmp_path, IDENTITY, failpoint=crash)
    assert fp.process(tmp_path, IDENTITY, handler(tmp_path)).completed == 1
    assert (tmp_path / "inbox" / "delivery").exists()
    original = tuple(query(tmp_path, "SELECT * FROM work")[0])
    assert fp.archive(tmp_path, IDENTITY).selected == 0
    assert not list((tmp_path / "inbox").iterdir())
    assert not published(tmp_path / "todo")
    assert tuple(query(tmp_path, "SELECT * FROM work")[0]) == original


def test_bounded_snapshots_leave_arrivals_for_next_invocation(tmp_path: Path) -> None:
    deliver(tmp_path, "a", b"a")

    def arrival(stage: str) -> None:
        if stage == "object_published":
            deliver(tmp_path, "b", b"b")

    assert fp.archive(tmp_path, IDENTITY, failpoint=arrival).selected == 1
    assert [path.name for path in (tmp_path / "inbox").iterdir()] == ["b"]

    class AddsWork:
        def process(self, item: fp.WorkItem) -> fp.Result:
            fp.archive(tmp_path, IDENTITY)
            return handler(tmp_path).process(item)

    report = fp.process(tmp_path, IDENTITY, AddsWork())
    assert report.selected == 1 and report.remaining == 1
    assert fp.process(tmp_path, IDENTITY, handler(tmp_path)).completed == 1


def test_interrupted_running_reconciles_actual_sample_output_once(tmp_path: Path) -> None:
    deliver(tmp_path, "a", b"line\n")
    fp.archive(tmp_path, IDENTITY)

    class Crash(CountsHandler):
        def process(self, item: fp.WorkItem) -> fp.Result:
            assert isinstance(super().process(item), fp.Completed)
            raise SystemExit("output published, completion not committed")

    with pytest.raises(SystemExit):
        fp.process(tmp_path, IDENTITY, Crash(tmp_path / "output"))
    assert query(tmp_path, "SELECT state FROM work")[0][0] == "RUNNING"
    target = next((tmp_path / "output").glob("*.json"))
    original = target.stat().st_ino, target.read_bytes()
    report = fp.process(tmp_path, IDENTITY, handler(tmp_path), fp.Policy(batch="one"))
    assert (report.selected, report.reconciled, report.completed) == (1, 1, 1)
    assert (target.stat().st_ino, target.read_bytes()) == original
    assert query(tmp_path, "SELECT state FROM work")[0][0] == "DONE"
    assert not published(tmp_path / "todo")


def test_unknown_external_effect_never_silently_retries(tmp_path: Path) -> None:
    deliver(tmp_path, "a", b"a")
    deliver(tmp_path, "b", b"b")
    fp.archive(tmp_path, IDENTITY)
    calls = []

    class External:
        def process(self, item: fp.WorkItem) -> fp.Result:
            calls.append(item.processing_key)
            raise RuntimeError("effect may have happened")

    external = External()
    assert fp.process(tmp_path, IDENTITY, external).unknown == 1
    assert fp.process(tmp_path, IDENTITY, external).unknown == 1
    assert len(calls) == 1

    class ExplicitRetry(External):
        def reconcile(self, item: fp.WorkItem) -> fp.Result:
            return fp.RetrySafe("synthetic independent proof of non-delivery")

    report = fp.process(tmp_path, IDENTITY, ExplicitRetry(), fp.Policy(batch="one"))
    assert report.selected == report.reconciled == 1 and report.completed == 0
    assert len(calls) == 1
    assert [row[0] for row in query(tmp_path, "SELECT state FROM work ORDER BY receipt_seq")] == [
        "READY",
        "READY",
    ]
    fp.process(tmp_path, IDENTITY, external, fp.Policy(batch="one"))
    assert len(calls) == 2


def test_explicit_known_safe_failure_stops_batch(tmp_path: Path) -> None:
    for name in ("a", "b"):
        deliver(tmp_path, name, name.encode())
    fp.archive(tmp_path, IDENTITY)

    class SafeFailure:
        def process(self, item: fp.WorkItem) -> fp.Result:
            return fp.RetrySafe("validation failed before any public effect")

    report = fp.process(tmp_path, IDENTITY, SafeFailure())
    assert report.selected == 1 and report.remaining == 2 and report.stopped
    assert all(row[0] == "READY" for row in query(tmp_path, "SELECT state FROM work"))


@pytest.mark.parametrize("role", ["archive", "process"])
def test_os_lock_overlap_and_release_on_process_death(tmp_path: Path, role: str) -> None:
    fp.archive(tmp_path, IDENTITY)
    script = (
        "import os,sys; from pathlib import Path; from dsl41.file_processing import _lock; "
        "lock=_lock(Path(sys.argv[1]),sys.argv[2]); lock.__enter__(); "
        "print('locked',flush=True); sys.stdin.readline(); os._exit(19)"
    )
    with subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path), role],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as child:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(fp.LockBusy):
            if role == "archive":
                fp.archive(tmp_path, IDENTITY)
            else:
                fp.process(tmp_path, IDENTITY, handler(tmp_path))
        if role == "archive":
            assert fp.process(tmp_path, IDENTITY, handler(tmp_path)).selected == 0
        else:
            assert fp.archive(tmp_path, IDENTITY).selected == 0
        child.communicate("exit\n", timeout=10)
        assert child.returncode == 19
    assert fp.archive(tmp_path, IDENTITY).selected == 0
    assert fp.process(tmp_path, IDENTITY, handler(tmp_path)).selected == 0


@pytest.mark.parametrize("tamper", ["malformed", "duplicate", "identity", "sequence", "path"])
def test_malformed_or_tampered_todo_refused(tmp_path: Path, tamper: str) -> None:
    deliver(tmp_path, "a", b"a")
    fp.archive(tmp_path, IDENTITY)
    target = published(tmp_path / "todo")[0]
    value = json.loads(target.read_text())
    if tamper == "malformed":
        target.write_text("{")
    elif tamper == "duplicate":
        target.write_text(target.read_text().replace('"schema":1', '"schema":1,"schema":1'))
    else:
        value[
            {"identity": "processing_key", "sequence": "receipt_sequence", "path": "content_hash"}[
                tamper
            ]
        ] = {"identity": "0" * 64, "sequence": 999, "path": "../../elsewhere"}[tamper]
        target.write_text(json.dumps(value))
    with pytest.raises(fp.ProtocolError):
        fp.process(tmp_path, IDENTITY, handler(tmp_path))
    assert query(tmp_path, "SELECT state FROM work")[0][0] == "READY"
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("damage", ["wrong", "missing", "symlink"])
def test_input_object_integrity_before_callback(tmp_path: Path, damage: str) -> None:
    deliver(tmp_path, "a", b"original")
    fp.archive(tmp_path, IDENTITY)
    obj = published(tmp_path / "objects")[0]
    if damage == "wrong":
        obj.chmod(0o644)
        obj.write_bytes(b"wrong")
    else:
        obj.unlink()
        if damage == "symlink":
            other = tmp_path / "other"
            other.write_bytes(b"original")
            obj.symlink_to(other)
    with pytest.raises((fp.ProtocolError, OSError)):
        fp.process(tmp_path, IDENTITY, handler(tmp_path))
    assert query(tmp_path, "SELECT state FROM work")[0][0] == "READY"
    assert not (tmp_path / "output").exists()


def test_corrupt_reused_object_and_changed_identity_refused(tmp_path: Path) -> None:
    deliver(tmp_path, "a", b"data")
    fp.archive(tmp_path, IDENTITY)
    obj = published(tmp_path / "objects")[0]
    obj.chmod(0o644)
    obj.write_bytes(b"bad")
    deliver(tmp_path, "b", b"data")
    with pytest.raises(fp.ProtocolError, match="corrupt existing object"):
        fp.archive(tmp_path, IDENTITY)
    assert (tmp_path / "inbox" / "b").exists()
    with pytest.raises(fp.ProtocolError, match="different processor identity"):
        fp.archive(tmp_path, "counts-v2")
    with pytest.raises(fp.ProtocolError, match="different processor identity"):
        fp.process(tmp_path, "counts-v2", handler(tmp_path))


@pytest.mark.parametrize("entry", ["symlink", "directory", "fifo"])
def test_nonregular_inbox_refused(tmp_path: Path, entry: str) -> None:
    (tmp_path / "inbox").mkdir()
    target = tmp_path / "inbox" / "delivery"
    if entry == "directory":
        target.mkdir()
    elif entry == "fifo":
        os.mkfifo(target)
    else:
        (tmp_path / "outside").write_bytes(b"bytes")
        target.symlink_to(tmp_path / "outside")
    with pytest.raises(fp.ProtocolError, match="not a regular file"):
        fp.archive(tmp_path, IDENTITY)


def test_reused_delivery_basename_is_refused(tmp_path: Path) -> None:
    deliver(tmp_path, "a", b"one")
    fp.archive(tmp_path, IDENTITY)
    deliver(tmp_path, "a", b"two")
    with pytest.raises(fp.ProtocolError, match="basename reused"):
        fp.archive(tmp_path, IDENTITY)


def test_stale_archiver_read_cannot_republish_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deliver(tmp_path, "a", b"data")
    fp.archive(tmp_path, IDENTITY)
    original = fp._publish_todo
    called = False

    def complete_before_publish(
        root: Path, db: sqlite3.Connection, identity: str, key: str, **seam: fp.Failpoint
    ) -> None:
        nonlocal called
        if not called:
            called = True
            assert fp.process(root, identity, handler(root)).completed == 1
        original(root, db, identity, key, **seam)

    monkeypatch.setattr(fp, "_publish_todo", complete_before_publish)
    fp.archive(tmp_path, IDENTITY)
    assert called and not published(tmp_path / "todo")
    assert fp.process(tmp_path, IDENTITY, handler(tmp_path)).selected == 0


def test_leftover_done_todo_cleaned_without_handler(tmp_path: Path) -> None:
    deliver(tmp_path, "a", b"a")
    fp.archive(tmp_path, IDENTITY)
    path = published(tmp_path / "todo")[0]
    todo = path.read_bytes()
    fp.process(tmp_path, IDENTITY, handler(tmp_path))
    path.write_bytes(todo)
    assert fp.process(tmp_path, IDENTITY, handler(tmp_path)).selected == 0
    assert not path.exists()
    path.write_bytes(todo)
    fp.archive(tmp_path, IDENTITY)
    assert not path.exists()


def test_archiver_can_remove_done_todo_after_processor_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deliver(tmp_path, "a", b"a")
    fp.archive(tmp_path, IDENTITY)
    path = published(tmp_path / "todo")[0]
    todo = path.read_bytes()
    fp.process(tmp_path, IDENTITY, handler(tmp_path))
    path.write_bytes(todo)
    original = Path.iterdir
    raced = False

    def snapshot(directory: Path):
        nonlocal raced
        entries = list(original(directory))
        if directory == tmp_path / "todo" and not raced:
            raced = True
            fp.archive(tmp_path, IDENTITY)
        return iter(entries)

    monkeypatch.setattr(Path, "iterdir", snapshot)
    assert fp.process(tmp_path, IDENTITY, handler(tmp_path)).selected == 0
    assert raced and not path.exists()


def test_sample_reconciliation_checks_identity_and_content(tmp_path: Path) -> None:
    deliver(tmp_path, "a", b"[1,2,3]\n")
    fp.archive(tmp_path, IDENTITY)
    row = query(tmp_path, "SELECT * FROM work")[0]
    item = fp._item(tmp_path, row, IDENTITY)
    sample = handler(tmp_path)
    assert isinstance(sample.reconcile(item), fp.RetrySafe)
    assert isinstance(sample.process(item), fp.Completed)
    target = next((tmp_path / "output").glob("*.json"))
    data = json.loads(target.read_text())
    assert data["count"] == 3 and data["format"] == "json"
    target.chmod(0o644)
    data["processing_key"] = "0" * 64
    target.write_text(json.dumps(data))
    assert isinstance(sample.reconcile(item), fp.Unknown)
    assert isinstance(sample.reconcile(replace(item, content_hash="0" * 64)), fp.Unknown)


def test_invalid_policy_before_io(tmp_path: Path) -> None:
    root = tmp_path / "absent"
    policy = fp.Policy()
    object.__setattr__(policy, "order", "invalid")
    with pytest.raises(ValueError, match="policy"):
        fp.archive(root, IDENTITY, policy)
    with pytest.raises(ValueError, match="policy"):
        fp.process(root, IDENTITY, handler(root), policy)
    assert not root.exists()


def test_charts_choose_deterministic_persisted_states() -> None:
    assert well_formed(ARCHIVE) == []
    assert well_formed(PROCESS) == []
    assert state_after(ARCHIVE) == "FILE_RECEIVED"
    assert state_after(ARCHIVE, "FILE_RECEIVED", "archive_committed") == "FILE_ARCHIVED"
    assert state_after(PROCESS) == "READY"
    for row in PROCESS.rows:
        assert state_after(PROCESS, row.source, row.event) == row.target
    with pytest.raises(StatechartError):
        state_after(PROCESS, "READY", "completed")
    with pytest.raises(ValueError, match="no move"):
        state_after(PROCESS, "DONE", "claim")


def test_synthetic_demo_counts_and_output_identity(tmp_path: Path) -> None:
    report = run(tmp_path)
    assert (report["receipts"], report["objects"], report["done"], report["todo"]) == (4, 3, 3, 0)
    for obj in published(tmp_path / "objects"):
        assert hashlib.sha256(obj.read_bytes()).hexdigest() == obj.name


def test_worker_import_stays_independent_of_compiler_and_scheduler(tmp_path: Path) -> None:
    script = (
        "import sys; import dsl41.file_processing; import examples.file_processing.worker; "
        "forbidden={'dsl41.dsl','dsl41.ir','dsl41.conditions','dsl41.oracle',"
        "'dsl41.runner_scheduler','dsl41.file_processing_dsl'}; "
        "assert not forbidden.intersection(sys.modules)"
    )
    child = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=15
    )
    assert child.returncode == 0, child.stderr
