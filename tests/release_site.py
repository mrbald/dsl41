"""The site the release-verb tests run on (DL-407, DL-412): tests/test_release_*.py share it.

Each test lays out a site the way offline-installation installs one: a runtime under
<prefix>/.venvs/, here a venv that imports this build, and a copy of the shipped launcher at
<prefix>/bin/dsl41-launch with only its CONFIGURATION values replaced. `sudo` and `systemctl`
are stubs on PATH, and the launcher's DSL41 is a stub engine that keeps its state in one JSON
file: it answers `query`, `sendevent` and `seal`, and hands `release`, `lint` and `rehearse` to
the runtime's dsl41. Its run root holds what a real engine writes (tests/release_root.py), so
the service side reads it with the package's own readers. The service drill runs the same
verbs against systemd and a real engine.
"""

from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
import sys
import venv
from collections.abc import Iterator
from pathlib import Path
from typing import Any


from release_root import genesis
from test_nightbank_deploy import NB, _configured, _night

SMALL = NB / "estate" / "small"
TESTS = Path(__file__).resolve().parent
ME = pwd.getpwuid(os.geteuid()).pw_name
#: the runtime's entry, with the test-only hook set from its first argument (DL-412)
ENTRY = """import sys
from dsl41 import release
from dsl41.cli import app
release.STOP_AFTER = sys.argv[1] or None
app(args=sys.argv[2:], prog_name="dsl41")
"""

#: the stub engine: its state is state.json beside it (see the module docstring)
STUB_DSL41 = r"""
import json, os, sys, uuid
from pathlib import Path

HERE = Path("@STUB@")
STATE = HERE / "state.json"
REAL = os.environ["RELEASE_TEST_DSL41"]
sys.path.insert(0, "@TESTS@")
args = sys.argv[1:]
if args[:1] in (["release"], ["lint"], ["rehearse"]):
    with (HERE / "forwarded.log").open("a") as log:
        log.write(" ".join(args[:3]) + "\n")
    forced = HERE / f"{args[0]}.exit"
    if forced.exists():
        sys.exit(int(forced.read_text()))
    os.execv(REAL, [REAL, "-m", "dsl41", *args])
state = json.loads(STATE.read_text())
state["calls"].append(args)


def save():
    # One reader at a time sees a whole state: the stub and the test both write it while the
    # verb under test runs `dsl41 query`, so the write lands by rename, never by truncation.
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, STATE)


def opt(name):
    return args[args.index(name) + 1] if name in args else None


def append(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        stream.write(json.dumps(record) + "\n")


root = Path(state["run_root"])
if args[0] == "query":
    save()
    if not state["up"]:
        print("no engine answers", file=sys.stderr)
        sys.exit(2)
    header = {"ok": True, "baseline_id": state["baseline"], "epoch": state["epoch"]}
    if args[1] == "timers":
        print(json.dumps(header | {"timers": state["timers"]}))
    elif "--brief" in args:
        print("\n".join(state["jobs"]))
    else:
        print(json.dumps(header | {"jobs": state["jobs"]}))
    sys.exit(0)
if args[0] == "sendevent":
    verb, job = args[1], opt("-J")
    if not state["up"] or job in state["refuse_send"]:
        save()
        print(f"refused: {verb} {job}", file=sys.stderr)
        sys.exit(2)
    rid = opt("--request-id") or str(uuid.uuid4())
    if rid in state["seen"] and job in state["refuse_resend"]:
        save()
        print(f"refused: {verb} {job} resent", file=sys.stderr)
        sys.exit(2)
    if rid in state["decisions"]:  # an exact retry is answered from the first decision
        code, answer = state["decisions"][rid]
        save()
        print(json.dumps(answer))
        sys.exit(code)
    row = state["jobs"][job]
    if verb == "ON_HOLD" and job in state["operator_holds"]:
        state["operator_holds"].remove(job)
        row["on_hold"], row["state_rev"] = True, row["state_rev"] + 1
    state["index"] += 1
    wal = root / "wal" / f"{state['period']:06d}.jsonl"
    record = {"rec": "input", "seq": state["index"], "request_id": rid, "kind": verb}
    append(wal, record | {"payload": {"job": job}})
    state["seen"].append(rid)
    if job in state["undecided"].get(verb, []):  # the engine died after the input
        state["undecided"][verb].remove(job)
        save()
        print("no decision: this command may still apply", file=sys.stderr)
        sys.exit(4)
    expect = opt("--expect")
    forced = (state["release_outcomes"].get(job) or [None]).pop(0) if verb == "OFF_HOLD" else None
    if forced == 3 or (expect is not None and int(expect) != row["state_rev"]):
        answer = {"ok": False, "decision": "rejected", "request_id": rid, "error": "it moved"}
        state["decisions"][rid] = [3, answer]
        rejected = {"rec": "decision", "index": state["index"], "decision": "rejected"}
        append(wal, rejected | {"legacy_batch": False})
        save()
        print(json.dumps(answer))
        sys.exit(3)
    noop = verb == "ON_HOLD" and job in state["noop_holds"]
    if not noop:
        if verb in ("ON_HOLD", "OFF_HOLD", "ON_NOEXEC", "FORCE_STARTJOB"):
            row["on_hold"] = verb == "ON_HOLD"
        row["state_rev"] += 1
        state["sent"].append([verb, job])
    answer = {"ok": True, "decision": "applied", "request_id": rid}
    answer["revisions"] = {} if noop else {f"job:{job}": row["state_rev"]}
    state["decisions"][rid] = [0, answer]
    decision = {"rec": "decision", "index": state["index"], "decision": "applied"}
    append(wal, decision | {"revisions": answer["revisions"], "legacy_batch": False})
    lost = (verb == "ON_HOLD" and job in state["send_unknown"]) or forced == 4
    if verb == "ON_HOLD" and job in state["send_unknown"]:
        state["send_unknown"].remove(job)
    save()
    if lost:
        print("no decision: this command may still apply", file=sys.stderr)
        sys.exit(4)
    print(json.dumps(answer))
    sys.exit(0)
if args[0] == "seal":
    rid, actor = opt("--request-id"), opt("--claimed-actor")
    for segment in sorted((root / "wal").glob("*.jsonl")):
        last = json.loads(segment.read_text().splitlines()[-1])
        if last.get("rec") == "seal" and last["request_id"] == rid:
            save()
            if last["claimed_actor"] != actor:
                print(f"request_id {rid} already named the boundary under a different envelope")
                sys.exit(2)
            sys.exit(0)  # an exact retry of a committed boundary is answered from it (DL-151)
    outcome = state["seal_outcomes"].pop(0) if state["seal_outcomes"] else "commit"
    state["seal_requests"].append(rid)
    if outcome in ("horizon", "horizon-misworded"):  # the live answer, built by the refusal
        from dsl41.boundary import RetryHorizonRefused

        save()
        ago, left = ("59.500", 500_000) if outcome == "horizon" else ("30.000", 200_000)
        error = (
            f"the last externally requested attempt was {ago}s ago and the closing period's"
            " retry_horizon_us is 60000000: a retry composed under this baseline can no"
            " longer be answered after the boundary. Wait it out, or seal with --force-seal"
        )
        print(json.dumps(RetryHorizonRefused(error, left_us=left).answer()))
        sys.exit(2)
    if outcome == "horizon-unworded":  # a coded answer without the time left
        save()
        print(json.dumps({"ok": False, "code": "seal_retry_horizon", "error": "a retry horizon"}))
        sys.exit(2)
    if outcome == "horizon-offline":  # the offline seal's own refusal and answer line
        from datetime import UTC, datetime, timedelta

        from dsl41.boundary import retry_horizon_gate
        from dsl41.cli_estate import refuse_seal
        from dsl41.runner_clock import EngineError

        at = datetime.now(UTC)
        attempt = {"rec": "input", "seq": 1, "expect": 3, "at": (at - timedelta(seconds=59.5)).isoformat()}
        try:
            retry_horizon_gate(
                [attempt, {"rec": "decision", "index": 1}],
                horizon_us=60_000_000,
                at=at,
                force_seal=False,
            )
        except EngineError as exc:
            save()
            sys.exit(refuse_seal(exc))
        raise SystemExit("stub dsl41: the gate passed")
    if outcome in ("refuse", "unknown"):
        save()
        print(f"seal {outcome}", file=sys.stderr)
        sys.exit(2 if outcome == "refuse" else 4)
    import release_root

    state["next"] = release_root.seal(
        root,
        state["period"],
        request_id=rid,
        actor=state["actor"] or actor,
        epoch=state["epoch"],
        closes_at_index=state["index"],
        files=[args[i + 1] for i, word in enumerate(args) if word == "--next"],
        opened_hash=state["opened_hash"],
    )
    state["up"] = False
    save()
    sys.exit({"commit": 0, "commit-unknown": 4, "commit-refused": 2}[outcome])
raise SystemExit(f"stub dsl41: unexpected {args}")
"""

STUB_SYSTEMCTL = r"""
import json, os, sys
from pathlib import Path

sys.path.insert(0, "@TESTS@")

state_path = Path(sys.argv[0]).resolve().parents[1] / "stub" / "state.json"
state = json.loads(state_path.read_text())
state["systemctl"].append(sys.argv[1:])


def save():
    # The state lands by rename, never by truncation (see STUB_DSL41's save).
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, state_path)

if sys.argv[1] == "show":
    print("active" if state["up"] else "failed")
elif sys.argv[1:] == ["start", "dsl41-engine.service"] and not state["start_fails"]:
    import release_root

    nxt = state["next"]
    state["up"], state["period"], state["baseline"] = True, nxt["period"], nxt["baseline"]
    state["epoch"] += 1
    release_root.open_next(Path(state["run_root"]), nxt["closing"], state["epoch"])
else:
    save()
    sys.exit(1)
save()
"""

#: sudo without its options, each call logged; the grants are the drill's to check
STUB_SUDO = """#!/bin/sh
while [ $# -gt 0 ]; do
    case $1 in
        -n | -H) shift ;;
        -u) shift 2 ;;
        *) break ;;
    esac
done
printf '%s\\n' "$*" >>"$RELEASE_TEST_SUDO_LOG"
exec "$@"
"""


def _script(path: Path, body: str) -> None:
    path.write_text(f"#!{sys.executable}\n{body}" if not body.startswith("#!") else body)
    path.chmod(0o755)


class Site:
    """A site the verbs release into, with the stub engine running period 1."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.releases = base / "releases"
        self.active = base / "active"
        self.root = base / "runs" / "nb"
        self.stub = base / "stub"
        self.prefix = base / "opt"
        for path in (self.prefix / "bin", self.stub, base / "path", self.root):
            path.mkdir(parents=True)
        self.python = _runtime(self.prefix / ".venvs" / "rt")
        # the launcher's DSL41 is the stub, inside the runtime it selects (DL-412)
        self.dsl41 = self.python.parent / "dsl41"
        shutil.copytree(SMALL, self.releases / "r1")
        _seal(self.releases / "r1")
        self.active.write_text("r1\n")
        self.launcher = self.prefix / "bin" / "dsl41-launch"
        _configured(
            base,
            DSL41=str(self.dsl41),
            RUN_ROOT=str(self.root),
            ESTATE_ANCHOR=f"{self.root}.anchor",
            ESTATE_RELEASES=str(self.releases),
            ACTIVE=str(self.active),
            PROPERTIES=str(_night(base)),
            SERVICE_ACCOUNT=ME,
        ).rename(self.launcher)
        _script(
            self.dsl41,
            STUB_DSL41.replace("@STUB@", str(self.stub)).replace("@TESTS@", str(TESTS)),
        )
        _script(base / "path" / "systemctl", STUB_SYSTEMCTL.replace("@TESTS@", str(TESTS)))
        genesis(self.root)
        _script(base / "path" / "sudo", STUB_SUDO)
        self.save(
            {
                "run_root": str(self.root),
                "up": True,
                "period": 1,
                "epoch": 1,
                "baseline": "baseline-1",
                "actor": None,
                "jobs": {
                    "NB_B": _job("BOX"),
                    "NB_MEMBER_C": _job("CMD", box="NB_B"),
                    "NB_FREE_C": _job("CMD"),
                    "NB_HELD_C": _job("CMD", on_hold=True),
                    "NB_IDLE_C": _job("CMD"),
                    "OPS_SPOOL_C": _job("CMD", box="OPS_B"),
                    "NB_ADDED_C": _job("CMD"),
                },
                "timers": [
                    {"due": "2026-10-28T02:00:00", "job": job, "kind": "schedule"}
                    for job in ("NB_B", "NB_MEMBER_C", "NB_FREE_C", "NB_HELD_C")
                ]
                + [{"due": "2026-10-28T03:00:00", "job": "NB_IDLE_C", "kind": "term"}],
                "seal_outcomes": [],
                "seal_requests": [],
                "opened_hash": None,
                "refuse_send": [],
                "decisions": {},
                "operator_holds": [],
                "send_unknown": [],
                "noop_holds": [],
                "release_outcomes": {},
                "refuse_resend": [],
                "undecided": {},
                "seen": [],
                "index": 0,
                "start_fails": False,
                "calls": [],
                "sent": [],
                "systemctl": [],
            }
        )

    def state(self) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads((self.stub / "state.json").read_text())
        return loaded

    def save(self, state: dict[str, Any]) -> None:
        # The stub's reader may run concurrently; the write lands by rename (see STUB_DSL41).
        tmp = self.stub / "state.json.tmp"
        tmp.write_text(json.dumps(state))
        os.replace(tmp, self.stub / "state.json")

    def edit(self, **values: Any) -> None:
        self.save(self.state() | values)

    def environ(self, **more: str) -> dict[str, str]:
        return {
            **os.environ,
            "PATH": f"{self.base / 'path'}:{os.environ['PATH']}",
            "RELEASE_TEST_DSL41": str(self.python),
            "RELEASE_TEST_SUDO_LOG": str(self.base / "sudo.log"),
            **more,
        }

    def command(self, *args: str, stop: str | None = None) -> list[str]:
        """`dsl41 release ARGS` on the site's runtime, the test-only hook set to STOP."""
        return [str(self.python), "-c", ENTRY, stop or "", "release", *args]

    def run(
        self, *args: str, stop: str | None = None, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.command(*args, stop=stop),
            capture_output=True,
            text=True,
            timeout=300,
            env=self.environ(**(env or {})),
        )

    def source(self, name: str, *, add_job: bool = False, change_job: bool = False) -> Path:
        """A release's inputs: the small estate, with one more file when `add_job` and
        OPS_SPOOL_C's command changed when `change_job`."""
        source = self.base / "sources" / name
        shutil.copytree(SMALL, source)
        if change_job:
            jil = source / "global.jil"
            jil.write_text(
                jil.read_text().replace("OPS_SPOOL_C --sleep 240", "OPS_SPOOL_C --sleep 241")
            )
        if add_job:
            (source / "added.jil").write_text(
                "insert_job: NB_ADDED_C\njob_type: c\ncommand: echo added\n"
                "machine: localhost\nowner: dsl41\n"
            )
            manifest = source / "estate.toml"
            text = manifest.read_text().replace('"infra.jil"]', '"infra.jil", "added.jil"]')
            manifest.write_text(text)
        return source

    def prepared(self, name: str, **kwargs: bool) -> None:
        done = self.run("prepare", name, str(self.source(name, **kwargs)))
        assert done.returncode == 0, done.stderr

    def record(self) -> dict[str, Any] | None:
        path = self.active.parent / ".release" / "apply.json"
        return json.loads(path.read_text()) if path.exists() else None

    def receipt(self, name: str, kind: str = "applied") -> dict[str, Any]:
        directory = self.releases / name / ".release"
        file = (directory / "latest").read_text().strip() if kind == "applied" else f"{kind}.json"
        loaded: dict[str, Any] = json.loads((directory / file).read_text())
        return loaded

    def stub_cli(self, *args: str) -> None:
        """The stub engine's CLI, as an operator runs it."""
        env = {**os.environ, "RELEASE_TEST_DSL41": str(self.python)}
        subprocess.run([str(self.dsl41), *args], check=True, env=env, capture_output=True)

    def sudo_log(self) -> list[str]:
        """What ran through sudo, the stub engine's path shortened to `dsl41`."""
        text = (self.base / "sudo.log").read_text()
        return text.replace(str(self.dsl41), "dsl41").splitlines()

    def seals(self) -> list[list[str]]:
        return [call for call in self.state()["calls"] if call[0] == "seal"]


def _seal(release: Path) -> None:
    """A release installed sealed, as the service side installs one: here the test's account
    is the service account (DL-412)."""
    for directory, _, files in os.walk(release, topdown=False):
        for file in files:
            os.chmod(Path(directory) / file, 0o444)
        os.chmod(directory, 0o555)


def _runtime(path: Path) -> Path:
    """An installed runtime's stand-in at PATH: a venv that imports this build; its python."""
    venv.EnvBuilder(symlinks=True, with_pip=False).create(path)
    (site_packages,) = path.glob("lib/python*/site-packages")
    entries = [entry for entry in sys.path if entry and Path(entry).is_dir()]
    (site_packages / "dsl41_test.pth").write_text("\n".join(entries) + "\n")
    return path / "bin" / "python"


def _job(job_type: str, *, box: str | None = None, on_hold: bool = False) -> dict[str, Any]:
    return {
        "status": "INACTIVE",
        "on_hold": on_hold,
        "box_name": box,
        "job_type": job_type,
        "state_rev": 3,
        "armed": False,
        "pending_timers": [],
    }


def site_at(tmp_path: Path) -> Iterator[Site]:
    """The `site` fixture's body: a Site under TMP_PATH, its read-only directories opened
    again afterwards so pytest can remove them."""
    yield Site(tmp_path)
    for directory, dirs, _ in os.walk(tmp_path):
        for name in dirs:
            os.chmod(Path(directory) / name, 0o755)
