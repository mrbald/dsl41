"""examples/soak: the generator is deterministic and its estate has the size
the capacity measurements claim -- 200 jobs, one machine that admits 20 runs
at once, and about 1,000 job starts a day, each job starting 1 to 5 times.

The size is checked twice: on the generator's own plan, and on one rehearsed
day, because a plan that the engine does not follow would make every number
in the runbook's capacity section describe another estate.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

from typer.testing import CliRunner

from dsl41.cli import app
from dsl41.cli_common import load_catalog_or_exit_2
from dsl41.ir import ExecSpec

ROOT = Path(__file__).resolve().parent.parent
SOAK = ROOT / "examples" / "soak"
ESTATE = SOAK / "estate" / "soak.jil"
COMMANDS = {"true", "false"}


def _module(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"soak_{name}", SOAK / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # a dataclass looks its module up by name
    spec.loader.exec_module(module)
    return module


GENERATE = _module("generate")


def _is_soak_command(command: str) -> bool:
    parts = [part.split() for part in command.split(";")]
    return all(
        " ".join(words) in COMMANDS or (words[0] == "sleep" and words[1].isdigit())
        for words in parts
    )


def test_the_checked_in_estate_regenerates_byte_for_byte(tmp_path: Path) -> None:
    done = subprocess.run(
        [sys.executable, str(SOAK / "generate.py"), "--out", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    assert (tmp_path / "soak.jil").read_text() == ESTATE.read_text()


def test_another_seed_draws_another_estate_of_the_same_size() -> None:
    first = GENERATE.build(GENERATE.SEED)
    other = GENERATE.build(GENERATE.SEED + 1)
    assert GENERATE.render(first, 1) != GENERATE.render(other, 1)
    assert [job.name for job in first] == [job.name for job in other]


def test_the_estate_has_the_tested_size() -> None:
    jobs = GENERATE.build(GENERATE.SEED)
    assert len(jobs) == 200
    assert all(1 <= job.starts <= 5 for job in jobs)
    assert 900 <= GENERATE.planned_starts(jobs) <= 1000

    catalog = load_catalog_or_exit_2([ESTATE], False)
    assert len(catalog.jobs) == 200
    assert catalog.machines["soakhost"].max_load_units() == 20
    commands = [job for job in catalog.jobs.values() if job.job_type == "CMD"]
    assert len(commands) == 184
    for job in commands:
        assert isinstance(job.exec_, ExecSpec)
        assert job.exec_.machine == "soakhost"
        assert _is_soak_command(job.exec_.command), job.exec_.command
        assert job.job_load_units() == 1
        # machine load is checked only for a positive priority (DL-247)
        priority = job.priority_value()
        assert priority is not None and priority > 0


def test_one_rehearsed_day_starts_about_1000_jobs_and_queues_on_the_machine(
    tmp_path: Path,
) -> None:
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps(_module("compressed").scenario(1)))
    result = CliRunner().invoke(
        app,
        [
            "rehearse",
            str(ESTATE),
            "--start",
            "2026-01-05T00:00:00",
            "--hours",
            "24",
            "--scenario",
            str(scenario),
            "--format",
            "json",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    catalog = load_catalog_or_exit_2([ESTATE], False)
    boxes = {name for name, job in catalog.jobs.items() if job.job_type == "BOX"}
    command_runs = {name: row["runs"] for name, row in doc["jobs"].items() if name not in boxes}
    box_runs = [
        e for e in doc["trace"] if e["job"] in boxes and e["transition"].endswith("RUNNING")
    ]
    assert 900 <= sum(command_runs.values()) + len(box_runs) <= 1100
    # every job runs, and none more than five times a day
    assert set(command_runs) == set(catalog.jobs) - boxes
    assert all(1 <= runs <= 5 for runs in command_runs.values()), command_runs
    assert {e["job"] for e in box_runs} == boxes
    live: set[str] = set()
    most = 0
    for entry in doc["trace"]:
        if entry["job"] in boxes or "->" not in entry["transition"]:
            continue
        if entry["transition"].endswith(("STARTING", "RUNNING")):
            live.add(entry["job"])
        else:
            live.discard(entry["job"])
        most = max(most, len(live))
    assert most == 20  # the limit is reached and never passed
    assert any(e["transition"].endswith("QUE_WAIT") for e in doc["trace"])


# ------------------------------------------------------------- the sampler

SAMPLE = _module("sample")


def test_the_sampler_counts_descriptors_not_lsof_rows() -> None:
    fields = "p4242\nfcwd\nftxt\nftxt\nf0\nf1\nf12\nfrtd\n"
    assert SAMPLE.lsof_fds(fields) == 3
    assert SAMPLE.lsof_fds("p4242\n") == 0


def test_the_sampler_reads_the_open_segment_apart_from_the_whole_wal(tmp_path: Path) -> None:
    assert SAMPLE.wal_bytes(tmp_path) == (0, 0)
    (tmp_path / "wal").mkdir()
    (tmp_path / "wal" / "000001.jsonl").write_text("x" * 10)
    (tmp_path / "wal" / "000002.jsonl").write_text("x" * 3)
    assert SAMPLE.wal_bytes(tmp_path) == (3, 13)


def test_the_sampler_counts_command_runs_apart_from_boxes() -> None:
    jobs = [
        {"status": "RUNNING", "job_type": "BOX", "run_number": 2},
        {"status": "RUNNING", "job_type": "CMD", "run_number": 3},
        {"status": "STARTING", "job_type": "CMD", "run_number": 1},
        {"status": "QUE_WAIT", "job_type": "CMD", "run_number": 0},
        {"status": "SUCCESS", "job_type": "CMD", "run_number": 4},
    ]
    assert SAMPLE.live_counts(jobs) == {
        "live_runs": 2,
        "live_boxes": 1,
        "que_wait": 1,
        "run_numbers": 10,
    }


# ------------------------------------------------------------- stop.sh

#: A stand-in for `dsl41`: it logs each call, and answers `supervise list`
#: from the lines of $FAKE/list, one per call, the last one repeated. A line
#: is an exit code and the text: stdout on exit 0, stderr otherwise.
FAKE_DSL41 = """#!/bin/sh
echo "$1 $2" >>"$FAKE/calls"
case "$1 $2" in
    "supervise list")
        n=$(($(cat "$FAKE/n" 2>/dev/null || echo 0) + 1))
        echo "$n" >"$FAKE/n"
        line=$(sed -n "${n}p" "$FAKE/list")
        [ -n "$line" ] || line=$(tail -n 1 "$FAKE/list")
        code=${line%% *}
        if [ "$code" -eq 0 ]; then echo "${line#* }"; else echo "${line#* }" >&2; fi
        exit "$code" ;;
esac
exit 0
"""


def _exited_pid() -> int:
    done = subprocess.Popen(["true"])
    done.wait()
    return done.pid


def _stop(tmp_path: Path, listing: list[str], engine_pid: int, **env: str):
    fake = tmp_path / "fake"
    fake.mkdir()
    dsl41 = fake / "dsl41"
    dsl41.write_text(FAKE_DSL41)
    dsl41.chmod(0o755)
    (fake / "list").write_text("\n".join(listing) + "\n")
    soak = tmp_path / "soak"
    soak.mkdir()
    (soak / "soak.env").write_text(
        f"DSL41='{dsl41}'\nPYTHON='{sys.executable}'\n"
        f"RUN_ROOT='{soak / 'engine'}'\nESTATE='{ESTATE}'\n"
    )
    (soak / "engine.pid").write_text(f"{engine_pid}\n")
    (soak / "sample.pid").write_text(f"{_exited_pid()}\n")
    done = subprocess.run(
        ["sh", str(SOAK / "stop.sh"), str(soak)],
        capture_output=True,
        text=True,
        env={**os.environ, "FAKE": str(fake), "DRAIN_POLL_SECONDS": "0", **env},
        timeout=60,
    )
    steps = [json.loads(line) for line in (soak / "stop.jsonl").read_text().splitlines()]
    calls = (fake / "calls").read_text().splitlines()
    return done, {step["step"]: step for step in steps}, calls


ALIVE = '0 {"runs": [{"wrapper_alive": true}]}'
DONE = '0 {"runs": [{"wrapper_alive": false}]}'


def test_stop_keeps_draining_through_a_failed_list(tmp_path: Path) -> None:
    done, steps, calls = _stop(tmp_path, ["1 socket error", ALIVE, DONE], _exited_pid())
    assert done.returncode == 0, done.stderr
    assert "supervise list exited 1: socket error" in done.stderr
    assert steps["drain"]["drained"] is True
    assert calls.count("supervise list") == 3
    assert calls[-1] == "supervise shutdown"


def test_stop_without_a_supervisor_has_nothing_to_drain(tmp_path: Path) -> None:
    done, steps, calls = _stop(tmp_path, ["2 no supervisor at x/supervisor.sock"], _exited_pid())
    assert done.returncode == 0, done.stderr
    assert steps["drain"]["drained"] is True
    assert calls.count("supervise list") == 1


def test_stop_reports_a_drain_that_ran_out_of_time(tmp_path: Path) -> None:
    done, steps, calls = _stop(
        tmp_path, ["2 the lease could not be taken"], _exited_pid(), DRAIN_SECONDS="0"
    )
    assert done.returncode == 0, done.stderr
    assert "the shutdown ends them" in done.stderr
    assert steps["drain"]["drained"] is False
    assert calls[-1] == "supervise shutdown"


def test_stop_gives_up_on_an_engine_that_does_not_exit(tmp_path: Path) -> None:
    engine = subprocess.Popen(["sleep", "60"])
    try:
        done, steps, calls = _stop(tmp_path, [DONE], engine.pid, ENGINE_EXIT_SECONDS="1")
    finally:
        engine.kill()
        engine.wait()
    assert done.returncode == 1
    assert "still runs 1 s after the seal" in done.stderr
    assert steps["engine_exit"]["exit"] == 1
    assert "audit --run-root" not in calls  # the engine still holds the lock
    assert "drain" not in steps
