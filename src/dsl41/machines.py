"""The registry of declared state machines, for the docs generator and the coverage gate."""

from __future__ import annotations

from typing import Any

from dsl41.boundary import ANCHOR_HEAD, PERIOD_ROW
from dsl41.oracle_state import HOST, JOB_FLAGS, JOB_HOLDING, JOB_STATUS, RUNTIME_ASSEMBLY
from dsl41.runner_seal import SEAL_BOUNDARY
from dsl41.runner_adapters import SUPERVISOR_CLIENT
from dsl41.runner_admission import ADMISSION
from dsl41.runner_effects import EFFECT
from dsl41.runner_journal import SUBSCRIPTION
from dsl41.runner_supervisor import SUPERVISOR_LEASE, SUPERVISOR_PROCESS
from dsl41.state_machine import StateMachine, Statechart

# Later slices add each machine here; a converted machine is a `Statechart`.
MACHINES: tuple[StateMachine[Any] | Statechart, ...] = (
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
