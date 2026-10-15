"""Watch that every move of a job row is written inside an InputBatch.

The store snapshots an entity at its first touch inside an open input, and
the input's commit moves the revision of each entity it changed
(concurrency-model ss3). A move written with no input open would change a
row and no revision, so an `expect` composed against the old revision would
still pass. The oracle writes a job's status, flags and holding through the
store's move verbs; this harness wraps them to record a call on a store with
no batch open. It records rather than raises: the engine turns an exception
in a dry apply into a refusal, which would hide it. The caller asserts the
list is empty.
"""

from __future__ import annotations

import weakref

from collections.abc import Callable
from typing import Any

import pytest

from dsl41.oracle import InputBatch
from dsl41.oracle_state import RuntimeState

#: The store's verbs that write what the `job` chart reached.
MOVES = (
    "transition",
    "move_flag",
    "reserve",
    "take_over_held",
    "release_reservations",
    "release_held",
)


def install(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Wrap the move verbs for this test. Returns the list of misplaced moves."""
    misplaced: list[str] = []
    batched: weakref.WeakSet[RuntimeState] = weakref.WeakSet()
    enter, commit = InputBatch.__enter__, InputBatch._commit

    def batch_enter(self: InputBatch) -> InputBatch:
        batched.add(self._oracle.store)
        try:
            return enter(self)
        except BaseException:
            batched.discard(self._oracle.store)
            raise

    def batch_commit(self: InputBatch) -> None:
        try:
            commit(self)
        finally:
            batched.discard(self._oracle.store)

    def watched(verb: Callable[..., Any]) -> Callable[..., Any]:
        def move(self: RuntimeState, job: str, *args: Any, **kwargs: Any) -> Any:
            if self not in batched:
                misplaced.append(f"a move of {job} was written outside an InputBatch")
            return verb(self, job, *args, **kwargs)

        return move

    monkeypatch.setattr(InputBatch, "__enter__", batch_enter)
    monkeypatch.setattr(InputBatch, "_commit", batch_commit)
    for name in MOVES:
        monkeypatch.setattr(RuntimeState, name, watched(getattr(RuntimeState, name)))
    return misplaced
