"""DL-492: launch policy through the live adapter and the real wrapper file."""

from __future__ import annotations

import json
import os
import resource
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from dsl41 import runner_adapters as adapters, runner_procid as procid, runner_wrapper
from dsl41.ir import lower_source
from dsl41.runner_clock import RealClock
from dsl41.runtime_policy import ResourceLimit


def run_wrapper(tmp_path: Path, spec: dict) -> dict:
    read_fd, write_fd = os.pipe()
    spec = {
        "version": 1,
        "run_id": str(uuid.uuid4()),
        "job": "p",
        "run_number": 1,
        "command": "true",
        "run_dir": str(tmp_path),
        "lifeline_fd": read_fd,
        "stdout_path": str(tmp_path / "out"),
        "stderr_path": str(tmp_path / "err"),
        **spec,
    }
    try:
        result = subprocess.run(
            [sys.executable, runner_wrapper.__file__],
            input=json.dumps(spec),
            capture_output=True,
            text=True,
            pass_fds=(read_fd,),
            timeout=15,
        )
    finally:
        os.close(read_fd)
        os.close(write_fd)
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr
    return json.loads((Path(spec["run_dir"]) / "status.json").read_text())


def run_process(tmp_path: Path, command: str, **kwargs: object) -> dict:
    """Compose and prepare via the adapter, then drive the wrapper by file path."""
    kwargs.setdefault("stdout", str(tmp_path / "out"))
    kwargs.setdefault("stderr", str(tmp_path / "err"))
    attrs = []
    for attr, key in (
        ("std_in_file", "stdin"),
        ("std_out_file", "stdout"),
        ("std_err_file", "stderr"),
    ):
        if key in kwargs:
            prefix = (
                ">"
                if kwargs.get("out_mode" if key == "stdout" else "err_mode") == "truncate"
                else ""
            )
            attrs.append(f"{attr}: {prefix if key != 'stdin' else ''}{kwargs[key]}")
    if "job" in kwargs:
        attrs.append(f"profile: {kwargs['job']}")
    job = lower_source(
        "insert_job: p\njob_type: c\ncommand: " + command + "\n" + "\n".join(attrs)
    ).jobs["p"]
    paths = {
        "std_in_file": None,
        "std_out_file": str(tmp_path / "out"),
        "std_err_file": str(tmp_path / "err"),
    }
    paths.update(
        {
            attr: str(kwargs[key])
            for attr, key in (
                ("std_in_file", "stdin"),
                ("std_out_file", "stdout"),
                ("std_err_file", "stderr"),
            )
            if key in kwargs
        }
    )
    ctx = adapters.AdapterContext(
        clock=RealClock(),
        run_root=tmp_path,
        paths=paths,
        spool=tmp_path / "runs" / str(uuid.uuid4()),
        shell=str(kwargs.get("shell", "/bin/sh")),
        site_profile=kwargs.get("site"),
        env=kwargs.get("env", {}),
    )
    run_dir, spec = adapters._build_run_spec(job, 1, ctx, grace_seconds=0)
    adapters._prepare_output_files(job, spec)
    if "limits" in kwargs:
        spec["limits"] = kwargs["limits"]
    before = resource.getrlimit(resource.RLIMIT_NOFILE)
    status = run_wrapper(run_dir, spec)
    assert resource.getrlimit(resource.RLIMIT_NOFILE) == before
    return status


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
    assert result["exit_code"] == 0
    assert order.read_text() == "site64\njob"
    assert (tmp_path / "out").read_text().strip() == "job:64"
    assert (tmp_path / "out").stat().st_mode & 0o777 == 0o600
    assert resource.getrlimit(resource.RLIMIT_NOFILE) == before


@pytest.mark.parametrize("failed", ["site", "job"])
def test_profile_failure_prevents_command(tmp_path: Path, failed: str):
    site, profile = tmp_path / "site", tmp_path / "profile"
    site.write_text("false\n" if failed == "site" else "true\n")
    profile.write_text("false\n" if failed == "job" else "true\n")
    marker = tmp_path / "command"
    result = run_process(tmp_path, f"touch '{marker}'", site=str(site), job=str(profile))
    assert result["exit_code"] != 0
    assert not marker.exists()


def test_open_failure_preserves_status_and_no_command_effect(tmp_path: Path):
    marker = tmp_path / "command"
    result = run_process(tmp_path, f"touch '{marker}'", stdout=str(tmp_path / "missing" / "out"))
    assert result["outcome"] == "spawn_failed" and result["error"]
    assert not marker.exists()


def test_limit_failure_is_child_setup_failure(tmp_path: Path):
    marker = tmp_path / "command"
    limits = [[-1, 0, 0]]
    result = run_process(tmp_path, f"touch '{marker}'", limits=limits)
    assert result["outcome"] == "spawn_failed"
    assert "Exception occurred in preexec_fn" in result["error"]
    assert f"limits={json.dumps(limits)}" in result["error"]
    assert not marker.exists()


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
    assert result["exit_code"] == 0
    assert shared.read_text() == "previous\nouterr"
    assert shared.stat().st_mode & 0o777 == 0o604


def test_shell_exec_refusal_and_command_exit_are_distinct(tmp_path: Path):
    missing = run_process(tmp_path, "true", shell=str(tmp_path / "missing"))
    assert missing["outcome"] == "spawn_failed"
    assert missing["error"]
    result = run_process(tmp_path, "exit 23")
    assert result["exit_code"] == 23


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
    assert result["exit_code"] == 0 and (tmp_path / "out").read_text() == "native-bash"
    control = run_process(tmp_path, str(script), shell="/bin/sh", out_mode="truncate")
    assert control["exit_code"] == 0 and (tmp_path / "out").read_text() != "native-bash"
    script.write_text(
        '#!/bin/sh\nif [ -n "${BASH_VERSION-}" ] && ! shopt -oq posix; '
        "then printf native-bash; else printf other; fi\n"
    )
    baseline = subprocess.run(["/bin/sh", str(script)], capture_output=True, text=True, timeout=10)
    wrong = subprocess.run([bash, str(script)], capture_output=True, text=True, timeout=10)
    result = run_process(tmp_path, str(script), shell=bash, out_mode="truncate")
    assert baseline.returncode == wrong.returncode == result["exit_code"] == 0
    assert baseline.stdout == "other" and wrong.stdout == "native-bash"
    assert (tmp_path / "out").read_text() == baseline.stdout


def test_shell_command_sees_no_private_positional_arguments(tmp_path: Path):
    result = run_process(tmp_path, 'printf "%s:%s" "$#" "$1"')
    assert result["exit_code"] == 0
    assert (tmp_path / "out").read_text() == "0:"


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
    assert result["exit_code"] == 0
    assert marker.read_text() == baseline.stdout
    assert (tmp_path / "out").read_text() == baseline.stdout
    wrong = run_process(tmp_path, str(script), shell="/bin/sh", out_mode="truncate")
    assert wrong["exit_code"] != 0 or (tmp_path / "out").read_text() != baseline.stdout


def test_profile_closing_fd_three_still_runs_command(tmp_path: Path):
    profile = tmp_path / "profile"
    profile.write_text("exec 3>&-\n")
    marker = tmp_path / "command"
    result = run_process(tmp_path, f"touch '{marker}'", site=str(profile))
    assert result["exit_code"] == 0 and marker.exists()


def test_stdin_and_truncate(tmp_path: Path):
    source = tmp_path / "input"
    source.write_text("from input")
    (tmp_path / "out").write_text("old")
    result = run_process(tmp_path, "cat", stdin=str(source), out_mode="truncate")
    assert result["exit_code"] == 0 and (tmp_path / "out").read_text() == "from input"


def test_explicit_unlimited_limit(tmp_path: Path):
    hard = resource.getrlimit(resource.RLIMIT_CORE)[1]
    if hard != resource.RLIM_INFINITY:
        pytest.skip("inherited hard core limit is finite")
    result = run_process(tmp_path, "ulimit -c", limits=[(resource.RLIMIT_CORE, None, None)])
    assert result["exit_code"] == 0
    assert (tmp_path / "out").read_text().strip() == "unlimited"


def test_background_profile_process_does_not_block_wait(tmp_path: Path):
    profile = tmp_path / "profile"
    pid_file = tmp_path / "background.pid"
    profile.write_text(f'sleep 30 </dev/null >/dev/null 2>&1 &\nprintf "%s" "$!" > "{pid_file}"\n')
    start = time.monotonic()
    try:
        result = run_process(tmp_path, "true", site=str(profile))
        assert result["exit_code"] == 0
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
    assert result["exit_code"] == 0
    assert (tmp_path / "out").read_text().strip() == "48"


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
    assert result["exit_code"] == 0
    assert (tmp_path / "out").read_text().strip() == "512"


def test_concurrent_children_keep_distinct_umasks(tmp_path: Path):
    from concurrent.futures import ThreadPoolExecutor

    left, right = tmp_path / "left", tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "profile").write_text("umask 077\n")
    (right / "profile").write_text("umask 022\n")

    def launch(path):
        return run_process(path, f"printf x > {path / 'created'}", site=str(path / "profile"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(launch, (left, right)))
    assert all(item["exit_code"] == 0 for item in results)
    assert (left / "created").stat().st_mode & 0o777 == 0o600
    assert (right / "created").stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize(
    "shell", ["/bin/sh", shutil.which("bash") or "/no-bash", shutil.which("ksh") or "/no-ksh"]
)
@pytest.mark.parametrize("where", ["site", "job"])
@pytest.mark.parametrize("status", [0, 23])
def test_closed_fd_profile_effects(tmp_path: Path, shell: str, where: str, status: int):
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
    assert baseline.returncode == result["exit_code"] == status
    assert marker.exists() is baseline_marker.exists() is (status == 0)


def test_shell_signal_status_is_observed_without_stage_inference(tmp_path: Path):
    result = run_process(tmp_path, "kill -TERM $$")
    assert result["outcome"] == "signaled" and result["signal"] == signal.SIGTERM


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
    assert baseline.returncode == actual["exit_code"] == 0
    assert baseline.stderr == (tmp_path / "err").read_text() == ""
    assert (tmp_path / "out").read_text() == baseline.stdout
    assert baseline.stdout == "site:job:job:from_site\nJIL_OVERRIDE=job\n"


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
    assert result["exit_code"] == 0 and marker.exists()
    assert out.read_text() == ("old-outOUT" if mode == "append" else "OUT")
    assert err.read_text() == ("old-errERR" if mode == "append" else "ERR")
    assert out.stat().st_mode & 0o777 == 0o640
    assert err.stat().st_mode & 0o777 == 0o604


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
    assert result["outcome"] == "spawn_failed" and result["error"]
    assert not marker.exists()


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
    assert baseline.returncode == wrong.returncode == actual["exit_code"] == 0
    assert baseline.stdout == "native-bash" and wrong.stdout == "other"
    assert (tmp_path / "out").read_text() == baseline.stdout
    assert (tmp_path / "err").read_text() == baseline.stderr == ""


@pytest.mark.parametrize(
    "site,profile,expected",
    [
        (None, None, "printf command"),
        (None, "/job", ". /job || exit $?; printf command"),
        ("/site", "/job", ". /site || exit $?; . /job || exit $?; printf command"),
        (None, "/a path/it's", ". '/a path/it'\"'\"'s' || exit $?; printf command"),
    ],
)
def test_adapter_script_and_limit_wire(tmp_path, site, profile, expected):
    extra = f"profile: {profile}\n" if profile else ""
    job = lower_source(
        'insert_job: p\njob_type: c\ncommand: printf command\nulimit: n="64,64"\n' + extra
    ).jobs["p"]
    ctx = adapters.AdapterContext(clock=RealClock(), run_root=tmp_path, site_profile=site)
    _, spec = adapters._build_run_spec(job, 1, ctx, grace_seconds=0)
    assert spec["command"] == expected
    assert spec["shell"] == "/bin/sh"
    assert spec["limits"] == [[resource.RLIMIT_NOFILE, 64, 64]]


@pytest.mark.parametrize("failed", ["site", "job"])
@pytest.mark.parametrize("operator", [";", "||"])
def test_profile_failure_stops_entire_command_list(tmp_path, failed, operator):
    site, job = tmp_path / "site's profile", tmp_path / "job's profile"
    site.write_text("return 23\n" if failed == "site" else "export ORDER=site\n")
    job.write_text("return 37\n" if failed == "job" else "export ORDER=job\n")
    marker = tmp_path / "effect"
    status = run_process(tmp_path, f"false {operator} touch {marker}", site=str(site), job=str(job))
    assert status["outcome"] == "exited"
    assert status["exit_code"] == (23 if failed == "site" else 37)
    assert not marker.exists()


@pytest.mark.parametrize("operator", [";", "||"])
def test_old_profile_composition_reaches_tail_on_real_shell(tmp_path, operator):
    profile, marker = tmp_path / "profile", tmp_path / "effect"
    profile.write_text("return 23\n")
    result = subprocess.run(
        ["/bin/sh", "-c", f". {profile} && false {operator} touch {marker}"], timeout=10
    )
    assert result.returncode == 0 and marker.exists()


def test_profile_exports_with_quoted_paths(tmp_path):
    site, job = tmp_path / "site's profile", tmp_path / "job's profile"
    site.write_text("export ORDER=site\nexport SITE=yes\n")
    job.write_text('export ORDER="$ORDER:job"\nexport JOB=yes\n')
    status = run_process(
        tmp_path, 'printf "%s:%s:%s" "$ORDER" "$SITE" "$JOB"', site=str(site), job=str(job)
    )
    assert status["exit_code"] == 0
    assert (tmp_path / "out").read_text() == "site:job:yes:yes"


@pytest.mark.parametrize("fields", [{}, {"shell": "/bin/sh", "limits": []}])
def test_wrapper_current_shapes_and_legacy_effects(tmp_path, fields):
    (tmp_path / "out").write_text("old:")
    (tmp_path / "err").write_text("old:")
    (tmp_path / "input").write_text("input")
    status = run_wrapper(
        tmp_path,
        {
            "command": 'cat; printf err >&2; test "$#" -eq 0; exit 7',
            "stdin_path": str(tmp_path / "input"),
            **fields,
        },
    )
    assert status["outcome"] == "exited" and status["exit_code"] == 7
    assert (tmp_path / "out").read_text() == "old:input"
    assert (tmp_path / "err").read_text() == "old:err"
    spawn = json.loads((tmp_path / "spawn.json").read_text())
    assert "shell" not in spawn and "limits" not in spawn
    assert "shell" not in status and "limits" not in status


@pytest.mark.parametrize("stream", ["std_out_file", "std_err_file"])
@pytest.mark.parametrize("prefix", ["", ">>"])
def test_default_stream_mode_and_replacement(tmp_path, stream, prefix):
    explicit = tmp_path / "explicit"
    explicit.write_text("old")
    job = lower_source(
        f"insert_job: p\njob_type: c\ncommand: printf new; printf new >&2\n{stream}: {prefix}{explicit}\n"
    ).jobs["p"]
    out, err = adapters.default_log_paths("p", 1, tmp_path)
    default = err if stream == "std_out_file" else out
    default.parent.mkdir(parents=True)
    default.write_text("old")
    for ordinal in (1, 2):
        ctx = adapters.AdapterContext(
            clock=RealClock(),
            run_root=tmp_path,
            spool=tmp_path / "runs" / "p.1" / str(ordinal),
            std_file_mode="truncate",
        )
        run_dir, spec = adapters._build_run_spec(job, 1, ctx, grace_seconds=0)
        adapters._prepare_output_files(job, spec, ctx.std_file_mode)
        assert run_wrapper(run_dir, spec)["exit_code"] == 0
        assert explicit.read_text() == ("old" + "new" * ordinal if prefix else "new")
        assert default.read_text() == "old" + "new" * ordinal


def test_live_resolver_expands_literal_stream_paths_once(tmp_path):
    job = lower_source(
        "insert_job: p\njob_type: c\ncommand: true\nstd_out_file: >$TARGET\nstd_err_file: >>relative\n"
    ).jobs["p"]
    ctx = adapters.AdapterContext(clock=RealClock(), run_root=tmp_path, env={"TARGET": ">literal"})
    _, spec = adapters._build_run_spec(job, 1, ctx, grace_seconds=0)
    assert spec["stdout_path"] == str(Path.cwd() / ">literal")
    assert spec["stderr_path"] == str(Path.cwd() / "relative")


@pytest.mark.parametrize("shell_name", ["sh", "dash", "bash", "ksh"])
def test_profile_alias_does_not_rewrite_command(tmp_path, shell_name):
    import shlex
    from dsl41 import main_config
    from test_main_config import config_file

    shell = shutil.which(shell_name)
    if shell is None:
        pytest.skip(f"real {shell_name} unavailable")
    executable = tmp_path / "launch_witness"
    executable.write_text("#!/bin/sh\nprintf ORIGINAL\n")
    executable.chmod(0o755)
    profile = tmp_path / "profile"
    profile.write_text(
        'if [ -n "${BASH_VERSION-}" ]; then shopt -s expand_aliases; fi\n'
        "alias launch_witness='printf ALIASED'\n"
    )
    (tmp_path / "a.jil").write_text(
        f"insert_job: p\njob_type: c\ncommand: launch_witness\nprofile: {profile}\n"
    )
    estate = main_config.prepare(config_file(tmp_path, engine=f'job_shell = "{shell}"\n'))
    ctx = adapters.AdapterContext(clock=RealClock(), run_root=tmp_path, shell=shell)
    _, spec = adapters._build_run_spec(estate.catalog.jobs["p"], 1, ctx, grace_seconds=0)
    env = {**os.environ, "PATH": str(tmp_path) + os.pathsep + os.defpath}
    for name in ("BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS"):
        env.pop(name, None)
    control = subprocess.run(
        [shell, "-c", f". {shlex.quote(str(profile))}\nlaunch_witness"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert control.returncode == 0 and control.stdout == "ALIASED"
    result = subprocess.run(
        [shell, "-c", spec["command"]],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "ORIGINAL", f"{shell} applied the profile alias: {result.stdout!r}"


@pytest.mark.parametrize("shell_name", ["sh", "dash", "bash", "ksh"])
def test_background_command_tail_sees_profile_export(tmp_path, shell_name):
    shell = shutil.which(shell_name)
    if shell is None:
        pytest.skip(f"real {shell_name} unavailable")
    profile = tmp_path / "profile"
    profile.write_text("export LAUNCH_PROFILE_VALUE=sourced\n")
    result = run_process(
        tmp_path,
        'true & printf "%s" "${LAUNCH_PROFILE_VALUE-unset}"; wait',
        job=str(profile),
        shell=shell,
    )
    assert result["exit_code"] == 0
    assert (tmp_path / "out").read_text() == "sourced"


@pytest.mark.parametrize("shell_name", ["sh", "dash", "bash", "ksh"])
@pytest.mark.parametrize("profile_status", [None, 0, 23], ids=["no-profile", "success", "failure"])
def test_comment_only_command_through_wrapper(tmp_path, shell_name, profile_status):
    import shlex
    from dsl41 import main_config
    from test_main_config import config_file

    shell = shutil.which(shell_name)
    if shell is None:
        pytest.skip(f"real {shell_name} unavailable")
    witness = tmp_path / "profile-ran"
    profile = tmp_path / "profile"
    attributes = ""
    if profile_status is not None:
        profile.write_text(f"printf ran > {shlex.quote(str(witness))}\nreturn {profile_status}\n")
        attributes = f"profile: {profile}\n"
    (tmp_path / "a.jil").write_text(
        "insert_job: p\njob_type: c\ncommand: # intentionally idle\n" + attributes
    )
    estate = main_config.prepare(config_file(tmp_path, engine=f'job_shell = "{shell}"\n'))
    job = estate.catalog.jobs["p"]
    assert job.exec_.command == "# intentionally idle"
    ctx = adapters.AdapterContext(clock=RealClock(), run_root=tmp_path, shell=shell)
    run_dir, spec = adapters._build_run_spec(job, 1, ctx, grace_seconds=0)
    adapters._prepare_output_files(job, spec)
    status = run_wrapper(run_dir, spec)
    assert status["outcome"] == "exited"
    assert status["exit_code"] == (0 if profile_status is None else profile_status)
    assert witness.exists() is (profile_status is not None)
    if profile_status is not None:
        assert witness.read_text() == "ran"
