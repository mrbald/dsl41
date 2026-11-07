"""The oracle store as one canonical snapshot: the rows, the timers and the three scalars no row
holds, with the load invariants a snapshot must satisfy.

The golden harness (`oracle_inputs.py`) digests this snapshot at every `SealPoint` of a script and
at its end, so two oracles that agree on every row and every timer agree on the digest, and a
store a snapshot cannot hold is named rather than digested. Sets sort by value and datetimes are
naive UTC (ss3.2). The retired host and route slots stay empty to preserve golden digests
(DL-351, DL-424).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from dsl41.oracle_state import (
    LIVE,
    Event,
    GlobalRuntime,
    JobRuntime,
    may_outlive_run,
)
from dsl41.runner_clock import EngineError


def _naive_utc(value: datetime) -> datetime:
    """ss3.2: a datetime in this artifact is naive UTC.

    Refused, not converted (`canon` converts an aware value reaching the encoder): an offset means
    a foreign clock, and this model compares instants, so an aware/naive pair would raise
    `TypeError` out of a validator."""
    if value.tzinfo is not None:
        raise ValueError(f"{value.isoformat()} is not naive UTC (ss3.2)")
    return value


#: a datetime this artifact carries in its own fields; `_canon_ready` catches the foreign models a
#: seal embeds (`Event`, `Effect`, `JobRuntime`)
NaiveUtc = Annotated[datetime, AfterValidator(_naive_utc)]


class SealedState(BaseModel):
    """The golden harness state: live row models and the scalars no row holds (ss3.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    jobs: dict[str, JobRuntime] = {}
    globals: dict[str, GlobalRuntime] = {}
    #: Empty compatibility slots in the golden payload, not runtime state (DL-351, DL-424).
    hosts: dict[str, object] = Field(default={}, max_length=0)
    routes: dict[str, object] = Field(default={}, max_length=0)
    #: ss3.2's order: `(due, token)`, never heap-array layout. The token carries the cross-job
    #: firing order of equal-time timers, which decides resource release, box cascades and who
    #: starts
    timers: tuple[tuple[NaiveUtc, int, Event], ...] = ()
    timer_seq: int = Field(default=0, ge=0)
    #: SEM-16's irreversible depletion. Keys survive their resource: a removed bucket keeps its
    #: entry and counts if a later period brings the resource back (PR-19a)
    consumed: dict[str, int] = {}
    enqueue_counter: int = Field(default=0, ge=0)
    now: NaiveUtc

    @model_validator(mode="after")
    def _load_invariants(self) -> SealedState:
        _check_timers(self.timers, self.timer_seq)
        _check_waiters(self.jobs, self.enqueue_counter)
        _check_reservations(self.jobs)
        for bucket, units in sorted(self.consumed.items()):
            if units < 0:
                raise ValueError(
                    f"consumed[{bucket!r}] is {units}: a seal that opened with invented"
                    " capacity is worse than one that refuses (ss5)"
                )
        return self


def _check_timers(timers: tuple[tuple[datetime, int, Event], ...], timer_seq: int) -> None:
    """ss7 phase 3 step 6: tokens unique, positive and <= `timer_seq`, in `(due, token)` order.

    Two equal `(due, token)` entries would force the heap to compare two unorderable `Event`
    objects."""
    seen: set[int] = set()
    for due, token, _event in timers:
        if token <= 0:
            raise ValueError(f"timer token {token} at {due.isoformat()}: tokens are positive")
        if token > timer_seq:
            raise ValueError(
                f"timer token {token} is above timer_seq {timer_seq}: the allocator"
                " cannot have issued it"
            )
        if token in seen:
            raise ValueError(
                f"timer token {token} appears twice: two equal heap keys make the"
                " firing order undefined"
            )
        seen.add(token)
    order = [(due, token) for due, token, _ in timers]
    if order != sorted(order):
        raise ValueError("timers are not in (due, token) order (ss3.2)")


def _check_waiters(jobs: Mapping[str, JobRuntime], enqueue_counter: int) -> None:
    """ss5: `waiter_seq` non-null iff QUE_WAIT, unique, positive, and no
    higher than the allocator that issued it."""
    ranks: dict[int, str] = {}
    for name, row in sorted(jobs.items()):
        if (row.waiter_seq is not None) != (row.status == "QUE_WAIT"):
            raise ValueError(
                f"job {name!r}: status {row.status} with waiter_seq {row.waiter_seq!r} --"
                " a rank is held exactly while QUE_WAIT (ss5)"
            )
        if row.waiter_seq is None:
            continue
        if row.waiter_seq <= 0:
            raise ValueError(f"job {name!r}: waiter_seq {row.waiter_seq} is not positive")
        if row.waiter_seq > enqueue_counter:
            raise ValueError(
                f"job {name!r}: waiter_seq {row.waiter_seq} is above enqueue_counter"
                f" {enqueue_counter} -- the allocator cannot have issued it (ss5)"
            )
        if row.waiter_seq in ranks:
            raise ValueError(
                f"jobs {ranks[row.waiter_seq]!r} and {name!r} share waiter_seq"
                f" {row.waiter_seq}: admission order would not survive the boundary (PR-21)"
            )
        ranks[row.waiter_seq] = name


def _check_reservations(jobs: Mapping[str, JobRuntime]) -> None:
    """ss5: a vector is held while STARTING or RUNNING; a non-live row holds only a renewable's
    units its run's policy did not free (DL-256, `may_outlive_run`); a bucket appears at most once
    (ss3.2)."""
    for name, row in sorted(jobs.items()):
        if row.status not in LIVE and not all(map(may_outlive_run, row.reservations)):
            raise ValueError(
                f"job {name!r}: status {row.status} still holds a machine load or a"
                " reservation released on completion -- the run's end releases those,"
                " and a row that is not live keeps only a resource's unreleased units"
                " (ss5, DL-256)"
            )
        buckets = [reservation.bucket for reservation in row.reservations]
        if len(set(buckets)) != len(buckets):
            raise ValueError(
                f"job {name!r}: bucket {sorted(buckets)} appears twice in one"
                " reservation vector (ss3.2)"
            )


def _canon_ready(value: object, path: str = "$") -> Any:
    """ss3.2's two shape rules a pydantic dump does not apply: a set sorts by value, and a
    datetime is naive UTC.

    An aware datetime is refused on `_naive_utc`'s reason; this is that rule's backstop for the
    embedded models this module does not own."""
    if isinstance(value, Mapping):
        return {key: _canon_ready(item, f"{path}.{key}") for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        try:
            items = sorted(value)
        except TypeError as exc:
            raise EngineError(f"{path}: a set of mixed types has no canonical order") from exc
        return [_canon_ready(item, f"{path}[]") for item in items]
    if isinstance(value, (list, tuple)):
        return [_canon_ready(item, f"{path}[{position}]") for position, item in enumerate(value)]
    if isinstance(value, datetime) and value.tzinfo is not None:
        raise EngineError(f"{path}: {value.isoformat()} is not naive UTC (ss3.2)")
    return value
