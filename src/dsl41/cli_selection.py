"""Pure CLI preparation. These helpers perform no reads, writes or sends."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import JsonValue

from dsl41.bulk_selection import BulkRequest, BulkVerb, bulk_request
from dsl41.job_selection import JobSelectors, SelectionSnapshot


def query_selectors(
    verb: str,
    *,
    job: str | None = None,
    application: str | None = None,
    group: str | None = None,
) -> JobSelectors:
    selectors = JobSelectors(application, group)
    if selectors.active and job is not None:
        raise ValueError("application/group selectors are mutually exclusive with -J")
    if selectors.active and verb not in ("status", "spec"):
        raise ValueError("only status and spec accept application/group query selectors")
    return selectors


@dataclass(frozen=True, slots=True)
class PreparedBulkRequest:
    request: BulkRequest
    wire_bytes: bytes

    @property
    def preview(self) -> str:
        """The complete reusable request to print before sending these same bytes."""
        return self.wire_bytes.decode("utf-8").removesuffix("\n")


def prepare_bulk_request(
    snapshot: SelectionSnapshot,
    *,
    request_id: str,
    verb: BulkVerb,
    arguments: Mapping[str, JsonValue],
    version: int,
    max_request_bytes: int,
    claimed_actor: str | None = None,
    scalar_expect: int | None = None,
    job: str | None = None,
) -> PreparedBulkRequest:
    if scalar_expect is not None:
        raise ValueError("a scalar --expect is invalid for bulk; preserve every snapshot revision")
    if job is not None:
        raise ValueError("application/group selectors are mutually exclusive with -J")
    request = bulk_request(
        snapshot, request_id=request_id, verb=verb, arguments=arguments, claimed_actor=claimed_actor
    )
    return PreparedBulkRequest(
        request, request.frame(version=version, max_request_bytes=max_request_bytes)
    )
