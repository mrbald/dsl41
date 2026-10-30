"""Engine subprocess that dies between a decision and its dispatch (runner-design ss7,
DL-102).

`_admit_and_apply` journals the attempt and its decision, and then dispatches the starts the
decision implies, each behind its `dispatch` record. An engine that dies BETWEEN those leaves
exactly one thing behind: a decided start with no `dispatch` record, and nothing anywhere on the
host. That is what resume dispatches.

The window is real and it is two statements wide, so this driver picks the instant rather than
racing for it: `_dispatch` is replaced with an untrappable `os._exit`. What the patch chooses is
WHEN this process dies. Every record before that point was written and fsync'd by the ordinary
path (the real domain fsyncs per record), and nothing after it exists -- which is the same log a
`kill -9` landing in that window would leave.

Not a test file: no test_ prefix, imported by the test for its JIL only.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

from dsl41.ir import lower_source
from dsl41.oracle_state import Event
from dsl41.runner_adapters import LocalCommandAdapter
from dsl41.runner_clock import RealClock
from dsl41.runner_startup import start_run

#: `echo` rather than `true`: the re-drive has to be provable from the
#: estate's side, not just from the log's, and stdout landing in the run
#: directory's out.log is a side effect only a real command produces.
REDRIVE_JIL = """\
insert_job: lost
job_type: c
command: echo re-driven
"""

#: what a crash in this window exits with, so the test can tell it apart
#: from a driver that fell over on its way there.
CRASH_CODE = 9


async def main(run_root: str) -> None:
    catalog = lower_source(REDRIVE_JIL)
    clock = RealClock()
    engine = start_run(
        catalog,
        Path(run_root),
        clock=clock,
        adapters={"CMD": LocalCommandAdapter(grace_seconds=2.0)},
    )

    def die(_emitted: object) -> None:
        os._exit(CRASH_CODE)

    engine._dispatch = die  # type: ignore[method-assign]
    engine.inject(Event(at=clock.now(), kind="STARTJOB", payload={"job": "lost"}))
    await engine.run_until_quiescent(datetime.max)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
