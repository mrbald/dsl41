"""Engine subprocess for the crash-recovery test (runner-design ss13 item 3).

Starts a real-domain run (RealClock + LocalCommandAdapter) over a tiny
inline estate, injects STARTJOB for every job, prints DRIVER-READY, and
runs until quiescent (which the slow jobs postpone for minutes). The test
SIGKILLs this process mid-run; the wrappers' lifeline EOF then kills and
records every command (tethered semantics, ss6a), and the test resumes
from the journal in-process.

Not a test file: no test_ prefix, imported by nothing.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

from dsl41.ir import lower_source
from dsl41.oracle import Event
from dsl41.runner import LocalCommandAdapter, RealClock, start_run

CRASH_JIL = """\
insert_job: fast
job_type: c
command: sleep 0.3

insert_job: slow_one
job_type: c
command: sleep 120

insert_job: slow_two
job_type: c
command: sleep 120
"""


async def main(run_root: str) -> None:
    catalog = lower_source(CRASH_JIL)
    clock = RealClock()
    engine = start_run(
        catalog,
        Path(run_root),
        clock=clock,
        adapters={"CMD": LocalCommandAdapter(grace_seconds=2.0)},
    )
    now = clock.now()
    for job in ("fast", "slow_one", "slow_two"):
        engine.inject(Event(at=now, kind="STARTJOB", payload={"job": job}))
    print("DRIVER-READY", flush=True)
    await engine.run_until_quiescent(datetime.max)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
