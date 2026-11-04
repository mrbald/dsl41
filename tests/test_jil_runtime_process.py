from __future__ import annotations

import asyncio
import json
import os
import resource
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

from dsl41 import runner_adapters as adapters
from dsl41 import runner_procid as procid
from dsl41.ir import lower_source
from dsl41.runner_clock import EngineError
from dsl41.runner_environment import RuntimeVariableError
from dsl41.runner_wrapper import ProcessStream
from dsl41.runtime_policy import ResourceLimit


DRIVER = r"""
import json, os, resource, subprocess, sys
from dataclasses import asdict
from dsl41.runner_wrapper import ProcessInput, ProcessStream, spawn_prepared_process, collect_process_evidence
v = json.load(sys.stdin)
spec = ProcessInput(
    shell=v["shell"], site_profile=v.get("site"), job_profile=v.get("job"),
    command=v["command"], stdin_path=v.get("stdin"),
    stdout=ProcessStream(v["stdout"], v.get("out_mode", "append")),
    stderr=ProcessStream(v["stderr"], v.get("err_mode", "append")),
    limits=tuple(tuple(x) for x in v.get("limits", [])),
)
def process_state():
    mask = os.umask(0)
    os.umask(mask)
    return {"umask": mask, "nofile": resource.getrlimit(resource.RLIMIT_NOFILE)}
before = process_state()
fds = []
try:
    profile_in = os.open(v.get("profile_stdin") or os.devnull, os.O_RDONLY)
    fds.append(profile_in)
    profile_out = os.open(v["profile_stdout"], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    fds.append(profile_out)
    profile_err = os.open(v["profile_stderr"], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    fds.append(profile_err)
    try:
        p = spawn_prepared_process(spec, env=v["env"], cwd=v.get("cwd"),
                                   profile_stdin=profile_in, profile_stdout=profile_out,
                                   profile_stderr=profile_err)
    finally:
        for fd in fds:
            os.close(fd)
        fds.clear()
    result = asdict(collect_process_evidence(p))
except (OSError, subprocess.SubprocessError) as exc:
    for fd in fds:
        os.close(fd)
    result = {"stage": "spawn_error", "command_reached": False, "returncode": None,
              "error_type": type(exc).__name__, "error": str(exc)}
result["parent_before"] = before
result["parent_after"] = process_state()
print(json.dumps(result))
"""


def run_process(tmp_path: Path, command: str, **kwargs: object) -> dict:
    values: dict = {
        "shell": "/bin/sh",
        "command": command,
        "stdout": str(tmp_path / "out"),
        "stderr": str(tmp_path / "err"),
        "profile_stdout": str(tmp_path / "profile.out"),
        "profile_stderr": str(tmp_path / "profile.err"),
        "env": {**os.environ, "SHELL": "/invalid/inherited-shell"},
    }
    values.update(kwargs)
    proc = subprocess.run(
        [sys.executable, "-c", DRIVER],
        input=json.dumps(values),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if proc.returncode:
        pytest.fail(
            f"driver exited {proc.returncode}: stdout={proc.stdout!r}; stderr={proc.stderr!r}"
        )
    return json.loads(proc.stdout)


def job(text: str):
    return lower_source(f"insert_job: p\njob_type: c\ncommand: true\n{text}\n").jobs["p"]


def prepared(j, tmp_path: Path, env: dict[str, str], *, mode="append"):
    return adapters.prepare_process_input(
        j,
        shell="/bin/sh",
        site_profile=None,
        mode=mode,
        limits=(ResourceLimit("n", 64, 64),),
        env=env,
        cwd=tmp_path,
        defaults={"std_out_file": "default.out", "std_err_file": "default.err"},
    )


def test_prepare_literal_operators_before_single_expansion(tmp_path: Path):
    j = job("std_out_file: >$TARGET\nstd_err_file: >>relative")
    value, refs = prepared(j, tmp_path, {"TARGET": ">literal"})
    assert value.stdout == ProcessStream(str(tmp_path / ">literal"), "truncate")
    assert value.stderr == ProcessStream(str(tmp_path / "relative"), "append")
    assert value.limits == ((resource.RLIMIT_NOFILE, 64, 64),)
    assert refs == {"TARGET"}
    value, _ = prepared(job("std_out_file: $TARGET"), tmp_path, {"TARGET": ">name"})
    assert value.stdout == ProcessStream(str(tmp_path / ">name"), "append")


def test_prepare_preserves_refusal_and_defaults(tmp_path: Path):
    value, refs = prepared(job(""), tmp_path, {}, mode="truncate")
    assert value.stdout == ProcessStream(str(tmp_path / "default.out"), "truncate")
    assert value.stdin_path is None and refs == set()
    with pytest.raises(RuntimeVariableError, match="reserved"):
        prepared(job("std_out_file: $__job_name"), tmp_path, {"__job_name": "wrong"})
    with pytest.raises(EngineError, match="CMD"):
        prepared(
            lower_source("insert_job: f\njob_type: f\nwatch_file: /tmp/x\n").jobs["f"], tmp_path, {}
        )


@pytest.mark.parametrize("shell", ["/bin/sh", shutil.which("bash") or "/no-bash"])
def test_profiles_limits_umask_and_selected_shell(tmp_path: Path, shell: str):
    if not os.access(shell, os.X_OK):
        pytest.skip(f"qualified shell unavailable: {shell}")
    site, profile = tmp_path / "site", tmp_path / "profile"
    order = tmp_path / "order"
    site.write_text(
        f'printf site > "{order}"\nulimit -n >> "{order}"\nexport VALUE=site\numask 027\nset -- altered\n'
    )
    profile.write_text(f'printf job >> "{order}"\nexport VALUE=job\n')
    before = resource.getrlimit(resource.RLIMIT_NOFILE)
    if before[1] != resource.RLIM_INFINITY and before[1] < 64:
        pytest.skip("hard descriptor limit is below the witness")
    result = run_process(
        tmp_path,
        'printf "%s:" "$VALUE"; ulimit -n',
        shell=shell,
        site=str(site),
        job=str(profile),
        limits=[
            (resource.RLIMIT_NOFILE, 64, before[1] if before[1] != resource.RLIM_INFINITY else None)
        ],
    )
    assert result["stage"] == "unknown" and result["command_reached"] is None
    assert result["returncode"] == 0
    assert result["parent_before"] == result["parent_after"]
    assert order.read_text() == "site64\njob"
    assert (tmp_path / "out").read_text().strip() == "job:64"
    assert (tmp_path / "out").stat().st_mode & 0o777 == 0o640
    assert resource.getrlimit(resource.RLIMIT_NOFILE) == before


@pytest.mark.parametrize("failed", ["site", "job"])
def test_profile_failure_prevents_command_and_open(tmp_path: Path, failed: str):
    site, profile = tmp_path / "site", tmp_path / "profile"
    site.write_text("false\n" if failed == "site" else "true\n")
    profile.write_text("false\n" if failed == "job" else "true\n")
    marker = tmp_path / "command"
    result = run_process(tmp_path, f"touch '{marker}'", site=str(site), job=str(profile))
    assert result["stage"] == "unknown" and result["command_reached"] is None
    assert result["returncode"] != 0
    assert not marker.exists() and not (tmp_path / "out").exists()


def test_open_failure_preserves_status_and_no_command_effect(tmp_path: Path):
    marker = tmp_path / "command"
    result = run_process(tmp_path, f"touch '{marker}'", stdout=str(tmp_path / "missing" / "out"))
    assert result["stage"] == "unknown" and result["returncode"] != 0
    assert result["command_reached"] is None and not marker.exists()


def test_limit_failure_is_child_setup_failure(tmp_path: Path):
    marker = tmp_path / "command"
    result = run_process(tmp_path, f"touch '{marker}'", limits=[(-1, 0, 0)])
    assert result["stage"] == "spawn_error" and result["command_reached"] is False
    assert result["returncode"] is None and result["error_type"] == "SubprocessError"
    assert not marker.exists() and not (tmp_path / "out").exists()


def test_existing_mode_and_shared_target(tmp_path: Path):
    shared = tmp_path / "shared"
    shared.write_text("previous\n")
    shared.chmod(0o604)
    result = run_process(
        tmp_path,
        "printf out; printf err >&2",
        stdout=str(shared),
        stderr=str(shared),
        out_mode="append",
        err_mode="append",
    )
    assert result["returncode"] == 0
    assert shared.read_text() == "previous\nouterr"
    assert shared.stat().st_mode & 0o777 == 0o604


def test_shell_exec_refusal_and_command_exit_are_distinct(tmp_path: Path):
    missing = run_process(tmp_path, "true", shell=str(tmp_path / "missing"))
    assert missing["stage"] == "spawn_error" and missing["command_reached"] is False
    assert missing["returncode"] is None and missing["error_type"] == "FileNotFoundError"
    result = run_process(tmp_path, "exit 23")
    assert result["stage"] == "unknown" and result["command_reached"] is None
    assert result["returncode"] == 23


def test_no_shebang_selected_shell_and_shebang_precedence(tmp_path: Path):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("real bash unavailable")
    script = tmp_path / "script"
    script.write_text(
        'if [ -n "${BASH_VERSION-}" ] && ! shopt -oq posix; '
        "then printf native-bash; else printf other; fi\n"
    )
    script.chmod(0o755)
    result = run_process(tmp_path, str(script), shell=bash)
    assert result["returncode"] == 0 and (tmp_path / "out").read_text() == "native-bash"
    control = run_process(tmp_path, str(script), shell="/bin/sh", out_mode="truncate")
    assert control["returncode"] == 0 and (tmp_path / "out").read_text() != "native-bash"
    script.write_text(
        '#!/bin/sh\nif [ -n "${BASH_VERSION-}" ] && ! shopt -oq posix; '
        "then printf native-bash; else printf other; fi\n"
    )
    baseline = subprocess.run(["/bin/sh", str(script)], capture_output=True, text=True, timeout=10)
    wrong = subprocess.run([bash, str(script)], capture_output=True, text=True, timeout=10)
    result = run_process(tmp_path, str(script), shell=bash, out_mode="truncate")
    assert baseline.returncode == wrong.returncode == result["returncode"] == 0
    assert baseline.stdout == "other" and wrong.stdout == "native-bash"
    assert (tmp_path / "out").read_text() == baseline.stdout


def test_file_observations_and_recovery(tmp_path: Path):
    a, b, hidden = (tmp_path / name for name in ("a.dat", "b.dat", ".hidden.dat"))
    a.write_text("1234")
    b.write_text("12")
    hidden.write_text("1234")
    log = tmp_path / adapters.WATCH_LOG
    now = datetime(2026, 1, 1)
    adapters.append_watch_line(
        log, {"artifact_format_version": 1, "kind": "start", "at": now.isoformat(), "run_id": "run"}
    )
    first = adapters.observe_pattern(str(tmp_path / "*.dat"), 4)
    assert [Path(item.path).name for item in first] == ["a.dat"]
    state = adapters.append_pattern_poll(
        log, "run", now, adapters.PatternWatchProgress(), first, immediate=False
    )
    assert not state.complete
    saved = adapters.read_watch_log(tmp_path).pattern
    assert saved == state
    replacement = tmp_path / "replacement"
    replacement.write_text("1234")
    replacement.replace(a)
    after = adapters.observe_pattern(str(tmp_path / "*.dat"), 4)
    assert first[0].inode != after[0].inode or first[0].device != after[0].device
    state = adapters.append_pattern_poll(log, "run", now, saved, after, immediate=False)
    assert not state.complete and state.stable[str(a)] == 1
    a.unlink()
    state = adapters.append_pattern_poll(log, "run", now, state, (), immediate=False)
    assert not state.previous
    b.write_text("1234")
    obs = adapters.observe_pattern(str(tmp_path / "*.dat"), 4)
    state = adapters.append_pattern_poll(log, "run", now, state, obs, immediate=False)
    state = adapters.append_pattern_poll(log, "run", now, state, obs, immediate=False)
    assert state.complete and adapters.read_watch_log(tmp_path).pattern == state
    assert [
        Path(item.path).name for item in adapters.observe_pattern(str(tmp_path / ".*.dat"), 4)
    ] == [".hidden.dat"]


def test_watcher_first_poll_and_durable_before_progress(tmp_path: Path, monkeypatch):
    entry = adapters.FileObservation("/a", 1, 2, 3, 100, 100)
    prior = adapters.PatternWatchProgress()
    assert adapters.fold_pattern_poll(prior, (entry,), immediate=True).complete

    def fail(*args):
        raise OSError("fsync failure")

    monkeypatch.setattr(adapters, "append_watch_line", fail)
    with pytest.raises(OSError, match="fsync failure"):
        adapters.append_pattern_poll(
            tmp_path / "log", "r", datetime.now(), prior, (entry,), immediate=False
        )
    assert prior == adapters.PatternWatchProgress()


def test_watcher_bad_and_torn_evidence(tmp_path: Path):
    log = tmp_path / adapters.WATCH_LOG
    start = {
        "artifact_format_version": 1,
        "kind": "start",
        "run_id": "r",
        "at": "2026-01-01T00:00:00",
    }
    log.write_text(json.dumps(start) + '\n{"kind":')
    assert adapters.read_watch_log(tmp_path).watch_seq == 1
    log.write_text(json.dumps(start) + '\n{"artifact_format_version":1,"run_id":"foreign"}\n')
    with pytest.raises(EngineError, match="run_id"):
        adapters.read_watch_log(tmp_path)
    bad = {
        "artifact_format_version": 1,
        "kind": "sample",
        "run_id": "r",
        "at": start["at"],
        "files": [
            {"path": "/a", "device": True, "inode": 2, "size": 3, "mtime_ns": 10, "ctime_ns": 10}
        ],
    }
    log.write_text(json.dumps(start) + "\n" + json.dumps(bad) + "\n")
    with pytest.raises(EngineError, match="observation"):
        adapters.read_watch_log(tmp_path)


def test_selected_ladder_real_target_and_spacing():
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal,time; signal.signal(signal.SIGINT,signal.SIG_IGN); print('ready',flush=True); time.sleep(30)",
        ],
        stdout=subprocess.PIPE,
        start_new_session=True,
        text=True,
    )
    assert process.stdout is not None and process.stdout.readline().strip() == "ready"
    token = procid.proc_start_token(process.pid)
    assert token is not None
    start = time.monotonic()
    try:
        asyncio.run(
            adapters.selected_signal_ladder(process.pid, token, (signal.SIGINT, signal.SIGKILL))
        )
        assert process.wait(timeout=5) == -signal.SIGKILL
        assert time.monotonic() - start >= 5.0
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()


def test_selected_ladder_guards_and_unknown(monkeypatch):
    sent = []
    monkeypatch.setattr(
        procid, "signal_ours", lambda pid, token, sig, **kw: sent.append((pid, token, sig)) or True
    )
    asyncio.run(
        adapters.selected_signal_ladder(
            1, "old", (9,), lambda: procid.Left("ours", {123: "token"}), pidfd=False
        )
    )
    assert sent == [(123, "token", 9)]
    with pytest.raises(OSError, match="unknown"):
        asyncio.run(
            adapters.selected_signal_ladder(
                1, "old", (9,), lambda: procid.Left("unknown", reason="unknown")
            )
        )
    sent.clear()
    asyncio.run(adapters.selected_signal_ladder(1, "old", (9,), lambda: procid.NOTHING_LEFT))
    assert not sent


def test_shell_command_sees_no_private_positional_arguments(tmp_path: Path):
    result = run_process(tmp_path, 'printf "%s:%s" "$#" "$1"')
    assert result["returncode"] == 0
    assert (tmp_path / "out").read_text() == "0:"


def test_pid_token_guard_leaves_real_unrelated_process_alive():
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert not procid.signal_ours(
            process.pid, "not-the-start-token", signal.SIGKILL, pidfd=False
        )
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_signal_ladder_stops_after_early_exit():
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; print('ready',flush=True); time.sleep(30)"],
        stdout=subprocess.PIPE,
        start_new_session=True,
        text=True,
    )
    assert process.stdout is not None and process.stdout.readline().strip() == "ready"
    token = procid.proc_start_token(process.pid)
    assert token is not None
    start = time.monotonic()
    try:
        asyncio.run(
            adapters.selected_signal_ladder(process.pid, token, (signal.SIGTERM, signal.SIGKILL))
        )
        assert process.wait(timeout=5) == -signal.SIGTERM
        assert time.monotonic() - start < 5.0
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()


def test_korn_profile_and_no_shebang(tmp_path: Path):
    shell = shutil.which("ksh")
    if shell is None:
        pytest.skip("real Korn shell unavailable; Linux qualification is separate")
    baseline = subprocess.run(
        [shell, "-c", 'printf "%s" "${.sh.version}"'],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert baseline.stdout
    site = tmp_path / "site"
    marker = tmp_path / "profile-version"
    site.write_text(f'printf "%s" "${{.sh.version}}" > "{marker}"\n')
    script = tmp_path / "script"
    script.write_text('printf "%s" "${.sh.version}"\n')
    script.chmod(0o755)
    result = run_process(tmp_path, str(script), shell=shell, site=str(site))
    assert result["stage"] == "unknown" and result["returncode"] == 0
    assert marker.read_text() == baseline.stdout
    assert (tmp_path / "out").read_text() == baseline.stdout
    wrong = run_process(tmp_path, str(script), shell="/bin/sh", out_mode="truncate")
    assert wrong["returncode"] != 0 or (tmp_path / "out").read_text() != baseline.stdout


def test_profile_closing_fd_three_still_runs_command(tmp_path: Path):
    profile = tmp_path / "profile"
    profile.write_text("exec 3>&-\n")
    marker = tmp_path / "command"
    result = run_process(tmp_path, f"touch '{marker}'", site=str(profile))
    assert result["stage"] == "unknown" and result["command_reached"] is None
    assert result["returncode"] == 0 and marker.exists()


def test_stdin_and_truncate(tmp_path: Path):
    source = tmp_path / "input"
    source.write_text("from input")
    (tmp_path / "out").write_text("old")
    result = run_process(tmp_path, "cat", stdin=str(source), out_mode="truncate")
    assert result["returncode"] == 0 and (tmp_path / "out").read_text() == "from input"


def test_explicit_unlimited_limit(tmp_path: Path):
    hard = resource.getrlimit(resource.RLIMIT_CORE)[1]
    if hard != resource.RLIM_INFINITY:
        pytest.skip("inherited hard core limit is finite")
    result = run_process(tmp_path, "ulimit -c", limits=[(resource.RLIMIT_CORE, None, None)])
    assert result["returncode"] == 0
    assert (tmp_path / "out").read_text().strip() == "unlimited"
    assert result["parent_before"] == result["parent_after"]


def test_background_profile_process_does_not_block_wait(tmp_path: Path):
    profile = tmp_path / "profile"
    pid_file = tmp_path / "background.pid"
    profile.write_text(f'sleep 30 </dev/null >/dev/null 2>&1 &\nprintf "%s" "$!" > "{pid_file}"\n')
    start = time.monotonic()
    try:
        result = run_process(tmp_path, "true", site=str(profile))
        assert result["stage"] == "unknown" and result["returncode"] == 0
        assert time.monotonic() - start < 10.0
    finally:
        if pid_file.exists():
            pid = int(pid_file.read_text())
            token = procid.proc_start_token(pid)
            sent = token is not None and procid.signal_ours(pid, token, signal.SIGKILL, pidfd=False)
            try:
                os.waitpid(pid, 0 if sent else os.WNOHANG)
            except ChildProcessError:
                pass


def test_child_limit_application_and_failure(monkeypatch):
    from dsl41 import runner_wrapper as wrapper

    calls = []
    monkeypatch.setattr(wrapper, "_restore_default_signals", lambda: calls.append("signals"))
    monkeypatch.setattr(
        wrapper.resource, "setrlimit", lambda kind, pair: calls.append((kind, pair))
    )
    wrapper._prepare_process_child(((resource.RLIMIT_NOFILE, 42, None),))
    assert calls == ["signals", (resource.RLIMIT_NOFILE, (42, resource.RLIM_INFINITY))]

    def fail(*args):
        raise ValueError("invalid limit")

    monkeypatch.setattr(wrapper.resource, "setrlimit", fail)
    with pytest.raises(ValueError, match="invalid limit"):
        wrapper._prepare_process_child(((resource.RLIMIT_NOFILE, None, None),))


def test_signal_ladder_target_failures(monkeypatch):
    with pytest.raises(ValueError, match="empty"):
        asyncio.run(adapters.selected_signal_ladder(1, "token", ()))
    with pytest.raises(OSError, match="no per-process targets"):
        asyncio.run(adapters.selected_signal_ladder(1, "token", (9,), lambda: procid.Left("ours")))
    monkeypatch.setattr(procid, "signal_ours", lambda *args, **kwargs: False)
    monkeypatch.setattr(procid, "scanned_process_gone", lambda *args: True)
    asyncio.run(
        adapters.selected_signal_ladder(1, "token", (9,), lambda: procid.Left("ours", {2: "old"}))
    )
    monkeypatch.setattr(procid, "scanned_process_gone", lambda *args: False)
    with pytest.raises(OSError, match="not sent"):
        asyncio.run(
            adapters.selected_signal_ladder(
                1, "token", (9,), lambda: procid.Left("ours", {2: "old"})
            )
        )

    def failed_read(*args):
        raise OSError("permission denied")

    monkeypatch.setattr(procid, "scanned_process_gone", failed_read)
    with pytest.raises(OSError, match="identity read failed"):
        asyncio.run(
            adapters.selected_signal_ladder(
                1, "token", (9,), lambda: procid.Left("ours", {2: "old"})
            )
        )


def test_signal_ladder_group_requires_leader_proof(monkeypatch):
    calls = []
    monkeypatch.setattr(procid, "verify_alive", lambda *args: False)
    monkeypatch.setattr(procid, "killpg_quiet", lambda *args: calls.append(args))
    asyncio.run(adapters.selected_signal_ladder(123, "wrong", (9,)))
    assert calls == []
    monkeypatch.setattr(procid, "verify_alive", lambda *args: True)
    monkeypatch.setattr(adapters, "group_has_live_member", lambda *args: False)
    asyncio.run(adapters.selected_signal_ladder(123, "right", (9,)))
    assert calls == []


def test_pattern_observe_race_and_duplicate(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(adapters.glob, "iglob", lambda pattern: iter(["/vanished"]))

    def gone(path):
        raise FileNotFoundError(path)

    monkeypatch.setattr(adapters.os, "stat", gone)
    assert adapters.observe_pattern("/irrelevant/*", 0) == ()
    item = adapters.FileObservation("/a", 1, 2, 3, 100, 100)
    with pytest.raises(EngineError, match="duplicate"):
        adapters.fold_pattern_poll(adapters.PatternWatchProgress(), (item, item), immediate=False)


@pytest.mark.parametrize(
    "record, error",
    [
        ({"kind": "sample", "run_id": "r", "at": "2026-01-01", "files": None}, "sample files"),
        (
            {"kind": "sample", "run_id": "r", "at": "2026-01-01", "files": [{"path": ""}]},
            "invalid file",
        ),
        ({"kind": "sample", "run_id": "r", "at": "bad", "files": []}, "timestamp"),
        ({"kind": "wrong", "run_id": "r", "at": "2026-01-01", "files": []}, "kind"),
        ({"kind": "sample", "run_id": "r", "at": "2026-01-01", "files": [None]}, "invalid file"),
        (
            {
                "kind": "sample",
                "run_id": "r",
                "at": "2026-01-01",
                "files": [
                    {
                        "path": "/x",
                        "device": 1,
                        "inode": 2,
                        "size": -1,
                        "mtime_ns": 10,
                        "ctime_ns": 10,
                    }
                ],
            },
            "observation",
        ),
    ],
)
def test_pattern_rejects_malformed_candidate(tmp_path: Path, record: dict, error: str):
    log = tmp_path / adapters.WATCH_LOG
    start = {
        "artifact_format_version": 1,
        "kind": "start",
        "run_id": "r",
        "at": "2026-01-01T00:00:00",
    }
    record = {"artifact_format_version": 1, **record}
    log.write_text(json.dumps(start) + "\n" + json.dumps(record) + "\n")
    with pytest.raises(EngineError, match=error):
        adapters.read_watch_log(tmp_path)


def test_pattern_rejects_unreadable_and_nonstart_logs(tmp_path: Path):
    path = tmp_path / adapters.WATCH_LOG
    assert adapters.read_watch_log(tmp_path) is None
    path.mkdir()
    with pytest.raises(EngineError, match="unreadable"):
        adapters.read_watch_log(tmp_path)
    path.rmdir()
    path.write_text(
        '{"artifact_format_version":1,"kind":"sample","run_id":"r","at":"2026-01-01"}\n'
    )
    with pytest.raises(EngineError, match="first line"):
        adapters.read_watch_log(tmp_path)
    path.write_text(
        '{"artifact_format_version":1,"kind":"start","run_id":"r","at":"2026-01-01"}\n\n'
    )
    with pytest.raises(EngineError, match="empty interior"):
        adapters.read_watch_log(tmp_path)


def test_prepare_requires_default_output_paths(tmp_path: Path):
    with pytest.raises(EngineError, match="output paths"):
        adapters.prepare_process_input(
            job(""),
            shell="/bin/sh",
            site_profile=None,
            mode="append",
            limits=(),
            env={},
            cwd=tmp_path,
            defaults={},
        )


def test_pattern_fence_checked_before_write(tmp_path: Path, monkeypatch):
    calls = []

    class Fence:
        def check(self):
            assert not log.exists()
            calls.append("checked")

    log = tmp_path / adapters.WATCH_LOG
    state = adapters.append_pattern_poll(
        log,
        "r",
        datetime.now(),
        adapters.PatternWatchProgress(),
        (),
        immediate=False,
        fence=Fence(),
    )
    assert calls == ["checked"] and state.polls == 1 and log.exists()
    before = log.read_bytes()

    def refuse():
        raise EngineError("lost fence")

    monkeypatch.setattr(Fence, "check", lambda self: refuse())
    with pytest.raises(EngineError, match="lost fence"):
        adapters.append_pattern_poll(
            log, "r", datetime.now(), state, (), immediate=False, fence=Fence()
        )
    assert log.read_bytes() == before


def test_pattern_bad_timestamp_empty_and_interior_corruption(tmp_path: Path):
    path = tmp_path / adapters.WATCH_LOG
    path.write_bytes(b"")
    assert adapters.read_watch_log(tmp_path) is None
    start = {"artifact_format_version": 1, "kind": "start", "run_id": "r", "at": 4}
    path.write_text(json.dumps(start) + "\n")
    with pytest.raises(EngineError, match="timestamp"):
        adapters.read_watch_log(tmp_path)
    path.write_text(
        '{"artifact_format_version":1,"kind":"start","run_id":"r","at":"2026-01-01"}\n{bad}\n'
    )
    with pytest.raises(EngineError, match="line"):
        adapters.read_watch_log(tmp_path)


def test_wait_status_does_not_invent_stage():
    from dsl41.runner_wrapper import collect_process_evidence

    class Exited:
        def wait(self):
            return 7

    result = collect_process_evidence(Exited())
    assert result.returncode == 7
    assert result.stage == "unknown" and result.command_reached is None


def test_profile_can_lower_child_limit(tmp_path: Path):
    hard = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    if hard != resource.RLIM_INFINITY and hard < 64:
        pytest.skip("hard descriptor limit is below the witness")
    profile = tmp_path / "profile"
    profile.write_text("ulimit -n 48\n")
    result = run_process(
        tmp_path,
        "ulimit -n",
        site=str(profile),
        limits=[(resource.RLIMIT_NOFILE, 64, hard if hard != resource.RLIM_INFINITY else None)],
    )
    assert result["returncode"] == 0
    assert (tmp_path / "out").read_text().strip() == "48"
    assert result["parent_before"] == result["parent_after"]


def test_scaled_file_limit_reaches_command(tmp_path: Path):
    from dsl41.runtime_policy import parse_limits

    hard = resource.getrlimit(resource.RLIMIT_FSIZE)[1]
    if hard != resource.RLIM_INFINITY:
        pytest.skip("hard file-size limit is finite")
    parsed = parse_limits("p", 'f="1,unlimited"')
    assert parsed == (ResourceLimit("f", 512, None),)
    command = f"""{sys.executable} -S -c 'import resource; print(resource.getrlimit(resource.RLIMIT_FSIZE)[0])' """
    result = run_process(
        tmp_path, command, limits=[(resource.RLIMIT_FSIZE, parsed[0].soft, parsed[0].hard)]
    )
    assert result["returncode"] == 0
    assert (tmp_path / "out").read_text().strip() == "512"


def test_concurrent_children_keep_distinct_umasks(tmp_path: Path):
    from concurrent.futures import ThreadPoolExecutor

    left, right = tmp_path / "left", tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "profile").write_text("umask 077\n")
    (right / "profile").write_text("umask 022\n")

    def launch(path):
        return run_process(path, "printf x", site=str(path / "profile"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(launch, (left, right)))
    assert all(item["returncode"] == 0 for item in results)
    assert (left / "out").stat().st_mode & 0o777 == 0o600
    assert (right / "out").stat().st_mode & 0o777 == 0o644
    assert all(item["parent_before"] == item["parent_after"] for item in results)


def test_shared_target_preserves_separate_stream_offsets(tmp_path: Path):
    shared = tmp_path / "shared"
    shared.write_text("old")
    result = run_process(
        tmp_path,
        "printf ABC; printf X >&2",
        stdout=str(shared),
        stderr=str(shared),
        out_mode="append",
        err_mode="truncate",
    )
    assert result["returncode"] == 0
    assert shared.read_text() == "XBC"


def test_sampled_watcher_collision_and_same_signature_limit():
    before = adapters.FileObservation("/watch", 1, 2, 8, 100, 100)
    changed = adapters.FileObservation("/watch", 1, 2, 8, 101, 102)
    initial = adapters.fold_pattern_poll(
        adapters.PatternWatchProgress(), (before,), immediate=False
    )
    replaced = adapters.fold_pattern_poll(initial, (changed,), immediate=False)
    assert not replaced.complete and replaced.stable["/watch"] == 1
    indistinguishable = adapters.FileObservation("/watch", 1, 2, 8, 100, 100)
    assert adapters.fold_pattern_poll(initial, (indistinguishable,), immediate=False).complete


@pytest.mark.parametrize("change", ["write", "chmod", "utime"])
def test_sampled_watcher_same_size_metadata_changes_reset(tmp_path: Path, change: str):
    watched = tmp_path / "watch"
    watched.write_bytes(b"1234")
    first = adapters.observe_pattern(str(watched), 4)
    assert len(first) == 1
    state = adapters.fold_pattern_poll(adapters.PatternWatchProgress(), first, immediate=False)
    if change == "write":
        watched.write_bytes(b"4321")
    elif change == "chmod":
        watched.chmod((watched.stat().st_mode & 0o777) ^ 0o100)
    else:
        stat = watched.stat()
        os.utime(watched, ns=(stat.st_atime_ns, stat.st_mtime_ns - 5_000_000_000))
    second = adapters.observe_pattern(str(watched), 4)
    assert second[0].size == first[0].size
    assert (second[0].mtime_ns, second[0].ctime_ns) != (first[0].mtime_ns, first[0].ctime_ns)
    reset = adapters.fold_pattern_poll(state, second, immediate=False)
    assert not reset.complete and reset.stable[str(watched)] == 1


def test_sampled_watcher_independent_paths_and_immediate_first_poll():
    a = adapters.FileObservation("/a", 1, 2, 8, 100, 100)
    b = adapters.FileObservation("/b", 1, 3, 8, 100, 100)
    changed_a = adapters.FileObservation("/a", 1, 2, 8, 100, 101)
    empty = adapters.PatternWatchProgress()
    first = adapters.fold_pattern_poll(empty, (a, b), immediate=False)
    second = adapters.fold_pattern_poll(first, (changed_a, b), immediate=False)
    assert second.complete and second.stable == {"/a": 1, "/b": 2}
    assert adapters.fold_pattern_poll(empty, (a,), immediate=True).complete
    missing = adapters.fold_pattern_poll(empty, (), immediate=True)
    late = adapters.fold_pattern_poll(missing, (a,), immediate=True)
    assert not late.complete
    assert adapters.fold_pattern_poll(late, (a,), immediate=True).complete


def test_sampled_watcher_restart_and_strict_replay(tmp_path: Path):
    log = tmp_path / adapters.WATCH_LOG
    at = datetime(2026, 1, 1)
    adapters.append_watch_line(
        log, {"artifact_format_version": 1, "kind": "start", "run_id": "r", "at": at.isoformat()}
    )
    first = adapters.FileObservation("/watch", 1, 2, 8, -10, 100)
    state = adapters.append_pattern_poll(
        log, "r", at, adapters.PatternWatchProgress(), (first,), immediate=False
    )
    restored = adapters.read_watch_log(tmp_path).pattern
    assert restored == state
    assert adapters.fold_pattern_poll(restored, (first,), immediate=False).complete
    changed = adapters.FileObservation("/watch", 1, 2, 8, -10, 101)
    assert not adapters.fold_pattern_poll(restored, (changed,), immediate=False).complete
    row = json.loads(log.read_text().splitlines()[1])
    assert row["at"] == "2026-01-01T00:00:00.000000"
    assert row["kind"] == "sample" and row["artifact_format_version"] == 1
    assert row["files"] == [
        {"path": "/watch", "device": 1, "inode": 2, "size": 8, "mtime_ns": -10, "ctime_ns": 100}
    ]
    del row["files"][0]["ctime_ns"]
    log.write_text(
        json.dumps(
            {"artifact_format_version": 1, "kind": "start", "run_id": "r", "at": at.isoformat()}
        )
        + "\n"
        + json.dumps(row)
        + "\n"
    )
    with pytest.raises(EngineError, match="invalid file observation"):
        adapters.read_watch_log(tmp_path)


@pytest.mark.parametrize(
    "shell", ["/bin/sh", shutil.which("bash") or "/no-bash", shutil.which("ksh") or "/no-ksh"]
)
@pytest.mark.parametrize("where", ["site", "job"])
@pytest.mark.parametrize("status", [0, 23])
def test_closed_fd_profile_effects_and_unknown_attribution(
    tmp_path: Path, shell: str, where: str, status: int
):
    if not os.access(shell, os.X_OK):
        pytest.skip(f"real shell unavailable: {shell}")
    profile = tmp_path / "profile"
    profile.write_text(f"exec 3>&-\nreturn {status}\n")
    marker, baseline_marker = tmp_path / "marker", tmp_path / "baseline-marker"
    baseline = subprocess.run(
        [shell, "-c", '. "$1" && touch "$2"', shell, str(profile), str(baseline_marker)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    result = run_process(tmp_path, f"touch '{marker}'", shell=shell, **{where: str(profile)})
    assert baseline.returncode == result["returncode"] == status
    assert result["stage"] == "unknown" and result["command_reached"] is None
    assert marker.exists() is baseline_marker.exists() is (status == 0)


@pytest.mark.parametrize(
    "shell,setting",
    [
        ("/bin/sh", "ulimit -f 0"),
        ("/bin/sh", "ulimit -u 0"),
        ("/bin/bash", "ulimit -u 0"),
        (shutil.which("ksh") or "/no-ksh", "ulimit -n 4"),
    ],
)
def test_profile_limits_match_uninstrumented_shell(tmp_path: Path, shell: str, setting: str):
    if not os.access(shell, os.X_OK):
        pytest.skip(f"real shell unavailable: {shell}")
    if setting == "ulimit -u 0":
        probe = subprocess.run([shell, "-c", "ulimit -u"], capture_output=True, timeout=10)
        if probe.returncode:
            pytest.skip(f"{shell} does not implement ulimit -u")
    profile = tmp_path / "profile"
    profile.write_text(setting + "\n")
    baseline_out, baseline_err = tmp_path / "baseline.out", tmp_path / "baseline.err"
    baseline = subprocess.run(
        [
            shell,
            "-c",
            '. "$1" && exec 1>"$2" 2>"$3" && true',
            shell,
            str(profile),
            str(baseline_out),
            str(baseline_err),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    result = run_process(
        tmp_path, "true", shell=shell, site=str(profile), out_mode="truncate", err_mode="truncate"
    )
    assert baseline.returncode == 0 and result["returncode"] == baseline.returncode
    assert baseline_out.exists() and baseline_err.exists()
    assert (tmp_path / "out").exists() and (tmp_path / "err").exists()
    assert (tmp_path / "out").read_bytes() == baseline_out.read_bytes() == b""
    assert (tmp_path / "err").read_bytes() == baseline_err.read_bytes() == b""
    assert result["stage"] == "unknown" and result["command_reached"] is None
    assert result["parent_before"] == result["parent_after"]


def test_shell_signal_status_is_observed_without_stage_inference(tmp_path: Path):
    result = run_process(tmp_path, "kill -TERM $$")
    assert result["returncode"] == -signal.SIGTERM
    assert result["stage"] == "unknown" and result["command_reached"] is None


def test_no_evidence_descriptor_in_profile(tmp_path: Path):
    profile = tmp_path / "profile"
    profile.write_text("[ ! -e /dev/fd/3 ]\n")
    result = run_process(tmp_path, "true", site=str(profile))
    assert result["returncode"] == 0
    assert result["stage"] == "unknown" and result["command_reached"] is None


def test_watcher_disappearance_resets_even_same_later_sample():
    original = adapters.FileObservation("/watch", 1, 2, 8, 100, 100)
    initial = adapters.fold_pattern_poll(
        adapters.PatternWatchProgress(), (original,), immediate=False
    )
    gone = adapters.fold_pattern_poll(initial, (), immediate=False)
    reappeared = adapters.fold_pattern_poll(gone, (original,), immediate=False)
    assert not reappeared.complete and reappeared.stable["/watch"] == 1


@pytest.mark.parametrize(
    "shell,declaration",
    [
        (shutil.which("bash") or "/no-bash", "declare"),
        (shutil.which("ksh") or "/no-ksh", "typeset"),
    ],
)
@pytest.mark.parametrize("inherited", [None, "inherited"])
def test_profiles_preserve_direct_declaration_scope(
    tmp_path: Path, shell: str, declaration: str, inherited: str | None
):
    import shlex

    if not os.access(shell, os.X_OK):
        pytest.skip(f"real shell unavailable: {shell}")
    site, job_profile = tmp_path / "site's profile", tmp_path / "job's profile"
    site.write_text(
        f"{declaration} -x JIL_SITE=site\n{declaration} -x JIL_OVERRIDE=site\nset -- from_site\n"
    )
    job_profile.write_text(
        f'[ "$JIL_SITE" = site ] || return 37\n[ "$JIL_OVERRIDE" = site ] || return 38\n{declaration} -x JIL_OVERRIDE=job\n{declaration} -x JIL_JOB=job\n'
    )
    command = 'printf \'%s:%s:%s:%s\\n\' "$JIL_SITE" "$JIL_OVERRIDE" "$JIL_JOB" "$1"; /usr/bin/env | grep \'^JIL_OVERRIDE=\''
    env = {**os.environ, "SHELL": "/invalid/inherited-shell"}
    if inherited is None:
        env.pop("JIL_OVERRIDE", None)
    else:
        env["JIL_OVERRIDE"] = inherited
    baseline_script = (
        f". {shlex.quote(str(site))} && . {shlex.quote(str(job_profile))} && {command}"
    )
    baseline = subprocess.run(
        [shell, "-c", baseline_script], env=env, capture_output=True, text=True, timeout=10
    )
    actual = run_process(
        tmp_path, command, shell=shell, site=str(site), job=str(job_profile), env=env
    )
    assert baseline.returncode == actual["returncode"] == 0
    assert baseline.stderr == (tmp_path / "err").read_text() == ""
    assert (tmp_path / "out").read_text() == baseline.stdout
    assert baseline.stdout == "site:job:job:from_site\nJIL_OVERRIDE=job\n"
    assert actual["stage"] == "unknown" and actual["command_reached"] is None


@pytest.mark.parametrize(
    "shell", ["/bin/sh", shutil.which("bash") or "/no-bash", shutil.which("ksh") or "/no-ksh"]
)
@pytest.mark.parametrize("mode", ["append", "truncate"])
def test_noclobber_profile_preserves_explicit_stream_modes(tmp_path: Path, shell: str, mode: str):
    if not os.access(shell, os.X_OK):
        pytest.skip(f"real shell unavailable: {shell}")
    profile = tmp_path / "profile"
    profile.write_text("set -C\n")
    out, err, marker = tmp_path / "out's path", tmp_path / "err's path", tmp_path / "marker"
    out.write_text("old-out")
    err.write_text("old-err")
    out.chmod(0o640)
    err.chmod(0o604)
    result = run_process(
        tmp_path,
        f"printf OUT; printf ERR >&2; touch '{marker}'",
        shell=shell,
        site=str(profile),
        stdout=str(out),
        stderr=str(err),
        out_mode=mode,
        err_mode=mode,
    )
    assert result["returncode"] == 0 and marker.exists()
    assert out.read_text() == ("old-outOUT" if mode == "append" else "OUT")
    assert err.read_text() == ("old-errERR" if mode == "append" else "ERR")
    assert out.stat().st_mode & 0o777 == 0o640
    assert err.stat().st_mode & 0o777 == 0o604


@pytest.mark.parametrize(
    "record",
    [
        '{"artifact_format_version":1,"kind":"start","run_id":"r","run_id":"r","at":"2026-01-01"}',
        '{"artifact_format_version":1,"kind":"sample","run_id":"r","at":"2026-01-01","files":[],"files":[{"path":"/a","device":1,"inode":2,"size":3,"mtime_ns":4,"ctime_ns":5}]}',
        '{"artifact_format_version":1,"kind":"sample","run_id":"r","at":"2026-01-01","files":[{"path":"/a","path":"/b","device":1,"inode":2,"size":3,"mtime_ns":4,"ctime_ns":5}]}',
    ],
)
def test_pattern_evidence_refuses_duplicate_keys_at_any_depth(tmp_path: Path, record: str):
    log = tmp_path / adapters.WATCH_LOG
    start = '{"artifact_format_version":1,"kind":"start","run_id":"r","at":"2026-01-01"}'
    log.write_text(("" if '"kind":"start"' in record else start + "\n") + record + "\n")
    with pytest.raises(EngineError, match="duplicate object key"):
        adapters.read_watch_log(tmp_path)


@pytest.mark.parametrize(
    "shell", ["/bin/sh", shutil.which("bash") or "/no-bash", shutil.which("ksh") or "/no-ksh"]
)
@pytest.mark.parametrize("stream", ["stdout", "stderr", "stdin"])
@pytest.mark.parametrize("mode", ["append", "truncate"])
def test_stream_open_failure_prevents_command_for_selected_shells(
    tmp_path: Path, shell: str, stream: str, mode: str
):
    if not os.access(shell, os.X_OK):
        pytest.skip(f"real shell unavailable: {shell}")
    marker = tmp_path / "marker"
    missing = tmp_path / "missing" / "stream"
    result = run_process(
        tmp_path,
        f"touch '{marker}'",
        shell=shell,
        out_mode=mode,
        err_mode=mode,
        **{stream: str(missing)},
    )
    assert result["returncode"] != 0 and not marker.exists()
    assert result["stage"] == "unknown" and result["command_reached"] is None


def test_explicit_profile_streams_capture_effects_and_input(tmp_path: Path):
    profile = tmp_path / "profile"
    profile.write_text(
        'IFS= read -r VALUE || VALUE=EOF\nprintf "PROFILE:%s" "$VALUE"\nprintf DIAGNOSTIC >&2\n'
    )
    source = tmp_path / "profile-input"
    source.write_text("caller-supplied\n")
    result = run_process(tmp_path, "printf COMMAND", site=str(profile), profile_stdin=str(source))
    assert result["returncode"] == 0
    assert (tmp_path / "profile.out").read_text() == "PROFILE:caller-supplied"
    assert (tmp_path / "profile.err").read_text() == "DIAGNOSTIC"
    assert (tmp_path / "profile.out").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "profile.err").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "out").read_text() == "COMMAND"
    assert (tmp_path / "err").read_text() == ""
    result = run_process(tmp_path, "printf COMMAND", site=str(profile))
    assert result["returncode"] == 0 and (tmp_path / "profile.out").read_text() == "PROFILE:EOF"


def test_profile_stream_setup_failure_does_not_launch_command(tmp_path: Path):
    marker = tmp_path / "marker"
    result = run_process(tmp_path, f"touch '{marker}'", profile_stdin=str(tmp_path / "missing"))
    assert result["stage"] == "spawn_error" and result["command_reached"] is False
    assert result["returncode"] is None and not marker.exists()


def test_pattern_torn_final_poll_keeps_durable_start(tmp_path: Path):
    log = tmp_path / adapters.WATCH_LOG
    start = {
        "artifact_format_version": 1,
        "kind": "start",
        "run_id": "r",
        "at": "2026-01-01T00:00:00",
    }
    adapters.append_watch_line(log, start)
    sample = adapters.FileObservation("/watch", 1, 2, 8, 100, 100)
    saved = adapters.append_pattern_poll(
        log, "r", datetime(2026, 1, 1), adapters.PatternWatchProgress(), (sample,), immediate=False
    )
    with log.open("ab") as stream:
        stream.write(b'{"kind":"po')
    progress = adapters.read_watch_log(tmp_path).pattern
    assert progress == saved and progress.stable["/watch"] == 1


def test_pattern_complete_bad_start_is_not_no_start(tmp_path: Path):
    log = tmp_path / adapters.WATCH_LOG
    log.write_bytes(b"{bad}\n")
    with pytest.raises(EngineError, match="line"):
        adapters.read_watch_log(tmp_path)


def test_signal_ladder_sends_terminal_to_all_after_early_refusal(monkeypatch):
    from types import SimpleNamespace

    calls = []
    reads = []
    times = iter([0.0, 6.0])
    monkeypatch.setattr(adapters, "time", SimpleNamespace(monotonic=lambda: next(times)))
    monkeypatch.setattr(procid, "scanned_process_gone", lambda *args: False)
    monkeypatch.setattr(
        procid, "killpg_quiet", lambda *args: pytest.fail("unexpected group signal")
    )

    def send(pid, token, sig, **kwargs):
        calls.append((pid, token, sig))
        return not (pid == 101 and sig == signal.SIGINT)

    monkeypatch.setattr(procid, "signal_ours", send)

    def members():
        reads.append(1)
        tokens = ("A-final", "B-final") if len(reads) >= 3 else ("A-first", "B-first")
        return procid.Left("ours", {101: tokens[0], 202: tokens[1]})

    with pytest.raises(OSError, match="signal 2 not sent to pid 101"):
        asyncio.run(
            adapters.selected_signal_ladder(
                1, "leader", (signal.SIGINT, signal.SIGKILL), members, pidfd=False
            )
        )
    assert calls == [
        (101, "A-first", signal.SIGINT),
        (202, "B-first", signal.SIGINT),
        (101, "A-final", signal.SIGKILL),
        (202, "B-final", signal.SIGKILL),
    ]
    assert len(reads) >= 3


def test_signal_ladder_terminal_refusal_still_sends_other_target(monkeypatch):
    calls = []
    monkeypatch.setattr(procid, "scanned_process_gone", lambda *args: False)

    def send(pid, token, sig, **kwargs):
        calls.append((pid, token, sig))
        return pid != 101

    monkeypatch.setattr(procid, "signal_ours", send)
    with pytest.raises(OSError, match="signal 9 not sent to pid 101"):
        asyncio.run(
            adapters.selected_signal_ladder(
                1,
                "leader",
                (signal.SIGKILL,),
                lambda: procid.Left("ours", {101: "A", 202: "B"}),
                pidfd=False,
            )
        )
    assert calls == [(101, "A", signal.SIGKILL), (202, "B", signal.SIGKILL)]


def test_signal_ladder_reports_prior_failure_after_proven_disappearance(monkeypatch):
    readings = iter([procid.Left("ours", {101: "A"}), procid.NOTHING_LEFT])
    calls = []
    monkeypatch.setattr(
        procid, "signal_ours", lambda pid, token, sig, **kwargs: calls.append((pid, sig)) or False
    )
    monkeypatch.setattr(procid, "scanned_process_gone", lambda *args: False)
    with pytest.raises(OSError, match="signal 2 not sent") as exc:
        asyncio.run(
            adapters.selected_signal_ladder(
                1, "leader", (signal.SIGINT, signal.SIGKILL), lambda: next(readings)
            )
        )
    assert calls == [(101, signal.SIGINT)] and "still alive" not in str(exc.value)


def test_signal_ladder_unknown_membership_keeps_verified_terminal_sends(monkeypatch):
    from types import SimpleNamespace

    calls = []
    times = iter([0.0, 6.0])
    monkeypatch.setattr(adapters, "time", SimpleNamespace(monotonic=lambda: next(times)))
    monkeypatch.setattr(
        procid, "signal_ours", lambda pid, token, sig, **kwargs: calls.append((pid, sig)) or True
    )

    def unreadable():
        return procid.Left("unknown", {202: "B"}, "membership unreadable")

    with pytest.raises(OSError, match="membership unreadable"):
        asyncio.run(
            adapters.selected_signal_ladder(
                1, "leader", (signal.SIGINT, signal.SIGKILL), unreadable
            )
        )
    assert calls == [(202, signal.SIGINT), (202, signal.SIGKILL)]


@pytest.mark.parametrize(
    "shell", ["/bin/sh", shutil.which("bash") or "/no-bash", shutil.which("ksh") or "/no-ksh"]
)
def test_profile_stream_descriptors_are_not_leaked_into_command(tmp_path: Path, shell: str):
    import shlex

    if not os.access(shell, os.X_OK):
        pytest.skip(f"real shell unavailable: {shell}")
    profile_in = tmp_path / "profile-input"
    profile_in.write_text("")
    baseline_in, baseline_profile_out, baseline_profile_err = (
        tmp_path / "baseline-input",
        tmp_path / "baseline-profile-out",
        tmp_path / "baseline-profile-err",
    )
    baseline_in.write_text("")
    inspect = (
        "import json,os,sys; "
        "targets={(os.stat(p).st_dev,os.stat(p).st_ino) for p in sys.argv[1:]}; "
        "fds=[]; "
        "exec('for n in range(3,128):\\n try: s=os.fstat(n)\\n except OSError: continue\\n if (s.st_dev,s.st_ino) in targets: fds.append(n)'); "
        "print(json.dumps(fds))"
    )

    def command(*paths: Path) -> str:
        return " ".join(
            shlex.quote(value)
            for value in (sys.executable, "-c", inspect, *(str(p) for p in paths))
        )

    baseline_out, baseline_err = tmp_path / "baseline-out", tmp_path / "baseline-err"
    baseline_script = (
        f"exec 1>{shlex.quote(str(baseline_out))}; "
        f"exec 2>{shlex.quote(str(baseline_err))}; exec 0</dev/null; "
        + command(baseline_in, baseline_profile_out, baseline_profile_err)
    )
    with (
        baseline_in.open("rb") as stdin,
        baseline_profile_out.open("wb") as stdout,
        baseline_profile_err.open("wb") as stderr,
    ):
        baseline = subprocess.run(
            [shell, "-c", baseline_script], stdin=stdin, stdout=stdout, stderr=stderr, timeout=10
        )
    result = run_process(
        tmp_path,
        command(profile_in, tmp_path / "profile.out", tmp_path / "profile.err"),
        shell=shell,
        profile_stdin=str(profile_in),
    )
    assert baseline.returncode == result["returncode"] == 0
    assert json.loads(baseline_out.read_text()) == json.loads((tmp_path / "out").read_text()) == []


def test_pattern_corrupt_interior_record_does_not_redispatch(tmp_path: Path):
    log = tmp_path / adapters.WATCH_LOG
    start = '{"artifact_format_version":1,"kind":"start","run_id":"r","at":"2026-01-01"}'
    poll = '{"artifact_format_version":1,"kind":"sample","run_id":"r","at":"2026-01-01","files":[]}'
    log.write_text(start + "\n{bad}\n" + poll + "\n")
    with pytest.raises(EngineError, match="line"):
        adapters.read_watch_log(tmp_path)


@pytest.mark.parametrize("where", ["site", "job"])
def test_native_bash_profile_identity_matches_direct_baseline(tmp_path: Path, where: str):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("real bash unavailable")
    profile = tmp_path / "profile"
    profile.write_text(
        'if [ -n "${BASH_VERSION-}" ] && ! shopt -oq posix; '
        "then printf native-bash; else printf other; fi\n"
    )
    env = {**os.environ, "SHELL": "/bin/sh"}
    for name in ("BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS"):
        env.pop(name, None)
    baseline = subprocess.run(
        [bash, "-c", '. "$1"', bash, str(profile)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    wrong = subprocess.run(
        ["/bin/sh", "-c", '. "$1"', "/bin/sh", str(profile)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    actual = run_process(tmp_path, "true", shell=bash, env=env, **{where: str(profile)})
    assert baseline.returncode == wrong.returncode == actual["returncode"] == 0
    assert baseline.stdout == "native-bash" and wrong.stdout == "other"
    assert (tmp_path / "profile.out").read_text() == baseline.stdout
    assert (tmp_path / "profile.err").read_text() == baseline.stderr == ""
