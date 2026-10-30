"""DL-404 slices 5 and 6: the file-processing README's exercise, run as the page prints it.

A block whose fence follows a `<!-- exercise: NAME -->` line is one step; `repeat` after
the name marks a step the operator types again until it prints what is shown. Each step
runs in one POSIX shell after the `variables` step, with `work` set to a short directory,
the locked environment first on PATH and the repository root as the working directory.
A `#` line after a command is that command's output, standard output and error together;
`…` matches any text. A command with no `#` line prints nothing. A first `#` line
`exit N: TEXT` expects exit status N; any other command exits 0.

The steps run the example estate under the real engine and the supervisor it spawns: the
watcher, the archiver, the processor and the example's REARM job. The test adds what the
page cannot show: the ledger and the outputs after the stop, and at the end that work
completed in admission order, every row was claimed once, and the interrupted item's
effect is the original file.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest
from test_docs_hygiene import ROOT
from test_fw_loop import _stop as _cancel
from test_fw_loop import _successes, _until

from dsl41.file_processing import archive, list_work, process, show_work
from dsl41.ir import lower_source
from dsl41.oracle_state import Event
from dsl41.runner_adapters import FileWatcherAdapter, LocalCommandAdapter
from dsl41.runner_clock import RealClock
from dsl41.runner_control import ControlServer
from dsl41.runner_startup import start_run
from examples.file_processing.estate import estate
from examples.file_processing.rearm import pending
from examples.file_processing.worker import counts_handler

if not sys.platform.startswith(("linux", "darwin")):  # pragma: no cover
    pytest.skip("the runner and its control socket are POSIX-only", allow_module_level=True)

README = ROOT / "examples" / "file_processing" / "README.md"
MARKER = re.compile(r"^<!-- exercise: ([a-z-]+)( repeat)? -->$")
EXIT = re.compile(r"exit (\d+): (.*)\Z")
#: Each repeat step's bound. A cycle is two one-second watch polls and three short jobs.
WAIT_S = 60.0
SEP = "\x1e"
PROCESSOR_ID = "counts-v1"
FIRST = "e2655ccff9fd0788f8676a41e9f7cb78cd1122fca579a099e8b340b4b702687f"
#: The steps the test reads the root after, and those slices 5 and 6 need.
REQUIRED = ("holding", "engine-stopped", "interrupted", "resumed", "stuck", "reject", "done")


@dataclass(frozen=True)
class Command:
    text: str
    expected: tuple[str, ...]


@dataclass(frozen=True)
class Step:
    name: str
    repeat: bool
    commands: tuple[Command, ...]


def steps() -> list[Step]:
    """The page's steps in order. A fence opens on the line after its marker; a name is
    used once; a `#` line follows a command; a command may continue with a backslash."""
    lines = README.read_text(encoding="utf-8").splitlines()
    found: list[Step] = []
    for number, line in enumerate(lines):
        match = MARKER.match(line)
        if match is None:
            continue
        assert lines[number + 1] == "```sh", f"{README.name}:{number + 2}: no ```sh fence"
        end = lines.index("```", number + 2)
        commands: list[tuple[list[str], list[str]]] = []
        continued = False
        for text in lines[number + 2 : end]:
            if continued:
                commands[-1][0].append(text)
            elif text == "#" or text.startswith("# "):
                assert commands, f"{match[1]}: an output line before any command"
                commands[-1][1].append(text[2:])
            else:
                commands.append(([text], []))
            continued = text.endswith("\\")
        found.append(
            Step(
                match[1],
                match[2] is not None,
                tuple(Command("\n".join(t), tuple(e)) for t, e in commands),
            )
        )
    names = [step.name for step in found]
    assert len(names) == len(set(names)), f"a step name is used twice: {names}"
    return found


def _pattern(expected: str) -> re.Pattern[str]:
    return re.compile(".*".join(re.escape(part) for part in expected.split("…")))


def mismatches(command: Command, status: int, output: str) -> list[str]:
    """What differs between a command's printed output and its run."""
    expected = list(command.expected)
    want = 0
    if expected and (match := EXIT.match(expected[0])):
        want, expected[0] = int(match[1]), match[2]
    actual = output.splitlines()
    problems = [] if status == want else [f"exit {status}, the page says {want}"]
    if len(actual) != len(expected) or not all(
        _pattern(e).fullmatch(a) for e, a in zip(expected, actual, strict=False)
    ):
        problems.append(f"printed {actual!r}, the page says {expected!r}")
    return [f"{command.text!r}: {problem}" for problem in problems]


def run_step(step: Step, prelude: Step, env: dict[str, str]) -> list[str]:
    """One pass of the step in one shell; each command's status ends its output."""
    script = "\n".join(command.text for command in prelude.commands) + "\n"
    for command in step.commands:
        script += f"{command.text}\nprintf '{SEP}%s\\n' \"$?\"\n"
    done = subprocess.run(
        ["sh", "-uc", script],
        cwd=ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=120,
    )
    parts = done.stdout.split(SEP)
    assert len(parts) == len(step.commands) + 1, f"{step.name}: {done.stdout}"
    problems = []
    for index, command in enumerate(step.commands):
        output = parts[index] if index == 0 else parts[index].partition("\n")[2]
        status = int(parts[index + 1].partition("\n")[0])
        problems += mismatches(command, status, output)
    return problems


def run_until(step: Step, prelude: Step, env: dict[str, str]) -> None:
    deadline = time.monotonic() + (WAIT_S if step.repeat else 0)
    while problems := run_step(step, prelude, env):
        if time.monotonic() > deadline:
            raise AssertionError(f"step {step.name}: " + "; ".join(problems))
        time.sleep(0.2)


def _pid(path: Path, key: str = "pid") -> int | None:
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    return int(text) if text.strip().isdigit() else int(json.loads(text)[key])


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _stop(*pids: int | None) -> None:
    """Teardown after a failed step: the engine, which ends its commands. It is not this
    process's child: a shell that exited started it."""
    for pid in pids:
        if pid is None or _gone(pid):
            continue
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 30
        while not _gone(pid):
            if time.monotonic() > deadline:  # pragma: no cover -- only a stuck process
                os.kill(pid, signal.SIGKILL)
                break
            time.sleep(0.05)


def test_every_marked_block_parses_and_the_exercise_covers_the_slice() -> None:
    """The page's grammar holds, and the exercise keeps the steps slices 5 and 6 need."""
    names = [step.name for step in steps()]
    assert names[0] == "variables"
    for name in REQUIRED:
        assert name in names
    for command in (c for step in steps() for c in step.commands):
        if command.expected and (match := EXIT.match(command.expected[0])):
            assert int(match[1]) != 0
    assert mismatches(Command("x", ("exit 2: no …",)), 2, "no supervisor\n") == []
    assert mismatches(Command("x", ()), 0, "") == []
    assert len(mismatches(Command("x", ("a",)), 1, "b\n")) == 2


def test_dl404_exercise_runs_the_estate_through_a_stop_and_a_rejection(short_root: Path) -> None:
    """Deliveries are archived and processed in admission order under the real engine and
    supervisor. A supervisor stop while the processor holds a claim leaves the ledger
    recoverable; on resume REARM starts the next cycle, which reconciles the claim without a
    second handler call; an operator rejects the toxic delivery and a manual run drains the
    queue. The handler's calls file is the witness independent of the ledger."""
    work = short_root
    root = work / "root"
    env = {
        **os.environ,
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}",
        "work": str(work),
    }
    env.pop("VIRTUAL_ENV", None)
    page = steps()
    assert {step.name for step in page} >= set(REQUIRED)
    prelude = page[0]
    first = root / "output" / f"{FIRST}.json"
    calls = root / "calls"
    effect: tuple[int, int, bytes] | None = None
    stopped = False
    try:
        for step in page:
            run_until(step, prelude, env)
            if step.name == "interrupted":
                (status,) = [row for row in list_work(root, PROCESSOR_ID) if row.work_key == FIRST]
                assert (status.state, status.attempt) == ("RUNNING", 1)
                assert calls.read_text().split() == [FIRST]
                effect = (first.stat().st_ino, first.stat().st_mtime_ns, first.read_bytes())
            if step.name == "engine-stopped-again":
                stopped = True
    finally:
        if not stopped:
            _stop(_pid(work / "engine.pid"))

    rows = list_work(root, PROCESSOR_ID)
    assert [(row.receipt_sequence, row.receipts[0].name, row.state) for row in rows] == [
        (1, "001-a.txt", "DONE"),
        (2, "002-b.json", "DONE"),
        (3, "003-bad.json", "REJECTED"),
        (4, "004-c.txt", "DONE"),
    ]
    done = [row for row in rows if row.state == "DONE"]
    outputs = sorted(path.name for path in (root / "output").iterdir())
    assert outputs == sorted(f"{row.work_key}.json" for row in done)
    assert (first.stat().st_ino, first.stat().st_mtime_ns, first.read_bytes()) == effect
    # the handler ran exactly once per row, the interrupted one included
    assert calls.read_text().split() == [row.work_key for row in rows]
    records = {row.work_key: show_work(root, PROCESSOR_ID, row.work_key)[1] for row in rows}
    # every row was claimed once: the handler ran once per row, the reconciler did the rest
    for record in records.values():
        assert [d.chart_row for d in record].count("file_process.01") == 1
    assert [d.chart_row for d in records[FIRST]] == [
        "file_process.01",
        "file_process.04",
        "file_process.05",
    ]
    # completions follow admission order across the three processor runs
    finished = [records[row.work_key][-1].decided_at for row in done]
    assert finished == sorted(finished)


def test_dl417_a_delivery_during_a_processor_run_is_processed_without_an_operator(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-417: a delivery and its signal published while the processor runs are processed
    by the next cycle with no operator action. The held handler keeps the first processor
    run alive while the test delivers; REARM, after that run ends, finds the delivery and
    publishes the signal again."""
    monkeypatch.chdir(ROOT)  # the processor imports the sample handler from the checkout
    root, run_root = short_root / "root", short_root / "run"
    sock, signal_path = run_root / "control.sock", root / "ready.signal"
    (root / "inbox").mkdir(parents=True)
    (root / "inbox" / "001-a.txt").write_bytes(b"one\n")
    (root / "hold").touch()
    signal_path.write_text("ready\n")
    held = "examples.file_processing.worker:held_counts_handler"
    jil = estate(root, signal_path, Path(sys.executable), "m1", handler=held, rearm=sock).to_jil()

    async def scenario() -> None:
        clock = RealClock()
        engine = start_run(
            lower_source(jil),
            run_root,
            clock=clock,
            adapters={"CMD": LocalCommandAdapter(), "FW": FileWatcherAdapter()},
            hold_open=True,
        )
        journal = engine.journal
        assert journal is not None
        async with contextlib.AsyncExitStack() as teardown:
            teardown.callback(journal.close)
            teardown.push_async_callback(engine.shutdown)
            server = ControlServer(engine, sock)
            await server.start()
            teardown.push_async_callback(server.close)
            loop_task = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
            teardown.push_async_callback(_cancel, loop_task)
            store = engine.oracle.store.job
            engine.inject(Event(at=clock.now(), kind="STARTJOB", payload={"job": "INBOX_FW"}))
            await _until(lambda: (root / "calls").exists(), "the first handler call", loop_task)
            # the processor holds; a delivery lands with its signal
            staged = short_root / "002-b.txt"
            staged.write_bytes(b"two\n")
            os.replace(staged, root / "inbox" / "002-b.txt")
            signal_path.write_text("ready\n")
            assert store["PROCESSOR"].status == "RUNNING"
            (root / "hold").unlink()
            await _until(lambda: _successes(engine, "PROCESSOR") == 2, "cycle 2", loop_task)
            await _until(lambda: _successes(engine, "REARM") == 2, "the second rearm", loop_task)
            await _until(
                lambda: store["INBOX_FW"].run_number == 3 and store["INBOX_FW"].status == "RUNNING",
                "the third watch",
                loop_task,
            )
            assert _successes(engine, "ARCHIVER") == 2

    asyncio.run(scenario())
    rows = list_work(root, PROCESSOR_ID)
    assert [(row.receipts[0].name, row.state) for row in rows] == [
        ("001-a.txt", "DONE"),
        ("002-b.txt", "DONE"),
    ]
    assert (root / "calls").read_text().split() == [row.work_key for row in rows]
    assert not signal_path.exists()
    assert list((root / "inbox").iterdir()) == []


RETRY_HANDLER = """
from dsl41.file_processing import RetrySafe


class Handler:
    def process(self, item):
        return RetrySafe("downstream unavailable")


def make(root):
    return Handler()
"""


def test_dl417_a_run_without_progress_starts_no_second_cycle(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-417: a handler that always answers RetrySafe makes one processor run after the
    delivery. REARM sees READY work and no progress, publishes no signal, and re-arms the
    watch only; with the signal gone nothing can start a second cycle."""
    site = short_root / "site"
    (site / "fp_retry").mkdir(parents=True)
    (site / "fp_retry" / "__init__.py").write_text("")
    (site / "fp_retry" / "handler.py").write_text(RETRY_HANDLER)
    monkeypatch.setenv("PYTHONPATH", str(site))
    monkeypatch.chdir(ROOT)  # REARM imports the example from the checkout
    root, run_root = short_root / "root", short_root / "run"
    sock, signal_path = run_root / "control.sock", root / "ready.signal"
    (root / "inbox").mkdir(parents=True)
    (root / "inbox" / "001-a.txt").write_bytes(b"one\n")
    signal_path.write_text("ready\n")
    handler = "fp_retry.handler:make"  # a dotted module: a bare `name:` reads as a JIL key
    jil = estate(root, signal_path, Path(sys.executable), "m1", handler=handler, rearm=sock)

    _one_cycle_and_no_second(jil.to_jil(), run_root, sock, signal_path)
    (row,) = list_work(root, PROCESSOR_ID)
    assert (row.state, row.attempt) == ("READY", 1)


def _one_cycle_and_no_second(jil: str, run_root: Path, sock: Path, signal_path: Path) -> None:
    """Start the watch once; await REARM's run and the watch it starts. REARM decided before
    its STARTJOB, and with no signal the watch cannot fire again."""

    async def scenario() -> None:
        clock = RealClock()
        engine = start_run(
            lower_source(jil),
            run_root,
            clock=clock,
            adapters={"CMD": LocalCommandAdapter(), "FW": FileWatcherAdapter()},
            hold_open=True,
        )
        journal = engine.journal
        assert journal is not None
        async with contextlib.AsyncExitStack() as teardown:
            teardown.callback(journal.close)
            teardown.push_async_callback(engine.shutdown)
            server = ControlServer(engine, sock)
            await server.start()
            teardown.push_async_callback(server.close)
            loop_task = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
            teardown.push_async_callback(_cancel, loop_task)
            store = engine.oracle.store.job
            engine.inject(Event(at=clock.now(), kind="STARTJOB", payload={"job": "INBOX_FW"}))
            await _until(lambda: _successes(engine, "REARM") == 1, "the rearm", loop_task)
            await _until(
                lambda: store["INBOX_FW"].run_number == 2 and store["INBOX_FW"].status == "RUNNING",
                "the second watch",
                loop_task,
            )
            assert not signal_path.exists()
            assert store["PROCESSOR"].status == "FAILURE" and store["PROCESSOR"].run_number == 1
            assert _successes(engine, "ARCHIVER") == 1

    asyncio.run(scenario())


class _Stop(BaseException):
    """Ends a process() call at its claim, as a killed processor does."""


def _stop_at_claim(name: str) -> None:
    if name == "claimed":
        raise _Stop


def test_dl417_a_stale_claim_the_processor_cannot_reach_starts_no_second_cycle(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-417: a RUNNING claim REARM has already seen, behind a handler that cannot load,
    gives one processor run (exit 2, before any claim) and no second cycle."""
    monkeypatch.chdir(ROOT)  # REARM imports the example from the checkout
    root, run_root = short_root / "root", short_root / "run"
    sock, signal_path = run_root / "control.sock", root / "ready.signal"
    (root / "inbox").mkdir(parents=True)
    (root / "inbox" / "001-a.txt").write_bytes(b"one\n")
    archive(root, PROCESSOR_ID)
    with pytest.raises(_Stop):
        process(root, PROCESSOR_ID, counts_handler(root), failpoint=_stop_at_claim)
    assert pending(root, PROCESSOR_ID)  # the REARM that saw the new claim republished
    signal_path.write_text("ready\n")
    handler = "fp_missing.handler:make"  # no such module: the worker exits 2
    jil = estate(root, signal_path, Path(sys.executable), "m1", handler=handler, rearm=sock)
    _one_cycle_and_no_second(jil.to_jil(), run_root, sock, signal_path)
    (row,) = list_work(root, PROCESSOR_ID)
    assert (row.state, row.attempt) == ("RUNNING", 1)
    assert (run_root / "logs" / "PROCESSOR.1.err").read_text().count("fp_missing") >= 1
