"""CLI and process plumbing for the three local workflow examples.

Business operations, fault injection, and result checks belong to each example.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


class CommandError(RuntimeError):
    def __init__(self, result: subprocess.CompletedProcess[str]) -> None:
        self.result = result
        super().__init__(f"command exited {result.returncode}: {result.stderr or result.stdout}")


def cli(
    *args: str, timeout: float = 30, expected: tuple[int, ...] = (0,)
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, "-m", "dsl41", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode not in expected:
        raise CommandError(result)
    return result


def new_run(name: str, requested: Path | None = None) -> Path:
    if requested is None:
        base = Path(os.environ.get("EXAMPLE_RUNS", "/tmp/dsl41-examples"))
        base.mkdir(parents=True, exist_ok=True)
        path = Path(tempfile.mkdtemp(prefix=f"{name}-", dir=base)).resolve()
    else:
        path = requested.resolve()
        if path.exists():
            raise ValueError(f"refusing to reuse run directory: {path}")
        path.mkdir(parents=True, mode=0o700)
    os.chmod(path, 0o700)
    if len(os.fsencode(path / "engine" / "supervisor.sock")) >= 100:
        raise ValueError("run path is too long for a Unix socket; choose a short --run-dir")
    return path


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def make_properties(
    run: Path,
    worker: Path,
    env: dict[str, str] | None = None,
    extra: dict[str, str] | None = None,
) -> Path:
    exports = env or {}
    for key, value in exports.items():
        if not re.fullmatch(r"[A-Z_][A-Z_0-9]*", key) or "\n" in value or "\r" in value:
            raise ValueError(f"invalid profile variable: {key}")
    profile = run / "profile.sh"
    profile.write_text(
        "".join(f"export {key}={shlex.quote(value)}\n" for key, value in exports.items()),
        encoding="utf-8",
    )
    profile.chmod(0o600)
    values = {
        "WORKER": shlex.join([sys.executable, str(worker.resolve()), "--run", str(run)]),
        "RUN": str(run),
        "PROFILE": shlex.quote(str(profile)),
        **(extra or {}),
    }
    if any("\n" in value or "\r" in value for value in values.values()):
        raise ValueError("properties must be single-line strings")
    path = run / "site.properties"
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")
    path.chmod(0o600)
    return path


def query_status(socket_path: Path) -> dict[str, Any]:
    response = json.loads(cli("query", "status", "--socket", str(socket_path)).stdout)
    if not response.get("ok"):
        raise RuntimeError(f"status query refused: {response}")
    return response


class Engine:
    def __init__(self, run: Path, catalog: Path, properties: Path) -> None:
        self.run = run
        self.catalog = catalog.resolve()
        self.properties = properties.resolve()
        self.root = run / "engine"
        self.socket = self.root / "control.sock"
        self.process: subprocess.Popen[bytes] | None = None

    def _record(self, args: list[str], result: subprocess.CompletedProcess[str]) -> None:
        with (self.run / "commands.jsonl").open("a", encoding="utf-8") as log:
            log.write(
                json.dumps(
                    {
                        "args": args,
                        "exit": result.returncode,
                        "stdout": result.stdout,
                        "stderr": result.stderr,
                    }
                )
                + "\n"
            )

    def _command(self, *args: str, timeout: float = 30) -> str:
        try:
            result = cli(*args, timeout=timeout, expected=(0, 1, 2, 3, 4))
        except subprocess.TimeoutExpired as exc:
            # The CLI may have sent its mutation before the local timeout.
            # Retain partial retry pins rather than treating it as unsent.
            def captured(value: str | bytes | None) -> str:
                return value.decode(errors="replace") if isinstance(value, bytes) else value or ""

            self._record(
                list(args),
                subprocess.CompletedProcess(
                    exc.cmd,
                    124,
                    captured(exc.stdout),
                    captured(exc.stderr) + "\nlauncher timeout; mutation outcome UNKNOWN\n",
                ),
            )
            raise
        self._record(list(args), result)
        if result.returncode != 0:
            # Preserve the original request pins in stderr. Never recompose an
            # uncertain mutation automatically.
            raise CommandError(result)
        return result.stdout

    def start(self, resume: bool = False) -> None:
        if self.process is not None and self.process.poll() is None:
            raise RuntimeError("this launcher already owns a running engine")
        args = [
            sys.executable,
            "-m",
            "dsl41",
            "run",
            str(self.catalog),
            "--run-root",
            str(self.root),
            "-p",
            str(self.properties),
            "--timezone",
            "UTC",
        ]
        if resume:
            args.append("--resume")
        with (self.run / "engine.log").open("ab") as log:
            self.process = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError(
                        f"engine exited {self.process.returncode}; see {self.run}/engine.log"
                    )
                try:
                    self.status()
                    return
                except (CommandError, OSError, subprocess.TimeoutExpired):
                    time.sleep(0.1)
            raise TimeoutError(f"engine did not become ready; see {self.run}/engine.log")
        except BaseException:
            self.stop()
            raise

    def status(self) -> dict[str, Any]:
        return query_status(self.socket)

    def job(self, name: str) -> dict[str, Any]:
        return self.status()["jobs"][name]

    def event(self, verb: str, job: str | None = None) -> dict[str, Any]:
        args = ["sendevent", verb, "--socket", str(self.socket)]
        if job is not None:
            args.extend(["--job", job])
        return json.loads(self._command(*args))

    def wait_job(self, name: str, status: str = "SUCCESS", timeout: float = 90) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            row = self.job(name)
            if row["status"] == status:
                return row
            if row["status"] in {"FAILURE", "TERMINATED"} and status not in {
                "FAILURE",
                "TERMINATED",
            }:
                raise RuntimeError(f"{name} ended {row['status']}; inspect {self.run}/engine/logs")
            time.sleep(0.1)
        raise TimeoutError(f"{name} did not reach {status}: {self.job(name)}")

    def seal(self, force: bool = False) -> dict[str, Any]:
        if self.process is None or self.process.poll() is not None:
            raise RuntimeError("seal requires this launcher's live engine")
        args = [
            "seal",
            "--run-root",
            str(self.root),
            "--next",
            str(self.catalog),
            "-p",
            str(self.properties),
            "--next-timezone",
            "UTC",
        ]
        if force:
            args.append("--force-seal")
        # A live seal prints its JSON decision, then a human-readable opener
        # command. The full output stays in commands.jsonl.
        output = self._command(*args, timeout=90)
        response = json.loads(output.splitlines()[0])
        if not response.get("ok") or response.get("kind") != "seal":
            raise RuntimeError(f"unexpected seal response: {response}")
        if self.process is None or self.process.wait(timeout=30) != 3:
            raise RuntimeError("committed seal did not produce engine exit 3")
        return response

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)

    def __enter__(self) -> Engine:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()
