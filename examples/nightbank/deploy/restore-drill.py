"""O: installed destructive restore, independent effects and refusal (DL-443); lane S."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def worker(night: Path) -> None:
    invocation = {
        key: os.environ[key]
        for key in ("DSL41_JOB", "DSL41_RUN_NUMBER", "DSL41_SCHEDULED_AT", "DSL41_ATTEMPT")
    }
    with (night / "restore-invocations").open("a") as stream:
        stream.write(json.dumps(invocation) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    with (night / "restore-writer").open("a") as stream:
        while not (night / "restore-release").exists():
            stream.write("active\n")
            stream.flush()
            os.fsync(stream.fileno())
            (night / "restore-ready").write_text(invocation["DSL41_ATTEMPT"])
            time.sleep(0.02)
    with (night / "restore-effects").open("a") as stream:
        stream.write(f"{invocation['DSL41_RUN_NUMBER']} {invocation['DSL41_ATTEMPT']}\n")
        stream.flush()
        os.fsync(stream.fileno())


def command(*args: str, timeout: int = 60) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd="/")
    assert result.returncode == 0, (args, result.returncode, result.stdout, result.stderr)
    return result.stdout


def wait(check, description: str, seconds: int = 60):
    deadline = time.monotonic() + seconds
    while True:
        result = check()
        if result:
            return result
        assert time.monotonic() < deadline, description
        time.sleep(0.1)


def manifest(paths: dict[str, Path]) -> dict:
    rows = {}
    for name, base in paths.items():
        for path in [base, *sorted(base.rglob("*"))] if base.is_dir() else [base]:
            info = path.stat()
            rows[f"{name}/{path.relative_to(base)}"] = (
                info.st_mode,
                info.st_uid,
                info.st_gid,
                hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
            )
    return rows


def drill(dsl41: str, root: Path, config: Path, inputs: Path, night: Path, state: Path, unit: str):
    python = str(Path(dsl41).with_name("python"))
    socket = str(root / "control.sock")
    backup = state / "restore-backup"
    assert not backup.exists(), backup
    backup.mkdir()

    def cli(*args: str):
        return command("sudo", "-u", "dsl41", "-H", dsl41, *args)

    def status():
        return json.loads(cli("query", "status", "--json", "--socket", socket))

    def event(verb: str, job: str):
        cli("sendevent", verb, "--job", job, "--socket", socket)

    def settled():
        row = status()["jobs"]["DRILL_RESTORE_C"]
        return row["status"] == "SUCCESS" and "attempt" not in row

    def start():
        command("systemctl", "reset-failed", unit)
        command("systemctl", "start", unit)

        def running():
            result = subprocess.run(
                ["sudo", "-u", "dsl41", dsl41, "query", "status", "--json", "--socket", socket],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.returncode == 0 and json.loads(result.stdout)["engine"] == "RUNNING"

        wait(running, "restored service RUNNING", 120)

    installed_worker = inputs / "bin/restore-worker.py"
    shutil.copy2(__file__, installed_worker)
    (inputs / "cfg/restore.jil").write_text(
        "insert_job: DRILL_RESTORE_C\njob_type: c\nmachine: bank_core\nowner: dsl41\n"
        f"command: exec {python} {installed_worker} worker {night}\n"
        "condition: v(DRILL_GATE) = OPEN\n\n"
        "insert_job: DRILL_RESTORE_HOLD_C\njob_type: c\ncommand: true\n"
        "machine: bank_core\ncondition: v(DRILL_GATE) = OPEN\n\n"
        "insert_job: DRILL_RESTORE_ICE_C\njob_type: c\ncommand: true\n"
        "machine: bank_core\ncondition: v(DRILL_GATE) = OPEN\n"
    )
    cli("reread", "-S", socket)
    cli("update", "-S", socket, "--wait")
    revision = status()["config_rev"]
    event("ON_HOLD", "DRILL_RESTORE_HOLD_C")
    event("ON_ICE", "DRILL_RESTORE_ICE_C")
    cli("sendevent", "SET_GLOBAL", "--global", "DRILL_RESTORE_PIN=durable", "--socket", socket)

    release = night / "restore-release"
    ready = night / "restore-ready"
    release.touch()
    event("FORCE_STARTJOB", "DRILL_RESTORE_C")
    wait(settled, "first run SUCCESS")
    release.unlink()
    event("FORCE_STARTJOB", "DRILL_RESTORE_C")
    wait(lambda: ready.exists(), "writer acknowledgement")
    assert (night / "restore-writer").stat().st_size > 0
    row = status()["jobs"]["DRILL_RESTORE_C"]
    assert row["run_number"] == 2 and row["attempt"]["n"] == 1
    assert row["attempt"]["state"] == "RUNNING"
    cgroup = command("systemctl", "show", "-p", "ControlGroup", "--value", unit).strip()
    command("systemctl", "stop", unit)
    assert command("systemctl", "show", "-p", "ActiveState", "--value", unit).strip() == "inactive"
    assert "engine stopped: clean_shutdown" in command(
        "journalctl", "-u", unit, "-o", "cat", "--no-pager"
    )

    def no_service_processes():
        result = subprocess.run(["pgrep", "-u", "dsl41"], capture_output=True, timeout=10)
        assert result.returncode in (0, 1), result.stderr
        return result.returncode == 1

    wait(no_service_processes, "no service-user writers")
    procs = Path("/sys/fs/cgroup") / cgroup.lstrip("/") / "cgroup.procs"
    assert not procs.exists() or not procs.read_text().strip()
    assert not (root / "control.sock").exists()
    # DL-428: a committed synthetic value makes the sidecar necessary to the backup.
    command(
        "sudo",
        "-u",
        "dsl41",
        python,
        "-c",
        "import os, sqlite3, sys; db=sqlite3.connect(sys.argv[1]); "
        "db.execute('PRAGMA wal_autocheckpoint=0'); "
        "db.execute(\"INSERT INTO meta VALUES ('restore_probe', 'wal-backed')\"); "
        "db.commit(); os._exit(0)",
        str(root / "engine.db"),
    )
    assert (root / "engine.db-wal").stat().st_size > 32
    paths = {
        "root": root,
        "inputs": inputs,
        "config": config,
        "profile": night / "profile.env",
        "incidents": night / "incidents.conf",
    }
    with (root / "leader.lock").open("rb") as fence:
        fcntl.flock(fence, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = manifest(paths)
        for name, path in paths.items():
            command("cp", "-a", str(path), str(backup / name))
        assert manifest({name: backup / name for name in paths}) == before
    command("cp", "-a", str(night), str(backup / "business"))
    for path in (root, inputs, night):
        shutil.rmtree(path)
    config.unlink()
    assert all(not path.exists() for path in (*paths.values(), night))
    night.mkdir(mode=0o700)
    shutil.chown(night, "dsl41", "dsl41")
    for name, path in paths.items():
        command("cp", "-a", str(backup / name), str(path))
    assert manifest(paths) == before
    assert not (night / "restore-effects").exists()
    for name in ("restore-effects", "restore-invocations", "restore-writer"):
        command("cp", "-a", str(backup / "business" / name), str(night / name))

    store = root / "engine.db"
    store.unlink()
    damaged = manifest(paths)
    for _ in range(2):
        refused = subprocess.run(
            ["sudo", "-u", "dsl41", dsl41, "run", "--config", str(config)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert refused.returncode == 2 and "runtime evidence" in refused.stdout + refused.stderr
        assert manifest(paths) == damaged
    command("cp", "-a", str(backup / "root/engine.db"), str(store))
    # DL-433: wrong-owner stores and WALs refuse for the service user without a traceback.
    for evidence in (store, root / "engine.db-wal"):
        original = evidence.stat()
        os.chown(evidence, 0, 0)
        os.chmod(evidence, 0o600)
        try:
            damaged = manifest(paths)
            for form in (
                ["run", "--config", str(config)],
                [
                    "run",
                    str(inputs / "cfg/restore.jil"),
                    "--run-root",
                    str(root),
                    "--as-machine",
                    "bank_core",
                ],
            ):
                for _ in range(2):
                    refused = subprocess.run(
                        ["sudo", "-u", "dsl41", dsl41, *form],
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    output = refused.stdout + refused.stderr
                    assert refused.returncode == 2 and str(evidence) in output, output
                    assert "Traceback" not in output
                    assert manifest(paths) == damaged
        finally:
            os.chown(evidence, original.st_uid, original.st_gid)
            os.chmod(evidence, original.st_mode & 0o777)
    history = json.loads(cli("runs", str(root), "--job", "DRILL_RESTORE_C", "--format", "json"))
    assert [(row["run_number"], row["status"]) for row in history] == [
        (1, "SUCCESS"),
        (2, "RUNNING"),
    ]
    assert "SUCCESS" in cli("journal", str(root))
    start()
    assert status()["config_rev"] == revision
    assert status()["jobs"]["DRILL_RESTORE_HOLD_C"]["on_hold"] is True
    assert status()["jobs"]["DRILL_RESTORE_ICE_C"]["on_ice"] is True
    global_row = json.loads(
        cli("query", "global", "--name", "DRILL_RESTORE_PIN", "--socket", socket)
    )
    assert global_row["globals"]["DRILL_RESTORE_PIN"]["value"] == "durable"
    wait(lambda: ready.exists() and ready.read_text() == "2", "replacement attempt 2")
    row = status()["jobs"]["DRILL_RESTORE_C"]
    assert row["run_number"] == 2 and row["attempt"]["n"] == 2
    assert row["attempt"]["after"] == "engine_stop"
    release.touch()
    wait(settled, "continued run SUCCESS")
    assert (night / "restore-effects").read_text() == "1 1\n2 2\n"
    invocations = [
        json.loads(line) for line in (night / "restore-invocations").read_text().splitlines()
    ]
    assert [row.pop("DSL41_ATTEMPT") for row in invocations] == ["1", "1", "2"]
    assert invocations[1] == invocations[2]
    attempts = [
        json.loads(path.read_text())
        for path in sorted((root / "runs/DRILL_RESTORE_C.2").glob("*/spawn.json"))
    ]
    assert len(attempts) == 2 and attempts[0]["run_id"] != attempts[1]["run_id"]
    command("systemctl", "stop", unit)
    start()
    assert (night / "restore-effects").read_text() == "1 1\n2 2\n"
    history = json.loads(cli("runs", str(root), "--job", "DRILL_RESTORE_C", "--format", "json"))
    assert [(row["run_number"], row["status"]) for row in history] == [
        (1, "SUCCESS"),
        (2, "SUCCESS"),
    ]
    (state / "restore-verdict.json").write_text(
        json.dumps(
            {
                "kind": "O",
                "lane": "S",
                "effects": ["1 1", "2 2"],
                "revision": revision,
                "sidecars": True,
            }
        )
        + "\n"
    )
    print(
        "ok: O restore: no writers, complete copy/delete/restore, fresh history, flags and exact effects"
    )


if __name__ == "__main__":
    if sys.argv[1] == "worker":
        worker(Path(sys.argv[2]))
    else:
        drill(sys.argv[1], *(Path(value) for value in sys.argv[2:7]), sys.argv[7])
