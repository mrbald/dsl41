"""DL-407, DL-412: `dsl41 release status`, interrupts, and the launcher and runtime the verbs read. The site and the stub engine are tests/release_site.py's."""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from release_site import ENTRY, ME, Site, _runtime, site_at


@pytest.fixture
def site(tmp_path: Path) -> Iterator[Site]:
    yield from site_at(tmp_path)


def _default_signals() -> None:
    for sig in (signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, signal.SIG_DFL)


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGHUP])
def test_an_interrupt_keeps_the_record_and_names_resume(site: Site, signum: int) -> None:
    """DL-412: Ctrl-C or a lost session stops the apply with exit 3 and its record kept."""
    site.prepared("v2")
    state = site.state()
    state["jobs"]["NB_IDLE_C"]["status"] = "RUNNING"
    site.save(state)
    process = subprocess.Popen(
        site.command("apply", "v2"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=site.environ(),
        # a terminal's dispositions, whatever this run inherited (`nohup`, a background job)
        preexec_fn=_default_signals,
    )
    deadline = time.monotonic() + 60
    while (site.record() or {}).get("step") != "held":
        assert time.monotonic() < deadline, "the apply never reached the drain"
        time.sleep(0.2)
    process.send_signal(signum)
    _, stderr = process.communicate(timeout=60)
    assert process.returncode == 3, stderr
    assert "interrupted." in stderr and "Traceback" not in stderr
    assert "the apply of v2 is recorded." in stderr
    assert "run `dsl41 release resume` to finish it." in stderr
    record = site.record()
    assert record is not None and record["step"] == "held"
    state = site.state()
    state["jobs"]["NB_IDLE_C"]["status"] = "SUCCESS"
    site.save(state)
    resumed = site.run("resume")
    assert resumed.returncode == 0, resumed.stderr
    assert site.active.read_text() == "v2\n"


def test_status_shows_the_active_release_the_engine_the_rollback_and_the_last_apply(
    site: Site,
) -> None:
    site.prepared("v2")
    shown = site.run("status")
    assert shown.returncode == 0, shown.stderr
    assert shown.stdout.splitlines() == [
        "active: r1.",
        f"engine: answers on {site.root}/control.sock.",
        "rollback: no receipt names an earlier release; apply one by name.",
        "recorded apply: none.",
        "last apply: none.",
        "releases: 2 installed; `--format json` lists each.",
    ]
    assert site.run("apply", "v2").returncode == 0
    site.edit(up=False)
    lines = site.run("status").stdout.splitlines()
    assert lines[0] == "active: v2, active since period 2."
    assert lines[1] == (
        f"engine: no answer on {site.root}/control.sock; check"
        " `systemctl status dsl41-engine.service`."
    )
    assert lines[2] == "rollback: `dsl41 release rollback` would apply r1."
    assert lines[3] == "recorded apply: none."
    assert lines[4].startswith("last apply: v2 over r1, started ")
    assert lines[4].endswith(": completed; v2 became active in period 2.")
    report = json.loads(site.run("status", "--format", "json").stdout)
    assert report["active"] == "v2" and report["apply"] is None
    assert report["rollback_to"] == "r1" and report["engine"]["answers"] is False
    assert report["last"]["outcome"] == "applied"
    assert [release["name"] for release in report["releases"]] == ["r1", "v2"]
    site.active.write_text("r1\nv2\n")
    lines = site.run("status").stdout.splitlines()
    assert lines[0] == (
        f"active: none; {site.active} does not name one release, so the launcher refuses it."
    )


def test_an_ignored_sighup_stays_ignored(site: Site) -> None:
    """DL-412: under `nohup`, a lost session does not stop the apply."""
    site.prepared("v2")
    state = site.state()
    state["jobs"]["NB_IDLE_C"]["status"] = "RUNNING"
    site.save(state)
    process = subprocess.Popen(
        site.command("apply", "v2"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=site.environ(),
        preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_IGN),
    )
    deadline = time.monotonic() + 60
    while (site.record() or {}).get("step") != "held":
        assert time.monotonic() < deadline, "the apply never reached the drain"
        time.sleep(0.2)
    # the signal lands while the drain waits on NB_IDLE_C; had it not been ignored, the apply
    # would stop with exit 3 instead of finishing once the job ends
    process.send_signal(signal.SIGHUP)
    state = site.state()
    state["jobs"]["NB_IDLE_C"]["status"] = "SUCCESS"
    site.save(state)
    _, stderr = process.communicate(timeout=120)
    assert process.returncode == 0, stderr


def test_an_interrupt_of_a_verb_that_records_nothing_exits_3(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """DL-412: Ctrl-C in prepare or status exits 3 like the others, not click's 1."""
    from dsl41 import release

    def interrupted() -> int:
        raise release.Interrupted("SIGINT")

    assert release.main(interrupted) == 3
    assert capsys.readouterr().err == "dsl41 release: interrupted.\n"


def test_the_verbs_run_only_on_the_runtime_the_launcher_selects(site: Site) -> None:
    """DL-412: another runtime under the same prefix refuses, so prepare and apply never
    judge a release under two versions."""
    other = _runtime(site.prefix / ".venvs" / "other")
    done = subprocess.run(
        [str(other), "-c", ENTRY, "", "release", "status"],
        capture_output=True,
        text=True,
        env=site.environ(),
    )
    assert done.returncode == 2
    assert f"and the launcher's DSL41 selects {os.path.realpath(site.python.parents[1])}." in (
        done.stderr
    )


def test_the_verbs_find_the_launcher_beside_the_runtime_only(site: Site) -> None:
    """DL-412: the launcher is <prefix>/bin/dsl41-launch for a runtime under <prefix>/.venvs,
    a plain file no one else can write; no argument or variable names another."""
    site.launcher.chmod(0o777)
    refused = site.run("status")
    assert (
        refused.returncode == 2
        and "must be a plain file that only root, or the account that runs it, can write"
        in (refused.stderr)
    )
    site.launcher.unlink()
    refused = site.run("status")
    assert refused.returncode == 2 and f"{site.launcher} is missing." in refused.stderr
    outside = subprocess.run(
        [sys.executable, "-m", "dsl41", "release", "status"], capture_output=True, text=True
    )
    assert outside.returncode == 2 and "not an installed runtime under" in outside.stderr


@pytest.mark.parametrize("parent", ["symlink", "group-writable"])
def test_the_verbs_refuse_a_launcher_whose_directory_others_can_replace(
    site: Site, parent: str
) -> None:
    """DL-412: every directory down to the launcher is checked, not only the file."""
    bin_dir = site.prefix / "bin"
    if parent == "symlink":
        real = site.base / "elsewhere"
        bin_dir.rename(real)
        bin_dir.symlink_to(real)
    else:
        bin_dir.chmod(0o775)
    refused = site.run("status")
    assert refused.returncode == 2, refused.stderr
    assert (
        f"{bin_dir} must be a real directory that only root, or the account that runs"
        in refused.stderr
    )


def test_the_launcher_runs_from_the_text_that_was_checked(site: Site) -> None:
    """DL-412: the launcher is read from the descriptor whose identity was checked and run
    by `sh -s`; its path is never executed, so its own execute bit plays no part."""
    site.launcher.chmod(0o644)
    shown = site.run("status")
    assert shown.returncode == 0, shown.stderr


@pytest.mark.parametrize(
    ("first", "runs"),
    [("#!/bin/bash", True), ("#!/usr/bin/env bash", True), ("#!/usr/bin/python3", False)],
)
def test_the_launcher_runs_under_the_interpreter_its_first_line_names(
    site: Site, first: str, runs: bool
) -> None:
    """DL-412: /bin/sh, /bin/bash or `/usr/bin/env bash`; any other first line refuses."""
    text = site.launcher.read_text()
    site.launcher.write_text(first + "\n" + text.split("\n", 1)[1])
    shown = site.run("status")
    if runs:
        assert shown.returncode == 0, shown.stderr
    else:
        assert shown.returncode == 2
        assert "it must name /bin/sh, /bin/bash or `/usr/bin/env bash`." in shown.stderr


def test_the_verbs_refuse_a_releases_directory_others_can_write(site: Site) -> None:
    """DL-412: on the previous layout ESTATE_RELEASES was the deployment group's; the verbs
    refuse it and name the fix."""
    site.releases.chmod(0o775)
    refused = site.run("status")
    assert refused.returncode == 2
    assert f"{site.releases} belongs to {ME} with mode 0775" in refused.stderr
    assert f"run `chown {ME} {site.releases}` and `chmod 0755 {site.releases}` as root." in (
        refused.stderr
    )


def test_a_launcher_without_the_verbs_vars_refuses(site: Site) -> None:
    text = site.launcher.read_text().replace('"ENGINE_UNIT=$ENGINE_UNIT"', "")
    site.launcher.write_text(text)
    refused = site.run("status")
    assert refused.returncode == 2
    assert "vars` printed no ENGINE_UNIT." in refused.stderr
    assert "ask the site package's owner to fix the launcher's CONFIGURATION." in refused.stderr


def test_the_launcher_vars_mode_prints_what_the_verbs_read(site: Site) -> None:
    done = subprocess.run([str(site.launcher), "vars"], capture_output=True, text=True)
    assert done.returncode == 0
    values: dict[str, str] = {}
    for line in done.stdout.splitlines():
        key, _, value = shlex.split(line)[0].partition("=")
        values[key] = value
    assert values["ESTATE_RELEASES"] == str(site.releases)
    assert values["ACTIVE"] == str(site.active)
    assert values["MAX_JOBS"] == "107" and values["MAX_GRACE_S"] == "10"
    assert values["SERVICE_ACCOUNT"] == ME and values["ENGINE_UNIT"] == "dsl41-engine.service"
    assert shlex.split(values["NEXT_PROFILE"]) == [
        "--next-detached",
        "--next-as-machine",
        "localhost",
        "--next-machine-policy",
        "strict",
    ]
