"""O: destructive same-path restore and refusal, runner §7 (DL-428, DL-433).

Lane U exercises fresh CLI and engine processes. Installed-unit and actual
host-reboot qualification remain separate boundaries (integration-testing §3).
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from test_main_config import config_file
from test_main_config_restart import cli_engine

pytestmark = pytest.mark.usefixtures("cli_env")


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "dsl41", *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _ok(*args: str) -> str:
    result = _cli(*args)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def _tree(directory: Path) -> dict[str, tuple[int, bytes | None]]:
    """Compare all names, modes and file bytes, including SQLite sidecars."""
    result = {}
    for path in [directory, *sorted(directory.rglob("*"))]:
        mode = path.lstat().st_mode
        try:
            data = path.read_bytes() if stat.S_ISREG(mode) else None
        except PermissionError:
            data = b"<unreadable>"
        result[str(path.relative_to(directory))] = (
            mode,
            data,
        )
    return result


def _status(root: Path) -> dict:
    return json.loads(_ok("query", "status", "--json", "--socket", str(root / "control.sock")))


def _settled(root: Path, job: str) -> dict:
    deadline = time.monotonic() + 30
    while True:
        row = _status(root)["jobs"][job]
        if row["status"] in ("SUCCESS", "FAILURE", "TERMINATED") and "attempt" not in row:
            return row
        assert time.monotonic() < deadline, json.dumps(row)
        time.sleep(0.02)


@pytest.fixture(params=[False, True], ids=["checkpointed", "sidecars"])
def restored_site(short_root: Path, tmp_path: Path, request: pytest.FixtureRequest):
    """Drain, fence, copy every scheduler/input file, delete, restore in place."""
    site = short_root / "site"
    site.mkdir()
    effects = short_root / "business-effects"
    worker = site / "worker.py"
    worker.write_text(
        "import json, os, sys, time\nfrom pathlib import Path\n"
        "site, effects = map(Path, sys.argv[1:])\n"
        "invocation = {key: os.environ[key] for key in "
        "('DSL41_JOB', 'DSL41_RUN_NUMBER', 'DSL41_SCHEDULED_AT', 'DSL41_ATTEMPT')}\n"
        "with (site / 'invocations.jsonl').open('a') as out:\n"
        "    out.write(json.dumps(invocation) + '\\n'); out.flush(); os.fsync(out.fileno())\n"
        "with (site / 'writer.log').open('a') as writer:\n"
        "    while not (site / 'release').exists():\n"
        "        writer.write('active\\n'); writer.flush(); os.fsync(writer.fileno())\n"
        "        (site / 'ready').write_text(os.environ['DSL41_ATTEMPT'])\n"
        "        time.sleep(0.02)\n"
        "with effects.open('a') as out:\n"
        "    out.write('continued\\n'); out.flush(); os.fsync(out.fileno())\n"
    )
    (site / "jobs.jil").write_text(
        "insert_global: RESTORE_PIN\nvalue: initial\n\n"
        "insert_job: DONE\njob_type: c\nmachine: localhost\n"
        f"command: echo done >> {effects}\n\n"
        "insert_job: ICED\njob_type: c\nmachine: localhost\ncommand: true\n"
        "\ninsert_job: CONTINUE\njob_type: c\nmachine: localhost\n"
        f"command: exec {sys.executable} {worker} {site} {effects}\n"
    )
    (site / "site.properties").write_text("SYNTHETIC_INPUT=restore\n")
    path = config_file(site, properties=["site.properties"])
    root = site / "run"
    socket = str(root / "control.sock")
    with cli_engine(path):
        _ok("sendevent", "FORCE_STARTJOB", "--job", "DONE", "--socket", socket)
        deadline = time.monotonic() + 30
        while True:
            status = json.loads(_ok("query", "status", "--json", "--socket", socket))
            done = status["jobs"]["DONE"]
            if done["status"] == "SUCCESS" and "attempt" not in done:
                break
            if time.monotonic() >= deadline or done["status"] in ("FAILURE", "TERMINATED"):
                shutil.copytree(
                    site,
                    tmp_path / "failed-site",
                    ignore=shutil.ignore_patterns("control.sock"),
                )
                pytest.fail(json.dumps(status, indent=2))
            time.sleep(0.02)
        _ok("sendevent", "ON_HOLD", "--job", "DONE", "--socket", socket)
        _ok("sendevent", "ON_ICE", "--job", "ICED", "--socket", socket)
        _ok("sendevent", "SET_GLOBAL", "--global", "RESTORE_PIN=durable", "--socket", socket)
        _ok("sendevent", "FORCE_STARTJOB", "--job", "CONTINUE", "--socket", socket)
        deadline = time.monotonic() + 30
        while not (site / "ready").exists():
            assert time.monotonic() < deadline, _status(root)
            time.sleep(0.02)
        assert (site / "writer.log").stat().st_size > 0
        assert _status(root)["jobs"]["CONTINUE"]["attempt"]["state"] == "RUNNING"
        _ok("engine", "stop", "--wait", "--socket", socket)

    assert "engine stopped: clean_shutdown" in (site / "cli-engine.log").read_text()
    assert not (root / "control.sock").exists()
    spawn = json.loads((root / "runs/DONE.1/1/spawn.json").read_text())
    assert spawn["job"] == "DONE" and spawn["run_number"] == 1
    for record in (root / "runs").rglob("spawn.json"):
        for key in ("wrapper_pid", "command_pid"):
            with pytest.raises(ProcessLookupError):
                os.kill(json.loads(record.read_text())[key], 0)

    if request.param:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import os, sqlite3, sys; db = sqlite3.connect(sys.argv[1]); "
                "db.execute('PRAGMA wal_autocheckpoint=0'); "
                "db.execute(\"INSERT INTO meta VALUES ('restore_probe', 'wal-backed')\"); "
                "db.commit(); os._exit(0)",
                str(root / "engine.db"),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert (root / "engine.db-wal").stat().st_size > 32
        assert (root / "engine.db-shm").is_file()

    backup = tmp_path / "scheduler-backup"
    with (root / "leader.lock").open("rb") as fence:
        fcntl.flock(fence, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = _tree(site)
        shutil.copytree(site, backup)
        assert _tree(backup) == before
        fcntl.flock(fence, fcntl.LOCK_UN)

    assert effects.read_text() == "done\n"
    business_backup = tmp_path / "separate-business-backup"
    shutil.copy2(effects, business_backup)
    shutil.rmtree(site)
    effects.unlink()
    assert not site.exists() and not effects.exists()
    shutil.copytree(backup, site)
    assert _tree(site) == before
    assert not effects.exists()
    if request.param:
        marker = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sqlite3, sys; db=sqlite3.connect(sys.argv[1]+'?mode=ro', uri=True); "
                "print(db.execute(\"SELECT value FROM meta WHERE key='restore_probe'\").fetchone()[0])",
                (root / "engine.db").as_uri(),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert marker.returncode == 0 and marker.stdout == "wal-backed\n", marker.stderr
    return path, effects, business_backup, spawn


def test_complete_restore_uses_fresh_readers_and_keeps_flags(restored_site) -> None:
    """O: DL-428/433 preserve one SUCCESS, flags, globals and attempt identity.

    The literal one-line effect and run 1 / attempt 1 are independent pins.
    Restoring scheduler files does not recreate the business file (R09).
    """
    path, effects, business_backup, spawn = restored_site
    root = path.parent / "run"
    history = json.loads(_ok("runs", str(root), "--job", "DONE", "--format", "json"))
    assert len(history) == 1
    row = history[0]
    assert (row["job"], row["run_number"], row["status"], row["exit_code"]) == (
        "DONE",
        1,
        "SUCCESS",
        0,
    )
    assert row["ended_at"] is not None and row["fidelity"] == "full"
    assert "SUCCESS" in _ok("journal", str(root))
    assert not effects.exists()
    shutil.copy2(business_backup, effects)
    with cli_engine(path):
        socket = str(root / "control.sock")
        status = json.loads(_ok("query", "status", "--json", "--socket", socket))
        assert status["jobs"]["DONE"]["on_hold"] is True
        assert status["jobs"]["ICED"]["on_ice"] is True
        assert status["jobs"]["DONE"]["run_number"] == 1
        global_row = json.loads(
            _ok("query", "global", "--name", "RESTORE_PIN", "--socket", socket)
        )["globals"]["RESTORE_PIN"]
        assert global_row["present"] is True and global_row["value"] == "durable"
        deadline = time.monotonic() + 30
        while (path.parent / "ready").read_text() != "2":
            assert time.monotonic() < deadline, _status(root)
            time.sleep(0.02)
        continuing = _status(root)["jobs"]["CONTINUE"]
        assert continuing["run_number"] == 1 and continuing["attempt"]["n"] == 2
        assert continuing["attempt"]["after"] == "engine_stop"
        (path.parent / "release").touch()
        assert _settled(root, "CONTINUE")["status"] == "SUCCESS"
    assert json.loads((root / "runs/DONE.1/1/spawn.json").read_text()) == spawn
    assert sorted(p.name for p in (root / "runs/DONE.1").iterdir()) == ["1"]
    invocations = [
        json.loads(line) for line in (path.parent / "invocations.jsonl").read_text().splitlines()
    ]
    assert [row.pop("DSL41_ATTEMPT") for row in invocations] == ["1", "2"]
    assert invocations[0] == invocations[1]
    assert invocations[0]["DSL41_JOB"] == "CONTINUE"
    assert invocations[0]["DSL41_RUN_NUMBER"] == "1"
    attempts = [
        json.loads(p.read_text()) for p in sorted((root / "runs/CONTINUE.1").glob("*/spawn.json"))
    ]
    assert len(attempts) == 2 and attempts[0]["run_id"] != attempts[1]["run_id"]
    assert effects.read_text() == "done\ncontinued\n"
    with cli_engine(path):
        assert _status(root)["jobs"]["CONTINUE"]["status"] == "SUCCESS"
    assert effects.read_text() == "done\ncontinued\n"
    history = json.loads(_ok("runs", str(root), "--format", "json"))
    assert [(row["job"], row["run_number"], row["status"]) for row in history] == [
        ("CONTINUE", 1, "SUCCESS"),
        ("DONE", 1, "SUCCESS"),
    ]


@pytest.mark.parametrize("damage", ["missing_store", "corrupt_store", "missing_identity"])
def test_incomplete_restore_refuses_without_mutation(restored_site, damage: str) -> None:
    """O: R05/R07 and DL-433 require exit 2 before writes for incomplete restore.

    Keep settings, inputs and real attempt spools but omit engine.db. A fresh
    store and RUNNING engine would invent history instead of refusing.
    """
    path, _, _, _ = restored_site
    root = path.parent / "run"
    store = root / "engine.db"
    if damage == "missing_store":
        store.unlink()
    elif damage == "corrupt_store":
        store.write_bytes(b"not a SQLite database")
    else:
        with sqlite3.connect(store) as db:
            db.execute("DELETE FROM meta WHERE key = 'root_id'")
        db.close()
    before = _tree(path.parent)
    log = path.parent.parent / "incomplete-restore.log"
    with log.open("w") as output:
        with subprocess.Popen(
            [sys.executable, "-m", "dsl41", "run", "--config", str(path)],
            stdout=output,
            stderr=subprocess.STDOUT,
        ) as child:
            try:
                deadline = time.monotonic() + 30
                while child.poll() is None and "engine up;" not in log.read_text():
                    assert time.monotonic() < deadline, log.read_text()
                    time.sleep(0.02)
                startup_exit = child.poll()
            finally:
                if child.poll() is None:
                    child.terminate()
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=10)
    after = _tree(path.parent)
    changed = sorted(
        key for key in before.keys() | after.keys() if before.get(key) != after.get(key)
    )
    assert startup_exit == 2 and after == before, {
        "expected_exit": 2,
        "actual_startup_exit": startup_exit,
        "created_store": (root / "engine.db").exists(),
        "changed_paths": changed,
        "output": log.read_text(),
    }
