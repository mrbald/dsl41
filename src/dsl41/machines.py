"""The registry of declared state machines, for the docs generator and the coverage gate."""

from __future__ import annotations

from dsl41.engine_charts import ATTEMPT, ENGINE_LIFECYCLE
from dsl41.job_chart import JOB
from dsl41.oracle_state import RUNTIME_ASSEMBLY
from dsl41.runner_admission import ADMISSION
from dsl41.runner_journal import SUBSCRIPTION
from dsl41.scan_chart import QUEUE_SCAN
from dsl41.state_machine import Statechart

#: Every declared machine, each a `Statechart`.
MACHINES: tuple[Statechart, ...] = (
    JOB,
    QUEUE_SCAN,
    RUNTIME_ASSEMBLY,
    ADMISSION,
    SUBSCRIPTION,
    ENGINE_LIFECYCLE,
    ATTEMPT,
)
