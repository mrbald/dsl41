"""The archive through the real `dsl41 run` (DL-545): the CLI engine arms the archive for a
new root and for a restarted one, and a restarted root's rows past the retention end up in
`archive.db`, byte for byte, and gone from the live store."""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from collections.abc import Callable
from typing import Any

import pytest
from test_engine_stops import _until
from test_live_engine_branches import _cli
from test_main_config import config_file

from dsl41 import main_config as main
from dsl41.oracle_state import Event
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_clock import RealClock
from dsl41.runner_config import EngineSettings
from dsl41.runner_journal import STORE_NAME
from dsl41.runner_startup import resume_run, start_run
from dsl41.runner_store import SqliteStore

pytestmark = pytest.mark.usefixtures("cli_env", "owned_processes")

#: how old the seeded records are when the CLI engine starts: past the one-day retention
_AGE = timedelta(days=3)
#: the snapshot interval the CLI engine runs with, and how long the test lets pass between two
#: admissions, measured after the first one's answer
_INTERVAL_S = 0.001
_APART_S = 0.05


class _PastClock(RealClock):
    """The real clock domain, three days behind the wall: every record it stamps is `_AGE` old
    or older when the CLI engine reads the wall."""

    def now(self) -> datetime:
        return super().now() - _AGE


def _estate(directory: Path) -> Path:
    (directory / "jobs.jil").write_text("insert_job: j\njob_type: c\ncommand: true\n")
    path = config_file(directory, engine='machine_policy = "local-eligible"\n')
    root = directory / "run"
    root.mkdir(exist_ok=True)
    (root / "engine.toml").write_text(f"retention_days = 1\nsnapshot_interval_s = {_INTERVAL_S}\n")
    return path


async def _events(client: Any, count: int) -> None:
    """`count` admissions, each sent `_APART_S` after the previous one was answered. The
    engine takes its snapshot time at an admission, before it answers, so the next one comes
    more than `_INTERVAL_S` after it, and a snapshot round falls due at every admission after
    the first."""
    for n in range(count):
        if n:
            answered = time.monotonic()
            await _until(lambda: time.monotonic() - answered > _APART_S)
        status = await client.ask("status")
        answer = await client.ask(
            "sendevent",
            verb="SET_GLOBAL",
            payload={"name": f"G{n}", "value": "v"},
            request_id=f"live-{n}",
            baseline_id=status["baseline_id"],
            expect={f"global:G{n}": 0},  # a new global: absent until this input
        )
        assert answer["ok"], answer


def test_a_new_root_started_by_dsl41_run_has_a_working_archive(short_root: Path) -> None:
    path = _estate(short_root)

    async def scenario() -> None:
        async with _cli(path) as (client, _):
            status = await client.ask("status")
            assert status["archive"]["state"] == "working", status["archive"]

    asyncio.run(scenario())


def test_a_restarted_root_archives_its_old_rows_before_they_go(short_root: Path) -> None:
    """A root whose records are three days old, restarted by `dsl41 run`: its snapshot round
    copies them to `archive.db`, verifies them and deletes them from the live store."""
    path = _estate(short_root)
    root = short_root / "run"

    async def seed() -> None:
        estate = main.prepare(path)
        engine = start_run(
            estate.catalog,
            root,
            clock=_PastClock(),
            adapters={"CMD": FakeAdapter(default=None)},
            launched=main.profile_of(estate),
            non_idempotent=estate.metadata["non_idempotent"],
            source_capture=main.encoded_parts(estate),
            settings=EngineSettings(retention_days=1),
        )
        try:
            for n in range(5):
                at = engine.clock.now()
                engine.inject(
                    Event(at=at, kind="SET_GLOBAL", payload={"name": f"S{n}", "value": "v"}),
                    request_id=f"seed-{n}",
                )
                await engine.run_until_quiescent(at)
        finally:
            await engine.shutdown()
            assert engine.journal is not None
            engine.journal.close()

    asyncio.run(seed())
    with closing(SqliteStore(root / STORE_NAME, read_only=True)) as store:
        last = store.last_seq()
        seeded = store.raw_batch(1, last + 1, 10**6).records
    assert [row[0] for row in seeded] == list(range(1, last + 1))
    oldest_kept = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1)
    assert all(datetime.fromisoformat(row[1]) < oldest_kept for row in seeded)  # all past it

    def archived() -> bool:
        with closing(SqliteStore(root / STORE_NAME, read_only=True)) as store:
            return store.first_seq() > last

    async def scenario() -> None:
        async with _cli(path) as (client, _):
            await _events(client, 3)
            await _until(archived)
            status = await client.ask("status")
            assert status["archive"]["state"] == "working", status["archive"]
            assert status["archive"]["verified_through"] >= last

    asyncio.run(scenario())
    with closing(sqlite3.connect(f"file:{root / 'archive.db'}?mode=ro", uri=True)) as db:
        held = [
            (seq, at, kind, actor, rid, payload.encode())
            for seq, at, kind, actor, rid, payload in db.execute(
                "SELECT seq, at_utc, kind, actor, request_id, payload FROM records"
                " WHERE seq <= ? ORDER BY seq",
                (last,),
            )
        ]
    assert held == seeded


async def _seed(path: Path, root: Path, names: range, value: str, *, genesis: bool) -> None:
    """Inputs three days old into the root, by an engine on the past clock: a new root, or
    a restart of the one there."""
    estate = main.prepare(path)
    clock = _PastClock()
    adapters = {"CMD": FakeAdapter(default=None)}
    settings = EngineSettings(retention_days=1)
    if genesis:
        engine = start_run(
            estate.catalog,
            root,
            clock=clock,
            adapters=adapters,
            launched=main.profile_of(estate),
            non_idempotent=estate.metadata["non_idempotent"],
            source_capture=main.encoded_parts(estate),
            settings=settings,
        )
    else:
        engine = await resume_run(
            estate.catalog,
            root,
            clock=clock,
            adapters=adapters,
            settings=settings,
            stored_sources=True,
        )
        await engine.run_until_quiescent(clock.now())
    try:
        for n in names:
            at = engine.clock.now()
            engine.inject(
                Event(at=at, kind="SET_GLOBAL", payload={"name": f"S{n}", "value": value}),
                request_id=f"seed-{value}-{n}",
            )
            await engine.run_until_quiescent(at)
    finally:
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()


def _copy(source: Path, target: Path) -> None:
    """The store's files, copied with no engine running."""
    target.mkdir(exist_ok=True)
    for end in ("", "-wal", "-shm"):
        (target / f"{STORE_NAME}{end}").unlink(missing_ok=True)
        if (source / f"{STORE_NAME}{end}").exists():
            shutil.copy2(source / f"{STORE_NAME}{end}", target / f"{STORE_NAME}{end}")


def test_a_live_store_put_back_to_an_older_copy_runs_on_and_keeps_both_lines(
    short_root: Path,
) -> None:
    """C6: a root is run and its old rows archived; with no engine running, its live store is
    put back to an older copy, and that copy's line goes on with other records at the same
    seqs. `dsl41 run` runs on: nothing stops, pruning goes on, and the archive holds both
    lines' rows at those seqs. Put back once more to the first line's later backup, it runs
    on again and nothing stops."""
    path = _estate(short_root)
    root = short_root / "run"
    older, later = short_root / "older", short_root / "later"
    asyncio.run(_seed(path, root, range(3), "first", genesis=True))
    _copy(root, older)
    asyncio.run(_seed(path, root, range(3, 6), "line-one", genesis=False))
    with closing(SqliteStore(root / STORE_NAME, read_only=True)) as store:
        line_one = store.last_seq()

    def archived_through(last: int) -> Callable[[], bool]:
        def through() -> bool:
            with closing(SqliteStore(root / STORE_NAME, read_only=True)) as store:
                return store.first_seq() > last

        return through

    async def run(last: int | None) -> dict[str, Any]:
        async with _cli(path) as (client, _):
            await _events(client, 3)
            if last is not None:
                await _until(archived_through(last))
            async with asyncio.timeout(30):
                while (status := (await client.ask("status"))["archive"])["histories"] is None:
                    await asyncio.sleep(0.01)
            return dict(status)

    first = asyncio.run(run(line_one))
    assert first["state"] == "working", first
    _copy(root, later)
    _copy(older, root)
    asyncio.run(_seed(path, root, range(3, 6), "line-two", genesis=False))
    with closing(SqliteStore(root / STORE_NAME, read_only=True)) as store:
        line_two = store.last_seq()
    second = asyncio.run(run(line_two))
    assert second["state"] == "working", second
    with closing(sqlite3.connect(f"file:{root / 'archive.db'}?mode=ro", uri=True)) as db:
        held: dict[int, set[str]] = {}
        for seq, payload in db.execute("SELECT seq, payload FROM records WHERE kind = 'input'"):
            held.setdefault(int(seq), set()).add(str(payload))
    both = [
        seq
        for seq, payloads in held.items()
        if any("line-one" in p for p in payloads) and any("line-two" in p for p in payloads)
    ]
    assert both  # one seq, two rows: one of each line
    _copy(later, root)
    third = asyncio.run(run(None))
    assert third["state"] == "working", third
