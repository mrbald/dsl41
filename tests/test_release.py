"""DL-407, DL-412: the release train's verbs, `dsl41 release prepare|apply|resume|rollback|status`,
and the hidden `dsl41 release service` they run through sudo.

Each test lays out a site the way offline-installation installs one: a runtime under
<prefix>/.venvs/, here a venv that imports this build, and a copy of the shipped launcher at
<prefix>/bin/dsl41-launch with only its CONFIGURATION values replaced. `sudo` and `systemctl`
are stubs on PATH, and the launcher's DSL41 is a stub engine that keeps its state in one JSON
file: it answers `query`, `sendevent` and `seal`, and hands `release`, `lint` and `rehearse` to
the runtime's dsl41. The service drill runs the same verbs against systemd and a real engine.
"""

from __future__ import annotations

import fcntl
import json
import os
import pwd
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import venv
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from test_nightbank_deploy import NB, _configured, _night

SMALL = NB / "estate" / "small"
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
import hashlib, json, os, sys, uuid
from pathlib import Path

HERE = Path("@STUB@")
STATE = HERE / "state.json"
REAL = os.environ["RELEASE_TEST_DSL41"]
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
    STATE.write_text(json.dumps(state))


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
        append(wal, {"rec": "decision", "index": state["index"], "decision": "rejected"})
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
    append(wal, decision | {"revisions": answer["revisions"]})
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
    if outcome in ("horizon", "horizon-unworded"):
        save()
        error = (
            "the last externally requested attempt was 59.500s ago and the closing period's"
            " retry_horizon_us is 60000000: a retry composed under this baseline can no"
            " longer be answered after the boundary. Wait it out, or seal with --force-seal"
        )
        if outcome == "horizon-unworded":
            error = "a retry horizon"
        print(json.dumps({"ok": False, "code": "seal_retry_horizon", "error": error}))
        sys.exit(2)
    if outcome == "horizon-offline":  # the offline seal's own refusal, rendered by its code
        from datetime import UTC, datetime, timedelta

        from dsl41.boundary import retry_horizon_gate
        from dsl41.cli_common import refuse
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
            sys.exit(refuse(exc))
        raise SystemExit("stub dsl41: the gate passed")
    if outcome in ("refuse", "unknown"):
        save()
        print(f"seal {outcome}", file=sys.stderr)
        sys.exit(2 if outcome == "refuse" else 4)
    files = [args[i + 1] for i, word in enumerate(args) if word == "--next"]
    address = "sha256:" + hashlib.sha256("\0".join(files).encode()).hexdigest()
    closing, period = state["period"], state["period"] + 1
    baseline = f"baseline-{period}"
    nxt = {"period_id": period, "baseline_id": baseline, "source_bundle_hash": address}
    seal = {"epoch": state["epoch"], "boundary_request": {"request_id": rid}, "next_period": nxt}
    (root / "seals").mkdir(parents=True, exist_ok=True)
    (root / "seals" / f"{closing:06d}.json").write_text(json.dumps(seal))
    opened = state["opened_hash"] or address
    manifest = root / "periods" / f"{period:06d}"
    manifest.mkdir(parents=True)
    (manifest / "manifest.json").write_text(json.dumps({"source_bundle_hash": opened}))
    bundle = root / "catalogs" / opened.split(":")[-1]
    bundle.mkdir(parents=True, exist_ok=True)
    sources = [{"path": path} for path in files]
    (bundle / "sources.json").write_text(json.dumps({"sources": sources}))
    record = {"rec": "seal", "period_id": closing, "request_id": rid, "next_period_id": period}
    append(root / "wal" / f"{closing:06d}.jsonl", record | {"claimed_actor": state["actor"] or actor})
    state["next"] = {"period": period, "baseline": baseline, "hash": opened, "closing": closing}
    state["up"] = False
    save()
    sys.exit({"commit": 0, "commit-unknown": 4, "commit-refused": 2}[outcome])
raise SystemExit(f"stub dsl41: unexpected {args}")
"""

STUB_SYSTEMCTL = r"""
import json, sys
from pathlib import Path

state_path = Path(sys.argv[0]).resolve().parents[1] / "stub" / "state.json"
state = json.loads(state_path.read_text())
state["systemctl"].append(sys.argv[1:])
if sys.argv[1] == "show":
    print("active" if state["up"] else "failed")
elif sys.argv[1:] == ["start", "dsl41-engine.service"] and not state["start_fails"]:
    nxt = state["next"]
    state["up"], state["period"], state["baseline"] = True, nxt["period"], nxt["baseline"]
    state["epoch"] += 1
    segment = Path(state["run_root"]) / "wal" / f"{nxt['period']:06d}.jsonl"
    segment.write_text(
        json.dumps(
            {
                "rec": "segment",
                "baseline_id": nxt["baseline"],
                "source_bundle_hash": nxt["hash"],
                "opens_from_seal": {"period_id": nxt["closing"], "digest": "sha256:x"},
            }
        )
        + "\n"
        + json.dumps({"rec": "leader", "epoch": state["epoch"]})
        + "\n"
    )
else:
    state_path.write_text(json.dumps(state))
    sys.exit(1)
state_path.write_text(json.dumps(state))
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
            STUB_DSL41.replace("@STUB@", str(self.stub)),
        )
        _script(base / "path" / "systemctl", STUB_SYSTEMCTL)
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
        (self.stub / "state.json").write_text(json.dumps(state))

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
        path = self.releases / ".apply.json"
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


@pytest.fixture
def site(tmp_path: Path) -> Iterator[Site]:
    yield Site(tmp_path)
    for directory, dirs, _ in os.walk(tmp_path):
        for name in dirs:
            os.chmod(Path(directory) / name, 0o755)


# ------------------------------------------------------------------ prepare


def test_prepare_installs_a_read_only_release_with_its_qualification(site: Site) -> None:
    done = site.run("prepare", "v2", str(site.source("v2", add_job=True)))
    assert done.returncode == 0, done.stderr
    release = site.releases / "v2"
    assert not (site.releases / "v2.new").exists()
    prepared = site.receipt("v2", "prepared")
    assert prepared["lint"]["exit"] == 0 and prepared["rehearse"]["exit"] == 0
    assert prepared["jobs"] == 83 and prepared["max_jobs"] == 107
    assert prepared["grace_s"] == 10.0 and prepared["max_grace_s"] == 10.0
    assert prepared["dsl41_version"] and prepared["environment"] == "wargame"
    assert set(prepared["files"]) >= {"estate.toml", "global.jil", "added.jil"}
    assert prepared["files"]["added.jil"].startswith("sha256:")
    assert prepared["lint"]["argv"][:2] == [str(site.dsl41), "lint"]
    assert f"{release}/added.jil" in prepared["rehearse"]["argv"]
    assert prepared["rehearse"]["argv"][-4:] == [
        "-p",
        str(site.base / "night" / "night.properties"),
        "--format",
        "json",
    ]
    assert not os.access(release / "global.jil", os.W_OK)
    assert not os.access(release, os.W_OK)
    assert os.access(release / ".release", os.W_OK)
    # the check ran as the service account, through the launcher's DSL41
    assert site.sudo_log() == ["dsl41 release service -- check v2"]


def test_prepare_refuses_a_name_that_exists_and_a_source_without_a_manifest(site: Site) -> None:
    """A prepared name is installed once; a name installed by hand points at --requalify."""
    site.prepared("v1")
    refused = site.run("prepare", "v1", str(site.source("again")))
    assert refused.returncode == 2
    assert f"{site.releases / 'v1'} is prepared already" in refused.stderr
    refused = site.run("prepare", "r1", str(site.source("again2")))
    assert refused.returncode == 2
    assert f"{site.releases / 'r1'} exists and is not prepared" in refused.stderr
    assert "`dsl41 release prepare --requalify r1`" in refused.stderr
    empty = site.base / "empty"
    empty.mkdir()
    refused = site.run("prepare", "v9", str(empty))
    assert refused.returncode == 2 and "holds no estate.toml" in refused.stderr
    for name in ("../x", ".hidden", "a/b", "v9.new"):
        refused = site.run("prepare", name, str(site.source("other" + str(len(name)))))
        assert refused.returncode == 2 and "is not a release name" in refused.stderr
    assert sorted(path.name for path in site.releases.iterdir()) == ["r1", "v1"]


def test_prepare_names_its_missing_argument(site: Site) -> None:
    """DL-412: a usage error names the argument it lacks."""
    missing = site.run("prepare", "v2")
    assert missing.returncode == 2
    assert "SOURCE_DIR" in missing.stderr and "unless --requalify" in missing.stderr
    both = site.run("prepare", "--requalify", "v2", str(site.source("v2")))
    assert both.returncode == 2 and "--requalify takes no SOURCE_DIR" in both.stderr
    nothing = site.run("apply")
    assert nothing.returncode == 2 and "Missing argument" in nothing.stderr


@pytest.mark.parametrize(
    ("values", "says"),
    [
        ({"MAX_JOBS": "81"}, "it holds 82 jobs, above the site's ceiling of 81 (MAX_JOBS)."),
        (
            {"MAX_GRACE_S": "5"},
            "the runtime's command grace, 10 s, is above the site's MAX_GRACE_S, 5 s.",
        ),
    ],
)
def test_prepare_refuses_a_release_above_the_launchers_ceiling(
    site: Site, values: dict[str, str], says: str
) -> None:
    text = site.launcher.read_text()
    for key, value in values.items():
        text = re.sub(rf"^{key}=\d+$", f"{key}={value}", text, count=1, flags=re.M)
    site.launcher.write_text(text)
    refused = site.run("prepare", "v2", str(site.source("v2")))
    assert refused.returncode == 2, refused.stderr
    assert says in refused.stderr
    assert "nothing is installed under v2, so the name is free again." in refused.stderr
    if "MAX_GRACE_S" in values:
        assert "ask the site package's owner" in refused.stderr
    assert not (site.releases / "v2").exists()


@pytest.mark.parametrize("verb", ["lint", "rehearse"])
def test_prepare_refuses_a_release_lint_or_rehearse_refuses(site: Site, verb: str) -> None:
    (site.stub / f"{verb}.exit").write_text("1")
    refused = site.run("prepare", "v2", str(site.source("v2")))
    assert refused.returncode == 2
    assert f"{verb} exited 1" in refused.stderr
    assert not (site.releases / "v2").exists()
    # a lint that fails runs no rehearse, so a lowering error prints once (DL-412)
    forwarded = (site.stub / "forwarded.log").read_text().splitlines()
    assert [line.split()[0] for line in forwarded].count("rehearse") == (verb == "rehearse")


def test_prepare_refuses_a_release_that_holds_a_symlink(site: Site) -> None:
    """DL-412: prepare's copy holds no symlink, so the service side refuses one in place."""
    raw = site.releases / "raw"
    shutil.copytree(SMALL, raw)
    (raw / "link.jil").symlink_to(raw / "global.jil")
    refused = site.run("prepare", "--requalify", "raw")
    assert refused.returncode == 2 and "raw holds symlinks: link.jil." in refused.stderr
    assert not (raw / ".release" / "prepared.json").exists()


# -------------------------------------------------------------------- apply


def test_apply_holds_drains_seals_selects_starts_verifies_and_releases_its_own_holds(
    site: Site,
) -> None:
    site.prepared("v2", add_job=True)
    done = site.run("apply", "v2")
    assert done.returncode == 0, done.stderr
    assert site.active.read_text() == "v2\n"
    assert site.record() is None
    state = site.state()
    # a top-level scheduled job is held; a box member, an earlier hold and a timer that is
    # not a schedule tick are not, and only this apply's hold is released
    assert state["sent"] == [["ON_HOLD", "NB_B"], ["ON_HOLD", "NB_FREE_C"]] + [
        ["OFF_HOLD", "NB_B"],
        ["OFF_HOLD", "NB_FREE_C"],
    ]
    assert state["jobs"]["NB_HELD_C"]["on_hold"] is True
    # each hold and each release is sent under a request id the helper recorded first
    sends = [call for call in state["calls"] if call[0] == "sendevent"]
    assert len(sends) == 4 and all("--request-id" in call for call in sends)
    (seal,) = site.seals()
    receipt = site.receipt("v2")
    assert receipt["previous"] == "r1" and receipt["period"] == 2
    assert [hold["job"] for hold in receipt["holds"]] == ["NB_B", "NB_FREE_C"]
    assert receipt["released"] == ["NB_B", "NB_FREE_C"] and receipt["kept"] == []
    assert (site.releases / "v2" / ".release" / f"applied-{receipt['request_id']}.json").exists()
    assert receipt["outcome"] == "applied" and receipt["actor"].startswith(f"{ME}@")
    assert "holds taken: NB_B, NB_FREE_C." in done.stderr
    assert done.stderr.endswith("dsl41 release: v2 is active in period 2.\n")
    assert seal[:5] == [
        "seal",
        "--run-root",
        str(site.root),
        "--estate-anchor",
        f"{site.root}.anchor",
    ]
    nexts = [seal[i + 1] for i, word in enumerate(seal) if word == "--next"]
    assert nexts[-1] == f"{site.releases / 'v2'}/added.jil" and len(nexts) == 7
    tail = seal[seal.index("-p") :]
    assert tail[:2] == ["-p", str(site.base / "night" / "night.properties")]
    assert tail[2:6] == [
        "--next-detached",
        "--next-as-machine",
        "localhost",
        "--next-machine-policy",
    ]
    # the service side claims the service account's actor itself (DL-412)
    assert tail[-4:] == ["--claimed-actor", f"os/{ME}", "--request-id", receipt["request_id"]]
    assert ["start", "dsl41-engine.service"] in state["systemctl"]
    rid = receipt["request_id"]
    assert site.sudo_log()[1:] == [
        "dsl41 release service -- changes r1 v2",
        f"dsl41 release service -- seal v2 {rid}",
        f"dsl41 release service -- committed {rid}",
        "systemctl start dsl41-engine.service",
        f"dsl41 release service -- pins {rid}",
        *(f"dsl41 release service -- holds {hold['job']}" for hold in receipt["holds"]),
    ]


def test_a_held_job_a_tick_latched_is_released(site: Site) -> None:
    """A tick that lands on a held job arms it and moves its revision; the helper's own
    hold is still released, so the latched start runs (deployment-runbook ss6, step 1)."""
    site.prepared("v2")
    site.edit(seal_outcomes=["unknown"])
    assert site.run("apply", "v2").returncode == 4
    state = site.state()
    state["jobs"]["NB_FREE_C"]["state_rev"] += 1
    state["jobs"]["NB_FREE_C"]["armed"] = True
    site.save(state)
    resumed = site.run("resume")
    assert resumed.returncode == 0, resumed.stderr
    receipt = site.receipt("v2")
    assert receipt["released"] == ["NB_B", "NB_FREE_C"] and receipt["kept"] == []
    assert site.state()["jobs"]["NB_FREE_C"]["on_hold"] is False


def test_a_pending_hold_is_sent_again_under_its_id_on_resume(site: Site) -> None:
    site.prepared("v2")
    site.edit(send_unknown=["NB_FREE_C"])
    stopped = site.run("apply", "v2")
    assert stopped.returncode == 3, stopped.stderr
    record = site.record()
    assert record is not None and record["step"] == "started"
    (pending,) = [entry for entry in record["holds"] if entry["job"] == "NB_FREE_C"]
    assert pending["state"] == "pending"
    resumed = site.run("resume")
    assert resumed.returncode == 0, resumed.stderr
    holds = [
        call[call.index("--request-id") + 1]
        for call in site.state()["calls"]
        if call[:2] == ["sendevent", "ON_HOLD"] and call[call.index("-J") + 1] == "NB_FREE_C"
    ]
    assert holds == [pending["request_id"], pending["request_id"]]
    assert ["ON_HOLD", "NB_FREE_C"] in site.state()["sent"]
    assert site.state()["sent"].count(["ON_HOLD", "NB_FREE_C"]) == 1
    assert "NB_FREE_C" in site.receipt("v2")["released"]


def test_a_hold_the_helper_never_applied_is_never_released(site: Site) -> None:
    """An operator holds NB_FREE_C between the helper's read and its send: the helper's
    ON_HOLD is rejected, the re-read finds the job held, and its hold stays."""
    site.prepared("v2")
    site.edit(operator_holds=["NB_FREE_C"])
    done = site.run("apply", "v2")
    assert done.returncode == 0, done.stderr
    receipt = site.receipt("v2")
    assert [entry["state"] for entry in receipt["holds"]] == ["released", "rejected"]
    assert receipt["released"] == ["NB_B"]
    assert ["OFF_HOLD", "NB_FREE_C"] not in site.state()["sent"]
    assert site.state()["jobs"]["NB_FREE_C"]["on_hold"] is True


def test_a_hold_that_moved_no_revision_is_a_no_op_and_never_released(site: Site) -> None:
    """ON_HOLD on a RUNNING job applies as a no-op: the answer's `revisions` has no entry
    for the job. That hold is not the helper's, so an operator's later hold stays."""
    site.prepared("v2")
    site.edit(noop_holds=["NB_FREE_C"], seal_outcomes=["unknown"])
    assert site.run("apply", "v2").returncode == 4
    record = site.record()
    assert record is not None
    (entry,) = [hold for hold in record["holds"] if hold["job"] == "NB_FREE_C"]
    assert entry["state"] == "noop" and "rev" not in entry
    state = site.state()
    state["jobs"]["NB_FREE_C"]["on_hold"] = True  # an operator's first hold
    site.save(state)
    resumed = site.run("resume")
    assert resumed.returncode == 0, resumed.stderr
    assert ["OFF_HOLD", "NB_FREE_C"] not in site.state()["sent"]
    assert site.receipt("v2")["released"] == ["NB_B"]


def test_an_applied_hold_records_the_revision_its_answer_carries(site: Site) -> None:
    site.prepared("v2")
    assert site.run("apply", "v2").returncode == 0
    holds = {hold["job"]: hold for hold in site.receipt("v2")["holds"]}
    assert holds["NB_FREE_C"]["rev"] == 4 and holds["NB_FREE_C"]["state"] == "released"


def test_an_operators_release_and_rehold_after_the_helpers_hold_is_kept(site: Site) -> None:
    """The run root's log shows an OFF_HOLD of NB_FREE_C from another request after the
    helper's hold applied: the hold there now is the operator's, and it stays."""
    site.prepared("v2")
    site.edit(seal_outcomes=["unknown"])
    assert site.run("apply", "v2").returncode == 4
    for verb in ("OFF_HOLD", "ON_HOLD"):
        site.stub_cli("sendevent", verb, "-J", "NB_FREE_C", "-S", "x")
    resumed = site.run("resume")
    # done, with a hold kept: exit 1, the hold named with the command that takes it off
    assert resumed.returncode == 1, resumed.stderr
    receipt = site.receipt("v2")
    assert receipt["released"] == ["NB_B"] and receipt["kept"] == ["NB_FREE_C"]
    assert (
        "NB_FREE_C stays on hold: another request changed its hold after this apply held it"
        " (OFF_HOLD" in resumed.stderr
    )
    assert (
        "that hold may be another operator's; agree with them before you release it:"
        f" `dsl41 sendevent OFF_HOLD -J NB_FREE_C -S {site.root}/control.sock`." in resumed.stderr
    )
    assert site.state()["jobs"]["NB_FREE_C"]["on_hold"] is True
    shown = site.run("status").stdout
    assert "  NB_FREE_C stays on hold: another request changed its hold" in shown


def test_a_hold_moved_by_noexec_and_set_again_by_an_operator_is_kept(site: Site) -> None:
    """ON_NOEXEC supersedes the helper's hold, OFF_NOEXEC leaves the job unheld, and an
    operator's ON_HOLD holds it again: no OFF_HOLD is in the log, and still the hold there
    now is not the helper's, so it stays."""
    site.prepared("v2")
    site.edit(seal_outcomes=["unknown"])
    assert site.run("apply", "v2").returncode == 4
    for verb in ("ON_NOEXEC", "OFF_NOEXEC", "ON_HOLD"):
        site.stub_cli("sendevent", verb, "-J", "NB_FREE_C", "-S", "x")
    resumed = site.run("resume")
    assert resumed.returncode == 1, resumed.stderr
    receipt = site.receipt("v2")
    assert receipt["released"] == ["NB_B"] and receipt["kept"] == ["NB_FREE_C"]
    assert "after this apply held it (ON_NOEXEC" in resumed.stderr
    assert site.state()["jobs"]["NB_FREE_C"]["on_hold"] is True


def test_a_hold_resent_and_refused_is_settled_by_the_log(site: Site) -> None:
    """The ON_HOLD applied and its answer was lost; the resume's resend is refused
    outright, and the log shows the hold applied, so it is the helper's to release."""
    site.prepared("v2")
    site.edit(send_unknown=["NB_FREE_C"], refuse_resend=["NB_FREE_C"])
    assert site.run("apply", "v2").returncode == 3
    resumed = site.run("resume")
    assert resumed.returncode == 0, resumed.stderr
    (entry,) = [hold for hold in site.receipt("v2")["holds"] if hold["job"] == "NB_FREE_C"]
    assert entry["state"] == "released" and entry["rev"] == 4
    assert site.state()["jobs"]["NB_FREE_C"]["on_hold"] is False


def test_a_release_resent_and_refused_is_settled_by_the_log(site: Site) -> None:
    site.prepared("v2")
    site.edit(release_outcomes={"NB_FREE_C": [4]}, refuse_resend=["NB_FREE_C"])
    assert site.run("apply", "v2").returncode == 3
    resumed = site.run("resume")
    assert resumed.returncode == 0, resumed.stderr
    assert site.receipt("v2")["released"] == ["NB_B", "NB_FREE_C"]
    assert site.state()["sent"].count(["OFF_HOLD", "NB_FREE_C"]) == 1


@pytest.mark.parametrize(
    ("verb", "says", "state"),
    [
        ("ON_HOLD", "the engine has not decided the hold of NB_FREE_C yet", "pending"),
        (
            "OFF_HOLD",
            "the engine has not decided the release of NB_FREE_C yet",
            "releasing",
        ),
    ],
)
def test_a_resend_whose_first_send_the_log_shows_undecided_stops_with_its_record(
    site: Site, verb: str, says: str, state: str
) -> None:
    """The engine logged the input and died before its decision; a resend is refused, and
    recovery may still apply the input, so the apply stops and keeps its record."""
    site.prepared("v2")
    site.edit(undecided={verb: ["NB_FREE_C"]}, refuse_resend=["NB_FREE_C"])
    assert site.run("apply", "v2").returncode == 3
    stopped = site.run("resume")
    assert stopped.returncode == 3, stopped.stderr
    assert f"{says}; resume once it answers" in stopped.stderr
    record = site.record()
    assert record is not None
    (entry,) = [hold for hold in record["holds"] if hold["job"] == "NB_FREE_C"]
    assert entry["state"] == state
    assert site.active.read_text() == ("r1\n" if verb == "ON_HOLD" else "v2\n")


def test_a_rejected_release_is_settled_again_and_sent_under_a_new_id(site: Site) -> None:
    """A tick that latches the job between the expect's read and the OFF_HOLD rejects it
    once; the helper reads the row again and releases the hold under a new id."""
    site.prepared("v2")
    site.edit(release_outcomes={"NB_FREE_C": [3]})
    done = site.run("apply", "v2")
    assert done.returncode == 0, done.stderr
    receipt = site.receipt("v2")
    assert receipt["released"] == ["NB_B", "NB_FREE_C"] and receipt["kept"] == []
    ids = [
        call[call.index("--request-id") + 1]
        for call in site.state()["calls"]
        if call[:2] == ["sendevent", "OFF_HOLD"] and "NB_FREE_C" in call
    ]
    assert len(ids) == 2 and ids[0] != ids[1]
    assert site.state()["jobs"]["NB_FREE_C"]["on_hold"] is False


def test_a_release_rejected_three_times_is_kept_with_its_reason(site: Site) -> None:
    site.prepared("v2")
    site.edit(release_outcomes={"NB_FREE_C": [3, 3, 3]})
    done = site.run("apply", "v2")
    assert done.returncode == 1, done.stderr
    receipt = site.receipt("v2")
    assert receipt["released"] == ["NB_B"] and receipt["kept"] == ["NB_FREE_C"]
    (entry,) = [hold for hold in receipt["holds"] if hold["job"] == "NB_FREE_C"]
    assert entry["reason"] == "the engine did not release it (OFF_HOLD exited 3: it moved)"
    assert entry["kept_because"] == "rejected"
    assert (
        "NB_FREE_C stays on hold: the engine did not release it (OFF_HOLD exited 3: it moved)."
        in done.stderr
    )
    assert "release it once it is safe: `dsl41 sendevent OFF_HOLD -J NB_FREE_C" in done.stderr
    assert done.stderr.endswith("dsl41 release: v2 is active in period 2.\n")


def test_a_release_with_no_decision_stops_and_resume_resends_it_under_its_id(
    site: Site,
) -> None:
    site.prepared("v2")
    site.edit(release_outcomes={"NB_FREE_C": [4]})
    stopped = site.run("apply", "v2")
    assert stopped.returncode == 3, stopped.stderr
    record = site.record()
    assert record is not None and record["step"] == "verified"
    (entry,) = [hold for hold in record["holds"] if hold["job"] == "NB_FREE_C"]
    assert entry["state"] == "releasing"
    resumed = site.run("resume")
    assert resumed.returncode == 0, resumed.stderr
    offs = [
        call[call.index("--request-id") + 1]
        for call in site.state()["calls"]
        if call[:2] == ["sendevent", "OFF_HOLD"] and "NB_FREE_C" in call
    ]
    assert offs == [entry["release_id"], entry["release_id"]]
    assert site.state()["sent"].count(["OFF_HOLD", "NB_FREE_C"]) == 1
    assert site.receipt("v2")["released"] == ["NB_B", "NB_FREE_C"]


def test_a_refused_hold_is_rejected_and_refuses_the_apply(site: Site) -> None:
    site.prepared("v2")
    site.edit(refuse_send=["NB_FREE_C"])
    refused = site.run("apply", "v2")
    assert refused.returncode == 2, refused.stderr
    assert "the engine refused to hold NB_FREE_C: refused: ON_HOLD NB_FREE_C" in refused.stderr
    assert site.record() is None and site.active.read_text() == "r1\n"
    assert site.state()["sent"] == [["ON_HOLD", "NB_B"], ["OFF_HOLD", "NB_B"]]


def test_a_seal_that_does_not_commit_promotes_nothing_and_releases_the_holds(
    site: Site,
) -> None:
    site.prepared("v2")
    site.edit(seal_outcomes=["refuse"])
    refused = site.run("apply", "v2")
    assert refused.returncode == 2, refused.stderr
    assert "the seal did not commit: seal refuse." in refused.stderr
    assert "v2 is not applied; nothing was sealed, and ACTIVE still names r1." in refused.stderr
    assert "this apply's holds are released: NB_B, NB_FREE_C;" in refused.stderr
    assert site.active.read_text() == "r1\n"
    assert site.record() is None
    assert not (site.releases / "v2" / ".release" / "latest").exists()
    # a refused apply leaves its receipt, and status says how it ended (DL-412)
    (path,) = (site.releases / "v2" / ".release").glob("refused-*.json")
    receipt = json.loads(path.read_text())
    assert receipt["outcome"] == "refused" and receipt["released"] == ["NB_B", "NB_FREE_C"]
    # the seal's own reason is recorded, so `status` can say it later (DL-412)
    assert receipt["reason"] == ["the seal did not commit: seal refuse."]
    shown = site.run("status").stdout
    assert "last apply: v2 over r1, started " in shown
    (line,) = [line for line in shown.splitlines() if line.startswith("last apply:")]
    assert line.endswith(": refused, and nothing was sealed: the seal did not commit: seal refuse.")
    state = site.state()
    assert [verb for verb, _ in state["sent"]] == ["ON_HOLD", "ON_HOLD", "OFF_HOLD", "OFF_HOLD"]
    assert ["start", "dsl41-engine.service"] not in state["systemctl"]


def test_a_refused_apply_whose_hold_the_engine_will_not_release_stays_recorded(
    site: Site,
) -> None:
    """DL-412: exit 2 promises the apply's holds are released. When the engine rejects the
    release of one three times, the refused apply keeps its record, exit 3, and a resume
    tries again before it clears the record."""
    site.prepared("v2")
    site.edit(seal_outcomes=["refuse"], release_outcomes={"NB_FREE_C": [3, 3, 3]})
    stopped = site.run("apply", "v2")
    assert stopped.returncode == 3, stopped.stderr
    assert "the seal did not commit: seal refuse." in stopped.stderr
    assert "the engine did not release this apply's hold of NB_FREE_C." in stopped.stderr
    record = site.record()
    assert record is not None and record["step"] == "aborting"
    assert not list((site.releases / "v2" / ".release").glob("refused-*.json"))
    assert site.state()["jobs"]["NB_FREE_C"]["on_hold"] is True
    resumed = site.run("resume")
    assert resumed.returncode == 2, resumed.stderr
    assert "the seal did not commit: seal refuse." in resumed.stderr
    assert site.record() is None and site.state()["jobs"]["NB_FREE_C"]["on_hold"] is False
    (path,) = (site.releases / "v2" / ".release").glob("refused-*.json")
    assert json.loads(path.read_text())["released"] == ["NB_B", "NB_FREE_C"]


def test_a_seal_that_exits_2_after_it_committed_is_read_from_the_estate(site: Site) -> None:
    site.prepared("v2")
    site.edit(seal_outcomes=["commit-refused"])
    done = site.run("apply", "v2")
    assert done.returncode == 0, done.stderr
    assert "the estate shows" in done.stderr and site.active.read_text() == "v2\n"


def test_an_unknown_seal_keeps_its_record_and_resume_reads_the_estate_then_retries(
    site: Site,
) -> None:
    site.prepared("v2")
    site.edit(seal_outcomes=["unknown"])
    unknown = site.run("apply", "v2")
    assert unknown.returncode == 4, unknown.stderr
    record = site.record()
    assert record is not None and record["step"] == "sealing"
    request_id = record["request_id"]
    assert record["seal_actor"] == f"os/{ME}"
    assert "the seal's outcome is unknown: it exited 4" in unknown.stderr
    assert "the scheduled jobs stay on hold, and the engine may have exited." in unknown.stderr
    assert "sends the seal again only under that id." in unknown.stderr
    assert "ss6a" not in unknown.stderr and "step" not in unknown.stderr
    assert site.active.read_text() == "r1\n"
    shown = site.run("status")
    assert "recorded apply: v2 over r1, started " in shown.stdout
    assert f"ask the estate whether the seal under request id {request_id}" in shown.stdout
    assert "  holds taken by this apply: NB_B, NB_FREE_C." in shown.stdout
    assert "request id None" not in shown.stdout
    # a plain apply and a rollback are refused while one is recorded before its period opened
    again = site.run("apply", "v2")
    assert again.returncode == 2 and "`dsl41 release resume` to finish it" in again.stderr
    back = site.run("rollback")
    assert back.returncode == 2 and "its new period has not opened" in back.stderr
    resumed = site.run("resume")
    assert resumed.returncode == 0, resumed.stderr
    assert site.state()["seal_requests"] == [request_id, request_id]
    assert site.receipt("v2")["request_id"] == request_id
    assert site.active.read_text() == "v2\n" and site.record() is None


def test_a_seal_committed_under_another_actor_is_read_not_sent_again(site: Site) -> None:
    """The live engine records the authenticated actor; a resend under another claim is
    a collision, exit 2. The resume reads the estate and never sends it."""
    site.prepared("v2")
    site.edit(actor="os/someone-else")
    killed = site.run("apply", "v2", stop="seal-reply")
    assert killed.returncode == -9
    record = site.record()
    assert record is not None and record["step"] == "sealing"
    resumed = site.run("resume")
    assert resumed.returncode == 0, resumed.stderr
    assert len(site.seals()) == 1
    assert site.active.read_text() == "v2\n"


def test_a_retry_horizon_refusal_is_waited_out_and_sealed_under_a_new_id(site: Site) -> None:
    site.prepared("v2")
    site.edit(seal_outcomes=["horizon"])
    done = site.run("apply", "v2")
    assert done.returncode == 0, done.stderr
    assert "waiting 1.5 s for the retry horizon of the last operator request" in done.stderr
    # the seal's own refusal text is not shown: the wait is keyed on its code (DL-412)
    assert "force-seal" not in done.stderr and "retry_horizon_us" not in done.stderr
    first, second = [call[call.index("--request-id") + 1] for call in site.seals()]
    assert first != second == site.receipt("v2")["request_id"]


def test_a_horizon_refusal_is_known_by_its_code_not_its_wording(site: Site) -> None:
    """DL-412: a `seal_retry_horizon` answer whose text names no figures is still a horizon
    refusal; the apply waits a fixed time, here past the bound, and seals nothing."""
    site.prepared("v2")
    site.edit(seal_outcomes=["horizon-unworded"])
    refused = site.run("apply", "v2", "--horizon-timeout", "5")
    assert refused.returncode == 2, refused.stderr
    assert "outlasts --horizon-timeout 5 s, so nothing was sealed." in refused.stderr
    assert "a retry horizon" not in refused.stderr
    assert site.record() is None and site.active.read_text() == "r1\n"


def test_an_offline_horizon_refusal_on_stderr_is_waited_out(site: Site) -> None:
    """DL-412: an offline seal prints the gate's sentence on stderr, not a coded answer; the
    apply reads the wait from it and seals again. The stub renders that sentence through the
    gate and `refuse` themselves."""
    site.prepared("v2")
    site.edit(seal_outcomes=["horizon-offline"])
    done = site.run("apply", "v2")
    assert done.returncode == 0, done.stderr
    assert "waiting 1.5 s for the retry horizon of the last operator request" in done.stderr
    first, second = [call[call.index("--request-id") + 1] for call in site.seals()]
    assert first != second == site.receipt("v2")["request_id"]


def test_a_horizon_past_the_timeout_and_a_drain_past_its_timeout_are_refusals(
    site: Site,
) -> None:
    site.prepared("v2")
    site.edit(seal_outcomes=["horizon"])
    refused = site.run("apply", "v2", "--horizon-timeout", "1")
    assert refused.returncode == 2, refused.stderr
    assert "outlasts --horizon-timeout 1 s, so nothing was sealed." in refused.stderr
    state = site.state()
    state["jobs"]["NB_IDLE_C"]["status"] = "QUE_WAIT"
    state["sent"] = []
    site.save(state)
    refused = site.run("apply", "v2", "--drain-timeout", "1")
    assert refused.returncode == 2, refused.stderr
    assert "jobs still run after 1 s: NB_IDLE_C." in refused.stderr
    assert "apply again later, or with a longer --drain-timeout." in refused.stderr
    assert "waiting for 1 running jobs to finish" in refused.stderr
    assert "this apply's holds are released" in refused.stderr
    assert [verb for verb, _ in site.state()["sent"]] == ["ON_HOLD"] * 2 + ["OFF_HOLD"] * 2
    assert site.active.read_text() == "r1\n" and site.record() is None
    assert len(site.seals()) == 1


def test_a_removed_or_changed_latent_job_refuses(site: Site) -> None:
    site.prepared("v1", add_job=True)
    assert site.run("apply", "v1").returncode == 0
    state = site.state()
    state["jobs"]["NB_ADDED_C"]["armed"] = True
    state["jobs"]["OPS_SPOOL_C"]["pending_timers"] = [{"due": "x", "kind": "term"}]
    site.save(state)
    site.prepared("v2")
    refused = site.run("apply", "v2")
    assert refused.returncode == 2, refused.stderr
    assert "v2 changes or removes NB_ADDED_C, and each is armed or has a pending timer." in (
        refused.stderr
    )
    assert site.active.read_text() == "v1\n" and len(site.seals()) == 1
    site.prepared("v3", add_job=True, change_job=True)
    refused = site.run("apply", "v3")
    assert refused.returncode == 2, refused.stderr
    assert "v3 changes or removes OPS_SPOOL_C, and each" in refused.stderr
    assert f"`dsl41 sendevent DISARM -J OPS_SPOOL_C -S {site.root}/control.sock`" in (
        refused.stderr
    )
    assert "-J JOB" not in refused.stderr
    assert site.active.read_text() == "v1\n" and len(site.seals()) == 1


def test_an_apply_refuses_up_front_when_no_engine_answers(site: Site) -> None:
    site.prepared("v2")
    site.edit(up=False)
    refused = site.run("apply", "v2")
    assert refused.returncode == 2
    assert refused.stderr.count("no engine answers") == 1
    assert f"no engine answers on {site.root}/control.sock." in refused.stderr
    assert (
        "check `systemctl status dsl41-engine.service` and"
        " `journalctl -u dsl41-engine.service`." in refused.stderr
    )
    assert site.record() is None


def test_a_second_apply_is_refused_while_the_first_holds_the_lock(site: Site) -> None:
    site.prepared("v2")
    lock = site.releases / ".apply.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, b"pid 1, nbdeploy@host, since then\n")
        refused = site.run("apply", "v2")
        rollback = site.run("rollback")
    finally:
        os.close(fd)
    assert refused.returncode == 2
    assert f"another apply holds {lock}: pid 1, nbdeploy@host, since then" in refused.stderr
    assert rollback.returncode == 2 and "another apply holds" in rollback.stderr
    assert site.state()["sent"] == [] and site.record() is None


@pytest.mark.parametrize(("stop", "step"), [("seal-reply", "sealing"), ("seal", "sealed")])
def test_an_apply_killed_around_the_seal_resumes_and_never_seals_again(
    site: Site, stop: str, step: str
) -> None:
    site.prepared("v2")
    killed = site.run("apply", "v2", stop=stop)
    assert killed.returncode == -9
    record = site.record()
    assert record is not None and record["step"] == step
    assert site.active.read_text() == "r1\n"
    assert ["start", "dsl41-engine.service"] not in site.state()["systemctl"]
    resumed = site.run("resume")
    assert resumed.returncode == 0, resumed.stderr
    assert site.state()["seal_requests"] == [record["request_id"]]
    assert site.active.read_text() == "v2\n"
    assert site.receipt("v2")["request_id"] == record["request_id"]


def test_the_environment_cannot_stop_an_apply(site: Site) -> None:
    """DL-412: the stop points are a test-only hook; the old variable does nothing."""
    site.prepared("v2")
    done = site.run("apply", "v2", env={"DSL41_RELEASE_STOP_AFTER": "seal"})
    assert done.returncode == 0, done.stderr
    assert site.record() is None and site.active.read_text() == "v2\n"


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


def test_a_failure_after_the_seal_stops_with_the_record_and_resume_finishes(
    site: Site,
) -> None:
    site.prepared("v2")
    site.edit(start_fails=True)
    stopped = site.run("apply", "v2")
    assert stopped.returncode == 3, stopped.stderr
    assert "stopped: `systemctl start dsl41-engine.service` exited 1." in stopped.stderr
    assert "no engine runs now, so no job is scheduled." in stopped.stderr
    assert "read `journalctl -u dsl41-engine.service` for the cause." in stopped.stderr
    assert "`dsl41 release resume` will start the engine once the sealed engine has exited" in (
        stopped.stderr
    )
    assert "step" not in stopped.stderr
    assert site.active.read_text() == "v2\n"
    assert site.record()["step"] == "selected"  # type: ignore[index]
    site.edit(start_fails=False)
    resumed = site.run("resume")
    assert resumed.returncode == 0, resumed.stderr
    assert len(site.seals()) == 1 and site.record() is None


def test_an_opened_bundle_the_seal_did_not_commit_stops_the_apply(site: Site) -> None:
    site.prepared("v2")
    site.edit(opened_hash="sha256:" + "0" * 64)
    stopped = site.run("apply", "v2")
    assert stopped.returncode == 3, stopped.stderr
    # the first line names the action; the problems follow (DL-412)
    assert (
        "dsl41 release: stopped: the new period does not match the seal: roll back with"
        " `dsl41 release rollback`, or resume to check again.\n" in stopped.stderr
    )
    assert "what does not match: period 2's segment does not open the committed seal" in (
        stopped.stderr
    )
    assert "period 2's manifest pins another bundle" in stopped.stderr
    record = site.record()
    assert record is not None and record["step"] == "opened"
    assert record["problems"][0] == "period 2's segment does not open the committed seal"
    assert [verb for verb, _ in site.state()["sent"]] == ["ON_HOLD", "ON_HOLD"]
    assert (
        "`dsl41 release rollback` would release this apply's holds instead, and roll back"
        " to r1." in stopped.stderr
    )
    # resume checks again, and stops again with the record kept
    again = site.run("resume")
    assert again.returncode == 3 and site.record() is not None
    shown = site.run("status").stdout
    assert "  the new period does not match the seal: period 2's segment" in shown
    assert "  `dsl41 release resume` will check the new period again" in shown
    assert "  `dsl41 release rollback` would release this apply's holds instead" in shown


def _failed_check(site: Site) -> dict[str, Any]:
    """v1 active, then an apply of v2 whose new period does not match its seal: exit 3 at
    step opened, with NB_B and NB_FREE_C held by it. The record."""
    site.prepared("v1")
    assert site.run("apply", "v1").returncode == 0
    site.prepared("v2")
    site.edit(opened_hash="sha256:" + "0" * 64)
    assert site.run("apply", "v2").returncode == 3
    record = site.record()
    assert record is not None and record["step"] == "opened"
    site.edit(opened_hash=None)
    return record


def test_rollback_takes_over_an_apply_whose_check_failed(site: Site) -> None:
    """DL-412: rollback may take over an apply whose new period opened and failed its check.
    It releases no hold: the rollback's apply owns them, so the jobs stay held through its
    seal, and it releases them at its end. The old apply's receipt is verify-failed-<id>."""
    record = _failed_check(site)
    back = site.run("rollback")
    assert back.returncode == 0, back.stderr
    assert "rolling back from v2 to v1." in back.stderr
    assert site.active.read_text() == "v1\n" and site.record() is None
    (path,) = (site.releases / "v2" / ".release").glob("verify-failed-*.json")
    assert path.name == f"verify-failed-{record['request_id']}.json"
    failed = json.loads(path.read_text())
    assert failed["outcome"] == "verify_failed" and failed["verify_failed"] is True
    assert failed["problems"] == record["problems"] and failed["released"] == []
    assert not list((site.releases / "v2" / ".release").glob("rolled-back-*"))
    assert not (site.releases / "v2" / ".release" / "latest").exists()
    receipt = site.receipt("v1")
    assert receipt["previous"] == "v2" and receipt["takeover_of"]["receipt"] == path.name
    assert receipt["released"] == ["NB_B", "NB_FREE_C"]
    assert len(site.seals()) == 3
    # the rollback held nothing again, and released the holds only after its seal
    calls = site.state()["calls"]
    sends = [(i, c[1], c[c.index("-J") + 1]) for i, c in enumerate(calls) if c[0] == "sendevent"]
    last_seal = max(i for i, c in enumerate(calls) if c[0] == "seal")
    assert [(verb, job) for i, verb, job in sends if i > last_seal] == [
        ("OFF_HOLD", "NB_B"),
        ("OFF_HOLD", "NB_FREE_C"),
    ]
    assert [(verb, job) for verb, job in site.state()["sent"]].count(("ON_HOLD", "NB_B")) == 2
    state = site.state()
    assert not state["jobs"]["NB_FREE_C"]["on_hold"] and not state["jobs"]["NB_B"]["on_hold"]
    # a rollback never applies the release whose check failed; `apply NAME` retries it
    refusal = (
        f"v2's last apply failed its check after the open ({path}), so a rollback does not"
        " apply it; to retry it on purpose, run `dsl41 release apply v2`."
    )
    shown = site.run("status").stdout
    assert f"rollback: {refusal}" in shown and "would apply v2" not in shown
    again = site.run("rollback")
    assert again.returncode == 2 and again.stderr == f"dsl41 release: {refusal}\n"
    assert site.active.read_text() == "v1\n" and len(site.seals()) == 3
    assert site.run("apply", "v2").returncode == 0
    assert site.active.read_text() == "v2\n"


def test_a_refused_takeover_leaves_the_failed_release_active_and_says_so(site: Site) -> None:
    """DL-412: when the rollback's apply is refused, its abort releases the holds it owns,
    the failed release stays active, status says so, and a rollback tries again."""
    _failed_check(site)
    state = site.state()
    state["jobs"]["NB_IDLE_C"]["status"] = "RUNNING"
    site.save(state)
    refused = site.run("rollback", "--drain-timeout", "1")
    assert refused.returncode == 2, refused.stderr
    assert site.active.read_text() == "v2\n" and site.record() is None
    state = site.state()
    assert not state["jobs"]["NB_FREE_C"]["on_hold"] and not state["jobs"]["NB_B"]["on_hold"]
    (path,) = (site.releases / "v2" / ".release").glob("verify-failed-*.json")
    shown = site.run("status").stdout
    assert f"active: v2; its last apply did not finish, see {path.name}." in shown
    assert "rollback: `dsl41 release rollback` would apply v1." in shown
    state["jobs"]["NB_IDLE_C"]["status"] = "SUCCESS"
    site.save(state)
    back = site.run("rollback")
    assert back.returncode == 0, back.stderr
    assert site.active.read_text() == "v1\n"


def test_a_takeover_killed_before_its_receipt_resumes_into_the_rollback(site: Site) -> None:
    """DL-412: one rename replaces the failed apply's record with the rollback's, so a kill
    right after it leaves the rollback recorded; resume writes the old apply's receipt and
    finishes the rollback. An earlier receipt of v2 cannot redirect it."""
    site.prepared("v0")
    assert site.run("apply", "v0").returncode == 0
    site.prepared("v1")
    assert site.run("apply", "v1").returncode == 0
    site.prepared("v2")
    assert site.run("apply", "v2").returncode == 0
    site.prepared("v3")
    site.edit(opened_hash="sha256:" + "0" * 64)
    assert site.run("apply", "v3").returncode == 3
    site.edit(opened_hash=None)
    killed = site.run("rollback", stop="takeover")
    assert killed.returncode == -9
    record = site.record()
    assert record is not None and record["candidate"] == "v2" and record["previous"] == "v3"
    taken = record["takeover_of"]
    assert taken["release"] == "v3" and taken["receipt"].startswith("verify-failed-")
    assert not (site.releases / "v3" / ".release" / taken["receipt"]).exists()
    assert {hold["carried_from"] for hold in record["holds"]} == {taken["request_id"]}
    shown = site.run("status").stdout
    assert f"  it took over the apply of v3, whose receipt is {taken['receipt']}." in shown
    assert "  holds taken by this apply: NB_B, NB_FREE_C." in shown
    resumed = site.run("resume")
    assert resumed.returncode == 0, resumed.stderr
    assert (site.releases / "v3" / ".release" / taken["receipt"]).exists()
    assert site.active.read_text() == "v2\n" and site.record() is None


def test_a_takeover_keeps_a_hold_the_engine_will_not_release(site: Site) -> None:
    """DL-412: the rollback owns the failed apply's holds; one the engine will not release at
    its end is kept, named with its command, and the rollback exits 1."""
    _failed_check(site)
    site.edit(release_outcomes={"NB_FREE_C": [3] * 3})
    back = site.run("rollback")
    assert back.returncode == 1, back.stderr
    assert "NB_FREE_C stays on hold: the engine did not release it" in back.stderr
    assert site.active.read_text() == "v1\n" and site.record() is None
    receipt = site.receipt("v1")
    assert receipt["kept"] == ["NB_FREE_C"]
    (carried,) = [hold for hold in receipt["holds"] if hold["job"] == "NB_FREE_C"]
    assert carried["carried_from"] == receipt["takeover_of"]["request_id"]
    holds = [call for call in site.state()["sent"] if call == ["ON_HOLD", "NB_FREE_C"]]
    assert len(holds) == 2  # v1's apply and v2's; the rollback held nothing new
    shown = site.run("status").stdout
    assert "  NB_FREE_C stays on hold: the engine did not release it" in shown


def test_rollback_takes_over_an_apply_stopped_after_its_check(site: Site) -> None:
    """DL-412: an apply stopped while releasing its holds, its check passed, is taken over the
    same way; its receipt is taken-over-<id>, with verify_failed false."""
    site.prepared("v1")
    assert site.run("apply", "v1").returncode == 0
    site.prepared("v2")
    site.edit(release_outcomes={"NB_FREE_C": [4]})
    assert site.run("apply", "v2").returncode == 3
    assert site.record()["step"] == "verified"  # type: ignore[index]
    back = site.run("rollback")
    assert back.returncode == 0, back.stderr
    (path,) = (site.releases / "v2" / ".release").glob("taken-over-*.json")
    given_up = json.loads(path.read_text())
    assert given_up["verify_failed"] is False and given_up["problems"] == []
    assert given_up["outcome"] == "taken_over" and given_up["released"] == ["NB_B"]
    assert site.active.read_text() == "v1\n" and site.record() is None
    # the take-off in flight is sent again under its id and answered from its decision
    assert site.state()["sent"].count(["OFF_HOLD", "NB_FREE_C"]) == 2
    # a check that passed does not stop a later rollback to v2
    assert site.run("rollback").returncode == 0 and site.active.read_text() == "v2\n"


def test_apply_refuses_an_unprepared_changed_or_active_release(site: Site) -> None:
    shutil.copytree(SMALL, site.releases / "raw")
    refused = site.run("apply", "raw")
    assert refused.returncode == 2 and "raw is installed but not prepared." in refused.stderr
    assert "run `dsl41 release prepare --requalify raw` to check it in place." in refused.stderr
    site.prepared("v2")
    site.active.write_text("v2\n")
    refused = site.run("apply", "v2")
    assert refused.returncode == 2 and "v2 is already active" in refused.stderr
    site.active.write_text("r1\n")
    jil = site.releases / "v2" / "global.jil"
    (site.releases / "v2").chmod(0o755)
    jil.chmod(0o644)
    jil.write_text(jil.read_text() + "\n")
    refused = site.run("apply", "v2")
    assert refused.returncode == 2 and "v2's files changed since prepare: global.jil" in (
        refused.stderr
    )
    assert site.state()["sent"] == []


def test_apply_refuses_another_runtime_until_requalify(site: Site) -> None:
    site.prepared("v2")
    release = site.releases / "v2"
    prepared = release / ".release" / "prepared.json"
    prepared.chmod(0o644)
    prepared.write_text(json.dumps(json.loads(prepared.read_text()) | {"dsl41_version": "0.0.1"}))
    refused = site.run("apply", "v2")
    assert refused.returncode == 2 and "was prepared under dsl41 0.0.1" in refused.stderr
    assert "run `dsl41 release prepare --requalify v2` to check it again." in refused.stderr
    before = {path: path.read_bytes() for path in release.rglob("*.jil")}
    usage = site.run("prepare", "--requalify", "v2", str(site.source("other")))
    assert usage.returncode == 2
    (site.stub / "lint.exit").write_text("1")
    refused = site.run("prepare", "--requalify", "v2")
    assert refused.returncode == 2 and "lint exited 1" in refused.stderr
    assert json.loads(prepared.read_text())["dsl41_version"] == "0.0.1"
    (site.stub / "lint.exit").unlink()
    done = site.run("prepare", "--requalify", "v2")
    assert done.returncode == 0, done.stderr
    assert {path: path.read_bytes() for path in release.rglob("*.jil")} == before
    assert not os.access(release / "global.jil", os.W_OK)
    requalified = site.receipt("v2", "prepared")
    assert requalified["dsl41_version"] != "0.0.1"
    assert requalified["source"] == str((site.base / "sources" / "v2").resolve())
    assert site.run("apply", "v2").returncode == 0


def test_requalify_checks_a_release_installed_but_never_prepared_in_place(site: Site) -> None:
    """DL-412: the refusal of an unprepared installed release names --requalify, and
    --requalify checks it where it is, its inputs made read-only, its files unchanged."""
    raw = site.releases / "raw"
    shutil.copytree(SMALL, raw)
    before = {path.name: path.read_bytes() for path in raw.glob("*.jil")}
    (site.stub / "lint.exit").write_text("1")
    refused = site.run("prepare", "--requalify", "raw")
    assert refused.returncode == 2 and "raw stays unprepared." in refused.stderr
    assert not (raw / ".release" / "prepared.json").exists()
    (site.stub / "lint.exit").unlink()
    done = site.run("prepare", "--requalify", "raw")
    assert done.returncode == 0, done.stderr
    prepared = site.receipt("raw", "prepared")
    assert prepared["source"] is None and set(prepared["files"]) >= set(before)
    assert {path.name: path.read_bytes() for path in raw.glob("*.jil")} == before
    assert not os.access(raw / "global.jil", os.W_OK) and not os.access(raw, os.W_OK)
    assert site.run("apply", "raw").returncode == 0
    assert site.active.read_text() == "raw\n"


def test_rollback_applies_the_previous_release_and_keeps_every_receipt(site: Site) -> None:
    site.prepared("v1")
    site.prepared("v2", add_job=True)
    refused = site.run("rollback")
    assert refused.returncode == 2
    assert "no receipt of r1 names the release it replaced." in refused.stderr
    assert "apply the earlier release by name: `dsl41 release apply NAME`." in refused.stderr
    assert site.run("apply", "v1").returncode == 0
    first = site.receipt("v1")
    assert site.run("apply", "v2").returncode == 0
    back = site.run("rollback")
    assert back.returncode == 0, back.stderr
    # the target is printed before the window starts
    assert back.stderr.startswith("dsl41 release: rolling back from v2 to v1.\n")
    assert site.active.read_text() == "v1\n"
    assert site.receipt("v1")["previous"] == "v2"
    receipts = sorted((site.releases / "v1" / ".release").glob("applied-*.json"))
    assert len(receipts) == 2
    assert (
        json.loads(
            (site.releases / "v1" / ".release" / f"applied-{first['request_id']}.json").read_text()
        )
        == first
    )
    assert len(site.seals()) == 3
    # v1's latest receipt names v2 now, so a second rollback goes forward again
    assert site.run("rollback").returncode == 0 and site.active.read_text() == "v2\n"


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


def test_the_service_side_takes_only_release_names_a_request_id_and_a_job(site: Site) -> None:
    rid = "0" * 8 + "-0000-0000-0000-" + "0" * 12
    for args in (
        ("service", "seal", "v2", "not-an-id"),
        ("service", "seal", "../x", rid),
        ("service", "seal", "r1", rid, "os/dsl41"),
        ("service", "seal", "r1"),
        ("service", "committed"),
        ("service", "pins", "x"),
        ("service", "holds", "../x", rid),
        ("service", "holds", ""),
        ("service", "check", "../x"),
        ("service", "changes", "r1", "../x"),
        ("service", "run", "v2"),
        ("service",),
    ):
        refused = site.run(*args)
        assert refused.returncode == 2, args
    assert site.seals() == []
    shown = site.run("service", "committed", rid)
    assert shown.returncode == 0 and json.loads(shown.stdout) == {"committed": False}
    # a job name takes the engine's rule, `#` included (DL-412)
    held = site.run("service", "holds", "EOD#1")
    assert held.returncode == 0 and json.loads(held.stdout) == {"changes": []}


def test_the_service_side_runs_only_as_the_service_account(site: Site) -> None:
    text = site.launcher.read_text().replace(f"SERVICE_ACCOUNT={ME}\n", "SERVICE_ACCOUNT=nobody\n")
    site.launcher.write_text(text)
    refused = site.run("service", "committed", "0" * 8 + "-0000-0000-0000-" + "0" * 12)
    assert refused.returncode == 2
    assert f"the service side runs as nobody, not as {ME}." in refused.stderr


@pytest.mark.parametrize("job", ["EOD#1", "job with space", "-dash", "B" * 201])
def test_any_job_name_the_engine_takes_is_held_and_released(site: Site, job: str) -> None:
    """DL-412: the service side reads the log for any name the engine addresses, by the
    engine's own rule; the argument separator keeps a name from reading as an option."""
    site.prepared("v2")
    state = site.state()
    state["jobs"][job] = _job("CMD")
    state["timers"].append({"due": "2026-10-28T02:00:00", "job": job, "kind": "schedule"})
    site.save(state)
    done = site.run("apply", "v2")
    assert done.returncode == 0, done.stderr
    assert job in site.receipt("v2")["released"]
    assert f"dsl41 release service -- holds {job}" in site.sudo_log()


def test_a_hand_run_service_seal_refuses_an_unprepared_or_changed_release(
    site: Site,
) -> None:
    """The deployment identity can run `service seal` through sudo by hand; it seals
    nothing prepare did not check."""
    rid = "0" * 8 + "-0000-0000-0000-" + "0" * 12
    refused = site.run("service", "seal", "r1", rid)
    assert refused.returncode == 2 and "r1 is installed but not prepared" in refused.stderr
    site.prepared("v2")
    jil = site.releases / "v2" / "global.jil"
    (site.releases / "v2").chmod(0o755)
    jil.chmod(0o644)
    jil.write_text(jil.read_text() + "\n")
    refused = site.run("service", "seal", "v2", rid)
    assert refused.returncode == 2 and "v2's files changed since prepare" in refused.stderr
    assert site.seals() == []


def _same_bytes_symlink(site: Site, name: str) -> None:
    """Replace a prepared input with a symlink to identical bytes outside the release: the
    digest check alone would pass it."""
    release = site.releases / name
    outside = site.base / "outside.jil"
    outside.write_bytes((release / "global.jil").read_bytes())
    release.chmod(0o755)
    (release / "global.jil").chmod(0o644)
    (release / "global.jil").unlink()
    (release / "global.jil").symlink_to(outside)


def test_a_service_seal_refuses_a_symlinked_input(site: Site) -> None:
    site.prepared("v2")
    _same_bytes_symlink(site, "v2")
    rid = "0" * 8 + "-0000-0000-0000-" + "0" * 12
    refused = site.run("service", "seal", "v2", rid)
    assert refused.returncode == 2 and "v2 holds symlinks: global.jil." in refused.stderr
    assert site.seals() == []


def test_service_changes_refuses_a_symlinked_input(site: Site) -> None:
    site.prepared("v2")
    _same_bytes_symlink(site, "v2")
    refused = site.run("service", "changes", "r1", "v2")
    assert refused.returncode == 2 and "v2 holds symlinks: global.jil." in refused.stderr


def test_an_apply_refuses_a_symlinked_input(site: Site) -> None:
    site.prepared("v2")
    _same_bytes_symlink(site, "v2")
    refused = site.run("apply", "v2")
    assert refused.returncode == 2 and "v2 holds symlinks: global.jil." in refused.stderr
    assert site.state()["sent"] == [] and site.record() is None


def test_service_changes_refuses_a_symlinked_properties_layer(site: Site) -> None:
    """DL-412: a layer linked to a file the service account can read prints nothing of it."""
    evil = site.releases / "evil"
    shutil.copytree(SMALL, evil)
    secret = site.base / "secret.properties"
    secret.write_text("first secret line\nsecond secret line\n")
    (evil / "layer.properties").symlink_to(secret)
    manifest = evil / "estate.toml"
    manifest.write_text(
        manifest.read_text().replace(
            'timezone = "UTC"', 'properties = ["layer.properties"]\ntimezone = "UTC"'
        )
    )
    refused = site.run("service", "changes", "r1", "evil")
    assert refused.returncode == 2 and "evil holds symlinks: layer.properties." in refused.stderr
    assert "secret" not in refused.stderr


def test_a_forged_prepared_json_naming_a_file_outside_the_release_refuses(site: Site) -> None:
    site.prepared("v2")
    prepared = site.releases / "v2" / ".release" / "prepared.json"
    forged = json.loads(prepared.read_text())
    forged["files"]["../outside.jil"] = forged["files"]["global.jil"]
    prepared.chmod(0o644)
    prepared.write_text(json.dumps(forged))
    rid = "0" * 8 + "-0000-0000-0000-" + "0" * 12
    refused = site.run("service", "seal", "v2", rid)
    assert refused.returncode == 2
    assert "v2's prepared.json names files outside it: ../outside.jil." in refused.stderr
    assert site.seals() == []


def test_a_release_input_is_read_inside_the_release_without_following_a_link(
    tmp_path: Path,
) -> None:
    """DL-412: `read_input` refuses a link as the file itself (O_NOFOLLOW) and a real path
    outside the release, whatever planted it after the walk."""
    from dsl41.release_service import Stop, read_input

    release, outside = tmp_path / "release", tmp_path / "outside"
    release.mkdir()
    outside.mkdir()
    (outside / "secret").write_text("secret")
    (release / "plain").write_text("plain")
    assert read_input(release, release / "plain") == b"plain"
    (release / "link").symlink_to(outside / "secret")
    (release / "dir").symlink_to(outside)
    for file in (release / "link", release / "dir" / "secret"):
        with pytest.raises(Stop) as refused:
            read_input(release, file)
        assert refused.value.code == 2 and "is outside" in refused.value.lines[0]
    inner = release / "inner"
    inner.symlink_to(release / "plain")
    with pytest.raises(Stop) as refused:
        read_input(release, inner)
    assert "cannot be read as a release input" in refused.value.lines[0]


def test_a_resume_refused_before_it_acts_says_its_apply_stays_recorded(site: Site) -> None:
    """DL-412: an exit 2 that keeps a recorded apply, with its holds, says so."""
    site.prepared("v2")
    site.edit(send_unknown=["NB_FREE_C"])
    assert site.run("apply", "v2").returncode == 3
    jil = site.releases / "v2" / "global.jil"
    (site.releases / "v2").chmod(0o755)
    jil.chmod(0o644)
    jil.write_text(jil.read_text() + "\n")
    refused = site.run("resume")
    assert refused.returncode == 2, refused.stderr
    assert "v2's files changed since prepare: global.jil." in refused.stderr
    assert "the apply of v2 stays recorded, with its holds." in refused.stderr
    assert site.record() is not None


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
    process.send_signal(signal.SIGHUP)
    time.sleep(1.5)
    assert process.poll() is None
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


def test_a_missing_grant_names_the_sudoers_file(site: Site) -> None:
    """DL-412: sudo's own refusal is read as a grant to fix, not as a failed check."""
    _script(
        site.base / "path" / "sudo", "#!/bin/sh\necho 'sudo: a password is required' >&2\nexit 1\n"
    )
    refused = site.run("prepare", "v2", str(site.source("v2")))
    assert refused.returncode == 2
    assert "sudo did not run `" in refused.stderr
    assert "sudoers grant is missing or names another path" in refused.stderr


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
