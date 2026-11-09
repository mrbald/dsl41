from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time

import pytest


DRIVER = Path(__file__).resolve().parents[1] / "examples" / "workflow_driver.py"
EFFECT = {
    "kind": "report",
    "target": "daily",
    "business_date": "2026-01-01",
    "payload": {"rows": 3},
}
pytestmark = pytest.mark.usefixtures("cli_env")


def invoke(tmp_path, *, job="job_a", config=None, identity=None, extra=()):
    env = {key: value for key, value in os.environ.items() if not key.startswith("DSL41_")}
    env.update(identity or {})
    command = [sys.executable, str(DRIVER), "--db", str(tmp_path / "ledger.sqlite")]
    if job is not None:
        command.extend(["--job", job])
    if config is not None:
        path = tmp_path / "scenario.json"
        path.write_text(json.dumps(config))
        command.extend(["--config", str(path)])
    return subprocess.run(
        command + list(extra), env=env, text=True, capture_output=True, timeout=15
    )


def rows(tmp_path, table):
    with closing(sqlite3.connect(tmp_path / "ledger.sqlite")) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY id")]


def test_default_durable_success_and_allowlisted_identity(tmp_path):
    identity = {
        "DSL41_JOB": "job_a",
        "DSL41_RUN_NUMBER": "4",
        "DSL41_ATTEMPT": "2",
        "DSL41_SCHEDULED_AT": "2026-01-01T00:00:00Z",
        "DSL41_SECRET": "excluded",
    }
    result = invoke(tmp_path, job=None, identity=identity)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt == {
        "version": 1,
        "attempt_id": 1,
        "job": "job_a",
        "scenario": "default",
        "batch": "default",
        "outcome": "success",
        "effect_id": None,
        "effect_created": False,
    }
    attempt = rows(tmp_path, "attempts")[0]
    assert json.loads(attempt["identity"]) == {
        k: v for k, v in identity.items() if k != "DSL41_SECRET"
    }
    assert attempt["recorded_at"]
    assert rows(tmp_path, "effects") == []


@pytest.mark.parametrize(
    "outcome,exit_code,effect_count",
    [
        ("success", 0, 1),
        ("fail_before_effect", 1, 0),
        ("fail_after_effect", 1, 1),
    ],
)
def test_effect_and_failure_boundaries(tmp_path, outcome, exit_code, effect_count):
    config = {
        "scenario": "trial",
        "batch": "day1",
        "jobs": {"job_a": {"outcome": outcome, "effect": EFFECT}},
    }
    result = invoke(tmp_path, config=config)
    assert result.returncode == exit_code, result.stderr
    attempt = rows(tmp_path, "attempts")[0]
    assert attempt["outcome"] == outcome
    assert attempt["scenario"] == "trial"
    assert attempt["batch"] == "day1"
    effects = rows(tmp_path, "effects")
    assert len(effects) == effect_count
    assert json.loads(result.stdout)["effect_created"] == bool(effect_count)
    if effects:
        effect = effects[0]
        assert {key: effect[key] for key in ("kind", "target", "business_date")} == {
            key: EFFECT[key] for key in ("kind", "target", "business_date")
        }
        assert json.loads(effect["payload"]) == EFFECT["payload"]
        assert attempt["effect_id"] == effect["id"]
    else:
        assert attempt["effect_id"] is None


@pytest.mark.parametrize("dedupe,count", [(False, 2), (True, 1)])
def test_optional_dedupe_preserves_each_attempt(tmp_path, dedupe, count):
    spec = {"effect": EFFECT, "outcome": "fail_after_effect"}
    if dedupe:
        spec["effect_key"] = "report-day1"
    config = {"jobs": {"job_a": spec}}
    for ordinal in (1, 2):
        result = invoke(tmp_path, config=config, identity={"DSL41_ATTEMPT": str(ordinal)})
        assert result.returncode == 1, result.stderr
    attempts = rows(tmp_path, "attempts")
    assert len(attempts) == 2
    assert [json.loads(row["identity"])["DSL41_ATTEMPT"] for row in attempts] == ["1", "2"]
    assert len(rows(tmp_path, "effects")) == count
    assert [row["effect_created"] for row in attempts] == [1, int(not dedupe)]


def test_job_batch_and_scenario_isolate_effect_keys(tmp_path):
    spec = {"effect": EFFECT, "effect_key": "same"}
    config = {"jobs": {"job_a": spec, "job_b": spec}}
    for job, extra in [
        ("job_a", ()),
        ("job_b", ()),
        ("job_a", ("--batch", "day2")),
        ("job_a", ("--scenario", "trial2")),
    ]:
        result = invoke(tmp_path, job=job, config=config, extra=extra)
        assert result.returncode == 0, result.stderr
    assert len(rows(tmp_path, "effects")) == 4
    assert len(rows(tmp_path, "attempts")) == 4
    result = invoke(tmp_path, job="unconfigured", config=config)
    assert result.returncode == 0
    assert len(rows(tmp_path, "effects")) == 4
    assert rows(tmp_path, "attempts")[-1]["job"] == "unconfigured"


def test_conflicting_dedupe_refused(tmp_path):
    spec = {"effect": EFFECT, "effect_key": "same"}
    assert invoke(tmp_path, config={"jobs": {"job_a": spec}}).returncode == 0
    spec["effect"] = {**EFFECT, "target": "different"}
    result = invoke(tmp_path, config={"jobs": {"job_a": spec}})
    assert result.returncode == 2
    assert "different effect" in result.stderr
    assert not result.stdout
    assert len(rows(tmp_path, "effects")) == 1
    attempts = rows(tmp_path, "attempts")
    assert len(attempts) == 2
    assert attempts[-1]["outcome"] == "refused"
    assert attempts[-1]["effect_id"] is None
    assert attempts[-1]["effect_created"] == 0
    assert rows(tmp_path, "effects")[0]["target"] == EFFECT["target"]


@pytest.mark.parametrize(
    "config",
    [
        [],
        {"unknown": 1},
        {"jobs": []},
        {"scenario": ""},
        {"jobs": {"job_a": {"outcome": "unknown", "effect": EFFECT}}},
        {"jobs": {"job_a": {"outcome": []}}},
        {"jobs": {"job_a": {"outcome": "fail_after_effect"}}},
        {"jobs": {"job_a": {"effect": {"kind": "report"}}}},
        {"jobs": {"job_a": {"effect_key": "key"}}},
        {"jobs": {"job_a": {"effect": {**EFFECT, "command": "forbidden"}}}},
        {"jobs": {"other": {"outcome": "unknown"}, "job_a": {"effect": EFFECT}}},
    ],
)
def test_malformed_config_refused_before_database(tmp_path, config):
    result = invoke(tmp_path, config=config)
    assert result.returncode == 2
    assert "error" in json.loads(result.stderr)
    assert not result.stdout
    assert not (tmp_path / "ledger.sqlite").exists()


@pytest.mark.parametrize(
    "raw",
    [
        "{",
        '{"jobs": {"job_a": {"effect": {"kind":"report","target":"daily","payload":NaN}}}}',
        '{"jobs":{"job_a":{"outcome":"fail_before_effect"},"job_a":{}}}',
        "[" * 100000 + "]" * 100000,
    ],
    ids=["syntax", "nonfinite", "duplicate-key", "deep-nesting"],
)
def test_invalid_json_refused(tmp_path, raw):
    path = tmp_path / "invalid.json"
    path.write_text(raw)
    result = invoke(tmp_path, extra=("--config", str(path)))
    assert result.returncode == 2
    assert "error" in json.loads(result.stderr)
    assert not result.stdout
    assert not (tmp_path / "ledger.sqlite").exists()


@pytest.mark.parametrize(
    "job,identity",
    [
        ("job_a", {"DSL41_JOB": "job_b"}),
        (None, {}),
        ("", {}),
    ],
)
def test_identity_refused_before_database(tmp_path, job, identity):
    result = invoke(tmp_path, job=job, identity=identity)
    assert result.returncode == 2
    assert not result.stdout
    assert not (tmp_path / "ledger.sqlite").exists()


def test_database_failure_cannot_report_success(tmp_path):
    (tmp_path / "ledger.sqlite").mkdir()
    result = invoke(tmp_path)
    assert result.returncode == 2
    assert not result.stdout
    assert "error" in json.loads(result.stderr)


def test_matching_explicit_and_environment_job(tmp_path):
    result = invoke(tmp_path, job="job_a", identity={"DSL41_JOB": "job_a"})
    assert result.returncode == 0, result.stderr
    assert rows(tmp_path, "attempts")[0]["job"] == "job_a"


def test_fresh_concurrent_keyed_effect(tmp_path):
    config = tmp_path / "keyed.json"
    config.write_text(json.dumps({"jobs": {"job_a": {"effect": EFFECT, "effect_key": "same"}}}))
    barrier = threading.Barrier(8)

    def writer():
        barrier.wait(timeout=10)
        return invoke(tmp_path, extra=("--config", str(config)))

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(writer) for _ in range(8)]
        results = [future.result(timeout=20) for future in futures]
    assert all(result.returncode == 0 for result in results), [r.stderr for r in results]
    attempts = rows(tmp_path, "attempts")
    assert len(attempts) == 8
    assert len(rows(tmp_path, "effects")) == 1
    assert sum(attempt["effect_created"] for attempt in attempts) == 1
    with closing(sqlite3.connect(tmp_path / "ledger.sqlite")) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_concurrent_writers_commit_distinct_attempts(tmp_path):
    assert invoke(tmp_path).returncode == 0
    instrumentation = tmp_path / "instrumentation"
    instrumentation.mkdir()
    (instrumentation / "sitecustomize.py").write_text(
        "import os, sys\n"
        "from pathlib import Path\n"
        "def connected(event, args):\n"
        "    if event == 'sqlite3.connect/handle':\n"
        "        def statement(sql):\n"
        "            if sql == 'BEGIN IMMEDIATE':\n"
        "                Path(os.environ['WRITER_READY']).write_text('waiting')\n"
        "        connection = args[0]\n"
        "        def initialized(frame, event, arg):\n"
        "            if event == 'line':\n"
        "                connection.set_trace_callback(statement)\n"
        "                sys.settrace(None)\n"
        "                return None\n"
        "            return initialized\n"
        "        sys._getframe(1).f_trace = initialized\n"
        "        sys.settrace(initialized)\n"
        "sys.addaudithook(connected)\n"
    )
    children = []
    ready = []
    with closing(sqlite3.connect(tmp_path / "ledger.sqlite")) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            for n in range(4):
                marker = tmp_path / f"ready-{n}"
                ready.append(marker)
                env = {k: v for k, v in os.environ.items() if not k.startswith("DSL41_")}
                env["PYTHONPATH"] = str(instrumentation) + os.pathsep + env.get("PYTHONPATH", "")
                env["WRITER_READY"] = str(marker)
                children.append(
                    subprocess.Popen(
                        [
                            sys.executable,
                            str(DRIVER),
                            "--db",
                            str(tmp_path / "ledger.sqlite"),
                            "--job",
                            f"job_{n}",
                        ],
                        env=env,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                )
            deadline = time.monotonic() + 3
            while not all(marker.exists() for marker in ready):
                assert time.monotonic() < deadline, "children did not reach BEGIN IMMEDIATE"
                for child in children:
                    if child.poll() is not None:
                        pytest.fail(f"writer exited before contention: {child.communicate()}")
                time.sleep(0.01)
            time.sleep(1)
            assert all(child.poll() is None for child in children)
            connection.commit()
            results = [child.communicate(timeout=10) for child in children]
            assert all(child.returncode == 0 for child in children), results
        finally:
            connection.rollback()
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=10)
    attempts = rows(tmp_path, "attempts")
    assert len(attempts) == 5
    assert len({row["id"] for row in attempts}) == 5
    with closing(sqlite3.connect(tmp_path / "ledger.sqlite")) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)


def test_held_writer_times_out_without_success_or_effect(tmp_path):
    assert invoke(tmp_path).returncode == 0
    with closing(sqlite3.connect(tmp_path / "ledger.sqlite")) as connection:
        connection.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        result = invoke(tmp_path, config={"jobs": {"job_a": {"effect": EFFECT}}})
        elapsed = time.monotonic() - started
        connection.rollback()
    assert result.returncode == 2
    assert "locked" in json.loads(result.stderr)["error"]
    assert not result.stdout
    assert 4 <= elapsed < 12
    assert len(rows(tmp_path, "attempts")) == 1
    assert rows(tmp_path, "effects") == []


def test_file_action_cli_executes_and_reports_classified_refusals(tmp_path):
    root = tmp_path / "files"
    root.mkdir(mode=0o700)
    (root / "inbox").mkdir(mode=0o700)
    (root / "work").mkdir(mode=0o700)
    content = b"neutral bytes\n"
    (root / "inbox/item.ready").write_bytes(content)
    config = {
        "jobs": {
            "job_a": {
                "file_action": {
                    "operation": "move",
                    "source": "inbox/item.ready",
                    "destination": "work/item.current",
                }
            }
        }
    }
    extra = ("--file-root", str(root))
    first = invoke(tmp_path, config=config, extra=extra)
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout)["cause"] == "completed"
    assert (root / "work/item.current").read_bytes() == content
    assert not (root / "inbox/item.ready").exists()
    second = invoke(tmp_path, config=config, extra=extra)
    assert second.returncode == 2
    assert json.loads(second.stdout)["cause"] == "missing_source"
    assert len(rows(tmp_path, "attempts")) == 2


@pytest.mark.parametrize(
    "spec",
    [
        {"file_action": {"operation": "delay", "seconds": 0}, "effect": EFFECT},
        {"file_action": {"operation": "delay", "seconds": 0, "fault": "after_mutation"}},
        {"file_action": {"operation": "delay", "seconds": True}},
        {"file_action": {"operation": "remove", "paths": ["../outside"]}},
    ],
)
def test_file_action_invalid_config_never_opens_ledger(tmp_path, spec):
    result = invoke(tmp_path, config={"jobs": {"job_a": spec}})
    assert result.returncode == 2
    assert not (tmp_path / "ledger.sqlite").exists()


def test_file_action_evidence_error_names_invocation(tmp_path):
    root = tmp_path / "files"
    root.mkdir(mode=0o700)
    (tmp_path / "ledger.sqlite").mkdir()
    result = invoke(
        tmp_path,
        config={"jobs": {"job_a": {"file_action": {"operation": "delay", "seconds": 0}}}},
        extra=("--file-root", str(root)),
    )
    assert result.returncode == 2
    assert not result.stdout
    evidence = json.loads(result.stderr)
    assert evidence["cause"] == "evidence_error"
    assert evidence["invocation_id"]
    assert evidence["uncommitted_cause"] == "pending"


def wal_observed_cli(tmp_path, mode):
    instrumentation = tmp_path / "wal-observer"
    instrumentation.mkdir()
    marker = tmp_path / "wal-busy"
    trace = tmp_path / "wal-statements.jsonl"
    (instrumentation / "sitecustomize.py").write_text(
        "import json, os, sqlite3\n"
        "from pathlib import Path\n"
        "original = sqlite3.connect\n"
        "class Observed(sqlite3.Connection):\n"
        "    def note(self, sql, error=None):\n"
        "        with Path(os.environ['WAL_TRACE']).open('a') as stream:\n"
        "            stream.write(json.dumps({'sql': sql, 'error': error}) + '\\n')\n"
        "    def execute(self, sql, *args, **kwargs):\n"
        "        try:\n"
        "            value = super().execute(sql, *args, **kwargs)\n"
        "        except sqlite3.OperationalError as error:\n"
        "            self.note(sql, str(error))\n"
        "            if sql == 'PRAGMA journal_mode=WAL' and error.sqlite_errorcode == 5:\n"
        "                Path(os.environ['WAL_BUSY']).touch()\n"
        "            raise\n"
        "        self.note(sql)\n"
        "        return value\n"
        "    def executescript(self, sql):\n"
        "        self.note('SCHEMA')\n"
        "        return super().executescript(sql)\n"
        "def connect(*args, **kwargs):\n"
        "    database = args[0] if args else kwargs.get('database')\n"
        "    expected = Path(os.environ['WAL_LEDGER']).resolve()\n"
        "    if database is not None and Path(os.fsdecode(database)).resolve() == expected:\n"
        "        return original(*args, factory=Observed, **kwargs)\n"
        "    return original(*args, **kwargs)\n"
        "sqlite3.connect = connect\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("DSL41_")}
    env.update(
        PYTHONPATH=str(instrumentation),
        WAL_BUSY=str(marker),
        WAL_TRACE=str(trace),
        WAL_LEDGER=str(tmp_path / "ledger.sqlite"),
    )
    command = [
        sys.executable,
        str(DRIVER),
        "--db",
        str(tmp_path / "ledger.sqlite"),
        "--job",
        "job_a",
    ]
    if mode == "ledger":
        spec = {"effect": EFFECT}
    else:
        root = tmp_path / "files"
        root.mkdir(mode=0o700)
        (root / "inbox").mkdir(mode=0o700)
        (root / "work").mkdir(mode=0o700)
        (root / "inbox/item.ready").write_bytes(b"physical input")
        command.extend(["--file-root", str(root)])
        spec = {
            "file_action": {
                "operation": "move",
                "source": "inbox/item.ready",
                "destination": "work/item.current",
            }
        }
    config = tmp_path / "wal-config.json"
    config.write_text(json.dumps({"jobs": {"job_a": spec}}))
    command.extend(["--config", str(config)])
    child = subprocess.Popen(
        command, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    return child, marker, trace


def wait_for_wal_busy(child, marker, trace):
    deadline = time.monotonic() + 5
    while not marker.exists():
        if child.poll() is not None:
            pytest.fail(f"CLI ended before observed WAL BUSY: {child.communicate()}")
        assert time.monotonic() < deadline, "CLI did not reach actual WAL BUSY"
        time.sleep(0.005)
    statements = [json.loads(line) for line in trace.read_text().splitlines()]
    assert any(
        row["sql"] == "PRAGMA journal_mode=WAL" and row["error"] == "database is locked"
        for row in statements
    )
    assert all(row["sql"].startswith("PRAGMA ") for row in statements)


@pytest.mark.parametrize("mode", ["ledger", "file"])
def test_wal_reserved_lock_release_recovers_both_connectors(tmp_path, mode):
    child = None
    with closing(sqlite3.connect(tmp_path / "ledger.sqlite")) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        try:
            child, marker, trace = wal_observed_cli(tmp_path, mode)
            wait_for_wal_busy(child, marker, trace)
            assert (
                blocker.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                == []
            )
            if mode == "file":
                assert (tmp_path / "files/inbox/item.ready").read_bytes() == b"physical input"
                assert not (tmp_path / "files/work/item.current").exists()
            blocker.commit()
            stdout, stderr = child.communicate(timeout=10)
            assert child.returncode == 0, stderr
            assert json.loads(stdout)["outcome"] == "success"
        finally:
            blocker.rollback()
            if child is not None:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=10)
    statements = [json.loads(line) for line in trace.read_text().splitlines()]
    assert sum(row["sql"] == "PRAGMA journal_mode=WAL" for row in statements) >= 2
    assert len(rows(tmp_path, "attempts")) == 1
    assert len(rows(tmp_path, "effects")) == int(mode == "ledger")
    if mode == "file":
        assert not (tmp_path / "files/inbox/item.ready").exists()
        assert (tmp_path / "files/work/item.current").read_bytes() == b"physical input"
        assert len(rows(tmp_path, "file_receipts")) == 1
        assert rows(tmp_path, "file_receipts")[0]["state"] == "completed"
    else:
        assert json.loads(rows(tmp_path, "effects")[0]["payload"]) == EFFECT["payload"]
    with closing(sqlite3.connect(tmp_path / "ledger.sqlite")) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_wal_reserved_lock_budget_expiry_is_locked_exit_two(tmp_path):
    child = None
    with closing(sqlite3.connect(tmp_path / "ledger.sqlite")) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        try:
            started = time.monotonic()
            child, marker, trace = wal_observed_cli(tmp_path, "ledger")
            wait_for_wal_busy(child, marker, trace)
            stdout, stderr = child.communicate(timeout=12)
            elapsed = time.monotonic() - started
            assert child.returncode == 2
            assert "locked" in json.loads(stderr)["error"]
            assert not stdout
            assert 4 <= elapsed < 12
            assert (
                blocker.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                == []
            )
        finally:
            blocker.rollback()
            if child is not None:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=10)
    statements = [json.loads(line) for line in trace.read_text().splitlines()]
    assert sum(row["sql"] == "PRAGMA journal_mode=WAL" for row in statements) >= 2
    assert all(row["sql"].startswith("PRAGMA ") for row in statements)
    assert all(
        0 <= int(row["sql"].split("=")[1]) <= 5000
        for row in statements
        if row["sql"].startswith("PRAGMA busy_timeout=")
    )


def test_wal_budget_includes_injected_sqlite_waits_and_sleeps(monkeypatch):
    from types import SimpleNamespace
    from examples import workflow_files as files

    clock = [0.0]
    sleeps = []
    timeouts = []
    attempts = []
    error = sqlite3.OperationalError("injected busy")
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY

    class WaitingConnection:
        def execute(self, sql):
            if sql.startswith("PRAGMA busy_timeout="):
                timeouts.append(int(sql.split("=")[1]))
            else:
                attempts.append(sql)
                assert len(attempts) <= 2, "SQLite wait received a fresh budget"
                clock[0] += 2 if len(attempts) == 1 else timeouts[-1] / 1000
                raise error

    def sleep(duration):
        assert duration >= 0
        sleeps.append(duration)
        clock[0] += duration

    monkeypatch.setattr(files, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    with pytest.raises(sqlite3.OperationalError, match="injected busy"):
        files.initialize_wal(WaitingConnection())
    assert len(attempts) == 2
    assert timeouts[0] == 5000
    assert timeouts[1] <= 2990
    assert 5 <= clock[0] < 5.02
    assert sum(sleeps) < 0.02


@pytest.mark.parametrize("code", [sqlite3.SQLITE_IOERR, sqlite3.SQLITE_BUSY_RECOVERY])
def test_wal_nonexact_busy_errors_are_not_retried(monkeypatch, code):
    from types import SimpleNamespace
    from examples import workflow_files as files

    error = sqlite3.OperationalError("injected nonexact busy")
    error.sqlite_errorcode = code
    attempts = []

    class RefusingConnection:
        def execute(self, sql):
            if sql == "PRAGMA journal_mode=WAL":
                attempts.append(sql)
                raise error

    def no_sleep(duration):
        pytest.fail("nonexact BUSY must not sleep or retry")

    monkeypatch.setattr(files, "time", SimpleNamespace(monotonic=lambda: 0.0, sleep=no_sleep))
    with pytest.raises(sqlite3.OperationalError) as raised:
        files.initialize_wal(RefusingConnection())
    assert raised.value is error
    assert len(attempts) == 1
