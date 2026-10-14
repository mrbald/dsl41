"""The second oracle's checks (docs/statechart-engine.md §11, DL-341,
DL-350).

The chart decides every job move, and the hook (`oracle_chart.ChartOracle`)
writes what the fired rows reached, with the old tables' transitions, so the
old tables keep validating each write inside the store's verbs. The checks
are §11's, each a statechart error: the job type behind a row that takes a
job's or a box's path, before the write; a settlement row whose holding move
differs from the one the store's verb makes; at each macrostep's end, every
job an item reached has its row project to its instance's configuration; and
at each input's commit, every job the input touched does
(`RuntimeState.commit_input`). Beside them, a fired row the hook has no write
for, or whose write needs a fact the payload does not carry, is one when it
fires (`AgreeingOracle._unwritten`). The old-versus-new harness stays the
arbiter of what the chart decides (DL-338).
"""

from __future__ import annotations

from dsl41.oracle import Oracle
from dsl41.state_machine import Outcome, StatechartError


class AgreeingOracle(Oracle):
    """`Oracle` with the statechart error of a row the hook does not write."""

    def _unwritten(self, job: str, outcome: Outcome, what: str) -> StatechartError:
        """The chart fired rows the oracle has no write for. Raised from the
        hook, the executor gives it the item's facts."""
        rows = ", ".join(outcome.rows) or "no row"
        return StatechartError(
            f"job {job!r}: {outcome.event.name} fired {rows}, and no {what} the oracle writes"
        )


def listed(active: frozenset[str]) -> str:
    return ", ".join(sorted(active))
