"""The registry of declared state machines, for the docs generator and the coverage gate."""

from __future__ import annotations

from typing import Any

from dsl41.boundary import ANCHOR_HEAD, PERIOD_ROW
from dsl41.state_machine import StateMachine

# Later slices add each machine here.
MACHINES: tuple[StateMachine[Any], ...] = (ANCHOR_HEAD, PERIOD_ROW)
