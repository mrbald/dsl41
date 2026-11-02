"""O: genesis eligibility and unchanged-tree refusal (DL-443); lane U."""

from __future__ import annotations

import fcntl
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dsl41.cli import app
from test_main_config import JOB, config_file
from test_restore_drill import _cli, _tree

pytestmark = pytest.mark.usefixtures("cli_env")


def invocation(base: Path, form: str, *, python: bool = False) -> list[str]:
    source = base / ("estate.py" if python else "jobs.jil")
    if python:
        source.write_text(
            "from pathlib import Path\n"
            f"Path({str(base / 'evaluated')!r}).touch()\n"
            "raise RuntimeError('estate Python evaluated')\n"
        )
    else:
        source.write_text(JOB)
    config = config_file(base, include=[source.name])
    return (
        ["run", "--config", str(config)]
        if form == "config"
        else ["run", str(source), "--run-root", str(base / "run")]
    )


@contextmanager
def running(args: list[str], root: Path, log: Path):
    with log.open("w") as stream:
        with subprocess.Popen(
            [sys.executable, "-m", "dsl41", *args], stdout=stream, stderr=subprocess.STDOUT
        ) as child:
            try:
                deadline = time.monotonic() + 30
                while "engine up;" not in log.read_text():
                    assert child.poll() is None, log.read_text()
                    assert time.monotonic() < deadline, log.read_text()
                    time.sleep(0.02)
                yield child
            finally:
                if child.poll() is None:
                    child.terminate()
                try:
                    child.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=10)
            assert child.returncode == 0, log.read_text()


@pytest.mark.parametrize("form", ["config", "legacy"])
@pytest.mark.parametrize(
    "evidence",
    [
        "runs/A.1/1",
        "plans/token",
        "leader.lock",
        "engine.db-wal",
        "engine.db-shm",
        "engine.db-journal",
        "control.sock",
        "runs",
        "plans",
        "logs/A.1.out",
        "logs",
        "perimeter.jsonl",
        "wal",
    ],
)
def test_each_evidence_class_refuses_before_python_without_mutation(
    short_root: Path, form: str, evidence: str
) -> None:
    """O: each DL-443 witness alone pins exit 2, its name and no estate effect."""
    args = invocation(short_root, form, python=True)
    root = short_root / "run"
    root.mkdir(mode=0o755)
    found = root / evidence
    if evidence.startswith("logs/"):
        found.parent.mkdir()
        found.touch()
    elif "/" in evidence:
        found.mkdir(parents=True)
    elif evidence in ("perimeter.jsonl", "journal.jsonl"):
        found.touch()
    else:
        found.write_bytes(b"malformed evidence\xff")
    before = _tree(short_root)
    for _ in range(2):
        result = _cli(*args)
        assert result.returncode == 2, result.stdout + result.stderr
        assert evidence.split("/")[0] in result.stdout + result.stderr
        assert "runtime evidence" in result.stdout + result.stderr
        assert _tree(short_root) == before
        assert not (short_root / "evaluated").exists()


@pytest.mark.parametrize("form", ["config", "legacy"])
@pytest.mark.parametrize("marker", ["journal.jsonl", "wal"])
@pytest.mark.parametrize("other_evidence", [False, True])
def test_1x_refusal_precedes_other_witnesses_without_mutation(
    short_root: Path, form: str, marker: str, other_evidence: bool
) -> None:
    """O: a 1.x layout pins its specific exit-2 diagnostic before Python or any write."""
    args = invocation(short_root, form, python=True)
    root = short_root / "run"
    root.mkdir(mode=0o755)
    if marker == "wal":
        (root / marker).mkdir()
    else:
        (root / marker).touch()
    if other_evidence:
        (root / "runs" / "A.1" / "1").mkdir(parents=True)
        (root / "leader.lock").write_bytes(b"malformed lock")
    before = _tree(short_root)
    for _ in range(2):
        result = _cli(*args)
        assert result.returncode == 2, result.stdout + result.stderr
        assert "a 1.x run root (journal.jsonl or wal/)" in result.stderr
        assert str(root) in result.stderr
        assert _tree(short_root) == before
        assert not (short_root / "evaluated").exists()


@pytest.mark.parametrize("form", ["config", "legacy"])
@pytest.mark.parametrize(
    "evidence", ["runs", "plans", "logs", "leader.lock", "wal", "perimeter.jsonl", "journal.jsonl"]
)
@pytest.mark.skipif(os.geteuid() == 0, reason="mode 000 does not make evidence unreadable to root")
def test_unreadable_evidence_refuses_without_mutation(
    short_root: Path, form: str, evidence: str
) -> None:
    """O: DL-443 refuses unreadable empty locations, not just nonempty ones."""
    args = invocation(short_root, form, python=True)
    root = short_root / "run"
    root.mkdir()
    found = root / evidence
    if evidence == "leader.lock" or evidence.endswith(".jsonl"):
        found.touch()
    else:
        found.mkdir()
    mode = found.stat().st_mode
    found.chmod(0)
    try:
        before = _tree(short_root)
        for _ in range(2):
            result = _cli(*args)
            assert result.returncode == 2, result.stdout + result.stderr
            assert evidence in result.stdout + result.stderr
            assert _tree(short_root) == before
    finally:
        found.chmod(mode)


@pytest.mark.parametrize("form", ["config", "legacy"])
def test_control_socket_alone_refuses_without_connecting(short_root: Path, form: str) -> None:
    """O: a DL-443 socket witness pins exit 2 without contacting its listener."""
    args = invocation(short_root, form, python=True)
    root = short_root / "run"
    root.mkdir()
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(root / "control.sock"))
        listener.listen()
        listener.settimeout(0.05)
        before = _tree(short_root)
        result = _cli(*args)
        assert result.returncode == 2 and "control.sock" in result.stdout + result.stderr
        assert _tree(short_root) == before
        with pytest.raises(TimeoutError):
            listener.accept()


@pytest.mark.parametrize("form", ["config", "legacy"])
@pytest.mark.parametrize("empty_lock", [False, True])
def test_provisioning_and_empty_lock_retry_allow_genesis(
    short_root: Path, form: str, empty_lock: bool
) -> None:
    """O: DL-443 permits settings/input-only genesis and an empty unlocked lock."""
    args = invocation(short_root, form)
    root = short_root / "run"
    root.mkdir()
    for name in ("runs", "plans", "logs"):
        (root / name).mkdir()
    (root / "engine.toml").write_text("snapshot_interval_s = 7.0\n")
    (root / "provisioned.jil").write_text(JOB)
    (root / "arbitrary.output").write_bytes(b"not a named witness")
    (short_root / "client-archive").mkdir()
    (short_root / "client-archive/A.1.out").touch()
    if empty_lock:
        (root / "leader.lock").touch()
    with running(args, root, short_root / "engine.log"):
        answer = _cli("query", "status", "--socket", str(root / "control.sock"))
        assert answer.returncode == 0
        status = json.loads(answer.stdout)
        assert status["engine"] == "RUNNING" and status["config_rev"] == 1
    assert (root / "engine.db").is_file()
    assert (root / "engine.toml").read_text() == "snapshot_interval_s = 7.0\n"


@pytest.mark.parametrize("form", ["config", "legacy"])
def test_contended_empty_lock_refuses_before_python(short_root: Path, form: str) -> None:
    """O: DL-443's empty-lock exception requires exclusive ownership."""
    args = invocation(short_root, form, python=True)
    root = short_root / "run"
    root.mkdir()
    with (root / "leader.lock").open("wb") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = _tree(short_root)
        result = _cli(*args)
        assert result.returncode == 2 and "contended" in result.stdout + result.stderr
        assert _tree(short_root) == before


@pytest.mark.parametrize("form", ["config", "legacy"])
def test_eligibility_is_rechecked_under_exclusive_ownership(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, form: str
) -> None:
    """O: DL-443 pins the ordering: evidence arrives immediately after acquisition."""
    import dsl41.runner_ledger as ledger

    args = invocation(short_root, form)
    root = short_root / "run"
    root.mkdir()
    (root / "leader.lock").touch()
    original = ledger.acquire_run_root
    witnesses = []

    def acquire(path):
        lock = original(path)
        (root / "plans/token").mkdir(parents=True)
        witnesses.append(_tree(root))
        return lock

    monkeypatch.setattr(ledger, "acquire_run_root", acquire)
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 2 and "plans" in result.output
    assert len(witnesses) == 1 and _tree(root) == witnesses[0]
    with (root / "leader.lock").open("rb") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)


@pytest.mark.parametrize("form", ["config", "legacy"])
@pytest.mark.parametrize("existing_store", [False, True], ids=["genesis", "missing-lock-resume"])
def test_concurrent_startup_cannot_create_a_second_engine(
    short_root: Path, form: str, existing_store: bool
) -> None:
    """O: hold the winner after lock acquisition; the contender exits 2 before writing."""
    args = invocation(short_root, form)
    root = short_root / "run"
    if existing_store:
        with running(args, root, short_root / "seed.log"):
            pass
        (root / "leader.lock").unlink()
    else:
        root.mkdir()
    held = short_root / "owned"
    release = short_root / "release"
    code = (
        "import runpy, sys, time\n"
        "from pathlib import Path\n"
        "import dsl41.runner_ledger as ledger\n"
        "original = ledger.acquire_run_root\n"
        "def acquire(path):\n"
        "    lock = original(path)\n"
        f"    Path({str(held)!r}).touch()\n"
        f"    while not Path({str(release)!r}).exists(): time.sleep(0.01)\n"
        "    return lock\n"
        "ledger.acquire_run_root = acquire\n"
        f"sys.argv = ['dsl41', *{args!r}]\n"
        "runpy.run_module('dsl41', run_name='__main__')\n"
    )
    with (short_root / "winner.log").open("w") as output:
        with subprocess.Popen([sys.executable, "-c", code], stdout=output, stderr=output) as child:
            try:
                deadline = time.monotonic() + 30
                while not held.exists():
                    assert child.poll() is None
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
                before = _tree(root)
                loser = _cli(*args)
                assert loser.returncode == 2, loser.stdout + loser.stderr
                assert (
                    "held by another engine" if existing_store else "leader.lock"
                ) in loser.stdout + loser.stderr
                assert _tree(root) == before
                assert (root / "engine.db").exists() == existing_store
                release.touch()
                while "engine up;" not in (short_root / "winner.log").read_text():
                    assert child.poll() is None
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
            finally:
                release.touch()
                child.terminate()
                child.wait(timeout=20)
            assert child.returncode == 0


@pytest.mark.parametrize("form", ["config", "legacy"])
def test_valid_missing_lock_root_resumes_its_stored_identity(short_root: Path, form: str) -> None:
    """O: DL-428/433 permit valid resume without a lock file, with no new genesis."""
    args = invocation(short_root, form)
    root = short_root / "run"
    with running(args, root, short_root / "seed.log"):
        before = json.loads(_cli("query", "status", "--socket", str(root / "control.sock")).stdout)
        answer = _cli("sendevent", "ON_HOLD", "--job", "A", "--socket", str(root / "control.sock"))
        assert answer.returncode == 0
    (root / "leader.lock").unlink()
    with running(args, root, short_root / "resumed.log"):
        after = json.loads(_cli("query", "status", "--socket", str(root / "control.sock")).stdout)
        assert after["baseline_id"] == before["baseline_id"]
        assert after["jobs"]["A"]["on_hold"] is True
        assert after["jobs"]["A"]["run_number"] == 0


@pytest.mark.parametrize("form", ["config", "legacy"])
@pytest.mark.parametrize("sidecars", [False, True], ids=["checkpointed", "wal"])
@pytest.mark.parametrize("missing_lock", [False, True], ids=["lock", "no-lock"])
@pytest.mark.parametrize(
    "damage, sql, message",
    [
        ("schema", "UPDATE meta SET value='999' WHERE key='schema_version'", "schema"),
        ("state", "UPDATE meta SET value='999' WHERE key='state_version'", "state version"),
        ("identity", "DELETE FROM meta WHERE key='root_id'", "root_id"),
        ("clock", "UPDATE meta SET value='virtual' WHERE key='clock_domain'", "clock-domain"),
        ("config", "UPDATE configs SET data=CAST('invalid' AS BLOB)", "config"),
        ("snapshot_version", "UPDATE snapshots SET state_version=999", "snapshot"),
        ("snapshot_data", "UPDATE snapshots SET data=CAST('invalid' AS BLOB)", "snapshot"),
        ("settings", "", "engine.toml"),
    ],
)
def test_restart_refusals_preserve_the_entire_tree(
    short_root: Path,
    form: str,
    sidecars: bool,
    missing_lock: bool,
    damage: str,
    sql: str,
    message: str,
) -> None:
    """O: DL-433 pins exit 2 and identical bytes/modes for every store validation gate.

    WAL cases put the invalid value in an uncheckpointed commit. Inspecting
    only the main file would wrongly accept these roots.
    """
    args = invocation(short_root, form)
    root = short_root / "run"
    with running(args, root, short_root / "engine.log"):
        pass
    if damage == "settings":
        (root / "engine.toml").write_text("grace_term_s = 0\n")
        sql = "INSERT INTO meta VALUES ('restore_probe', 'wal-backed')"
    if sidecars:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import os,sqlite3,sys; db=sqlite3.connect(sys.argv[1]); "
                "db.execute('PRAGMA wal_autocheckpoint=0'); db.execute(sys.argv[2]); "
                "db.commit(); os._exit(0)",
                str(root / "engine.db"),
                sql,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert (root / "engine.db-wal").stat().st_size > 32
    else:
        with sqlite3.connect(root / "engine.db") as db:
            db.execute(sql)
        db.close()
        assert not (root / "engine.db-wal").exists()
    root.chmod(0o755)
    if missing_lock:
        (root / "leader.lock").unlink()
    before = _tree(short_root)
    for _ in range(2):
        result = _cli(*args)
        assert result.returncode == 2, (damage, result.stdout, result.stderr)
        assert message in result.stdout + result.stderr
        assert _tree(short_root) == before


@pytest.mark.parametrize("form", ["config", "legacy"])
@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_pending_plan_refusal_preserves_the_entire_tree(
    short_root: Path, form: str, damage: str
) -> None:
    """O: DL-433/437 require missing/corrupt pending plans to refuse before writes."""
    import shutil

    from test_main_config_restart import seed_root

    args = invocation(short_root, form)
    _, plan = seed_root(short_root / "dsl41.toml", stopping=True, boundary=False)
    if damage == "missing":
        shutil.rmtree(plan.plan_dir)
    else:
        plan.plan_json.write_bytes(b"invalid JSON")
    root = short_root / "run"
    root.chmod(0o755)
    before = _tree(short_root)
    for _ in range(2):
        result = _cli(*args)
        assert result.returncode == 2 and "plan" in result.stdout + result.stderr
        assert _tree(short_root) == before


@pytest.mark.parametrize("form", ["config", "legacy"])
def test_legacy_profile_refusal_preserves_the_entire_tree(short_root: Path, form: str) -> None:
    """O: DL-428/437 keep a launch-profile mismatch from changing durable state."""
    args = invocation(short_root, form)
    root = short_root / "run"
    with running(args, root, short_root / "engine.log"):
        pass
    source = short_root / "jobs.jil"
    before = _tree(short_root)
    for _ in range(2):
        result = _cli("run", str(source), "--run-root", str(root), "--timezone", "Europe/Zurich")
        assert result.returncode == 2 and "default_tz" in result.stdout + result.stderr
        assert _tree(short_root) == before


@pytest.mark.parametrize("form", ["config", "legacy"])
def test_unreadable_sqlite_sidecar_refuses_without_mutation(short_root: Path, form: str) -> None:
    """O: DL-433 refuses an unreadable backup sidecar without omitting its evidence."""
    args = invocation(short_root, form)
    root = short_root / "run"
    with running(args, root, short_root / "engine.log"):
        pass
    sidecar = root / "engine.db-wal"
    sidecar.symlink_to("missing-backup-wal")
    before = _tree(short_root)
    for _ in range(2):
        result = _cli(*args)
        assert result.returncode == 2 and "engine.db-wal" in result.stdout + result.stderr
        assert _tree(short_root) == before and sidecar.readlink() == Path("missing-backup-wal")


@pytest.mark.parametrize("form", ["config", "legacy"])
@pytest.mark.parametrize(
    "evidence", ["engine.db", "engine.db-wal", "engine.db-shm", "engine.db-journal"]
)
@pytest.mark.parametrize("damage", ["unreadable", "directory", "fifo"])
def test_store_copy_failures_refuse_without_mutation_or_residue(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, form: str, evidence: str, damage: str
) -> None:
    """O: DL-433 keeps unreadable/malformed required files inside the exit-2 boundary."""
    if damage == "unreadable" and os.geteuid() == 0:
        pytest.skip("mode 000 does not make evidence unreadable to root")
    args = invocation(short_root, form)
    root = short_root / "run"
    with running(args, root, short_root / "engine.log"):
        pass
    target = root / evidence
    if damage == "unreadable":
        if not target.exists():
            target.write_bytes(b"required sidecar")
        target.chmod(0)
    else:
        target.unlink(missing_ok=True)
        target.mkdir() if damage == "directory" else os.mkfifo(target)
    temporary = short_root / "inspection-tmp"
    temporary.mkdir()
    monkeypatch.setenv("TMPDIR", str(temporary))
    before = _tree(short_root)
    try:
        for _ in range(2):
            result = _cli(*args)
            assert result.returncode == 2, result.stdout + result.stderr
            assert evidence in result.stdout + result.stderr
            assert "Traceback" not in result.stdout + result.stderr
            assert _tree(short_root) == before
            assert list(temporary.iterdir()) == []
            with (root / "leader.lock").open("rb") as stream:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        if damage == "unreadable":
            target.chmod(0o600)


@pytest.mark.parametrize("form", ["config", "legacy"])
@pytest.mark.parametrize("damage", ["missing_identity", "schema", "corrupt_store"])
def test_missing_lock_refusal_has_no_signal_or_lock_creation(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, form: str, damage: str
) -> None:
    """O: DL-433 forbids any runtime effect before a missing-lock root is validated."""
    from dsl41 import runner_procid, runner_startup

    args = invocation(short_root, form)
    root = short_root / "run"
    with running(args, root, short_root / "engine.log"):
        pass
    if damage == "corrupt_store":
        (root / "engine.db").write_bytes(b"invalid SQLite")
    else:
        with sqlite3.connect(root / "engine.db") as db:
            db.execute(
                "DELETE FROM meta WHERE key='root_id'"
                if damage == "missing_identity"
                else "UPDATE meta SET value='999' WHERE key='schema_version'"
            )
        db.close()
    (root / "leader.lock").unlink()

    def forbidden(*args, **kwargs):
        pytest.fail("refusal reached a runtime effect")

    monkeypatch.setattr(runner_startup, "_first_reading", forbidden)
    monkeypatch.setattr(runner_procid, "signal_ours", forbidden)
    before = _tree(short_root)
    for _ in range(2):
        result = CliRunner().invoke(app, args)
        assert result.exit_code == 2, result.output
        assert _tree(short_root) == before
        assert not (root / "leader.lock").exists()


def test_empty_lock_probe_uses_acquire_retry_interval(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O: DL-443's probe tolerates the same brief shared-lock overlap as acquire."""
    import errno
    import dsl41.cli_run as cli

    root = short_root / "run"
    root.mkdir()
    (root / "leader.lock").touch()
    real = fcntl.flock
    attempts = []
    delays = []

    def probe(stream, flags):
        attempts.append(flags)
        if len(attempts) <= 2:
            raise BlockingIOError(errno.EAGAIN, "shared stop probe still held")
        return real(stream, flags)

    monkeypatch.setattr(fcntl, "flock", probe)
    monkeypatch.setattr(time, "sleep", delays.append)
    cli._check_genesis(root)
    assert len(attempts) == 3 and delays == [0.1, 0.1]
