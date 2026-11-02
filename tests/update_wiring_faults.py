"""One isolated fault scenario; the calling pytest process owns the watchdog (DL-42)."""

import asyncio
import json
import os
from pathlib import Path
import sys
import subprocess
import time

import pytest

import update_wiring_barrier
from test_update_wiring import _barrier_scenario
from update_wiring_barrier import (
    FAULT_DEADLINES_S,
    INVENTORY_S,
    RerunBarrier,
    Supervision,
    process_table,
)


def main() -> None:
    fault, directory = sys.argv[1:]
    root = Path(directory)
    started = time.monotonic()
    barrier = RerunBarrier(
        root / "barrier", before_record=fault != "late-registration", fault=fault
    )
    error: BaseException | None = None
    diagnostic = ""
    with pytest.MonkeyPatch.context() as monkeypatch:
        if fault == "no-waitid":
            monkeypatch.delattr(os, "waitid", raising=False)
            assert not hasattr(os, "waitid")
        if fault == "inventory-failure":
            cleanup = Supervision.cleanup
            inventory_calls = 0

            def failed_inventory():
                nonlocal inventory_calls
                inventory_calls += 1
                if inventory_calls == 1:
                    raise subprocess.TimeoutExpired(["ps", "-axo"], timeout=INVENTORY_S)
                raise subprocess.CalledProcessError(1, ["ps", "-axo"])

            async def cleanup_without_inventory(supervision):
                with pytest.MonkeyPatch.context() as fault_patch:
                    fault_patch.setattr(update_wiring_barrier, "process_table", failed_inventory)
                    await cleanup(supervision)

            monkeypatch.setattr(Supervision, "cleanup", cleanup_without_inventory)
        try:
            # This path submits no executor work, so no executor waiter needs probing (DL-42).
            asyncio.run(
                _barrier_scenario(
                    root,
                    monkeypatch,
                    ("post_record" if fault == "late-registration" else "post_spawn_pre_record"),
                    barrier,
                )
            )
        except BaseException as exc:
            error = exc
            diagnostic = "\n".join(getattr(exc, "__notes__", []))
    supervision = barrier.supervision
    if not diagnostic:
        diagnostic = supervision.diagnostic()
    report = dict(
        deadline=FAULT_DEADLINES_S[fault],
        elapsed=time.monotonic() - started,
        exception=repr(error) if error is not None else None,
        exception_type=type(error).__name__ if error is not None else None,
        failed_phase=supervision.phase,
        engine_exception_preserved=(
            supervision.task.done()
            and not supervision.task.cancelled()
            and error is supervision.task.exception()
        ),
        diagnostic=diagnostic,
        owned=sorted(barrier.owned),
        survivors=sorted(barrier.owned & process_table().keys()),
        inventory_errors=[repr(exc) for exc in supervision.inventory_errors],
        released=barrier.release.is_set(),
        engine_task_done=supervision.task.done(),
        shutdown_done=supervision.shutdown_task is not None and supervision.shutdown_task.done(),
        wrappers_reaped=all(
            launch.proc is None or launch.proc.returncode is not None
            for launch in barrier.all_launches.values()
        ),
        open_fifos=barrier.open_fifos,
        skipped_signals=supervision.skipped_signals,
    )
    (root / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)
    assert not report["survivors"], report
    assert report["released"] and report["engine_task_done"] and report["shutdown_done"], report
    assert report["wrappers_reaped"] and not report["open_fifos"], report


if __name__ == "__main__":
    main()
