"""The release train's verbs: prepare, apply, resume, rollback and status (DL-407, DL-412).

They run as the site's deployment identity, which owns ESTATE_RELEASES and ACTIVE and sends
`query` and `sendevent` itself. What runs as the service account goes through
`release_service.service`, by sudo as SERVICE_ACCOUNT, running `dsl41 release service` at the
launcher's runtime path; the engine unit starts through `sudo -n systemctl start ENGINE_UNIT`.

An apply holds ESTATE_RELEASES/.apply.lock for its whole life, and records each step in
ESTATE_RELEASES/.apply.json before it acts. A stopped apply, an interrupted or killed one
included, resumes from that record and never from ACTIVE. Every seal claims the service
account's actor, so a retry is the same envelope live or offline. Before it sends a seal again
under a recorded request id, and after any seal that did not exit 0, it reads the estate for
that id (deployment-runbook ss6a, Day 2). A committed seal is never sent again, and a retry
never takes a fresh id.

A hold is the apply's when its own request applied it. The request id is composed and recorded
before the send, so a resume sends a pending hold again under that id, and the engine answers
from its first decision; an applied hold that moved no revision held nothing new and is not
the apply's. The apply releases each of its holds that is still held, a hold a schedule tick
latched included, under a request id recorded before the send, so a release with no decision
is sent again under that id; a resend refused outright is settled by the run root's log, and
one the log shows undecided stops the apply until the engine decides it. It keeps a hold when
that log shows, after its own hold, an applied request of another that can move the job's hold:
the hold there now may be somebody else's. It never releases a hold it did not apply.

Each finished apply leaves a receipt in its release's .release directory: applied-<request
id>.json when it completed, which .release/latest names; refused-<apply id>.json when it sealed
nothing; rolled-back-<request id>.json when a rollback took it over after its period opened.
"""

from __future__ import annotations

import fcntl
import getpass
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Any

from dsl41.release_service import (
    CMD_APPLY,
    CMD_PREPARE,
    CMD_RESUME,
    CMD_ROLLBACK,
    CMD_STATUS,
    EXIT_KEPT,
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_STOPPED,
    EXIT_UNKNOWN,
    GROUP,
    SERVICE,
    Config,
    Stop,
    changed_files,
    check_prepared,
    checked_name,
    config,
    file_digests,
    fsync_dir,
    read_active,
    read_json,
    release_dir,
    say,
    write_json,
    write_text,
)

#: the job states the drain waits out (deployment-runbook ss0, job recipe step 3)
LIVE = ("STARTING", "RUNNING", "QUE_WAIT")
#: the apply's steps, in order; each is recorded before the next acts
STEPS = ("started", "held", "drained", "sealing", "sealed", "selected", "opened", "verified")
ABORTING = "aborting"
#: what a resume does from each step, in the operator's words (DL-412)
RESUME = {
    "started": "hold the scheduled jobs, wait for running jobs to finish, and seal",
    "held": "hold the scheduled jobs, wait for running jobs to finish, and seal",
    "drained": "hold the scheduled jobs, wait for running jobs to finish, and seal",
    "sealing": "ask the estate whether the seal under request id {request_id} committed, go on"
    " if it did, and otherwise send that seal again under the same id",
    "sealed": "name {candidate} in ACTIVE, start the engine, check the new period, and release"
    " the holds",
    "selected": "start the engine once the sealed engine has exited, check the new period, and"
    " release the holds",
    "opened": "check the new period again, and release the holds",
    "verified": "release the holds and write the receipt",
    ABORTING: "release the holds and clear the record; nothing was sealed",
}
#: the seal's retry-horizon refusal, code `seal_retry_horizon`, and the wait it names
#: (period-model ss9)
HORIZON_CODE = "seal_retry_horizon"
HORIZON = re.compile(
    r"was (-?\d+(?:\.\d+)?)s ago and the closing period's retry_horizon_us is (\d+)"
)
#: the wait before the next seal when a horizon refusal names no figures
HORIZON_FALLBACK_S = 10.0
#: why a kept hold is kept; the first three leave the job on hold (DL-412)
STILL_HELD = ("rejected", "moved", "not_in_log")
#: seconds between the drain's progress lines
PROGRESS_S = 60.0

#: A test-only hook: the tests and the service drill set it to kill the verb on either side of
#: recording a committed seal ("seal-reply", "seal"). Nothing reads it from the environment, so
#: no operator's shell can set it (DL-412).
STOP_AFTER: str | None = None


@dataclass(frozen=True)
class Waits:
    """The apply's three bounds, in seconds."""

    drain_s: float = 600.0
    horizon_s: float = 300.0
    open_s: float = 90.0


class Interrupted(Exception):
    """SIGINT or SIGHUP reached an apply: it stops with its record kept (DL-412)."""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def main(action: Callable[[], int], *, records: bool = False) -> int:
    """Run one verb and return its exit code. A `Stop` prints its lines. For a verb that
    records an apply, SIGINT and SIGHUP stop it with exit 3 and the record kept, and the line
    names `resume` (DL-412)."""
    os.umask(0o022)
    handlers = {}
    if records:
        handlers = {sig: signal.signal(sig, _interrupt) for sig in (signal.SIGINT, signal.SIGHUP)}
    try:
        return action()
    except Stop as stop:
        for line in stop.lines:
            say(line)
        return stop.code
    except Interrupted:
        return interrupted()
    except Exception as exc:  # anything else stops here, with its record kept
        say(f"stopped by an unexpected error: {type(exc).__name__}: {exc}")
        say(f"`{CMD_STATUS}` shows whether an apply is recorded.")
        return EXIT_STOPPED
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


def _interrupt(signum: int, frame: FrameType | None) -> None:
    for sig in (signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, signal.SIG_IGN)
    raise Interrupted(signal.Signals(signum).name)


def interrupted() -> int:
    say("interrupted.")
    try:
        recorded = read_json(config().record)
    except (Stop, OSError, ValueError):
        recorded = None
    if recorded is not None:
        say(f"the apply of {recorded['candidate']} is recorded.")
        say(f"run `{CMD_RESUME}` to finish it.")
    else:
        say(f"no apply is recorded; `{CMD_STATUS}` shows how the last one ended.")
    return EXIT_STOPPED


# ------------------------------------------------ the service account's part


def as_service(cfg: Config, *args: str) -> list[str]:
    return ["sudo", "-n", "-u", cfg.service_account, "-H", cfg.dsl41, GROUP, SERVICE, *args]


def service_json(cfg: Config, *args: str) -> dict[str, Any]:
    """One service-side read, as the service account; its stdout is one JSON object."""
    done = subprocess.run(as_service(cfg, *args), capture_output=True, text=True, check=False)
    sys.stderr.write(done.stderr)
    if done.returncode != 0:
        raise service_failed(cfg, args[0], done)
    try:
        answer: dict[str, Any] = json.loads(done.stdout)
    except ValueError:
        raise Stop(EXIT_STOPPED, f"the service side's {args[0]} printed no JSON.") from None
    return answer


def service_failed(cfg: Config, what: str, done: subprocess.CompletedProcess[str]) -> Stop:
    """The line for a service-side command that failed; sudo's own refusal names the grant."""
    if done.stderr.startswith("sudo:"):
        return Stop(
            EXIT_STOPPED,
            f"sudo did not run `{cfg.dsl41} {GROUP} {SERVICE}` as {cfg.service_account}.",
            "the deployment identity's sudoers grant is missing or names another path; ask the"
            " site package's owner.",
        )
    return Stop(
        EXIT_STOPPED,
        f"the service side's {what} failed with exit {done.returncode}; its error is above.",
    )


# ------------------------------------------------------------------ prepare


def make_read_only(path: Path) -> None:
    """The release's inputs, readable by all and writable by none; its .release directory
    stays the deployment identity's (DL-407)."""
    for directory, _, files in os.walk(path, topdown=False):
        here = Path(directory)
        for file in files:
            (here / file).chmod(0o444)
        here.chmod(0o755 if here.name == ".release" else 0o555)


def remove(path: Path) -> None:
    """A refused release, read-only directories included."""
    for directory, _, _ in os.walk(path):
        Path(directory).chmod(0o755)
    shutil.rmtree(path)


def qualify(cfg: Config, name: str) -> dict[str, Any]:
    """Lint and rehearse release NAME as the service account, and hold it to the
    launcher's ceiling; the check's record, or a refusal."""
    try:
        check = service_json(cfg, "check", name)
    except Stop as exc:
        raise Stop(EXIT_REFUSED, *exc.lines) from exc
    lines = []
    if check["lint"]["exit"] != 0:
        lines.append(f"lint exited {check['lint']['exit']}; its findings are above.")
    elif check["rehearse"]["exit"] != 0:
        lines.append(f"rehearse exited {check['rehearse']['exit']}; its error is above.")
    elif check["jobs"] > cfg.max_jobs:
        lines.append(
            f"it holds {check['jobs']} jobs, above the site's ceiling of {cfg.max_jobs} (MAX_JOBS)."
        )
    if check["grace_s"] > cfg.max_grace_s:
        lines.append(
            f"the runtime's command grace, {check['grace_s']:g} s, is above the site's"
            f" MAX_GRACE_S, {cfg.max_grace_s:g} s."
        )
        lines.append("every prepare refuses until that changes; ask the site package's owner.")
    if lines:
        raise Stop(EXIT_REFUSED, f"{name} is refused.", *lines)
    return check


def write_prepared(cfg: Config, name: str, source: str | None, check: dict[str, Any]) -> None:
    path = cfg.releases / name
    write_json(
        path / ".release" / "prepared.json",
        {
            "name": name,
            "source": source,
            "prepared_at": now(),
            "prepared_by": getpass.getuser(),
            "environment": cfg.environment,
            "max_jobs": cfg.max_jobs,
            "max_grace_s": cfg.max_grace_s,
            "files": file_digests(path),
            **check,
        },
    )
    say(f"prepared {path}: {check['jobs']} jobs, under dsl41 {check['dsl41_version']}.")


def prepare(name: str, source: Path) -> int:
    cfg = config()
    checked_name(name)
    target, staging = cfg.releases / name, cfg.releases / f"{name}.new"
    for path in (target, staging):
        if path.exists() or path.is_symlink():
            if (path / ".release" / "prepared.json").is_file():
                raise Stop(
                    EXIT_REFUSED,
                    f"{path} is prepared already, and a prepared name is installed once.",
                    "prepare the inputs under a new name.",
                )
            raise Stop(
                EXIT_REFUSED,
                f"{path} exists and is not prepared.",
                f"run `{CMD_PREPARE} --requalify {name}` to check it in place, or"
                " prepare under a new name.",
            )
    if not (source / "estate.toml").is_file():
        raise Stop(EXIT_REFUSED, f"{source} holds no estate.toml, so it is not a release.")
    if (source / ".release").exists():
        raise Stop(
            EXIT_REFUSED, f"{source} holds .release: give a release's inputs, not a release."
        )
    shutil.copytree(source, staging)
    staging.rename(target)
    fsync_dir(cfg.releases)
    try:
        (target / ".release").mkdir()
        make_read_only(target)
        check = qualify(cfg, name)
    except Stop as exc:
        remove(target)
        raise Stop(
            exc.code, *exc.lines, f"nothing is installed under {name}, so the name is free again."
        ) from exc
    except BaseException:
        remove(target)
        raise
    write_prepared(cfg, name, str(source.resolve()), check)
    return EXIT_OK


def requalify(name: str) -> int:
    """The checks again, on the installed release under this runtime, its files unchanged;
    prepared.json is replaced only when they pass. A release installed but never prepared is
    checked in place, its inputs made read-only first (DL-412)."""
    cfg = config()
    path = release_dir(cfg, name)
    earlier = read_json(path / ".release" / "prepared.json")
    if earlier is None:
        try:
            (path / ".release").mkdir(exist_ok=True)
            make_read_only(path)
        except OSError as exc:
            raise Stop(
                EXIT_REFUSED,
                f"cannot make {name}'s inputs read-only: {exc.strerror}: {exc.filename}.",
                "the deployment identity must own the release's files.",
            ) from exc
    else:
        moved = changed_files(path, earlier)
        if moved:
            raise Stop(
                EXIT_REFUSED,
                f"{name}'s files changed since prepare: {', '.join(moved)}.",
                "prepare the fixed inputs under a new name.",
            )
    try:
        check = qualify(cfg, name)
    except Stop as exc:
        kept = "prepared.json is unchanged." if earlier else f"{name} stays unprepared."
        raise Stop(exc.code, *exc.lines, kept) from exc
    write_prepared(cfg, name, earlier.get("source") if earlier else None, check)
    return EXIT_OK


# -------------------------------------------------------------------- apply


@contextmanager
def apply_lock(cfg: Config) -> Iterator[None]:
    """ESTATE_RELEASES/.apply.lock, held for the whole apply; a held lock refuses."""
    path = cfg.releases / ".apply.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = os.pread(fd, 4096, 0).decode(errors="replace").strip() or "unknown"
            raise Stop(EXIT_REFUSED, f"another apply holds {path}: {holder}.") from None
        os.ftruncate(fd, 0)
        holder = f"pid {os.getpid()}, {getpass.getuser()}@{socket.gethostname()}, since {now()}"
        os.pwrite(fd, f"{holder}\n".encode(), 0)
        yield
    finally:
        os.close(fd)


def query(cfg: Config, what: str, job: str | None = None) -> dict[str, Any]:
    done = subprocess.run(
        [cfg.dsl41, "query", what, "-S", cfg.socket, *(["--job", job] if job else [])],
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        raise Stop(
            EXIT_REFUSED,
            f"no engine answers on {cfg.socket}: `dsl41 query {what}` exited"
            f" {done.returncode}: {done.stderr.strip()}",
            f"check the engine with `systemctl status {cfg.engine_unit}`.",
        )
    try:
        answer: dict[str, Any] = json.loads(done.stdout)
    except ValueError:
        raise Stop(EXIT_REFUSED, f"`dsl41 query {what}` printed no JSON.") from None
    return answer


def send(cfg: Config, verb: str, job: str, *expect: str) -> tuple[int, dict[str, Any]]:
    """One sendevent; its exit code and its answer, a refusal's text under `error`."""
    done = subprocess.run(
        [cfg.dsl41, "sendevent", verb, "-J", job, "-S", cfg.socket, *expect],
        capture_output=True,
        text=True,
    )
    try:
        answer = json.loads(done.stdout.strip().splitlines()[-1]) if done.stdout.strip() else {}
    except ValueError:
        answer = {}
    if done.returncode not in (0, 2, 3, 4):
        raise Stop(
            EXIT_REFUSED,
            f"`dsl41 sendevent {verb} -J {job}` exited {done.returncode}: {done.stderr.strip()}",
        )
    answer = answer if isinstance(answer, dict) else {}
    if done.returncode == 2:
        answer = answer | {"error": answer.get("error") or done.stderr.strip()}
    return done.returncode, answer


def horizon_wait(output: str) -> float | None:
    """The seconds a seal refused with `seal_retry_horizon` asks to wait, keyed on the
    answer's code and never on its wording; None for any other answer (period-model ss9)."""
    for line in output.splitlines():
        try:
            answer = json.loads(line)
        except ValueError:
            continue
        if isinstance(answer, dict) and answer.get("code") == HORIZON_CODE:
            found = HORIZON.search(str(answer.get("error", "")))
            if found is None:
                return HORIZON_FALLBACK_S
            return int(found.group(2)) / 1e6 - float(found.group(1)) + 1.0
    return None


def still_held(entry: dict[str, Any]) -> bool:
    """A kept hold that leaves the job on hold; a kept entry that names no cause counts as
    one (DL-412)."""
    return entry["state"] == "kept" and entry.get("kept_because", "rejected") in STILL_HELD


def kept_lines(cfg: Config, holds: list[dict[str, Any]]) -> list[str]:
    """Two lines for each of an apply's holds that stays on: why, and the release to type."""
    lines = []
    for entry in (entry for entry in holds if still_held(entry)):
        job = entry["job"]
        command = f"`dsl41 sendevent OFF_HOLD -J {shlex.quote(job)} -S {cfg.socket}`"
        because = entry.get("kept_because")
        if because == "moved":
            lines.append(f"{job} stays on hold: {entry['reason']}.")
            lines.append(
                f"that hold may be another operator's; agree with them before you release it: {command}."
            )
        elif because == "not_in_log":
            lines.append(
                f"{job} stays on hold: the run root's log does not show this apply's hold."
            )
            lines.append(f"release it once it is safe: {command}.")
        else:
            lines.append(f"{job} stays on hold: {entry['reason']}.")
            lines.append(f"release it once it is safe: {command}.")
    return lines


def receipt_holds(cfg: Config, receipt: dict[str, Any]) -> list[str]:
    return kept_lines(cfg, list(receipt.get("holds") or []))


class Apply:
    """One apply, driven from its record, which is written before each step acts."""

    def __init__(self, cfg: Config, record: dict[str, Any], waits: Waits) -> None:
        self.cfg, self.record, self.waits = cfg, record, waits

    @property
    def name(self) -> str:
        return str(self.record["candidate"])

    def step(self, step: str) -> None:
        self.record["step"] = step
        self.record["updated_at"] = now()
        write_json(self.cfg.record, self.record)

    def at_or_before(self, step: str) -> bool:
        return STEPS.index(self.record["step"]) <= STEPS.index(step)

    def stopped(self, exc: BaseException) -> Stop:
        cause = list(exc.lines) if isinstance(exc, Stop) else [f"{type(exc).__name__}: {exc}"]
        step = self.record["step"]
        lines = [f"stopped: {cause[0]}", *cause[1:], f"the apply of {self.name} is recorded."]
        lines.append(f"`{CMD_RESUME}` will {RESUME[step].format(**self.record)}.")
        if step in ("opened", "verified"):
            lines.append(
                f"`{CMD_ROLLBACK}` would release this apply's holds instead, and roll"
                f" back to {self.record['previous']}."
            )
        return Stop(EXIT_STOPPED, *lines)

    def run(self) -> int:
        if self.record["step"] == ABORTING:
            return self.abort("a refused apply's holds are released now.")
        if self.at_or_before("drained"):
            try:
                self.hold()
                self.step("held")
                self.drain()
                self.step("drained")
            except Stop as exc:
                if exc.code == EXIT_STOPPED:
                    raise self.stopped(exc) from exc
                return self.abort(*exc.lines)
        if self.at_or_before("sealing"):
            try:
                code, why = self.seal()
            except (Stop, OSError, ValueError, KeyError) as exc:
                raise self.stopped(exc) from exc
            if code == EXIT_REFUSED:
                return self.abort(*why)
            if code != EXIT_OK:
                raise Stop(
                    EXIT_UNKNOWN,
                    f"the seal's outcome is unknown: it exited {code}, and the estate shows no"
                    f" seal under request id {self.record['request_id']}.",
                    "the scheduled jobs stay on hold, and the engine may have exited.",
                    f"the apply of {self.name} is recorded with that request id.",
                    f"run `{CMD_RESUME}`: it asks the estate about that request id first,"
                    " and sends the seal again only under that id.",
                    "do not seal by hand.",
                )
            self.step("sealed")
            if STOP_AFTER == "seal":
                os.kill(os.getpid(), signal.SIGKILL)
        try:
            if self.at_or_before("sealed"):
                self.select()
                self.step("selected")
            if self.at_or_before("selected"):
                self.start()
                self.step("opened")
            if self.at_or_before("opened"):
                self.verify()
                self.step("verified")
            released, kept = self.release_holds()
        except (Stop, OSError, ValueError, KeyError) as exc:
            raise self.stopped(exc) from exc
        receipt = self.write_receipt("applied", released, kept)
        self.clear()
        lines = kept_lines(self.cfg, receipt["holds"])
        for line in lines:
            say(line)
        say(f"{self.name} is active in period {receipt['period']}.")
        return EXIT_KEPT if lines else EXIT_OK

    def write_receipt(
        self, outcome: str, released: list[str], kept: list[str], **more: Any
    ) -> dict[str, Any]:
        """The receipt of a finished apply, in its release's .release directory; an applied
        one is named by .release/latest."""
        receipt = {
            key: self.record.get(key)
            for key in ("previous", "request_id", "actor", "seal_actor", "holds", "started_at")
        }
        receipt |= {
            "name": self.name,
            "outcome": outcome,
            "finished_at": now(),
            "released": released,
            "kept": kept,
            **more,
        }
        seal = self.record.get("sealed")
        if seal is not None:
            receipt |= {
                "period": seal["next_period"],
                "seal": seal["seal"],
                "source_bundle_hash": seal["source_bundle_hash"],
            }
        directory = self.cfg.releases / self.name / ".release"
        if outcome == "applied":
            receipt["applied_at"] = receipt["finished_at"]
            file = f"applied-{self.record['request_id']}.json"
            write_json(directory / file, receipt)
            write_text(directory / "latest", f"{file}\n")
        elif outcome == "refused":
            write_json(
                directory / f"refused-{self.record.get('apply_id') or uuid.uuid4()}.json", receipt
            )
        else:
            write_json(directory / f"rolled-back-{self.record['request_id']}.json", receipt)
        return receipt

    def clear(self) -> None:
        self.cfg.record.unlink()
        fsync_dir(self.cfg.releases)

    def hold(self) -> None:
        """Hold every scheduled top-level job with a future tick that is not held; a hold
        that was there before stays and is never released here (deployment-runbook ss6).
        A hold is the apply's when its own request applied: the request id is composed and
        recorded before the send, so a resume sends a pending hold again under it."""
        for entry in self.record["holds"]:
            if entry["state"] == "pending":
                self.send_hold(entry, resend=True)
        timers = query(self.cfg, "timers")["timers"]
        mine = {entry["job"] for entry in self.record["holds"] if entry["state"] != "rejected"}
        scheduled = sorted({row["job"] for row in timers if row.get("kind") == "schedule"})
        for job in (job for job in scheduled if job not in mine):
            for _ in range(3):
                answer = query(self.cfg, "status", job)
                row = answer["jobs"].get(job)
                if row is None or row.get("job_type") is None or row.get("box_name") is not None:
                    break
                if row.get("on_hold"):
                    break
                entry = {
                    "job": job,
                    "state": "pending",
                    "request_id": str(uuid.uuid4()),
                    "request_id_expect": int(row["state_rev"]),
                    "request_id_epoch": int(answer["epoch"]),
                    "request_id_baseline": str(answer["baseline_id"]),
                }
                self.record["holds"].append(entry)
                self.step(self.record["step"])
                if self.send_hold(entry):
                    break
            else:
                raise Stop(
                    EXIT_REFUSED,
                    f"{job} changed under each of three holds.",
                    "apply again.",
                )
        taken = [entry["job"] for entry in self.record["holds"] if entry["state"] == "applied"]
        say(f"holds taken: {', '.join(taken) or 'none'}.")

    def send_hold(self, entry: dict[str, Any], *, resend: bool = False) -> bool:
        """The ON_HOLD of ENTRY under its recorded request id, expect, epoch and baseline; an
        exact resend is answered from the first decision, which settles whether the hold is
        the apply's. An applied answer that moved no revision of the job held nothing new (a
        RUNNING job's hold is a no-op), so that hold is never the apply's to release. A
        resend refused outright proves nothing about the first send: the run root's log
        settles it."""
        code, answer = self.send_recorded("ON_HOLD", entry, "request_id")
        rev = (answer.get("revisions") or {}).get(f"job:{entry['job']}")
        if code == 2 and resend:
            mine = [
                c for c in self.hold_log(entry["job"]) if c["request_id"] == entry["request_id"]
            ]
            if mine and mine[0]["decision"] == "undecided":
                raise Stop(
                    EXIT_STOPPED,
                    f"the engine has not decided the hold of {entry['job']} yet; resume once it"
                    " answers.",
                )
            if mine:
                code, rev = 0, mine[0]["rev"]
        if code == 2:
            entry |= {"state": "rejected", "reason": answer["error"]}
            self.step(self.record["step"])
            raise Stop(
                EXIT_REFUSED, f"the engine refused to hold {entry['job']}: {answer['error']}"
            )
        if code != 0:
            entry["state"] = "rejected"
        elif rev is None:
            entry["state"] = "noop"
        else:
            entry |= {"state": "applied", "rev": rev}
        self.step(self.record["step"])
        return code == 0

    def hold_log(self, job: str) -> list[dict[str, Any]]:
        changes: list[dict[str, Any]] = service_json(self.cfg, "holds", job)["changes"]
        return changes

    def send_recorded(self, verb: str, entry: dict[str, Any], key: str) -> tuple[int, Any]:
        """VERB for ENTRY's job under the request id at KEY and the expect, epoch and baseline
        recorded with it; no decision stops the apply with its record, and a resume sends
        the same request again."""
        code, answer = send(
            self.cfg,
            verb,
            entry["job"],
            *("--request-id", entry[key], "--expect", str(entry[f"{key}_expect"])),
            *("--epoch", str(entry[f"{key}_epoch"]), "--baseline", entry[f"{key}_baseline"]),
        )
        if code == 4:
            raise Stop(
                EXIT_STOPPED,
                f"the engine gave no decision on the {verb} of {entry['job']}; a resume sends it"
                f" again under request id {entry[key]}.",
            )
        return code, answer

    def drain(self) -> None:
        """No job STARTING, RUNNING or QUE_WAIT; then no job the release changes or removes
        may be armed or have a pending timer (deployment-runbook ss0, job recipe step 3)."""
        start = time.monotonic()
        deadline, shown = start + self.waits.drain_s, None
        while True:
            jobs = query(self.cfg, "status")["jobs"]
            live = sorted(job for job, row in jobs.items() if row.get("status") in LIVE)
            if not live:
                break
            if time.monotonic() >= deadline:
                raise Stop(
                    EXIT_REFUSED,
                    f"jobs still run after {self.waits.drain_s:g} s: {', '.join(live)}.",
                    "apply again later, or with a longer --drain-timeout.",
                )
            if shown is None or time.monotonic() - shown >= PROGRESS_S:
                waited = time.monotonic() - start
                say(
                    f"waiting for {len(live)} running jobs to finish, {waited:.0f} s of"
                    f" {self.waits.drain_s:g} s: {', '.join(live)}."
                )
                shown = time.monotonic()
            time.sleep(1)
        changes = service_json(self.cfg, "changes", self.record["previous"], self.name)

        def latent(job: str) -> bool:
            row = jobs.get(job) or {}
            return bool(row.get("armed") or row.get("pending_timers"))

        moving = sorted(job for job in (*changes["removed"], *changes["changed"]) if latent(job))
        if moving:
            raise Stop(
                EXIT_REFUSED,
                f"{self.name} changes or removes {', '.join(moving)}, and each is armed or has a"
                " pending timer.",
                "a job the release changes or removes must be idle when the seal runs.",
                "wait until it is, or disarm it with"
                f" `dsl41 sendevent DISARM -J JOB -S {self.cfg.socket}`; then apply again.",
            )

    def read_estate(self) -> bool:
        """Whether the seal under the recorded request id committed; records its facts."""
        seal = service_json(self.cfg, "committed", self.record["request_id"])
        if seal["committed"]:
            self.record["sealed"] = seal
            say(f"the estate shows {seal['seal']} under request id {self.record['request_id']}.")
        return bool(seal["committed"])

    def seal(self) -> tuple[int, list[str]]:
        """The seal, as the service account, and the lines a refusal prints. A recorded
        request id is read in the estate before it is sent again, and so is every seal that
        did not exit 0. A retry-horizon refusal is waited out and sealed again under a new
        request id, its own text not shown (period-model ss9)."""
        if self.record.get("request_id") is not None and self.read_estate():
            return EXIT_OK, []
        deadline = time.monotonic() + self.waits.horizon_s
        while True:
            if self.record.get("request_id") is None:
                self.record["request_id"] = str(uuid.uuid4())
            self.step("sealing")
            done = subprocess.run(
                as_service(self.cfg, "seal", self.name, self.record["request_id"]),
                capture_output=True,
                text=True,
            )
            if done.returncode == EXIT_OK and STOP_AFTER == "seal-reply":
                os.kill(os.getpid(), signal.SIGKILL)
            wait = horizon_wait(done.stdout) if done.returncode == EXIT_REFUSED else None
            if wait is None:
                sys.stderr.write(done.stdout + done.stderr)
            if self.read_estate():
                return EXIT_OK, []
            if done.returncode == EXIT_OK:
                raise Stop(EXIT_STOPPED, "the seal exited 0, and the estate shows no seal.")
            if done.returncode == EXIT_REFUSED and done.stderr.startswith("sudo:"):
                raise service_failed(self.cfg, "seal", done)
            if done.returncode != EXIT_REFUSED:
                return done.returncode, []
            if wait is None:
                return EXIT_REFUSED, ["the seal did not commit; its refusal is above."]
            if time.monotonic() + wait > deadline:
                return EXIT_REFUSED, [
                    f"the retry horizon of the last operator request outlasts --horizon-timeout"
                    f" {self.waits.horizon_s:g} s, so nothing was sealed.",
                    "apply again with a longer --horizon-timeout.",
                ]
            say(
                f"waiting {wait:.1f} s for the retry horizon of the last operator request, then sealing."
            )
            time.sleep(max(wait, 0.0))
            self.record["request_id"] = None

    def select(self) -> None:
        """ACTIVE names the candidate: a plain file replaced by a rename (DL-407)."""
        write_text(self.cfg.active, f"{self.name}\n")

    def start(self) -> None:
        """Start the engine unit once the sealed engine has exited, and wait until the
        successor answers in the committed period under a new leader."""
        seal, unit = self.record["sealed"], self.cfg.engine_unit
        deadline = time.monotonic() + self.waits.open_s
        if self.successor_answers():
            return
        journal = f"read `journalctl -u {unit}` for the cause."
        show = ["systemctl", "show", "-p", "ActiveState", "--value", unit]
        waiting = False
        while True:
            state = subprocess.run(show, capture_output=True, text=True).stdout.strip()
            if state in ("inactive", "failed"):
                break
            if time.monotonic() >= deadline:
                raise Stop(
                    EXIT_STOPPED,
                    f"the sealed engine has not exited after {self.waits.open_s:g} s: {unit} is"
                    f" {state}.",
                    journal,
                )
            if not waiting:
                say(f"waiting up to {self.waits.open_s:g} s for the sealed engine to exit.")
                waiting = True
            time.sleep(1)
        done = subprocess.run(["sudo", "-n", "systemctl", "start", unit], check=False)
        if done.returncode != 0:
            raise Stop(
                EXIT_STOPPED,
                f"`systemctl start {unit}` exited {done.returncode}.",
                "no engine runs now, so no job is scheduled.",
                journal,
            )
        say(f"started {unit}; waiting for it to answer in period {seal['next_period']}.")
        while time.monotonic() < deadline:
            if self.successor_answers():
                return
            time.sleep(1)
        raise Stop(
            EXIT_STOPPED,
            f"no engine answered in period {seal['next_period']} within {self.waits.open_s:g} s.",
            journal,
        )

    def successor_answers(self) -> bool:
        """An engine answers in the committed period's baseline, under an epoch past the
        seal's: the successor, not the sealed engine."""
        seal = self.record["sealed"]
        status = [self.cfg.dsl41, "query", "status", "-S", self.cfg.socket]
        done = subprocess.run(status, capture_output=True, text=True, check=False)
        if done.returncode != 0:
            return False
        header = json.loads(done.stdout)
        return bool(
            header.get("baseline_id") == seal["next_baseline"]
            and int(header.get("epoch", 0)) > int(seal["epoch"])
        )

    def verify(self) -> None:
        """The opener's segment, leader record and manifest name the bundle the seal
        committed, and the bundle was read from the candidate's files. The problems found
        are recorded, so `status` and a rollback's receipt show them (DL-412)."""
        pins = service_json(self.cfg, "pins", self.record["request_id"])
        prefix = f"{self.cfg.releases / self.name}/"
        sealed, period = pins["source_bundle_hash"], pins["next_period"]
        segment = pins["segment"] or {}
        problems = []
        if not segment:
            problems.append(f"period {period} has no opening segment")
        elif (
            segment.get("source_bundle_hash") != sealed
            or (segment.get("opens_from_seal") or {}).get("period_id") != pins["period"]
            or segment.get("baseline_id") != pins["next_baseline"]
        ):
            problems.append(f"period {period}'s segment does not open the committed seal")
        if pins["leader_epoch"] is None or pins["leader_epoch"] <= pins["epoch"]:
            problems.append(f"period {period} has no leader record after the seal")
        if pins["manifest_source_bundle_hash"] != sealed:
            problems.append(f"period {period}'s manifest pins another bundle than {sealed}")
        outside = [path for path in pins["sources"] if not path.startswith(prefix)]
        if outside:
            problems.append(f"the bundle reads {', '.join(outside)}, outside {prefix}")
        if problems:
            self.record["problems"] = problems
            self.step(self.record["step"])
            raise Stop(EXIT_STOPPED, f"the new period's check failed: {'; '.join(problems)}.")
        self.record.pop("problems", None)

    def release_holds(self) -> tuple[list[str], list[str]]:
        """OFF_HOLD for each hold this apply's own request applied, under a request id
        recorded before the send. A pending hold is settled first, and a release left
        without a decision is sent again under its id. A hold stays when the job is no
        longer held or no longer defined (a ghost keeps its row, period-model ss10), or when
        the run root's log shows, after the apply's hold, an applied request of another that
        can move the job's hold. A hold the apply never applied is never released.
        The receipt's lists are the recorded states."""
        for entry in self.record["holds"]:
            if entry["state"] == "pending":
                self.send_hold(entry, resend=True)
        for entry in self.record["holds"]:
            resend = entry["state"] == "releasing"
            while entry["state"] in ("applied", "releasing"):
                if entry["state"] == "applied":
                    self.settle_release(entry)
                if entry["state"] == "releasing":
                    self.send_release(entry, resend=resend)
                    resend = False
        released = [e["job"] for e in self.record["holds"] if e["state"] == "released"]
        kept = [e["job"] for e in self.record["holds"] if e["state"] == "kept"]
        return released, kept

    def settle_release(self, entry: dict[str, Any]) -> None:
        """Whether the apply's applied hold on ENTRY's job is still its own to release."""
        job = entry["job"]
        answer = query(self.cfg, "status", job)
        row = answer["jobs"].get(job)
        reason = because = None
        if row is None or row.get("job_type") is None:
            reason, because = "the job is no longer defined", "undefined"
        elif not row.get("on_hold"):
            reason, because = "the job is no longer held", "unheld"
        else:
            log = self.hold_log(job)
            ids = [change["request_id"] for change in log]
            mine = {entry["request_id"], *entry.get("release_ids", [])}
            if entry["request_id"] not in ids:
                reason = "the run root's log does not show this apply's hold"
                because = "not_in_log"
            else:
                after = log[ids.index(entry["request_id"]) + 1 :]
                others = [
                    f"{c['kind']} {c['request_id']}" for c in after if c["request_id"] not in mine
                ]
                if others:
                    reason = (
                        "another request changed its hold after this apply held it"
                        f" ({', '.join(others)})"
                    )
                    because = "moved"
        if reason is not None:
            entry |= {"state": "kept", "reason": reason, "kept_because": because}
        else:
            release_id = str(uuid.uuid4())
            entry |= {
                "state": "releasing",
                "release_ids": [*entry.get("release_ids", []), release_id],
                "release_id": release_id,
                "release_id_expect": int(row["state_rev"]),
                "release_id_epoch": int(answer["epoch"]),
                "release_id_baseline": str(answer["baseline_id"]),
            }
        self.step(self.record["step"])

    def send_release(self, entry: dict[str, Any], *, resend: bool = False) -> None:
        """The recorded OFF_HOLD. A rejection, such as a tick that latched the job after the
        expect was read, settles the release again under a new id, three sends in all,
        before the hold is kept. A resend refused outright is settled by the run root's log:
        an applied OFF_HOLD under the release id released the hold."""
        code, answer = self.send_recorded("OFF_HOLD", entry, "release_id")
        attempts = int(entry.get("release_attempts", 1))
        if code == 2 and resend:
            mine = [
                c
                for c in self.hold_log(entry["job"])
                if c["request_id"] == entry["release_id"] and c["kind"] == "OFF_HOLD"
            ]
            if mine and mine[0]["decision"] == "undecided":
                raise Stop(
                    EXIT_STOPPED,
                    f"the engine has not decided the release of {entry['job']} yet;"
                    " resume once it answers.",
                )
            code = 0 if mine else 3
        if code == 0:
            entry["state"] = "released"
        elif code == 3 and attempts < 3:
            entry |= {"state": "applied", "release_attempts": attempts + 1}
        else:
            reason = answer.get("error") or answer.get("code") or answer.get("decision")
            entry |= {
                "state": "kept",
                "reason": f"the engine did not release it (OFF_HOLD exited {code}: {reason})",
                "kept_because": "rejected",
            }
        self.step(self.record["step"])

    def abort(self, *reason: str) -> int:
        """Nothing was sealed: release this apply's holds, write a refused receipt and clear
        the record (DL-412)."""
        self.step(ABORTING)
        try:
            released, kept = self.release_holds()
        except Stop as exc:
            raise Stop(
                EXIT_STOPPED,
                *reason,
                f"stopped while releasing this apply's holds: {exc.lines[0]}",
                *exc.lines[1:],
                f"the apply of {self.name} is recorded.",
                f"`{CMD_RESUME}` will {RESUME[ABORTING]}.",
            ) from exc
        receipt = self.write_receipt("refused", released, kept, reason=list(reason))
        self.clear()
        for line in reason:
            say(line)
        say(
            f"{self.name} is not applied; nothing was sealed, and ACTIVE still names"
            f" {self.record['previous']}."
        )
        if released:
            say(
                f"this apply's holds are released: {', '.join(released)}; a start a tick"
                " latched under one of them runs now."
            )
        for line in kept_lines(self.cfg, receipt["holds"]):
            say(line)
        return EXIT_REFUSED

    def give_up(self) -> None:
        """A rollback's take-over of an apply whose new period opened: its holds are released, a
        rolled-back receipt records the check's result and problems, and the record is
        cleared (DL-412)."""
        try:
            released, kept = self.release_holds()
        except (Stop, OSError, ValueError, KeyError) as exc:
            raise self.stopped(exc) from exc
        failed = self.record["step"] == "opened"
        receipt = self.write_receipt(
            "rolled_back",
            released,
            kept,
            verify_failed=failed,
            problems=list(self.record.get("problems") or []),
        )
        self.clear()
        for line in kept_lines(self.cfg, receipt["holds"]):
            say(line)
        say(f"the apply of {self.name} is given up; its receipt is {receipt_name(receipt)}.")


def receipt_name(receipt: dict[str, Any]) -> str:
    return f"rolled-back-{receipt['request_id']}.json"


def begin(cfg: Config, name: str, waits: Waits) -> int:
    """A new apply of NAME, recorded before it acts; the lock is held."""
    previous = read_active(cfg)
    check_prepared(cfg, name)
    if previous == name:
        raise Stop(EXIT_REFUSED, f"{name} is already active.")
    query(cfg, "status")
    record: dict[str, Any] = {
        "candidate": name,
        "previous": previous,
        "step": STEPS[0],
        "apply_id": str(uuid.uuid4()),
        "request_id": None,
        "seal_actor": f"os/{cfg.service_account}",
        "holds": [],
        "actor": f"{getpass.getuser()}@{socket.gethostname()}",
        "started_at": now(),
    }
    write_json(cfg.record, record)
    return Apply(cfg, record, waits).run()


def recorded_refusal(record: dict[str, Any]) -> Stop:
    return Stop(
        EXIT_REFUSED,
        f"an apply of {record['candidate']} is recorded.",
        f"run `{CMD_STATUS}`, then `{CMD_RESUME}` to finish it.",
    )


def apply(name: str, waits: Waits) -> int:
    cfg = config()
    with apply_lock(cfg):
        record = read_json(cfg.record)
        if record is not None:
            raise recorded_refusal(record)
        return begin(cfg, checked_name(name), waits)


def resume(waits: Waits) -> int:
    cfg = config()
    with apply_lock(cfg):
        record = read_json(cfg.record)
        if record is None:
            raise Stop(
                EXIT_REFUSED,
                "no apply is recorded, so there is nothing to resume.",
                f"`{CMD_STATUS}` shows how the last apply ended.",
            )
        if record["step"] in STEPS[: STEPS.index("sealing") + 1]:
            check_prepared(cfg, record["candidate"])
        return Apply(cfg, record, waits).run()


def rollback(waits: Waits) -> int:
    """The apply of the release the active release's latest receipt names as its previous
    one. With an apply recorded whose new period opened, the rollback takes it over first,
    to the release that apply replaced (DL-412)."""
    cfg = config()
    with apply_lock(cfg):
        record = read_json(cfg.record)
        if record is not None:
            if record["step"] not in ("opened", "verified"):
                raise Stop(
                    EXIT_REFUSED,
                    f"an apply of {record['candidate']} is recorded, and its new period has not"
                    " opened.",
                    f"run `{CMD_RESUME}` to finish it; `{CMD_STATUS}` says what it will do.",
                )
            target = str(record["previous"])
            check_prepared(cfg, target)
            query(cfg, "status")
            say(f"rolling back from {record['candidate']} to {target}.")
            Apply(cfg, record, waits).give_up()
            return begin(cfg, target, waits)
        previous = read_active(cfg)
        found = rollback_target(cfg, previous)
        if found is None:
            raise Stop(
                EXIT_REFUSED,
                f"no receipt of {previous} names the release it replaced.",
                f"apply the earlier release by name: `{CMD_APPLY} NAME`.",
            )
        say(f"rolling back from {previous} to {found}.")
        return begin(cfg, found, waits)


def rollback_target(cfg: Config, active: str) -> str | None:
    """The release ACTIVE's latest receipt replaced: a second rollback goes forward again."""
    latest = latest_receipt(cfg, active)
    if latest is None or not latest.get("previous"):
        return None
    return str(latest["previous"])


# ------------------------------------------------------------------- status


def latest_receipt(cfg: Config, name: str) -> dict[str, Any] | None:
    """The receipt of the apply that last made NAME active, which .release/latest names."""
    directory = cfg.releases / name / ".release"
    try:
        latest = (directory / "latest").read_text().strip()
    except FileNotFoundError:
        return None
    return read_json(directory / latest)


def last_apply(cfg: Config) -> dict[str, Any] | None:
    """The receipt of the apply that finished last, of any release and any outcome."""
    receipts = []
    for pattern in ("applied-*.json", "refused-*.json", "rolled-back-*.json"):
        for path in cfg.releases.glob(f"*/.release/{pattern}"):
            receipt = read_json(path)
            if receipt is not None:
                receipts.append(receipt)
    if not receipts:
        return None
    return max(
        receipts,
        key=lambda r: str(r.get("finished_at") or r.get("applied_at") or ""),
    )


def engine_answer(cfg: Config) -> dict[str, Any]:
    done = subprocess.run(
        [cfg.dsl41, "query", "status", "-S", cfg.socket], capture_output=True, text=True
    )
    if done.returncode != 0:
        return {"answers": False, "error": done.stderr.strip()}
    try:
        header = json.loads(done.stdout)
    except ValueError:
        return {"answers": False, "error": "it printed no JSON"}
    return {"answers": True, "baseline_id": header.get("baseline_id"), "epoch": header.get("epoch")}


def ending(receipt: dict[str, Any]) -> str:
    """How a finished apply ended, in one clause."""
    outcome = receipt.get("outcome", "applied")
    if outcome == "applied":
        return f"completed; {receipt['name']} became active in period {receipt.get('period')}"
    if outcome == "refused":
        why = " ".join(receipt.get("reason") or [])
        return f"refused, and nothing was sealed: {why}"
    problems = "; ".join(receipt.get("problems") or [])
    if receipt.get("verify_failed"):
        return f"rolled back after its new period's check failed: {problems}"
    return "rolled back after its new period opened"


def status(fmt: str) -> int:
    """What is active, what an engine answers, where a rollback goes, the recorded apply and
    what resume does, and how the last apply ended, with any hold it left on (DL-412)."""
    cfg = config()
    try:
        active: str | None = read_active(cfg)
        inactive = None
    except Stop as exc:
        active, inactive = None, exc.lines[0]
    releases: list[dict[str, Any]] = []
    for path in sorted(cfg.releases.iterdir()) if cfg.releases.is_dir() else []:
        if path.name.startswith(".") or not path.is_dir():
            continue
        receipts = sorted((path / ".release").glob("applied-*.json"))
        releases.append(
            {
                "name": path.name,
                "prepared": read_json(path / ".release" / "prepared.json"),
                "applied": latest_receipt(cfg, path.name),
                "applies": len(receipts),
            }
        )
    record = read_json(cfg.record)
    if record is not None:
        record["resume"] = RESUME[record["step"]].format(**record)
    target = rollback_target(cfg, active) if active and record is None else None
    report = {
        "active": active,
        "inactive": inactive,
        "engine": engine_answer(cfg),
        "rollback_to": target,
        "apply": record,
        "last": last_apply(cfg),
        "releases": releases,
    }
    if fmt == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
        return EXIT_OK
    for line in status_lines(cfg, report):
        print(line)
    return EXIT_OK


def status_lines(cfg: Config, report: dict[str, Any]) -> list[str]:
    active, record, last = report["active"], report["apply"], report["last"]
    lines = []
    if active is None:
        lines.append(f"active: none; {report['inactive']}")
    else:
        receipt = latest_receipt(cfg, active)
        since = f", active since period {receipt.get('period')}" if receipt else ""
        lines.append(f"active: {active}{since}.")
    engine = report["engine"]
    if engine["answers"]:
        lines.append(f"engine: answers on {cfg.socket}.")
    else:
        lines.append(
            f"engine: no answer on {cfg.socket}; check `systemctl status {cfg.engine_unit}`."
        )
    if record is None:
        if report["rollback_to"]:
            lines.append(f"rollback: `{CMD_ROLLBACK}` would apply {report['rollback_to']}.")
        elif active is not None:
            lines.append("rollback: no receipt names an earlier release; apply one by name.")
        lines.append("recorded apply: none.")
    else:
        lines.append(
            f"recorded apply: {record['candidate']} over {record['previous']}, started"
            f" {record['started_at']} by {record.get('actor')}."
        )
        if record.get("problems"):
            lines.append(f"  the new period's check failed: {'; '.join(record['problems'])}.")
        lines.append(f"  `{CMD_RESUME}` will {record['resume']}.")
        if record["step"] in ("opened", "verified"):
            lines.append(
                f"  `{CMD_ROLLBACK}` would release this apply's holds instead, and roll"
                f" back to {record['previous']}."
            )
        held = [e["job"] for e in record["holds"] if e["state"] in ("applied", "releasing")]
        pending = [e["job"] for e in record["holds"] if e["state"] == "pending"]
        if held:
            lines.append(f"  holds taken by this apply: {', '.join(held)}.")
        if pending:
            lines.append(f"  hold sent, decision unknown: {', '.join(pending)}.")
    if last is None:
        lines.append("last apply: none.")
    else:
        lines.append(
            f"last apply: {last['name']} over {last.get('previous')}, started"
            f" {last.get('started_at')} by {last.get('actor')}: {ending(last)}."
        )
        lines += [f"  {line}" for line in receipt_holds(cfg, last)]
    lines.append(f"releases: {len(report['releases'])} installed; `--format json` lists each.")
    return lines
