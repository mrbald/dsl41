"""PR-36: SPAWN idempotency that outlives the supervisor (period-model ss11a,
DL-129).

The dedup key was an entry in `self.runs`, so a delayed duplicate SPAWN became
a second execution the moment LIST evicted the run. The store is the run
DIRECTORY now -- a `.by_run_id` index entry, a `receipt.json` written before
the fork, and the `reply.json` that was the answer as first given -- and every
row of ss11a's answer table is a crash point here.

Two kinds of test, deliberately:

- the crash matrix drives `Supervisor.spawn_run` IN PROCESS and stops it at a
  named write boundary (`_crash_point`), then asks a FRESH Supervisor over the
  same run root what the directory says. Killing a real process and hoping it
  died in the window would test the window less often than it tested the race;
- the wire tests drive a real supervisor subprocess, because the frozen
  envelope, the grammar refusal and survival across a supervisor RESTART are
  claims about the protocol, not about a Python object.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import sys
import tempfile
from pathlib import Path

import pytest

if not sys.platform.startswith(("linux", "darwin")):  # pragma: no cover
    pytest.skip("supervisor tier is POSIX-only", allow_module_level=True)

from test_runner_supervisor import RawClient, start_supervisor, teardown_supervisor, wait_for

from dsl41 import runner_supervisor
from dsl41.canon import ARTIFACT_FORMAT_VERSION, canonical_bytes, decode

RUN_ID = "8a4f1e2c-3b5d-4e6f-9a1b-2c3d4e5f6a7b"
OTHER_RUN_ID = "1b2c3d4e-5f6a-4b8c-8d9e-0f1a2b3c4d5e"


class _Boom(RuntimeError):
    """The crash the matrix injects, at one named write boundary."""


def _spec(root: Path, job: str, run_number: int, command: str, *, run_id: str = RUN_ID) -> dict:
    """A frozen ss2 wrapper input spec, minus lifeline_fd (the supervisor's)."""
    run_dir = root / "runs" / f"{job}.{run_number}"
    return {
        "version": 1,
        "run_id": run_id,
        "job": job,
        "run_number": run_number,
        "command": command,
        "run_dir": str(run_dir),
        "stdout_path": str(root / "logs" / f"{job}.{run_number}.out"),
        "stderr_path": str(root / "logs" / f"{job}.{run_number}.err"),
        "stdin_path": None,
        "grace_seconds": 2.0,
    }


@pytest.fixture
def sup_root():
    """A short base dir: pytest's tmp_path can exceed sun_path's 104-byte
    macOS limit once supervisor.sock is appended (the same workaround
    test_runner_supervisor.py's fixture makes, and the reason this one is not
    imported from there -- an imported fixture shadows the name every test
    below then takes as a parameter)."""
    directory = tempfile.mkdtemp(prefix="dsl41i-", dir="/tmp")
    try:
        yield Path(directory)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def sups(sup_root: Path):
    """A factory for in-process Supervisors over one run root, with every fd
    and forked wrapper cleaned up afterwards. Each call is a fresh
    INCARNATION: empty memory, the same directory."""
    (sup_root / "runs").mkdir(parents=True, exist_ok=True)
    (sup_root / "logs").mkdir(exist_ok=True)
    made: list[runner_supervisor.Supervisor] = []

    def make() -> runner_supervisor.Supervisor:
        sup = runner_supervisor.Supervisor(str(sup_root))
        made.append(sup)
        return sup

    yield make
    for sup in made:
        for run in sup.runs.values():
            try:
                os.close(run.lifeline_w)
            except OSError:
                pass
            try:
                os.waitpid(run.wrapper_pid, 0)
            except (ChildProcessError, OSError):
                pass
        for fd in (sup._chld_r, sup._chld_w):
            try:
                os.close(fd)
            except OSError:
                pass
        sup._sel.close()  # the kqueue/epoll fd __init__ opens


def crash_at(sup: runner_supervisor.Supervisor, stage: str) -> None:
    """Stop this supervisor the instant `stage` is reached."""

    def hook(name: str) -> None:
        if name == stage:
            raise _Boom(stage)

    sup._crash_point = hook  # type: ignore[method-assign]


def _index(root: Path, run_id: str = RUN_ID) -> Path:
    return root / "runs" / ".by_run_id" / run_id


# ----------------------------------------------------------- the write order


def test_pr36_the_write_order_is_mkdir_index_receipt_spawn_reply(sup_root: Path, sups) -> None:
    """ss11a step by step. Index BEFORE receipt: the frozen idempotency key is
    `run_id` and every later lookup goes through the index, so the first
    durable thing that names the run must be the index. Draft 7 wrote the
    receipt first, and a crash between the two left a receipt nothing could
    find."""
    root = sup_root
    spec = _spec(root, "j", 1, "true")
    seen: list[tuple[str, bool, bool, bool]] = []

    sup = sups()

    def hook(name: str) -> None:
        run_dir = root / "runs" / "j.1"
        seen.append(
            (
                name,
                _index(root).exists(),
                (run_dir / "receipt.json").exists(),
                (run_dir / "reply.json").exists(),
            )
        )

    sup._crash_point = hook  # type: ignore[method-assign]
    reply = sup.spawn_run(spec)
    assert reply["ok"] is True and "duplicate" not in reply

    assert seen == [
        ("after_mkdir", False, False, False),
        ("after_index", True, False, False),
        ("after_receipt", True, True, False),
        ("after_spawn", True, True, False),
        ("after_reply", True, True, True),
    ]
    assert (root / "runs" / "j.1").is_dir()


def test_pr36_the_three_files_are_canonical_and_versioned(sup_root: Path, sups) -> None:
    """ss3.2-canonical bytes, and the shared `artifact_format_version` (the
    ss15 amendment): the index entry, the receipt and the reply are read by
    audit, so their bytes are the canonical form and nothing else."""
    root = sup_root
    spec = _spec(root, "j", 1, "true")
    reply = sups().spawn_run(spec)

    files = {
        "index": _index(root),
        "receipt": root / "runs" / "j.1" / "receipt.json",
        "reply": root / "runs" / "j.1" / "reply.json",
    }
    for name, path in files.items():
        raw = path.read_bytes()
        record = decode(raw)
        assert isinstance(record, dict)
        assert canonical_bytes(record) == raw, f"{name} is not canonical"
        assert record["artifact_format_version"] == ARTIFACT_FORMAT_VERSION
        assert record["run_id"] == RUN_ID

    assert decode(files["index"].read_bytes()) == {
        "artifact_format_version": ARTIFACT_FORMAT_VERSION,
        "job": "j",
        "run_id": RUN_ID,
        "run_number": 1,
    }
    receipt = decode(files["receipt"].read_bytes())
    assert isinstance(receipt, dict)
    assert receipt["spec_fingerprint"].startswith("sha256:")
    assert receipt["spec_fingerprint"] == runner_supervisor.spec_fingerprint(spec)
    # lifeline_fd is OURS to fill, so it is out of the fingerprint: a retry
    # that carried one must not read as a different spec
    assert (
        runner_supervisor.spec_fingerprint({**spec, "lifeline_fd": 3})
        == receipt["spec_fingerprint"]
    )
    assert decode(files["reply"].read_bytes()) == {
        "artifact_format_version": ARTIFACT_FORMAT_VERSION,
        "run_id": RUN_ID,
        "spawned_at": reply["spawned_at"],
        "wrapper_pid": reply["wrapper_pid"],
    }


# --------------------------------------------------------- the answer table


def test_pr36_crash_after_mkdir_is_a_first_application(sup_root: Path, sups) -> None:
    """ss11a's last row: an orphan directory with no index and no receipt is
    REUSED, because nothing durable names its run. A crash between `mkdir` and
    the index has made no promise, and the retry's own path is the first
    application -- one process."""
    root = sup_root
    spec = _spec(root, "j", 1, "true")
    first = sups()
    crash_at(first, "after_mkdir")
    with pytest.raises(_Boom):
        first.spawn_run(spec)
    assert (root / "runs" / "j.1").is_dir() and not _index(root).exists()

    reply = sups().spawn_run(spec)
    assert reply["ok"] is True and "duplicate" not in reply
    assert _index(root).exists()


def test_pr36_crash_after_the_index_is_indeterminate(sup_root: Path, sups) -> None:
    """Index, no receipt: the crash landed between the two. Nothing may
    re-spawn -- the engine's E7 policy decides the run."""
    root = sup_root
    spec = _spec(root, "j", 1, "true")
    first = sups()
    crash_at(first, "after_index")
    with pytest.raises(_Boom):
        first.spawn_run(spec)
    assert _index(root).exists()
    assert not (root / "runs" / "j.1" / "receipt.json").exists()

    reply = sups().spawn_run(spec)
    assert reply["ok"] is False and reply["error"] == "indeterminate"
    assert not (root / "runs" / "j.1" / "spawn.json").exists()


def test_pr36_crash_after_the_receipt_is_indeterminate(sup_root: Path, sups) -> None:
    """Receipt, no spawn record, nothing alive: the crash landed between the
    receipt and the fork. Writing the receipt BEFORE the spawn is the safe
    direction -- a run that never happened is reported unknown (E7 handles
    that); the other order lets a retry spawn twice, which nothing handles."""
    root = sup_root
    spec = _spec(root, "j", 1, "true")
    first = sups()
    crash_at(first, "after_receipt")
    with pytest.raises(_Boom):
        first.spawn_run(spec)

    reply = sups().spawn_run(spec)
    assert reply["ok"] is False and reply["error"] == "indeterminate"
    assert not (root / "runs" / "j.1" / "spawn.json").exists()


def test_pr36_crash_before_the_reply_answers_from_the_wrappers_own_record(
    sup_root: Path, sups
) -> None:
    """No `reply.json`, but the wrapper wrote `spawn.json`: the duplicate is
    reconstructed from it -- `wrapper_pid`, and `spawned_at := started_at`.
    Equivalent, and the protocol says so rather than promising bytes it did
    not keep."""
    root = sup_root
    spec = _spec(root, "j", 1, "sleep 30")
    first = sups()
    crash_at(first, "after_spawn")
    with pytest.raises(_Boom):
        first.spawn_run(spec)
    record = root / "runs" / "j.1" / "spawn.json"
    wait_for(record.exists)
    spawned = json.loads(record.read_text())
    assert not (root / "runs" / "j.1" / "reply.json").exists()

    reply = sups().spawn_run(spec)
    assert reply == {
        "ok": True,
        "run_id": RUN_ID,
        "wrapper_pid": spawned["wrapper_pid"],
        "spawned_at": spawned["started_at"],
        "duplicate": True,
    }
    os.kill(spawned["command_pid"], signal.SIGKILL)


def test_pr36_receipt_with_no_record_and_a_live_wrapper_is_in_progress(
    sup_root: Path, sups
) -> None:
    """The one row memory answers, and only for liveness: a receipt is
    durable, the fork happened, and this incarnation still holds the lifeline
    -- so there is no second spawn and no answer yet. A wrapper from an
    earlier incarnation cannot be alive (its lifeline EOF'd with its
    supervisor), which is why the check is sound."""
    root = sup_root
    spec = _spec(root, "j", 1, "sleep 30")
    sup = sups()
    crash_at(sup, "after_reply")  # the run is registered; the reply is written
    with pytest.raises(_Boom):
        sup.spawn_run(spec)
    (root / "runs" / "j.1" / "reply.json").unlink()  # a reply write that failed
    record = root / "runs" / "j.1" / "spawn.json"
    wait_for(record.exists)
    spawned = json.loads(record.read_text())
    record.unlink()  # ... before the wrapper's own record landed

    reply = sup.spawn_run(spec)  # the SAME incarnation: the wrapper is alive
    assert reply["ok"] is False and reply["error"] == "in_progress"
    os.kill(spawned["command_pid"], signal.SIGKILL)


def test_pr36_an_index_naming_a_missing_directory_is_indeterminate(sup_root: Path, sups) -> None:
    """Impossible by write order (`mkdir` precedes the index), and treated as
    indeterminate if ever seen -- never as a fresh spawn."""
    root = sup_root
    spec = _spec(root, "j", 1, "true")
    sups().spawn_run(spec)
    for name in sorted((root / "runs" / "j.1").iterdir()):
        name.unlink()
    (root / "runs" / "j.1").rmdir()

    reply = sups().spawn_run(spec)
    assert reply["ok"] is False and reply["error"] == "indeterminate"


def test_pr36_a_different_fingerprint_at_the_same_run_id_is_a_collision(
    sup_root: Path, sups
) -> None:
    """`receipt.json` with a different `spec_fingerprint`: two different specs
    are claiming one identity, and there is no correct pick between them."""
    root = sup_root
    sups().spawn_run(_spec(root, "j", 1, "true"))
    reply = sups().spawn_run(_spec(root, "j", 1, "rm -rf /"))
    assert reply["ok"] is False and reply["error"] == "collision"


def test_pr36_the_same_run_id_against_another_job_is_a_collision(sup_root: Path, sups) -> None:
    """Ownership is one-to-one in BOTH directions: one run_id maps to one
    (job, run_number). The index says which, and it is the index that
    answers -- not the incoming path, which is where this SPAWN would
    otherwise have found an empty slot and spawned."""
    root = sup_root
    sups().spawn_run(_spec(root, "j", 1, "true"))
    reply = sups().spawn_run(_spec(root, "other", 7, "true"))
    assert reply["ok"] is False and reply["error"] == "collision"
    assert not (root / "runs" / "other.7").exists()


def test_pr36_another_run_id_at_the_same_path_is_a_collision(sup_root: Path, sups) -> None:
    """The other direction: a directory that already carries a receipt for a
    DIFFERENT run_id is never reused and never given a second index."""
    root = sup_root
    sups().spawn_run(_spec(root, "j", 1, "true"))
    reply = sups().spawn_run(_spec(root, "j", 1, "true", run_id=OTHER_RUN_ID))
    assert reply["ok"] is False and reply["error"] == "collision"
    assert not _index(root, OTHER_RUN_ID).exists()


def test_pr36_an_orphan_directory_indexed_under_another_run_id_is_a_collision(
    sup_root: Path, sups
) -> None:
    """The same collision one crash earlier: the path's receipt is not there
    yet, so the only thing that names its owner is the index. Reusing the
    directory would put two run_ids on one tombstone."""
    root = sup_root
    first = sups()
    crash_at(first, "after_index")
    with pytest.raises(_Boom):
        first.spawn_run(_spec(root, "j", 1, "true"))
    assert not (root / "runs" / "j.1" / "receipt.json").exists()

    reply = sups().spawn_run(_spec(root, "j", 1, "true", run_id=OTHER_RUN_ID))
    assert reply["ok"] is False and reply["error"] == "collision"


def test_pr36_a_replay_resolves_through_the_index_not_the_incoming_path(
    sup_root: Path, sups
) -> None:
    """The frozen duplicate envelope, from a supervisor whose memory never
    held this run: `{ok, run_id, wrapper_pid, spawned_at, duplicate}` out of
    `reply.json`, and nothing re-spawned."""
    root = sup_root
    spec = _spec(root, "j", 1, "sleep 30")
    first = sups().spawn_run(spec)
    record = root / "runs" / "j.1" / "spawn.json"
    wait_for(record.exists)
    command_pid = json.loads(record.read_text())["command_pid"]
    before = record.read_bytes()

    fresh = sups()
    assert fresh.runs == {}  # the entry is not in THIS supervisor's memory
    again = fresh.spawn_run(spec)
    assert again == {
        "ok": True,
        "run_id": RUN_ID,
        "wrapper_pid": first["wrapper_pid"],
        "spawned_at": first["spawned_at"],
        "duplicate": True,
    }
    assert record.read_bytes() == before  # one wrapper, one record
    os.kill(command_pid, signal.SIGKILL)


def test_pr36_the_run_id_grammar_is_enforced_at_the_wire(sup_root: Path, sups) -> None:
    """ss11a: `run_id` names a directory entry here, so it is refused BEFORE
    anything is created -- the supervisor accepted any string until DL-129."""
    root = sup_root
    for bad in ("", "../escape", "not-a-uuid", RUN_ID.upper(), RUN_ID + "x"):
        spec = _spec(root, "j", 1, "true", run_id=bad)
        reply = sups().spawn_run(spec)
        assert reply["ok"] is False and reply["error"] == "bad_run_id", bad
    assert not (root / "runs" / "j.1").exists()
    assert not (root / "runs" / ".by_run_id").exists()


def test_pr36_the_wire_spec_must_name_the_directory_the_supervisor_owns(
    sup_root: Path, sups
) -> None:
    """The supervisor creates the run directory now, so it insists on the one
    it owns: index -> (job, run_number) -> directory is the only route a
    replay has back to the tombstone."""
    root = sup_root
    spec = {**_spec(root, "j", 1, "true"), "run_dir": str(root / "elsewhere")}
    reply = sups().spawn_run(spec)
    assert reply["ok"] is False and reply["error"] == "bad_spec"
    assert not (root / "elsewhere").exists()


def test_pr36_list_evicts_completed_runs_and_idempotency_does_not_notice(
    sup_root: Path, sups, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two halves of ss11a's premise. LIST keeps a bounded window of
    completions -- an estate whose root never rolls would otherwise grow it
    without limit -- and the runs that fall out of it still answer, because
    the store was never this dict."""
    monkeypatch.setattr(runner_supervisor, "_LIST_COMPLETED_WINDOW", 1)
    sup = sups()
    ids = [f"8a4f1e2c-3b5d-4e6f-9a1b-2c3d4e5f6a7{n}" for n in (1, 2, 3)]
    first = [sup.spawn_run(_spec(sup_root, "j", n, "true", run_id=ids[n - 1])) for n in (1, 2, 3)]
    wait_for(
        lambda: all((sup_root / "runs" / f"j.{n}" / "status.json").exists() for n in (1, 2, 3))
    )

    for run_id in ids:  # what _reap does once each wrapper is collected
        run = sup.runs[run_id]
        run.wrapper_rc = 0
        os.close(run.lifeline_w)
        with contextlib.suppress(ChildProcessError, OSError):
            os.waitpid(run.wrapper_pid, 0)
        sup._evict_completed(run)
    assert list(sup.runs) == [ids[-1]], "only the window's worth of completions stays"

    for index, run_id in enumerate(ids, start=1):
        again = sup.spawn_run(_spec(sup_root, "j", index, "true", run_id=run_id))
        assert again["duplicate"] is True
        assert again["wrapper_pid"] == first[index - 1]["wrapper_pid"]


def test_pr36_two_spellings_of_one_run_root_are_one_run_root(sup_root: Path, sups) -> None:
    """The engine and the supervisor are TOLD the run root separately, so one
    may hold `/tmp/r` where the other holds `/private/tmp/r`, or a relative
    path against an absolute one. Comparing the spellings would refuse every
    SPAWN on the host while the estate is perfectly healthy."""
    link = sup_root.parent / (sup_root.name + "-link")
    link.symlink_to(sup_root)
    try:
        sup = runner_supervisor.Supervisor(str(link))
        try:
            reply = sup.spawn_run(_spec(sup_root, "j", 1, "true"))
            assert reply["ok"] is True, reply
            assert (sup_root / "runs" / "j.1" / "receipt.json").exists()
            for run in sup.runs.values():
                os.close(run.lifeline_w)
                with contextlib.suppress(ChildProcessError, OSError):
                    os.waitpid(run.wrapper_pid, 0)
        finally:
            os.close(sup._chld_r)
            os.close(sup._chld_w)
            sup._sel.close()
    finally:
        link.unlink()


def test_pr36_a_directory_from_the_old_ownership_rule_is_never_reused(sup_root: Path, sups) -> None:
    """The orphan row assumes only this protocol ever created directories. A
    directory made under the OLD rule -- engine-owned, no receipt -- holds a
    run's evidence, and forking into it would overwrite that run's records
    with a second run's."""
    old_dir = sup_root / "runs" / "j.1"
    old_dir.mkdir(parents=True)
    (old_dir / "spawn.json").write_text(json.dumps({"version": 1, "run_id": OTHER_RUN_ID}))

    reply = sups().spawn_run(_spec(sup_root, "j", 1, "true"))
    assert reply["ok"] is False and reply["error"] == "indeterminate"
    assert not (old_dir / "receipt.json").exists()


def test_pr36_an_undurable_index_temp_file_does_not_refuse_a_first_application(
    sup_root: Path, sups
) -> None:
    """`durable_write` leaves `.<name>.<pid>.tmp` behind if it dies between
    the fsync and the rename: a complete record that was never durable. The
    reverse scan must not read one, or a run that never spawned is refused
    where §11a says "first application"."""
    index_dir = sup_root / "runs" / ".by_run_id"
    index_dir.mkdir(parents=True)
    (index_dir / f".{OTHER_RUN_ID}.999.tmp").write_bytes(
        canonical_bytes(
            {
                "artifact_format_version": ARTIFACT_FORMAT_VERSION,
                "job": "j",
                "run_id": OTHER_RUN_ID,
                "run_number": 1,
            }
        )
    )
    (sup_root / "runs" / "j.1").mkdir()  # the orphan the crash left

    reply = sups().spawn_run(_spec(sup_root, "j", 1, "true"))
    assert reply["ok"] is True and "duplicate" not in reply


def test_pr36_a_spawn_record_naming_another_run_is_never_the_answer(sup_root: Path, sups) -> None:
    """The rule `_signal_command` already applies to this file: a `spawn.json`
    that does not name this run is spoofed, stale or foreign. Building the
    duplicate envelope from it would hand the engine another run's pid, which
    it would then journal as this run's dispatch."""
    spec = _spec(sup_root, "j", 1, "sleep 30")
    first = sups()
    crash_at(first, "after_spawn")  # no reply.json is written
    with pytest.raises(_Boom):
        first.spawn_run(spec)
    record = sup_root / "runs" / "j.1" / "spawn.json"
    wait_for(record.exists)
    honest = json.loads(record.read_text())
    record.write_text(json.dumps({**honest, "run_id": OTHER_RUN_ID, "wrapper_pid": 4242}))

    reply = sups().spawn_run(spec)
    assert reply["ok"] is False and reply["error"] == "indeterminate"
    os.kill(honest["command_pid"], signal.SIGKILL)


# ------------------------------------------------------------- over the wire


def test_pr36_the_supervisor_creates_the_detached_run_directory(sup_root: Path) -> None:
    """Ownership moved (ss11a): the engine no longer makes the directory
    before it sends SPAWN. Otherwise the engine creates it, dies before
    sending, and the retry reads "directory exists, no receipt" --
    indeterminate -- for a run that provably never reached the supervisor."""
    proc = start_supervisor(sup_root)
    cli = RawClient(sup_root)
    try:
        tok = cli.send({"v": 1, "cmd": "ACQUIRE", "controller_id": "A", "ttl_s": 60})["token"]
        run_dir = sup_root / "runs" / "made.1"
        assert not run_dir.exists()
        spawned = cli.send(
            {"v": 1, "cmd": "SPAWN", "token": tok, "spec": _spec(sup_root, "made", 1, "true")}
        )
        assert spawned["ok"] is True
        assert run_dir.is_dir()
        assert (run_dir / "receipt.json").exists() and (run_dir / "reply.json").exists()
        assert _index(sup_root).exists()
    finally:
        cli.close()
        teardown_supervisor(sup_root, proc)


def test_pr36_the_wire_refuses_a_freehand_run_id(sup_root: Path) -> None:
    proc = start_supervisor(sup_root)
    cli = RawClient(sup_root)
    try:
        tok = cli.send({"v": 1, "cmd": "ACQUIRE", "controller_id": "A", "ttl_s": 60})["token"]
        spec = _spec(sup_root, "j", 1, "true", run_id="j.1")
        reply = cli.send({"v": 1, "cmd": "SPAWN", "token": tok, "spec": spec})
        assert reply["ok"] is False and reply["error"] == "bad_run_id"
        assert not (sup_root / "runs" / "j.1").exists()
    finally:
        cli.close()
        teardown_supervisor(sup_root, proc)


def test_pr36_the_wire_duplicate_envelope_is_frozen(sup_root: Path) -> None:
    proc = start_supervisor(sup_root)
    cli = RawClient(sup_root)
    try:
        tok = cli.send({"v": 1, "cmd": "ACQUIRE", "controller_id": "A", "ttl_s": 60})["token"]
        spec = _spec(sup_root, "j", 1, "sleep 30")
        first = cli.send({"v": 1, "cmd": "SPAWN", "token": tok, "spec": spec})
        again = cli.send({"v": 1, "cmd": "SPAWN", "token": tok, "spec": spec})
        assert set(again) == {"ok", "run_id", "wrapper_pid", "spawned_at", "duplicate"}
        assert again["duplicate"] is True
        assert again["wrapper_pid"] == first["wrapper_pid"]
        assert again["spawned_at"] == first["spawned_at"]
    finally:
        cli.close()
        teardown_supervisor(sup_root, proc)


def test_pr36_a_collision_is_refused_over_the_wire(sup_root: Path) -> None:
    """Both directions, on the socket: one run_id against a second
    (job, run_number), and a second run_id against one path."""
    proc = start_supervisor(sup_root)
    cli = RawClient(sup_root)
    try:
        tok = cli.send({"v": 1, "cmd": "ACQUIRE", "controller_id": "A", "ttl_s": 60})["token"]
        cli.send(
            {"v": 1, "cmd": "SPAWN", "token": tok, "spec": _spec(sup_root, "j", 1, "sleep 30")}
        )
        moved = cli.send(
            {"v": 1, "cmd": "SPAWN", "token": tok, "spec": _spec(sup_root, "j", 2, "sleep 30")}
        )
        assert moved["ok"] is False and moved["error"] == "collision"
        assert not (sup_root / "runs" / "j.2").exists()

        other = cli.send(
            {
                "v": 1,
                "cmd": "SPAWN",
                "token": tok,
                "spec": _spec(sup_root, "j", 1, "sleep 30", run_id=OTHER_RUN_ID),
            }
        )
        assert other["ok"] is False and other["error"] == "collision"
        assert not _index(sup_root, OTHER_RUN_ID).exists()
    finally:
        cli.close()
        teardown_supervisor(sup_root, proc)


def test_pr36_idempotency_survives_a_supervisor_restart(sup_root: Path) -> None:
    """The whole point of the unit. The entry is gone -- the process that held
    it is gone -- and the directory answers: the same wrapper_pid, the same
    spawned_at, `duplicate: true`, and no second wrapper."""
    proc = start_supervisor(sup_root)
    cli = RawClient(sup_root)
    spec = _spec(sup_root, "j", 1, "sleep 30")
    try:
        tok = cli.send({"v": 1, "cmd": "ACQUIRE", "controller_id": "A", "ttl_s": 60})["token"]
        first = cli.send({"v": 1, "cmd": "SPAWN", "token": tok, "spec": spec})
        record = sup_root / "runs" / "j.1" / "spawn.json"
        wait_for(record.exists)
        before = record.read_bytes()
    finally:
        cli.close()
    proc.send_signal(signal.SIGKILL)  # no orderly shutdown: the world vanishes
    proc.wait()

    proc2 = start_supervisor(sup_root)
    cli2 = RawClient(sup_root)
    try:
        listing = cli2.send({"v": 1, "cmd": "LIST"})
        assert listing["runs"] == []  # a restart's memory is empty by definition
        tok2 = cli2.send({"v": 1, "cmd": "ACQUIRE", "controller_id": "B", "ttl_s": 60})["token"]
        again = cli2.send({"v": 1, "cmd": "SPAWN", "token": tok2, "spec": spec})
        assert again == {
            "ok": True,
            "run_id": RUN_ID,
            "wrapper_pid": first["wrapper_pid"],
            "spawned_at": first["spawned_at"],
            "duplicate": True,
        }
        assert record.read_bytes() == before
        assert cli2.send({"v": 1, "cmd": "LIST"})["runs"] == []  # nothing was spawned
    finally:
        cli2.close()
        teardown_supervisor(sup_root, proc2)


def test_pr36_a_run_directory_is_never_pruned_while_its_effect_can_replay(
    sup_root: Path, sups
) -> None:
    """ss11a's retention floor, stated as the hazard it guards: "no index
    entry" IS "first application", so deleting an index entry or a run
    directory AUTHORIZES a spawn. The floor is a safety rule, not
    housekeeping."""
    root = sup_root
    spec = _spec(root, "j", 1, "true")
    sups().spawn_run(spec)
    wait_for(lambda: (root / "runs" / "j.1" / "status.json").exists())
    assert sups().spawn_run(spec)["duplicate"] is True

    _index(root).unlink()  # the pruning ss11a forbids
    for entry in sorted((root / "runs" / "j.1").iterdir()):
        entry.unlink()
    (root / "runs" / "j.1").rmdir()
    replayed = sups().spawn_run(spec)
    assert replayed["ok"] is True and "duplicate" not in replayed  # a SECOND run


def test_pr36_a_field_named_digest_is_part_of_the_fingerprint(sup_root: Path) -> None:
    """The fingerprint covers the WHOLE spec. `canon.digest` strips a
    top-level `digest` key by design; a fingerprint built through it would
    let two specs differing only in a field of that name share one, and a
    replay would answer duplicate for a spec it never received."""
    a = runner_supervisor.spec_fingerprint({"run_id": "x", "digest": "one"})
    b = runner_supervisor.spec_fingerprint({"run_id": "x", "digest": "two"})
    assert a != b


def test_pr36_a_corrupt_index_entry_is_indeterminate_not_absence(sup_root: Path, sups) -> None:
    """Corruption is not absence: "no index entry" AUTHORIZES a spawn, so an
    unreadable one reading as absent would turn one damaged file into a
    second execution of a run that already happened."""
    sup = sups()
    assert sup.spawn_run(_spec(sup_root, "a", 1, "true"))["ok"] is True
    _index(sup_root).write_bytes(b"{not json")
    again = sups().spawn_run(_spec(sup_root, "a", 1, "true"))
    assert again["ok"] is False and again["error"] == "indeterminate"
    # and in the orphan scan: an unreadable entry MIGHT claim the directory,
    # so reuse under it is refused rather than risked
    (sup_root / "runs" / "b.1").mkdir()
    other = sups().spawn_run(_spec(sup_root, "b", 1, "true", run_id=OTHER_RUN_ID))
    assert other["ok"] is False and other["error"] == "collision"


def test_pr36_a_reply_naming_another_run_is_never_the_answer(sup_root: Path, sups) -> None:
    """A reply.json at our path that names a different run_id is foreign --
    served as a duplicate it would hand the engine a stranger's pid. It
    falls through, and with no spawn record and nothing alive the truthful
    answer is indeterminate."""
    sup = sups()
    spec = _spec(sup_root, "a", 1, "true")
    fingerprint = runner_supervisor.spec_fingerprint(spec)
    run_dir = sup_root / "runs" / "a.1"
    run_dir.mkdir()
    _index(sup_root).parent.mkdir(parents=True, exist_ok=True)
    _write(
        _index(sup_root),
        {"artifact_format_version": 1, "job": "a", "run_id": RUN_ID, "run_number": 1},
    )
    _write(
        run_dir / "receipt.json",
        {
            "artifact_format_version": 1,
            "received_at": "x",
            "run_id": RUN_ID,
            "spec_fingerprint": fingerprint,
        },
    )
    _write(
        run_dir / "reply.json",
        {
            "artifact_format_version": 1,
            "run_id": OTHER_RUN_ID,
            "spawned_at": "y",
            "wrapper_pid": 4242,
        },
    )
    answer = sup.spawn_run(spec)
    assert answer["ok"] is False and answer["error"] == "indeterminate"
    assert "wrapper_pid" not in answer


def test_pr36_a_wrong_typed_spec_is_refused_before_anything_durable(sup_root: Path, sups) -> None:
    """The whole frozen ss2 schema, checked before mkdir. A field that only
    explodes after the fork (grace_seconds reaching float()) would kill the
    one process whose death EOFs every wrapper on the host; and an unknown
    key is a key whose type is not pinned, which is the one thing the
    fingerprint's typed float encoding cannot be injective over."""
    sup = sups()
    bad_type = _spec(sup_root, "a", 1, "true")
    bad_type["grace_seconds"] = "x"
    answer = sup.spawn_run(bad_type)
    assert answer["ok"] is False and answer["error"] == "bad_spec"
    unknown = _spec(sup_root, "a", 1, "true")
    unknown["surprise"] = "float:0x1.4p+3"
    answer = sup.spawn_run(unknown)
    assert answer["ok"] is False and answer["error"] == "bad_spec"
    assert not (sup_root / "runs" / "a.1").exists()
    assert not _index(sup_root).exists()
    # the supervisor is still standing and a good spec still spawns
    good = sup.spawn_run(_spec(sup_root, "a", 1, "true"))
    assert good["ok"] is True


def test_pr36_an_unreadable_index_file_is_never_absence(sup_root: Path, sups) -> None:
    """Absent means exactly ENOENT. An EACCES on the index read is a file
    that EXISTS and cannot be read -- reporting it absent would authorize a
    second fork of a run that already happened."""
    sup = sups()
    assert sup.spawn_run(_spec(sup_root, "a", 1, "true"))["ok"] is True
    _index(sup_root).chmod(0o000)
    try:
        again = sups().spawn_run(_spec(sup_root, "a", 1, "true"))
    finally:
        _index(sup_root).chmod(0o600)
    assert again["ok"] is False and again["error"] == "indeterminate"


def test_pr36_a_tombstone_the_canon_ingress_refuses_is_unreadable(sup_root: Path, sups) -> None:
    """The ss11a records are read through the ss3.2 ingress: duplicate keys
    and an artifact_format_version this binary does not implement both make
    the record unsupported evidence, not an answer -- `json.load` would have
    accepted either."""
    sup = sups()
    assert sup.spawn_run(_spec(sup_root, "a", 1, "true"))["ok"] is True
    original = _index(sup_root).read_bytes()

    _index(sup_root).write_bytes(b'{"run_id":"x","run_id":"y","job":"a","run_number":1}')
    dup = sups().spawn_run(_spec(sup_root, "a", 1, "true"))
    assert dup["ok"] is False and dup["error"] == "indeterminate"

    versioned = json.loads(original)
    versioned["artifact_format_version"] = 2
    _index(sup_root).write_bytes(json.dumps(versioned, sort_keys=True).encode())
    future = sups().spawn_run(_spec(sup_root, "a", 1, "true"))
    assert future["ok"] is False and future["error"] == "indeterminate"


def _write(path: Path, record: dict) -> None:
    path.write_bytes(canonical_bytes(record))


def test_pr36_the_grammar_matches_the_engines(sup_root: Path) -> None:
    """The supervisor cannot import `runner_effects` (DL-42), so the grammar
    is inlined -- two copies of one rule. They are pinned equal here, because
    a run_id the engine mints and the supervisor refuses is a run that cannot
    start."""
    from dsl41.runner_effects import RUN_ID_RE

    assert runner_supervisor._RUN_ID_RE.pattern == RUN_ID_RE.pattern


def test_pr36_a_wrapper_spawn_failure_leaves_a_receipt_and_no_answer(
    sup_root: Path, sups, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The receipt is written before the fork, so a fork that fails leaves the
    same evidence a crash there would: nothing may re-spawn."""
    root = sup_root
    spec = _spec(root, "j", 1, "true")
    sup = sups()

    def boom(_spec_arg: dict) -> tuple[int, int]:
        raise OSError("EAGAIN")

    monkeypatch.setattr(sup, "_spawn_wrapper", boom)
    reply = sup.spawn_run(spec)
    assert reply["ok"] is False and reply["error"].startswith("spawn_failed")
    assert (root / "runs" / "j.1" / "receipt.json").exists()
    assert sups().spawn_run(spec)["error"] == "indeterminate"


class _StubClient:
    """The supervisor half of the detached adapter, reduced to what one SPAWN
    touches. It records the world AS THE SUPERVISOR WOULD SEE IT and then
    stops the adapter, which is the only moment the ownership question has an
    answer."""

    def __init__(self, refusal: Exception) -> None:
        self.refusal = refusal
        self.seen_run_dir: str | None = None
        self.existed_at_spawn: bool | None = None
        self.forgotten: list[str] = []
        self.lost = asyncio.Event()

    def exit_future(self, run_id: str) -> asyncio.Future:
        return asyncio.get_running_loop().create_future()

    def forget_exit(self, run_id: str) -> None:
        self.forgotten.append(run_id)

    async def spawn(self, spec: dict) -> dict:
        self.seen_run_dir = spec["run_dir"]
        self.existed_at_spawn = Path(spec["run_dir"]).exists()
        raise self.refusal


def test_pr36_the_detached_adapter_sends_spawn_before_any_directory_exists(
    tmp_path: Path,
) -> None:
    """The ownership change where it is actually made (ss11a): by the time the
    engine sends SPAWN, the run directory does not exist -- the supervisor
    creates it on receipt. An engine that made it first could die before
    sending, and the retry's supervisor would read "directory exists, no
    receipt" for a run that provably never reached the host."""
    from dsl41.ir import lower_source
    from dsl41.runner_adapters import (
        AdapterContext,
        Failed,
        SupervisedCommandAdapter,
        SupervisorUnavailable,
    )
    from dsl41.runner_clock import RealClock

    catalog = lower_source("insert_job: j\njob_type: c\ncommand: true\n")
    (tmp_path / "runs").mkdir()
    client = _StubClient(SupervisorUnavailable("stop here"))
    adapter = SupervisedCommandAdapter(client)  # type: ignore[arg-type]
    ctx = AdapterContext(clock=RealClock(), run_root=tmp_path, run_id=RUN_ID)

    result = asyncio.run(adapter.run(catalog.jobs["j"], 1, ctx))
    assert isinstance(result, Failed)
    assert client.seen_run_dir == str(tmp_path / "runs" / "j.1")  # the wire names it
    assert client.existed_at_spawn is False, "the engine must not create it"
    assert not (tmp_path / "runs" / "j.1").exists()


def test_pr36_in_progress_is_not_a_completion(tmp_path: Path) -> None:
    """ss11a's `in_progress` says "no second spawn, and no answer yet". The
    wrapper is ALIVE, so failing the row here would report a completion for a
    running process -- the one thing the outcome channel may never do. The
    adapter waits for its outcome instead."""
    from dsl41.ir import lower_source
    from dsl41.runner_adapters import (
        AdapterContext,
        Failed,
        SpawnInProgress,
        SupervisedCommandAdapter,
        SupervisorUnavailable,
        outcome_from_status,
    )
    from dsl41.runner_clock import RealClock

    catalog = lower_source("insert_job: j\njob_type: c\ncommand: true\n")
    run_dir = tmp_path / "runs" / "j.1"
    run_dir.mkdir(parents=True)
    (run_dir / "status.json").write_text(
        json.dumps(
            {
                "version": 1,
                "run_id": RUN_ID,
                "job": "j",
                "run_number": 1,
                "outcome": "exited",
                "exit_code": 0,
                "ended_at": "2026-08-20T12:00:00+00:00",
            }
        )
    )
    client = _StubClient(SpawnInProgress("SPAWN refused: in_progress (...)"))
    adapter = SupervisedCommandAdapter(client)  # type: ignore[arg-type]
    ctx = AdapterContext(clock=RealClock(), run_root=tmp_path, run_id=RUN_ID)

    result = asyncio.run(adapter.run(catalog.jobs["j"], 1, ctx))
    assert result == outcome_from_status({"outcome": "exited", "exit_code": 0}) == 0

    # the discriminator: every OTHER refusal in the same setup still fails the
    # row, because for those the run is not running
    refused = SupervisedCommandAdapter(_StubClient(SupervisorUnavailable("collision")))  # type: ignore[arg-type]
    assert isinstance(asyncio.run(refused.run(catalog.jobs["j"], 1, ctx)), Failed)


def test_pr36_the_engine_no_longer_makes_the_detached_directory(tmp_path: Path) -> None:
    """The engine half of the ownership change (`_build_run_spec`): the
    tethered path still creates the directory it owns, and the detached path
    creates none."""
    from dsl41.ir import lower_source
    from dsl41.runner_adapters import AdapterContext, _build_run_spec
    from dsl41.runner_clock import RealClock

    catalog = lower_source("insert_job: j\njob_type: c\ncommand: true\n")
    job_ir = catalog.jobs["j"]
    ctx = AdapterContext(clock=RealClock(), run_root=tmp_path)
    run_dir, spec = _build_run_spec(job_ir, 1, ctx, grace_seconds=1.0, create_run_dir=False)
    assert not run_dir.exists()
    assert spec["run_dir"] == str(run_dir)  # the wire spec still names it

    tethered, _ = _build_run_spec(job_ir, 2, ctx, grace_seconds=1.0)
    assert tethered.is_dir()


# ------------------------------------------------ PR-36a: the engine replays


def _probe_adapter(*, hang: bool = False):
    """A SupervisedCommandAdapter in TYPE only: the resume ladder chooses the
    PR-36a replay by adapter type, and what this test pins is the CHOICE and
    the identity it rides on -- not the wire, which the in-process Supervisor
    tests above already own. It carries the wiring attributes the DL-130
    profile derivation reads, at the defaults, so the genesis pin and the
    resume wiring agree; the GENESIS probe hangs (a run mid-flight at the
    crash), the resume probe answers 0."""

    from dsl41.runner_adapters import SupervisedCommandAdapter

    class _Probe(SupervisedCommandAdapter):
        grace_seconds = 10.0
        settle_seconds = 5.0

        def __init__(self) -> None:  # no client: run() below never uses one
            self.calls: list[tuple[str, int, str | None]] = []

        async def run(self, job_ir, run_number, ctx):  # type: ignore[override]
            self.calls.append((job_ir.name, run_number, ctx.run_id))
            if hang:
                # park on the VIRTUAL clock, exactly as FakeAdapter does: a
                # real Event here never wakes and wedges run_until_quiescent
                from datetime import datetime as _dt

                await ctx.clock.sleep_until(_dt.max)
            return 0

    return _Probe()


def _crashed_run_root(tmp: Path):
    """A run root whose journal holds an applied, resolved, BOUND spawn --
    and whose spool holds nothing: the supervisor died before the fork."""
    import asyncio as _asyncio

    from dsl41.ir import lower_source
    from dsl41.oracle_state import Event
    from dsl41.runner_clock import VirtualClock
    from dsl41.runner_startup import start_run
    from datetime import datetime

    t0 = datetime(2026, 7, 1, 8, 0)
    catalog = lower_source("insert_job: j\njob_type: c\ncommand: x\n")
    engine = start_run(
        catalog,
        tmp / "run",
        clock=VirtualClock(start=t0),
        # the SAME adapter type the resume wires: the DL-130 profile is
        # derived from the wiring, and a genesis pinned tethered would
        # refuse the detached-typed resume as a new period
        adapters={"CMD": _probe_adapter(hang=True)},
    )

    async def scenario() -> None:
        engine.inject(Event(at=t0, kind="STARTJOB", payload={"job": "j"}))
        await engine.run_until_quiescent(t0)
        await engine.shutdown()

    _asyncio.run(scenario())
    assert engine.journal is not None
    engine.journal.close()
    [spawned] = [e for e in engine.outbox.effects() if e.kind == "SPAWN"]
    return catalog, t0, spawned.run_id


def _resume_with_probe(tmp: Path, catalog, t0):
    import asyncio as _asyncio
    from datetime import timedelta

    from dsl41.runner_startup import resume_run

    probe = _probe_adapter()

    async def scenario():
        from dsl41.runner_clock import VirtualClock

        engine = await resume_run(
            catalog,
            tmp / "run",
            clock=VirtualClock(start=t0 + timedelta(minutes=1)),
            adapters={"CMD": probe},
            settle_seconds=0.0,
            grace_seconds=0.0,
        )
        await engine.run_until_quiescent(t0 + timedelta(minutes=2))
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()
        return engine

    return probe, _asyncio.run(scenario())


def test_pr36a_resume_replays_a_bound_spawn_with_no_spool_evidence(tmp_path: Path) -> None:
    """The supervisor died after its mkdir: the directory exists, empty, and
    the WAL says the spawn was applied. The old ladder fabricated 'dispatch
    lost ... (never spawned)' FAILURE; ss11a makes the SPAWN idempotent, so
    resume REPLAYS it under the bound identity and the host answers."""
    catalog, t0, bound = _crashed_run_root(tmp_path)
    (tmp_path / "run" / "runs" / "j.1").mkdir(parents=True)  # mkdir happened, nothing else
    probe, engine = _resume_with_probe(tmp_path, catalog, t0)
    assert probe.calls == [("j", 1, bound)]
    assert engine.oracle.store.job["j"].status == "SUCCESS"  # the probe's exit 0
    assert not [e for e in engine.oracle.trace() if "dispatch lost" in str(getattr(e, "cause", ""))]


def test_pr36a_an_untraced_bound_spawn_replays_instead_of_failing(tmp_path: Path) -> None:
    """Even less on disk: no directory at all. The intent is durable and
    bound, so the supervisor's directory -- not this engine's guess -- says
    whether the run exists. The identity-less twin (a pre-DL-118 chain)
    keeps the loud FAILURE, which the ledger suite pins."""
    catalog, t0, bound = _crashed_run_root(tmp_path)
    probe, engine = _resume_with_probe(tmp_path, catalog, t0)
    assert probe.calls == [("j", 1, bound)]
    assert engine.oracle.store.job["j"].status == "SUCCESS"


def test_pr36a_a_supervisor_known_dead_run_with_no_local_trace_still_replays(
    tmp_path: Path,
) -> None:
    """S6c: candidates are the union of local evidence (dispatch records,
    the `runs/` listing) AND what the HOST says it is running. A run the
    supervisor still LISTs -- dead now -- but this local root never
    dispatched and never made a directory for gets a run_dir=None
    candidate purely from that LIST row, and `_spool_has_evidence(None)`
    must read that as "nothing to check", not crash on the missing
    directory (`_resume_untraced_starts` is a DIFFERENT ladder, for
    candidates absent everywhere including the supervisor)."""
    import asyncio as _asyncio
    from datetime import timedelta

    from dsl41.runner_clock import VirtualClock
    from dsl41.runner_startup import resume_run

    catalog, t0, bound = _crashed_run_root(tmp_path)

    class _DeadListing:
        async def list_runs(self) -> dict:
            return {"runs": [{"job": "j", "run_number": 1, "wrapper_alive": False, "run_id": bound}]}

    probe = _probe_adapter()

    async def scenario():
        engine = await resume_run(
            catalog,
            tmp_path / "run",
            clock=VirtualClock(start=t0 + timedelta(minutes=1)),
            adapters={"CMD": probe},
            supervisor=_DeadListing(),  # type: ignore[arg-type]
            settle_seconds=0.0,
            grace_seconds=0.0,
        )
        await engine.run_until_quiescent(t0 + timedelta(minutes=2))
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()
        return engine

    engine = _asyncio.run(scenario())
    assert probe.calls == [("j", 1, bound)]
    assert engine.oracle.store.job["j"].status == "SUCCESS"


def test_pr36_a_dead_duplicate_resolves_through_the_spool_not_a_wait(tmp_path: Path) -> None:
    """reply.json survived a crash its wrapper did not: a fresh supervisor
    answers duplicate, no exit push will ever come, and no record will ever
    appear. Awaiting would poll forever; the spool ladder judges the dead
    evidence instead -- here the wrapper HAD recorded, so the recorded exit
    is the answer."""
    import asyncio as _asyncio

    from dsl41.ir import lower_source
    from dsl41.runner_adapters import AdapterContext, SupervisedCommandAdapter
    from dsl41.runner_clock import VirtualClock
    from datetime import datetime

    rid = RUN_ID
    run_dir = tmp_path / "runs" / "j.1"
    run_dir.mkdir(parents=True)
    (tmp_path / "logs").mkdir()
    (run_dir / "status.json").write_text(
        json.dumps(
            {
                "version": 1,
                "run_id": rid,
                "job": "j",
                "run_number": 1,
                "outcome": "exited",
                "exit_code": 7,
                "ended_at": "2026-07-01T08:05:00+00:00",
            }
        )
    )

    class _Client:
        def exit_future(self, _run_id):  # registered before spawn
            return _asyncio.get_event_loop().create_future()

        def forget_exit(self, _run_id):
            pass

        async def spawn(self, spec):
            return {
                "ok": True,
                "run_id": spec["run_id"],
                "wrapper_pid": 4242,
                "spawned_at": "x",
                "duplicate": True,
            }

        async def list_runs(self):
            return {"runs": []}  # nothing alive anywhere

    adapter = SupervisedCommandAdapter(_Client(), grace_seconds=0.0, settle_seconds=0.0)  # type: ignore[arg-type]
    catalog = lower_source("insert_job: j\njob_type: c\ncommand: x\n")
    ctx = AdapterContext(
        clock=VirtualClock(start=datetime(2026, 7, 1, 8, 0)), run_root=tmp_path, run_id=rid
    )

    async def scenario():
        return await _asyncio.wait_for(adapter.run(catalog.jobs["j"], 1, ctx), timeout=10.0)

    assert _asyncio.run(scenario()) == 7


def test_pr36_an_index_entry_disowning_its_name_is_indeterminate(sup_root: Path, sups) -> None:
    """The entry at .by_run_id/<R1> whose run_id field says R2 is tampered or
    misfiled; believing either half would answer for a run the other half
    disowns. Replay refuses; the orphan scan refuses reuse the same way."""
    sup = sups()
    assert sup.spawn_run(_spec(sup_root, "a", 1, "true"))["ok"] is True
    _write(
        _index(sup_root),
        {
            "artifact_format_version": 1,
            "job": "a",
            "run_id": OTHER_RUN_ID,
            "run_number": 1,
        },
    )
    again = sups().spawn_run(_spec(sup_root, "a", 1, "true"))
    assert again["ok"] is False and again["error"] == "indeterminate"


def test_pr36_a_transient_list_failure_still_ends_in_the_spool(tmp_path: Path) -> None:
    """The duplicate-born wait re-asks LIST: a transient failure at the first
    ask must not turn into a forever-wait when the connection itself stays
    healthy and no push is ever coming. The re-check answers definitively
    "not alive" and the spool's recorded exit is the verdict."""
    import asyncio as _asyncio

    from dsl41.ir import lower_source
    from dsl41.runner_adapters import (
        AdapterContext,
        SupervisedCommandAdapter,
        SupervisorUnavailable,
    )
    from dsl41.runner_clock import VirtualClock
    from datetime import datetime

    rid = RUN_ID
    run_dir = tmp_path / "runs" / "j.1"
    run_dir.mkdir(parents=True)
    (tmp_path / "logs").mkdir()
    # NO status.json and none coming: only the LIST re-check can end this
    # wait, and the spool ladder then reports the truthful nothing

    class _Client:
        def __init__(self) -> None:
            self.lost = _asyncio.Event()
            self.asked = 0

        def exit_future(self, _run_id):
            return _asyncio.get_event_loop().create_future()

        def forget_exit(self, _run_id):
            pass

        async def reconnect(self):
            return True

        async def spawn(self, spec):
            return {
                "ok": True,
                "run_id": spec["run_id"],
                "wrapper_pid": 4242,
                "spawned_at": "x",
                "duplicate": True,
            }

        async def list_runs(self):
            self.asked += 1
            if self.asked == 1:
                raise SupervisorUnavailable("transient")  # the first ask fails
            return {"runs": []}

    client = _Client()
    adapter = SupervisedCommandAdapter(client, grace_seconds=0.0, settle_seconds=0.0)  # type: ignore[arg-type]
    catalog = lower_source("insert_job: j\njob_type: c\ncommand: x\n")
    ctx = AdapterContext(
        clock=VirtualClock(start=datetime(2026, 7, 1, 8, 0)), run_root=tmp_path, run_id=rid
    )

    async def scenario():
        return await _asyncio.wait_for(adapter.run(catalog.jobs["j"], 1, ctx), timeout=30.0)

    from dsl41.runner_adapters import Failed

    result = _asyncio.run(scenario())
    assert isinstance(result, Failed) and result.cause == "exit_status_unobservable"
    assert client.asked >= 2  # the transient failure was re-asked, then decided
