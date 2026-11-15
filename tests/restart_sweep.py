"""The restart sweep: a real engine process ended at every journal append of a scripted scenario,
restarted on the same run root, and checked against invariants at every such point (DL-425,
DL-426, DL-433).

A scenario is a JIL catalog of gated commands and a script of steps. The reference run executes
the script with no fault. A crash run arms a fault in the first engine process: `before N` ends it
with SIGKILL on entry to its Nth `Journal.append` call, `after N` once that call has committed and
before it returns. One call is one transaction and may hold several records; genesis counts.

After the death the driver ends what the systemd unit would end (`KillMode=control-group`). A
session stands for the unit's control group: each engine process is started as a session leader,
and every wrapper leaves that session for its own (wrapper protocol, duty 1), so the unit here is
the engine's session plus the session of every wrapper it started, as `spawn.json` and the dying
engine's list of its children name them. A process that left these sessions is not reached.

The unit can act in two orders, and this module models one, `wrappers-first` (`UNIT_ORDER`): the
wrappers act on the lost lifeline and record it before the unit's TERM reaches their commands;
then every process still in those sessions gets TERM, then CONT, a bounded wait, then KILL. That
is not the unit's only behaviour. Under `KillMode=control-group` systemd sends TERM to the whole
group at once after the main process fails, so a command can die of that TERM before its
wrapper acts: the wrapper records `signaled 15`, and the restart does not run the attempt again.
That other order is BUG-89, and this module does not sweep it.

A successor (`resume_run`) then opens the same root, with a clock past every timer the scenario
arms, and the driver finishes the script: an event whose answer was lost is sent again with the
same envelope, and every gate is released by the scenario's codes until the store is quiescent.
The invariants (`check_invariants`) are functions of the final store, the spool and the
witness log; the final state each scenario must reach is declared in the scenario from the rules,
and no expected output is written per crash point. I7 counts any pending record closed by a
discharge, or plan step with a done mark, as a kept start, and does not check that the
supersession it records was due. I6 cannot see a lost attempt overlap its replacement, because
the unit's kill ends the lost attempt's processes before the successor opens.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from test_live_engine_branches import _Client

from dsl41 import runner_procid
from dsl41.canon import decode
from dsl41.ir import lower_source
from dsl41.runner_clock import RealClock
from dsl41.runner_config import EngineSettings
from dsl41.runner_control import ControlClientError, roundtrip
from dsl41.runner_journal import STORE_NAME, Journal

LINUX = sys.platform.startswith("linux")
#: the one order of the unit's kill this module sweeps (module docstring; the other is BUG-89)
UNIT_ORDER = "wrappers-first"
#: crash points one scenario runs at once, independent of xdist
CONCURRENCY = int(os.environ.get("DSL41_SWEEP_JOBS", "4"))
#: the bound on every wait for the engine, its commands or its records
BOUND_S = 60.0
#: the wrapper's grace on lifeline EOF: TERM, this, then KILL of the command's group
WRAPPER_GRACE_S = 1.0
#: the engine's stop ladder: TERM to KILL far longer than a scenario, so no ladder's KILL is
#: ever due while the script runs and the order of records stays the script's
SETTINGS = EngineSettings(
    grace_term_s=30.0,
    grace_kill_s=0.5,
    # H9: no snapshot and no prune while a scenario runs
    snapshot_interval_s=1e9,
    retention_days=36500,
)
#: the unit's stop timeout stands in for systemd's TimeoutStopSec, bounded for a test
UNIT_STOP_S = 2.0

BEGINNING = datetime(2026, 7, 1)
LATER = BEGINNING + timedelta(minutes=2)

TESTS = Path(__file__).resolve().parent


# ------------------------------------------------------------------ the store, read only


def rows(store: Path) -> list[tuple[int, str, dict[str, Any]]]:
    """Every journal row of `store` (a run root or an `engine.db`): seq, kind and payload. The
    connection is read only, so it never checkpoints the WAL a crash left, nor deletes it."""
    path = store if store.name == STORE_NAME else store / STORE_NAME
    if not path.exists():
        return []
    with contextlib.closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
        found = db.execute("SELECT seq, kind, payload FROM journal ORDER BY seq").fetchall()
    out = []
    for seq, kind, payload in found:
        body = decode(payload)
        assert isinstance(body, dict)
        out.append((int(seq), str(kind), body))
    return out


# ------------------------------------------------------------------ the gated command

GATE = r"""
import fcntl, json, os, signal, subprocess, sys, time
from pathlib import Path

directory = Path(sys.argv[1])
leftover = sys.argv[2] == "leftover"
sys.path.insert(0, sys.argv[3])  # the stdlib-only sibling the wrapper imports too
from runner_procid import proc_start_token
job, run, ordinal = os.environ["DSL41_JOB"], os.environ["DSL41_RUN_NUMBER"], os.environ["DSL41_ATTEMPT"]
name = f"{job}.{run}.{ordinal}" + (".leftover" if leftover else "")
gates = directory / "gates"


def witness(line):
    fd = os.open(directory / "witness.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (line + "\n").encode())
    finally:
        os.close(fd)


def acknowledge():
    body = {"job": job, "run": int(run), "ordinal": int(ordinal), "pid": os.getpid(),
            "token": proc_start_token(os.getpid()), "leftover": leftover,
            "run_id": os.environ.get("DSL41_COOKIE", "").rpartition(".")[2]}
    temporary = gates / (name + ".tmp")
    temporary.write_text(json.dumps(body))
    os.rename(temporary, gates / (name + ".ack"))


witness(f"start {name} {os.getpid()}")
if leftover:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
else:
    # I6: one open description per attempt, shared with its leftover; the kernel drops the lock
    # only when every process of the attempt is gone, whatever ended it
    lock = os.open(directory / "locks" / job, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        witness(f"overlap {name} {os.getpid()}")
    leaves = json.loads((directory / "behaviour.json").read_text()).get(job, [])
    if f"{run}.{ordinal}" in leaves or f"{run}.*" in leaves:
        subprocess.Popen(
            [sys.executable, __file__, str(directory), "leftover", sys.argv[3]], pass_fds=(lock,)
        )
        while not (gates / (name + ".leftover.ack")).exists():
            time.sleep(0.005)
acknowledge()
release = gates / (name + ".go")
while not release.exists():
    time.sleep(0.005)
code = int(release.read_text() or "0")
witness(f"exit {name} {os.getpid()} {code}")
sys.exit(code)
"""


def gate_command(directory: Path) -> str:
    """The JIL command of a gated job: the shell execs the gate, so the gate leads its group."""
    procid = Path(runner_procid.__file__).parent
    return f"exec {sys.executable} {directory / 'gate.py'} {directory} leader {procid}"


# ------------------------------------------------------------------ the engine process


@dataclass(frozen=True)
class Fault:
    position: str  # "before" or "after"
    n: int

    def __str__(self) -> str:
        return f"{self.position}-{self.n}"


class _ScriptClock(RealClock):
    """The real clock, or a clock that stands at BEGINNING until the store holds a STARTJOB of
    `job` and at LATER from then on: the latency between an admission and its dispatch, and a
    restart past every timer the scenario arms (DL-491)."""

    def __init__(self, root: Path, job: str | None) -> None:
        super().__init__()
        self.root = root
        self.job = job
        self.moved = False

    def now(self) -> datetime:
        if self.job is None:
            return super().now()
        if not self.moved and (self.root / STORE_NAME).exists():
            self.moved = any(
                kind == "input"
                and body.get("kind") == "STARTJOB"
                and body.get("payload", {}).get("job") == self.job
                for _, kind, body in rows(self.root)
            )
        return LATER if self.moved else BEGINNING


def _children() -> dict[int, str]:
    """This process's wrappers with their start tokens: a child of this engine is this run's."""
    listing = subprocess.Popen(
        ["ps", "-axww", "-o", "pid=,ppid=,command="], stdout=subprocess.PIPE, text=True
    )
    out, _ = listing.communicate(timeout=10)
    me, found = os.getpid(), {}
    for line in out.splitlines():
        fields = line.split(None, 2)
        if len(fields) < 3:
            continue
        pid, ppid, command = fields
        if int(ppid) == me and int(pid) != listing.pid and "runner_wrapper" in command:
            token = runner_procid.proc_start_token(int(pid))
            if token is not None:
                found[int(pid)] = token
    return found


def _install_fault(directory: Path, tag: str, fault: Fault | None) -> None:
    """H2: count every `Journal.append` call of this process, genesis included, and end the
    process at the fault. Before the SIGKILL the process names its children (the wrappers it
    started), so the unit's kill reaches a wrapper that has not written `spawn.json` yet."""
    original = Journal.append
    log = os.open(directory / f"appends-{tag}.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    calls = 0

    def die() -> None:
        (directory / f"children-{tag}.json").write_text(json.dumps(_children()))
        os.kill(os.getpid(), signal.SIGKILL)

    def append(self: Journal, *records: Any) -> int:
        nonlocal calls
        calls += 1
        os.write(log, f"{calls} {' '.join(r.kind for r in records)}\n".encode())
        if fault is not None and fault.position == "before" and calls == fault.n:
            die()
        last = original(self, *records)
        if fault is not None and fault.position == "after" and calls == fault.n:
            die()
        return last

    Journal.append = append  # type: ignore[method-assign]


def engine_child(spec_path: str) -> None:
    """H1: one engine incarnation, built as the tests build one: `start_run` or `resume_run`, a
    real `ControlServer`, `LocalCommandAdapter`, the real wrapper and real commands."""
    from dsl41.runner_adapters import LocalCommandAdapter
    from dsl41.runner_control import ControlServer
    from dsl41.runner_startup import resume_run, start_run

    spec = json.loads(Path(spec_path).read_text())
    directory = Path(spec["directory"])
    root = directory / "run"
    fault = Fault(spec["fault"][0], spec["fault"][1]) if spec["fault"] else None
    _install_fault(directory, spec["tag"], fault)

    async def run() -> None:
        restarting = (root / STORE_NAME).exists()
        clock = _ScriptClock(root, spec["clock_job"])
        catalog = lower_source((directory / "jobs.jil").read_text())
        options: dict[str, Any] = {
            "clock": clock,
            "adapters": {"CMD": LocalCommandAdapter(grace_seconds=WRAPPER_GRACE_S)},
            "settings": SETTINGS,
            "hold_open": True,
        }
        engine = (
            await resume_run(catalog, root, **options)
            if restarting
            else start_run(catalog, root, **options)
        )
        server = ControlServer(engine, root / "control.sock")
        await server.start()
        try:
            horizon = datetime.fromisoformat(spec["horizon"]) if spec["horizon"] else datetime.max
            await engine.run_until_quiescent(horizon)
            if engine.exit_code is None:
                await asyncio.Event().wait()  # past the horizon: the driver's stop or the fault
        finally:
            await server.close()
            await engine.shutdown()
            engine.journal.close()

    asyncio.run(run())


# ------------------------------------------------------------------ the unit's processes


def _processes() -> dict[int, str]:
    out = subprocess.run(
        ["ps", "-axww", "-o", "pid=,command="], capture_output=True, text=True, timeout=10
    ).stdout
    found = {}
    for line in out.splitlines():
        pid, _, command = line.strip().partition(" ")
        if pid.isdigit():
            found[int(pid)] = command
    return found


def _lives(pid: int, token: str | None) -> bool | None:
    """One reading of `pid`: True while it is the process `token` names, False when the reading
    shows no process, a zombie or another token, None when the reading failed. A failed
    reading is not gone: every wait (the unit's members and wrappers, a gate at quiescence)
    keeps waiting inside its bound, and nothing is signalled without a token that matched."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    look = runner_procid._proc_look(pid)
    if look is None:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass
        return None
    found, zombie = look
    if zombie:
        return False
    return None if token is None else runner_procid.start_tokens_match(found, token)


class Unit:
    """The processes one run started (module docstring). Every pid is proven before it is used:
    a session leader by the start token its record gave (the engine's at its start, a wrapper's
    in this run's `spawn.json` or in the dying engine's list of its children), any other member
    by its session and a command line that names this run's directory, with the start token
    read when it was listed. Every signal checks that token again (`signal_ours`); a member that
    is gone is dropped."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        #: proven session leaders: pid -> start token
        self.leaders: dict[int, str] = {}

    def lead(self, pid: int, token: str | None) -> None:
        if token is not None:
            self.leaders.setdefault(pid, token)

    def note_wrappers(self) -> None:
        for path in (self.directory / "run" / "runs").glob("*/*/spawn.json"):
            with contextlib.suppress(OSError, ValueError, KeyError):
                record = json.loads(path.read_text())
                self.lead(int(record["wrapper_pid"]), record["wrapper_start_time"])
        for path in self.directory.glob("children-*.json"):
            for pid, token in json.loads(path.read_text()).items():
                self.lead(int(pid), token)

    def wrappers(self) -> list[int]:
        """The live wrappers of this run: proven leaders still running as themselves."""
        self.note_wrappers()
        return [pid for pid, token in self.leaders.items() if _lives(pid, token) is not False]

    def members(self) -> dict[int, str | None]:
        """Every member not shown gone, pid -> start token, read now; None where the reading
        failed, which keeps it a member and leaves it unsignalled."""
        self.note_wrappers()
        found: dict[int, str | None] = {
            pid: token for pid, token in self.leaders.items() if _lives(pid, token) is not False
        }
        me = os.getpid()
        for pid, command in _processes().items():
            if pid == me or pid in found or str(self.directory) not in command:
                continue
            try:
                sid = os.getsid(pid)
            except OSError:
                continue
            if sid not in self.leaders:
                continue
            token = runner_procid.proc_start_token(pid)
            if _lives(pid, token) is not False:
                found[pid] = token
        return found

    def signal(self, sig: int) -> None:
        for pid, token in self.members().items():
            if token is not None:
                runner_procid.signal_ours(pid, token, sig)

    async def end(self) -> None:
        """H5 in the `wrappers-first` order (`UNIT_ORDER`): the wrappers' own ladders first, then
        TERM, CONT, a bounded wait and KILL."""
        await _bounded(lambda: not self.wrappers(), WRAPPER_GRACE_S + 10, "the wrappers' ladders")
        self.signal(signal.SIGTERM)
        self.signal(signal.SIGCONT)
        with contextlib.suppress(TimeoutError):
            await _bounded(lambda: not self.members(), UNIT_STOP_S, "the unit's TERM")
        self.signal(signal.SIGKILL)
        await _bounded(lambda: not self.members(), 10, "the unit's KILL")


async def _bounded(predicate: Callable[[], bool], bound: float, what: str) -> None:
    deadline = time.monotonic() + bound
    while not await asyncio.to_thread(predicate):
        if time.monotonic() > deadline:
            raise TimeoutError(f"{what}: still waiting after {bound} s")
        await asyncio.sleep(0.02)


# ------------------------------------------------------------------ the script


class Died(Exception):
    """The engine process ended while the driver waited on it."""


@dataclass(frozen=True)
class Send:
    """Send one event with a fixed request id; its envelope is fixed when it is first sent. A
    KILLJOB step's `ready` waits for its command's acknowledgement (H4)."""

    verb: str
    job: str
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Release:
    """Release one gated command, or the leftover it left (`leftover`)."""

    job: str
    run: int
    ordinal: int
    leftover: bool = False


@dataclass(frozen=True)
class Restart:
    """A restart the script makes itself: the engine dies, the unit ends, a successor opens."""


@dataclass(frozen=True)
class Step:
    name: str
    ready: Callable[[World], bool]
    act: Send | Release | Restart | None = None


@dataclass(frozen=True)
class End:
    """One job's final row as the `status` reply shows it (I8)."""

    status: str
    run_number: int
    exit_code: int | None
    on_hold: bool = False


@dataclass
class Scenario:
    """A catalog, a script, and how its gates end. `codes` maps (job, run, ordinal) to the exit
    code a gate is released with, None for a gate the engine ends; `clock_job` arms the script
    clock (`_ScriptClock`); `horizon` is the first incarnation's loop horizon. `ends` and `runs`
    are the final state the rules give, declared in the scenario: each job's row (I8) and the
    (job, run number) pairs dispatched (I10)."""

    name: str
    jil: Callable[[Path], str]
    steps: list[Step]
    codes: Callable[[str, int, int], int | None]
    ends: Mapping[str, End]
    runs: frozenset[tuple[str, int]]
    behaviour: Mapping[str, Any] = field(default_factory=dict)
    clock_job: str | None = None
    horizon: datetime | None = None


class World:
    """What the driver reads: the store, the gates' acknowledgements and the witness log."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.root = directory / "run"

    def rows(self) -> list[tuple[int, str, dict[str, Any]]]:
        return rows(self.root)

    def records(self, kind: str) -> list[dict[str, Any]]:
        return [body for _, found, body in self.rows() if found == kind]

    def acks(self) -> dict[str, dict[str, Any]]:
        found = {}
        for path in (self.directory / "gates").glob("*.ack"):
            found[path.name.removesuffix(".ack")] = json.loads(path.read_text())
        return found

    def acked(self, job: str, run: int, ordinal: int, leftover: bool = False) -> bool:
        name = f"{job}.{run}.{ordinal}" + (".leftover" if leftover else "")
        return (self.directory / "gates" / f"{name}.ack").exists()

    def plans(self, job: str) -> list[dict[str, Any]]:
        return [b for b in self.records("follow_up") if b["event"] == "plan" and b["job"] == job]

    def finished(self, job: str) -> bool:
        """Whether a `complete` plan of `job` is done: the oracle has the run's end."""
        rows = self.rows()
        done = {b["attempt"] for _, k, b in rows if k == "follow_up" and b["event"] == "done"}
        return any(
            k == "follow_up"
            and b["event"] == "plan"
            and b["job"] == job
            and b["plan"] == "complete"
            and b["attempt"] in done
            for _, k, b in rows
        )


def _gate_name(job: str, run: int, ordinal: int, leftover: bool) -> str:
    return f"{job}.{run}.{ordinal}" + (".leftover" if leftover else "")


class Driver:
    """One run of a scenario: the reference with `fault` None, else one crash point (H4, H6)."""

    def __init__(self, scenario: Scenario, directory: Path, fault: Fault | None) -> None:
        self.scenario = scenario
        self.directory = directory
        self.root = directory / "run"
        self.fault = fault
        self.world = World(directory)
        self.unit = Unit(directory)
        self.client = _Client(self.root)
        self.child: subprocess.Popen[bytes] | None = None
        self.incarnation = 0
        self.envelopes: dict[str, dict[str, Any]] = {}
        self.answered: set[str] = set()
        self.released: set[str] = set()
        #: the store as the crash left it, before any successor wrote
        self.crashed: list[tuple[int, str, dict[str, Any]]] | None = None
        self.exits: list[int | None] = []
        #: the first incarnation's append calls when its script ended: the reference's crash points
        self.appends = 0
        self.final: list[tuple[int, str, dict[str, Any]]] = []
        self.final_status: dict[str, Any] | None = None
        self.after_quiet: list[tuple[int, str, dict[str, Any]]] = []
        #: the crashed store's WAL size at the death and when the successor starts (None: none)
        self.wal: int | None = None
        self.wal_at_successor: int | None = None
        self.seconds = 0.0

    # ---- setup and processes

    def prepare(self) -> None:
        for name in ("gates", "locks"):
            (self.directory / name).mkdir()
        (self.directory / "gate.py").write_text(GATE)
        (self.directory / "behaviour.json").write_text(json.dumps(self.scenario.behaviour))
        (self.directory / "jobs.jil").write_text(self.scenario.jil(self.directory))

    async def start(self, *, fault: Fault | None, horizon: datetime | None) -> None:
        self.incarnation += 1
        tag = f"e{self.incarnation}"
        spec = {
            "directory": str(self.directory),
            "tag": tag,
            "fault": [fault.position, fault.n] if fault else None,
            "clock_job": self.scenario.clock_job,
            "horizon": horizon.isoformat() if horizon else None,
        }
        spec_path = self.directory / f"spec-{tag}.json"
        spec_path.write_text(json.dumps(spec))
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from restart_sweep import engine_child; engine_child(sys.argv[2])"
        )
        log = (self.directory / f"engine-{tag}.log").open("w")
        with log:
            self.child = subprocess.Popen(
                [sys.executable, "-c", code, str(TESTS), str(spec_path)],
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        self.unit.lead(self.child.pid, runner_procid.proc_start_token(self.child.pid))
        await self.until(self._answers, "the control socket")

    def _answers(self) -> bool:
        try:
            roundtrip(self.client.socket, {"v": 5, "cmd": "status"}, timeout=5)
        except (OSError, ControlClientError):
            return False
        return True

    def log(self) -> str:
        return "\n".join(
            f"--- {path.name}\n{path.read_text()}"
            for path in sorted(self.directory.glob("engine-*.log"))
        )

    async def until(self, predicate: Callable[[], bool], what: str) -> None:
        """H4: wait for `predicate`, racing the engine's exit, within `BOUND_S`."""
        deadline = time.monotonic() + BOUND_S
        while True:
            if await asyncio.to_thread(predicate):
                return
            assert self.child is not None
            if self.child.poll() is not None:
                tail = (self.directory / f"engine-e{self.incarnation}.log").read_text()
                raise Died(f"e{self.incarnation} ended while waiting for {what}: {tail[-600:]}")
            if time.monotonic() > deadline:
                raise TimeoutError(f"{self.label()}: waiting for {what}\n{self.log()}")
            await asyncio.sleep(0.01)

    async def ask(self, cmd: str, **fields: Any) -> dict[str, Any]:
        """One control request; a request the engine's death cut short is `Died`."""
        try:
            return await self.client.ask(cmd, **fields)
        except (OSError, ControlClientError) as exc:
            assert self.child is not None
            try:
                await asyncio.to_thread(self.child.wait, 5)
            except subprocess.TimeoutExpired:
                raise exc from None
            raise Died(f"e{self.incarnation} ended during {cmd}") from exc

    def label(self) -> str:
        return f"{self.scenario.name} {self.fault or 'reference'}"

    async def died(self) -> None:
        """The engine is gone: reap it, keep the store as it stands, end the unit (H5)."""
        assert self.child is not None
        code = await asyncio.to_thread(self.child.wait, BOUND_S)
        self.exits.append(code)
        if self.crashed is None and self.fault is not None:
            # the driver reads a copy, so the successor opens what the crash left, hot WAL
            # included: the last connection to close would checkpoint the WAL and delete it
            copy = self.directory / "crashed"
            copy.mkdir()
            for name in (STORE_NAME, f"{STORE_NAME}-wal", f"{STORE_NAME}-shm"):
                if (self.root / name).exists():
                    shutil.copy2(self.root / name, copy / name)
            self.wal = _wal_size(self.root)
            self.crashed = rows(copy)
        await self.unit.end()

    # ---- the acts

    def _rid(self, step: Step) -> str:
        return f"{self.scenario.name}:{step.name}"

    async def send(self, step: Step, act: Send) -> None:
        rid = self._rid(step)
        if rid in self.answered:
            return
        envelope = self.envelopes.get(rid)
        if envelope is None:
            status = await self.ask("status")
            envelope = {
                "verb": act.verb,
                "payload": {"job": act.job, **act.payload},
                "request_id": rid,
                "baseline_id": status["baseline_id"],
                "expect": {f"job:{act.job}": status["jobs"][act.job]["state_rev"]},
            }
            self.envelopes[rid] = envelope
        answer = await self.ask("sendevent", **envelope)
        assert answer["ok"], (self.label(), rid, answer)
        self.answered.add(rid)

    def release(self, job: str, run: int, ordinal: int, leftover: bool) -> None:
        name = _gate_name(job, run, ordinal, leftover)
        code = 0 if leftover else self.scenario.codes(job, run, ordinal)
        if code is None or name in self.released:
            return
        path = self.directory / "gates" / f"{name}.go"
        path.with_suffix(".tmp").write_text(str(code))
        os.rename(path.with_suffix(".tmp"), path)
        self.released.add(name)

    def release_every_gate(self) -> None:
        """After a restart every blocker goes by the scenario's policy."""
        for name, body in self.world.acks().items():
            if name not in self.released:
                self.release(body["job"], body["run"], body["ordinal"], body["leftover"])

    # ---- the run

    async def run(self) -> None:
        self.prepare()
        try:
            index = await self._first_incarnation()
            await self._successor(index)
            await self._stop()
            await self._quiet_restart()
        finally:
            await self.close()

    async def _first_incarnation(self) -> int:
        """The script in order until it ends, the fault fires, or the script restarts the
        engine; answers the index of the first step still to take."""
        steps = self.scenario.steps
        try:
            await self.start(fault=self.fault, horizon=self.scenario.horizon)
        except Died:
            await self.died()
            return 0
        for index, step in enumerate(steps):
            try:
                await self.until(lambda step=step: step.ready(self.world), step.name)
                if isinstance(step.act, Send):
                    await self.send(step, step.act)
                elif isinstance(step.act, Release):
                    gate = step.act
                    self.release(gate.job, gate.run, gate.ordinal, gate.leftover)
                elif isinstance(step.act, Restart):
                    assert self.child is not None
                    self.child.kill()
                    await self.died()
                    self.appends = self.count_appends()
                    return index + 1
            except Died:
                await self.died()
                return index
        try:
            await self._quiescent()
        except Died:
            await self.died()
        self.appends = self.count_appends()
        return len(steps)

    def count_appends(self) -> int:
        return len((self.directory / "appends-e1.log").read_text().splitlines())

    async def _successor(self, index: int) -> None:
        """H6: the successor finishes the script: lost answers are sent again, later events go
        once their step is ready, and every gate is released, to quiescence."""
        if index == len(self.scenario.steps) and self.child is not None:
            if self.child.poll() is None:
                return  # the script ended with the first incarnation alive
        if self.wal is not None:
            self.wal_at_successor = _wal_size(self.root)
        await self.start(fault=None, horizon=None)
        for step in self.scenario.steps[index:]:
            if not isinstance(step.act, Send):
                continue

            def ready(step: Step = step) -> bool:
                self.release_every_gate()
                return step.ready(self.world)

            await self.until(ready, step.name)
            await self.send(step, step.act)
        await self._quiescent(release=True)

    async def _quiescent(self, *, release: bool = False) -> None:
        def settled() -> bool:
            if release:
                self.release_every_gate()
            return quiet(self.world, self.scenario) is None

        while True:
            try:
                await self.until(settled, "quiescence")
            except TimeoutError as exc:
                named = [*still_open(self.world.rows()), f"{quiet(self.world, self.scenario)}"]
                raise TimeoutError(f"{self.label()}: no quiescence: {'; '.join(named)}") from exc
            last = len(self.world.rows())
            await self.ask("status")  # the loop has passed every synchronous step
            if len(self.world.rows()) == last and settled():
                return

    async def _stop(self) -> None:
        assert self.child is not None
        answer = await self.ask("engine_stop")
        assert answer["ok"], answer
        code = await asyncio.to_thread(self.child.wait, BOUND_S)
        self.exits.append(code)
        assert code == 0, f"{self.label()}: the engine's stop exited {code}\n{self.log()}"

    async def _quiet_restart(self) -> None:
        """I9's restart: one more incarnation after quiescence, stopped once it started."""
        self.final = self.world.rows()
        await self.start(fault=None, horizon=None)
        await self.until(
            lambda: (
                sum(
                    1
                    for _, kind, body in self.world.rows()[len(self.final) :]
                    if kind == "engine" and body["event"] == "started"
                )
                == 1
            ),
            "the quiet restart",
        )
        self.final_status = await self.ask("status")
        await self._quiescent()
        await self._stop()
        self.after_quiet = self.world.rows()[len(self.final) :]

    async def close(self) -> None:
        """H8: every process of every session this run started ends here."""
        if self.child is not None and self.child.poll() is None:
            self.child.kill()
            await asyncio.to_thread(self.child.wait, BOUND_S)
        self.unit.note_wrappers()
        for name in self.world.acks():
            # a gate nobody waits for any more exits on its own once released
            path = self.directory / "gates" / f"{name}.go"
            if not path.exists():
                path.write_text("0")
        with contextlib.suppress(TimeoutError):
            await _bounded(lambda: not self.unit.members(), 5, "released gates")
        self.unit.signal(signal.SIGKILL)
        await _bounded(lambda: not self.unit.members(), 10, "the run's last processes")


def quiet(world: World, scenario: Scenario) -> str | None:
    """Why the run is not quiescent, or None: an open attempt, pending start or plan step, a
    gate not yet released or still running."""
    table = open_work(world.rows())
    if table:
        return table
    for name, body in world.acks().items():
        code = 0 if body["leftover"] else scenario.codes(body["job"], body["run"], body["ordinal"])
        released = (world.directory / "gates" / f"{name}.go").exists()
        if code is not None and not released:
            return f"gate {name} not released"
        if _lives(body["pid"], body["token"]) is not False:
            return f"gate {name} still runs"
    return None


def _wal_size(root: Path) -> int | None:
    wal = root / f"{STORE_NAME}-wal"
    return wal.stat().st_size if wal.exists() else None


def still_open(journal: list[tuple[int, str, dict[str, Any]]]) -> list[str]:
    """A run that never reached quiescence, by invariant: every pending record with no closer
    (I4), every plan step with no done mark (I5), and every attempt with no end."""
    closed = {b["discharges"] for _, k, b in journal if k == "dispatch" and "discharges" in b}
    closed |= {
        b["pending"] for _, k, b in journal if k == "dispatch_pending" and b["event"] != "pending"
    }
    done = {
        (b["attempt"], b["step"])
        for _, k, b in journal
        if k == "follow_up" and b["event"] == "done"
    }
    found = [
        f"I4: pending {seq} ({b['job']}.{b['run_number']}) is not closed"
        for seq, k, b in journal
        if k == "dispatch_pending" and b["event"] == "pending" and seq not in closed
    ]
    found += [
        f"I5: plan {seq} ({b['job']}.{b['run_number']} {b['plan']}) step {i} has no done mark"
        for seq, k, b in journal
        if k == "follow_up" and b["event"] == "plan"
        for i, _ in enumerate(b["steps"])
        if (b["attempt"], i) not in done
    ]
    attempts = open_work(journal)
    if attempts is not None and attempts.startswith("open attempts"):
        found.append(f"quiescence: {attempts}")
    return found


def open_work(journal: list[tuple[int, str, dict[str, Any]]]) -> str | None:
    """What the store leaves open: an attempt with no end, a pending start, a plan step with no
    done mark (the open table's fold, read from the records)."""
    attempts: dict[str, str] = {}
    pending: set[int] = set()
    plans: dict[str, int] = {}
    for seq, kind, body in journal:
        if kind == "dispatch":
            attempts[body["attempt"]] = f"{body['job']}.{body['run_number']}.{body['ordinal']}"
            pending.discard(body.get("discharges", -1))
        elif kind == "outcome" and body.get("ladder") != "running":
            attempts.pop(body["attempt"], None)
        elif kind == "dispatch_pending":
            if body["event"] == "pending":
                pending.add(seq)
            else:
                pending.discard(body["pending"])
        elif kind == "follow_up":
            if body["event"] == "ladder_done":
                attempts.pop(body["attempt"], None)
            elif body["event"] == "plan" and body["steps"]:
                plans[body["attempt"]] = len(body["steps"])
            elif body["event"] == "done":
                plans.pop(body["attempt"], None)
    if attempts:
        return f"open attempts {sorted(attempts.values())}"
    if pending:
        return f"pending starts {sorted(pending)}"
    if plans:
        return f"open plans {sorted(plans)}"
    return None


# ------------------------------------------------------------------ determinism (H7)

#: the fields of a record that name a seq: compared relative to the record's own seq
_SEQ_FIELDS = ("discharges", "pending", "seq", "first_dispatch_seq")


def normalised(journal: list[tuple[int, str, dict[str, Any]]]) -> list[tuple[Any, ...]]:
    """H7: each record as kind, event, job, run number, ordinal, outcome, plan and the seqs it
    names, relative to its own; attempts by (job, run, ordinal), never by uuid."""
    names: dict[str, str] = {}
    for _, kind, body in journal:
        if kind == "dispatch":
            names[body["attempt"]] = f"{body['job']}.{body['run_number']}.{body['ordinal']}"
        elif kind == "follow_up" and body["event"] == "plan":
            for step in body["steps"]:
                if "attempt" in step:
                    names[step["attempt"]] = f"{body['job']}.{body['run_number']}.{step['ordinal']}"
    out = []
    for seq, kind, body in journal:
        payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
        refs = tuple(
            (name, body[name] - seq)
            for name in _SEQ_FIELDS
            if isinstance(body.get(name), int) and not isinstance(body.get(name), bool)
        )
        steps = tuple(
            (step["step"], names.get(step.get("attempt", ""), "")) for step in body.get("steps", ())
        )
        out.append(
            (
                kind,
                body.get("event") or body.get("kind") or body.get("decision"),
                body.get("job") or payload.get("job"),
                body.get("run_number"),
                body.get("ordinal"),
                body.get("outcome") or payload.get("status"),
                body.get("plan"),
                names.get(body.get("attempt", ""), body.get("attempt") and "?"),
                body.get("cause"),
                body.get("ladder"),
                body.get("step"),
                body.get("source"),
                steps,
                refs,
            )
        )
    return out


# ------------------------------------------------------------------ the invariants


def check_invariants(driver: Driver, reference: Driver) -> list[str]:
    """I1 to I10 over one crash point's final store, spool and witness log; answers each broken
    invariant with its evidence."""
    broken: list[str] = []
    final = driver.final
    if driver.crashed is None:
        return ["H2: the fault never fired"]
    crashed = driver.crashed
    assert driver.fault is not None
    calls = len((driver.directory / "appends-e1.log").read_text().splitlines())
    if driver.exits[0] != -signal.SIGKILL or calls != driver.fault.n:
        broken.append(f"H2: e1 ended with {driver.exits[0]} at append call {calls}")
    # I1: the final successor started, and only the driver's stop ended it
    if not any(k == "engine" and b["event"] == "started" for _, k, b in final[len(crashed) :]):
        broken.append("I1: the successor recorded no `engine started`")
    if driver.exits[-2:] != [0, 0]:
        broken.append(f"I1: exits {driver.exits}")
    # H5: the successor opened the store as the crash left it, its WAL untouched by the driver
    if driver.wal and driver.wal_at_successor != driver.wal:
        broken.append(f"H5: the WAL was {driver.wal} bytes at the death, {driver.wal_at_successor}")
    # I2: the successor's journal begins with the crashed journal
    if final[: len(crashed)] != crashed:
        broken.append("I2: the successor's journal does not begin with the crashed journal")
    # H7: the crashed records are a prefix of the reference's
    mine, theirs = normalised(crashed), normalised(reference.final)
    differ = [i for i, (a, b) in enumerate(zip(mine, theirs, strict=False)) if a != b]
    if differ or len(mine) > len(theirs):
        at = differ[0] if differ else len(theirs)
        broken.append(f"H7: record {at} is {mine[at]}, the reference's {theirs[at : at + 1]}")
    broken += _once(final)
    broken += _pending_closed(final)
    broken += _plans_done(final)
    broken += _no_overlap(driver.directory)
    broken += _nothing_lost(driver.root, final)
    broken += declared(driver)
    # I9: a restart after quiescence appends no dispatch and no pending start
    late = [k for _, k, _ in driver.after_quiet if k in ("dispatch", "dispatch_pending")]
    if late:
        broken.append(f"I9: the quiet restart appended {late}")
    return broken


def _once(final: list[tuple[int, str, dict[str, Any]]]) -> list[str]:
    """I3: no two dispatches share (job, run, ordinal)."""
    counts = Counter(
        (b["job"], b["run_number"], b["ordinal"]) for _, k, b in final if k == "dispatch"
    )
    return [f"I3: {key} dispatched {n} times" for key, n in counts.items() if n > 1]


def _pending_closed(final: list[tuple[int, str, dict[str, Any]]]) -> list[str]:
    """I4: every pending start is closed exactly once, by one dispatch or one discharge."""
    pending = [s for s, k, b in final if k == "dispatch_pending" and b["event"] == "pending"]
    broken = []
    for seq in pending:
        closers = [
            s
            for s, k, b in final
            if (k == "dispatch" and b.get("discharges") == seq)
            or (k == "dispatch_pending" and b["event"] != "pending" and b["pending"] == seq)
        ]
        if len(closers) != 1:
            broken.append(f"I4: pending {seq} closed by {closers}")
    return broken


def _plans_done(final: list[tuple[int, str, dict[str, Any]]]) -> list[str]:
    """I5: every plan step has exactly one done mark."""
    broken = []
    for seq, kind, body in final:
        if kind != "follow_up" or body["event"] != "plan":
            continue
        for index, _ in enumerate(body["steps"]):
            marks = [
                s
                for s, k, b in final
                if k == "follow_up"
                and b["event"] == "done"
                and b["attempt"] == body["attempt"]
                and b["step"] == index
            ]
            if len(marks) != 1:
                broken.append(f"I5: plan {seq} step {index} done by {marks}")
    return broken


def _no_overlap(directory: Path) -> list[str]:
    """I6: by the witness log, two attempts of one job never run at the same time. The gate's
    per-attempt lock records `overlap` when an attempt starts while a process of another is
    alive; start and exit lines bound the intervals the log can close."""
    path = directory / "witness.log"
    lines = path.read_text().splitlines() if path.exists() else []
    broken = [f"I6: {line}" for line in lines if line.startswith("overlap ")]
    started: dict[str, int] = {}
    for at, line in enumerate(lines):
        word, name, *_ = line.split()
        if word == "start":
            started[name] = at
        elif word == "exit":
            # a killed process writes no exit line: only the lock bounds its life
            job = name.split(".")[0]
            broken += [
                f"I6: {other} started while {name} ran"
                for other, begun in started.items()
                if other.split(".")[0] == job
                and other.split(".")[:3] != name.split(".")[:3]
                and started[name] < begun < at
            ]
    return broken


def _nothing_lost(root: Path, final: list[tuple[int, str, dict[str, Any]]]) -> list[str]:
    """I7: every start the journal decided is, at quiescence, a dispatch, a closed pending record,
    a done plan step, or a recorded supersession. The decided starts are the runs replay plans
    from the applied inputs (`Replay.spawns`: `dispatch_plan` over each input, less the starts
    `spawn_superseded` refused, which is the supersession an input records), every run a
    `dispatch_pending` names (a start held back by a drain, a held group or a recovery's
    `_owe`), and every replacement a plan step names. A start in `Replay.spawns` with no
    dispatch and no pending record is the start `_discharge_pending`'s second loop exists for
    (DL-102): one the previous engine decided and never recorded."""
    from dsl41.runner_journal import open_reader, replay_store

    with contextlib.closing(open_reader(root)) as store:
        spawns = replay_store(store, where=str(root)).replay.spawns
    dispatched = {(b["job"], b["run_number"]) for _, k, b in final if k == "dispatch"}
    pending = {
        (b["job"], b["run_number"])
        for _, k, b in final
        if k == "dispatch_pending" and b["event"] == "pending"
    }
    broken = [
        f"I7: {job}.{run} was decided and is neither dispatched nor pending"
        for job, run in sorted(spawns - dispatched - pending)
    ]
    # a pending record or a plan step is closed by I4 and I5; a closed one is its supersession
    return broken


def declared(driver: Driver) -> list[str]:
    """I8 and I10 against the scenario's declared final state, for a crash point and for the
    reference alike. I8: each job's `status`, `run_number`, `exit_code` and `on_hold` in the
    status reply. I10: the (job, run number) pairs that were dispatched; a rerun may change an
    ordinal, never a run number.

    The rules that may change a crash point's expectation are DL-424 (a lost attempt runs again
    as the run's next attempt and uses one up), DL-485 (a marked job is held) and E9 (a start
    missed while down is dropped). In S1 and S2 none changes it: DL-424 shifts ordinals only,
    and every scenario code exits as the declaration says at any ordinal the shift reaches; the
    harness marks no job; no job starts on a schedule. One rule outside those three does change
    it (`overdue_before_launch`)."""
    if driver.final_status is None:
        return ["I8: the run has no final statuses"]
    broken = []
    for job, want in driver.scenario.ends.items():
        row = driver.final_status["jobs"][job]
        found = End(row["status"], row["run_number"], row["exit_code"], row["on_hold"])
        if found != want:
            broken.append(f"I8: {job} ends {found}, declared {want}")
    dispatched = {(b["job"], b["run_number"]) for _, k, b in driver.final if k == "dispatch"}
    dropped = overdue_before_launch(driver)
    runs = driver.scenario.runs - dropped
    if dispatched != runs:
        broken.append(f"I10: runs dispatched {sorted(dispatched)}, expected {sorted(runs)}")
    broken += [_deadline_first(driver, job, run) for job, run in sorted(dropped)]
    return [line for line in broken if line]


def _deadline_first(driver: Driver, job: str, run: int) -> str:
    """The mechanism of a dropped start, from the final journal: the job's deadline ends the
    run (a time observation whose decision moves the job's row) before the start's pending
    record is discharged. Answers why not, or the empty string."""
    final = driver.final
    after = len(driver.crashed or [])
    moved = {
        b["index"]: b["revisions"] for _, k, b in final if k == "decision" and "revisions" in b
    }
    pending = [
        seq
        for seq, k, b in final
        if k == "dispatch_pending"
        and b["event"] == "pending"
        and (b["job"], b["run_number"]) == (job, run)
    ]
    discharged = [
        seq
        for seq, k, b in final
        if k == "dispatch_pending" and b["event"] == "discharged" and b["pending"] in pending
    ]
    deadline = [
        seq
        for seq, k, b in final[after:]
        if k == "input" and b.get("kind") is None and f"job:{job}" in moved.get(b["index"], {})
    ]
    if not discharged or not deadline or deadline[0] > discharged[0]:
        return (
            f"I10: {job}.{run} was dropped, but its deadline (seq {deadline[:1]}) does not come"
            f" before the discharge of its pending start (seq {discharged[:1]})"
        )
    return ""


def overdue_before_launch(driver: Driver) -> set[tuple[str, int]]:
    """The starts a crash point's expectation drops from I10. A start the crashed journal decided
    and never dispatched is owed as pending work (DL-102), and DL-425's RESUME paragraph rules
    its order: "Due timers and reconciliation run before any recovered start is dispatched, so a
    start an overdue deadline would end is never launched". The recovery barrier holds the
    start until that due work has run (runner-design §7, step 5). So a `term_run_time` already
    past at the restart ends the run first and the start is discharged, never dispatched. Read
    from the crashed journal: the decided starts with no dispatch, of a job whose
    `term_run_time` from the start's instant falls at or before the restart's clock (LATER once
    the script clock has moved). The job's row still ends as declared (I8), and
    `_deadline_first` checks the order in the final journal."""
    from dsl41.runner_journal import open_reader, replay_store

    crashed = driver.crashed or []
    clock = driver.scenario.clock_job
    moved = any(
        k == "input" and b.get("kind") == "STARTJOB" and b["payload"].get("job") == clock
        for _, k, b in crashed
    )
    if clock is None or not moved or not crashed:
        return set()
    with contextlib.closing(open_reader(driver.directory / "crashed")) as store:
        replayed = replay_store(store, where="the crashed store")
    dispatched = {(b["job"], b["run_number"]) for _, k, b in crashed if k == "dispatch"}
    dropped = set()
    for job, run in replayed.replay.spawns - dispatched:
        row = replayed.oracle.store.job[job]
        minutes = replayed.oracle.catalog.jobs[job].sem.term_run_time_min
        if minutes and row.status_at is not None:
            if row.status_at + timedelta(minutes=minutes) <= LATER:
                dropped.add((job, run))
    return dropped


# ------------------------------------------------------------------ the sweep


@dataclass
class Point:
    #: None for the reference run
    fault: Fault | None
    broken: list[str]
    directory: Path
    seconds: float
    #: the crash left a non-empty WAL, which the successor opened as it was
    hot: bool = False


#: the crash points of one position, split so a known defect's point is its own pytest item
PARTS = ("genesis", "rest", "all")
BUG88 = "BUG-88: a death inside genesis leaves a root that no restart opens"


class Bug88(AssertionError):
    """The `before 1` point failed as BUG-88 says, and only so."""


def numbers(part: str, appends: int) -> range:
    """`genesis` is append call 1, `rest` the calls after it, `all` every call."""
    return {"genesis": range(1, 2), "rest": range(2, appends + 1), "all": range(1, appends + 1)}[
        part
    ]


def verdict(points: list[Point], *, expected: str | None = None) -> None:
    """Pass when no point broke an invariant. With `expected` (BUG88), every broken point must
    be the restart that stops at `assert last_at is not None`: those points drop their
    directories and the call raises `Bug88`, which the strict mark expects. Any other break
    fails as itself and keeps its directory."""
    broken = [p for p in points if p.broken]
    if not broken:
        # the H5 check is not vacuous: some crash left a hot WAL for its successor
        assert expected is not None or any(p.hot for p in points), "no crash left a hot WAL"
        return
    known = expected is not None and all(
        p.fault is not None
        and len(p.broken) == 1
        and p.broken[0].startswith("run: Died: e2 ended while waiting for the control socket")
        and "assert last_at is not None" in p.broken[0]
        for p in broken
    )
    if known:
        for p in broken:
            (p.directory / "broken").unlink()
        raise Bug88(f"{expected}\n{report(points)}")
    raise AssertionError(report(points))


async def sweep(
    scenario: Scenario, position: str, make_directory: Callable[[], Path], part: str = "all"
) -> tuple[Driver, list[Point]]:
    """The reference run, then one crash run per append call of its first incarnation that
    `part` names, at most `CONCURRENCY` at once. A crash point that breaks an invariant keeps
    its directory."""
    reference = Driver(scenario, make_directory(), None)
    failed: list[Point] = []
    started = time.monotonic()
    try:
        await reference.run()
    except Exception as exc:  # the crash points still run and report
        # the first incarnation's records stand; a check that needs the rest says so
        reference.final = reference.world.rows()
        (reference.directory / "broken").write_text(str(exc))
        broken = [f"run: {type(exc).__name__}: {exc}", *_once(reference.final)]
        failed.append(Point(None, broken, reference.directory, 0.0))
    else:
        if broken := declared(reference):
            (reference.directory / "broken").write_text("\n".join(broken))
            failed.append(Point(None, broken, reference.directory, 0.0))
    if leftover := await asyncio.to_thread(reference.unit.members):
        failed.append(Point(None, [f"H8: left behind {leftover}"], reference.directory, 0.0))
    reference.seconds = time.monotonic() - started
    assert reference.appends > 0, failed
    limit = asyncio.Semaphore(CONCURRENCY)

    async def point(n: int) -> Point:
        async with limit:
            directory = make_directory()
            driver = Driver(scenario, directory, Fault(position, n))
            started = time.monotonic()
            try:
                await driver.run()
                broken = check_invariants(driver, reference)
            except Exception as exc:  # a crash point reports, the sweep goes on
                # the store as the run left it still answers I3
                broken = [f"run: {type(exc).__name__}: {exc}", *_once(driver.world.rows())]
            leftover = await asyncio.to_thread(driver.unit.members)
            if leftover:
                broken.append(f"H8: left behind {leftover}")
            if broken:
                (directory / "broken").write_text("\n".join(broken))
            hot = bool(driver.wal) and driver.wal_at_successor == driver.wal
            return Point(driver.fault, broken, directory, time.monotonic() - started, hot)

    points = await asyncio.gather(*(point(n) for n in numbers(part, reference.appends)))
    reference.seconds = time.monotonic() - started
    return reference, failed + list(points)


def report(points: Iterable[Point]) -> str:
    return "\n".join(
        f"{p.fault or 'reference'} ({p.directory}): " + "; ".join(p.broken)
        for p in points
        if p.broken
    )


def budget(reference: Driver, points: list[Point]) -> str:
    """The sweep's size and time: append calls, crash points, the slowest point, the whole."""
    crashes = [p for p in points if p.fault is not None]
    position = crashes[0].fault.position if crashes and crashes[0].fault else "?"
    return (
        f"{reference.scenario.name} {position}: {reference.appends} append calls,"
        f" {len(crashes)} crash points, slowest {max(p.seconds for p in crashes):.1f} s,"
        f" sweep {reference.seconds:.1f} s"
    )


@contextlib.contextmanager
def directories() -> Iterator[Callable[[], Path]]:
    """Short directories for the sweep's runs (AF_UNIX paths); a broken point's stays."""
    made: list[Path] = []

    def make() -> Path:
        made.append(Path(tempfile.mkdtemp(prefix="dsl41-", dir="/tmp")))
        return made[-1]

    try:
        yield make
    finally:
        for directory in made:
            if not (directory / "broken").exists():
                shutil.rmtree(directory, ignore_errors=True)
