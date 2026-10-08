"""The fork leak test over the SEM corpus (concurrency-model ss4).

The engine dry-applies a control input on `Oracle.fork()` before it logs
the input. Two things must hold for that to be sound, and this harness
checks both on every feed of every SEM trace test in test_oracle.py (the
`fork` param of its autouse fixture):

- the dry apply leaves the original's canonical state and trace
  byte-equal to what they were, and
- the fork's result equals the real apply's: the same state, the same
  trace, the same emitted events, or the same exception.

`state_bytes` serializes every attribute the fork must copy. `COPIED`
and `SHARED` name every attribute an `Oracle` and a `RuntimeState` hold,
and test_dry_apply.py pins the two sets against `vars()`, so a new
attribute cannot escape the check by being added after it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from dsl41.canon import canonical_bytes
from dsl41.oracle import Oracle
from dsl41.oracle_state import Event

#: Oracle attributes the fork copies, so a dry apply cannot reach them.
ORACLE_COPIED = frozenset(
    {
        "store",
        "_trace",
        "_emitted",
        "_queue",
        "_now",
        "_opening_release",
        "_in_wake",
        "_full_scan_requested",
        "_scan_owed",
        "_tz_cache",
        "_calendars",
        "_window_starts",
    }
)
#: Oracle attributes fixed once the constructor returns, so the fork shares them.
ORACLE_SHARED = frozenset(
    {"catalog", "semantics", "_referencers", "_pool", "_tz_aliases", "_default_tz"}
)
#: RuntimeState attributes. The fork copies every one (scalars by value),
#: except `_violations`: a fork's channel starts empty, since an orphan must
#: not refuse the input being dry-applied.
STORE_COPIED = frozenset(
    {
        "_jobs",
        "_globals",
        "_hosts",
        "_timers",
        "_timer_seq",
        "_period_id",
        "_period_seeded",
        "_inputs_committed",
        "_genesis_finished",
        "_consumed",
        "_enqueue_counter",
        "_snapshots",
        "_in_input",
        "_violations",
    }
)


def _when(at: datetime | None) -> str | None:
    return None if at is None else at.isoformat()


def _event(ev: Event) -> object:
    return ev.model_dump(mode="json")


def state_bytes(oracle: Oracle) -> bytes:
    """The canonical bytes of everything a dry apply could change."""
    store = oracle.store
    doc = {
        "jobs": {name: row.model_dump(mode="json") for name, row in store.job.items()},
        "globals": {name: row.model_dump(mode="json") for name, row in store.globals_.items()},
        "hosts": {name: row.model_dump(mode="json") for name, row in store.hosts.items()},
        # the heap's array layout, not only its firing order: a fork that
        # shared the list would reorder it here first
        "timers": [[_when(due), token, _event(ev)] for due, token, ev in store._timers],
        "timer_seq": store.timer_seq,
        "period": [
            store._period_id,
            store._period_seeded,
            store._inputs_committed,
            store._genesis_finished,
        ],
        "consumed": dict(store.consumed),
        "enqueue_counter": store.enqueue_counter,
        "snapshots": sorted(store._snapshots),
        "in_input": store._in_input,
        "violations": [[subject, repr(v)] for subject, v in store._violations],
        "trace": [entry.model_dump(mode="json") for entry in oracle._trace],
        "emitted": [_event(ev) for ev in oracle._emitted],
        "queue": [_event(ev) for ev in oracle._queue],
        "now": _when(oracle._now),
        "opening_release": list(oracle._opening_release),
        "flags": [oracle._in_wake, oracle._full_scan_requested, oracle._scan_owed],
        "tz_cache": sorted(oracle._tz_cache),
        "calendars": sorted(oracle._calendars),
        "window_starts": (
            None if oracle._window_starts is None else [list(w) for w in oracle._window_starts]
        ),
    }
    return canonical_bytes(doc)


class ForkCheckedOracle(Oracle):
    """An Oracle whose every input is first applied on a fork."""

    def feed(self, ev: Event) -> list[Event]:
        return self._checked(lambda oracle: Oracle.feed(oracle, ev))

    def advance(self, now: datetime) -> list[Event]:
        return self._checked(lambda oracle: Oracle.advance(oracle, now))

    def _checked(self, step: Callable[[Oracle], list[Event]]) -> list[Event]:
        before = state_bytes(self)
        fork = self.fork()
        dry: list[Event] = []
        dry_fault: Exception | None = None
        try:
            dry = step(fork)
        except Exception as exc:  # noqa: BLE001 -- compared with the real apply's below
            dry_fault = exc
        assert state_bytes(self) == before, "a dry apply on a fork changed the original"
        try:
            real = step(self)
        except Exception as exc:
            assert dry_fault is not None, f"only the real apply raised: {exc!r}"
            assert (type(exc), str(exc)) == (type(dry_fault), str(dry_fault))
            assert state_bytes(fork) == state_bytes(self), "the fork's result differs"
            raise
        assert dry_fault is None, f"only the dry apply raised: {dry_fault!r}"
        assert state_bytes(fork) == state_bytes(self), "the fork's result differs"
        assert [_event(ev) for ev in dry] == [_event(ev) for ev in real]
        return real
