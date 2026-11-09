"""Public synthetic file choreography; injected faults are explicit test seams."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from examples import workflow_driver as driver
from examples import workflow_files as files


@pytest.fixture
def sandbox(tmp_path):
    root = tmp_path / "files"
    root.mkdir(mode=0o700)
    for name in ("inbox", "work", "archive"):
        (root / name).mkdir(mode=0o700)
    return files.FileRoot(root, tmp_path / "ledger.sqlite")


def run(root, action, **kwargs):
    return files.execute(
        root,
        files.validate_action(action),
        schema=driver.SCHEMA,
        job=kwargs.pop("job", "worker"),
        **kwargs,
    )


def records(root, table="file_receipts"):
    with closing(sqlite3.connect(root.ledger)) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY id")]


def put(root, name, content=b"synthetic\x00bytes\n"):
    (root.root / name).write_bytes(content)
    return content


MOVE = {"operation": "move", "source": "inbox/item.ready", "destination": "work/item.current"}


def test_ordinary_move_reexecutes_and_records_missing_source(sandbox):
    content = put(sandbox, MOVE["source"])
    first = run(sandbox, MOVE)
    assert first["outcome"] == "success"
    assert not (sandbox.root / MOVE["source"]).exists()
    assert (sandbox.root / MOVE["destination"]).read_bytes() == content
    second = run(sandbox, MOVE)
    assert (second["outcome"], second["cause"]) == ("refused", "missing_source")
    assert second["invocation_id"] != first["invocation_id"]
    assert len(records(sandbox, "attempts")) == 2
    assert [r["state"] for r in records(sandbox)] == ["completed", "refused"]
    assert records(sandbox)[1]["claim_key"] is None


def test_archive_absent_moved_and_occupied_destination(sandbox):
    action = {
        "operation": "archive",
        "source": "work/item.current",
        "destination": "archive/item.saved",
        "missing_ok": True,
    }
    absent = run(sandbox, action)
    assert absent["result"]["members"] == [{"path": "work/item.current", "status": "absent"}]
    content = put(sandbox, "work/item.current")
    assert run(sandbox, action)["result"]["members"][0]["status"] == "moved"
    later = put(sandbox, "work/item.current", b"later")
    sentinel = put(sandbox, "inbox/sentinel", b"unrelated")
    refused = run(sandbox, action)
    assert refused["cause"] == "collision"
    assert (sandbox.root / "archive/item.saved").read_bytes() == content
    assert (sandbox.root / "work/item.current").read_bytes() == later
    assert (sandbox.root / "inbox/sentinel").read_bytes() == sentinel
    assert len(records(sandbox, "attempts")) == 3


def test_remove_prevalidates_all_members_and_leaves_nonlisted_later_arrivals(sandbox, tmp_path):
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    put(sandbox, "inbox/first")
    put(sandbox, "inbox/sibling", b"sibling")
    action = {"operation": "remove", "paths": ["inbox/first", "inbox/missing"]}
    assert run(sandbox, action)["cause"] == "missing_source"
    assert (sandbox.root / "inbox/first").exists()
    action["missing_ok"] = True

    def arrival(phase, index):
        if phase == "before_mutation" and index == 0:
            put(sandbox, "inbox/later", b"later")

    result = run(sandbox, action, fault=arrival)
    assert [item["status"] for item in result["result"]["members"]] == ["removed", "absent"]
    assert not (sandbox.root / "inbox/first").exists()
    assert (sandbox.root / "inbox/sibling").read_bytes() == b"sibling"
    assert (sandbox.root / "inbox/later").read_bytes() == b"later"
    assert outside.read_bytes() == b"outside"


@pytest.mark.parametrize(
    "name", ["/absolute", "a//b", "a/./b", "../escape", "a/../b", "a/", "a\0b", "*.ready"]
)
def test_invalid_path_syntax_is_configuration_error(name):
    with pytest.raises(ValueError):
        files.validate_action({**MOVE, "source": name})


@pytest.mark.parametrize("kind", ["directory", "fifo", "final_link", "ancestor_link", "hardlink"])
def test_unsupported_sources_leave_files_unchanged(sandbox, tmp_path, kind):
    source = sandbox.root / MOVE["source"]
    if kind == "directory":
        source.mkdir()
    elif kind == "fifo":
        os.mkfifo(source)
    elif kind == "final_link":
        outside = tmp_path / "outside"
        outside.write_bytes(b"outside")
        source.symlink_to(outside)
    elif kind == "ancestor_link":
        (sandbox.root / "alias").symlink_to(sandbox.root / "inbox", target_is_directory=True)
        put(sandbox, "inbox/item.ready")
    else:
        put(sandbox, MOVE["source"])
        os.link(source, sandbox.root / "inbox/linked")
    result = run(
        sandbox, {**MOVE, **({"source": "alias/item.ready"} if kind == "ancestor_link" else {})}
    )
    assert result["outcome"] == "refused"
    assert source.exists()
    assert not (sandbox.root / MOVE["destination"]).exists()
    assert len(records(sandbox, "attempts")) == 1


def test_same_file_alias_and_injected_cross_device_refuse(sandbox, monkeypatch):
    put(sandbox, MOVE["source"])
    assert run(sandbox, {**MOVE, "destination": MOVE["source"]})["cause"] == "same_file"
    monkeypatch.setattr(
        sandbox, "info", SimpleNamespace(st_dev=sandbox.info.st_dev + 1, st_ino=sandbox.info.st_ino)
    )
    assert run(sandbox, MOVE)["cause"] == "cross_device"
    assert (sandbox.root / MOVE["source"]).exists()
    assert not (sandbox.root / MOVE["destination"]).exists()


def test_root_permissions_wrong_root_and_system_symlink_alias(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    context = files.FileRoot(root, tmp_path / "ledger")
    assert context.root == root.resolve()
    alias = tmp_path / "root-alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError, match="itself must not be a symlink"):
        files.FileRoot(alias, tmp_path / "ledger")
    assert files.FileRoot(alias / ".." / "private", tmp_path / "ledger").root == root.resolve()
    root.chmod(0o777)
    with pytest.raises(ValueError, match="owned"):
        files.FileRoot(root, tmp_path / "ledger")
    with pytest.raises(ValueError, match="absolute"):
        files.FileRoot(Path("relative"), tmp_path / "ledger")
    with pytest.raises(ValueError, match="filesystem root"):
        files.FileRoot(Path("/"), tmp_path / "ledger")


@pytest.mark.parametrize("keyed", [False, True])
def test_fail_before_and_after_effect_both_modes(sandbox, keyed):
    action = {**MOVE, **({"key": "delivery"} if keyed else {})}
    kwargs = {"namespace": "engine-one", "logical_run": "1"}
    content = put(sandbox, MOVE["source"])
    before = run(sandbox, action, outcome="fail_before_effect", **kwargs)
    assert before["outcome"] == "fail_before_effect"
    assert records(sandbox)[0]["claim_key"] is None
    assert (sandbox.root / MOVE["source"]).read_bytes() == content
    after = run(sandbox, action, outcome="fail_after_effect", **kwargs)
    assert after["outcome"] == "fail_after_effect"
    assert not (sandbox.root / MOVE["source"]).exists()
    assert (sandbox.root / MOVE["destination"]).read_bytes() == content
    repeated = run(sandbox, action, **kwargs)
    assert repeated["outcome"] == ("success" if keyed else "refused")
    assert repeated["cause"] == ("reused" if keyed else "missing_source")
    assert len(records(sandbox, "attempts")) == 3


def test_keyed_namespace_run_spec_and_original_identity(sandbox):
    action = {**MOVE, "key": "delivery"}
    identity = {"DSL41_JOB": "worker", "DSL41_RUN_NUMBER": "1", "DSL41_ATTEMPT": "first"}
    first = run(sandbox, action, namespace="engine-one", identity=identity)
    assert first["cause"] == "missing_source"
    put(sandbox, MOVE["source"], b"first")
    first = run(sandbox, action, namespace="engine-one", identity=identity)
    (sandbox.root / MOVE["destination"]).unlink()
    put(sandbox, MOVE["source"], b"second")
    reused = run(
        sandbox,
        action,
        namespace="engine-one",
        identity={**identity, "DSL41_ATTEMPT": "replacement"},
    )
    assert reused["cause"] == "reused"
    assert reused["reused_from"] == {
        "invocation_id": first["invocation_id"],
        "job": "worker",
        "run": "1",
        "attempt": "first",
        "attempt_id": first["attempt_id"],
    }
    assert (sandbox.root / MOVE["source"]).read_bytes() == b"second"
    assert (
        run(
            sandbox,
            {**action, "destination": "work/changed"},
            namespace="engine-one",
            identity=identity,
        )["cause"]
        == "changed_spec"
    )
    second = run(sandbox, action, namespace="engine-two", identity=identity)
    assert second["cause"] == "completed"
    assert (sandbox.root / MOVE["destination"]).read_bytes() == b"second"
    (sandbox.root / MOVE["destination"]).unlink()
    put(sandbox, MOVE["source"], b"third")
    assert (
        run(
            sandbox, action, namespace="engine-one", identity={**identity, "DSL41_RUN_NUMBER": "2"}
        )["cause"]
        == "completed"
    )
    assert (sandbox.root / MOVE["destination"]).read_bytes() == b"third"


@pytest.mark.parametrize("keyed", [False, True])
def test_process_kill_after_unlink_retains_pending_identity(sandbox, keyed):
    action = {
        "operation": "remove",
        "paths": ["inbox/item.ready"],
        **({"key": "one"} if keyed else {}),
    }
    put(sandbox, "inbox/item.ready")
    code = (
        "import os,json; from pathlib import Path; from examples import workflow_files as f; "
        "from examples.workflow_driver import SCHEMA; "
        f"root=f.FileRoot(Path({str(sandbox.root)!r}),Path({str(sandbox.ledger)!r})); "
        f"action=f.validate_action(json.loads({json.dumps(action)!r})); "
        "f.execute(root,action,schema=SCHEMA,job='worker',namespace='engine-one',"
        "identity={'DSL41_JOB':'worker','DSL41_RUN_NUMBER':'1','DSL41_ATTEMPT':'killed'},"
        "fault=lambda phase,index: os._exit(77) if phase=='after_mutation' else None)"
    )
    child = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=10)
    assert child.returncode == 77, child.stderr
    assert not (sandbox.root / "inbox/item.ready").exists()
    pending = records(sandbox)[0]
    assert pending["state"] == "pending"
    assert records(sandbox, "attempts")[0]["outcome"] == "pending"
    assert json.loads(pending["identity"])["DSL41_ATTEMPT"] == "killed"
    assert pending["invocation_id"]
    put(sandbox, "inbox/item.ready", b"new")
    next_call = run(sandbox, action, namespace="engine-one", identity={"DSL41_RUN_NUMBER": "1"})
    assert next_call["prior_uncertain"] == [pending["invocation_id"]]
    assert next_call["cause"] == ("uncertain" if keyed else "completed")
    assert (sandbox.root / "inbox/item.ready").exists() == keyed
    assert len(records(sandbox, "attempts")) == 2
    assert (
        run(sandbox, {"operation": "delay", "seconds": 0}, job="other", logical_run="1")[
            "prior_uncertain"
        ]
        == []
    )
    assert (
        run(sandbox, {"operation": "delay", "seconds": 0}, logical_run="2")["prior_uncertain"] == []
    )


def test_injected_remove_io_failure_records_prefix_and_uncertainty(sandbox):
    for name in ("first", "second", "third"):
        put(sandbox, "inbox/" + name, name.encode())

    def fault(phase, index):
        if phase == "before_mutation" and index == 1:
            raise OSError("injected member I/O failure")

    result = run(
        sandbox,
        {"operation": "remove", "paths": ["inbox/first", "inbox/second", "inbox/third"]},
        fault=fault,
    )
    assert (result["outcome"], result["cause"]) == ("failure", "partial_io")
    assert [m["path"] for m in result["result"]["members"]] == ["inbox/first"]
    assert result["result"]["uncertain"] == {"path": "inbox/second", "index": 1}
    assert not (sandbox.root / "inbox/first").exists()
    assert (sandbox.root / "inbox/second").read_bytes() == b"second"
    assert (sandbox.root / "inbox/third").read_bytes() == b"third"
    assert records(sandbox)[0]["state"] == "pending"
    assert len(records(sandbox, "attempts")) == 1


def test_delay_releases_lock_and_transaction_with_receipt_order(sandbox):
    started, finish = threading.Event(), threading.Event()

    def barrier(phase, index):
        if phase == "delay_started":
            started.set()
            assert finish.wait(5)

    put(sandbox, MOVE["source"])
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(run, sandbox, {"operation": "delay", "seconds": 0.01}, fault=barrier)
        try:
            assert started.wait(5)
            middle = run(sandbox, MOVE)
            assert middle["outcome"] == "success"
            delivery = files.execute(
                sandbox,
                {
                    "operation": "publish",
                    "items": [{"target": "inbox/second.ready", "content": "second"}],
                    "key": "release",
                },
                schema=driver.SCHEMA,
                job="feeder",
                namespace="deliveries",
                logical_run="one",
            )
            assert delivery["outcome"] == "success"
        finally:
            finish.set()
        delayed = waiting.result(timeout=10)
    assert (
        delayed["started_at"]
        < middle["started_at"]
        < middle["completed_at"]
        < delayed["completed_at"]
    )
    assert (
        middle["completed_at"]
        < delivery["started_at"]
        < delivery["completed_at"]
        < delayed["completed_at"]
    )
    assert (sandbox.root / "inbox/second.ready").read_bytes() == b"second"
    assert [r["state"] for r in records(sandbox)] == ["completed", "completed", "completed"]
    assert not (sandbox.root / MOVE["source"]).exists()


@pytest.mark.parametrize("seconds", [True, -1, 3601, float("nan"), float("inf"), "1"])
def test_invalid_delay_is_configuration_error(seconds):
    with pytest.raises(ValueError):
        files.validate_action({"operation": "delay", "seconds": seconds})


def test_deliberately_held_lock_records_only_busy_attempt(sandbox):
    put(sandbox, MOVE["source"])
    with sandbox.locked():
        result = run(sandbox, MOVE)
    assert result["cause"] == "busy"
    assert result["outcome"] == "refused"
    assert (sandbox.root / MOVE["source"]).exists()
    assert len(records(sandbox, "attempts")) == 1
    assert records(sandbox)[0]["claim_key"] is None
    assert records(sandbox)[0]["state"] == "refused"


def test_unavailable_ledger_reports_invocation_and_uncommitted_cause(sandbox):
    sandbox.ledger.mkdir()
    content = put(sandbox, MOVE["source"])
    with pytest.raises(files.EvidenceError, match="uncommitted cause pending") as error:
        run(sandbox, MOVE)
    assert error.value.invocation
    assert (sandbox.root / MOVE["source"]).read_bytes() == content
    assert not (sandbox.root / MOVE["destination"]).exists()


def test_reserved_ledger_sidecars_and_aliases_preserve_integrity(sandbox):
    root = files.FileRoot(sandbox.root, sandbox.root / "ledger.sqlite")
    assert run(root, {"operation": "delay", "seconds": 0})["outcome"] == "success"
    with closing(sqlite3.connect(root.ledger)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "INSERT INTO effects (scenario,batch,job,kind,target,payload) VALUES ('s','b','j','k','t','null')"
        )
        connection.commit()
        wal = Path(str(root.ledger) + "-wal")
        assert wal.exists()
        sentinel = Path(str(root.ledger) + "-journal")
        sentinel.write_bytes(b"\0" * 512)
        put(root, MOVE["source"], b"source")
        for action in (
            {"operation": "remove", "paths": ["ledger.sqlite-wal"]},
            {"operation": "remove", "paths": ["ledger.sqlite-journal"]},
            {**MOVE, "source": "LEDGER.SQLITE"},
            {**MOVE, "destination": "LeDgEr.SqLiTe"},
            {"operation": "remove", "paths": [files.LOCK]},
            {"operation": "remove", "paths": [files.STAGE + "/item"]},
        ):
            assert run(root, action)["cause"] == "reserved_path"
        assert wal.exists()
        assert sentinel.read_bytes() == b"\0" * 512
        assert (root.root / MOVE["source"]).read_bytes() == b"source"
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT COUNT(*) FROM effects").fetchone() == (1,)
    assert len(records(root, "attempts")) == 7


def test_readonly_ledger_and_busy_evidence_failure_never_mutate(sandbox):
    run(sandbox, {"operation": "delay", "seconds": 0})
    content = put(sandbox, MOVE["source"])
    sandbox.ledger.chmod(0o400)
    try:
        with pytest.raises(files.EvidenceError) as error:
            run(sandbox, MOVE)
        assert error.value.invocation
        assert error.value.cause == "pending"
        with sandbox.locked():
            with pytest.raises(files.EvidenceError) as busy:
                run(sandbox, MOVE)
        assert busy.value.cause == "busy"
    finally:
        sandbox.ledger.chmod(0o600)
    assert (sandbox.root / MOVE["source"]).read_bytes() == content
    assert not (sandbox.root / MOVE["destination"]).exists()
    assert len(records(sandbox, "attempts")) == 1


def test_invalid_peer_lock_records_a_refusal_without_touching_data(sandbox, tmp_path):
    content = put(sandbox, MOVE["source"])
    outside = tmp_path / "outside-lock"
    outside.write_bytes(b"outside")
    (sandbox.root / files.LOCK).symlink_to(outside)
    result = run(sandbox, MOVE)
    assert result["cause"] == "unsupported_path"
    assert len(records(sandbox, "attempts")) == 1
    assert outside.read_bytes() == b"outside"
    assert (sandbox.root / MOVE["source"]).read_bytes() == content


def test_delay_completion_busy_exits_two_and_keeps_keyed_pending_evidence(sandbox):
    started, complete = threading.Event(), threading.Event()

    def barrier(phase, index):
        if phase == "delay_started":
            started.set()
            assert complete.wait(5)

    action = {"operation": "delay", "seconds": 0, "key": "wait"}
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(
            run, sandbox, action, namespace="engine-one", logical_run="1", fault=barrier
        )
        try:
            assert started.wait(5)
            with sandbox.locked():
                complete.set()
                result = waiting.result(timeout=5)
        finally:
            complete.set()
    assert (result["outcome"], result["cause"], files.exit_code(result)) == ("refused", "busy", 2)
    assert len(records(sandbox, "attempts")) == 1
    pending = records(sandbox)[0]
    assert pending["state"] == "pending"
    assert pending["claim_key"] is not None
    assert pending["cause"] == "busy"
    replay = run(sandbox, action, namespace="engine-one", logical_run="1")
    assert replay["cause"] == "uncertain"
    assert len(records(sandbox, "attempts")) == 2


def test_real_delay_waits_configured_duration_and_completes_receipt(sandbox):
    import time

    seconds = 0.2
    started = time.monotonic()
    result = run(sandbox, {"operation": "delay", "seconds": seconds})
    elapsed = time.monotonic() - started
    assert elapsed >= seconds
    assert result["outcome"] == "success"
    assert result["cause"] == "completed"
    assert records(sandbox)[0]["state"] == "completed"
