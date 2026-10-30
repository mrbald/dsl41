"""Processing contract probes: REJECTED, the decision record, the attempt counter and the
operator interface (CONTRACT.md "Failure disposition is `stop`"). Effects live under tmp_path.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from file_processing_checker import check_counts_outputs, expected_output, record

from dsl41 import file_processing as fp
from dsl41.file_processing_charts import PROCESS, state_after
from examples.file_processing.exits import EXIT_LOCK_BUSY, EXIT_REFUSED, EXIT_STOPPED
from examples.file_processing.worker import CountsHandler

IDENTITY = "counts-v1"
REPO = Path(__file__).resolve().parents[1]
TOXIC = b"[1,2"


def query(root: Path, sql: str, *args: object) -> list[sqlite3.Row]:
    db = sqlite3.connect(root / "ledger.sqlite3")
    db.row_factory = sqlite3.Row
    try:
        return db.execute(sql, args).fetchall()
    finally:
        db.close()


def deliver(root: Path, name: str, data: bytes) -> None:
    (root / "inbox").mkdir(parents=True, exist_ok=True)
    (root / "inbox" / name).write_bytes(data)


def handler(root: Path) -> CountsHandler:
    return CountsHandler(root / "output")


def key_of(data: bytes) -> str:
    return fp.processing_key(IDENTITY, hashlib.sha256(data).hexdigest())


def todo(root: Path, data: bytes) -> Path:
    return root / "todo" / f"{key_of(data)}.json"


def work(root: Path, data: bytes) -> tuple[str, int]:
    row = query(root, "SELECT state, attempt FROM work WHERE work_key=?", key_of(data))[0]
    return row["state"], row["attempt"]


def moves(root: Path, data: bytes) -> list[tuple[str, str]]:
    return [(row["chart_row"], row["origin"]) for row in record(root).get(key_of(data), [])]


def worker_cli(root: Path, role: str) -> subprocess.CompletedProcess[str]:
    module = "examples.file_processing.worker"
    return subprocess.run(
        [sys.executable, "-m", module, role, "--root", str(root)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=30,
    )


def operator_cli(
    root: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    module = "examples.file_processing.operate"
    return subprocess.run(
        [sys.executable, "-m", module, "--root", str(root), *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


class Raises:
    def process(self, item: fp.WorkItem) -> fp.Result:
        raise RuntimeError("effect may have happened")


def unknown_row(root: Path, data: bytes = TOXIC) -> None:
    """An admitted item whose handler raised: UNKNOWN at attempt 1, TODO kept."""
    deliver(root, "toxic", data)
    fp.archive(root, IDENTITY)
    callback = handler(root) if data == TOXIC else Raises()
    assert fp.process(root, IDENTITY, callback).unknown == 1
    assert work(root, data) == ("UNKNOWN", 1)


def reject(root: Path, data: bytes, state: str, attempt: int) -> fp.Decision:
    return fp.rule_work(
        root,
        IDENTITY,
        key_of(data),
        "rejected",
        expect_state=state,
        expect_attempt=attempt,
        actor="op",
        reason="toxic input",
    )


def test_chart_declares_rejected_as_an_operator_only_terminal() -> None:
    assert fp.TERMINAL == {"DONE", "REJECTED"}
    assert state_after(PROCESS, "READY", "rejected") == "REJECTED"
    assert state_after(PROCESS, "UNKNOWN", "rejected") == "REJECTED"
    assert {(x.state, x.event) for x in PROCESS.exclusions} >= {("RUNNING", "rejected")}
    with pytest.raises(ValueError, match="no move"):
        state_after(PROCESS, "REJECTED", "retry_safe")


@pytest.mark.parametrize("prior", ["READY", "UNKNOWN"])
@pytest.mark.parametrize("role", ["archive", "process"])
def test_rejection_commits_before_todo_removal_and_either_worker_finishes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prior: str, role: str
) -> None:
    if prior == "UNKNOWN":
        unknown_row(tmp_path)
    else:
        deliver(tmp_path, "toxic", TOXIC)
        fp.archive(tmp_path, IDENTITY)
    attempt = work(tmp_path, TOXIC)[1]

    def interrupted(*_: object) -> None:
        raise SystemExit("rejection committed; TODO not removed")

    monkeypatch.setattr(fp, "_remove_todo", interrupted)
    with pytest.raises(SystemExit):
        reject(tmp_path, TOXIC, prior, attempt)
    monkeypatch.undo()
    assert work(tmp_path, TOXIC) == ("REJECTED", attempt)
    assert moves(tmp_path, TOXIC)[-1] == (
        "file_process.08" if prior == "READY" else "file_process.09",
        "operator",
    )
    assert todo(tmp_path, TOXIC).exists()
    if role == "archive":
        assert fp.archive(tmp_path, IDENTITY).selected == 0
    else:
        report = fp.process(tmp_path, IDENTITY, handler(tmp_path))
        assert (report.selected, report.remaining, report.unknown) == (0, 0, 0)
    assert not todo(tmp_path, TOXIC).exists()
    assert work(tmp_path, TOXIC) == ("REJECTED", attempt)
    check_counts_outputs(tmp_path)


def test_repair_never_republishes_a_rejected_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deliver(tmp_path, "toxic", TOXIC)
    fp.archive(tmp_path, IDENTITY)
    todo(tmp_path, TOXIC).unlink()
    original = fp._publish_todo
    called = False

    def reject_before_publish(
        root: Path, db: sqlite3.Connection, identity: str, key: str, **seam: fp.Failpoint
    ) -> None:
        nonlocal called
        if not called:
            called = True
            reject(root, TOXIC, "READY", 0)
        original(root, db, identity, key, **seam)

    monkeypatch.setattr(fp, "_publish_todo", reject_before_publish)
    assert fp.archive(tmp_path, IDENTITY).selected == 0
    monkeypatch.undo()
    assert called and not todo(tmp_path, TOXIC).exists()
    assert fp.archive(tmp_path, IDENTITY).selected == 0
    assert not todo(tmp_path, TOXIC).exists()


def test_selection_and_remaining_exclude_rejected_work(tmp_path: Path) -> None:
    unknown_row(tmp_path)
    deliver(tmp_path, "good", b"one\n")
    fp.archive(tmp_path, IDENTITY)
    blocked = fp.process(tmp_path, IDENTITY, handler(tmp_path))
    assert (blocked.selected, blocked.completed, blocked.stopped) == (1, 0, True)
    assert work(tmp_path, b"one\n") == ("READY", 0)
    assert work(tmp_path, TOXIC) == ("READY", 1)
    reject(tmp_path, TOXIC, "READY", 1)
    report = fp.process(tmp_path, IDENTITY, handler(tmp_path))
    assert (report.selected, report.completed, report.remaining, report.unknown) == (1, 1, 0, 0)
    assert fp.process(tmp_path, IDENTITY, handler(tmp_path)).selected == 0
    check_counts_outputs(tmp_path)


def test_duplicate_delivery_preserves_rejection_and_is_cleaned(tmp_path: Path) -> None:
    unknown_row(tmp_path)
    reject(tmp_path, TOXIC, "UNKNOWN", 1)
    before = [tuple(row) for row in query(tmp_path, "SELECT * FROM work")]
    decisions = [tuple(row) for row in query(tmp_path, "SELECT * FROM decisions")]
    deliver(tmp_path, "toxic-again", TOXIC)
    report = fp.archive(tmp_path, IDENTITY)
    assert (report.completed, report.duplicates) == (1, 1)
    assert not list((tmp_path / "inbox").iterdir())
    assert not todo(tmp_path, TOXIC).exists()
    assert [tuple(row) for row in query(tmp_path, "SELECT * FROM work")] == before
    assert [tuple(row) for row in query(tmp_path, "SELECT * FROM decisions")] == decisions
    assert [tuple(row) for row in query(tmp_path, "SELECT cleaned FROM receipts")] == [(1,), (1,)]
    check_counts_outputs(tmp_path)


def test_duplicate_of_rejected_work_is_kept_while_its_object_is_corrupt(tmp_path: Path) -> None:
    unknown_row(tmp_path)
    reject(tmp_path, TOXIC, "UNKNOWN", 1)
    obj = tmp_path / "objects" / hashlib.sha256(TOXIC).hexdigest()
    obj.chmod(0o644)
    obj.write_bytes(b"corrupt")
    deliver(tmp_path, "toxic-again", TOXIC)
    with pytest.raises(fp.ProtocolError, match="corrupt existing object"):
        fp.archive(tmp_path, IDENTITY)
    assert (tmp_path / "inbox" / "toxic-again").read_bytes() == TOXIC


@pytest.mark.parametrize("ruling", ["processor", "operator"])
def test_stale_running_is_normalized_before_its_missing_object_is_read(
    tmp_path: Path, ruling: str
) -> None:
    deliver(tmp_path, "a", b"line\n")
    fp.archive(tmp_path, IDENTITY)

    def crash(stage: str) -> None:
        if stage == "claimed":
            raise SystemExit("claim committed; handler not called")

    with pytest.raises(SystemExit):
        fp.process(tmp_path, IDENTITY, handler(tmp_path), failpoint=crash)
    assert work(tmp_path, b"line\n") == ("RUNNING", 1)
    (tmp_path / "objects" / hashlib.sha256(b"line\n").hexdigest()).unlink()
    if ruling == "processor":
        with pytest.raises(fp.ProtocolError, match="missing or unreadable archived object"):
            fp.process(tmp_path, IDENTITY, handler(tmp_path))
        origin = "reconciler"
    else:
        fp.rule_work(
            tmp_path,
            IDENTITY,
            key_of(b"line\n"),
            "uncertain",
            expect_state="RUNNING",
            expect_attempt=1,
            actor="op",
            reason="claim of a dead run",
        )
        origin = "operator"
    assert work(tmp_path, b"line\n") == ("UNKNOWN", 1)
    assert moves(tmp_path, b"line\n") == [
        ("file_process.01", "handler"),
        ("file_process.04", origin),
    ]
    assert fp.list_work(tmp_path, IDENTITY)[0].object == "missing"
    reject(tmp_path, b"line\n", "UNKNOWN", 1)
    assert not todo(tmp_path, b"line\n").exists()
    assert fp.process(tmp_path, IDENTITY, handler(tmp_path)).selected == 0


def test_attempt_counts_claims_only_and_reaches_the_handler(tmp_path: Path) -> None:
    deliver(tmp_path, "a", b"a\n")
    fp.archive(tmp_path, IDENTITY)
    seen: list[int] = []

    class Twice:
        def process(self, item: fp.WorkItem) -> fp.Result:
            seen.append(item.attempt)
            if len(seen) == 1:
                return fp.RetrySafe("validation failed before any effect")
            raise RuntimeError("effect may have happened")

        def reconcile(self, item: fp.WorkItem) -> fp.Result:
            seen.append(-item.attempt)
            return fp.Completed("independent proof")

    assert fp.process(tmp_path, IDENTITY, Twice()).stopped
    assert work(tmp_path, b"a\n") == ("READY", 1)
    assert fp.process(tmp_path, IDENTITY, Twice()).unknown == 1
    assert work(tmp_path, b"a\n") == ("UNKNOWN", 2)
    assert fp.process(tmp_path, IDENTITY, Twice()).completed == 1
    assert work(tmp_path, b"a\n") == ("DONE", 2)
    assert seen == [1, 2, -2]
    rows = query(tmp_path, "SELECT attempt, chart_row, origin, reason, evidence FROM decisions")
    assert [tuple(row) for row in rows] == [
        (1, "file_process.01", "handler", None, None),
        (1, "file_process.03", "handler", "validation failed before any effect", None),
        (2, "file_process.01", "handler", None, None),
        (2, "file_process.04", "handler", "RuntimeError: effect may have happened", None),
        (2, "file_process.05", "reconciler", None, "independent proof"),
    ]


def test_decision_record_is_append_only_and_outlives_the_todo(tmp_path: Path) -> None:
    deliver(tmp_path, "a", b"a\n")
    fp.archive(tmp_path, IDENTITY)
    fp.process(tmp_path, IDENTITY, handler(tmp_path))
    assert not todo(tmp_path, b"a\n").exists()
    rows = record(tmp_path)[key_of(b"a\n")]
    assert [(row["prior_state"], row["state"]) for row in rows] == [
        ("READY", "RUNNING"),
        ("RUNNING", "DONE"),
    ]
    assert all(row["actor"] == IDENTITY for row in rows)
    assert all(datetime.fromisoformat(row["decided_at"]).tzinfo == UTC for row in rows)
    evidence = json.loads(rows[-1]["evidence"])
    assert evidence["output"] == str(tmp_path / "output" / f"{key_of(b'a\n')}.json")
    db = sqlite3.connect(tmp_path / "ledger.sqlite3")
    try:
        for sql in ("UPDATE decisions SET reason='x'", "DELETE FROM decisions"):
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                db.execute(sql)
    finally:
        db.close()
    assert len(record(tmp_path)[key_of(b"a\n")]) == 2
    check_counts_outputs(tmp_path)


def test_claim_is_durable_before_the_handler_runs(tmp_path: Path) -> None:
    """CONTRACT.md invariant 5, read through a second connection inside the handler."""
    deliver(tmp_path, "a", b"a\n")
    fp.archive(tmp_path, IDENTITY)
    observed: list[tuple[object, ...]] = []

    class Observe(CountsHandler):
        def process(self, item: fp.WorkItem) -> fp.Result:
            observed.append(work(tmp_path, b"a\n"))
            observed.append(moves(tmp_path, b"a\n")[-1])
            return super().process(item)

    assert fp.process(tmp_path, IDENTITY, Observe(tmp_path / "output")).completed == 1
    assert observed == [("RUNNING", 1), ("file_process.01", "handler")]
    check_counts_outputs(tmp_path)


@pytest.mark.parametrize("reconciler", ["none", "unknown", "retry_safe"])
def test_unknown_work_never_reaches_process(tmp_path: Path, reconciler: str) -> None:
    """CONTRACT.md invariant 8: a reconciliation never calls process in the same run."""
    deliver(tmp_path, "a", b"a\n")
    fp.archive(tmp_path, IDENTITY)
    calls: list[str] = []

    class Counted(Raises):
        def process(self, item: fp.WorkItem) -> fp.Result:
            calls.append("process")
            return super().process(item)

    class Reconciles(Counted):
        def reconcile(self, item: fp.WorkItem) -> fp.Result:
            calls.append("reconcile")
            if reconciler == "unknown":
                return fp.Unknown("still unproven")
            return fp.RetrySafe("independent proof of no effect")

    callback = Counted() if reconciler == "none" else Reconciles()
    assert fp.process(tmp_path, IDENTITY, callback).unknown == 1
    for _ in range(2):
        report = fp.process(tmp_path, IDENTITY, callback, fp.Policy(batch="all"))
        assert report.stopped and report.reconciled == 1
        if reconciler == "retry_safe":
            break
    assert calls.count("process") == 1
    expected = "READY" if reconciler == "retry_safe" else "UNKNOWN"
    assert work(tmp_path, b"a\n") == (expected, 1)
    check_counts_outputs(tmp_path)


def test_rulings_check_arguments_state_attempt_and_the_chart(tmp_path: Path) -> None:
    unknown_row(tmp_path)
    key = key_of(TOXIC)

    def rule(event: str, state: str, attempt: int, **given: str) -> fp.Decision:
        return fp.rule_work(
            tmp_path,
            IDENTITY,
            key,
            event,
            expect_state=state,
            expect_attempt=attempt,
            actor="op",
            **given,
        )

    before = [tuple(row) for row in query(tmp_path, "SELECT * FROM work")]
    for event, state, given in (
        ("completed", "UNKNOWN", {"reason": "no evidence"}),
        ("retry_safe", "UNKNOWN", {"evidence": "not a reason"}),
        ("rejected", "UNKNOWN", {}),
        ("claim", "READY", {"reason": "operators never claim"}),
        ("rejected", "RUNNING", {"reason": "convert first"}),
        ("completed", "RUNNING", {"evidence": "convert first"}),
        ("uncertain", "UNKNOWN", {"reason": "only a claim converts"}),
        ("completed", "READY", {"evidence": "the chart excludes it"}),
        ("rejected", "DONE", {"reason": "terminal"}),
        ("rejected", "BOGUS", {"reason": "undeclared"}),
    ):
        with pytest.raises(ValueError) as refused:
            rule(event, state, 1, **given)
        assert not isinstance(refused.value, fp.ProtocolError), (event, state)
    for state, attempt in (("READY", 1), ("UNKNOWN", 0), ("UNKNOWN", 2)):
        with pytest.raises(fp.ProtocolError, match="not .* at attempt"):
            rule("rejected", state, attempt, reason="stale view")
    assert [tuple(row) for row in query(tmp_path, "SELECT * FROM work")] == before
    decision = rule("completed", "UNKNOWN", 1, evidence="operator verified the effect")
    assert (decision.prior_state, decision.state, decision.chart_row) == (
        "UNKNOWN",
        "DONE",
        "file_process.05",
    )
    assert (decision.origin, decision.actor, decision.attempt) == ("operator", "op", 1)
    assert not todo(tmp_path, TOXIC).exists()
    with pytest.raises(AssertionError, match="no output"):
        check_counts_outputs(tmp_path)
    with pytest.raises(ValueError, match="chart moves no DONE"):
        rule("rejected", "DONE", 1, reason="terminal")


def test_operator_retry_safe_returns_unknown_work_to_the_queue(tmp_path: Path) -> None:
    unknown_row(tmp_path, b"a\n")
    calls: list[int] = []

    class Counts(CountsHandler):
        def process(self, item: fp.WorkItem) -> fp.Result:
            calls.append(item.attempt)
            return super().process(item)

    fp.rule_work(
        tmp_path,
        IDENTITY,
        key_of(b"a\n"),
        "retry_safe",
        expect_state="UNKNOWN",
        expect_attempt=1,
        actor="op",
        reason="checked downstream: no effect",
    )
    assert work(tmp_path, b"a\n") == ("READY", 1)
    assert todo(tmp_path, b"a\n").exists()
    assert fp.process(tmp_path, IDENTITY, Counts(tmp_path / "output")).completed == 1
    assert calls == [2]
    check_counts_outputs(tmp_path)


def test_ruling_takes_the_process_lock_and_reads_take_none(tmp_path: Path) -> None:
    unknown_row(tmp_path)
    with fp._lock(tmp_path, "process"):
        with pytest.raises(fp.LockBusy):
            reject(tmp_path, TOXIC, "UNKNOWN", 1)
        [status] = fp.list_work(tmp_path, IDENTITY)
        shown, decisions = fp.show_work(tmp_path, IDENTITY, key_of(TOXIC))
    assert status == shown
    assert (status.state, status.attempt, status.object, status.todo) == (
        "UNKNOWN",
        1,
        "verified",
        True,
    )
    assert [(r.name, r.cleaned) for r in status.receipts] == [("toxic", True)]
    assert status.latest == decisions[-1]
    assert [d.chart_row for d in decisions] == ["file_process.01", "file_process.04"]


def test_operator_reads_refuse_a_root_without_a_ledger(tmp_path: Path) -> None:
    with pytest.raises(fp.ProtocolError, match="no ledger"):
        fp.list_work(tmp_path, IDENTITY)
    assert not (tmp_path / "ledger.sqlite3").exists()


def test_operator_path_rejects_a_toxic_delivery_and_the_queue_moves_on(tmp_path: Path) -> None:
    """The sample reconciler finds no output, so the toxic head loops READY/UNKNOWN."""
    deliver(tmp_path, "a-toxic", TOXIC)
    deliver(tmp_path, "b-good", b"[1,2]\n")
    assert worker_cli(tmp_path, "archive").returncode == 0
    unknown = []
    for _ in range(3):
        stopped = worker_cli(tmp_path, "process")
        assert stopped.returncode == EXIT_STOPPED, stopped.stderr
        unknown.append(json.loads(stopped.stdout)["unknown"])
    assert unknown == [1, 0, 1]
    listed = operator_cli(tmp_path, "list")
    assert listed.returncode == 0, listed.stderr
    rows = {row["work_key"]: row for row in json.loads(listed.stdout)}
    toxic, good = rows[key_of(TOXIC)], rows[key_of(b"[1,2]\n")]
    assert (toxic["state"], toxic["attempt"], toxic["todo"]) == ("UNKNOWN", 2, True)
    assert toxic["latest"]["chart_row"] == "file_process.04"
    assert (good["state"], good["attempt"]) == ("READY", 0)
    rule = ["rule", "--key", key_of(TOXIC), "--event", "rejected", "--expect-state", "UNKNOWN"]
    before = [tuple(row) for row in query(tmp_path, "SELECT * FROM decisions")]
    refused = operator_cli(tmp_path, *rule, "--expect-attempt", "1", "--reason", "stale view")
    assert (refused.returncode, refused.stdout) == (EXIT_REFUSED, "")
    assert refused.stderr.startswith("rule refused: ") and len(refused.stderr.splitlines()) == 1
    assert [tuple(row) for row in query(tmp_path, "SELECT * FROM decisions")] == before
    env = {**os.environ, "LOGNAME": "op-on-call"}
    ruled = operator_cli(tmp_path, *rule, "--expect-attempt", "2", "--reason", "toxic", env=env)
    assert ruled.returncode == 0, ruled.stderr
    decision = json.loads(ruled.stdout)
    assert (decision["state"], decision["origin"], decision["actor"]) == (
        "REJECTED",
        "operator",
        "op-on-call",
    )
    drained = worker_cli(tmp_path, "process")
    assert drained.returncode == 0, drained.stderr
    assert json.loads(drained.stdout)["completed"] == 1
    deliver(tmp_path, "c-toxic-again", TOXIC)
    archived = worker_cli(tmp_path, "archive")
    assert json.loads(archived.stdout)["duplicates"] == 1
    assert not list((tmp_path / "inbox").iterdir())
    shown = operator_cli(tmp_path, "show", "--key", key_of(TOXIC))
    assert shown.returncode == 0, shown.stderr
    view = json.loads(shown.stdout)
    assert view["work"]["state"] == "REJECTED" and not view["work"]["todo"]
    assert [r["cleaned"] for r in view["work"]["receipts"]] == [True, True]
    assert [(d["chart_row"], d["origin"]) for d in view["decisions"]] == [
        ("file_process.01", "handler"),
        ("file_process.04", "handler"),
        ("file_process.06", "reconciler"),
        ("file_process.01", "handler"),
        ("file_process.04", "handler"),
        ("file_process.09", "operator"),
    ]
    check_counts_outputs(tmp_path)


def test_operator_cli_reports_a_busy_process_lock(tmp_path: Path) -> None:
    unknown_row(tmp_path)
    with fp._lock(tmp_path, "process"):
        busy = operator_cli(
            tmp_path,
            *("rule", "--key", key_of(TOXIC), "--event", "rejected"),
            *("--expect-state", "UNKNOWN", "--expect-attempt", "1", "--reason", "toxic"),
        )
        listed = operator_cli(tmp_path, "list")
    assert (busy.returncode, busy.stdout) == (EXIT_LOCK_BUSY, "")
    assert busy.stderr.startswith("process.lock busy")
    assert listed.returncode == 0 and len(json.loads(listed.stdout)) == 1
    assert work(tmp_path, TOXIC) == ("UNKNOWN", 1)


def make_checker_fail(root: Path, defect: str) -> None:
    deliver(root, "a", b"a\n")
    deliver(root, "b", b"b\n")
    fp.archive(root, IDENTITY)
    fp.process(root, IDENTITY, handler(root), fp.Policy(batch="one"))
    done = root / "output" / f"{key_of(b'a\n')}.json"
    if defect == "stray":
        (root / "output" / "stray.json").write_bytes(b"{}")
    elif defect == "bytes":
        done.chmod(0o644)
        done.write_bytes(done.read_bytes().replace(b"1", b"2"))
    elif defect == "missing":
        done.unlink()
    elif defect == "bare_key":
        (root / "output" / key_of(b"a\n")).write_bytes(done.read_bytes())
    else:
        target = root / "output" / f"{key_of(b'b\n')}.json"
        target.write_bytes(
            expected_output(root, key_of(b"b\n"), hashlib.sha256(b"b\n").hexdigest())
        )


@pytest.mark.parametrize("defect", ["stray", "bytes", "missing", "bare_key", "unclaimed"])
def test_checker_catches_each_defect(tmp_path: Path, defect: str) -> None:
    make_checker_fail(tmp_path, defect)
    with pytest.raises(AssertionError):
        check_counts_outputs(tmp_path)


def test_processor_commits_while_a_list_reads_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """list_work holds no SQLite snapshot while it hashes objects and reads TODOs."""
    deliver(tmp_path, "a", b"a\n")
    fp.archive(tmp_path, IDENTITY)
    original = fp._object_status
    reports: list[fp.Summary] = []

    def process_meanwhile(root: Path, content_hash: str) -> str:
        if not reports:
            reports.append(fp.process(root, IDENTITY, handler(root)))
        return original(root, content_hash)

    monkeypatch.setattr(fp, "_object_status", process_meanwhile)
    [status] = fp.list_work(tmp_path, IDENTITY)
    monkeypatch.undo()
    assert reports[0].completed == 1
    assert (status.state, status.object, status.todo) == ("READY", "verified", False)
    assert work(tmp_path, b"a\n") == ("DONE", 1)
    check_counts_outputs(tmp_path)


def tree(root: Path) -> list[tuple[str, int, bytes]]:
    return [
        (
            str(path.relative_to(root)),
            path.lstat().st_mode,
            path.read_bytes() if path.is_file() else b"",
        )
        for path in sorted(root.rglob("*"))
    ]


def test_operator_reads_create_nothing(tmp_path: Path) -> None:
    deliver(tmp_path, "a", b"a\n")
    fp.archive(tmp_path, IDENTITY)
    (tmp_path / "inbox").rmdir()
    before = tree(tmp_path)
    [status] = fp.list_work(tmp_path, IDENTITY)
    fp.show_work(tmp_path, IDENTITY, status.work_key)
    assert tree(tmp_path) == before
    directories = [tmp_path, *(path for path in tmp_path.rglob("*") if path.is_dir())]
    try:
        for path in directories:
            path.chmod(0o555)
        assert fp.list_work(tmp_path, IDENTITY) == [status]
    finally:
        for path in directories:
            path.chmod(0o755)
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "ledger.sqlite3").write_bytes(b"")
    with pytest.raises(fp.ProtocolError, match="unsupported ledger schema"):
        fp.list_work(empty, IDENTITY)
    assert [path.name for path in empty.iterdir()] == ["ledger.sqlite3"]
    assert (empty / "ledger.sqlite3").read_bytes() == b""


@pytest.mark.parametrize("root", ["absent", "no_ledger"])
def test_ruling_refuses_a_root_without_a_ledger_before_locking(tmp_path: Path, root: str) -> None:
    target = tmp_path / "root"
    if root == "no_ledger":
        target.mkdir()
    with pytest.raises(fp.ProtocolError, match="not a worker root|no ledger"):
        fp.rule_work(
            target,
            IDENTITY,
            "0" * 64,
            "rejected",
            expect_state="READY",
            expect_attempt=0,
            actor="op",
            reason="none",
        )
    assert list(tmp_path.rglob("*")) == ([target] if root == "no_ledger" else [])


def test_hot_journal_is_named_and_a_worker_recovers_it(tmp_path: Path) -> None:
    """A writer killed after its cache spilled leaves a hot journal. A raw connection with a
    one-page cache stands in for a worker, whose own transactions are too small to spill."""
    for name in ("a", "b"):
        deliver(tmp_path, name, f"{name}\n".encode())
    fp.archive(tmp_path, IDENTITY)
    before = [tuple(row) for row in query(tmp_path, "SELECT * FROM work")]
    script = (
        "import os,sqlite3,sys; db=sqlite3.connect(sys.argv[1],isolation_level=None)\n"
        "db.execute('PRAGMA cache_size=1'); db.execute('BEGIN IMMEDIATE')\n"
        "for _ in range(50): db.execute('INSERT INTO identity VALUES (?)', ('x' * 3000,))\n"
        "os._exit(9)\n"
    )
    child = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "ledger.sqlite3")],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == 9, child.stderr
    assert (tmp_path / "ledger.sqlite3-journal").exists()
    for args in (("list",), ("show", "--key", key_of(b"a\n"))):
        refused = operator_cli(tmp_path, *args)
        assert (refused.returncode, refused.stdout) == (EXIT_REFUSED, "")
        assert refused.stderr.startswith(f"{args[0]} refused: ledger has a hot journal")
        assert "never restore from backup" in refused.stderr
        assert len(refused.stderr.splitlines()) == 1
    assert worker_cli(tmp_path, "archive").returncode == 0
    assert not (tmp_path / "ledger.sqlite3-journal").exists()
    assert len(fp.list_work(tmp_path, IDENTITY)) == 2
    assert [tuple(row) for row in query(tmp_path, "SELECT * FROM work")] == before
    assert len(query(tmp_path, "SELECT * FROM identity")) == 1
