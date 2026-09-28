"""LIST-reply builders shared by the supervisor test doubles (DL-220).

`SupervisorClient.list_runs` returns `SupervisorListSuccess |
SupervisorRefusal`, not a raw dict -- so a fake that stands in for it must
hand back the same models, built through their own constructors, rather
than a dict a consumer's `isinstance`/attribute reads would silently treat
as a refusal. These builders take the terse field subsets the tests already
write and fill in the rest of each model's required fields with values no
test here inspects.

Not a test file: no test_ prefix, imported by the fakes that need it.
"""

from __future__ import annotations

from typing import Any

from dsl41.runner_adapters import SupervisorListSuccess, SupervisorRefusal, SupervisorRunRow

_ROW_DEFAULTS: dict[str, Any] = {
    "run_id": "",
    "job": "",
    "run_number": 0,
    "run_dir": "",
    "wrapper_pid": 0,
    "wrapper_alive": False,
    "spawned_at": "1970-01-01T00:00:00",
    "wrapper_rc": None,
}

_LISTING_DEFAULTS: dict[str, Any] = {
    "version": 1,
    "supervisor_pid": 0,
    "boot_id": "boot",
    "incarnation": "inc-1",
    "deadman_s": None,
    "lease": None,
}


def stub_row(**overrides: Any) -> SupervisorRunRow:
    """One `runs` row, its 8 required fields defaulted past what a given
    test does not care about."""
    return SupervisorRunRow(**{**_ROW_DEFAULTS, **overrides})


def stub_listing(
    *, runs: list[SupervisorRunRow] | None = None, **overrides: Any
) -> SupervisorListSuccess:
    """A successful LIST reply (`ok: true`), its non-`runs` fields
    defaulted the same way `stub_row` defaults a row's."""
    return SupervisorListSuccess(**{**_LISTING_DEFAULTS, **overrides}, ok=True, runs=runs or [])


def stub_refusal(error: str = "internal: stub refusal") -> SupervisorRefusal:
    """A refused reply -- LIST answers this shape too (supervisor-protocol
    ss5), on a malformed envelope, an unsupported version, an unknown verb,
    or a handler exception."""
    return SupervisorRefusal(ok=False, error=error)
