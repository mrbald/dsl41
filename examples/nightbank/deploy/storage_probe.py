"""Synthetic operational storage checks (O), pinned by DL-92, DL-99 and DL-428.

The ordinary lane runs extra legacy probe-child coverage. The service lane
also freezes the real systemd engine and tests main-config contention.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path


def group_has_live_members(pgid: int):
    result = subprocess.run(
        ["ps", "-ax", "-o", "pgid=,stat="], capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 0, result.stderr
    return any(
        int(group) == pgid and not state.startswith("Z")
        for group, state in (line.split() for line in result.stdout.splitlines() if line.strip())
    )


def signal_group(pgid: int, signum: int):
    try:
        os.killpg(pgid, signum)
    except ProcessLookupError:
        pass
    except PermissionError:
        if group_has_live_members(pgid):
            raise


def run_probe(argv: list[str], timeout_s: float = 120, *, on_started=None):
    """Own a session; a deadline ends its whole group, including stopped children."""
    process = subprocess.Popen(
        argv, start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        if on_started is not None:
            on_started(process)
        stdout, stderr = process.communicate(timeout=timeout_s)
        return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
    except BaseException as error:
        previous_term = signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            print(f"probe deadline/failure: ending process group {process.pid}", file=sys.stderr)
            for signum in (signal.SIGCONT, signal.SIGTERM):
                signal_group(process.pid, signum)
            # The unreaped leader pins the group id through the five-second cleanup grace.
            time.sleep(5)
            try:
                signal_group(process.pid, signal.SIGKILL)
            finally:
                stdout, stderr = process.communicate(timeout=5)
            if isinstance(error, subprocess.TimeoutExpired):
                error.output, error.stderr = stdout, stderr
            else:
                print(stdout, end="", flush=True)
                print(stderr, end="", file=sys.stderr, flush=True)
        finally:
            signal.signal(signal.SIGTERM, previous_term)
        raise


def deadline_signal(_signum, _frame):
    raise SystemExit(124)


def wait_for(predicate, description: str, seconds: float = 15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out: {description}")


class Engine:
    def __init__(self, work: Path, evidence: Path | None = None):
        self.work = work
        self.root = work / "run"
        self.socket = self.root / "control.sock"
        self.jil = work / "estate.jil"
        self.effects = work / "effects"
        if not self.jil.exists():
            self.jil.write_text(
                "insert_job: probe\njob_type: c\n"
                f"command: printf '%s\\n' \"$DSL41_RUN_NUMBER\" >> {self.effects}\n"
            )
        self.argv = [
            sys.executable,
            "-I",
            "-m",
            "dsl41",
            "run",
            "--run-root",
            str(self.root),
            str(self.jil),
        ]
        evidence = evidence or work
        self.stdout = (evidence / "engine.out").open("a+")
        self.stderr = (evidence / "engine.err").open("a+")
        self.proc = subprocess.Popen(self.argv, stdout=self.stdout, stderr=self.stderr)

    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, "-I", "-m", "dsl41", *args],
            capture_output=True,
            text=True,
            timeout=20,
            cwd=self.work,
        )
        print(
            json.dumps(
                {
                    "command": args,
                    "exit": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                }
            ),
            flush=True,
        )
        return result

    def status(self):
        result = self.cli("query", "status", "--socket", str(self.socket))
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    def ready(self):
        def up():
            assert self.proc.poll() is None, Path(self.stderr.name).read_text()
            self.stdout.seek(0)
            return "engine up; control socket:" in self.stdout.read()

        wait_for(up, "engine startup", 60)
        assert self.status()["engine"] == "RUNNING"

    def freeze(self):
        # An unreaped direct child cannot have its pid reused (DL-99).
        assert self.proc.poll() is None
        os.kill(self.proc.pid, signal.SIGSTOP)

        def stopped():
            pid, status = os.waitpid(self.proc.pid, os.WNOHANG | os.WUNTRACED)
            if pid:
                assert os.WIFSTOPPED(status) and os.WSTOPSIG(status) == signal.SIGSTOP
                return True
            return False

        wait_for(stopped, "kernel acknowledged SIGSTOP")

    def thaw(self):
        assert self.proc.poll() is None
        os.kill(self.proc.pid, signal.SIGCONT)

    def close(self):
        if self.proc.poll() is None:
            self.thaw()
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self.stdout.close()
        self.stderr.close()


@contextlib.contextmanager
def engine(work: Path, evidence: Path | None = None) -> Iterator[Engine]:
    live = Engine(work, evidence)
    try:
        live.ready()
        yield live
    finally:
        live.close()


def durable_state(root: Path):
    with contextlib.closing(sqlite3.connect(f"{root.as_uri()}/engine.db?mode=ro", uri=True)) as db:
        tables = list(db.iterdump())
    files = {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file() and path.name != "engine.db-shm"
    }
    return tables, files


def records(root: Path):
    with contextlib.closing(sqlite3.connect(f"{root.as_uri()}/engine.db?mode=ro", uri=True)) as db:
        return [
            (kind, json.loads(payload))
            for kind, payload in db.execute("SELECT kind, payload FROM journal ORDER BY seq")
        ]


def test_frozen_leader(work: Path):
    """O: extra legacy probe-child coverage; DL-99/DL-424 pin exit 2 and no writes.

    V2 has no lease. Expired diagnostic metadata has no authority over flock.
    waitpid acknowledges STOP on our unreaped child; CONT targets that same pid.
    """
    with engine(work) as live:
        lock = live.root / "leader.lock"
        note = json.loads(lock.read_text())
        assert note["pid"] == live.proc.pid
        live.freeze()
        note.update(since="2000-01-01T00:00:00", expires_at="2000-01-01T00:00:01")
        with lock.open("w") as stream:
            json.dump(note, stream)
            stream.flush()
            os.fsync(stream.fileno())
        for frozen in (True, False):
            before = durable_state(live.root)
            lock_before = lock.read_bytes(), lock.stat().st_ino
            socket_before = live.socket.stat().st_ino
            refused = live.cli(*live.argv[4:])
            assert refused.returncode == 2
            assert "is held by another engine" in refused.stderr
            assert f"pid {live.proc.pid}" in refused.stderr
            assert live.proc.poll() is None
            assert durable_state(live.root) == before
            assert (lock.read_bytes(), lock.stat().st_ino) == lock_before
            assert live.socket.stat().st_ino == socket_before
            assert not live.effects.exists()
            if frozen:
                live.thaw()
                assert live.status()["engine"] == "RUNNING"
        # A positive control proves the incumbent can still write and launch (DL-428).
        start_once(live)


def unit_property(unit: str, name: str):
    result = subprocess.run(
        ["systemctl", "show", "--property", name, "--value", unit],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def process_identity(pid: int):
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return fields[19], fields[0]


def verified_signal(pid: int, token: str, signum: int):
    assert process_identity(pid)[0] == token, "unit process start token changed"
    os.kill(pid, signum)
    print(json.dumps({"pid": pid, "start_token": token, "signal": signum}), flush=True)


def test_frozen_unit(work: Path, unit: str, config: Path, root: Path):
    """O: DL-99 pins config-path refusal/no writes; DL-429 pins the live unit identity.

    MainPID, its kernel start token and InvocationID must stay fixed. Token-checked
    STOP and CONT address the observed unit engine, not a probe-owned child.
    """
    pid = int(unit_property(unit, "MainPID"))
    invocation = unit_property(unit, "InvocationID")
    assert pid > 1 and invocation
    token, _ = process_identity(pid)
    lock = root / "leader.lock"
    socket = root / "control.sock"
    assert json.loads(lock.read_text())["pid"] == pid

    def identity():
        return (
            int(unit_property(unit, "MainPID")),
            unit_property(unit, "InvocationID"),
            process_identity(pid)[0],
        )

    expected = pid, invocation, token
    print(json.dumps({"unit": unit, "identity": expected, "config": str(config)}), flush=True)
    try:
        verified_signal(pid, token, signal.SIGSTOP)
        wait_for(lambda: process_identity(pid)[1] in {"T", "t"}, "unit STOP acknowledgement")
        for frozen in (True, False):
            before = durable_state(root)
            lock_before = lock.read_bytes(), lock.stat().st_ino
            socket_before = socket.stat().st_ino
            refused = subprocess.run(
                [str(Path(sys.executable).parent / "dsl41"), "run", "--config", str(config)],
                cwd=work,
                capture_output=True,
                text=True,
                timeout=30,
            )
            print(
                json.dumps(
                    {
                        "frozen": frozen,
                        "exit": refused.returncode,
                        "stdout": refused.stdout,
                        "stderr": refused.stderr,
                    }
                ),
                flush=True,
            )
            assert refused.returncode == 2
            assert "is held by another engine" in refused.stderr
            assert f"pid {pid}" in refused.stderr
            assert identity() == expected
            assert durable_state(root) == before
            assert (lock.read_bytes(), lock.stat().st_ino) == lock_before
            assert socket.stat().st_ino == socket_before
            if frozen:
                verified_signal(pid, token, signal.SIGCONT)
                status = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        "-m",
                        "dsl41",
                        "query",
                        "status",
                        "--socket",
                        str(socket),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                assert status.returncode == 0, status.stderr
                assert json.loads(status.stdout)["engine"] == "RUNNING"
        assert unit_property(unit, "ActiveState") == "active"
    finally:
        verified_signal(pid, token, signal.SIGCONT)


def request(live: Engine, request_id: str, status: dict):
    return live.cli(
        "sendevent",
        "STARTJOB",
        "--job",
        "probe",
        "--socket",
        str(live.socket),
        "--request-id",
        request_id,
        "--baseline",
        status["baseline_id"],
        "--expect",
        str(status["jobs"]["probe"]["state_rev"]),
    )


def settled(live: Engine):
    state = live.status()["jobs"]["probe"]
    return state["status"] == "SUCCESS" and state.get("attempt") is None


def start_once(live: Engine):
    pin = live.status()
    first = request(live, "storage-first", pin)
    assert first.returncode == 0
    wait_for(lambda: settled(live), "first run settled")
    assert live.effects.read_text().splitlines() == ["1"]
    return pin


def assert_first_decision(result):
    answer = json.loads(result.stdout)
    assert result.returncode == 0
    assert {key: answer[key] for key in ("decision", "index", "request_id", "revisions")} == {
        "decision": "applied",
        "index": 1,
        "request_id": "storage-first",
        "revisions": {"job:probe": 1},
    }


def inspect_space(root: Path):
    """O: S23 pins readable capacity for the store, sidecars, spool and logs."""
    paths = [root, root / "engine.db", root / "runs", root / "logs"]
    paths.extend(sorted(root.glob("engine.db-*")))
    paths.extend(sorted((root / "runs").rglob("*")))
    for path in paths:
        info = os.statvfs(path)
        assert info.f_blocks > 0 and info.f_frsize > 0
        assert 0 <= info.f_bavail <= info.f_blocks
        print(
            json.dumps(
                {
                    "space": str(path),
                    "device": path.stat().st_dev,
                    "bytes": info.f_blocks * info.f_frsize,
                    "available": info.f_bavail * info.f_frsize,
                }
            ),
            flush=True,
        )


def test_admission_enospc(work: Path, bounded: Path):
    """O: DL-92/DL-425/DL-428 pin UNKNOWN, no input/dispatch/effect, recovery.

    The invented command appends its run number outside the full filesystem.
    Expected effects are literal [1], then [1, 2]; no runtime computes them.
    A failed durable write exits 1 (DL-268, DL-429).
    """
    mount = subprocess.run(
        ["findmnt", "-n", "-b", "-o", "FSTYPE,SIZE", "--mountpoint", str(bounded)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert mount.returncode == 0 and mount.stdout.split() == ["tmpfs", "33554432"], mount
    full_work = bounded / "admission"
    full_work.mkdir()
    # Keep CLI evidence and business effects outside the filesystem being filled.
    (full_work / "effects").symlink_to(work / "effects")
    fill = bounded / "fill"
    try:
        with engine(full_work, work) as live:
            original = start_once(live)
            inspect_space(live.root)
            retry = request(live, "storage-first", original)
            assert_first_decision(retry)
            pin = live.status()
            live.freeze()
            before = records(live.root)
            with fill.open("wb", buffering=0) as stream:
                try:
                    while True:
                        stream.write(b"x" * 4096)
                except OSError as error:
                    assert error.errno == errno.ENOSPC
                    print(f"fill errno={error.errno}", flush=True)
            assert os.statvfs(bounded).f_bavail == 0
            live.thaw()
            failed = request(live, "storage-full", pin)
            assert failed.returncode == 4, failed
            assert "retry ONLY as --request-id storage-full" in failed.stderr
            wait_for(lambda: live.proc.poll() is not None, "engine stopped on storage failure")
            assert live.proc.returncode == 1
            assert "database or disk is full" in (work / "engine.err").read_text()
            assert records(live.root) == before
            assert live.effects.read_text().splitlines() == ["1"]
            assert sorted(p.name for p in (live.root / "runs").iterdir()) == ["probe.1"]
        fill.unlink()
        # Fresh engine and CLI processes restore the committed state (DL-428).
        (work / "engine.out").unlink()
        with engine(full_work, work) as resumed:
            assert resumed.status()["jobs"]["probe"]["run_number"] == 1
            assert resumed.effects.read_text().splitlines() == ["1"]
            retry = request(resumed, "storage-first", original)
            assert_first_decision(retry)
            assert resumed.effects.read_text().splitlines() == ["1"]
            accepted = request(resumed, "storage-full", pin)
            assert accepted.returncode == 0, accepted
            wait_for(lambda: settled(resumed), "second run settled after space returned")
            assert resumed.status()["jobs"]["probe"]["run_number"] == 2
            assert resumed.effects.read_text().splitlines() == ["1", "2"]
            launches = [
                body["run_number"] for kind, body in records(resumed.root) if kind == "dispatch"
            ]
            assert launches == [1, 2]
    finally:
        fill.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="Use fresh work directories and a fresh bounded tmpfs. The storage row cannot "
        "be rerun in the same service-drill container.",
    )
    parser.add_argument("scenario", choices=("leader", "unit", "admission"))
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--bounded", type=Path)
    parser.add_argument("--unit")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, deadline_signal)
    if not args.worker:
        try:
            result = run_probe(
                [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:], "--worker"]
            )
        except subprocess.TimeoutExpired as error:
            print(error.output, end="", flush=True)
            print(error.stderr, end="", file=sys.stderr, flush=True)
            return 124
        print(result.stdout, end="", flush=True)
        print(result.stderr, end="", file=sys.stderr, flush=True)
        return result.returncode
    args.work.mkdir(parents=True, exist_ok=True)
    print(
        json.dumps(
            {
                "python": sys.version,
                "uid": os.getuid(),
                "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            }
        ),
        flush=True,
    )
    if args.scenario == "leader":
        test_frozen_leader(args.work)
    elif args.scenario == "unit":
        assert args.unit and args.config and args.root
        test_frozen_unit(args.work, args.unit, args.config, args.root)
    else:
        assert args.bounded is not None
        test_admission_enospc(args.work, args.bounded)


if __name__ == "__main__":
    sys.exit(main())
