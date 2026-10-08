"""The registry of declared state machines, for the docs generator and the coverage gate."""

from __future__ import annotations

from typing import Any

from dsl41.boundary import ANCHOR_HEAD, PERIOD_ROW
from dsl41.oracle_state import JOB_FLAGS, JOB_HOLDING, JOB_STATUS, RUNTIME_ASSEMBLY
from dsl41.runner import SEAL_BOUNDARY
from dsl41.runner_adapters import SUPERVISOR_CLIENT
from dsl41.runner_admission import ADMISSION
from dsl41.runner_effects import EFFECT
from dsl41.runner_hosts import HOST
from dsl41.runner_journal import SUBSCRIPTION
from dsl41.runner_supervisor import SUPERVISOR_LEASE, SUPERVISOR_PROCESS
from dsl41.state_machine import StateMachine

# Later slices add each machine here.
MACHINES: tuple[StateMachine[Any], ...] = (
    JOB_STATUS,
    JOB_FLAGS,
    JOB_HOLDING,
    RUNTIME_ASSEMBLY,
    ANCHOR_HEAD,
    PERIOD_ROW,
    SUPERVISOR_PROCESS,
    SUPERVISOR_LEASE,
    SUPERVISOR_CLIENT,
    HOST,
    ADMISSION,
    EFFECT,
    SUBSCRIPTION,
    SEAL_BOUNDARY,
)
