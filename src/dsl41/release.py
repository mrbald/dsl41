"""The release train's verbs: prepare, apply, resume, rollback and status (DL-407, DL-412).

They run as the site's deployment identity, which owns ESTATE_RELEASES and ACTIVE and sends
`query` and `sendevent` itself. What runs as the service account goes through
`release_service.service`, by sudo as SERVICE_ACCOUNT, running `dsl41 release service` at the
launcher's runtime path; the engine unit starts through `sudo -n systemctl start ENGINE_UNIT`.

An apply holds .release/apply.lock beside ACTIVE for its whole life, and records each step in
.release/apply.json there before it acts. A stopped apply, an interrupted or killed one
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
nothing; verify-failed-<request id>.json, or taken-over-<request id>.json when its check passed,
when a rollback took it over after its period opened.
"""

from __future__ import annotations

import fcntl
import getpass
import json
import os
import shlex
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

from dsl41.cli_common import UNIT_DOWN
from dsl41.oracle_state import LIVE_OR_QUEUED
from dsl41.release_service import (
    CMD_APPLY,
    CMD_RESUME,
    CMD_ROLLBACK,
    CMD_STATUS,
    EXIT_KEPT,
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_STOPPED,
    EXIT_UNKNOWN,
    GROUP,
    PREPARE,
    RECEIPT_OUTCOMES,
    SERVICE,
    Config,
    Stop,
    check_prepared,
    checked_name,
    config,
    read_active,
    read_json,
    say,
    write_json,
    write_text,
)
from dsl41.runner_procid import fsync_dir

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
#: the seal's retry-horizon refusal code; its answer names the time left in
#: `RetryHorizonRefused.FIELD` (period-model ss9, DL-420)
HORIZON_CODE = "seal_retry_horizon"
#: the wait before the next seal when a horizon refusal carries no time left
HORIZON_FALLBACK_S = 10.0
#: why a kept hold is kept; the first three leave the job on hold (DL-412)
STILL_HELD = ("rejected", "moved", "not_in_log")
#: every receipt a finished apply leaves, by its kind's file prefix (DL-412)
RECEIPTS = tuple(f"{kind}-*.json" for kind in RECEIPT_OUTCOMES)
#: the outcomes of an apply a rollback took over after its new period opened (DL-412)
TAKEN_OVER = (RECEIPT_OUTCOMES["verify-failed"], RECEIPT_OUTCOMES["taken-over"])
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
    """Run one verb and return its exit code. A `Stop` prints its lines. SIGINT and SIGHUP stop
    any verb with exit 3; for one that records an apply the record is kept, and the line names
    `resume`. A signal the caller ignores, as `nohup` ignores SIGHUP, stays ignored (DL-412)."""
    os.umask(0o022)
    handlers = {
        sig: signal.signal(sig, _interrupt)
        for sig in (signal.SIGINT, signal.SIGHUP)
        if signal.getsignal(sig) is not signal.SIG_IGN
    }
    try:
        return action()
    except Stop as stop:
        for line in stop.lines:
            say(line)
        return stop.code
    except Interrupted:
        if not records:
            say("interrupted.")
            return EXIT_STOPPED
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
    return ["sudo", "-n", "-u", cfg.service_account, "-H", cfg.dsl41, GROUP, SERVICE, "--", *args]


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


def service_run(cfg: Config, *args: str, stdin: str | None = None) -> None:
    """One service-side action; its lines pass through, and its refusal is this verb's,
    exit 2."""
    done = subprocess.run(
        as_service(cfg, *args), input=stdin, capture_output=True, text=True, check=False
    )
    sys.stderr.write(done.stderr)
    if done.returncode == EXIT_REFUSED and not done.stderr.startswith("sudo:"):
        raise Stop(EXIT_REFUSED)
    if done.returncode != 0:
        raise service_failed(cfg, args[0], done)


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


def prepare(name: str, source: Path) -> int:
    """The service side installs SOURCE as release NAME and checks it; the deployment
    identity writes nothing under ESTATE_RELEASES (DL-412)."""
    cfg = config()
    checked_name(name)
    with refused_as_2():
        service_run(cfg, PREPARE, name, os.path.abspath(source))
    return EXIT_OK


def requalify(name: str) -> int:
    """The service side checks installed release NAME again, sealing it first when it is not
    sealed to the service account (DL-412)."""
    cfg = config()
    with refused_as_2():
        service_run(cfg, "requalify", checked_name(name))
    return EXIT_OK


@contextmanager
def refused_as_2() -> Iterator[None]:
    """A prepare that could not reach the service side changed nothing: exit 2."""
    try:
        yield
    except Stop as exc:
        raise Stop(EXIT_REFUSED, *exc.lines) from exc


# -------------------------------------------------------------------- apply


@contextmanager
def apply_lock(cfg: Config) -> Iterator[None]:
    """.release/apply.lock beside ACTIVE, held for the whole apply; a held lock refuses."""
    cfg.state.mkdir(mode=0o755, exist_ok=True)
    path = cfg.lock
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


class NoAnswer(Stop):
    """No engine answered `dsl41 query`: a refusal, exit 2, and `said`, what the query said.
    An answer that is not JSON is a plain refusal, never "no answer yet" (DL-420)."""

    def __init__(self, said: str, *lines: str) -> None:
        super().__init__(EXIT_REFUSED, *lines)
        self.said = said


def query(cfg: Config, what: str, job: str | None = None) -> dict[str, Any]:
    """The engine's answer to `dsl41 query WHAT`, the one wrapper of it (DL-420). No answer
    raises `NoAnswer`: a verb that needs the engine refuses with it, and a reader that only
    waits for or reports the engine catches it. An answer that is not JSON refuses, exit 2."""
    done = subprocess.run(
        [cfg.dsl41, "query", what, "-S", cfg.socket, *(["--job", job] if job else [])],
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        said = done.stderr.strip()
        detail = [f"`dsl41 query` said: {said}"] if said and "no engine" not in said else []
        raise NoAnswer(
            said,
            f"no engine answers on {cfg.socket}.",
            *detail,
            f"check `systemctl status {cfg.engine_unit}` and `journalctl -u {cfg.engine_unit}`.",
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


def horizon_wait(stdout: str) -> float | None:
    """The seconds a seal refused inside the retry horizon asks to wait (period-model ss9),
    or None for any other refusal. A live seal prints the engine's answer, and an offline one
    its own answer line, each with the code `seal_retry_horizon` and the time left in its
    field; the prose is never read. A coded answer without the field waits a fixed time
    (DL-412, DL-420)."""
    from dsl41.boundary import RetryHorizonRefused

    for line in stdout.splitlines():
        try:
            answer = json.loads(line)
        except ValueError:
            continue
        if isinstance(answer, dict) and answer.get("code") == HORIZON_CODE:
            left = answer.get(RetryHorizonRefused.FIELD)
            if type(left) is not int:
                return HORIZON_FALLBACK_S
            return left / 1e6 + 1.0
    return None


def seal_refusal(done: subprocess.CompletedProcess[str]) -> str:
    """The reason a seal gave for its refusal, for the line and the refused receipt: the
    answer's `error`, or the seal's last line of its own (DL-412)."""
    for line in done.stdout.splitlines():
        try:
            answer = json.loads(line)
        except ValueError:
            continue
        if isinstance(answer, dict) and answer.get("error"):
            return str(answer["error"]).rstrip(".")
    own = [
        line.strip()
        for line in done.stderr.splitlines()
        if line.strip() and not line.startswith(f"dsl41 {GROUP}:")
    ]
    return own[-1].rstrip(".") if own else f"it exited {done.returncode}"


def still_held(entry: dict[str, Any]) -> bool:
    """A kept hold that leaves the job on hold; a kept entry that names no cause counts as
    one, and so does a ghost's hold carried from a taken-over apply (DL-412)."""
    if entry["state"] != "kept":
        return False
    because = entry.get("kept_because", "rejected")
    return because in STILL_HELD or (because == "undefined" and "carried_from" in entry)


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


def receipt_holds(
    cfg: Config, receipt: dict[str, Any], record: dict[str, Any] | None = None
) -> list[str]:
    """`kept_lines` for a finished apply's holds. A hold the recorded apply RECORD carries from
    that apply is RECORD's to settle, so no release command is printed for it (DL-420)."""
    carried = {
        entry["job"]
        for entry in (record or {}).get("holds") or []
        if entry.get("carried_from") is not None
        and entry.get("carried_from") == receipt.get("request_id")
    }
    holds = [entry for entry in receipt.get("holds") or [] if entry["job"] not in carried]
    return kept_lines(cfg, holds)


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
            lines.append(takeover_line(self.record["previous"]))
        return Stop(EXIT_STOPPED, *lines)

    def run(self) -> int:
        if self.record.get("takeover_of", {}).get("body") is not None:
            self.finish_takeover()
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
        self, kind: str, released: list[str], kept: list[str], **more: Any
    ) -> dict[str, Any]:
        """The receipt of a finished apply, of KIND, in its release's .release directory; an
        applied one is named by .release/latest."""
        receipt = self.receipt_body(kind, released, kept, **more)
        receipt_id = self.record["request_id"] if kind == "applied" else self.record["apply_id"]
        send_receipt(self.cfg, self.name, kind, str(receipt_id), receipt)
        return receipt

    def receipt_body(
        self, kind: str, released: list[str], kept: list[str], **more: Any
    ) -> dict[str, Any]:
        """The body of a KIND receipt; its `outcome` is the kind's (RECEIPT_OUTCOMES)."""
        outcome = RECEIPT_OUTCOMES[kind]
        receipt = {
            key: self.record.get(key)
            for key in (
                "previous",
                "request_id",
                "actor",
                "seal_actor",
                "holds",
                "started_at",
                "takeover_of",
            )
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
        if outcome == "applied":
            receipt["applied_at"] = receipt["finished_at"]
        return receipt

    def clear(self) -> None:
        self.cfg.record.unlink()
        fsync_dir(self.cfg.state)

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
            live = sorted(job for job, row in jobs.items() if row.get("status") in LIVE_OR_QUEUED)
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
                "wait until each is idle, or disarm it, then apply again:",
                *(
                    f"`dsl41 sendevent DISARM -J {shlex.quote(job)} -S {self.cfg.socket}`"
                    for job in moving
                ),
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
            refused = done.returncode == EXIT_REFUSED
            wait = horizon_wait(done.stdout) if refused else None
            if wait is None:
                sys.stderr.write(done.stdout + done.stderr)
            if self.read_estate():
                return EXIT_OK, []
            if done.returncode == EXIT_OK:
                raise Stop(EXIT_STOPPED, "the seal exited 0, and the estate shows no seal.")
            if done.returncode != EXIT_REFUSED:
                return done.returncode, []
            if wait is None:
                return EXIT_REFUSED, [
                    "the seal did not commit.",
                    f"the seal said: {seal_refusal(done)}.",
                ]
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
            if state in UNIT_DOWN:
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
        try:
            header = query(self.cfg, "status")
        except NoAnswer:
            return False
        return bool(
            header.get("baseline_id") == seal["next_baseline"]
            and int(header.get("epoch", 0)) > int(seal["epoch"])
        )

    def verify(self) -> None:
        """The opener's segment opens the committed seal, by the package's opening proof
        (`runner_history.prove_opening`, run on the service side); a leader record follows the
        seal; the manifest pins the bundle the seal committed; and the bundle was read from the
        candidate's files. The problems found are recorded, so `status` and a rollback's
        receipt show them (DL-412, DL-420)."""
        pins = service_json(self.cfg, "pins", self.record["request_id"])
        prefix = f"{self.cfg.releases / self.name}/"
        sealed, period = pins["source_bundle_hash"], pins["next_period"]
        problems = []
        if not pins["opened"]:
            problems.append(f"period {period} has no opening segment")
        elif pins["opening_refusal"] is not None:
            problems.append(f"period {period}'s segment does not open the committed seal")
        if pins["leader_epoch"] is None or pins["leader_epoch"] <= pins["epoch"]:
            problems.append(f"period {period} has no leader record after the seal")
        if pins["manifest_source_bundle_hash"] is None:
            problems.append(f"period {period} has no manifest")
        elif pins["manifest_source_bundle_hash"] != sealed:
            problems.append(f"period {period}'s manifest pins another bundle than {sealed}")
        outside = [path for path in pins["sources"] if not path.startswith(prefix)]
        if outside:
            problems.append(f"the bundle reads {', '.join(outside)}, outside {prefix}")
        problems += pins["unreadable"]
        if problems:
            self.record["problems"] = problems
            self.step(self.record["step"])
            raise Stop(
                EXIT_STOPPED,
                f"the new period does not match the seal: roll back with `{CMD_ROLLBACK}`, or"
                " resume to check again.",
                f"what does not match: {'; '.join(problems)}.",
            )
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
        the record; exit 2, or 1 when a hold stays on, each named with its reason and its
        command, as a completed apply names one (DL-412)."""
        reason = tuple(self.record.setdefault("abort_reason", list(reason)))
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
        lines = kept_lines(self.cfg, receipt["holds"])
        for line in lines:
            say(line)
        return EXIT_KEPT if lines else EXIT_REFUSED

    def take_over(self, target: str) -> int:
        """A rollback's take-over of an apply whose new period opened (DL-412). It releases no
        hold: one rename replaces this apply's record with the apply of TARGET, which owns
        every hold this apply took and has not released, so the jobs stay held through the
        rollback's drain and seal. Each carried hold the apply kept is the successor's to
        settle again at its end, as its own applied hold, so no carried hold is shown as one
        for the operator to release while the rollback runs; the settle still releases only a
        hold the log shows as the apply's own (DL-420). The new record carries this apply's
        receipt to write, verify-failed-<id>.json when its check found problems,
        taken-over-<id>.json when not. There is never a moment with no record."""
        rid = self.record["request_id"]
        problems = list(self.record.get("problems") or [])
        holds = self.record["holds"]
        kind = "verify-failed" if problems else "taken-over"
        body = self.receipt_body(
            kind,
            [e["job"] for e in holds if e["state"] == "released"],
            [e["job"] for e in holds if e["state"] == "kept"],
            problems=problems,
        )
        carried = []
        for entry in (e for e in holds if e["state"] != "released"):
            carried.append(entry | {"carried_from": rid})
            if entry["state"] == "kept":
                carried[-1] |= {"state": "applied"}
                carried[-1].pop("release_attempts", None)
        successor = new_record(
            self.cfg,
            target,
            self.name,
            holds=carried,
            takeover_of={
                "release": self.name,
                "request_id": rid,
                "kind": kind,
                "receipt": f"{kind}-{rid}.json",
                "body": body,
            },
        )
        write_json(self.cfg.record, successor)
        if STOP_AFTER == "takeover":
            os.kill(os.getpid(), signal.SIGKILL)
        return Apply(self.cfg, successor, self.waits).run()

    def finish_takeover(self) -> None:
        """The receipt of the apply taken over, written once; then the apply of its target
        runs as any apply does, with that apply's holds as its own."""
        taken = self.record["takeover_of"]
        path = self.cfg.releases / taken["release"] / ".release" / taken["receipt"]
        if not path.exists():
            send_receipt(
                self.cfg, taken["release"], taken["kind"], taken["request_id"], taken["body"]
            )
        taken["body"] = None
        self.step(self.record["step"])
        say(f"the apply of {taken['release']} is taken over; its receipt is {taken['receipt']}.")


def send_receipt(cfg: Config, name: str, kind: str, receipt_id: str, body: dict[str, Any]) -> None:
    """The service side writes the receipt: nothing under a release is the deployment
    identity's to write (DL-412)."""
    service_run(cfg, "receipt", name, kind, receipt_id, stdin=json.dumps(body))


def begin(cfg: Config, name: str, waits: Waits) -> int:
    """A new apply of NAME, recorded before it acts; the lock is held."""
    previous = read_active(cfg)
    check_prepared(cfg, name)
    if previous == name:
        raise Stop(EXIT_REFUSED, f"{name} is already active.")
    query(cfg, "status")
    record = new_record(cfg, name, previous)
    write_json(cfg.record, record)
    return Apply(cfg, record, waits).run()


def new_record(
    cfg: Config, name: str, previous: str, holds: list[dict[str, Any]] | None = None, **more: Any
) -> dict[str, Any]:
    """The record of a new apply of NAME over PREVIOUS, before its first step."""
    return {
        "candidate": name,
        "previous": previous,
        "step": STEPS[0],
        "apply_id": str(uuid.uuid4()),
        "request_id": None,
        "seal_actor": f"os/{cfg.service_account}",
        "holds": holds or [],
        "actor": f"{getpass.getuser()}@{socket.gethostname()}",
        "started_at": now(),
        **more,
    }


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
            with keeps_record(record):
                check_prepared(cfg, record["candidate"])
        return Apply(cfg, record, waits).run()


@contextmanager
def keeps_record(record: dict[str, Any]) -> Iterator[None]:
    """A refusal before a recorded apply goes on says that the apply stays recorded, with the
    holds it took (DL-412)."""
    try:
        yield
    except Stop as exc:
        raise Stop(
            exc.code,
            *exc.lines,
            f"the apply of {record['candidate']} stays recorded, with its holds.",
        ) from exc


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
            with keeps_record(record):
                check_prepared(cfg, target)
                query(cfg, "status")
            say(f"rolling back from {record['candidate']} to {target}.")
            return Apply(cfg, record, waits).take_over(target)
        previous = read_active(cfg)
        found = rollback_target(cfg, previous)
        if found is None:
            raise Stop(
                EXIT_REFUSED,
                f"no receipt of {previous} names the release it replaced.",
                f"apply the earlier release by name: `{CMD_APPLY} NAME`.",
            )
        failed = failed_check(cfg, found)
        if failed is not None:
            raise Stop(EXIT_REFUSED, failed_line(found, failed))
        say(f"rolling back from {previous} to {found}.")
        return begin(cfg, found, waits)


def rollback_target(cfg: Config, active: str) -> str | None:
    """The release ACTIVE's latest receipt replaced: a second rollback goes forward again.
    When ACTIVE's newest receipt is a taken-over apply's, a rollback whose apply was refused,
    it names the release to go back to (DL-412)."""
    newest = newest_receipt(cfg, active)
    if newest is not None and newest[1].get("outcome") in TAKEN_OVER:
        return str(newest[1]["previous"])
    latest = latest_receipt(cfg, active)
    if latest is None or not latest.get("previous"):
        return None
    return str(latest["previous"])


def newest_receipt(cfg: Config, name: str = "*") -> tuple[Path, dict[str, Any]] | None:
    """The receipt that finished last, of any outcome: of release NAME, or of every release
    by default."""
    newest: tuple[str, Path, dict[str, Any]] | None = None
    for pattern in RECEIPTS:
        for path in cfg.releases.glob(f"{name}/.release/{pattern}"):
            receipt = read_json(path)
            if receipt is None:
                continue
            when = str(receipt.get("finished_at") or receipt.get("applied_at") or "")
            if newest is None or when > newest[0]:
                newest = (when, path, receipt)
    return None if newest is None else (newest[1], newest[2])


def failed_check(cfg: Config, name: str) -> Path | None:
    """The receipt of NAME's last apply when its new period failed the check: a rollback
    never applies such a release, which only `apply NAME` retries (DL-412)."""
    newest = newest_receipt(cfg, name)
    if newest is None or newest[1].get("outcome") != RECEIPT_OUTCOMES["verify-failed"]:
        return None
    return newest[0]


def failed_line(name: str, receipt: Path) -> str:
    return (
        f"{name}'s last apply failed its check after the open ({receipt.name}), so a rollback"
        f" does not apply it; to retry it on purpose, run `{CMD_APPLY} {name}`."
    )


def takeover_line(previous: str) -> str:
    """What a rollback does with an apply whose new period opened (DL-412)."""
    return (
        f"`{CMD_ROLLBACK}` keeps this apply's holds and rolls back to {previous} under them;"
        " the holds are released when that rollback ends."
    )


# ------------------------------------------------------------------- status


def latest_receipt(cfg: Config, name: str) -> dict[str, Any] | None:
    """The receipt of the apply that last made NAME active, which .release/latest names."""
    directory = cfg.releases / name / ".release"
    try:
        latest = (directory / "latest").read_text().strip()
    except FileNotFoundError:
        return None
    return read_json(directory / latest)


def engine_answer(cfg: Config) -> dict[str, Any]:
    try:
        header = query(cfg, "status")
    except NoAnswer as exc:
        return {"answers": False, "error": exc.said}
    except Stop:
        return {"answers": False, "error": "it printed no JSON"}
    return {"answers": True, "baseline_id": header.get("baseline_id"), "epoch": header.get("epoch")}


def ending(receipt: dict[str, Any]) -> str:
    """How a finished apply ended, in one clause."""
    outcome = receipt.get("outcome", "applied")
    if outcome == "applied":
        return f"completed; {receipt['name']} became active in period {receipt.get('period')}"
    if outcome == "refused":
        why = " ".join(receipt.get("reason") or []).rstrip(".")
        return f"refused, and nothing was sealed: {why}"
    if outcome == RECEIPT_OUTCOMES["verify-failed"]:
        problems = "; ".join(receipt.get("problems") or [])
        return f"its new period did not match the seal ({problems}), and a rollback took it over"
    return "a rollback took it over after its new period opened"


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
    last = newest_receipt(cfg)
    failed = failed_check(cfg, target) if target else None
    report = {
        "active": active,
        "inactive": inactive,
        "engine": engine_answer(cfg),
        "rollback_to": None if failed else target,
        "rollback_refused": (
            f"none, since {target}'s last apply failed its check ({failed.name})."
            if failed
            else None
        ),
        "apply": record,
        "last": None if last is None else last[1],
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
        newest = newest_receipt(cfg, active)
        if newest is not None and newest[1].get("outcome") in TAKEN_OVER:
            since += f"; its last apply did not finish, see {newest[0].name}"
        lines.append(f"active: {active}{since}.")
    engine = report["engine"]
    if engine["answers"]:
        lines.append(f"engine: answers on {cfg.socket}.")
    else:
        lines.append(
            f"engine: no answer on {cfg.socket}; check `systemctl status {cfg.engine_unit}`."
        )
    if record is None:
        if report["rollback_refused"]:
            lines.append(f"rollback: {report['rollback_refused']}")
        elif report["rollback_to"]:
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
            lines.append(
                f"  the new period does not match the seal: {'; '.join(record['problems'])}."
            )
        lines.append(f"  `{CMD_RESUME}` will {record['resume']}.")
        if record["step"] in ("opened", "verified"):
            lines.append(f"  {takeover_line(record['previous'])}")
        held = [e["job"] for e in record["holds"] if e["state"] in ("applied", "releasing")]
        pending = [e["job"] for e in record["holds"] if e["state"] == "pending"]
        if record.get("takeover_of"):
            taken = record["takeover_of"]
            lines.append(
                f"  it took over the apply of {taken['release']}, whose receipt is"
                f" {taken['receipt']}."
            )
        if held:
            lines.append(f"  holds taken by this apply: {', '.join(held)}.")
        lines += [f"  {line}" for line in kept_lines(cfg, record["holds"])]
        if pending:
            lines.append(f"  hold sent, decision unknown: {', '.join(pending)}.")
    if last is None:
        lines.append("last apply: none.")
    else:
        lines.append(
            f"last apply: {last['name']} over {last.get('previous')}, started"
            f" {last.get('started_at')} by {last.get('actor')}: {ending(last)}."
        )
        lines += [f"  {line}" for line in receipt_holds(cfg, last, record)]
    lines.append(f"releases: {len(report['releases'])} installed; `--format json` lists each.")
    return lines
