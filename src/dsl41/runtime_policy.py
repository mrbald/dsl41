"""Pure execution-policy inputs for process and engine integration."""

from __future__ import annotations

import os
import pwd
import re
import signal
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    ValidationError,
    field_validator,
)

from dsl41.conditions import And, ExitCodeAtom, Paren, StatusAtom, Or
from dsl41.ir import ExecSpec, FwSpec, JobIR, find_var_sites, unquote_jil_value
from dsl41.runner_environment import ATTRIBUTE_NAMES, JobEnvironment as BaseJobEnvironment

POLICY_FIELDS = (
    "job_profile",
    "job_shell",
    "std_file_mode",
    "kill_signals",
    "set_job_attribute_environmentals",
)
StreamMode = Literal["append", "truncate"]


def reserve_attributes(value: dict[str, str]) -> dict[str, str]:
    if ATTRIBUTE_NAMES.intersection(value):
        raise ValidationError.from_exception_data(
            "job_env",
            [
                {
                    "type": "value_error",
                    "loc": (),
                    "input": "<redacted job_env>",
                    "ctx": {
                        "error": ValueError(
                            "job_env cannot override reserved job attribute environment names"
                        )
                    },
                }
            ],
        )
    return value


JobEnvironment = Annotated[BaseJobEnvironment, AfterValidator(reserve_attributes)]


class RuntimePolicySettings(BaseModel):
    """Revisioned settings; execution activation belongs to the integration slice."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_profile: str | None = Field(default=None, exclude_if=lambda value: value is None)
    job_shell: str | None = Field(default=None, exclude_if=lambda value: value is None)
    std_file_mode: StreamMode = Field(default="append", exclude_if=lambda value: value == "append")
    kill_signals: tuple[StrictInt, ...] = Field(
        default=(2, 9), exclude_if=lambda value: value == (2, 9)
    )
    set_job_attribute_environmentals: StrictBool = Field(
        default=True, exclude_if=lambda value: value is True
    )

    @field_validator("job_profile")
    @classmethod
    def literal_profile(cls, value: str | None) -> str | None:
        if value is not None and (not value.startswith("/") or "$" in value or "\0" in value):
            raise ValueError(
                "job_profile requires one literal absolute path without variables or NUL"
            )
        return value

    @field_validator("job_shell")
    @classmethod
    def shell_path(cls, value: str | None) -> str | None:
        if value is not None and (
            not os.path.isabs(value)
            or "\0" in value
            or Path(value).name not in {"sh", "dash", "bash", "ksh"}
        ):
            raise ValueError("job_shell requires an absolute path to sh, dash, bash or ksh")
        return value

    @field_validator("kill_signals", mode="before")
    @classmethod
    def signal_sequence(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("kill_signals")
    @classmethod
    def signal_ladder(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if not value or any(n <= 0 or n not in signal.valid_signals() for n in value):
            raise ValueError("kill_signals requires a nonempty list of supported positive signals")
        if value[-1] != signal.SIGKILL:
            raise ValueError("kill_signals must end with KILL (9) for bounded escalation")
        return value


def policy_values(settings: Mapping[str, object]) -> dict[str, object]:
    """Normalized policy fields, including defaults, for a captured runtime revision."""
    policy = RuntimePolicySettings.model_validate(
        {key: settings[key] for key in POLICY_FIELDS if key in settings}
    )
    return {key: getattr(policy, key) for key in POLICY_FIELDS}


def resolve_job_shell(
    explicit: str | None, passwd_shell: str | None = None
) -> tuple[str, str | None]:
    """Resolve once on the engine host; return the selected path and optional fallback warning."""

    def usable(path: str) -> bool:
        return (
            os.path.isabs(path)
            and Path(path).name in {"sh", "dash", "bash", "ksh"}
            and os.path.isfile(path)
            and os.access(path, os.X_OK)
        )

    if explicit is not None:
        if not usable(explicit):
            raise ValueError("engine.job_shell must name an executable sh, dash, bash or ksh")
        return explicit, None
    shell = pwd.getpwuid(os.geteuid()).pw_shell if passwd_shell is None else passwd_shell
    if usable(shell):
        return shell, None
    return "/bin/sh", f"passwd shell {shell!r} is unsupported or unavailable; using /bin/sh"


@dataclass(frozen=True)
class PolicyFinding:
    code: str
    job: str
    attribute: str
    reason: str
    alternative: str

    @property
    def message(self) -> str:
        return f"job {self.job}: {self.attribute}: {self.reason}; {self.alternative}"


class PolicyRefused(ValueError):
    def __init__(self, finding: PolicyFinding):
        self.finding = finding
        super().__init__(f"[{finding.code}] {finding.message}")


def refuse(job: str, attribute: str, code: str, reason: str, alternative: str) -> None:
    raise PolicyRefused(PolicyFinding(code, job, attribute, reason, alternative))


def stream_target(
    job: str, attribute: str, text: str, default: StreamMode
) -> tuple[str, StreamMode | None]:
    """Read only source operators; expanded values are always literal path text."""
    mode = None if attribute == "std_in_file" else default
    prefix = ">>" if text.startswith(">>") else ">" if text.startswith(">") else ""
    if prefix:
        if attribute == "std_in_file":
            refuse(job, attribute, "std-file", "output prefix on stdin", "use a literal input path")
        text = text[len(prefix) :]
        mode = "append" if prefix == ">>" else "truncate"
        if not text or text[0].isspace():
            refuse(
                job,
                attribute,
                "std-file",
                "empty or whitespace-prefixed output target",
                "put the path immediately after > or >>",
            )
    if not text or "\0" in text:
        refuse(job, attribute, "std-file", "empty target or NUL", "use a nonempty path without NUL")
    return text, mode


@dataclass(frozen=True)
class ResourceLimit:
    resource: str
    soft: int | None
    hard: int | None


_LIMIT = re.compile(r'([a-z])\s*=\s*"([0-9]+|unlimited)\s*,\s*([0-9]+|unlimited)"')
LIMIT_RESOURCES = {
    "c": ("RLIMIT_CORE", 512),
    "d": ("RLIMIT_DATA", 1024),
    "f": ("RLIMIT_FSIZE", 512),
    "m": ("RLIMIT_AS", 1024),
    "n": ("RLIMIT_NOFILE", 1),
    "s": ("RLIMIT_STACK", 1024),
    "t": ("RLIMIT_CPU", 1),
    "r": ("RLIMIT_RTPRIO", 1),
    "l": ("RLIMIT_MEMLOCK", 1024),
}


def parse_limits(job: str, text: str) -> tuple[ResourceLimit, ...]:
    limits: list[ResourceLimit] = []
    rest = text.strip()
    seen: set[str] = set()
    while rest:
        match = _LIMIT.match(rest)
        if match is None:
            refuse(
                job, "ulimit", "ulimit", "invalid resource pair", 'use resource="soft,hard" pairs'
            )
        assert match is not None
        name, raw_soft, raw_hard = match.groups()
        if name not in LIMIT_RESOURCES or name in seen:
            refuse(
                job,
                "ulimit",
                "ulimit",
                "unsupported or duplicate resource",
                "use each of c,d,f,m,n,s,t,r,l once",
            )
        seen.add(name)
        unit = LIMIT_RESOURCES[name][1]

        def limit_value(text: str) -> int | None:
            if text == "unlimited":
                return None
            digits = text.lstrip("0") or "0"
            if len(digits) > 20 or int(digits) * unit >= 2**64 - 1:
                refuse(
                    job,
                    "ulimit",
                    "ulimit",
                    "resource limit exceeds the representable range",
                    "use a representable limit or unlimited",
                )
            return int(digits) * unit

        soft = limit_value(raw_soft)
        hard = limit_value(raw_hard)
        if hard is not None and (soft is None or soft > hard):
            refuse(
                job,
                "ulimit",
                "ulimit",
                "soft limit exceeds hard limit",
                "set soft less than or equal to hard",
            )
        limits.append(ResourceLimit(name, soft, hard))
        rest = rest[match.end() :].strip()
        if not rest:
            break
        if not rest.startswith(",") or not rest[1:].strip():
            refuse(
                job,
                "ulimit",
                "ulimit",
                "invalid pair separator",
                "separate resource pairs with commas",
            )
        rest = rest[1:].strip()
    if not limits:
        refuse(job, "ulimit", "ulimit", "empty limit list", 'use resource="soft,hard" pairs')
    return tuple(limits)


def validate_watch_pattern(job: str, text: str) -> None:
    path = PurePosixPath(text)
    if (
        not text
        or "\0" in text
        or any(c in text for c in "[]{}\\")
        or any(c in str(path.parent) for c in "*?")
        or "**" in text
    ):
        refuse(
            job,
            "watch_file",
            "watch-pattern",
            "unsupported filename pattern",
            "use * or ? only in the final filename component",
        )


def execution_fields(job: JobIR) -> dict[str, str]:
    if job.exec_ is None:
        return {}
    return {
        key: value
        for key, value in job.exec_.model_dump(exclude={"kind"}).items()
        if isinstance(value, str)
    }


def _raw(job: JobIR) -> dict[str, str]:
    return {
        key.lower(): unquote_jil_value(value)
        for lane in (job.annotations, job.passthrough)
        for key, value in lane.items()
    }


def _integer(job: str, attribute: str, text: str) -> int:
    digits = text.lstrip("0") or "0"
    if re.fullmatch(r"[0-9]+", text) is None or len(digits) > 10 or int(digits) > 2147483647:
        refuse(
            job,
            attribute,
            "runtime-attribute",
            "expected an integer from 0 through 2147483647",
            "use a nonnegative integer in that range",
        )
    return int(digits)


def _boolean(job: str, attribute: str, text: str) -> bool:
    if text.lower() not in {"y", "n", "1", "0"}:
        refuse(job, attribute, "runtime-attribute", "expected y/n/1/0", "use y, n, 1 or 0")
    return text.lower() in {"y", "1"}


@dataclass(frozen=True)
class JobRuntimePolicy:
    n_retrys: int = 0
    min_run_alarm: int | None = None
    max_run_alarm: int | None = None
    alarm_if_fail: bool = True
    alarm_if_terminated: bool = True
    send_notification: bool = False
    limits: tuple[ResourceLimit, ...] = ()


def job_policy(job: JobIR, *, hook_configured: bool = False) -> JobRuntimePolicy:
    """Validate interpreted attributes without changing the preserved IR or executing them."""
    name, raw = job.name, _raw(job)
    for attribute, reason, alternative in (
        ("job_class", "no class capacity contract is defined", "use explicit resources"),
        (
            "permission",
            "per-job permissions conflict with the engine access model",
            "use the engine access map",
        ),
        (
            "auto_delete",
            "automatic deletion would mutate the configured estate",
            "remove the definition through reread and update",
        ),
        (
            "shell",
            "explicit JIL shells are unsupported",
            "configure engine.job_shell or use its default",
        ),
    ):
        if attribute in raw:
            refuse(name, attribute, attribute.replace("_", "-"), reason, alternative)
    for attribute, value in execution_fields(job).items():
        if "$$" in value:
            sites = find_var_sites(attribute, value)
            reason = "AutoSys substitution and JIL PID shorthand are unsupported"
            if sites:
                reason += f" (global {sites[0].name})"
            refuse(
                name,
                attribute,
                "global-substitution",
                reason,
                "use a single-dollar shell variable in commands or engine.job_env in paths",
            )
    for attribute in ("condition", "box_success", "box_failure"):
        condition = getattr(job.sem, attribute)
        todo = [] if condition is None else [condition.cond]
        while todo:
            node = todo.pop()
            if isinstance(node, (And, Or)):
                todo.extend(node.operands)
            elif isinstance(node, Paren):
                todo.append(node.inner)
            elif isinstance(node, (StatusAtom, ExitCodeAtom)) and node.job.instance is not None:
                refuse(
                    name,
                    attribute,
                    "external-instance",
                    "cross-instance conditions require another scheduler instance",
                    "use local job conditions or an explicit integration job",
                )
    if type(job.sem.n_retrys) is not int or not 0 <= job.sem.n_retrys <= 20:
        refuse(
            name,
            "n_retrys",
            "n-retrys",
            "expected an integer from 0 through 20",
            "use 0 through 20 on CMD",
        )
    if job.sem.n_retrys and job.job_type != "CMD":
        refuse(
            name,
            "n_retrys",
            "n-retrys",
            "BOX/FW retry reset semantics are unqualified",
            "use 0 on BOX/FW",
        )
    fields = execution_fields(job)
    if job.job_type == "FW":
        for attribute in ("profile", "std_in_file", "std_out_file", "std_err_file"):
            if attribute in fields or attribute in raw:
                refuse(
                    name,
                    attribute,
                    "fw-execution",
                    "engine-side file watchers have no shell execution environment",
                    "use a CMD job for profiles and streams",
                )
        assert isinstance(job.exec_, FwSpec)
        validate_watch_pattern(name, job.exec_.watch_file)
    for attribute in ("notification_template", "notification_alarm_types"):
        if raw.get(attribute):
            refuse(
                name,
                attribute,
                "notification-unsupported",
                "template lookup and alarm filter grammar are unqualified",
                "leave this field empty and use hook policy",
            )
    heartbeat = _integer(name, "heartbeat_interval", raw.get("heartbeat_interval", "0"))
    if heartbeat:
        refuse(
            name,
            "heartbeat_interval",
            "heartbeat-unsupported",
            "no qualified heartbeat producer and interval/rearm contract; process liveness is not a heartbeat.",
            "use 0 or omit heartbeat_interval",
        )
    enabled = _boolean(name, "send_notification", raw.get("send_notification", "0"))
    if enabled and not hook_configured:
        refuse(
            name,
            "send_notification",
            "notification-hook",
            "enabled notifications require a configured hook",
            "configure engine.status_hook or disable send_notification",
        )
    limits: tuple[ResourceLimit, ...] = ()
    if "ulimit" in raw:
        if job.job_type != "CMD":
            refuse(
                name,
                "ulimit",
                "ulimit",
                "resource limits apply only to CMD children",
                "use a CMD job",
            )
        limits = parse_limits(name, raw["ulimit"])
    return JobRuntimePolicy(
        n_retrys=job.sem.n_retrys,
        min_run_alarm=None
        if "min_run_alarm" not in raw
        else _integer(name, "min_run_alarm", raw["min_run_alarm"]),
        max_run_alarm=None
        if "max_run_alarm" not in raw
        else _integer(name, "max_run_alarm", raw["max_run_alarm"]),
        alarm_if_fail=_boolean(name, "alarm_if_fail", raw.get("alarm_if_fail", "y")),
        alarm_if_terminated=_boolean(
            name, "alarm_if_terminated", raw.get("alarm_if_terminated", "y")
        ),
        send_notification=enabled,
        limits=limits,
    )


def attribute_environment(job: JobIR, run_machine: str, *, enabled: bool = True) -> dict[str, str]:
    """Generated overlay; the caller removes ATTRIBUTE_NAMES from inherited environments."""
    if not enabled:
        return {}
    values = {"__job_name": job.name, "__run_machine": run_machine}
    if job.exec_ is not None and job.exec_.machine is not None:
        values["__machine"] = job.exec_.machine
    if job.box.box_name is not None:
        values["__box_name"] = job.box.box_name
    if isinstance(job.exec_, ExecSpec):
        values["__max_exit_success"] = str(job.sem.max_exit_success)
    return values
