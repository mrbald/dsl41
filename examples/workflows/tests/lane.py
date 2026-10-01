"""Compose plumbing for the workflow examples' integration lane (DL-236).

The lane drives `docker compose` the way an operator does. It imports neither
dsl41 nor the examples' Python. Every command it runs is kept as evidence.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tarfile
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
COMPOSE_FILE = "examples/workflows/compose.yaml"
RUN_LINE = re.compile(r"^(?:run|Evidence|retained run): (/runs/[^\s/]+)$", re.MULTILINE)


def now() -> str:
    return datetime.now(UTC).isoformat()


def docker() -> list[str]:
    context = os.environ.get("WORKFLOW_CONTEXT")
    return ["docker", "--context", context] if context else ["docker"]


def compose(project: str) -> list[str]:
    return [*docker(), "compose", "-p", project, "-f", COMPOSE_FILE]


@dataclass
class Result:
    args: list[str]
    exit: int | None
    stdout: str
    stderr: str
    duration_s: float
    started_at: str
    timed_out: bool = False

    def describe(self) -> str:
        return (
            f"{' '.join(self.args)}\nexit {self.exit} after {self.duration_s:.1f}s"
            f"{' (timed out)' if self.timed_out else ''}\n"
            f"--- stdout (tail)\n{self.stdout[-3000:]}\n--- stderr (tail)\n{self.stderr[-3000:]}"
        )

    def require(self, status: int = 0) -> Result:
        assert self.exit == status, self.describe()
        return self

    def run_dir(self) -> str:
        """The run directory the launcher printed, as a path inside the volume."""
        match = RUN_LINE.search(self.stdout)
        assert match, f"no run directory line on stdout:\n{self.describe()}"
        return match.group(1)

    def final_json(self) -> dict[str, Any]:
        """The JSON object the launcher printed last, after its own lines."""
        lines = self.stdout.splitlines()
        assert "{" in lines, f"no JSON object on stdout:\n{self.describe()}"
        value = json.loads("\n".join(lines[lines.index("{") :]))
        assert isinstance(value, dict)
        return value


def execute(args: list[str], timeout: float, stdout: Any = None) -> Result:
    """Run one host command from the repository root and keep its outcome."""
    started_at, start = now(), time.monotonic()
    try:
        done = subprocess.run(
            args,
            cwd=REPO,
            stdin=subprocess.DEVNULL,
            stdout=stdout if stdout is not None else subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return Result(
            args,
            None,
            _text(exc.stdout),
            _text(exc.stderr),
            time.monotonic() - start,
            started_at,
            timed_out=True,
        )
    except OSError as exc:
        return Result(args, None, "", str(exc), time.monotonic() - start, started_at)
    return Result(
        args,
        done.returncode,
        _text(done.stdout) if stdout is None else f"<written to {stdout.name}>",
        _text(done.stderr),
        time.monotonic() - start,
        started_at,
    )


def _text(value: bytes | str | None) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value or ""


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def scheduler_rows(path: Path) -> list[tuple[str, int, str, int | None]]:
    """(job, run number, status, exit code) from an offline `dsl41 runs` history."""
    return sorted(
        (row["job"], row["run_number"], row["status"], row["exit_code"]) for row in read_json(path)
    )


def scheduler_run(token: str) -> dict[str, Any]:
    """Decode the DSL41_RUN forensic tag a worker recorded on entry."""
    value = json.loads(base64.urlsafe_b64decode(token))
    assert isinstance(value, dict)
    return value


def _force_remove(function: Any, path: str, _exc: BaseException) -> None:
    os.chmod(os.path.dirname(path), 0o700)
    os.chmod(path, 0o700)
    function(path)


class Lane:
    """One compose project: its own database, network and run volume."""

    def __init__(self, project: str, evidence: Path) -> None:
        self.project = project
        self.evidence = evidence
        self.commands: list[Result] = []
        evidence.mkdir(parents=True)

    def compose(self, *args: str, timeout: float = 120, stdout: Any = None) -> Result:
        result = execute([*compose(self.project), *args], timeout, stdout)
        self.commands.append(result)
        return result

    def up(self) -> None:
        self.compose("up", "-d", "--wait", "postgres", timeout=300).require()

    def run(self, example: str, *args: str, timeout: float) -> Result:
        """Run one example's launcher in a disposable runner container."""
        command = ["python", f"examples/{example}/demo.py", *args]
        return self.compose("run", "--rm", "-T", "runner", *command, timeout=timeout)

    def exec(self, *command: str, timeout: float = 120, stdout: Any = None) -> Result:
        """Run a one-off command in a runner container beside the run volume."""
        args = ["run", "--rm", "--no-deps", "-T", "runner", *command]
        return self.compose(*args, timeout=timeout, stdout=stdout)

    def psql(self, sql: str) -> str:
        result = self.compose(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "example",
            "-d",
            "examples",
            "-v",
            "ON_ERROR_STOP=1",
            "-qAtc",
            sql,
            timeout=60,
        )
        return result.require().stdout.strip()

    def collect(self) -> Path:
        """Copy the run volume out as a tar and extract it beside the test's evidence."""
        existing = sorted(self.evidence.glob("runs-*.tar"))
        archive = self.evidence / f"runs-{len(existing) + 1}.tar"
        with archive.open("wb") as handle:
            command = ["tar", "-C", "/runs", "-cf", "-", "."]
            self.exec(*command, timeout=300, stdout=handle).require()
        target = self.evidence / "runs"
        if target.exists():
            shutil.rmtree(target, onexc=_force_remove)
        with tarfile.open(archive) as tar:
            tar.extractall(target, filter="data")
        return target

    def collected(self, run_dir: str) -> Path:
        """Collect the volume and return one launcher's run directory in the copy."""
        path = self.collect() / Path(run_dir).relative_to("/runs")
        assert path.is_dir(), f"{run_dir} is not in the collected run volume"
        return path

    def write_commands(self) -> None:
        write_json(self.evidence / "command.json", [asdict(result) for result in self.commands])

    def close(self) -> None:
        """Always collect evidence and record commands, then delete the project."""
        problems = []
        self.compose("kill", "--remove-orphans", timeout=60)
        try:
            self.collect()
        except Exception as exc:  # the project must still come down
            problems.append(f"evidence collection failed: {exc!r}")
        self.write_commands()
        down = self.compose("down", "--volumes", "--remove-orphans", timeout=300)
        self.write_commands()
        if down.exit != 0:
            problems.append(down.describe())
        assert not problems, "\n".join(problems)
