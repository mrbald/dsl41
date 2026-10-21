"""DL-390 maintenance admission, containment, force and delivery boundaries."""

from __future__ import annotations

import json
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
    monkeypatch.setattr(sup, "_maintenance_wait", lambda _: pytest.fail("unexpected termination"))
    answer = sup._h_maintenance_stop(conn, {**request, "force": force})
    assert answer["error"] == "maintenance_refused" and sup.state == "serving"


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
    answer = sup._h_maintenance_stop(conn, request)
    assert answer["maintenance"] == "completed" and sup.state == "maintenance_completed"
    assert answer["incarnation"] == sup.incarnation
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

    def wait(_):
        assert_excluded(Path(sup.run_root) / "leader.lock")
        if failure == "termination":
            raise OSError("unverified command")

    monkeypatch.setattr(sup, "_maintenance_wait", wait)
    if failure == "teardown":
        monkeypatch.setattr(
            sup, "_maintenance_prepare_teardown", lambda _: (_ for _ in ()).throw(OSError("unlink"))
        )
    answer = sup._h_maintenance_stop(conn, {**request, "force": "terminate"})
    assert answer["error"] == "maintenance_uncertain" and sup.state == "maintenance_uncertain"
    assert_excluded(Path(sup.run_root) / "leader.lock")
    for verb in ("MAINTENANCE_STOP", "SPAWN", "SIGNAL", "SHUTDOWN"):
        assert sup._fence(verb, request)["error"] == "maintenance_uncertain"
    assert sup.state == "maintenance_uncertain" and sup._running
    assert sup._h_ping(conn, {})["process_state"] == "maintenance_uncertain"
    assert sup._h_list(conn, {})["process_state"] == "maintenance_uncertain"


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
        ({"ok": False, "error": "maintenance_refused"}, 2),
        ({"ok": False, "error": "maintenance_uncertain"}, 4),
        ({"ok": True}, 4),
        (OSError("partial send or lost reply"), 4),
        (ValueError("malformed reply"), 4),
    ],
)
def test_cli_mixed_version_and_uncertainty_never_fallback(monkeypatch, tmp_path, reply, expected):
    from dsl41 import runner_adapters

    sent = []

    def send(request):
        sent.append(request)
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
    assert [request["cmd"] for request in sent] == ["ACQUIRE", "MAINTENANCE_STOP"]
    assert sent[-1]["force"] is None


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
    answer = sup._h_maintenance_stop(conn, {**request, "force": "terminate"})
    assert answer.get("maintenance") == "completed", answer
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
    answer = sup._h_maintenance_stop(conn, {**request, "force": "terminate"})
    return answer, signals, time.monotonic() - started, child, token, run_dir


def _kill_child(child, token):
    from test_runner_supervisor import wait_for

    if procid.verify_alive(child, token):
        os.kill(child, signal.SIGKILL)
    wait_for(lambda: not procid.verify_alive(child, token))


def test_force_kills_a_term_ignoring_group_member_after_its_leader_exits(rig, monkeypatch):
    answer, signals, _elapsed, child, token, run_dir = _force_group_child(
        rig, monkeypatch, "ignore", 0.5
    )
    try:
        assert answer.get("maintenance") == "completed", answer
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
    rs.Supervisor._maintenance_wait(owner, None)
    assert [sig for sig, _at in sent] == [signal.SIGTERM, signal.SIGKILL]
    assert sent[1][1] >= 1.0
    # A 20 ms cadence would take about 50 samples during the 1 s grace.
    assert 1 <= len(samples) < 20
    assert all(b - a >= rs._MAINTENANCE_SAMPLE_S for a, b in zip(samples, samples[1:]))
    assert len(reap_calls) > 2 * len(samples)


def test_force_waits_for_a_graceful_group_member_without_kill(rig, monkeypatch):
    answer, signals, elapsed, child, token, _run_dir = _force_group_child(
        rig, monkeypatch, "graceful", 5.0
    )
    try:
        assert answer.get("maintenance") == "completed", answer
        assert [sig for _pgid, sig, _at in signals] == [signal.SIGTERM]
        assert elapsed < 5.0
        assert not procid.verify_alive(child, token)
    finally:
        _kill_child(child, token)


def test_force_never_signals_a_contained_member_outside_the_verified_group(rig, monkeypatch):
    sup = rig[0]
    answer, signals, _elapsed, child, token, _run_dir = _force_group_child(
        rig, monkeypatch, "escape", 0.5
    )
    try:
        assert answer["error"] == "maintenance_uncertain", answer
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
    rs.Supervisor._maintenance_wait(owner, None)
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
    monkeypatch.setattr(sup, "_maintenance_wait", lambda _: None)
    try:
        answer = sup._h_maintenance_stop(conn, {**request, "force": "terminate"})
        assert answer["error"] == "maintenance_uncertain"
        os.fstat(write_fd)
        assert run.wrapper_rc is None
        assert_excluded(Path(sup.run_root) / "leader.lock")
    finally:
        os.close(read_fd)


@pytest.mark.parametrize("stale_tail", [None, "chld", "listen"])
@pytest.mark.parametrize("partial_teardown", [False, True])
def test_real_process_completed_maintenance_exits_eight_and_cli_verifies_exit(
    short_root, stale_tail, partial_teardown
):
    import subprocess
    import sys
    from test_runner_supervisor import wait_for

    (short_root / "leader.lock").touch()
    launcher = f"""
import os, sys, selectors
from types import SimpleNamespace
from dsl41 import runner_supervisor as r
r.ServiceContainment.observe = lambda: SimpleNamespace(members=lambda: {{os.getpid()}})
original = r.Supervisor._bind
def bind(self):
    original(self)
    select = self._sel.select
    def batch(*args, **kwargs):
        ready = select(*args, **kwargs)
        if any(key.data[0] == 'conn' and mask & selectors.EVENT_READ for key, mask in ready):
            keys = [key for key in self._sel.get_map().values() if key.data[0] == {stale_tail!r}]
            if keys:
                ready = [pair for pair in ready if pair[0].data[0] != {stale_tail!r}]
                ready.append((keys[0], selectors.EVENT_READ))
        return ready
    self._sel.select = batch
r.Supervisor._bind = bind
if {partial_teardown!r}:
    prepare = r.Supervisor._maintenance_prepare_teardown
    def partial(self, proof):
        prepare(self, proof)
        raise OSError('synthetic error after owned teardown')
    r.Supervisor._maintenance_prepare_teardown = partial
sys.exit(r.main(sys.argv[1:]))
"""
    supervisor = subprocess.Popen(
        [sys.executable, "-c", launcher, "--run-root", str(short_root)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        wait_for(lambda: (short_root / "supervisor.sock").exists())
        cli = subprocess.run(
            [sys.executable, "-m", "dsl41", "supervise", "shutdown", "--run-root", str(short_root)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if partial_teardown:
            assert cli.returncode == 4, cli.stderr
            assert supervisor.poll() is None
            supervisor.terminate()  # deliberate fixture host stop after proving retained uncertainty
            assert supervisor.wait(timeout=5) == 0, supervisor.stderr.read()
        else:
            assert cli.returncode == 0, cli.stderr
            assert json.loads(cli.stdout)["maintenance"] == "completed"
            assert supervisor.wait(timeout=5) == 8, supervisor.stderr.read()
        assert not (short_root / "supervisor.sock").exists()
        assert not (short_root / "supervisor.pid").exists()
    finally:
        if supervisor.poll() is None:
            supervisor.terminate()
        supervisor.wait(timeout=10)
        supervisor.stderr.close()


def test_late_teardown_failure_never_returns_maintenance_success(rig, monkeypatch):
    sup, conn, request, _proof = rig
    real = rs.os.unlink

    def unlink(path):
        if str(path) == sup.sock_path:
            raise PermissionError("synthetic late unlink failure")
        real(path)

    monkeypatch.setattr(rs.os, "unlink", unlink)
    answer = sup._h_maintenance_stop(conn, request)
    assert answer["error"] == "maintenance_uncertain"
    assert sup.state == "maintenance_uncertain"
    assert_excluded(Path(sup.run_root) / "leader.lock")
    assert Path(sup.sock_path).exists()


@pytest.mark.parametrize(
    "which",
    [
        "stays_alive",
        "unobservable",
        "gone",
        "zombie",
        "reused",
        "vanished_during_probe",
        "wait_then_gone",
    ],
)
def test_cli_requires_completion_and_target_process_exit(monkeypatch, tmp_path, which):
    from dsl41 import runner_adapters

    reply = {
        "ok": True,
        "maintenance": "completed",
        "incarnation": "a" * 32,
        "supervisor_pid": 123,
        "supervisor_start_time": "ticks:1",
        "boot_id": procid.current_boot_id(),
    }

    def send(request):
        if request["cmd"] == "ACQUIRE":
            return {"ok": True, "token": 1, "incarnation": "a" * 32}
        return reply

    monkeypatch.setattr(
        runner_adapters, "SupervisorConn", lambda _: SimpleNamespace(send=send, close=lambda: None)
    )

    kill_calls = 0

    def kill(*_):
        nonlocal kill_calls
        kill_calls += 1
        if which == "gone" or (
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
    import time

    ticks = iter([0, 1, 10] if which == "wait_then_gone" else [0, 10])
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    (tmp_path / "supervisor.sock").touch()
    result = CliRunner().invoke(app, ["supervise", "shutdown", "--run-root", str(tmp_path)])
    assert result.exit_code == (4 if which in ("stays_alive", "unobservable") else 0), result.output


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
        answer = sup._h_maintenance_stop(conn, {**request, "force": "terminate"})
        assert answer["error"] == "maintenance_uncertain"
        assert "no command identity" in answer["detail"]
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
        answer = sup._h_maintenance_stop(conn, request)
    assert answer["error"] == "maintenance_uncertain"
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
    rs.Supervisor._maintenance_wait(owner, None)
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
        monkeypatch.setattr(sup, "_maintenance_wait", fail)
    else:
        monkeypatch.setattr(sup, "_maintenance_prepare_teardown", fail)
    with pytest.raises(rs.StatechartError):
        sup._h_maintenance_stop(conn, {**request, "force": "terminate"})
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
    answer = sup._h_maintenance_stop(conn, request)
    if fault == "no_listener":
        assert answer.get("maintenance") == "completed"
    else:
        assert answer["error"] == "maintenance_uncertain"
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
    rs.Supervisor._maintenance_wait(owner, None)
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
        ("RefuseManualStop", "no"),
        ("SuccessExitStatus", ""),
        ("RestartPreventExitStatus", "2 7"),
    ],
)
def test_stale_or_unprotected_unit_policy_refuses_maintenance(
    monkeypatch, tmp_path, property_name, value
):
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
    assert [request["cmd"] for request in sent] == ["ACQUIRE"]


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
        rs.Supervisor._maintenance_wait(owner, None)
    assert sent == [signal.SIGTERM, signal.SIGKILL]
    assert clock.now >= 2


@pytest.mark.parametrize("maintenance", [False, True])
def test_final_reply_transport_failure_does_not_reclassify_successful_maintenance(
    rig, monkeypatch, maintenance
):
    sup, conn, request, _proof = rig
    if maintenance:
        answer = sup._h_maintenance_stop(conn, request)
        assert answer["maintenance"] == "completed"
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
        answer = sup._h_maintenance_stop(conn, {**request, "force": "terminate"})
        assert answer["error"] == "maintenance_uncertain"
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
