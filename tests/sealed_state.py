"""The oracle store as one canonical snapshot: the rows, the timers and the three scalars no row
holds, with the load invariants a snapshot must satisfy.

The golden harness (`oracle_inputs.py`) digests this snapshot at every `SealPoint` of a script and
at its end, so two oracles that agree on every row and every timer agree on the digest, and a
store a snapshot cannot hold is named rather than digested. Sets sort by value and datetimes are
naive UTC (ss3.2). A host row drops `last_contact` and carries `deadman_us` null (ss3.3).
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
    HostRuntime,
    HostState,
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


class RouteRuntime(BaseModel):
    """ss3.3's fourth row: which executor a logical role resolves to.

    A route names an executor and nothing else. It carries no generation: effect birth takes
    `generation` from the host row's current value, so a stale route cannot exist, and an evicted
    host's work is the concurrency model's ss8 case (DL-189)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    executor_id: str = Field(min_length=1)
    state_rev: int = Field(default=0, ge=0)


class SealedHost(BaseModel):
    """A host row as a seal carries it: ss3.3's row minus `last_contact`, with `deadman_us` null.

    Both exclusions are in the shape. A carried `last_contact` lets the new period conclude a
    quarantined host's deadman expired, and a carried deadman lets C2 restart the supervisor at
    120s while the row says 60s, permitting eviction before the real kill bound. A host with a
    null deadman is not evictable except by force, the safe direction; the deadman stays null
    until the host re-registers (PR-24a).

    `deadman_us`, not `deadman_s`: ss3.2 has no floats at any depth. The field is always present
    and null, since ss3.2 requires a typed field to be present."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: HostState = "active"
    generation: int = Field(default=0, ge=0)
    deadman_us: None = None
    forced_by: str | None = None
    state_before_quarantine: HostState | None = None
    state_rev: int = Field(default=0, ge=0)

    @classmethod
    def of(cls, row: HostRuntime | SealedHost) -> SealedHost:
        """Project a live host row. Every carried field is named, so a field added to
        `HostRuntime` lands here or the ss3.3 exclusion test reds."""
        if isinstance(row, SealedHost):
            return row
        return cls(
            state=row.state,
            generation=row.generation,
            forced_by=row.forced_by,
            state_before_quarantine=row.state_before_quarantine,
            state_rev=row.state_rev,
        )

    def to_row(self) -> HostRuntime:
        """The `HostRuntime` an opening engine seeds: the carried fields, with the two exclusions
        null until the host re-registers."""
        return HostRuntime(
            state=self.state,
            generation=self.generation,
            deadman_s=None,
            last_contact=None,
            forced_by=self.forced_by,
            state_before_quarantine=self.state_before_quarantine,
            state_rev=self.state_rev,
        )


class SealedState(BaseModel):
    """ss3.1's `state`: every authoritative row plus the three scalars no row holds.

    The rows are the live models, since a second spelling of a carried row would be a second
    authority. Only `hosts` is projected, because ss3.3 excludes two of its fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    jobs: dict[str, JobRuntime] = {}
    globals: dict[str, GlobalRuntime] = {}
    hosts: dict[str, SealedHost] = {}
    routes: dict[str, RouteRuntime] = {}
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

    @model_validator(mode="before")
    @classmethod
    def _project_hosts(cls, data: Any) -> Any:
        """Accept live `HostRuntime` rows and project them (ss3.3), so no caller has to remember
        the two exclusions."""
        if isinstance(data, Mapping) and isinstance(data.get("hosts"), Mapping):
            hosts = {
                host_id: SealedHost.of(row) if isinstance(row, (HostRuntime, SealedHost)) else row
                for host_id, row in data["hosts"].items()
            }
            data = {**data, "hosts": hosts}
        return data

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
        for role, route in sorted(self.routes.items()):
            if route.executor_id not in self.hosts:
                raise ValueError(
                    f"route {role!r} names executor {route.executor_id!r}, which has no"
                    " host row: an effect born for it could never dispatch (ss7)"
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
