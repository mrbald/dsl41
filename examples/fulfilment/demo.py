"""Run a synthetic warehouse wave, then reconcile a committed label's lost reply."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workflows"))
from support import Engine, cli, make_properties, new_run, write_json  # noqa: E402
from worker import initialise  # noqa: E402

BASE = Path(__file__).resolve().parent


def inspect(run: Path, phase: str) -> dict:
    result = subprocess.run(
        [sys.executable, str(BASE / "check.py"), "--run", str(run), "--phase", phase],
        text=True,
        capture_output=True,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError(f"fulfilment check failed: {result.stderr}")
    evidence = json.loads(result.stdout)
    write_json(run / f"check-{phase}.json", evidence)
    return evidence


def demo(requested: Path | None, incident: bool, leave_incident: bool = False) -> Path:
    if leave_incident and not incident:
        raise ValueError("--leave-incident requires the default incident")
    database_url = os.environ["DATABASE_URL"]
    run = new_run("fulfilment", requested)
    print(f"run: {run}", flush=True)
    suffix = uuid.uuid4().hex
    config = {"schema": "fulfilment_" + suffix, "wave": "wave-" + suffix, "incident": incident}
    write_json(run / "config.json", config)
    initialise(config)
    engine = None
    service = None
    try:
        args = [
            sys.executable,
            str(BASE / "carrier.py"),
            "--ledger",
            str(run / "carrier.sqlite"),
            "--ready",
            str(run / "carrier-ready.json"),
        ]
        if incident:
            args += ["--drop-key", f"{config['wave']}/P2/1/label"]
        with (run / "carrier.log").open("ab") as log:
            service = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 15
        while not (run / "carrier-ready.json").exists():
            if service.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError(f"carrier startup failed: {run}/carrier.log")
            time.sleep(0.05)
        config["carrier_url"] = json.loads((run / "carrier-ready.json").read_text())["url"]
        write_json(run / "config.json", config)
        properties = make_properties(run, BASE / "worker.py", {"DATABASE_URL": database_url})
        engine = Engine(run, BASE / "estate.jil", properties)
        engine.start()
        engine.event("STARTJOB", "FF_WAVE_B")
        if incident:
            engine.wait_job("FF_LABEL_2_C", "FAILURE")
            inspect(run, "incident")
            write_json(run / "status-incident.json", engine.status())
            if leave_incident:
                engine.stop()
                write_json(
                    run / "runs.json",
                    json.loads(cli("runs", str(engine.root), "--format", "json").stdout),
                )
                print("Incident retained for manual recovery. Stopping the carrier before exit.")
                return run
            engine.event("FORCE_STARTJOB", "FF_LABEL_2_C")
        engine.wait_job("FF_MANIFEST_C")
        engine.wait_job("FF_WAVE_B")
        write_json(run / "status-complete.json", engine.status())
        engine.stop()
        write_json(
            run / "runs.json", json.loads(cli("runs", str(engine.root), "--format", "json").stdout)
        )
        # Refuse conflicting reuse of an existing identity. A refusal changes no effect.
        request = Request(
            config["carrier_url"] + "/operations",
            json.dumps(
                {
                    "key": f"{config['wave']}/P1/1/label",
                    "payload": {"action": "label", "quantity": 999},
                }
            ).encode(),
            {"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=3):
                raise RuntimeError("carrier accepted conflicting operation content")
        except HTTPError as exc:
            if exc.code != 409:
                raise
            write_json(run / "identity-conflict.json", {"http_status": exc.code})
        print(json.dumps(inspect(run, "complete"), indent=2))
        return run
    finally:
        try:
            if engine is not None:
                engine.stop()
        finally:
            if service is not None and service.poll() is None:
                service.terminate()
                try:
                    service.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    service.kill()
                    service.wait(timeout=5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--no-incident", action="store_true")
    parser.add_argument("--leave-incident", action="store_true")
    args = parser.parse_args()
    demo(args.run_dir, not args.no_incident, args.leave_incident)
