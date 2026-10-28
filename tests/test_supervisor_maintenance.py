"""DL-390 maintenance admission, containment, force and delivery boundaries; DL-401's
admission answer, the loop that runs the operation, maintenance.json and its readers."""

from __future__ import annotations

import json
import errno
import os
import selectors
import signal
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from dsl41 import runner_procid as procid
from dsl41 import runner_supervisor as rs
from dsl41.cli import app


@pytest.fixture
def rig(short_root, monkeypatch):
    sup = rs.Supervisor(str(short_root))
    sup._bind()
    sup._sel.register(sup._listen, selectors.EVENT_READ, ("listen", None))
    left, right = socket.socketpair()
    conn = rs._Conn(left)
    sup._conns[left.fileno()] = conn
    sup._sel.register(left, selectors.EVENT_READ, ("conn", conn))
    grant = sup._h_acquire(conn, {"controller_id": "maintenance"})
    request = {"incarnation": sup.incarnation, "token": grant["token"], "force": None}
    proof = SimpleNamespace(members=lambda: {os.getpid()})
    monkeypatch.setattr(rs.ServiceContainment, "observe", lambda: proof)
    (short_root / "leader.lock").write_bytes(b"existing engine evidence\n")
    try:
        yield sup, conn, request, proof
    finally:
        for client in sup._conns.values():
            client.out.clear()
        sup._teardown()
        right.close()


def assert_excluded(path):
    with pytest.raises(OSError):
        fd = procid.flock_exclusive(path)
        os.close(fd)


def stop(sup, conn, request):
    """MAINTENANCE_STOP's admission answer, then the loop's passes until the operation
    leaves `maintaining`, each pause spent on `rs.time`, as the loop's select spends it."""
    answer = sup._h_maintenance_stop(conn, request)
    while sup._maintaining:
        sup._maintenance_pass()
        rs.time.sleep(sup._maintenance.pause)
    return answer


def record_of(sup):
    return rs.read_maintenance_record(sup.run_root)


def ladder_of(effect):
    """A stand-in for the forced ladder: one step that runs `effect`, then done."""

    def ladder(*_args):
        yield from ()
        effect()

    return ladder


def run_ladder(owner, proof=None):
    """The ladder to its end, each pause slept on `rs.time`."""
    for pause in rs.Supervisor._maintenance_ladder(owner, proof):
        rs.time.sleep(pause)


@pytest.mark.parametrize("failure", ["missing", "held", "symlink", "unreadable", "replaced"])
def test_leader_evidence_refuses_without_teardown(rig, monkeypatch, failure):
    sup, conn, request, _proof = rig
    path = Path(sup.run_root) / "leader.lock"
    before = path.read_bytes()
    held = None
    if failure == "missing":
        path.unlink()
    elif failure == "held":
        held = procid.flock_exclusive(path)
    elif failure == "symlink":
        path.rename(path.with_suffix(".original"))
        path.symlink_to(path.with_suffix(".original"))
    elif failure == "unreadable":
        path.chmod(0)
    else:
        real = rs.flock_existing

        def replace(candidate):
            fd = real(candidate)
            path.unlink()
            path.write_bytes(b"replacement")
            return fd

        monkeypatch.setattr(rs, "flock_existing", replace)
    try:
        answer = sup._h_maintenance_stop(conn, request)
        assert answer["error"] == "maintenance_refused"
        assert sup.state == "serving"
        assert sup._maintenance_lock_fd is None
        assert record_of(sup) is None
        assert Path(sup.sock_path).exists() and Path(sup.pid_path).exists()
        if failure == "missing":
            assert not path.exists()
        elif failure in ("held", "unreadable"):
            path.chmod(0o600)
            assert path.read_bytes() == before
    finally:
        if held is not None:
            os.close(held)


@pytest.mark.parametrize("force", ["orphan", "kill", True, 1, []])
def test_unknown_modes_never_enter_termination(rig, monkeypatch, force):
    sup, conn, request, _proof = rig
    monkeypatch.setattr(sup, "_maintenance_ladder", lambda *_: pytest.fail("unexpected ladder"))
    answer = sup._h_maintenance_stop(conn, {**request, "force": force})
    assert answer["error"] == "maintenance_refused" and sup.state == "serving"
    assert record_of(sup) is None


@pytest.mark.parametrize("reason", ["active", "just_spawned", "prior_incarnation", "evicted"])
def test_empty_list_does_not_prove_idle(rig, monkeypatch, reason):
    sup, conn, request, proof = rig
    lifeline = None
    if reason in ("active", "just_spawned"):
        read_fd, write_fd = os.pipe()
        lifeline = read_fd, write_fd
        run = rs._Run(
            run_id="r",
            job="j",
            run_number=1,
            run_dir=str(Path(sup.run_root) / "run"),
            wrapper_pid=99999999,
            lifeline_w=write_fd,
            spawned_at="now",
            grace_seconds=0,
        )
        sup.runs["r"] = run
        if reason == "active":
            Path(run.run_dir).mkdir()
            (Path(run.run_dir) / "status.json").write_text('{"outcome":"exited"}')
    else:
        proof.members = lambda: {os.getpid(), 99999999}
    monkeypatch.setattr(rs, "killpg_quiet", lambda *_: pytest.fail("default sent a signal"))
    try:
        answer = sup._h_maintenance_stop(conn, request)
        assert answer["error"] == "maintenance_refused" and sup.state == "serving"
        assert Path(sup.sock_path).exists()
        if lifeline:
            os.fstat(lifeline[1])
    finally:
        if lifeline:
            os.close(lifeline[0])


def test_unavailable_observation_refuses_even_force(rig, monkeypatch):
    sup, conn, request, _proof = rig

    def fail():
        raise OSError("cannot observe complete containment")

    monkeypatch.setattr(rs.ServiceContainment, "observe", fail)
    answer = sup._h_maintenance_stop(conn, {**request, "force": "terminate"})
    assert answer["error"] == "maintenance_refused" and sup.state == "serving"
    assert sup._maintenance_lock_fd is None


def test_success_is_late_and_holds_engine_exclusion_through_teardown(rig, monkeypatch):
    sup, conn, request, _proof = rig
    path = Path(sup.run_root) / "leader.lock"
    original = sup._maintenance_prepare_teardown

    def prepare(proof):
        assert_excluded(path)
        original(proof)

    monkeypatch.setattr(sup, "_maintenance_prepare_teardown", prepare)
    answer = stop(sup, conn, request)
    assert answer["maintenance"] == "admitted" and sup.state == "maintenance_completed"
    assert answer["incarnation"] == sup.incarnation
    assert answer["bound_s"] == rs._MAINTENANCE_TEARDOWN_S and answer["force"] is None
    record = record_of(sup)
    assert record["state"] == "completed" and record["incarnation"] == sup.incarnation
    assert not Path(sup.sock_path).exists() and not Path(sup.pid_path).exists()
    assert_excluded(path)
    sup._teardown()
    fd = procid.flock_existing(path)
    os.close(fd)


@pytest.mark.parametrize("failure", ["residual", "observation", "termination", "teardown"])
def test_accepted_force_uncertainty_retains_lifelines_and_lock(rig, monkeypatch, failure):
    sup, conn, request, proof = rig
    samples = 0

    def members():
        nonlocal samples
        samples += 1
        if failure == "observation" and samples > 1:
            raise OSError("observation failed after admission")
        return {os.getpid(), 99999999} if samples == 1 or failure == "residual" else {os.getpid()}

    proof.members = members

    def wait():
        assert_excluded(Path(sup.run_root) / "leader.lock")
        if failure == "termination":
            raise OSError("unverified command")

    monkeypatch.setattr(sup, "_maintenance_ladder", ladder_of(wait))
    if failure == "teardown":
        monkeypatch.setattr(
            sup, "_maintenance_prepare_teardown", lambda _: (_ for _ in ()).throw(OSError("unlink"))
        )
    answer = stop(sup, conn, {**request, "force": "terminate"})
    assert answer["maintenance"] == "admitted" and sup.state == "maintenance_uncertain"
    record = record_of(sup)
    assert record["state"] == "uncertain" and record["detail"] and record["force"] == "terminate"
    assert_excluded(Path(sup.run_root) / "leader.lock")
    for verb in ("MAINTENANCE_STOP", "SPAWN", "SIGNAL", "SHUTDOWN"):
        assert sup._fence(verb, request)["error"] == "maintenance_uncertain"
    assert sup.state == "maintenance_uncertain" and sup._running
    assert sup._h_ping(conn, {})["process_state"] == "maintenance_uncertain"
    assert sup._h_list(conn, {})["process_state"] == "maintenance_uncertain"
    shown = {key: record[key] for key in ("force", "bound_s", "started_at")}
    assert sup._h_ping(conn, {})["maintenance"] == shown
    assert sup._h_list(conn, {})["maintenance"] == shown


def test_force_signals_only_validated_known_group(rig, monkeypatch):
    sup, _conn, _request, proof = rig
    sup._maintenance_lock_fd = procid.flock_existing(Path(sup.run_root) / "leader.lock")
    read_fd, write_fd = os.pipe()
    run = rs._Run(
        run_id="r",
        job="j",
        run_number=1,
        run_dir=sup.run_root,
        wrapper_pid=2000,
        lifeline_w=write_fd,
        spawned_at="now",
        grace_seconds=0,
    )
    proof.members = lambda: {os.getpid(), 2000, 3000, 4000}
    spawn = {
        "job": "j",
        "run_number": 1,
        "run_id": "r",
        "boot_id": sup.boot_id,
        "command_pid": 3000,
        "command_pgid": 3000,
        "command_start_time": "ticks:1",
    }
    (Path(run.run_dir) / "spawn.json").write_text(json.dumps(spawn))
    monkeypatch.setattr(rs, "verify_alive", lambda *_: True)
    monkeypatch.setattr(os, "getpgid", lambda _: 3000)
    sent = []
    monkeypatch.setattr(rs, "killpg_quiet", lambda pid, sig: sent.append((pid, sig)))
    try:
        sup._maintenance_signal(run, signal.SIGTERM, proof)
        sup._maintenance_signal(run, signal.SIGKILL, proof)
        assert sent == [(3000, signal.SIGTERM), (3000, signal.SIGKILL)]
        spawn["command_pgid"] = 4000
        (Path(run.run_dir) / "spawn.json").write_text(json.dumps(spawn))
        with pytest.raises(OSError, match="unverified command group"):
            sup._maintenance_signal(run, signal.SIGTERM, proof)
        assert len(sent) == 2
    finally:
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.parametrize(
    "delegated,main,group,kill_mode",
    [
        ("yes", "123", "/system.slice/test.service", "control-group"),
        ("no", "456", "/system.slice/test.service", "control-group"),
        ("no", "123", "/shared.service", "control-group"),
        ("no", "123", "/system.slice/test.service", "process"),
    ],
)
def test_manager_ownership_is_mandatory(monkeypatch, tmp_path, delegated, main, group, kill_mode):
    proof = procid.ServiceContainment(tmp_path, "/system.slice/test.service", "test.service", 123)
    output = f"MainPID={main}\nControlGroup={group}\nDelegate={delegated}\nKillMode={kill_mode}\n"
    monkeypatch.setattr(procid.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=output))
    with pytest.raises(procid.ObservationUnavailable):
        proof.members()


def test_containment_reads_complete_subtree_and_refuses_unreadable_child(monkeypatch, tmp_path):
    proof = procid.ServiceContainment(tmp_path, "/test.service", "test.service", os.getpid())
    monkeypatch.setattr(procid.ServiceContainment, "verify_manager", lambda _: None)
    (tmp_path / "cgroup.procs").write_text(f"{os.getpid()}\n")
    child = tmp_path / "child"
    child.mkdir()
    (child / "cgroup.procs").write_text("456\n")
    assert proof.members() == {os.getpid(), 456}
    (child / "cgroup.procs").unlink()
    with pytest.raises(OSError):
        proof.members()


@pytest.mark.parametrize("platform_name", ["darwin", "win32"])
def test_non_linux_containment_refuses_before_probing(monkeypatch, platform_name):
    def unexpected_probe(*_args, **_kwargs):
        pytest.fail("unsupported platform must refuse before probing containment")

    monkeypatch.setattr(procid.sys, "platform", platform_name)
    monkeypatch.setattr(Path, "read_text", unexpected_probe)
    monkeypatch.setattr(procid.subprocess, "run", unexpected_probe)
    with pytest.raises(
        procid.ObservationUnavailable, match="maintenance requires Linux service containment"
    ):
        procid.ServiceContainment.observe()


@pytest.mark.parametrize("hierarchy", ["v1", "v2", "hybrid"])
def test_systemd_v1_and_v2_hierarchies_are_qualified(monkeypatch, tmp_path, hierarchy):
    actual = Path.read_text

    def read(path, *a, **kw):
        if str(path) == "/proc/self/cgroup":
            v1 = "1:name=systemd:/system.slice/test.service\n"
            v2 = "0::/system.slice/test.service\n"
            return v1 + v2 if hierarchy == "hybrid" else v1 if hierarchy == "v1" else v2
        if str(path) == "/proc/self/mountinfo":
            tail = "cgroup cgroup rw,name=systemd" if hierarchy != "v2" else "cgroup2 cgroup rw"
            return f"1 0 0:1 / {tmp_path} rw - {tail}\n"
        return actual(path, *a, **kw)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(procid.sys, "platform", "linux")
    monkeypatch.setattr(procid.ServiceContainment, "verify_manager", lambda _: None)
    proof = procid.ServiceContainment.observe()
    assert proof.directory == tmp_path / "system.slice/test.service"


@pytest.mark.parametrize(
    "reply,expected",
    [
        ({"ok": False, "error": "unknown_verb"}, 2),
        ({"ok": False, "error": "unsupported_version"}, 2),
        ({"ok": False, "error": "maintenance_refused"}, 2),
        ({"ok": False, "error": "maintenance_uncertain"}, 4),
        ({"ok": True}, 4),
        ({"ok": True, "maintenance": "completed"}, 4),
        (OSError("partial send or lost reply"), 4),
        (ValueError("malformed reply"), 4),
    ],
)
def test_cli_mixed_version_and_uncertainty_never_fallback(monkeypatch, tmp_path, reply, expected):
    """MAINTENANCE_STOP is sent once, as v 2. A lost or malformed reply reconnects once,
    fences the request with its own lease and reads PING; it never sends the verb again
    (DL-401). A v 1 completion reply from an older supervisor is unknown."""
    from dsl41 import runner_adapters

    sent = []

    def send(request):
        sent.append(request)
        if request["cmd"] == "PING" and len(sent) == 1:
            return {"ok": True, "incarnation": "a" * 32, "process_state": "serving"}
        if request["cmd"] == "ACQUIRE":
            return {"ok": True, "token": 1, "incarnation": "a" * 32}
        if isinstance(reply, BaseException):
            raise reply
        return reply

    conn = SimpleNamespace(send=send, close=lambda: None)
    monkeypatch.setattr(runner_adapters, "SupervisorConn", lambda _: conn)
    (tmp_path / "supervisor.sock").touch()
    result = CliRunner().invoke(app, ["supervise", "shutdown", "--run-root", str(tmp_path)])
    assert result.exit_code == expected, result.output
    cmds = [request["cmd"] for request in sent]
    if isinstance(reply, BaseException):
        assert cmds == ["PING", "ACQUIRE", "MAINTENANCE_STOP", "ACQUIRE", "PING"]
        assert sent[3]["token"] == 1  # the lost request's own lease
    else:
        assert cmds == ["PING", "ACQUIRE", "MAINTENANCE_STOP"]
    assert sent[2]["force"] is None and sent[2]["v"] == rs.MAINTENANCE_STOP_VERSION


@pytest.mark.parametrize("ignore_term", [False, True])
def test_real_wrapper_force_waits_for_spawn_and_escalates_term_grace_kill(
    rig, monkeypatch, ignore_term
):
    from test_runner_supervisor import _spec, wait_for

    sup, conn, request, proof = rig
    run_dir = Path(sup.run_root) / "runs/j.1"
    command = f"trap '' TERM; touch '{run_dir}/ready'; sleep 30" if ignore_term else "sleep 30"
    spec = _spec(run_dir, command)
    spec["grace_seconds"] = 0.05
    started = sup.spawn_run(spec)
    assert started["ok"]
    if ignore_term:
        wait_for(lambda: (run_dir / "ready").exists())

    # The fake boundary supplies only containment. Wrapper/command identity, spawn
    # readiness, signals, waitpid and status records run through their real code.
    proof.members = lambda: _service_members(sup)
    signals = []
    original = rs.killpg_quiet

    def signal_group(pid, sig):
        assert_excluded(Path(sup.run_root) / "leader.lock")
        signals.append(sig)
        original(pid, sig)

    monkeypatch.setattr(rs, "killpg_quiet", signal_group)
    answer = stop(sup, conn, {**request, "force": "terminate"})
    assert answer.get("maintenance") == "admitted", answer
    assert sup.state == "maintenance_completed", record_of(sup)
    assert answer["bound_s"] >= 0.05 + rs._MAINTENANCE_TEARDOWN_S
    assert signals[0] == signal.SIGTERM
    assert set(signals) <= {signal.SIGTERM, signal.SIGKILL}
    if ignore_term:
        assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert all(run.wrapper_rc is not None for run in sup.runs.values())
    status = json.loads((run_dir / "status.json").read_text())
    assert status["outcome"] == "signaled" and status.get("reason") != "parent lost"


def _service_members(sup):
    """Stand-in containment: this process and every process in a run's wrapper session.

    Each wrapper leads its own session (runner_wrapper duty 1). A command's whole tree
    stays in that session unless it calls setsid, as a service cgroup keeps a forked
    tree, so the sample sees every group member, not only the recorded leader. Zombies
    are left out, as cgroup v2's cgroup.procs leaves them out."""
    import subprocess

    sessions = {run.wrapper_pid for run in sup.runs.values()}
    listing = subprocess.run(["ps", "-A", "-o", "pid="], capture_output=True, text=True)
    found = {os.getpid()}
    for pid in map(int, listing.stdout.split()):
        try:
            if os.getsid(pid) in sessions and not procid.proc_is_zombie(pid):
                found.add(pid)
        except OSError:
            continue
    return found


_GROUP_CHILD = """
import os, signal, sys, time
mode, ready = sys.argv[1:]
if mode == "escape":
    os.setpgid(0, 0)
if mode == "graceful":
    signal.signal(signal.SIGTERM, lambda *_: (time.sleep(0.3), sys.exit(0)))
else:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
with open(ready + ".tmp", "w") as out:
    out.write(str(os.getpid()))
os.rename(ready + ".tmp", ready)
time.sleep(30)
"""


def _force_group_child(rig, monkeypatch, mode, grace):
    """Force maintenance on `sh -c 'child & wait'`: the shell leader dies on TERM at once
    and leaves its child behind. Only containment is faked (`_service_members`)."""
    import shlex
    import sys
    import time

    from test_runner_supervisor import _spec, wait_for

    sup, conn, request, proof = rig
    run_dir = Path(sup.run_root) / "runs/j.1"
    ready = run_dir / "child.pid"
    command = (
        f"{shlex.quote(sys.executable)} -c {shlex.quote(_GROUP_CHILD)} {mode} "
        f"{shlex.quote(str(ready))} & wait"
    )
    spec = _spec(run_dir, command)
    spec["grace_seconds"] = grace
    assert sup.spawn_run(spec)["ok"]
    child = int(wait_for(lambda: ready.exists() and ready.read_text()))
    token = procid.proc_start_token(child)
    assert token is not None

    proof.members = lambda: _service_members(sup)
    signals = []
    original = rs.killpg_quiet

    def signal_group(pgid, sig):
        signals.append((pgid, sig, time.monotonic()))
        original(pgid, sig)

    monkeypatch.setattr(rs, "killpg_quiet", signal_group)
    started = time.monotonic()
    stop(sup, conn, {**request, "force": "terminate"})
    return sup.state, signals, time.monotonic() - started, child, token, run_dir


def _kill_child(child, token):
    from test_runner_supervisor import wait_for

    if procid.verify_alive(child, token):
        os.kill(child, signal.SIGKILL)
    wait_for(lambda: not procid.verify_alive(child, token))


def test_force_kills_a_term_ignoring_group_member_after_its_leader_exits(rig, monkeypatch):
    state, signals, _elapsed, child, token, run_dir = _force_group_child(
        rig, monkeypatch, "ignore", 0.5
    )
    try:
        assert state == "maintenance_completed", record_of(rig[0])
        assert [sig for _pgid, sig, _at in signals] == [signal.SIGTERM, signal.SIGKILL]
        assert len({pgid for pgid, _sig, _at in signals}) == 1
        assert signals[1][2] - signals[0][2] >= 0.5
        assert not procid.verify_alive(child, token)
        status = json.loads((run_dir / "status.json").read_text())
        assert status["outcome"] == "signaled"
    finally:
        _kill_child(child, token)


def test_force_group_containment_sampling_is_rate_bounded(monkeypatch):
    clock = SimpleNamespace(now=0.0)

    def sleep(seconds):
        clock.now += seconds

    monkeypatch.setattr(rs, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep))
    monkeypatch.setattr(rs, "_load_json", lambda _: {})
    run = SimpleNamespace(run_id="r", run_dir="unused", wrapper_rc=None, grace_seconds=1.0)
    group = (3000, {3001: "ticks:2"})
    samples, sent = [], []

    def signal_run(_run, sig, _proof, _group=None):
        sent.append((sig, clock.now))
        return None if sig == signal.SIGKILL else group

    def sample(groups, _proof):
        samples.append(clock.now)
        return groups

    reap_calls = []

    def reap():
        reap_calls.append(clock.now)
        if clock.now > 0:
            run.wrapper_rc = 0  # the wrapper exits with its leader; the group stays

    owner = SimpleNamespace(
        runs={"r": run}, _reap=reap, _maintenance_signal=signal_run, _maintenance_groups=sample
    )
    run_ladder(owner)
    assert [sig for sig, _at in sent] == [signal.SIGTERM, signal.SIGKILL]
    assert sent[1][1] >= 1.0
    # A 20 ms cadence would take about 50 samples during the 1 s grace.
    assert 1 <= len(samples) < 20
    assert all(b - a >= rs._MAINTENANCE_SAMPLE_S for a, b in zip(samples, samples[1:]))
    assert len(reap_calls) > 2 * len(samples)


def test_force_waits_for_a_graceful_group_member_without_kill(rig, monkeypatch):
    state, signals, elapsed, child, token, _run_dir = _force_group_child(
        rig, monkeypatch, "graceful", 5.0
    )
    try:
        assert state == "maintenance_completed", record_of(rig[0])
        assert [sig for _pgid, sig, _at in signals] == [signal.SIGTERM]
        assert elapsed < 5.0
        assert not procid.verify_alive(child, token)
    finally:
        _kill_child(child, token)


def test_force_never_signals_a_contained_member_outside_the_verified_group(rig, monkeypatch):
    sup = rig[0]
    state, signals, _elapsed, child, token, _run_dir = _force_group_child(
        rig, monkeypatch, "escape", 0.5
    )
    try:
        assert state == "maintenance_uncertain", record_of(sup)
        assert record_of(sup)["state"] == "uncertain"
        assert [sig for _pgid, sig, _at in signals] == [signal.SIGTERM]
        assert os.getpgid(child) not in {pgid for pgid, _sig, _at in signals}
        assert procid.verify_alive(child, token)
        assert sup.state == "maintenance_uncertain"
        assert_excluded(Path(sup.run_root) / "leader.lock")
    finally:
        _kill_child(child, token)


@pytest.mark.parametrize(
    "survivor", ["none", "member", "outside", "second_uncontained", "second_own_group"]
)
def test_force_group_identity_needs_a_surviving_recorded_member(rig, monkeypatch, survivor):
    sup, _conn, _request, proof = rig
    sup._maintenance_lock_fd = procid.flock_existing(Path(sup.run_root) / "leader.lock")
    run = SimpleNamespace(run_dir=sup.run_root, wrapper_rc=0, job="j", run_number=1, run_id="r")
    spawn = {
        "job": "j",
        "run_number": 1,
        "run_id": "r",
        "boot_id": sup.boot_id,
        "command_pid": 3000,
        "command_pgid": 3000,
        "command_start_time": "ticks:1",
    }
    (Path(sup.run_root) / "spawn.json").write_text(json.dumps(spawn))
    # The leader is gone. Pid 3500 now carries pgid 3000; only the recorded member 3001,
    # alive across the sample and still in the group, proves it is the verified group.
    # A second surviving recorded member, 3002, outside containment or outside the
    # group refuses even beside that proof.
    alive = {
        "none": set(),
        "member": {3001},
        "outside": {3001},
        "second_uncontained": {3001, 3002},
        "second_own_group": {3001, 3002},
    }[survivor]
    monkeypatch.setattr(rs, "verify_alive", lambda pid, _token: pid in alive)
    contained = {os.getpid(), 3500}
    if survivor != "outside":
        contained |= {3001}
    if survivor == "second_own_group":
        contained |= {3002}
    proof.members = lambda: contained
    own_group = survivor == "second_own_group"
    monkeypatch.setattr(os, "getpgid", lambda pid: pid if own_group and pid == 3002 else 3000)
    monkeypatch.setattr(rs, "proc_start_token", lambda pid: f"ticks:{pid}")
    sent = []
    monkeypatch.setattr(rs, "killpg_quiet", lambda pgid, sig: sent.append((pgid, sig)))
    group = (3000, {3000: "ticks:1", 3001: "ticks:2", 3002: "ticks:3"})
    if survivor in ("outside", "second_uncontained", "second_own_group"):
        with pytest.raises(OSError, match="outside its verified containment"):
            sup._maintenance_signal(run, signal.SIGKILL, proof, group)
        assert sent == []
    elif survivor == "none":
        assert sup._maintenance_signal(run, signal.SIGKILL, proof, group) is None
        assert sent == []
    else:
        found = sup._maintenance_signal(run, signal.SIGKILL, proof, group)
        assert found == (3000, {3001: "ticks:2", 3500: "ticks:3500"})
        assert sent == [(3000, signal.SIGKILL)]


def test_slow_group_sample_never_spends_the_reaping_interval_after_kill(monkeypatch):
    clock = SimpleNamespace(now=0.0, kill_at=None)

    def sleep(seconds):
        clock.now += seconds

    monkeypatch.setattr(rs, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep))
    monkeypatch.setattr(rs, "_load_json", lambda _: {})
    run = SimpleNamespace(run_id="r", run_dir="unused", wrapper_rc=None, grace_seconds=1.0)
    group = (3000, {3000: "ticks:1"})

    def signal_run(_run, sig, _proof, _group=None):
        if sig == signal.SIGKILL:
            clock.kill_at = clock.now
        return group

    def sample(groups, _proof):
        clock.now += 2.0  # a manager observation inside its own five-second bound
        return {} if clock.kill_at is not None else groups

    def reap():
        if clock.kill_at is not None and clock.now >= clock.kill_at + 0.3:
            run.wrapper_rc = 0  # the killed leader takes 0.3 s to exit

    owner = SimpleNamespace(
        runs={"r": run}, _reap=reap, _maintenance_signal=signal_run, _maintenance_groups=sample
    )
    run_ladder(owner)
    assert run.wrapper_rc == 0
    assert clock.now - clock.kill_at > 2.0


def test_uncertain_force_keeps_live_wrapper_lifeline(rig, monkeypatch):
    sup, conn, request, proof = rig
    read_fd, write_fd = os.pipe()
    run = rs._Run(
        run_id="r",
        job="j",
        run_number=1,
        run_dir=sup.run_root,
        wrapper_pid=99999999,
        lifeline_w=write_fd,
        spawned_at="now",
        grace_seconds=0,
    )
    sup.runs["r"] = run
    proof.members = lambda: {os.getpid(), 99999999}
    monkeypatch.setattr(sup, "_maintenance_ladder", ladder_of(lambda: None))
    try:
        stop(sup, conn, {**request, "force": "terminate"})
        assert sup.state == "maintenance_uncertain"
        assert record_of(sup)["detail"] == "work remains after the termination bound"
        os.fstat(write_fd)
        assert run.wrapper_rc is None
        assert_excluded(Path(sup.run_root) / "leader.lock")
    finally:
        os.close(read_fd)


#: A supervisor process whose containment is faked: this process and every process in a
#: run's wrapper session, as `_service_members` reads it. Everything else is real.
_LAUNCHER = """
import os, selectors, subprocess, sys
from types import SimpleNamespace
from dsl41 import runner_procid as procid
from dsl41 import runner_supervisor as r

OWNER = []

def members():
    sessions = {run.wrapper_pid for run in OWNER[0].runs.values()}
    listing = subprocess.run(["ps", "-A", "-o", "pid="], capture_output=True, text=True)
    found = {os.getpid()}
    for pid in map(int, listing.stdout.split()):
        try:
            if os.getsid(pid) in sessions and not procid.proc_is_zombie(pid):
                found.add(pid)
        except OSError:
            continue
    return found

r.ServiceContainment.observe = lambda: SimpleNamespace(members=members)
original = r.Supervisor._bind
def bind(self):
    OWNER.append(self)
    original(self)
    select = self._sel.select
    def batch(*args, **kwargs):
        ready = select(*args, **kwargs)
        if STALE and any(k.data[0] == 'conn' and m & selectors.EVENT_READ for k, m in ready):
            # a key whose descriptor the map no longer names this way is skipped
            ready.append((ready[0][0]._replace(data=("chld", None)), selectors.EVENT_READ))
        return ready
    self._sel.select = batch
r.Supervisor._bind = bind
if PARTIAL:
    prepare = r.Supervisor._maintenance_prepare_teardown
    def partial(self, proof):
        prepare(self, proof)
        raise OSError('synthetic error after owned teardown')
    r.Supervisor._maintenance_prepare_teardown = partial
sys.exit(r.main(sys.argv[1:]))
"""


def _launch(root, *, stale=False, partial=False):
    import subprocess
    import sys

    from test_runner_supervisor import wait_for

    code = f"STALE = {stale!r}\nPARTIAL = {partial!r}\n" + _LAUNCHER
    supervisor = subprocess.Popen(
        [sys.executable, "-c", code, "--run-root", str(root)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    wait_for(lambda: (root / "supervisor.sock").exists())
    return supervisor


def _reap_launched(supervisor):
    if supervisor.poll() is None:
        supervisor.terminate()
    supervisor.wait(timeout=10)
    supervisor.stderr.close()


@pytest.mark.parametrize("stale", [False, True])
@pytest.mark.parametrize("partial_teardown", [False, True])
def test_real_process_completed_maintenance_exits_eight_and_cli_verifies_exit(
    short_root, stale, partial_teardown
):
    import subprocess
    import sys

    (short_root / "leader.lock").touch()
    supervisor = _launch(short_root, stale=stale, partial=partial_teardown)
    try:
        cli = subprocess.run(
            [sys.executable, "-m", "dsl41", "supervise", "shutdown", "--run-root", str(short_root)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        record = rs.read_maintenance_record(short_root)
        if partial_teardown:
            assert cli.returncode == 4, cli.stderr
            assert "maintenance is uncertain: synthetic error" in cli.stderr
            assert record["state"] == "uncertain"
            assert supervisor.poll() is None
            supervisor.terminate()  # deliberate fixture host stop after proving retained uncertainty
            assert supervisor.wait(timeout=5) == 0, supervisor.stderr.read()
        else:
            assert cli.returncode == 0, cli.stderr
            # exit 0 prints the completed record, nothing else (DL-401)
            assert json.loads(cli.stdout) == record and record["state"] == "completed"
            assert supervisor.wait(timeout=5) == 8, supervisor.stderr.read()
        assert not (short_root / "supervisor.sock").exists()
        assert not (short_root / "supervisor.pid").exists()
    finally:
        _reap_launched(supervisor)


def test_late_teardown_failure_never_returns_maintenance_success(rig, monkeypatch):
    sup, conn, request, _proof = rig
    real = rs.os.unlink

    def unlink(path):
        if str(path) == sup.sock_path:
            raise PermissionError("synthetic late unlink failure")
        real(path)

    monkeypatch.setattr(rs.os, "unlink", unlink)
    stop(sup, conn, request)
    assert sup.state == "maintenance_uncertain"
    assert_excluded(Path(sup.run_root) / "leader.lock")
    assert Path(sup.sock_path).exists()


@pytest.mark.parametrize(
    "which",
    [
        "stays_alive",
        "stays_alive_without_socket",
        "unobservable",
        "gone",
        "zombie",
        "reused",
        "vanished_during_probe",
        "wait_then_gone",
        "gone_admitted",
        "uncertain",
    ],
)
def test_cli_requires_completion_and_target_process_exit(monkeypatch, tmp_path, which):
    """The admitted operation is watched in maintenance.json (DL-401): exit 0 needs the
    named process gone and the record completed. A live process at the bound is 3 only
    while a PING shows its operation still running; with no running operation on its
    socket it is 4, as are an identity it cannot read, an exit without completion and an
    uncertain record."""
    from dsl41 import cli_common, runner_adapters

    identity = {
        "incarnation": "a" * 32,
        "supervisor_pid": 123,
        "supervisor_start_time": "ticks:1",
        "boot_id": procid.current_boot_id(),
        "force": None,
        "bound_s": 0.0,
        "started_at": "2026-10-25T00:00:00+00:00",
    }
    state = {"gone_admitted": "admitted", "uncertain": "uncertain"}.get(which, "completed")
    record = {"version": 1, "state": state, **identity, "finished_at": "then", "detail": "why"}
    (tmp_path / "maintenance.json").write_text(json.dumps(record))

    pings = []

    def send(request):
        if request["cmd"] == "PING":
            pings.append(request)
            # the first PING precedes the request; a later one is the watch's probe
            running = len(pings) > 1 and which == "stays_alive"
            state = "maintenance_stopping" if running else "serving"
            return {"ok": True, "incarnation": "a" * 32, "process_state": state}
        if request["cmd"] == "ACQUIRE":
            return {"ok": True, "token": 1, "incarnation": "a" * 32}
        return {"ok": True, "maintenance": "admitted", **identity}

    monkeypatch.setattr(
        runner_adapters, "SupervisorConn", lambda _: SimpleNamespace(send=send, close=lambda: None)
    )
    # the bound is spent at once where the process stays; wait_then_gone needs a pass
    margin = 5.0 if which == "wait_then_gone" else 0.0
    monkeypatch.setattr(cli_common, "MAINTENANCE_MARGIN_S", margin)
    kill_calls = 0

    def kill(*_):
        nonlocal kill_calls
        kill_calls += 1
        if which in ("gone", "gone_admitted") or (
            which in ("vanished_during_probe", "wait_then_gone") and kill_calls > 1
        ):
            raise ProcessLookupError()

    monkeypatch.setattr(os, "kill", kill)
    monkeypatch.setattr(
        procid,
        "proc_start_token",
        lambda _: (
            None
            if which in ("unobservable", "vanished_during_probe")
            else "ticks:2"
            if which == "reused"
            else "ticks:1"
        ),
    )
    monkeypatch.setattr(procid, "proc_is_zombie", lambda _: which == "zombie")
    (tmp_path / "supervisor.sock").touch()
    result = CliRunner().invoke(app, ["supervise", "shutdown", "--run-root", str(tmp_path)])
    expected = {
        "stays_alive": 3,
        "stays_alive_without_socket": 4,
        "unobservable": 4,
        "gone_admitted": 4,
        "uncertain": 4,
    }
    assert result.exit_code == expected.get(which, 0), result.output
    if which == "stays_alive":
        assert "in progress; it finishes on its own" in result.output
    if which == "stays_alive_without_socket":
        assert "alive without a running operation on its socket (serving)" in result.output
    if which == "uncertain":
        assert "maintenance is uncertain: why" in result.output


def test_force_missing_spawn_identity_is_uncertain_without_signal_or_lifeline_close(
    rig, monkeypatch
):
    sup, conn, request, proof = rig
    read_fd, write_fd = os.pipe()
    run = rs._Run(
        run_id="r",
        job="j",
        run_number=1,
        run_dir=sup.run_root,
        wrapper_pid=99999999,
        lifeline_w=write_fd,
        spawned_at="now",
        grace_seconds=0,
    )
    sup.runs["r"] = run
    proof.members = lambda: {os.getpid(), 99999999}
    clock = SimpleNamespace(now=0.0)

    def sleep(seconds):
        clock.now += seconds

    monkeypatch.setattr(rs, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep))
    monkeypatch.setattr(rs, "killpg_quiet", lambda *_: pytest.fail("signalled an unknown group"))
    try:
        stop(sup, conn, {**request, "force": "terminate"})
        assert "no command identity" in record_of(sup)["detail"]
        assert sup.state == "maintenance_uncertain" and clock.now >= 5
        os.fstat(write_fd)
        assert_excluded(Path(sup.run_root) / "leader.lock")
    finally:
        os.close(read_fd)


def test_owned_teardown_failure_before_reply_is_uncertain(rig, monkeypatch):
    sup, conn, request, _proof = rig
    fail_fd = sup._chld_w
    real = os.close
    with monkeypatch.context() as patch:

        def close(fd):
            if fd == fail_fd:
                raise OSError("synthetic pipe teardown failure")
            real(fd)

        patch.setattr(os, "close", close)
        stop(sup, conn, request)
    assert sup.state == "maintenance_uncertain"
    assert_excluded(Path(sup.run_root) / "leader.lock")


@pytest.mark.parametrize("run_count", [1, 2])
def test_termination_observation_latency_cannot_consume_command_grace(monkeypatch, run_count):
    clock = SimpleNamespace(now=0.0)

    def sleep(seconds):
        clock.now += seconds

    monkeypatch.setattr(rs, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep))
    monkeypatch.setattr(rs, "_load_json", lambda _: {})
    runs = {
        str(i): SimpleNamespace(wrapper_rc=None, run_dir="unused", run_id=str(i), grace_seconds=3.0)
        for i in range(run_count)
    }
    sent = []

    def signal_run(run, sig, _proof, _group=None):
        if sig == signal.SIGTERM:
            clock.now += 4.0  # a successful manager observation can take up to five seconds
        sent.append((run.run_id, sig, clock.now))
        if sig == signal.SIGKILL:
            run.wrapper_rc = 0

    owner = SimpleNamespace(runs=runs, _reap=lambda: None, _maintenance_signal=signal_run)
    run_ladder(owner)
    for run_id in runs:
        signals = [(sig, at) for ident, sig, at in sent if ident == run_id]
        assert [sig for sig, _ in signals] == [signal.SIGTERM, signal.SIGKILL]
        assert signals[1][1] - signals[0][1] >= 3.0


@pytest.mark.parametrize(
    "shape", ["descriptor_not_regular", "path_not_regular", "no_read", "no_write"]
)
def test_leader_lock_requires_regular_readable_writable_evidence(tmp_path, shape):
    path = tmp_path / "leader.lock"
    path.touch()
    fd = os.open(path, os.O_RDWR)
    extra = None
    try:
        if shape == "descriptor_not_regular":
            os.close(fd)
            fd, extra = os.pipe()
        elif shape == "path_not_regular":
            path.unlink()
            path.mkdir()
        else:
            path.chmod(0o200 if shape == "no_read" else 0o400)
        with pytest.raises(OSError):
            procid.verify_lock_path(fd, path)
    finally:
        os.close(fd)
        if extra is not None:
            os.close(extra)


@pytest.mark.parametrize(
    "cgroups,mounts,reason",
    [
        ("3:cpu:/a\n", "", "unambiguous systemd"),
        ("0::/a.service\n0::/b.service\n", "", "unambiguous systemd"),
        ("0::/session.scope\n", "", "not in a service"),
        (
            "0::/a.service\n",
            "1 0 0:1 /restricted /sys/fs/cgroup rw - cgroup2 cgroup rw\n",
            "mount namespace",
        ),
        (
            "0::/a.service\n",
            "1 0 0:1 / /sys/fs/cgroup\\040escaped rw - cgroup2 cgroup rw\n",
            "mount namespace",
        ),
        (
            "0::a.service\n",
            "1 0 0:1 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n",
            "complete cgroup mount",
        ),
        (
            "0::/../a.service\n",
            "1 0 0:1 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n",
            "complete cgroup mount",
        ),
        (
            "0::/a.service\n",
            "1 0 0:1 / /sys/fs/cgroup rw - ext4 disk rw\n",
            "complete cgroup mount",
        ),
        (
            "1:name=systemd:/a.service\n",
            "1 0 0:1 / /sys/fs/cgroup rw - cgroup cgroup rw,cpu\n",
            "complete cgroup mount",
        ),
    ],
)
def test_unqualified_cgroup_hierarchy_never_becomes_empty_containment(
    monkeypatch, cgroups, mounts, reason
):
    def read(path, *_a, **_kw):
        return cgroups if str(path) == "/proc/self/cgroup" else mounts

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(procid.sys, "platform", "linux")
    with pytest.raises(procid.ObservationUnavailable, match=reason):
        procid.ServiceContainment.observe()


def test_manager_success_is_checked_before_reading_membership(monkeypatch, tmp_path):
    pid = os.getpid()
    proof = procid.ServiceContainment(tmp_path, "/test.service", "test.service", pid)
    observed = []

    def query(argv, **kwargs):
        observed.append((argv, kwargs))
        return SimpleNamespace(
            stdout=f"MainPID={pid}\nControlGroup=/test.service\nDelegate=no\nKillMode=control-group\nRefuseManualStop=yes\nSuccessExitStatus=8\nRestartPreventExitStatus=2 7 8\n"
        )

    monkeypatch.setattr(procid.subprocess, "run", query)
    (tmp_path / "cgroup.procs").write_text(f"{pid}\n")
    assert proof.members() == {pid}
    assert observed[0][0][:3] == ["systemctl", "show", "test.service"]
    assert 0 < observed[0][1]["timeout"] <= 5 and observed[0][1]["check"] is True


@pytest.mark.parametrize(
    "fault",
    ["missing", "symlink", "nested_symlink", "unreadable", "malformed", "zero", "self_absent"],
)
def test_cgroup_membership_faults_are_never_idle(monkeypatch, tmp_path, fault):
    directory = tmp_path / "service"
    directory.mkdir()
    (directory / "cgroup.procs").write_text(f"{os.getpid()}\n")
    proof = procid.ServiceContainment(directory, "/test.service", "test.service", os.getpid())
    monkeypatch.setattr(procid.ServiceContainment, "verify_manager", lambda _: None)
    if fault == "missing":
        (directory / "cgroup.procs").unlink()
        directory.rmdir()
    elif fault == "symlink":
        actual = tmp_path / "actual"
        directory.rename(actual)
        directory.symlink_to(actual, target_is_directory=True)
    elif fault == "nested_symlink":
        (directory / "subgroup").symlink_to(tmp_path, target_is_directory=True)
    elif fault == "unreadable":

        def walk(_path, *, onerror, **_kwargs):
            onerror(PermissionError("subtree unreadable"))
            return iter(())

        monkeypatch.setattr(os, "walk", walk)
    else:
        (directory / "cgroup.procs").write_text(
            {"malformed": "garbage\n", "zero": "0\n", "self_absent": "99999999\n"}[fault]
        )
    with pytest.raises(OSError):
        proof.members()


@pytest.mark.parametrize("stage", ["admission", "force", "teardown"])
def test_maintenance_statechart_error_is_fatal_and_never_a_refusal(rig, monkeypatch, stage):
    sup, conn, request, proof = rig

    def fail(*_a):
        raise rs.StatechartError("synthetic maintenance chart assertion")

    if stage == "admission":
        monkeypatch.setattr(rs.ServiceContainment, "observe", fail)
    elif stage == "force":
        proof.members = lambda: {os.getpid(), 99999999}
        monkeypatch.setattr(sup, "_maintenance_ladder", ladder_of(fail))
    else:
        monkeypatch.setattr(sup, "_maintenance_prepare_teardown", fail)
    with pytest.raises(rs.StatechartError):
        stop(sup, conn, {**request, "force": "terminate"})
    assert sup.state != "maintenance_completed"


@pytest.mark.parametrize(
    "fault", ["missing_completed", "foreign_record", "dead_identity", "outside", "wrong_group"]
)
def test_force_never_signals_unverified_or_absent_command_identity(rig, monkeypatch, fault):
    sup, _conn, _request, proof = rig
    sup._maintenance_lock_fd = procid.flock_existing(Path(sup.run_root) / "leader.lock")
    run = SimpleNamespace(run_dir=sup.run_root, wrapper_rc=0, job="j", run_number=1, run_id="r")
    spawn = {
        "job": "j",
        "run_number": 1,
        "run_id": "r",
        "boot_id": sup.boot_id,
        "command_pid": 3000,
        "command_pgid": 3000,
        "command_start_time": "ticks:1",
    }
    if fault != "missing_completed":
        if fault == "foreign_record":
            spawn["run_id"] = "other"
        (Path(sup.run_root) / "spawn.json").write_text(json.dumps(spawn))
    monkeypatch.setattr(rs, "verify_alive", lambda *_: fault != "dead_identity")
    proof.members = lambda: {os.getpid()} if fault == "outside" else {os.getpid(), 3000}
    monkeypatch.setattr(os, "getpgid", lambda _: 4000 if fault == "wrong_group" else 3000)
    monkeypatch.setattr(
        rs, "killpg_quiet", lambda *_: pytest.fail("unverified group was signalled")
    )
    if fault in ("foreign_record", "outside", "wrong_group"):
        with pytest.raises(OSError):
            sup._maintenance_signal(run, signal.SIGTERM, proof)
    else:
        sup._maintenance_signal(run, signal.SIGTERM, proof)


@pytest.mark.parametrize(
    "fault",
    [
        "lost_idle",
        "socket_replaced",
        "socket_regular",
        "pid_symlink",
        "pid_missing",
        "pid_foreign",
        "no_listener",
    ],
)
def test_teardown_requires_final_idle_and_ownership_proofs(rig, monkeypatch, fault):
    sup, conn, request, proof = rig
    if fault == "lost_idle":
        samples = iter([{os.getpid()}, {os.getpid(), 99999999}])
        proof.members = lambda: next(samples)
    elif fault.startswith("socket"):
        os.unlink(sup.sock_path)
        if fault == "socket_regular":
            Path(sup.sock_path).touch()
        else:
            replacement = socket.socket(socket.AF_UNIX)
            replacement.bind(sup.sock_path)
            replacement.close()
    elif fault == "pid_symlink":
        Path(sup.pid_path).unlink()
        Path(sup.pid_path).symlink_to(Path(sup.run_root) / "leader.lock")
    elif fault == "pid_missing":
        Path(sup.pid_path).unlink()
    elif fault == "pid_foreign":
        Path(sup.pid_path).write_text('{"incarnation":"foreign"}')
    else:
        sup._sel.unregister(sup._listen)
        sup._listen.close()
        sup._listen = None
    stop(sup, conn, request)
    if fault == "no_listener":
        assert sup.state == "maintenance_completed"
    else:
        assert sup.state == "maintenance_uncertain"
        assert_excluded(Path(sup.run_root) / "leader.lock")


def test_slow_verified_kill_batch_gets_a_full_final_reaping_interval(monkeypatch):
    clock = SimpleNamespace(now=0.0, last_kill=None, kills=0)

    def sleep(seconds):
        clock.now += seconds

    monkeypatch.setattr(rs, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep))
    monkeypatch.setattr(rs, "_load_json", lambda _: {})
    runs = {
        str(i): SimpleNamespace(wrapper_rc=None, run_dir="unused", run_id=str(i), grace_seconds=1.0)
        for i in range(6)
    }

    def signal_run(_run, sig, _proof, _group=None):
        if sig == signal.SIGKILL:
            clock.now += 0.5
            clock.kills += 1
            clock.last_kill = clock.now

    def reap():
        if clock.kills == len(runs) and clock.now >= clock.last_kill + 1.5:
            for run in runs.values():
                run.wrapper_rc = 0

    owner = SimpleNamespace(runs=runs, _reap=reap, _maintenance_signal=signal_run)
    run_ladder(owner)
    assert clock.kills == len(runs)
    assert all(run.wrapper_rc is not None for run in runs.values())
    assert clock.now - clock.last_kill >= 1.5


@pytest.mark.parametrize("race", ["membership", "getpgid", "changed_group"])
def test_command_exit_during_valid_observation_is_not_a_false_uncertainty(rig, monkeypatch, race):
    sup, _conn, _request, proof = rig
    sup._maintenance_lock_fd = procid.flock_existing(Path(sup.run_root) / "leader.lock")
    run = SimpleNamespace(run_dir=sup.run_root, wrapper_rc=None, job="j", run_number=1, run_id="r")
    spawn = {
        "job": "j",
        "run_number": 1,
        "run_id": "r",
        "boot_id": sup.boot_id,
        "command_pid": 3000,
        "command_pgid": 3000,
        "command_start_time": "ticks:1",
    }
    (Path(sup.run_root) / "spawn.json").write_text(json.dumps(spawn))
    alive = iter([True, False])
    monkeypatch.setattr(rs, "verify_alive", lambda *_: next(alive))
    proof.members = lambda: {os.getpid()} if race == "membership" else {os.getpid(), 3000}

    def group(_):
        if race == "getpgid":
            raise ProcessLookupError()
        return 4000

    monkeypatch.setattr(os, "getpgid", group)
    monkeypatch.setattr(rs, "killpg_quiet", lambda *_: pytest.fail("exited command signalled"))
    sup._maintenance_signal(run, signal.SIGKILL, proof)


@pytest.mark.parametrize(
    "property_name,value",
    [
        ("SuccessExitStatus", ""),
        ("RestartPreventExitStatus", "2 7"),
    ],
)
def test_stale_unit_exit_policy_refuses_maintenance(monkeypatch, tmp_path, property_name, value):
    properties = {
        "MainPID": str(os.getpid()),
        "ControlGroup": "/test.service",
        "Delegate": "no",
        "KillMode": "control-group",
        "RefuseManualStop": "yes",
        "SuccessExitStatus": "8",
        "RestartPreventExitStatus": "2 7 8",
    }
    properties[property_name] = value
    monkeypatch.setattr(
        procid.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(
            stdout="\n".join(f"{key}={value}" for key, value in properties.items())
        ),
    )
    proof = procid.ServiceContainment(tmp_path, "/test.service", "test.service", os.getpid())
    with pytest.raises(procid.ObservationUnavailable):
        proof.verify_manager()


def test_maintenance_admission_reads_no_stop_guard(monkeypatch, tmp_path):
    """DL-399, DL-416: admission verifies identity, containment and exit policy,
    and no stop guard, so a unit without RefuseManualStop is admitted here."""
    calls = []

    def query(argv, **_kwargs):
        calls.append(argv)
        return SimpleNamespace(
            stdout=(
                f"MainPID={os.getpid()}\nControlGroup=/test.service\nDelegate=no\n"
                "KillMode=control-group\nRefuseManualStop=no\nSuccessExitStatus=8\n"
                "RestartPreventExitStatus=2 7 8\n"
            )
        )

    monkeypatch.setattr(procid.subprocess, "run", query)
    proof = procid.ServiceContainment(tmp_path, "/test.service", "test.service", os.getpid())
    proof.verify_manager()
    assert len(calls) == 1
    assert not any("RefuseManualStop" in arg for arg in calls[0])


@pytest.mark.parametrize(
    "action,force", [("shutdown", "orphan"), ("shutdown", "kill"), ("list", "terminate")]
)
def test_cli_invalid_force_refuses_before_connecting(tmp_path, action, force):
    result = CliRunner().invoke(
        app, ["supervise", action, "--force", force, "--run-root", str(tmp_path)]
    )
    assert result.exit_code == 2
    assert "--force=terminate is valid only" in result.output


@pytest.mark.parametrize(
    "acquired",
    [
        {"ok": True, "token": 1},
        {"ok": True, "incarnation": "", "token": 1},
        {"ok": True, "incarnation": "a", "token": True},
    ],
)
def test_cli_malformed_lease_never_attempts_maintenance(monkeypatch, tmp_path, acquired):
    from dsl41 import runner_adapters

    sent = []

    def send(request):
        sent.append(request)
        return acquired

    monkeypatch.setattr(
        runner_adapters, "SupervisorConn", lambda _: SimpleNamespace(send=send, close=lambda: None)
    )
    (tmp_path / "supervisor.sock").touch()
    result = CliRunner().invoke(app, ["supervise", "shutdown", "--run-root", str(tmp_path)])
    assert result.exit_code == 2
    assert "malformed supervisor lease" in result.output
    assert [request["cmd"] for request in sent] == ["PING", "ACQUIRE"]


def test_blocking_supervisor_client_skips_exit_push_before_maintenance_reply():
    from dsl41.runner_adapters import SupervisorConn

    left, right = socket.socketpair()
    client = SupervisorConn.__new__(SupervisorConn)
    client.conn, client.buf, client.incarnation = left, b"", None
    client.timeout_s = 60.0
    right.sendall(b'{"push":"exit","run_id":"completed"}\n{"ok":true,"incarnation":"current"}\n')
    try:
        assert client.send({"cmd": "ACQUIRE"}) == {"ok": True, "incarnation": "current"}
        assert client.incarnation == "current"
    finally:
        client.close()
        right.close()


@pytest.mark.parametrize("reply", [b'{"ok":', b'{"push":"exit"}\n'])
def test_blocking_supervisor_request_deadline_survives_partial_reads_and_pushes(monkeypatch, reply):
    from dsl41 import runner_adapters as adapters

    clock = SimpleNamespace(now=0.0, reads=0)
    sent = []
    waits = []

    def settimeout(seconds):
        waits.append(seconds)

    def sendall(payload):
        sent.append(json.loads(payload))
        clock.now += 5.0

    def recv(_size):
        clock.reads += 1
        assert clock.reads <= 3, "request timeout was extended by reply traffic"
        if waits[-1] < 40.0:
            clock.now += waits[-1]
            raise TimeoutError("synthetic socket wait expired")
        clock.now += 40.0
        return reply

    wire = SimpleNamespace(settimeout=settimeout, sendall=sendall, recv=recv)
    client = adapters.SupervisorConn.__new__(adapters.SupervisorConn)
    client.conn, client.buf, client.incarnation, client.timeout_s = wire, b"", "current", 60.0
    monkeypatch.setattr(adapters, "time", SimpleNamespace(monotonic=lambda: clock.now))
    with pytest.raises(TimeoutError):
        client.send({"cmd": "MAINTENANCE_STOP", "token": 7})
    assert clock.now == 60.0
    assert waits[-1] == 15.0
    assert [request["cmd"] for request in sent] == ["MAINTENANCE_STOP"]


def test_blocking_supervisor_deadline_includes_buffered_push_processing(monkeypatch):
    from dsl41 import runner_adapters as adapters

    clock = SimpleNamespace(now=0.0)
    sent = []
    loads = json.loads

    def parse(line):
        clock.now += 31.0
        return loads(line)

    client = adapters.SupervisorConn.__new__(adapters.SupervisorConn)
    client.conn = SimpleNamespace(settimeout=lambda _: None, sendall=sent.append)
    client.buf = b'{"push":"exit"}\n' * 2 + b'{"ok":true,"incarnation":"late"}\n'
    client.incarnation, client.timeout_s = "current", 60.0
    monkeypatch.setattr(adapters, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(adapters, "json", SimpleNamespace(dumps=json.dumps, loads=parse))
    with pytest.raises(TimeoutError, match="supervisor reply deadline expired"):
        client.send({"cmd": "MAINTENANCE_STOP", "token": 7})
    assert len(sent) == 1
    assert client.incarnation == "current"
    assert client.buf == b'{"ok":true,"incarnation":"late"}\n'


def test_blocking_supervisor_each_request_gets_a_fresh_deadline(monkeypatch):
    from dsl41 import runner_adapters as adapters

    clock = SimpleNamespace(now=0.0)
    waits = []
    sent = []

    def sendall(payload):
        sent.append(json.loads(payload))
        clock.now += 5.0

    def recv(_size):
        clock.now += 10.0
        return b'{"ok":true,"incarnation":"current"}\n'

    client = adapters.SupervisorConn.__new__(adapters.SupervisorConn)
    client.conn = SimpleNamespace(settimeout=waits.append, sendall=sendall, recv=recv)
    client.buf, client.incarnation, client.timeout_s = b"", None, 60.0
    monkeypatch.setattr(adapters, "time", SimpleNamespace(monotonic=lambda: clock.now))
    for _ in range(2):
        assert client.send({"cmd": "PING"}) == {"ok": True, "incarnation": "current"}
        clock.now += 100.0
    assert [request["cmd"] for request in sent] == ["PING", "PING"]
    assert waits == [60.0, 55.0, 45.0] * 2


def test_force_bound_never_treats_unreaped_wrappers_as_complete(monkeypatch):
    clock = SimpleNamespace(now=0.0)

    def sleep(seconds):
        clock.now += seconds

    monkeypatch.setattr(rs, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep))
    monkeypatch.setattr(rs, "_load_json", lambda _: {})
    run = SimpleNamespace(run_id="r", run_dir="unused", wrapper_rc=None, grace_seconds=0.0)
    sent = []
    owner = SimpleNamespace(
        runs={"r": run},
        _reap=lambda: None,
        _maintenance_signal=lambda _run, sig, _proof, _group=None: sent.append(sig),
    )
    with pytest.raises(OSError, match="wrappers remain"):
        run_ladder(owner)
    assert sent == [signal.SIGTERM, signal.SIGKILL]
    assert clock.now >= 2


@pytest.mark.parametrize("maintenance", [False, True])
def test_final_reply_transport_failure_does_not_reclassify_successful_maintenance(
    rig, monkeypatch, maintenance
):
    sup, conn, request, _proof = rig
    if maintenance:
        answer = stop(sup, conn, request)
        assert answer["maintenance"] == "admitted" and sup.state == "maintenance_completed"
    else:
        answer = {"ok": True}
    sup._send(conn, answer)

    def select(**_kwargs):
        raise OSError("final delivery failed")

    with monkeypatch.context() as patch:
        patch.setattr(sup._sel, "select", select)
        if maintenance:
            sup._teardown()
            assert sup.state == "closed" and sup._maintenance_lock_fd is None
        else:
            with pytest.raises(OSError, match="final delivery failed"):
                sup._teardown()
    if not maintenance:
        # A real process exit closes these after fatal legacy teardown; the in-process fixture must.
        sup._drop_conn(conn)
        sup._listen.close()
        for fd in (sup._chld_r, sup._chld_w, sup._lock_fd):
            os.close(fd)
        sup._lock_fd = None
        sup._sel.close()


def test_partial_termination_stops_on_second_run_identity_failure_and_retains_recovery_scope(
    rig, monkeypatch
):
    sup, conn, request, proof = rig
    read_fds = []
    for number in (1, 2):
        directory = Path(sup.run_root) / f"run{number}"
        directory.mkdir()
        read_fd, write_fd = os.pipe()
        read_fds.append(read_fd)
        run = rs._Run(
            run_id=f"r{number}",
            job=f"j{number}",
            run_number=1,
            run_dir=str(directory),
            wrapper_pid=2000 + number,
            lifeline_w=write_fd,
            spawned_at="now",
            grace_seconds=0,
        )
        sup.runs[run.run_id] = run
        (directory / "spawn.json").write_text(
            json.dumps(
                {
                    "job": run.job,
                    "run_number": 1,
                    "run_id": run.run_id if number == 1 else "foreign",
                    "boot_id": sup.boot_id,
                    "command_pid": 3000 + number,
                    "command_pgid": 3000 + number,
                    "command_start_time": "ticks:1",
                }
            )
        )
    proof.members = lambda: {os.getpid(), 2001, 2002, 3001, 3002}
    monkeypatch.setattr(rs, "verify_alive", lambda *_: True)
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    sent = []
    monkeypatch.setattr(rs, "killpg_quiet", lambda pid, sig: sent.append((pid, sig)))
    try:
        stop(sup, conn, {**request, "force": "terminate"})
        assert record_of(sup)["state"] == "uncertain"
        assert sent == [(3001, signal.SIGTERM)]
        assert sup.state == "maintenance_uncertain" and sup._running
        assert_excluded(Path(sup.run_root) / "leader.lock")
        for run in sup.runs.values():
            os.fstat(run.lifeline_w)
            assert run.wrapper_rc is None
        for verb in ("SPAWN", "SIGNAL", "SHUTDOWN", "MAINTENANCE_STOP"):
            assert sup._fence(verb, request)["error"] == "maintenance_uncertain"
        assert sup._h_list(conn, {})["process_state"] == "maintenance_uncertain"
        assert Path(sup.sock_path).exists() and Path(sup.pid_path).exists()
    finally:
        for fd in read_fds:
            os.close(fd)


def _manager_policy_text(**overrides):
    properties = {
        "MainPID": str(os.getpid()),
        "ControlGroup": "/test.service",
        "Delegate": "no",
        "KillMode": "control-group",
        "RefuseManualStop": "yes",
        "SuccessExitStatus": "[unprintable]",
        "RestartPreventExitStatus": "[unprintable]",
    }
    properties.update(overrides)
    return "\n".join(f"{key}={value}" for key, value in properties.items())


@pytest.mark.parametrize(
    "unprintable",
    [
        ("SuccessExitStatus",),
        ("RestartPreventExitStatus",),
        ("SuccessExitStatus", "RestartPreventExitStatus"),
    ],
)
def test_el8_unprintable_policy_reads_typed_loaded_manager_values(
    monkeypatch, tmp_path, unprintable
):
    calls = []
    path = "/org/freedesktop/systemd1/unit/test_2eservice"
    printable = {
        name: "8"
        for name in ("SuccessExitStatus", "RestartPreventExitStatus")
        if name not in unprintable
    }

    def query(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[0] == "systemctl":
            return SimpleNamespace(stdout=_manager_policy_text(**printable))
        if "GetUnit" in argv:
            assert argv[-2:] == ["s", "test.service"]
            return SimpleNamespace(stdout=f'o "{path}"\n')
        assert argv[:4] == ["busctl", "--system", "get-property", "org.freedesktop.systemd1"]
        assert argv[4:6] == [path, "org.freedesktop.systemd1.Service"]
        return SimpleNamespace(
            stdout="(aiai) 1 8 0\n" if argv[-1] == "SuccessExitStatus" else "(aiai) 3 8 7 2 0\n"
        )

    monkeypatch.setattr(procid.subprocess, "run", query)
    proof = procid.ServiceContainment(tmp_path, "/test.service", "test.service", os.getpid())
    proof.verify_manager()
    assert len(calls) == 2 + len(unprintable)
    assert all(0 < kwargs["timeout"] <= 5 and kwargs["check"] is True for _, kwargs in calls)


@pytest.mark.parametrize("unprintable", [True, False])
def test_the_busctl_policy_read_is_logged_once_per_process(
    monkeypatch, tmp_path, capsys, unprintable
):
    """DL-390: systemd 239's formatter prints [unprintable]; the log names the busctl read."""
    path = "/org/freedesktop/systemd1/unit/test_2eservice"
    printable = {} if unprintable else {"SuccessExitStatus": "8", "RestartPreventExitStatus": "2 8"}

    def query(argv, **_kwargs):
        if argv[0] == "systemctl":
            return SimpleNamespace(stdout=_manager_policy_text(**printable))
        if "GetUnit" in argv:
            return SimpleNamespace(stdout=f'o "{path}"\n')
        return SimpleNamespace(stdout="(aiai) 1 8 0\n")

    monkeypatch.setattr(procid.subprocess, "run", query)
    monkeypatch.setattr(procid, "_busctl_policy_logged", False)
    proof = procid.ServiceContainment(tmp_path, "/test.service", "test.service", os.getpid())
    proof.verify_manager()
    proof.verify_manager()
    line = (
        "supervisor: exit policy read through busctl (systemd text formatter printed [unprintable])"
    )
    assert capsys.readouterr().err.splitlines().count(line) == (1 if unprintable else 0)


def test_a_failed_busctl_log_write_changes_no_verdict_and_is_tried_again(monkeypatch, tmp_path):
    """DL-402: the diagnostic never decides admission. A stderr write that fails (ENOSPC here)
    leaves the manager verdict as it was, and the line is written on the next read."""
    path = "/org/freedesktop/systemd1/unit/test_2eservice"

    def query(argv, **_kwargs):
        if argv[0] == "systemctl":
            return SimpleNamespace(stdout=_manager_policy_text())
        if "GetUnit" in argv:
            return SimpleNamespace(stdout=f'o "{path}"\n')
        return SimpleNamespace(stdout="(aiai) 1 8 0\n")

    class FailingOnce:
        def __init__(self) -> None:
            self.failed = False
            self.lines: list[str] = []

        def write(self, text: str) -> int:
            if not self.failed:
                self.failed = True
                raise OSError(errno.ENOSPC, "no space left on device")
            self.lines.append(text)
            return len(text)

        def flush(self) -> None:
            pass

    stderr = FailingOnce()
    monkeypatch.setattr(procid.subprocess, "run", query)
    monkeypatch.setattr(procid, "_busctl_policy_logged", False)
    monkeypatch.setattr(procid.sys, "stderr", stderr)
    proof = procid.ServiceContainment(tmp_path, "/test.service", "test.service", os.getpid())
    proof.verify_manager()  # the write fails; the verdict stands
    assert stderr.failed and stderr.lines == [] and procid._busctl_policy_logged is False
    proof.verify_manager()  # written now, and the flag set
    assert "".join(stderr.lines).count("exit policy read through busctl") == 1
    assert procid._busctl_policy_logged is True


@pytest.mark.parametrize(
    "typed",
    [
        "",
        "ai 1 8",
        "(aiai) 1 8",
        "(aiai) -1 8 0",
        "(aiai) 1 x 0",
        "(aiai) 1 8.0 0",
        "(aiai) 1 8 1",
        "(aiai) 0 0 8",
        "(aiai) 2 8 0",
        "(aiai) 1 8 0 trailing",
        "(aiai) 1 2147483648 0",
    ],
)
def test_malformed_typed_policy_is_unavailable_not_authorization(monkeypatch, tmp_path, typed):
    def query(argv, **_kwargs):
        if argv[0] == "systemctl":
            return SimpleNamespace(stdout=_manager_policy_text())
        if "GetUnit" in argv:
            return SimpleNamespace(stdout='o "/org/freedesktop/systemd1/unit/test_2eservice"')
        return SimpleNamespace(stdout=typed)

    monkeypatch.setattr(procid.subprocess, "run", query)
    proof = procid.ServiceContainment(tmp_path, "/test.service", "test.service", os.getpid())
    with pytest.raises(procid.ObservationUnavailable):
        proof.verify_manager()


def test_oversized_typed_integer_cannot_bypass_policy_decoder(monkeypatch, tmp_path):
    import sys

    previous = sys.get_int_max_str_digits()
    try:
        sys.set_int_max_str_digits(640)
        typed = "(aiai) 1 " + "8" * 641 + " 0"

        def query(argv, **_kwargs):
            if argv[0] == "systemctl":
                return SimpleNamespace(stdout=_manager_policy_text())
            if "GetUnit" in argv:
                return SimpleNamespace(stdout='o "/org/freedesktop/systemd1/unit/test_2eservice"')
            return SimpleNamespace(stdout=typed)

        monkeypatch.setattr(procid.subprocess, "run", query)
        proof = procid.ServiceContainment(tmp_path, "/test.service", "test.service", os.getpid())
        with pytest.raises(procid.ObservationUnavailable):
            proof.verify_manager()
    finally:
        sys.set_int_max_str_digits(previous)


@pytest.mark.parametrize("property_name", ["SuccessExitStatus", "RestartPreventExitStatus"])
@pytest.mark.parametrize("typed", ["(aiai) 0 1 8", "(aiai) 2 2 7 1 8", "(aiai) 0 0"])
def test_signal_eight_or_empty_exit_array_never_satisfies_exit_eight_policy(
    monkeypatch, tmp_path, property_name, typed
):
    def query(argv, **_kwargs):
        if argv[0] == "systemctl":
            return SimpleNamespace(stdout=_manager_policy_text())
        if "GetUnit" in argv:
            return SimpleNamespace(stdout='o "/org/freedesktop/systemd1/unit/test_2eservice"')
        return SimpleNamespace(stdout=typed if argv[-1] == property_name else "(aiai) 1 8 0")

    monkeypatch.setattr(procid.subprocess, "run", query)
    proof = procid.ServiceContainment(tmp_path, "/test.service", "test.service", os.getpid())
    with pytest.raises(procid.ObservationUnavailable, match="exit policy"):
        proof.verify_manager()


@pytest.mark.parametrize(
    "reply",
    [
        "",
        "o not-json",
        's "/org/freedesktop/systemd1/unit/test_2eservice"',
        "o 8",
        'o "/org/other"',
        'o "/org/freedesktop/systemd1/unit/"',
    ],
)
def test_unit_object_resolution_must_be_typed_and_manager_owned(monkeypatch, tmp_path, reply):
    calls = []

    def query(argv, **_kwargs):
        calls.append(argv)
        return SimpleNamespace(stdout=_manager_policy_text() if argv[0] == "systemctl" else reply)

    monkeypatch.setattr(procid.subprocess, "run", query)
    proof = procid.ServiceContainment(tmp_path, "/test.service", "test.service", os.getpid())
    with pytest.raises(procid.ObservationUnavailable, match="unit object path"):
        proof.verify_manager()
    assert len(calls) == 2


@pytest.mark.parametrize("stage", ["get_unit", "success", "prevent_restart"])
@pytest.mark.parametrize("failure", ["missing_helper", "manager_refusal", "timeout"])
def test_typed_observation_capability_failure_refuses_before_maintenance(
    rig, monkeypatch, stage, failure
):
    import subprocess

    sup, conn, request, proof = rig
    manager = procid.ServiceContainment(
        Path(sup.run_root), "/test.service", "test.service", os.getpid()
    )

    def query(argv, **_kwargs):
        if argv[0] == "systemctl":
            return SimpleNamespace(stdout=_manager_policy_text())
        current = (
            "get_unit"
            if "GetUnit" in argv
            else "success"
            if argv[-1] == "SuccessExitStatus"
            else "prevent_restart"
        )
        if current == stage:
            if failure == "missing_helper":
                raise FileNotFoundError("busctl unavailable")
            if failure == "manager_refusal":
                raise subprocess.CalledProcessError(1, argv)
            raise subprocess.TimeoutExpired(argv, 1)
        return SimpleNamespace(
            stdout='o "/org/freedesktop/systemd1/unit/test_2eservice"'
            if current == "get_unit"
            else "(aiai) 1 8 0"
        )

    monkeypatch.setattr(procid.subprocess, "run", query)

    def observe():
        manager.verify_manager()
        return proof

    monkeypatch.setattr(rs.ServiceContainment, "observe", observe)
    monkeypatch.setattr(
        rs, "killpg_quiet", lambda *_: pytest.fail("observation failure signalled work")
    )
    answer = sup._h_maintenance_stop(conn, request)
    assert answer["error"] == "maintenance_refused" and sup.state == "serving"
    assert sup._maintenance_lock_fd is None
    assert Path(sup.sock_path).exists() and Path(sup.pid_path).exists()


def test_unprintable_formatter_does_not_bypass_other_manager_checks(monkeypatch, tmp_path):
    calls = []

    def query(argv, **_kwargs):
        calls.append(argv)
        return SimpleNamespace(stdout=_manager_policy_text(MainPID="99999999"))

    monkeypatch.setattr(procid.subprocess, "run", query)
    proof = procid.ServiceContainment(tmp_path, "/test.service", "test.service", os.getpid())
    with pytest.raises(procid.ObservationUnavailable):
        proof.verify_manager()
    assert len(calls) == 1 and calls[0][0] == "systemctl"


def test_typed_manager_queries_share_one_five_second_budget(monkeypatch, tmp_path):
    clock = SimpleNamespace(now=100.0)
    observed = []
    monkeypatch.setattr(procid.time, "monotonic", lambda: clock.now)

    def query(argv, **kwargs):
        observed.append(kwargs["timeout"])
        clock.now += 1.0
        if argv[0] == "systemctl":
            return SimpleNamespace(stdout=_manager_policy_text())
        if "GetUnit" in argv:
            return SimpleNamespace(stdout='o "/org/freedesktop/systemd1/unit/test_2eservice"')
        return SimpleNamespace(stdout="(aiai) 1 8 0")

    monkeypatch.setattr(procid.subprocess, "run", query)
    proof = procid.ServiceContainment(tmp_path, "/test.service", "test.service", os.getpid())
    proof.verify_manager()
    assert observed == [5, 4, 3, 2]


def test_exhausted_manager_budget_starts_no_additional_helper(monkeypatch, tmp_path):
    clock = SimpleNamespace(now=100.0)
    calls = []
    monkeypatch.setattr(procid.time, "monotonic", lambda: clock.now)

    def query(argv, **_kwargs):
        calls.append(argv)
        clock.now += 5.0
        return SimpleNamespace(stdout=_manager_policy_text())

    monkeypatch.setattr(procid.subprocess, "run", query)
    proof = procid.ServiceContainment(tmp_path, "/test.service", "test.service", os.getpid())
    with pytest.raises(procid.ObservationUnavailable, match="timed out"):
        proof.verify_manager()
    assert len(calls) == 1


# ---- DL-401: admission answers at once; the operation runs in the loop


def _line(**request):
    return json.dumps(request).encode("utf-8")


@pytest.mark.parametrize(
    "version,detail",
    [(1, True), (3, False), (True, False), (2.0, False), (None, False)],
)
def test_maintenance_stop_is_version_2_and_its_v1_is_refused_by_name(rig, version, detail):
    """The verb's own version (DL-401). v 1, the completion-only dialect, is a tombstone
    that names DL-401; any other version is the unknown case. Nothing is admitted."""
    sup, conn, request, _proof = rig
    answer = sup._answer(conn, _line(cmd="MAINTENANCE_STOP", v=version, **request))
    assert answer["error"] == "unsupported_version"
    assert ("DL-401" in answer.get("detail", "")) is detail
    assert sup.state == "serving" and record_of(sup) is None
    assert sup._answer(conn, _line(cmd="PING", v=2))["error"] == "unsupported_version"


def test_admission_answers_with_the_bound_and_the_record_before_the_operation(rig, monkeypatch):
    """The admitted answer is the record's identity, bound and start. The record is
    durable before the answer. The loop then answers PING, LIST and the lease verbs and
    refuses run mutations while the forced ladder runs; the latched signal and the
    deadman wait for the outcome (DL-401). An unknown request field is ignored."""
    sup, conn, request, proof = rig
    proof.members = lambda: {os.getpid(), 99999999} if steps else {os.getpid()}
    read_fd, write_fd = os.pipe()
    sup.runs["r"] = rs._Run(
        run_id="r",
        job="j",
        run_number=1,
        run_dir=sup.run_root,
        wrapper_pid=99999999,
        lifeline_w=write_fd,
        spawned_at="now",
        grace_seconds=120,
    )
    steps = [0.5, 0.25]

    def ladder(_proof):
        while steps:
            yield steps.pop(0)
        sup.runs["r"].wrapper_rc = 0

    monkeypatch.setattr(sup, "_maintenance_ladder", ladder)
    try:
        answer = sup._answer(
            conn,
            _line(
                cmd="MAINTENANCE_STOP", v=2, future="ignored", **{**request, "force": "terminate"}
            ),
        )
        record = record_of(sup)
        assert answer["maintenance"] == "admitted" and answer["force"] == "terminate"
        # spawn wait, grace, settle, two verified sends for one group, samples, teardown
        assert answer["bound_s"] == 5.0 + 120 + 2.0 + 2 * 4.0 + 10.0 + 15.0
        assert {key: answer[key] for key in record if key not in ("version", "state")} == {
            key: record[key] for key in record if key not in ("version", "state")
        }
        assert record["state"] == "admitted" and sup.state == "maintenance_stopping"
        assert sup._pause() == 0.0
        sup._signals = 1
        sup.deadman_s = 1e-9
        for pause in (0.5, 0.25):
            sup._maintenance_pass()
            assert sup._pause() == pause and sup.state == "maintenance_stopping"
            ping = sup._answer(conn, _line(cmd="PING", v=1))
            assert ping["process_state"] == "maintenance_stopping"
            assert ping["maintenance"] == {
                "force": "terminate",
                "bound_s": answer["bound_s"],
                "started_at": answer["started_at"],
            }
            assert sup._answer(conn, _line(cmd="LIST", v=1))["maintenance"] == ping["maintenance"]
            renewed = sup._answer(conn, _line(cmd="RENEW", v=1, ttl_s=60, **request))
            assert renewed["ok"] is True
            for verb in ("SPAWN", "SIGNAL", "SHUTDOWN"):
                refused = sup._answer(conn, _line(cmd=verb, v=1, **request))
                assert refused == {"ok": False, "error": "maintenance_in_progress"}
            again = sup._answer(conn, _line(cmd="MAINTENANCE_STOP", v=2, **request))
            assert again == {"ok": False, "error": "maintenance_in_progress"}
        sup._maintenance_pass()
        assert sup.state == "maintenance_completing" and sup._pause() == 0.0
        sup._maintenance_pass()
        assert sup.state == "maintenance_completed" and sup._signals
        assert record_of(sup)["state"] == "completed" and record_of(sup)["finished_at"]
        assert sup._pause() == 1.0
    finally:
        os.close(read_fd)


@pytest.mark.parametrize("fault", ["record", "start_token"])
def test_admission_that_cannot_record_or_name_itself_is_refused(rig, monkeypatch, fault):
    """No record, no identity: a refusal, exit 2, nothing changed (DL-401)."""
    sup, conn, request, _proof = rig
    if fault == "record":

        def write(*_args):
            raise OSError("synthetic full disk")

        monkeypatch.setattr(rs, "durable_write_json", write)
    else:
        monkeypatch.setattr(rs, "proc_start_token", lambda _pid: None)
    answer = sup._h_maintenance_stop(conn, request)
    assert answer["error"] == "maintenance_refused" and sup.state == "serving"
    assert sup._maintenance is None and sup._maintenance_lock_fd is None
    assert record_of(sup) is None
    assert sup._h_ping(conn, {}).get("maintenance") is None


def test_an_uncertain_outcome_the_record_cannot_take_is_still_uncertain(rig, monkeypatch, capsys):
    sup, conn, request, proof = rig
    proof.members = lambda: {os.getpid(), 99999999}

    def fail():
        def write(*_args):
            raise OSError("synthetic lost disk")

        monkeypatch.setattr(rs, "durable_write_json", write)
        raise OSError("unverified command")

    monkeypatch.setattr(sup, "_maintenance_ladder", ladder_of(fail))
    stop(sup, conn, {**request, "force": "terminate"})
    assert sup.state == "maintenance_uncertain"
    assert record_of(sup)["state"] == "admitted"
    err = capsys.readouterr().err
    assert "supervisor: maintenance uncertain: unverified command" in err
    assert "maintenance.json not written: synthetic lost disk" in err


@pytest.mark.parametrize("runs", [5, 200])
def test_the_ladder_runs_dl394_in_full_past_its_estimate(monkeypatch, runs):
    """The bound is an estimate, never a stop (DL-401). Runs that ignore TERM, at 4 s per
    verified send for five and 0.1 s for two hundred, each get TERM, their grace, then KILL,
    and the ladder ends on their reaping, however long the sends take."""
    clock = SimpleNamespace(now=0.0)

    def sleep(seconds):
        clock.now += seconds

    monkeypatch.setattr(rs, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep))
    monkeypatch.setattr(rs, "_load_json", lambda _: {})
    cost = 4.0 if runs == 5 else 0.1
    table = {
        str(i): SimpleNamespace(wrapper_rc=None, run_dir="unused", run_id=str(i), grace_seconds=3.0)
        for i in range(runs)
    }
    sent = []

    def signal_run(run, sig, _proof, _group=None):
        clock.now += cost
        sent.append((run.run_id, sig))
        if sig == signal.SIGKILL:
            run.wrapper_rc = -9

    owner = SimpleNamespace(runs=table, _reap=lambda: None, _maintenance_signal=signal_run)
    run_ladder(owner)
    assert [sig for _, sig in sent].count(signal.SIGKILL) == runs
    assert all(run.wrapper_rc == -9 for run in table.values())
    assert clock.now > 5.0 + 3.0 + 2.0 if runs == 5 else True


def test_each_pass_runs_at_most_one_observation_and_the_loop_answers_between(rig, monkeypatch):
    """The real ladder under the latency model: each verified send and each containment
    sample costs 4 s on the supervisor's clock. A pass spends at most one of them, and PING
    is answered between passes (DL-401)."""
    sup, conn, request, proof = rig
    clock = SimpleNamespace(now=0.0)

    def sleep(seconds):
        clock.now += seconds

    monkeypatch.setattr(rs, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep))
    lifelines = []
    for number in range(3):
        read_fd, write_fd = os.pipe()
        lifelines.append(read_fd)
        sup.runs[f"r{number}"] = rs._Run(
            run_id=f"r{number}",
            job="j",
            run_number=number,
            run_dir=sup.run_root,
            wrapper_pid=99999990 + number,
            lifeline_w=write_fd,
            spawned_at="now",
            grace_seconds=1.0,
        )
    members = {os.getpid(), 99999990}
    proof.members = lambda: set(members)
    real_load = rs._load_json
    monkeypatch.setattr(  # every run has its spawn record; the pid record is read for real
        rs, "_load_json", lambda path: {} if path.endswith("spawn.json") else real_load(path)
    )

    def send(run, sig, _proof, group=None):
        clock.now += 4.0
        if sig == signal.SIGKILL:
            run.wrapper_rc = -9
            members.discard(99999990)
        return (3000, {3001: "ticks:1"}) if sig == signal.SIGTERM else None

    def sample(groups, _proof):
        clock.now += 4.0
        return groups

    monkeypatch.setattr(sup, "_maintenance_signal", send)
    monkeypatch.setattr(sup, "_maintenance_groups", sample)
    monkeypatch.setattr(sup, "_reap", lambda: None)
    try:
        answer = sup._h_maintenance_stop(conn, {**request, "force": "terminate"})
        assert answer["maintenance"] == "admitted"
        passes = 0
        while sup._maintaining:
            before = clock.now
            sup._maintenance_pass()
            assert clock.now - before <= 4.0, (passes, clock.now - before)
            assert sup._h_ping(conn, {})["ok"] is True
            rs.time.sleep(sup._pause())
            passes += 1
        assert sup.state == "maintenance_completed", record_of(sup)
        assert passes > 6
    finally:
        for fd in lifelines:
            os.close(fd)


def test_a_lost_request_is_fenced_by_its_own_lease_before_the_ping(rig):
    """A reconnect that presents the lost request's lease to ACQUIRE ends that token:
    the request, read only afterwards, is refused stale_token, and PING says serving.
    The ordering of two connections cannot admit it later (DL-401)."""
    sup, conn, request, _proof = rig
    left, right = socket.socketpair()
    other = rs._Conn(left)
    sup._conns[left.fileno()] = other
    sup._sel.register(left, selectors.EVENT_READ, ("conn", other))
    try:
        fence = sup._answer(
            other, _line(cmd="ACQUIRE", v=1, controller_id="again", ttl_s=60, **request)
        )
        assert fence["ok"] is True and fence["token"] != request["token"]
        late = sup._answer(conn, _line(cmd="MAINTENANCE_STOP", v=2, **request))
        assert late == {"ok": False, "error": "stale_token"}
        assert sup._answer(other, _line(cmd="PING", v=1))["process_state"] == "serving"
        assert record_of(sup) is None
    finally:
        sup._drop_conn(other)
        right.close()


@pytest.mark.parametrize(
    "content,match",
    [
        ({"version": 2}, "has version 2"),
        ({"version": True}, "has version True"),
        ({"version": "1"}, "has version '1'"),
        ({}, "has version None"),
        ({"version": 1, "state": "admitted"}, "lacks or mistypes"),
        ({"version": 1, "state": "done"}, "lacks or mistypes"),
        ([1], "not one JSON object"),
        (b"\xff", "not one JSON object"),
        ("dir", "cannot be read"),
    ],
)
def test_every_reader_refuses_a_record_it_cannot_trust(short_root, content, match):
    path = short_root / rs.MAINTENANCE_RECORD
    if content == "dir":
        path.mkdir()
    elif isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(json.dumps(content))
    with pytest.raises(ValueError, match=match):
        rs.read_maintenance_record(short_root)
    # the CLI's rerun meets the same reader: an unknown outcome, never a guess
    result = CliRunner().invoke(app, ["supervise", "shutdown", "--run-root", str(short_root)])
    assert result.exit_code == 4, result.output
    assert match.split("'")[0] in result.output


def _record(pid, start, state="completed", **extra):
    return {
        "version": 1,
        "state": state,
        "incarnation": "a" * 32,
        "supervisor_pid": pid,
        "supervisor_start_time": start,
        "boot_id": procid.current_boot_id(),
        "force": None,
        "bound_s": 0.0,
        "started_at": "2026-10-25T00:00:00+00:00",
        "finished_at": "2026-10-25T00:00:01+00:00",
        "future_field": "ignored",
        **extra,
    }


def _own_note(gone_pid):
    return json.dumps({"version": 1, "incarnation": "a" * 32, "pid": gone_pid})


@pytest.mark.parametrize(
    "state",
    [
        "completed",
        "no_note",
        "empty_note",
        "partial_note",
        "admitted",
        "uncertain",
        "later_boot",
        "later_pid_record",
        "later_lock_note",
        "unreadable_note",
        "unknown_note_version",
        "pid_record_not_an_object",
        "pid_record_unreadable",
    ],
)
def test_a_shutdown_rerun_without_a_socket_reads_the_record(tmp_path, state):
    """No socket and a record: the record's operation decides, and only for the supervisor
    the start note names (DL-401). An exited supervisor whose record says completed, named
    by a well-formed note, is 0. A missing, empty, partial or unparsable note is no
    evidence, and a note or pid record naming a later supervisor is one that ended outside
    maintenance: 4, never the old record's 0. So is anything the record cannot prove."""
    import subprocess

    gone = subprocess.Popen(["true"])
    gone.wait()
    extra = {"detail": "why"} if state == "uncertain" else {}
    base = state if state in ("admitted", "uncertain") else "completed"
    record = _record(gone.pid, "ticks:1", base, **extra)
    if state == "later_boot":
        record["boot_id"] = "another-boot"
    (tmp_path / rs.MAINTENANCE_RECORD).write_text(json.dumps(record))
    note = tmp_path / "supervisor.lock"
    later = {"version": 1, "incarnation": "b" * 32, "pid": 4242}
    if state != "no_note":
        note.write_text(_own_note(gone.pid))
    if state == "empty_note":
        note.write_text("")
    elif state == "partial_note":
        note.write_text(_own_note(gone.pid)[:20])
    elif state == "later_pid_record":
        (tmp_path / "supervisor.pid").write_text(json.dumps(later))
    elif state == "later_lock_note":
        note.write_text(json.dumps(later))
    elif state == "unreadable_note":
        note.write_text("{not json")
    elif state == "pid_record_not_an_object":
        (tmp_path / "supervisor.pid").write_text("[1]")
    elif state == "pid_record_unreadable":
        (tmp_path / "supervisor.pid").mkdir()
    elif state == "unknown_note_version":
        note.write_text(json.dumps({**later, "version": 2}))
    assert rs.read_maintenance_record(tmp_path)["future_field"] == "ignored"
    result = CliRunner().invoke(app, ["supervise", "shutdown", "--run-root", str(tmp_path)])
    assert result.exit_code == (0 if state == "completed" else 4), result.output
    if state == "completed":
        assert set(json.loads(result.output)) == set(record)
    if state.startswith("later_") and state != "later_boot":
        assert "ended outside maintenance: not a maintenance outcome" in result.output
    plain = CliRunner().invoke(app, ["supervise", "list", "--run-root", str(tmp_path)])
    assert plain.exit_code == 2 and "no supervisor at" in plain.output


def test_an_interrupted_start_note_is_no_evidence(short_root, monkeypatch):
    """A start whose note write fails part way leaves a note no reader accepts, never the
    old one whole and never an empty file: the first byte is spoiled before the new note
    is written, and the file is cut to its length last (DL-401). The old record then has
    no evidence: 4, not 0."""
    import subprocess

    gone = subprocess.Popen(["true"])
    gone.wait()
    (short_root / rs.MAINTENANCE_RECORD).write_text(json.dumps(_record(gone.pid, "ticks:1")))
    (short_root / "supervisor.lock").write_text(_own_note(gone.pid))
    real_pwrite = rs.os.pwrite
    calls = []

    def interrupted(fd, data, offset):
        calls.append(offset)
        if len(calls) < 2:
            return real_pwrite(fd, data, offset)
        real_pwrite(fd, data[: len(data) // 2], offset)
        raise OSError("synthetic disk error mid-write")

    for name in ("_install_signals", "_set_subreaper", "_reap"):
        monkeypatch.setattr(rs.Supervisor, name, lambda self: None)
    later = rs.Supervisor(str(short_root), deadman_s=1e-9)
    with monkeypatch.context() as patch:
        patch.setattr(rs.os, "pwrite", interrupted)
        with pytest.raises(OSError, match="mid-write"):
            later.run()
    assert later.state == "closed" and not (short_root / "supervisor.pid").exists()
    with pytest.raises(ValueError):
        json.loads((short_root / "supervisor.lock").read_text())
    result = CliRunner().invoke(app, ["supervise", "shutdown", "--run-root", str(short_root)])
    assert result.exit_code == 4, result.output
    assert "supervisor.lock is not one JSON object" in result.output


def test_a_later_supervisor_that_ends_by_its_deadman_voids_the_old_record(short_root, monkeypatch):
    """The deadman window (DL-401): supervisor A's record says completed; B starts on the
    same root, notes itself in supervisor.lock, and exits by its deadman, which removes its
    socket and pid record. The rerun reads B's note and answers 4, not A's 0. A start leaves
    the record as it found it."""
    import subprocess

    gone = subprocess.Popen(["true"])
    gone.wait()
    record = _record(gone.pid, "ticks:1")
    (short_root / rs.MAINTENANCE_RECORD).write_text(json.dumps(record))
    for name in ("_install_signals", "_set_subreaper", "_reap"):
        monkeypatch.setattr(rs.Supervisor, name, lambda self: None)
    later = rs.Supervisor(str(short_root), deadman_s=1e-9)
    assert later.run() == 0 and later.state == "closed"
    assert later._h_ping(None, {}).get("maintenance") is None
    assert not (short_root / "supervisor.pid").exists()
    note = json.loads((short_root / "supervisor.lock").read_text())
    assert note["incarnation"] == later.incarnation
    assert rs.read_maintenance_record(short_root) == record
    result = CliRunner().invoke(app, ["supervise", "shutdown", "--run-root", str(short_root)])
    assert result.exit_code == 4, result.output
    assert f"names another supervisor ({later.incarnation})" in result.output


def _spawn_over_the_wire(root, command, grace):
    """A run the launched supervisor holds, spawned by a client that then leaves."""
    from dsl41.runner_adapters import SupervisorConn

    from test_runner_supervisor import _spec

    conn = SupervisorConn(root / "supervisor.sock")
    try:
        grant = conn.send({"cmd": "ACQUIRE", "controller_id": "spawner", "ttl_s": 60})
        spec = _spec(root / "runs" / "j.1", command, grace)
        answer = conn.send({"cmd": "SPAWN", "token": grant["token"], "spec": spec})
        assert answer["ok"], answer
    finally:
        conn.close()
    return root / "runs" / "j.1"


def test_forced_maintenance_outlives_the_request_deadline(short_root, capsys):
    """A forced stop whose grace exceeds the client's request deadline completes with
    exit 0 after about the grace: the request is answered at admission, and the client
    then watches the record within `bound_s` (DL-401). The acceptance names a 120 s
    grace against the 60 s deadline; this runs the same shape at 3 s against 1 s."""
    import time

    from dsl41.cli_common import guarded_maintenance
    from dsl41.runner_adapters import SupervisorConn

    from test_runner_supervisor import wait_for

    (short_root / "leader.lock").touch()
    supervisor = _launch(short_root)
    try:
        run_dir = _spawn_over_the_wire(
            short_root, f"trap '' TERM; touch '{short_root}/runs/j.1/ready'; sleep 30", 3.0
        )
        wait_for(lambda: (run_dir / "ready").exists())
        client = SupervisorConn(short_root / "supervisor.sock", timeout_s=1.0)
        started = time.monotonic()
        try:
            code, record = guarded_maintenance(
                client, short_root / "supervisor.sock", controller_id="t", force="terminate"
            )
        finally:
            client.close()
        elapsed = time.monotonic() - started
        assert code == 0, capsys.readouterr().err
        assert record["state"] == "completed" and record["bound_s"] >= 3.0
        assert 3.0 <= elapsed < 3.0 + 10.0
        assert supervisor.wait(timeout=10) == 8
        status = json.loads((run_dir / "status.json").read_text())
        assert status["outcome"] == "signaled" and status.get("reason") != "parent lost"
    finally:
        _reap_launched(supervisor)


_KILLED_CLIENT = """
import json, os, socket, sys
sock = socket.socket(socket.AF_UNIX)
sock.connect(sys.argv[1])
reader = sock.makefile("rb")
sock.sendall(b'{"v": 1, "cmd": "ACQUIRE", "controller_id": "killed", "ttl_s": 60}\\n')
grant = json.loads(reader.readline())
request = {"v": 2, "cmd": "MAINTENANCE_STOP", "token": grant["token"],
           "incarnation": grant["incarnation"], "force": "terminate"}
sock.sendall(json.dumps(request).encode() + b"\\n")
os.kill(os.getpid(), 9)
"""


@pytest.mark.parametrize("client", ["killed", "lost"])
def test_a_lost_reply_is_resolved_without_a_resend_and_the_supervisor_exits_8_once(
    short_root, monkeypatch, capsys, client
):
    """Astra's acceptance 5 (DL-401). `killed`: a client dies right after sending an
    admitted forced MAINTENANCE_STOP, and `supervise shutdown` resolves it from PING and
    maintenance.json, sending no MAINTENANCE_STOP of its own. `lost`: the reply of the
    client's own request is lost, and the one reconnect resolves it. Either way the
    supervisor admits once and exits 8."""
    import subprocess
    import sys

    from dsl41 import runner_adapters
    from dsl41.cli_common import guarded_maintenance

    from test_runner_supervisor import wait_for

    (short_root / "leader.lock").touch()
    supervisor = _launch(short_root)
    sock = short_root / "supervisor.sock"
    try:
        run_dir = _spawn_over_the_wire(
            short_root, f"trap '' TERM; touch '{short_root}/runs/j.1/ready'; sleep 30", 1.0
        )
        wait_for(lambda: (run_dir / "ready").exists())
        sent = []
        real_send = runner_adapters.SupervisorConn.send

        def counted(self, request):
            sent.append(request["cmd"])
            if (
                client == "lost"
                and request["cmd"] == "MAINTENANCE_STOP"
                and sent.count("MAINTENANCE_STOP") == 1
            ):
                payload = {"v": 1, **request, "incarnation": self.incarnation}
                self.conn.sendall(json.dumps(payload).encode("utf-8") + b"\n")
                raise OSError("synthetic lost reply")
            return real_send(self, request)

        monkeypatch.setattr(runner_adapters.SupervisorConn, "send", counted)
        if client == "killed":
            killed = subprocess.run([sys.executable, "-c", _KILLED_CLIENT, str(sock)])
            assert killed.returncode == -signal.SIGKILL
            wait_for(lambda: rs.read_maintenance_record(short_root) is not None)
        conn = runner_adapters.SupervisorConn(sock)
        try:
            code, record = guarded_maintenance(conn, sock, controller_id="t", force="terminate")
        finally:
            conn.close()
        assert code == 0, capsys.readouterr().err
        assert record["state"] == "completed"
        assert sent.count("MAINTENANCE_STOP") == (0 if client == "killed" else 1)
        if client == "lost":
            assert sent == ["PING", "ACQUIRE", "MAINTENANCE_STOP", "ACQUIRE", "PING"]
        assert supervisor.wait(timeout=10) == 8
        log = supervisor.stderr.read().decode()
        assert log.count("supervisor: maintenance admitted") == 1, log
        assert log.count("supervisor: maintenance completed") == 1, log
    finally:
        _reap_launched(supervisor)


@pytest.mark.parametrize("fault", ["after_rename", "before_rename", "unlink_fails"])
def test_a_refused_admission_leaves_no_record_of_its_own(rig, monkeypatch, capsys, fault):
    """A write that fails after its rename would leave an `admitted` record for a refused
    request. The supervisor removes a record of its own incarnation before it refuses; a
    previous operation's record stays; a removal that fails is logged (DL-401)."""
    sup, conn, request, _proof = rig
    path = Path(sup.run_root) / rs.MAINTENANCE_RECORD
    previous = _record(4242, "ticks:1")
    path.write_text(json.dumps(previous))
    real_write, real_unlink = rs.durable_write_json, rs.os.unlink

    def write(target, record):
        if fault != "before_rename":
            real_write(target, record)
        raise OSError("synthetic directory fsync failure")

    def unlink(target):
        if str(target) == str(path):
            raise PermissionError("synthetic unlink failure")
        real_unlink(target)

    monkeypatch.setattr(rs, "durable_write_json", write)
    if fault == "unlink_fails":
        monkeypatch.setattr(rs.os, "unlink", unlink)
    answer = sup._h_maintenance_stop(conn, request)
    assert answer["error"] == "maintenance_refused" and sup.state == "serving"
    err = capsys.readouterr().err
    if fault == "after_rename":
        assert not path.exists()
        assert "removed maintenance.json of a refused admission" in err
    elif fault == "before_rename":
        assert rs.read_maintenance_record(sup.run_root) == previous
    else:
        assert rs.read_maintenance_record(sup.run_root)["incarnation"] == sup.incarnation
        assert "maintenance.json of a refused admission stays: synthetic unlink" in err


def test_a_watch_that_ping_started_never_exits_2(monkeypatch, tmp_path):
    """PING showed an admitted operation: every later failure is an unknown outcome, 4,
    never the refusal's 2 (DL-401). Here the record has a version this reader refuses."""
    from dsl41 import runner_adapters

    (tmp_path / rs.MAINTENANCE_RECORD).write_text(
        json.dumps({**_record(1, "ticks:1"), "version": 2})
    )
    sent = []

    def send(request):
        sent.append(request["cmd"])
        return {"ok": True, "incarnation": "a" * 32, "process_state": "maintenance_stopping"}

    monkeypatch.setattr(
        runner_adapters, "SupervisorConn", lambda _: SimpleNamespace(send=send, close=lambda: None)
    )
    (tmp_path / "supervisor.sock").touch()
    result = CliRunner().invoke(app, ["supervise", "shutdown", "--run-root", str(tmp_path)])
    assert result.exit_code == 4, result.output
    assert "has version 2" in result.output and sent == ["PING"]


def test_failed_outcome_writes_after_teardown_are_4_not_in_progress(rig, monkeypatch):
    """Both outcome writes fail after the owned teardown: the supervisor stays uncertain
    without its socket and the record still says admitted. Nothing more is scheduled, so a
    watch that ends there is 4, not 3: 3 needs a PING that shows the operation running
    (DL-401)."""
    from dsl41.cli_common import ping_probe, settle_maintenance
    from dsl41.runner_adapters import SupervisorConn

    sup, conn, request, _proof = rig
    answer = sup._h_maintenance_stop(conn, request)
    assert answer["maintenance"] == "admitted" and sup.state == "maintenance_completing"

    def write(*_args):
        raise OSError("synthetic lost disk")

    monkeypatch.setattr(rs, "durable_write_json", write)
    sup._maintenance_pass()
    assert sup.state == "maintenance_uncertain" and not Path(sup.sock_path).exists()
    assert record_of(sup)["state"] == "admitted"
    sock = Path(sup.run_root) / "supervisor.sock"
    code, reason, _ = settle_maintenance(
        Path(sup.run_root), sup.incarnation, within_s=0.0, probe=ping_probe(sock, SupervisorConn)
    )
    assert code == 4
    assert "alive without a running operation on its socket (no answer)" in reason
    assert "read supervisor.log before anything else" in reason
