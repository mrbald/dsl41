"""Every event the process tier's machines accept, in every state a builder reaches.

DL-293 found a missing row in the job machine with an exhaustive event x state
test (tests/test_job_machines.py). These tests do the same for the supervisor's
`supervisor_process` and `supervisor_lease`, the engine's `supervisor_client`,
and the anchor's `anchor_head` and `period_row`. Each test builds states
through the owner's own code, sends every event that code accepts, and
passes only when each event takes a declared row or is refused by the code's
own rule.

None of these machines writes a trace line. Each is a statechart, whose
executor raises a `StatechartError` on an event no row takes, so each test
checks the rows each event took, or that the run raised nothing, and the
chart's coverage items. Each test records into its own directory and asserts
which declared (row, source) pairs its builder took; each pair it does not
take is named, with the reason. The fixture then hands the hits to the
session's directory, so the coverage gate counts them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from dsl41.state_machine import HITS_ENV, Executor, Outcome


@pytest.fixture
def recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A private directory for the hits. Afterwards they go on to the
    session's directory, if it has one."""
    session = os.environ.get(HITS_ENV)
    directory = tmp_path / "hits"
    directory.mkdir()
    monkeypatch.setenv(HITS_ENV, str(directory))
    yield directory
    if session:
        for path in directory.glob("hits-*.jsonl"):
            shutil.copyfile(path, Path(session) / f"hits-{tmp_path.name}-{path.name}")


def _records(directory: Path, prefix: str) -> list[dict[str, str]]:
    return [
        json.loads(line)
        for path in sorted(directory.glob(f"{prefix}-*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


# ------------------------------------------------------------ supervisor_lease


class _Clock:
    """The supervisor's `time` module with a monotonic clock the test moves."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    @staticmethod
    def time() -> float:
        return time.time()

    @staticmethod
    def sleep(seconds: float) -> None:
        time.sleep(seconds)


#: the lease's events. A request is (verb, connection, credentials, ttl_s).
type _LeaseEvent = tuple[str, str, str, float | str | None]

_SLOTS = ("a", "b")
_CREDENTIALS = ("current", "none", "stale", "foreign")
#: the lease verbs' own refusals (supervisor-protocol ss5). Each changes nothing.
_LEASE_REFUSALS = {"bad_controller_id", "lease_held", "wrong_incarnation", "stale_token"}
#: a ttl_s with no representable expiry: not a number, and not finite
_UNTIMED = ("x", float("inf"))
#: the rows an answered verb may take, and those a dropped connection may take
_LEASE_ROWS = {
    "ACQUIRE": {"01", "02", "03"},
    "RENEW": {"07"},
    "RELEASE": {"11"},
    "EOF": {"14", "15"},
}


def _lease_events() -> list[_LeaseEvent]:
    events: list[_LeaseEvent] = [
        ("ACQUIRE", "a", "bad_controller_id", 60.0),
        ("ELAPSE", "", "", None),
    ]
    for slot in _SLOTS:
        for creds in _CREDENTIALS:
            events += [("ACQUIRE", slot, creds, ttl) for ttl in (60.0, 0.0, *_UNTIMED)]
            events += [("RENEW", slot, creds, ttl) for ttl in (60.0, -1.0, *_UNTIMED)]
            events.append(("RELEASE", slot, creds, None))
        events.append(("EOF", slot, "", None))
    return events


# ---------------------------------------------------------- supervisor_process


class _Injected(Exception):
    """An error the test raises at a chosen point of the supervisor's run."""


#: where an event is delivered: right after the move into a state, or at the
#: first reap while in it (the loop's tick, and each pass of the shutdown wait)
_POINTS = (
    ("starting", "enter"),
    ("bound", "enter"),
    ("serving", "enter"),
    ("serving", "reap"),
    ("draining", "enter"),
    ("draining", "reap"),
    ("shutting_down", "enter"),
    ("shutting_down", "reap"),
    ("stopped", "enter"),
    ("stopped", "reap"),
    ("refused", "enter"),
    ("closed", "enter"),
)
_STIMULI = (
    "none",
    "signal",
    "sigchld",
    "shutdown",
    "stale_shutdown",
    "deadman",
    "error",
    "error_after_signal",
)
#: what ends the serving loop, delivered on entering `serving`
_STOPPERS = ("signal", "shutdown", "pipelined_shutdowns", "deadman")


#: the seventeen (source, row) pairs of the table the chart replaced, which the
#: walk took each of. A row of a grouping state (31) is judged by the leaf
#: state it left, so each old source stays a pair. Teardown is one row per
#: class of what the state owns (31 to 35), and a signal in shutting_down is 36.
_PROCESS_PAIRS = frozenset(
    {
        *(("starting", f"supervisor_process.{n}") for n in ("01", "02", "03")),
        ("bound", "supervisor_process.04"),
        ("serving", "supervisor_process.05"),
        ("serving", "supervisor_process.06"),
        ("shutting_down", "supervisor_process.36"),
        ("shutting_down", "supervisor_process.07"),
        ("serving", "supervisor_process.08"),
        ("stopped", "supervisor_process.09"),
        ("stopped", "supervisor_process.32"),
        ("refused", "supervisor_process.35"),
        ("starting", "supervisor_process.34"),
        ("bound", "supervisor_process.33"),
        *((state, "supervisor_process.31") for state in ("serving", "shutting_down")),
        ("shutting_down", "supervisor_process.12"),
    }
)


# ----------------------------------------------------------- supervisor_client


class _FakeSupervisor:
    """A unix-socket peer that answers every request ok, except a SIGNAL for
    run `hold`, which it never answers. The client's chart is moved by its
    transport's facts only, so the peer's lease rules do not matter here."""

    def __init__(self) -> None:
        self.writers: list[asyncio.StreamWriter] = []
        self.held = asyncio.Event()

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.writers.append(writer)
        with contextlib.suppress(OSError):
            while line := await reader.readline():
                req = json.loads(line)
                if req.get("cmd") == "SIGNAL" and req.get("run_id") == "hold":
                    self.held.set()
                    continue
                reply = {"ok": True, "token": 1, "incarnation": "i", "deadman_s": None}
                writer.write(json.dumps(reply).encode() + b"\n")
                await writer.drain()
        writer.close()

    def drop_all(self) -> None:
        for writer in self.writers:
            writer.close()
        self.writers.clear()


_CLIENT_EVENTS = ("connect", "reconnect", "request", "acquire", "eof", "cancel", "close")


class _SpiedExecutor(Executor):
    """An executor that keeps every outcome with the configuration it met, so
    the walk can name what each event took and where it was posted."""

    def __init__(self) -> None:
        super().__init__()
        self.posts: list[tuple[frozenset[str], Outcome]] = []

    def dispatch(self, key: Any, event: Any) -> list[Outcome]:
        met = self.active(key)
        outcomes = super().dispatch(key, event)
        self.posts += [(met, outcome) for outcome in outcomes]
        return outcomes


# ------------------------------------------------------ anchor_head, period_row


class _Crash(Exception):
    """The anchor write that fails after `claim_successor` wrote its claim file."""


_ESTATE = "estate-exhaustive"


def _seal(period: int, variant: str) -> str:
    """A seal digest. A seal names its own period, as a real one does through
    the record it hashes, so no digest closes two periods."""
    return f"sha256:{period}{variant * 63}"


#: an anchor event: (verb, period, root, seal variant); unused fields are empty
type _AnchorEvent = tuple[str, int, str, str]


def _anchor_events() -> list[_AnchorEvent]:
    events: list[_AnchorEvent] = [("reclaim", 0, "", ""), ("lose_claim", 0, "", "")]
    for root in ("r1", "r2"):
        events += [("create_open", 1, root, ""), ("create_open_stranger", 1, root, "")]
        events.append(("open_claimed", 0, root, ""))
        for variant in "ab":
            events += [("attest", p, root, variant) for p in (1, 2, 3)]
            events += [
                (verb, p, root, variant) for verb in ("claim", "claim_crash") for p in (1, 2)
            ]
    # a period closes at its own seal; another digest for it is the variant-b events above
    events += [("close_period", p, "", "a") for p in (1, 2, 3)]
    events += [("finalize", p, "", "") for p in (1, 2, 3)]
    return events
