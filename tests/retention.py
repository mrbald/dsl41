"""Retention in one call, for the tests that prune a store outside an engine: the cleanup, the
bound and every step to it, as an engine's drain takes them (DL-543)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from dsl41.runner import PRUNE_STEP
from dsl41.runner_store import Pruned, Store

#: an instant every record of a test store is older than
FAR: datetime = datetime(2999, 1, 1)


def prune(store: Store, seq: int, older_than: datetime, *, limit: int = PRUNE_STEP) -> int:
    """Delete every snapshot but the latest two, then every journal row below the bound of
    `seq` and `older_than`, one step at a time; answer how many journal rows went. With no
    snapshot nothing is deleted."""
    store.prune_snapshots()
    bound = store.prune_bound(seq, older_than)
    if bound is None:
        return 0
    deleted = 0
    while True:
        went = store.prune_step(bound, limit).deleted
        deleted += went
        if went < limit:
            return deleted


class EarlierPrune:
    """The three operations over an earlier build's store, whose one `prune_before` deletes
    the snapshots and the rows below its bound together. The engine of this build calls them;
    the earlier code does the work."""

    _store: Any

    def prune_snapshots(self) -> None:
        return None  # the earlier `prune_before` deletes them in the step's transaction

    def prune_bound(self, seq: int, older_than: datetime) -> int | None:
        latest = self._store.latest_snapshot()
        if latest is None:
            return None
        recent = next(
            (found for found, record in self._store.read_from(0) if record.at_utc >= older_than),
            seq,
        )
        return min(seq, int(latest.seq), int(recent))

    def prune_step(self, bound: int, limit: int) -> Pruned:
        decisions = [
            int(record.payload["index"])
            for found, record in self._store.read_from(0)
            if found < bound and record.kind == "decision"
        ]
        deleted = int(self._store.prune_before(bound, FAR))
        return Pruned(deleted, max(decisions) if decisions and deleted else None)

    def first_seq(self) -> int:
        """The engine reads it after a step; the build before DL-536 has no such read."""
        return int(next((seq for seq, _ in self._store.read_from(0)), self._store.last_seq() + 1))
