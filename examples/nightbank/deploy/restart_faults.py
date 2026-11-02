"""Synthetic O assertions for installed recovery (DL-431, DL-433, DL-436).

Run by the service drill with the installed interpreter and -I. The parent controls
wrapper pause acknowledgements and the restart's first-reading barrier. Expectations
are fixed below, independent of the runtime's plan selector and process scanner.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import json
import os
import platform
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

CASES = (
    "before-spawn",
    "spawn-lost",
    "status-before",
    "status-after",
    "quarantine",
    "scan-failed",
    "send-refused",
    "kill-scan-failed",
    "kill-emfile",
    "probe-send",
    "probe-emfile",
    "watch-prefix",
    "witness-checks",
    "cleanup-checks",
)
LIMIT = 15.0
ROOT_MARKER = "DSL41_FAULT_ROOT"


def until(check: Callable[[], Any]) -> Any:
    deadline = time.monotonic() + LIMIT
    while time.monotonic() < deadline:
        try:
            value = check()
        except (FileNotFoundError, ConnectionRefusedError):
            value = None  # a barrier or listener not published yet; never a drain verdict
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError(f"barrier timed out: {check}")


def live(pid: int) -> bool:
    """Independent kernel witness: a zombie is dead; a read failure is never absence."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return False
    return raw.rsplit(")", 1)[1].split()[0] != "Z"


def stopped(pid: int) -> bool:
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "T"


def rows(root: Path, kind: str) -> list[tuple[int, dict[str, Any]]]:
    with contextlib.closing(sqlite3.connect(root / "run/engine.db")) as db:
        return [
            (s, json.loads(b))
            for s, b in db.execute(
                "SELECT seq, payload FROM journal WHERE kind = ? ORDER BY seq", (kind,)
            )
        ]


def call(root: Path, cmd: str, **fields: Any) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX) as conn:
        conn.settimeout(2)
        conn.connect(str(root / "run/control.sock"))
        conn.sendall(json.dumps({"v": 3, "cmd": cmd, **fields}).encode() + b"\n")
        with conn.makefile("rb") as stream:
            return json.loads(stream.readline())


def publish(path: Path, text: str) -> None:
    """Publish a complete acknowledgement with one rename (DL-433 test barriers)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text)
    temporary.replace(path)


def acknowledge(root: Path, role: str) -> None:
    pid = os.getpid()
    token = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    publish(root / "owned" / f"{pid}.json", json.dumps({"pid": pid, "token": token, "role": role}))


def owned_processes(root: Path) -> dict[int, str]:
    """Find this fixture's processes independently of possibly malformed evidence."""
    found = {}
    marker = f"{ROOT_MARKER}={root}".encode()
    for directory in Path("/proc").iterdir():
        if not directory.name.isdigit():
            continue
        try:
            if marker not in (directory / "environ").read_bytes().split(b"\0"):
                continue
            fields = (directory / "stat").read_text().rsplit(")", 1)[1].split()
            if fields[0] != "Z":
                found[int(directory.name)] = fields[19]
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
    return found


def cleanup_owned(root: Path, known: dict[int, str] | None = None) -> None:
    """Stop only token-checked fixture processes, even without any PID acknowledgement."""
    deadline = time.monotonic() + 5
    while True:
        targets = {**(known or {}), **owned_processes(root)}
        for pid, token in targets.items():
            try:
                fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
                if fields[19] == token and fields[0] != "Z":
                    os.kill(pid, signal.SIGKILL)
            except (FileNotFoundError, ProcessLookupError):
                pass
        if not owned_processes(root):
            return
        if time.monotonic() >= deadline:
            raise AssertionError(f"owned processes survived cleanup: {owned_processes(root)}")
        time.sleep(0.01)


def assert_publications(root: Path) -> None:
    assert (root / "publish-attempts").read_text().splitlines() == ["j:1"], "duplicate publication"


def assert_unsignalled(root: Path) -> None:
    assert (root / "signals").read_text() == "", "unauthorized outsider signal"


def worker(root: Path, *, child: bool = False) -> None:
    """A command records its invocation outside scheduler history, then waits for a gate."""
    acknowledge(root, "child" if child else "worker")
    if child and root.name == "foreign":
        publish(root / "signals", "")

        def witnessed(sig: int, _frame: object) -> None:
            with (root / "signals").open("a") as out:
                out.write(f"{sig}\n")

        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(sig, witnessed)
    elif child or root.name != "quarantine":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if child:
        publish(root / "child", str(os.getpid()))
    else:
        env = {
            key: os.environ[key]
            for key in (
                "DSL41_JOB",
                "DSL41_RUN_NUMBER",
                "DSL41_SCHEDULED_AT",
                "DSL41_ATTEMPT",
            )
        }
        with (root / "invocations").open("a") as out:
            out.write(json.dumps({"pid": os.getpid(), **env}) + "\n")
            out.flush()
            os.fsync(out.fileno())
        try:
            (root / "claimed").mkdir()
        except FileExistsError:
            pass
        if root.name == "quarantine":
            clean = {k: v for k, v in os.environ.items() if k != "DSL41_COOKIE"}
            subprocess.Popen([sys.executable, "-I", __file__, "child", str(root)], env=clean)
    while not (root / "finish").exists():
        time.sleep(0.01)
    if not child:
        with (root / "publish-attempts").open("a") as attempts:
            attempts.write("j:1\n")
            attempts.flush()
            os.fsync(attempts.fileno())
        try:
            with (root / "published").open("x") as out:
                out.write("j:1\n")
        except FileExistsError:
            pass


async def engine_worker(root: Path, mode: str) -> None:
    from dsl41 import runner_procid
    from dsl41.ir import lower_source
    from dsl41.oracle_state import Event
    from dsl41.runner_adapters import FileWatcherAdapter, LocalCommandAdapter
    from dsl41.runner_clock import RealClock
    from dsl41.runner_config import EngineSettings
    from dsl41.runner_control import ControlServer
    from dsl41.runner_startup import resume_run, start_run

    acknowledge(root, "engine")

    def refused(*_args: object) -> None:
        raise OSError(errno.EPERM, "synthetic send refusal")

    if mode == "probe-send":
        signal.pidfd_send_signal = refused
    elif mode == "probe-emfile":

        def exhausted(*_args: object) -> int:
            raise OSError(errno.EMFILE, "synthetic probe exhaustion")

        os.pidfd_open = exhausted
    elif mode == "scan-failed":
        runner_procid.ours = lambda *_: runner_procid.Ours({}, error=errno.EIO)
    kwargs = dict(
        clock=RealClock(),
        hold_open=True,
        settings=EngineSettings(grace_term_s=0.2, grace_kill_s=0.3),
        adapters={"CMD": LocalCommandAdapter(grace_seconds=0.2), "FW": FileWatcherAdapter()},
    )
    catalog = lower_source((root / "estate.jil").read_text())
    run = root / "run"
    if (run / "engine.db").exists():
        engine = await resume_run(catalog, run, **kwargs)
        if mode == "send-refused":
            # Selection already succeeded. DL-436 forbids a per-target fallback now.
            real_send = signal.pidfd_send_signal if engine.stops.pidfd else os.kill

            def refuse_kill(target: int, sig: int) -> None:
                if sig == signal.SIGKILL:
                    raise OSError(errno.EPERM, "synthetic KILL refusal")
                real_send(target, sig)

            if engine.stops.pidfd:
                signal.pidfd_send_signal = refuse_kill
            else:
                os.kill = refuse_kill
        if mode in ("kill-scan-failed", "kill-emfile"):
            import builtins
            from dsl41.runner_procid import Left

            original_members = engine.stops._members
            original_signal = runner_procid.signal_ours
            original_open = builtins.open
            original_ours = runner_procid.ours
            original_pidfd = getattr(os, "pidfd_open", None)
            phase = {"reads": 0, "blocked": set()}

            def members(*args: Any) -> Left:
                phase["reads"] += 1
                if phase["reads"] == 2 and mode == "kill-scan-failed":
                    runner_procid.ours = lambda *_: runner_procid.Ours({}, error=errno.EIO)
                    try:
                        return original_members(*args)
                    finally:
                        runner_procid.ours = original_ours
                look = original_members(*args)
                phase["blocked"] = set(look.pids)
                return look

            def blocked() -> bool:
                return phase["reads"] >= 2 and not (root / "recover").exists()

            def proc_open(path: Any, *args: Any, **kwargs: Any) -> Any:
                if (
                    mode == "kill-emfile"
                    and blocked()
                    and str(path) in {f"/proc/{pid}/stat" for pid in phase["blocked"]}
                ):
                    raise OSError(errno.EMFILE, "synthetic stat exhaustion")
                return original_open(path, *args, **kwargs)

            def pidfd_open(pid: int, *args: Any) -> int:
                if blocked() and pid in phase["blocked"]:
                    raise OSError(errno.EMFILE, "synthetic pidfd exhaustion")
                assert original_pidfd is not None
                return original_pidfd(pid, *args)

            def send(pid: int, token: str, sig: int, **kwargs: Any) -> bool:
                if mode == "kill-scan-failed" and blocked() and sig == signal.SIGKILL:
                    return False
                sent = original_signal(pid, token, sig, **kwargs)
                if sent:
                    with (root / "sent-signals").open("a") as log:
                        log.write(f"{pid} {sig}\n")
                return sent

            engine.stops._members = members
            builtins.open = proc_open
            if original_pidfd is not None:
                os.pidfd_open = pidfd_open
            runner_procid.signal_ours = send
        (root / "first-reading").touch()
        while not (root / "continue").exists():
            await asyncio.sleep(0.01)
    else:
        engine = start_run(catalog, run, **kwargs)
        engine.inject(Event(at=engine.clock.now(), kind="ON_HOLD", payload={"job": "guard"}))
        engine.inject(Event(at=engine.clock.now(), kind="STARTJOB", payload={"job": "j"}))
    server = ControlServer(engine, run / "control.sock")
    try:
        await server.start()
        await engine.run_until_quiescent(datetime.max)
    finally:
        await server.close()
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()


class FaultCase:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.processes: list[subprocess.Popen[bytes]] = []
        self.pids: dict[int, str] = {}
        self.logs: list[Any] = []

    def remember(self, pid: int) -> None:
        if pid in self.pids:
            return
        try:
            self.pids[pid] = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        except FileNotFoundError:
            pass
        else:
            path = self.root / "owned" / f"{pid}.json"
            if not path.exists():
                publish(path, json.dumps({"pid": pid, "token": self.pids[pid], "role": "observed"}))

    def launch(self, *, pause: str = "", mode: str = "normal") -> subprocess.Popen[bytes]:
        env = {**os.environ, "DSL41_WRAPPER_TEST_PAUSE": pause, ROOT_MARKER: str(self.root)}
        log = (self.root / f"engine-{len(self.processes)}.log").open("wb")
        self.logs.append(log)
        proc = subprocess.Popen(
            [sys.executable, "-I", __file__, "engine", str(self.root), mode],
            env=env,
            stdout=log,
            stderr=log,
        )
        self.processes.append(proc)
        return proc

    def wrapper(self, engine: subprocess.Popen[bytes]) -> int:
        def stopped() -> int | None:
            for name in Path(f"/proc/{engine.pid}/task/{engine.pid}/children").read_text().split():
                pid = int(name)
                if b"runner_wrapper.py" in Path(f"/proc/{pid}/cmdline").read_bytes():
                    raw = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
                    if raw[0] == "T":
                        return pid
            return None

        pid = until(stopped)
        self.remember(pid)
        return pid

    def invocations(self) -> list[dict[str, Any]]:
        path = self.root / "invocations"
        result = (
            [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        )
        for item in result:
            if item["pid"] not in self.pids:
                self.remember(item["pid"])
        return result

    def kill(self, proc: subprocess.Popen[bytes]) -> None:
        proc.kill()
        assert proc.wait(5) == -signal.SIGKILL

    def resume(self, **kwargs: Any) -> subprocess.Popen[bytes]:
        for marker in ("first-reading", "continue"):
            (self.root / marker).unlink(missing_ok=True)
        proc = self.launch(**kwargs)
        until(lambda: (self.root / "first-reading").exists())
        return proc

    def release(self) -> None:
        (self.root / "continue").touch()
        until(lambda: call(self.root, "status").get("engine") == "RUNNING")
        assert call(self.root, "status")["jobs"]["guard"]["on_hold"] is True

    def stop(self, proc: subprocess.Popen[bytes]) -> None:
        assert call(self.root, "engine_stop")["ok"] is True
        assert proc.wait(10) == 0

    def cleanup(self) -> None:
        # Evidence is an assertion input, never a prerequisite for cleanup (DL-433).
        try:
            for proc in self.processes:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(5)
        finally:
            try:
                cleanup_owned(self.root, self.pids)
            finally:
                for log in self.logs:
                    log.close()


def run_case(root: Path, case: str) -> None:
    """O: every branch pins records and effects from runner §7, DL-431/433/436."""
    root.mkdir(parents=True)
    if case == "witness-checks":
        witness_checks(root)
        return
    if case == "cleanup-checks":
        cleanup_checks(root)
        return
    script = Path(__file__).resolve()
    command = f"exec {sys.executable} -I {script} worker {root}"
    if case == "watch-prefix":
        job = f"job_type: f\nwatch_file: {root / 'watched'}\nwatch_interval: 1\n"
    else:
        job = f"job_type: c\ncommand: {command}\n"
    (root / "estate.jil").write_text(
        f"insert_job: j\n{job}\ninsert_job: guard\njob_type: c\ncommand: true\n"
    )
    fault = FaultCase(root)
    try:
        outsider = None
        if case == "scan-failed":
            foreign_root = root / "foreign"
            foreign_root.mkdir()
            outsider = subprocess.Popen(
                [sys.executable, "-I", __file__, "child", str(foreign_root)],
                start_new_session=True,
                env={
                    **{k: v for k, v in os.environ.items() if k != "DSL41_COOKIE"},
                    ROOT_MARKER: str(root),
                },
            )
            fault.processes.append(outsider)
            until(lambda: (foreign_root / "child").exists())
        if case == "watch-prefix":
            watch_prefix(fault)
            return
        pause = {
            "before-spawn": "pre_spawn",
            "spawn-lost": "post_spawn_pre_record",
            "status-before": "post_status_pre_reap",
            "status-after": "post_wait_pre_status",
        }.get(case, "post_record")
        first = fault.launch(pause=pause)
        if case.startswith("status-"):
            until(lambda: fault.invocations())
            os.kill(first.pid, signal.SIGSTOP)
            until(lambda: stopped(first.pid))
            (root / "finish").touch()
        wrapper = fault.wrapper(first)
        spool = root / "run/runs/j.1/1"
        if case != "before-spawn":
            until(lambda: fault.invocations())
        else:
            assert fault.invocations() == [] and not (root / "claimed").exists()
        if case == "spawn-lost":
            until(lambda: (root / "claimed").is_dir())
            assert not (root / "published").exists()
        if case.startswith("status-"):
            assert (spool / "status.json").exists() == (case == "status-before")
        if case == "quarantine":
            until(lambda: (root / "child").exists())
            child = int((root / "child").read_text())
            fault.remember(child)
            (spool / "status.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "run_id": "00000000-0000-4000-8000-00000000beef",
                        "job": "j",
                        "run_number": 1,
                        "outcome": "exited",
                        "exit_code": 0,
                    }
                )
            )
        fault.kill(first)
        if case != "status-after":
            os.kill(wrapper, signal.SIGKILL)
        if case in ("spawn-lost", "before-spawn"):
            assert not (spool / "spawn.json").exists()
            assert not (spool / "status.json").exists()
            try:
                call(root, "status")
            except OSError:
                pass
            else:
                raise AssertionError("dead engine answered status")
            if case == "spawn-lost":
                assert live(fault.invocations()[0]["pid"])
        mode = (
            case
            if case
            in (
                "scan-failed",
                "send-refused",
                "probe-send",
                "probe-emfile",
                "kill-scan-failed",
                "kill-emfile",
            )
            else "normal"
        )
        second = fault.resume(mode=mode)
        if case == "quarantine":
            until(lambda: not live(fault.invocations()[0]["pid"]))
            assert live(child)
        fault.release()
        if case == "status-before":
            until(lambda: call(root, "status")["jobs"]["j"]["status"] == "SUCCESS")
            assert len(rows(root, "dispatch")) == 1
            assert len(fault.invocations()) == 1
            assert rows(root, "outcome")[0][1]["outcome"] == "natural"
        elif case == "quarantine":
            until(lambda: not live(child))
            until(lambda: any(b.get("event") == "ladder_done" for _, b in rows(root, "follow_up")))
            status = call(root, "status")["jobs"]["j"]
            assert (status["status"], status["on_hold"]) == ("FAILURE", True)
            assert len(fault.invocations()) == len(rows(root, "dispatch")) == 1
            [(out_seq, outcome)] = rows(root, "outcome")
            assert (outcome["outcome"], outcome["cause"], outcome["ladder"]) == (
                "unobservable",
                "quarantined",
                "running",
            )
            [done] = [s for s, b in rows(root, "follow_up") if b.get("event") == "ladder_done"]
            assert out_seq < done
        elif case in ("kill-scan-failed", "kill-emfile"):
            until(
                lambda: (
                    "KILL not confirmed"
                    in call(root, "status")["jobs"]["j"].get("attempt", {}).get("note", "")
                )
            )
            state = call(root, "status")["jobs"]["j"]["attempt"]
            assert state["held"] and "survived its KILL" not in state["note"]
            assert live(fault.invocations()[0]["pid"])
            trace = root / "sent-signals"
            assert not trace.exists() or all(
                line.split()[1] != "9" for line in trace.read_text().splitlines()
            )
            (root / "recover").touch()
            until(lambda: len(fault.invocations()) == 2)
            assert not live(fault.invocations()[0]["pid"])
            assert any(line.split()[1] == "9" for line in trace.read_text().splitlines())
            [done] = [
                seq for seq, body in rows(root, "follow_up") if body.get("event") == "ladder_done"
            ]
            assert rows(root, "outcome")[0][0] < done < rows(root, "dispatch")[1][0]
        elif case in ("scan-failed", "send-refused"):
            expected = "scan failed: EIO" if case == "scan-failed" else "KILL not sent"
            until(
                lambda: (
                    expected in call(root, "status")["jobs"]["j"].get("attempt", {}).get("note", "")
                )
            )
            attempt = call(root, "status")["jobs"]["j"]["attempt"]
            assert expected in attempt["note"] and "survived its KILL" not in attempt["note"]
            assert not any(b.get("event") == "ladder_done" for _, b in rows(root, "follow_up"))
            assert len(rows(root, "dispatch")) == 1
            refusal = call(root, "engine_stop", v=999)
            assert (refusal["ok"], refusal["refused"], refusal["code"]) == (
                False,
                True,
                "unsupported_version",
            )
            if outsider is not None:
                assert live(outsider.pid)
                assert_unsignalled(root / "foreign")
            assert call(root, "engine_stop")["ok"] is True
            header = call(root, "status")
            refused_start = call(
                root,
                "sendevent",
                verb="FORCE_STARTJOB",
                payload={"job": "guard"},
                request_id="synthetic-draining",
                baseline_id=header["baseline_id"],
                expect={"job:guard": header["jobs"]["guard"]["state_rev"]},
            )
            assert refused_start["ok"] is False and refused_start["code"] == "engine_draining"
            assert len(rows(root, "dispatch")) == 1
            if case == "send-refused":
                assert call(root, "engine_stop")["ok"] is True
            assert second.wait(8 if case == "scan-failed" else 5) == 0
            endings = [b for _, b in rows(root, "engine") if b["event"] == "unclean_shutdown"]
            assert len(endings) == 1
            assert endings[0]["unsettled"] == [rows(root, "dispatch")[0][1]["attempt"]]
            if outsider is not None:
                assert live(outsider.pid)
                assert_unsignalled(root / "foreign")
            return
        else:
            until(lambda: len(rows(root, "dispatch")) == 2)
            if case == "status-after":
                before = rows(root, "outcome")[0]
                os.kill(wrapper, signal.SIGCONT)
                until(lambda: (spool / "status.json").exists())
                assert rows(root, "outcome")[0] == before
            until(lambda: len(fault.invocations()) == (1 if case == "before-spawn" else 2))
            dispatches = rows(root, "dispatch")
            assert [(b["job"], b["run_number"], b["ordinal"]) for _, b in dispatches] == [
                ("j", 1, 1),
                ("j", 1, 2),
            ]
            assert rows(root, "outcome")[0][1]["cause"] == "engine_death"
            if case != "before-spawn":
                assert not live(fault.invocations()[0]["pid"])
            if case in ("spawn-lost", "probe-send", "probe-emfile"):
                outcome_seq = rows(root, "outcome")[0][0]
                [done] = [s for s, b in rows(root, "follow_up") if b.get("event") == "ladder_done"]
                assert outcome_seq < done < dispatches[1][0]
            if case == "spawn-lost":
                fault.kill(second)
                third = fault.resume()
                fault.release()
                until(lambda: len(fault.invocations()) == 3)
                records = fault.invocations()
                assert len({r["DSL41_ATTEMPT"] for r in records}) == 3
                assert all(not live(r["pid"]) for r in records[:-1])
                for key, expected in (("DSL41_JOB", "j"), ("DSL41_RUN_NUMBER", "1")):
                    assert {r[key] for r in records} == {expected}
                assert len({r["DSL41_SCHEDULED_AT"] for r in records}) == 1
                assert [b["ordinal"] for _, b in rows(root, "dispatch")] == [1, 2, 3]
                assert (root / "claimed").is_dir() and not (root / "published").exists()
                (root / "finish").touch()
                until(lambda: call(root, "status")["jobs"]["j"]["status"] == "SUCCESS")
                assert (root / "published").read_text() == "j:1\n"
                assert_publications(root)
                second = third
        fault.stop(second)
    finally:
        original = sys.exception()
        try:
            fault.cleanup()
        except Exception as cleanup_error:
            if original is None:
                raise
            print(f"cleanup also failed: {cleanup_error}", file=sys.stderr)


def watch_prefix(fault: FaultCase) -> None:
    """O, runner §7: preserve exact FW prefix and one start across an engine death."""
    root = fault.root
    first = fault.launch()
    log = root / "run/runs/j.1/watch.jsonl"
    until(lambda: log.exists() and len(log.read_bytes().splitlines()) >= 2)
    os.kill(first.pid, signal.SIGSTOP)
    until(lambda: stopped(first.pid))
    prefix = log.read_bytes()
    fault.kill(first)
    second = fault.resume()
    fault.release()
    until(lambda: len(log.read_bytes()) > len(prefix))
    assert log.read_bytes().startswith(prefix)
    (root / "watched").write_text("synthetic\n")
    until(lambda: call(root, "status")["jobs"]["j"]["status"] == "SUCCESS")
    evidence = [json.loads(line) for line in log.read_bytes().splitlines()]
    assert sum(r["kind"] == "start" for r in evidence) == 1
    assert len(rows(root, "dispatch")) == 1
    fault.stop(second)


def witness_checks(root: Path) -> None:
    """O, runner §7: deliberate duplicate publication and outsider TERM must be detected."""
    publication = root / "publication"
    publication.mkdir()
    (publication / "finish").touch()
    env = {
        **os.environ,
        ROOT_MARKER: str(root),
        "DSL41_JOB": "j",
        "DSL41_RUN_NUMBER": "1",
        "DSL41_SCHEDULED_AT": "2026-07-01T08:00:00Z",
        "DSL41_ATTEMPT": "1",
    }
    for _ in range(2):
        subprocess.run(
            [sys.executable, "-I", __file__, "worker", str(publication)],
            env=env,
            check=True,
            timeout=5,
        )
    try:
        assert_publications(publication)
    except AssertionError as error:
        assert str(error) == "duplicate publication"
    else:
        raise AssertionError("second publication was not detected")
    foreign = root / "foreign"
    foreign.mkdir()
    proc = subprocess.Popen([sys.executable, "-I", __file__, "child", str(foreign)], env=env)
    try:
        until(lambda: (foreign / "child").exists())
        assert_unsignalled(foreign)
        os.kill(proc.pid, signal.SIGTERM)
        until(lambda: (foreign / "signals").read_text())
        try:
            assert_unsignalled(foreign)
        except AssertionError as error:
            assert str(error) == "unauthorized outsider signal"
        else:
            raise AssertionError("unauthorized TERM was not detected")
    finally:
        proc.kill()
        proc.wait(5)


def cleanup_checks(root: Path) -> None:
    """O, DL-433: malformed evidence and a failed pre-registration barrier never leak."""
    malformed = root / "malformed"
    malformed.mkdir()
    fault = FaultCase(malformed)
    proc = subprocess.Popen(
        [sys.executable, "-I", __file__, "child", str(malformed)],
        env={**os.environ, ROOT_MARKER: str(malformed)},
    )
    fault.processes.append(proc)
    try:
        until(lambda: (malformed / "child").exists())
        (malformed / "child").write_text("")
        (malformed / "invocations").write_text("{partial")
    finally:
        fault.cleanup()
    assert proc.poll() is not None and not owned_processes(malformed)
    original = FaultCase.wrapper

    def broken(self: FaultCase, engine: subprocess.Popen[bytes]) -> int:
        until(lambda: (self.root / "invocations").exists())
        assert engine.poll() is None
        raise AssertionError("forced barrier failure before wrapper registration")

    FaultCase.wrapper = broken
    barrier = root / "barrier"
    try:
        try:
            run_case(barrier, "send-refused")
        except AssertionError as error:
            assert str(error) == "forced barrier failure before wrapper registration"
        else:
            raise AssertionError("barrier failure was lost")
        assert not owned_processes(barrier)
    finally:
        FaultCase.wrapper = original
        cleanup_owned(barrier)


def hang_driver(root: Path) -> None:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)

    def stall(self: FaultCase, _engine: subprocess.Popen[bytes]) -> int:
        until(lambda: (self.root / "invocations").exists())
        publish(self.root / "ready", "complete")
        while True:
            time.sleep(0.01)

    FaultCase.wrapper = stall
    run_case(root, "send-refused")


def receipt(root: Path, case: str, result: str) -> None:
    """Retain the tested bytes, environment and actual record sequence, including failures."""
    import dsl41

    package = Path(dsl41.__file__).parent
    hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (
            Path(__file__),
            root / "estate.jil",
            *(
                package / name
                for name in (
                    "runner.py",
                    "runner_startup.py",
                    "runner_attempts.py",
                    "runner_stops.py",
                    "runner_procid.py",
                    "runner_adapters.py",
                    "runner_wrapper.py",
                )
            ),
        )
        if path.is_file()
    }
    doc = {
        "kind": "O",
        "case": case,
        "result": result,
        "hashes": hashes,
        "python": sys.version,
        "kernel": platform.release(),
        "arch": platform.machine(),
        "os": platform.freedesktop_os_release(),
        "package": str(package),
        "command": sys.argv,
        "invocations": (root / "invocations").read_text()
        if (root / "invocations").exists()
        else "",
        "records": {
            kind: rows(root, kind) if (root / "run/engine.db").exists() else []
            for kind in ("dispatch", "outcome", "follow_up", "engine")
        },
    }
    (root / "receipt.json").write_text(json.dumps(doc, sort_keys=True) + "\n")
    print(f"receipt: {json.dumps(doc, sort_keys=True)}", flush=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["list"]:
        print("\n".join(CASES))
        raise SystemExit(0)
    action, directory, *extra = sys.argv[1:]
    selected = Path(directory).resolve()
    if action == "hang":
        hang_driver(selected)
    elif action == "engine":
        asyncio.run(engine_worker(selected, extra[0]))
    elif action in ("worker", "child"):
        worker(selected, child=action == "child")
    else:
        assert action in CASES

        def deadline(_sig: int, _frame: object) -> None:
            raise TimeoutError("external deadline; cleaning owned processes")

        signal.signal(signal.SIGTERM, deadline)
        try:
            run_case(selected, action)
        except BaseException:
            for path in selected.glob("engine-*.log"):
                print(f"{path.name}:\n{path.read_text()}", file=sys.stderr)
            try:
                receipt(selected, action, "failed")
            except Exception as error:
                print(f"receipt failed: {error}", file=sys.stderr)
            raise
        receipt(selected, action, "passed")
        print(f"ok: O {action} (DL-431, DL-433, DL-436)")
