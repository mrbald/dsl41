"""Frozen bulk command values; admission and execution belong to the runner."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast, get_args

from pydantic import JsonValue

from dsl41.canon import canonical_bytes, is_scalar_string, is_wire_int
from dsl41.job_selection import JobSelectors, SelectionSnapshot

BulkVerb = Literal[
    "STARTJOB",
    "FORCE_STARTJOB",
    "KILLJOB",
    "ON_ICE",
    "OFF_ICE",
    "ON_HOLD",
    "OFF_HOLD",
    "ON_NOEXEC",
    "OFF_NOEXEC",
    "DISARM",
    "RELEASE_RESOURCE",
    "CHANGE_STATUS",
]


@dataclass(frozen=True, slots=True)
class TargetExpectation:
    job: str
    state_rev: int


@dataclass(frozen=True, slots=True)
class BulkRequest:
    """The reusable semantic request, including the original membership and every pin."""

    request_id: str
    baseline_id: str
    config_rev: int
    selectors: JobSelectors
    targets: tuple[str, ...]
    verb: BulkVerb
    arguments_json: str
    expect: tuple[TargetExpectation, ...]
    claimed_actor: str | None = None

    def __post_init__(self) -> None:
        if not self.selectors.active or any(
            value == "" for value in self.selectors.as_dict().values()
        ):
            raise ValueError("bulk needs at least one selector and refuses empty selectors")
        if not self.targets:
            raise ValueError("bulk selection has zero targets")
        if len(set(self.targets)) != len(self.targets):
            raise ValueError("duplicate targets")
        if self.verb not in get_args(BulkVerb):
            raise ValueError("bulk requires an existing job-directed verb; SET_GLOBAL is refused")
        for value in (self.request_id, self.baseline_id, *self.targets):
            if not isinstance(value, str) or not value or not is_scalar_string(value):
                raise ValueError(
                    "request id, baseline and targets must be non-empty scalar strings"
                )
        if self.request_id.startswith("engine:"):
            raise ValueError("engine: request ids are reserved")
        if self.claimed_actor is not None and (
            not isinstance(self.claimed_actor, str) or not is_scalar_string(self.claimed_actor)
        ):
            raise ValueError("claimed_actor must be a Unicode scalar string")
        if not is_wire_int(self.config_rev) or self.config_rev < 0:
            raise ValueError("config_rev must be a non-negative integer")
        if len({item.job for item in self.expect}) != len(self.expect) or {
            item.job for item in self.expect
        } != set(self.targets):
            raise ValueError("expect must name every target exactly once and nothing else")
        if any(not is_wire_int(item.state_rev) or item.state_rev < 0 for item in self.expect):
            raise ValueError("expect revisions must be non-negative integers")
        pins = {item.job: item for item in self.expect}
        object.__setattr__(self, "expect", tuple(pins[name] for name in self.targets))
        arguments = json.loads(self.arguments_json)
        if not isinstance(arguments, dict) or "job" in arguments:
            raise ValueError("bulk arguments must be an object without an individual job")
        object.__setattr__(self, "arguments_json", canonical_bytes(arguments).decode("utf-8"))

    @property
    def arguments(self) -> dict[str, JsonValue]:
        """Return a detached copy; callers cannot mutate the frozen request's arguments."""
        return cast(dict[str, JsonValue], json.loads(self.arguments_json))

    def semantic_body(self) -> dict[str, JsonValue]:
        return {
            "baseline_id": self.baseline_id,
            "config_rev": self.config_rev,
            "selectors": dict(self.selectors.as_dict()),
            "targets": list(self.targets),
            "verb": self.verb,
            "payload": self.arguments,
            "expect": {f"job:{item.job}": item.state_rev for item in self.expect},
            "claimed_actor": self.claimed_actor,
        }

    def fingerprint(self, *, actor: str | None) -> str:
        """Hash the server-resolved actor explicitly, never the request's client claim."""
        if actor is not None and (not isinstance(actor, str) or not is_scalar_string(actor)):
            raise ValueError("actor must be a Unicode scalar string or None")
        body = self.semantic_body()
        del body["claimed_actor"]
        body["actor"] = actor
        return hashlib.sha256(canonical_bytes(body)).hexdigest()

    def as_request(self, *, version: int) -> dict[str, JsonValue]:
        """Build a detached wire candidate; the integration slice owns the AF dialect."""
        if not is_wire_int(version) or version <= 0:
            raise ValueError("wire version must be a positive integer")
        return {
            "cmd": "sendevent_bulk",
            "v": version,
            "request_id": self.request_id,
            **self.semantic_body(),
        }

    @classmethod
    def from_request(cls, value: object) -> BulkRequest:
        """Decode a saved preview or admission input through the same model validation."""
        required = {
            "cmd",
            "v",
            "request_id",
            "baseline_id",
            "config_rev",
            "selectors",
            "targets",
            "verb",
            "payload",
            "expect",
        }
        if not isinstance(value, Mapping):
            raise ValueError("bulk request must be an object")
        if not required.issubset(value) or set(value).difference(required | {"claimed_actor"}):
            raise ValueError("bulk request has missing or unknown fields")
        if value["cmd"] != "sendevent_bulk":
            raise ValueError("bulk request cmd must be sendevent_bulk")
        if not is_wire_int(value["v"]) or value["v"] <= 0:
            raise ValueError("wire version must be a positive integer")
        targets = value["targets"]
        if not isinstance(targets, list) or any(not isinstance(name, str) for name in targets):
            raise ValueError("bulk targets must be an array of job names")
        expect = value["expect"]
        if not isinstance(expect, Mapping) or any(
            not isinstance(key, str) or not key.startswith("job:") for key in expect
        ):
            raise ValueError("expect must be an object of job:<name> revisions")
        return cls(
            request_id=cast(str, value["request_id"]),
            baseline_id=cast(str, value["baseline_id"]),
            config_rev=cast(int, value["config_rev"]),
            selectors=JobSelectors.from_dict(value["selectors"]),
            targets=tuple(cast(list[str], targets)),
            verb=cast(BulkVerb, value["verb"]),
            arguments_json=canonical_bytes(value["payload"]).decode("utf-8"),
            expect=tuple(
                TargetExpectation(key.removeprefix("job:"), revision)
                for key, revision in expect.items()
            ),
            claimed_actor=cast(str | None, value.get("claimed_actor")),
        )

    def frame(self, *, version: int, max_request_bytes: int) -> bytes:
        """Refuse the whole request above the caller's byte budget, including its newline."""
        if not is_wire_int(max_request_bytes) or max_request_bytes <= 0:
            raise ValueError("max_request_bytes must be a positive integer")
        encoded = json.dumps(self.as_request(version=version)).encode("utf-8") + b"\n"
        if len(encoded) > max_request_bytes:
            raise ValueError(
                f"bulk request is oversized: {len(encoded)} > {max_request_bytes} bytes"
            )
        return encoded


def bulk_request(
    snapshot: SelectionSnapshot,
    *,
    request_id: str,
    verb: BulkVerb,
    arguments: Mapping[str, JsonValue],
    claimed_actor: str | None = None,
) -> BulkRequest:
    return BulkRequest(
        request_id=request_id,
        baseline_id=snapshot.baseline_id,
        config_rev=snapshot.config_rev,
        selectors=snapshot.selectors,
        targets=snapshot.targets,
        verb=verb,
        arguments_json=canonical_bytes(dict(arguments)).decode("utf-8"),
        expect=tuple(TargetExpectation(job.name, job.state_rev) for job in snapshot.jobs),
        claimed_actor=claimed_actor,
    )


def check_selection(request: BulkRequest, current: SelectionSnapshot) -> None:
    """Check membership/configuration against a newly resolved applied-catalog snapshot."""
    if (
        request.baseline_id != current.baseline_id
        or request.config_rev != current.config_rev
        or request.selectors != current.selectors
        or request.targets != current.targets
    ):
        raise ValueError("bulk selection or configuration changed")


@dataclass(frozen=True, slots=True)
class ExpectationFailure:
    job: str
    expected: int
    actual: int | None


def expectation_failures(
    request: BulkRequest, job_revisions: Mapping[str, int]
) -> tuple[ExpectationFailure, ...]:
    """Return all stale/missing pins in target order; the runner calls after its time half."""
    expected = {item.job: item.state_rev for item in request.expect}
    failures: list[ExpectationFailure] = []
    for name in request.targets:
        actual = job_revisions.get(name)
        if actual is not None and (not is_wire_int(actual) or actual < 0):
            raise ValueError("current job revisions must be non-negative integers")
        if actual != expected[name]:
            failures.append(ExpectationFailure(name, expected[name], actual))
    return tuple(failures)
