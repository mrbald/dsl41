"""Literal invocation environments and dispatch-time stream paths (DL-447)."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, cast

from pydantic import (
    AfterValidator,
    StrictStr,
    ValidationError,
    ValidatorFunctionWrapHandler,
    WrapValidator,
)

from dsl41.idempotence import invocation_env
from dsl41.ir import ExecSpec, FwSpec, JobIR

NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
REFERENCE = re.compile(r"\$(?:([A-Za-z_][A-Za-z0-9_]*)|\{([A-Za-z_][A-Za-z0-9_]*)\})")
INSTANCE_NAMES = frozenset({"AUTOSERV", "AUTOSYS", "AUTOUSER", "AUTOROOT"})
ATTRIBUTE_NAMES = frozenset(
    {"__job_name", "__machine", "__run_machine", "__box_name", "__max_exit_success"}
)
STREAM_ATTRIBUTES = ("std_in_file", "std_out_file", "std_err_file")


def validate_job_env(value: dict[str, str]) -> dict[str, str]:
    for name, item in value.items():
        if NAME.fullmatch(name) is None:
            raise ValueError("job_env requires environment names [A-Za-z_][A-Za-z0-9_]*")
        if name.startswith("DSL41_") or name in {"AUTO_JOB_NAME", "AUTORUN"}:
            raise ValueError(f"job_env cannot override {name}")
        if "\0" in item:
            raise ValueError(f"job_env.{name} must not contain NUL")
    return value


def _redact_job_env(value: object, handler: ValidatorFunctionWrapHandler) -> dict[str, str]:
    try:
        return cast(dict[str, str], handler(value))
    except ValidationError as exc:
        errors = exc.errors(include_url=False)
        for error in errors:
            error["input"] = "<redacted job_env>"
        raise ValidationError.from_exception_data("job_env", errors) from None  # type: ignore[arg-type]


JobEnvironment = Annotated[
    dict[StrictStr, StrictStr], AfterValidator(validate_job_env), WrapValidator(_redact_job_env)
]

GENERATED_NAMES = frozenset(
    {
        "DSL41_JOB",
        "DSL41_RUN_NUMBER",
        "DSL41_SCHEDULED_AT",
        "DSL41_ATTEMPT",
        "DSL41_RUN",
        "DSL41_COOKIE",
        "AUTO_JOB_NAME",
        "AUTORUN",
    }
)


class RuntimeVariableError(ValueError):
    """A path that cannot be resolved before the profile runs (DL-447)."""


def inherited_environment(startup: Mapping[str, str]) -> dict[str, str]:
    return {
        name: value
        for name, value in startup.items()
        if name not in INSTANCE_NAMES | GENERATED_NAMES
    }


def path_environment(startup: Mapping[str, str]) -> dict[str, str]:
    """Availability and emptiness suffice for literal-path preflight (DL-447).

    Each nonempty inherited value is represented by a fixed symbol. No value is disclosed.
    """
    return {name: "set" if value else "" for name, value in inherited_environment(startup).items()}


def expand_path(
    job: str, attribute: str, text: str, env: Mapping[str, str]
) -> tuple[str, set[str]]:
    """Substitute each source reference once; values are never parsed again (DL-447)."""
    pieces: list[str] = []
    names: set[str] = set()
    offset = 0
    while (dollar := text.find("$", offset)) >= 0:
        pieces.append(text[offset:dollar])
        match = REFERENCE.match(text, dollar)
        if match is None:
            raise RuntimeVariableError(
                f"job {job}: {attribute} has unsupported dollar syntax; supported forms are"
                " $VAR and ${VAR}, with names [A-Za-z_][A-Za-z0-9_]*."
            )
        name = match.group(1) or match.group(2)
        if name in ATTRIBUTE_NAMES:
            raise RuntimeVariableError(
                f"job {job}: {attribute} references reserved variable {name}, unavailable in paths;"
                " use a literal path or a currently supported variable."
            )
        if name not in env:
            if name.startswith("DSL41_") or name in GENERATED_NAMES:
                raise RuntimeVariableError(
                    f"job {job}: {attribute} references unavailable generated variable {name};"
                    " paths resolve before profile execution; this name cannot be set in job_env."
                )
            raise RuntimeVariableError(
                f"job {job}: {attribute} references unavailable variable {name}; paths resolve"
                f" before profile execution; set engine.job_env.{name}."
            )
        pieces.append(env[name])
        names.add(name)
        offset = match.end()
    pieces.append(text[offset:])
    resolved = "".join(pieces)
    if not resolved or "\0" in resolved:
        raise RuntimeVariableError(
            f"job {job}: {attribute} resolves to an empty path or contains NUL."
        )
    return resolved, names


def resolve_streams(
    job: JobIR,
    env: Mapping[str, str],
    *,
    cwd: Path,
    defaults: Mapping[str, str] | None = None,
) -> tuple[dict[str, str | None], set[str]]:
    """Resolve stream paths, or validate an FW's literal watch path (DL-447)."""
    if isinstance(job.exec_, FwSpec) and "$" in (job.exec_.watch_file or ""):
        raise RuntimeVariableError(f"job {job.name}: watch_file with dollar syntax is unsupported.")
    paths: dict[str, str | None] = {}
    referenced: set[str] = set()
    if isinstance(job.exec_, ExecSpec):
        for attribute in STREAM_ATTRIBUTES:
            text = getattr(job.exec_, attribute)
            if text is None:
                resolved = (defaults or {}).get(attribute)
                if resolved is None:
                    paths[attribute] = None
                    continue
                names: set[str] = set()
                if not resolved or "\0" in resolved:
                    raise RuntimeVariableError(
                        f"job {job.name}: {attribute} resolves to an empty path or contains NUL."
                    )
            else:
                from dsl41.runtime_policy import stream_target

                target, _mode = stream_target(job.name, attribute, text, "append")
                resolved, names = expand_path(job.name, attribute, target, env)
            paths[attribute] = (
                resolved if os.path.isabs(resolved) else os.path.join(str(cwd), resolved)
            )
            referenced.update(names)
    return paths, referenced


def symbolic_environment(job: str) -> dict[str, str]:
    """Generated values are available before a dispatch sequence is allocated (DL-447)."""
    from datetime import datetime

    return {
        **invocation_env(job, 1, datetime(2000, 1, 1), 1),
        "DSL41_COOKIE": "symbolic",
        "AUTO_JOB_NAME": job,
        "AUTORUN": "1-1",
        "DSL41_RUN": "symbolic",
    }
