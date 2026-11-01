"""The idempotence declaration and the invocation environment (crash spec, 2.0 charter).

An estate is idempotent by requirement. `ESTATE_DIR/non-idempotent.toml` names the exceptions:

    [jobs]
    "emea.ledger_post" = "posts to the GL twice if rerun"

The file is a sidecar, not a JIL attribute, so the JIL stays vendor-clean (DL-07). An absent
file means every job is idempotent. A name with a dot is a quoted key; a bare dotted key is a
nested table and is refused. `Exceptions.canonical_bytes` is the file's part of the reload token
and of the applied configuration, so recovery reads the marked set from the configuration the
engine runs, never from disk.

Every launch exports the logical invocation: the job, its run number and the scheduled instant
stay the same across the attempts of one run, and the attempt id is new each time. A job that
keys its work on the first three is idempotent with respect to its invocation across any number
of crashes.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from dsl41.canon import canonical_bytes

#: The sidecar's file name inside the estate directory.
EXCEPTIONS_FILE: Final[str] = "non-idempotent.toml"

ENV_JOB: Final[str] = "DSL41_JOB"
ENV_RUN_NUMBER: Final[str] = "DSL41_RUN_NUMBER"
ENV_SCHEDULED_AT: Final[str] = "DSL41_SCHEDULED_AT"
ENV_ATTEMPT: Final[str] = "DSL41_ATTEMPT"


class IdempotenceError(ValueError):
    """A declaration that cannot be read: one sentence naming the file and, where known, the
    line."""


@dataclass(frozen=True)
class Exceptions:
    """The jobs the operator marked non-idempotent, each with a one-line reason."""

    reasons: Mapping[str, str]

    @property
    def names(self) -> tuple[str, ...]:
        """The marked job names, sorted."""
        return tuple(sorted(self.reasons))

    def is_idempotent(self, name: str) -> bool:
        return name not in self.reasons

    def reason(self, name: str) -> str | None:
        """The operator's reason for a marked job, None for an idempotent one."""
        return self.reasons.get(name)

    def canonical_bytes(self) -> bytes:
        """The marked set as canonical bytes; an absent file and an empty table agree."""
        return canonical_bytes({"jobs": dict(self.reasons)})


def _leaf_path(table: Mapping[str, Any]) -> tuple[str, ...]:
    """The key path to the first leaf of a one-line parse."""
    path: list[str] = []
    node: Any = table
    while isinstance(node, Mapping) and node:
        key = next(iter(node))
        path.append(key)
        node = node[key]
    return tuple(path)


def _line_of(text: str, key_path: tuple[str, ...]) -> int | None:
    """The 1-based line whose key is `key_path` (relative to `[jobs]`), found by parsing each
    line alone; None when no single line carries it (a multi-line value)."""
    in_jobs = False
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            in_jobs = line.split("#", 1)[0].strip() == "[jobs]"
            continue
        if not in_jobs or "=" not in line:
            continue
        try:
            parsed = tomllib.loads(line)
        except tomllib.TOMLDecodeError:
            continue
        found = _leaf_path(parsed)
        if found[: len(key_path)] == key_path:
            return number
    return None


def _top_level_line(text: str, key: str) -> int | None:
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if line.startswith("[") and line.split("#", 1)[0].strip().strip("[]").strip() == key:
            return number
        if "=" in line and not line.startswith("#"):
            try:
                if key in tomllib.loads(line):
                    return number
            except tomllib.TOMLDecodeError:
                continue
    return None


def _where(path: Path, line: int | None) -> str:
    return f"{path}:{line}" if line is not None else str(path)


def load_exceptions(estate_dir: Path) -> Exceptions:
    """The exceptions declared in `estate_dir/non-idempotent.toml`; empty when the file is
    absent. A link that names no file is not absent: any other read failure is an
    IdempotenceError, as are a malformed file, a non-string reason and an empty one. A reason is
    one line; control characters such as ESC inside it are allowed, a stated limit: the reason is
    the operator's own text and is printed as written."""
    path = estate_dir / EXCEPTIONS_FILE
    if not os.path.lexists(path):
        return Exceptions({})
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise IdempotenceError(f"{path}: cannot read the declaration: {exc}") from exc
    return parse_exceptions(text, path)


def parse_exceptions(text: str, path: Path) -> Exceptions:
    """Interpret a captured exception declaration at its original path (DL-437)."""
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise IdempotenceError(f"{path}: malformed TOML: {exc}") from exc
    for key in document:
        if key != "jobs":
            where = _where(path, _top_level_line(text, key))
            raise IdempotenceError(f"{where}: unknown key {key!r}; the file has one [jobs] table")
    jobs = document.get("jobs", {})
    if not isinstance(jobs, dict):
        where = _where(path, _top_level_line(text, "jobs"))
        raise IdempotenceError(f"{where}: 'jobs' must be a table of job name to reason")
    reasons: dict[str, str] = {}
    for name, reason in jobs.items():
        if not name:
            where = _where(path, _line_of(text, ("",)))
            raise IdempotenceError(f"{where}: a job name is empty")
        where = _where(path, _line_of(text, (name,)))
        if isinstance(reason, dict):
            raise IdempotenceError(
                f"{where}: {name!r} is a table, not a reason; quote a job name that has a dot"
            )
        if not isinstance(reason, str):
            raise IdempotenceError(
                f"{where}: the reason for {name!r} must be a string, got {type(reason).__name__}"
            )
        if not reason.strip():
            raise IdempotenceError(f"{where}: the reason for {name!r} is empty")
        if reason.splitlines() != [reason]:
            raise IdempotenceError(f"{where}: the reason for {name!r} must be one line")
        reasons[name] = reason
    return Exceptions(reasons)


def invocation_env(
    job: str, run_number: int, scheduled_at: datetime, attempt: int | str
) -> dict[str, str]:
    """The four variables every launch exports (DSL41_JOB, DSL41_RUN_NUMBER,
    DSL41_SCHEDULED_AT, DSL41_ATTEMPT). `scheduled_at` is ISO-8601 UTC with a `Z` suffix,
    truncated to the second (the fraction is dropped, not rounded); a naive value is read as UTC,
    as everywhere else in the runner."""
    if scheduled_at.tzinfo is None:
        instant = scheduled_at.replace(tzinfo=UTC)
    else:
        instant = scheduled_at.astimezone(UTC)
    return {
        ENV_JOB: job,
        ENV_RUN_NUMBER: str(run_number),
        ENV_SCHEDULED_AT: instant.strftime("%Y-%m-%dT%H:%M:%SZ"),
        ENV_ATTEMPT: str(attempt),
    }
