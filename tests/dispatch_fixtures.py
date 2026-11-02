"""Complete synthetic version-28 dispatch fixtures (DL-447)."""

from datetime import UTC
from typing import Any

from dsl41.runner_journal import dispatch_record as record
from dsl41.runner_procid import invocation_tag


def dispatch_record(job: str, run_number: int, **kwargs: Any):
    first = kwargs.pop("first_dispatch_seq", 1)
    ordinal = kwargs.get("ordinal", 1)
    at = kwargs["started_at"].replace(tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    env = {
        "DSL41_JOB": job,
        "DSL41_RUN_NUMBER": str(run_number),
        "DSL41_SCHEDULED_AT": at,
        "DSL41_ATTEMPT": str(ordinal),
        "AUTO_JOB_NAME": job,
        "AUTORUN": f"{first}-{ordinal}",
        "DSL41_RUN": invocation_tag(kwargs["attempt"], job, run_number, None),
        **kwargs.pop("env", {}),
    }
    env["DSL41_RUN"] = invocation_tag(kwargs["attempt"], job, run_number, None)
    return record(
        job,
        run_number,
        first_dispatch_seq=first,
        env=env,
        inherited={},
        paths={},
        cwd="/",
        **kwargs,
    )
