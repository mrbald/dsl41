"""DL-407: the nightbank release helper, `examples/nightbank/deploy/dsl41-release`.

Each test installs the shipped helper beside a copy of the shipped launcher, with only the
launcher's CONFIGURATION values replaced and the helper's interpreter line pointed at this
interpreter. `sudo` and `systemctl` are stubs on PATH, and the launcher's dsl41 is a stub
engine that keeps its state in one JSON file: it answers `query`, `sendevent` and `seal`,
and hands `estate args`, `lint` and `rehearse` to this build's dsl41. The service drill
runs the same helper against systemd and a real engine.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from test_nightbank_deploy import DEPLOY, NB, _configured, _night

HELPER = DEPLOY / "dsl41-release"
DSL41 = Path(sys.executable).with_name("dsl41")
SMALL = NB / "estate" / "small"

#: the stub engine: its state is state.json beside it (see the module docstring)
STUB_DSL41 = r"""
import hashlib, json, os, sys, uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / "state.json"
REAL = os.environ["RELEASE_TEST_DSL41"]
args = sys.argv[1:]
if args[:2] == ["estate", "args"] or args[:1] in (["lint"], ["rehearse"]):
    forced = HERE / f"{args[0]}.exit"
    if forced.exists():
        sys.exit(int(forced.read_text()))
    os.execv(REAL, [REAL, *args])
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
    row = state["jobs"][job]
    expect = opt("--expect")
    if expect is not None and int(expect) != row["state_rev"]:
        save()
        print(json.dumps({"ok": False, "decision": "rejected"}))
        sys.exit(3)
    row["on_hold"] = verb == "ON_HOLD"
    row["state_rev"] += 1
    state["sent"].append([verb, job])
    save()
    answer = {"ok": True, "decision": "applied", "request_id": str(uuid.uuid4())}
    print(json.dumps(answer | {"revisions": {f"job:{job}": row["state_rev"]}}))
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
    if outcome == "horizon":
        save()
        print(
            "the last externally requested attempt was 59.500s ago and the closing period's"
            " retry_horizon_us is 60000000: a retry composed under this baseline can no"
            " longer be answered after the boundary.",
            file=sys.stderr,
        )
        sys.exit(2)
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
    """A layout the helper releases into, with the stub engine running period 1."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.releases = base / "releases"
        self.active = base / "active"
        self.root = base / "runs" / "nb"
        self.stub = base / "stub"
        bin_dir = base / "bin"
        for path in (bin_dir, self.stub, base / "path", self.root):
            path.mkdir(parents=True)
        shutil.copytree(SMALL, self.releases / "r1")
        self.active.write_text("r1\n")
        launcher = _configured(
            base,
            DSL41=str(self.stub / "dsl41"),
            RUN_ROOT=str(self.root),
            ESTATE_ANCHOR=f"{self.root}.anchor",
            ESTATE_RELEASES=str(self.releases),
            ACTIVE=str(self.active),
            PROPERTIES=str(_night(base)),
        )
        launcher.rename(bin_dir / "dsl41-launch")
        text = HELPER.read_text().split("\n", 1)[1]
        self.helper = bin_dir / "dsl41-release"
        self.helper.write_text(f"#!{sys.executable} -I\n{text}")
        self.helper.chmod(0o755)
        _script(self.stub / "dsl41", STUB_DSL41)
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

    def run(
        self, *args: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        environ = {
            **{
                key: value for key, value in os.environ.items() if key != "DSL41_RELEASE_STOP_AFTER"
            },
            "PATH": f"{self.base / 'path'}:{os.environ['PATH']}",
            "RELEASE_TEST_DSL41": str(DSL41),
            "RELEASE_TEST_SUDO_LOG": str(self.base / "sudo.log"),
            **(env or {}),
        }
        return subprocess.run(
            [str(self.helper), *args], capture_output=True, text=True, timeout=300, env=environ
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

    def seals(self) -> list[list[str]]:
        return [call for call in self.state()["calls"] if call[0] == "seal"]


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
    assert prepared["lint"]["argv"][:2] == [str(site.stub / "dsl41"), "lint"]
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
    # the check ran as the service account, through the helper's own service mode
    assert (site.base / "sudo.log").read_text() == f"{site.helper} service check v2\n"


def test_prepare_refuses_a_name_that_exists_and_a_source_without_a_manifest(site: Site) -> None:
    refused = site.run("prepare", "r1", str(site.source("again")))
    assert refused.returncode == 2
    assert f"{site.releases / 'r1'} exists" in refused.stderr
    empty = site.base / "empty"
    empty.mkdir()
    refused = site.run("prepare", "v9", str(empty))
    assert refused.returncode == 2 and "holds no estate.toml" in refused.stderr
    for name in ("../x", ".hidden", "a/b", "v9.new"):
        refused = site.run("prepare", name, str(site.source("other" + str(len(name)))))
        assert refused.returncode == 2 and "is not one release name" in refused.stderr
    assert sorted(path.name for path in site.releases.iterdir()) == ["r1"]


@pytest.mark.parametrize(
    ("values", "says"),
    [
        ({"MAX_JOBS": "81"}, "it holds 82 jobs, above MAX_JOBS=81"),
        ({"MAX_GRACE_S": "5"}, "the runtime's command grace 10.0s is above MAX_GRACE_S=5"),
    ],
)
def test_prepare_refuses_a_release_above_the_launchers_ceiling(
    site: Site, values: dict[str, str], says: str
) -> None:
    launcher = site.base / "bin" / "dsl41-launch"
    text = launcher.read_text()
    for key, value in values.items():
        text = re.sub(rf"^{key}=\d+$", f"{key}={value}", text, count=1, flags=re.M)
    launcher.write_text(text)
    refused = site.run("prepare", "v2", str(site.source("v2")))
    assert refused.returncode == 2, refused.stderr
    assert says in refused.stderr
    assert not (site.releases / "v2").exists()


@pytest.mark.parametrize("verb", ["lint", "rehearse"])
def test_prepare_refuses_a_release_lint_or_rehearse_refuses(site: Site, verb: str) -> None:
    (site.stub / f"{verb}.exit").write_text("1")
    refused = site.run("prepare", "v2", str(site.source("v2")))
    assert refused.returncode == 2
    assert f"{verb} exited 1" in refused.stderr
    assert not (site.releases / "v2").exists()


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
    # each release names the revision its hold left
    offs = [call for call in state["calls"] if call[:2] == ["sendevent", "OFF_HOLD"]]
    assert [call[call.index("--expect") + 1] for call in offs] == ["4", "4"]
    (seal,) = site.seals()
    receipt = site.receipt("v2")
    assert receipt["previous"] == "r1" and receipt["period"] == 2
    assert [hold["job"] for hold in receipt["holds"]] == ["NB_B", "NB_FREE_C"]
    assert receipt["released"] == ["NB_B", "NB_FREE_C"] and receipt["kept"] == []
    assert (site.releases / "v2" / ".release" / f"applied-{receipt['request_id']}.json").exists()
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
    assert tail[-4:] == ["--claimed-actor", "os/dsl41", "--request-id", receipt["request_id"]]
    assert ["start", "dsl41-engine.service"] in state["systemctl"]
    rid = receipt["request_id"]
    assert (site.base / "sudo.log").read_text().splitlines()[1:] == [
        f"{site.helper} service changes r1 v2",
        f"{site.helper} service seal v2 {rid} os/dsl41",
        f"{site.helper} service committed {rid}",
        "systemctl start dsl41-engine.service",
        f"{site.helper} service pins {rid}",
    ]


def test_a_hold_moved_since_the_helper_took_it_is_kept(site: Site) -> None:
    site.prepared("v2")
    site.edit(seal_outcomes=["unknown"])
    assert site.run("apply", "v2").returncode == 4
    state = site.state()
    # an operator touches NB_FREE_C's hold while the apply is stopped
    state["jobs"]["NB_FREE_C"]["state_rev"] += 1
    site.save(state)
    resumed = site.run("apply", "--resume", "v2")
    assert resumed.returncode == 0, resumed.stderr
    assert "holds kept, each moved since this apply took it: NB_FREE_C" in resumed.stderr
    receipt = site.receipt("v2")
    assert receipt["released"] == ["NB_B"] and receipt["kept"] == ["NB_FREE_C"]
    assert site.state()["jobs"]["NB_FREE_C"]["on_hold"] is True


def test_a_seal_that_does_not_commit_promotes_nothing_and_releases_the_holds(
    site: Site,
) -> None:
    site.prepared("v2")
    site.edit(seal_outcomes=["refuse"])
    refused = site.run("apply", "v2")
    assert refused.returncode == 2, refused.stderr
    assert "the seal did not commit" in refused.stderr
    assert site.active.read_text() == "r1\n"
    assert site.record() is None
    assert not (site.releases / "v2" / ".release" / "latest").exists()
    state = site.state()
    assert [verb for verb, _ in state["sent"]] == ["ON_HOLD", "ON_HOLD", "OFF_HOLD", "OFF_HOLD"]
    assert ["start", "dsl41-engine.service"] not in state["systemctl"]


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
    assert record["seal_actor"] == "os/dsl41"
    assert "retries under that id" in unknown.stderr
    assert site.active.read_text() == "r1\n"
    shown = site.run("status")
    assert f"at step sealing, request id {request_id}" in shown.stdout
    assert f"read the estate for request id {request_id}" in shown.stdout
    # a plain apply is refused while one is recorded, and so is a resume of another name
    again = site.run("apply", "v2")
    assert again.returncode == 2 and "apply --resume v2" in again.stderr
    other = site.run("apply", "--resume", "r1")
    assert other.returncode == 2 and "the recorded apply is of v2, not r1" in other.stderr
    resumed = site.run("apply", "--resume", "v2")
    assert resumed.returncode == 0, resumed.stderr
    assert site.state()["seal_requests"] == [request_id, request_id]
    assert site.receipt("v2")["request_id"] == request_id
    assert site.active.read_text() == "v2\n" and site.record() is None


def test_a_seal_committed_under_another_actor_is_read_not_sent_again(site: Site) -> None:
    """The live engine records the authenticated actor; a resend under another claim is
    a collision, exit 2. The resume reads the estate and never sends it."""
    site.prepared("v2")
    site.edit(actor="os/someone-else")
    killed = site.run("apply", "v2", env={"DSL41_RELEASE_STOP_AFTER": "seal-reply"})
    assert killed.returncode == -9
    record = site.record()
    assert record is not None and record["step"] == "sealing"
    resumed = site.run("apply", "--resume", "v2")
    assert resumed.returncode == 0, resumed.stderr
    assert len(site.seals()) == 1
    assert site.active.read_text() == "v2\n"


def test_a_retry_horizon_refusal_is_waited_out_and_sealed_under_a_new_id(site: Site) -> None:
    site.prepared("v2")
    site.edit(seal_outcomes=["horizon"])
    done = site.run("apply", "v2")
    assert done.returncode == 0, done.stderr
    assert "waiting 1.5s for the retry horizon" in done.stderr
    first, second = [call[call.index("--request-id") + 1] for call in site.seals()]
    assert first != second == site.receipt("v2")["request_id"]


def test_a_horizon_past_the_timeout_and_a_drain_past_its_timeout_are_refusals(
    site: Site,
) -> None:
    site.prepared("v2")
    site.edit(seal_outcomes=["horizon"])
    refused = site.run("apply", "v2", "--horizon-timeout", "1")
    assert refused.returncode == 2, refused.stderr
    assert "the retry horizon outlasts --horizon-timeout 1s" in refused.stderr
    state = site.state()
    state["jobs"]["NB_IDLE_C"]["status"] = "QUE_WAIT"
    state["sent"] = []
    site.save(state)
    refused = site.run("apply", "v2", "--drain-timeout", "1")
    assert refused.returncode == 2, refused.stderr
    assert "still STARTING, RUNNING or QUE_WAIT after 1s: NB_IDLE_C" in refused.stderr
    assert [verb for verb, _ in site.state()["sent"]] == ["ON_HOLD"] * 2 + ["OFF_HOLD"] * 2
    assert site.active.read_text() == "r1\n" and site.record() is None
    assert len(site.seals()) == 1


def test_a_removed_latent_job_refuses_and_a_changed_one_crosses(site: Site) -> None:
    site.prepared("v1", add_job=True)
    assert site.run("apply", "v1").returncode == 0
    state = site.state()
    state["jobs"]["NB_ADDED_C"]["armed"] = True
    state["jobs"]["OPS_SPOOL_C"]["pending_timers"] = [{"due": "x", "kind": "term"}]
    site.save(state)
    site.prepared("v2")
    refused = site.run("apply", "v2")
    assert refused.returncode == 2, refused.stderr
    assert "NB_ADDED_C: the seal refuses a removed job that is executing or latent" in (
        refused.stderr
    )
    assert site.active.read_text() == "v1\n" and len(site.seals()) == 1
    site.prepared("v3", add_job=True, change_job=True)
    done = site.run("apply", "v3")
    assert done.returncode == 0, done.stderr
    assert "OPS_SPOOL_C is latent and changed: it crosses with a recorded assumption" in (
        done.stderr
    )


def test_an_apply_refuses_up_front_when_no_engine_answers(site: Site) -> None:
    site.prepared("v2")
    site.edit(up=False)
    refused = site.run("apply", "v2")
    assert refused.returncode == 2 and "query status exited 2" in refused.stderr
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
    killed = site.run("apply", "v2", env={"DSL41_RELEASE_STOP_AFTER": stop})
    assert killed.returncode == -9
    record = site.record()
    assert record is not None and record["step"] == step
    assert site.active.read_text() == "r1\n"
    assert ["start", "dsl41-engine.service"] not in site.state()["systemctl"]
    resumed = site.run("apply", "--resume", "v2")
    assert resumed.returncode == 0, resumed.stderr
    assert site.state()["seal_requests"] == [record["request_id"]]
    assert site.active.read_text() == "v2\n"
    assert site.receipt("v2")["request_id"] == record["request_id"]


def test_a_failure_after_the_seal_stops_with_the_record_and_resume_finishes(
    site: Site,
) -> None:
    site.prepared("v2")
    site.edit(start_fails=True)
    stopped = site.run("apply", "v2")
    assert stopped.returncode == 3, stopped.stderr
    assert "recorded at step selected" in stopped.stderr
    assert "will start the engine unit once the sealed engine has exited" in stopped.stderr
    assert site.active.read_text() == "v2\n"
    site.edit(start_fails=False)
    resumed = site.run("apply", "--resume", "v2")
    assert resumed.returncode == 0, resumed.stderr
    assert len(site.seals()) == 1 and site.record() is None


def test_an_opened_bundle_the_seal_did_not_commit_stops_the_apply(site: Site) -> None:
    site.prepared("v2")
    site.edit(opened_hash="sha256:" + "0" * 64)
    stopped = site.run("apply", "v2")
    assert stopped.returncode == 3, stopped.stderr
    assert "period 2's segment does not open the committed seal" in stopped.stderr
    assert "period 2's manifest pins another bundle" in stopped.stderr
    record = site.record()
    assert record is not None and record["step"] == "opened"
    assert [verb for verb, _ in site.state()["sent"]] == ["ON_HOLD", "ON_HOLD"]


def test_apply_refuses_an_unprepared_changed_or_active_release(site: Site) -> None:
    shutil.copytree(SMALL, site.releases / "raw")
    refused = site.run("apply", "raw")
    assert refused.returncode == 2 and "raw is not prepared" in refused.stderr
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
    assert "run `dsl41-release prepare --requalify v2`" in refused.stderr
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
    shutil.copytree(SMALL, site.releases / "raw")
    refused = site.run("prepare", "--requalify", "raw")
    assert refused.returncode == 2 and "raw was never prepared" in refused.stderr


def test_rollback_applies_the_previous_release_and_keeps_every_receipt(site: Site) -> None:
    site.prepared("v1")
    site.prepared("v2", add_job=True)
    refused = site.run("rollback")
    assert refused.returncode == 2 and "r1: no previous release; use apply NAME" in refused.stderr
    assert site.run("apply", "v1").returncode == 0
    first = site.receipt("v1")
    assert site.run("apply", "v2").returncode == 0
    back = site.run("rollback")
    assert back.returncode == 0, back.stderr
    assert "rolling back from v2 to v1" in back.stderr
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


def test_status_lists_active_prepared_and_applied_releases(site: Site) -> None:
    site.prepared("v2")
    assert site.run("apply", "v2").returncode == 0
    shown = site.run("status")
    assert shown.returncode == 0
    lines = shown.stdout.splitlines()
    assert lines[0] == "active: v2"
    assert lines[1] == "release r1: not prepared"
    assert lines[2].startswith("release v2: prepared ")
    assert "after r1, period 2 (1 applies)" in lines[2]
    assert lines[3] == "apply: none recorded"
    report = json.loads(site.run("status", "--format", "json").stdout)
    assert report["active"] == "v2" and report["apply"] is None
    assert [release["name"] for release in report["releases"]] == ["r1", "v2"]


def test_the_service_mode_takes_only_release_names_a_request_id_and_an_actor(
    site: Site,
) -> None:
    rid = "0" * 8 + "-0000-0000-0000-" + "0" * 12
    for args in (
        ("service", "seal", "v2", "not-an-id", "os/dsl41"),
        ("service", "seal", "../x", rid, "os/dsl41"),
        ("service", "seal", "r1", rid, "a b"),
        ("service", "seal", "r1", rid),
        ("service", "committed"),
        ("service", "pins", "x"),
        ("service", "changes", "r1", "../x"),
        ("service", "run", "v2"),
        ("service",),
    ):
        refused = site.run(*args)
        assert refused.returncode == 2, args
    assert site.seals() == []
    shown = site.run("service", "committed", rid)
    assert shown.returncode == 0 and json.loads(shown.stdout) == {"committed": False}


def test_the_launcher_vars_mode_prints_what_the_helper_reads(site: Site) -> None:
    done = subprocess.run(
        [str(site.base / "bin" / "dsl41-launch"), "vars"], capture_output=True, text=True
    )
    assert done.returncode == 0
    values: dict[str, str] = {}
    for line in done.stdout.splitlines():
        key, _, value = shlex.split(line)[0].partition("=")
        values[key] = value
    assert values["ESTATE_RELEASES"] == str(site.releases)
    assert values["ACTIVE"] == str(site.active)
    assert values["MAX_JOBS"] == "107" and values["MAX_GRACE_S"] == "10"
    assert shlex.split(values["NEXT_PROFILE"]) == [
        "--next-detached",
        "--next-as-machine",
        "localhost",
        "--next-machine-policy",
        "strict",
    ]
