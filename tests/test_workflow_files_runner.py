"""Real watcher and command adapters; scheduler outcomes never stand in for files."""

import asyncio
import contextlib
from datetime import datetime
import json
from pathlib import Path
import shlex
import sys

import pytest

from dsl41.ir import lower_source
from dsl41.oracle_state import Event
from dsl41.runner_adapters import WATCH_LOG, FileWatcherAdapter, LocalCommandAdapter, read_watch_log
from dsl41.runner_clock import RealClock
from dsl41.runner_journal import read_journal
from dsl41.runner_startup import start_run
from examples import workflow_driver as driver
from examples import workflow_files as files
from test_fw_loop import _stop, _until
from test_workflow_feed import invoke as release
from test_workflow_files import records


DRIVER = Path(driver.__file__).resolve()
pytestmark = pytest.mark.usefixtures("cli_env")


def command(root, config, job):
    return shlex.join(
        [
            sys.executable,
            str(DRIVER),
            "--db",
            str(root.ledger),
            "--file-root",
            str(root.root),
            "--config",
            str(config),
            "--namespace",
            "engine-one",
            "--job",
            job,
        ]
    )


def empty_poll(run_root, ordinal):
    path = run_root / "runs" / f"watcher.{ordinal}" / WATCH_LOG
    if not path.exists():
        return False
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return any(
        (row["kind"] == "poll" and not row["exists"])
        or (row["kind"] == "sample" and not row["files"])
        for row in rows
    )


@pytest.mark.parametrize("watched", ["item.ready", "*.ready"], ids=["exact", "final-wildcard"])
def test_sem10_native_watcher_consumption_and_archive_before_rename(short_root, watched):
    base = short_root
    data = base / "files"
    data.mkdir(mode=0o700)
    for name in ("inbox", "work", "archive"):
        (data / name).mkdir(mode=0o700)
    root = files.FileRoot(data, base / "ledger.sqlite")
    config = base / "scenario.json"
    config.write_text(
        json.dumps(
            {
                "scenario": "native",
                "jobs": {
                    "archive_prior": {
                        "file_action": {
                            "operation": "archive",
                            "source": "work/item.current",
                            "destination": "archive/prior.saved",
                            "missing_ok": True,
                        }
                    },
                    "consume": {
                        "file_action": {
                            "operation": "move",
                            "source": "inbox/item.ready",
                            "destination": "work/item.current",
                            "key": "delivery",
                        }
                    },
                },
            }
        )
    )
    source = (
        "insert_job: cycle\njob_type: b\n\n"
        "insert_job: watcher\njob_type: f\nbox_name: cycle\n"
        f"watch_file: {data / 'inbox' / watched}\nwatch_interval: 1\nwatch_file_min_size: 1\n\n"
        "insert_job: archive_prior\njob_type: c\nbox_name: cycle\ncondition: s(watcher)\n"
        f"command: {command(root, config, 'archive_prior')}\nmachine: m1\n\n"
        "insert_job: consume\njob_type: c\nbox_name: cycle\ncondition: s(archive_prior)\n"
        f"command: {command(root, config, 'consume')}\nmachine: m1\n"
    )
    run_root = base / "run"

    async def scenario():
        clock = RealClock()
        engine = start_run(
            lower_source(source),
            run_root,
            clock=clock,
            adapters={"CMD": LocalCommandAdapter(), "FW": FileWatcherAdapter()},
            hold_open=True,
        )
        assert engine.journal is not None
        async with contextlib.AsyncExitStack() as teardown:
            teardown.callback(engine.journal.close)
            teardown.push_async_callback(engine.shutdown)
            loop_task = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
            teardown.push_async_callback(_stop, loop_task)
            for ordinal, content in ((1, "first synthetic\n"), (2, "second synthetic\n")):
                assert not (data / "inbox/item.ready").exists()
                engine.inject(Event(at=clock.now(), kind="STARTJOB", payload={"job": "cycle"}))
                await _until(
                    lambda: (
                        engine.oracle.store.job["watcher"].run_number == ordinal
                        and empty_poll(run_root, ordinal)
                    ),
                    f"empty watch {ordinal}",
                    loop_task,
                )
                assert engine.oracle.store.job["consume"].status == "INACTIVE"
                delivery = await asyncio.to_thread(
                    release,
                    root,
                    [{"target": "inbox/item.ready", "content": content}],
                    release=str(ordinal),
                )
                assert delivery.returncode == 0, delivery.stderr
                await _until(
                    lambda: engine.oracle.store.job["cycle"].status == "SUCCESS",
                    f"native cycle {ordinal}",
                    loop_task,
                )
                assert not (data / "inbox/item.ready").exists()
                assert (data / "work/item.current").read_bytes() == content.encode()
                fold = read_watch_log(run_root / "runs" / f"watcher.{ordinal}")
                assert fold is not None and fold.complete
            assert (data / "archive/prior.saved").read_bytes() == b"first synthetic\n"
            assert engine.oracle.store.job["consume"].run_number == 2
            assert (
                sum(
                    entry.job == "consume" and entry.transition == "RUNNING->SUCCESS"
                    for entry in engine.oracle.trace()
                )
                == 2
            )

    asyncio.run(scenario())
    ledger = records(root)
    consumers = [row for row in ledger if json.loads(row["scope"])["job"] == "consume"]
    assert len(consumers) == 2
    assert len({row["invocation_id"] for row in consumers}) == 2
    assert [json.loads(row["identity"])["DSL41_RUN_NUMBER"] for row in consumers] == ["1", "2"]
    assert all(row["state"] == "completed" for row in consumers)
    archives = [
        json.loads(row["result"])
        for row in ledger
        if json.loads(row["scope"])["job"] == "archive_prior"
    ]
    assert [row["result"]["members"][0]["status"] for row in archives] == ["absent", "moved"]
    journal = read_journal(run_root)
    assert [
        (row["payload"]["run_number"], row["payload"].get("exit_code"))
        for row in journal
        if row.get("kind") == "STATUS" and row["payload"].get("job") == "consume"
    ] == [(1, 0), (2, 0)]


def test_sem04_manual_success_creates_no_file_or_driver_evidence(short_root):
    data = short_root / "files"
    data.mkdir(mode=0o700)
    (data / "inbox").mkdir()
    (data / "work").mkdir()
    (data / "inbox/item.ready").write_bytes(b"unchanged")
    root = files.FileRoot(data, short_root / "ledger.sqlite")
    config = short_root / "scenario.json"
    config.write_text(
        json.dumps(
            {
                "jobs": {
                    "consume": {
                        "file_action": {
                            "operation": "move",
                            "source": "inbox/item.ready",
                            "destination": "work/item.current",
                        }
                    }
                }
            }
        )
    )
    source = f"insert_job: consume\njob_type: c\ncommand: {command(root, config, 'consume')}\nmachine: m1\n"

    async def scenario():
        clock = RealClock()
        engine = start_run(
            lower_source(source),
            short_root / "run",
            clock=clock,
            adapters={"CMD": LocalCommandAdapter(), "FW": FileWatcherAdapter()},
        )
        assert engine.journal is not None
        try:
            engine.inject(
                Event(
                    at=clock.now(), kind="STATUS", payload={"job": "consume", "status": "SUCCESS"}
                )
            )
            await engine.run_until_quiescent(datetime.max)
            assert engine.oracle.store.job["consume"].status == "SUCCESS"
            assert engine.oracle.store.job["consume"].run_number == 0
        finally:
            await engine.shutdown()
            engine.journal.close()

    asyncio.run(scenario())
    assert not root.ledger.exists()
    assert (data / "inbox/item.ready").read_bytes() == b"unchanged"
    assert not (data / "work/item.current").exists()
    assert not (data / files.LOCK).exists()
