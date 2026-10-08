"""Watch that every move of the job's machines is taken inside an InputBatch.

Only an `InputBatch` drains the violation channel (concurrency-model ss4). A
`take` outside one would leave its violation an orphan: under the suite's
strict variable it raises at the next `begin_input`, and in production it is
dropped to stderr with no trace line. A strict run only sees a take that
broke its table, so this harness watches every take, broken or not.

Every take of `job_status`, `job_flags` and `job_holding` reaches the channel
through `RuntimeState.note_violation`, with the take's result or None.
`install` wraps it to record a call on a store with no batch open, and wraps
`RuntimeState.begin_input` to record a channel that is not empty. It records
rather than raises: the engine turns an exception in a dry apply into a
refusal, which would hide it. The caller asserts the list is empty.
"""

from __future__ import annotations

import weakref

import pytest

from dsl41.oracle import InputBatch
from dsl41.oracle_state import RuntimeState
from dsl41.state_machine import Violation


def install(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Wrap the channel's writers for this test. Returns the list of misplaced moves."""
    misplaced: list[str] = []
    batched: weakref.WeakSet[RuntimeState] = weakref.WeakSet()
    enter, commit = InputBatch.__enter__, InputBatch._commit
    note, begin = RuntimeState.note_violation, RuntimeState.begin_input

    def batch_enter(self: InputBatch) -> InputBatch:
        batched.add(self._oracle.store)
        try:
            return enter(self)
        except BaseException:
            batched.discard(self._oracle.store)
            raise

    def batch_commit(self: InputBatch, *, time_half: bool = False) -> None:
        try:
            commit(self, time_half=time_half)
        finally:
            batched.discard(self._oracle.store)

    def note_violation(self: RuntimeState, subject: str, violation: Violation | None) -> None:
        if self not in batched:
            misplaced.append(f"a move of {subject} was taken outside an InputBatch")
        note(self, subject, violation)

    def begin_input(self: RuntimeState) -> None:
        if self._violations:
            misplaced.append(f"the channel held {self._violations!r} at begin_input")
        begin(self)

    monkeypatch.setattr(InputBatch, "__enter__", batch_enter)
    monkeypatch.setattr(InputBatch, "_commit", batch_commit)
    monkeypatch.setattr(RuntimeState, "note_violation", note_violation)
    monkeypatch.setattr(RuntimeState, "begin_input", begin_input)
    return misplaced
