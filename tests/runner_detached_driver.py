"""Detached-engine subprocess for the 11f kill-matrix tests (spec ss5 items
1 and 4). Starts a real-domain DETACHED run (SupervisorClient +
SupervisedCommandAdapter) over one inline `slow` CMD job, opens a control
socket, injects STARTJOB, prints DRIVER-READY, and holds open.

Two kill paths the tests drive:
- SIGKILL of this process: the supervisor (a separate process) keeps the
  wrapper's lifeline, so the job SURVIVES; the test resumes and reattaches.
- SIGINT/SIGTERM: an orderly detach-stop (release the lease, close the
  client) that leaves the job running under the supervisor.

Not a test file: no test_ prefix, imported by nothing.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from datetime import datetime
from pathlib import Path

from dsl41.ir import lower_source
from dsl41.oracle import Event
from dsl41.runner import (
    ControlServer,
    FileWatcherAdapter,
    RealClock,
    SupervisedCommandAdapter,
    SupervisorClient,
    start_run,
)


async def main(run_root: str, sleep_s: str) -> None:
    catalog = lower_source(f"insert_job: slow\njob_type: c\ncommand: sleep {sleep_s}; exit 0\n")
    root = Path(run_root)
    root.mkdir(parents=True, exist_ok=True)
    client = SupervisorClient(root)
    await client.ensure_running()
    await client.acquire()
    engine = start_run(
        catalog,
        root,
        clock=RealClock(),
        adapters={
            "CMD": SupervisedCommandAdapter(client, grace_seconds=2.0),
            "FW": FileWatcherAdapter(),
        },
        hold_open=True,
    )
    server = ControlServer(engine, root / "control.sock")
    await server.start()
    engine.inject(Event(at=engine.clock.now(), kind="STARTJOB", payload={"job": "slow"}))
    loop_task = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
    stop = asyncio.Event()
    aloop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        aloop.add_signal_handler(sig, stop.set)
    print("DRIVER-READY", flush=True)
    stop_task = asyncio.ensure_future(stop.wait())
    done, _ = await asyncio.wait({loop_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
    engine.detach.stopping = True  # teardown must not kill: jobs continue (ss3 case b)
    stop_task.cancel()
    if loop_task not in done:
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass
    await server.close()
    await engine.shutdown()
    await client.release()
    await client.close()
    if engine.journal is not None:
        engine.journal.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
