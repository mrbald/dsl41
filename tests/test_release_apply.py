"""DL-407, DL-412: `dsl41 release apply` and `resume`: holds, drain, seal, start, check and release. The site and the stub engine are tests/release_site.py's."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from release_site import ME, SMALL, Site, _job, site_at


@pytest.fixture
def site(tmp_path: Path) -> Iterator[Site]:
    yield from site_at(tmp_path)


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
    # the release's manifest under the launcher's environment, then the site's layer (DL-422)
    assert seal[5:9] == [
        "--manifest",
        f"{site.releases / 'v2'}/estate.toml",
        "--environment",
        "wargame",
    ]
    assert "--next" not in seal
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
        # the receipt is the service side's to write (DL-412)
        f"dsl41 release service -- receipt v2 applied {rid}",
    ]


def test_a_refused_hold_is_rejected_and_refuses_the_apply(site: Site) -> None:
    site.prepared("v2")
    site.edit(refuse_send=["NB_FREE_C"])
    refused = site.run("apply", "v2")
    assert refused.returncode == 2, refused.stderr
    assert "the engine refused to hold NB_FREE_C: refused: ON_HOLD NB_FREE_C" in refused.stderr
    assert site.record() is None and site.active.read_text() == "r1\n"
    assert site.state()["sent"] == [["ON_HOLD", "NB_B"], ["OFF_HOLD", "NB_B"]]


def test_a_refused_apply_whose_hold_the_engine_will_not_release_names_it(site: Site) -> None:
    """DL-412: an abort whose release of a hold is rejected three times records the hold
    kept with its reason, clears the record and exits 1, as a completed apply does."""
    site.prepared("v2")
    site.edit(seal_outcomes=["refuse"], release_outcomes={"NB_FREE_C": [3, 3, 3]})
    done = site.run("apply", "v2")
    assert done.returncode == 1, done.stderr
    assert "the seal said: seal refuse." in done.stderr
    assert "NB_FREE_C stays on hold: the engine did not release it" in done.stderr
    assert site.record() is None and site.state()["jobs"]["NB_FREE_C"]["on_hold"] is True
    (path,) = (site.releases / "v2" / ".release").glob("refused-*.json")
    receipt = json.loads(path.read_text())
    assert receipt["released"] == ["NB_B"] and receipt["kept"] == ["NB_FREE_C"]
    shown = site.run("status").stdout
    assert "  NB_FREE_C stays on hold: the engine did not release it" in shown


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
    (site.active.parent / ".release").mkdir(exist_ok=True)
    lock = site.active.parent / ".release" / "apply.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, b"pid 1, nbdeploy@host, since then\n")
        refused = site.run("apply", "v2")
        rollback = site.run("rollback", "r1")
    finally:
        os.close(fd)
    assert refused.returncode == 2
    assert f"another apply holds {lock}: pid 1, nbdeploy@host, since then" in refused.stderr
    assert rollback.returncode == 2 and "another apply holds" in rollback.stderr
    assert site.state()["sent"] == [] and site.record() is None


def test_the_environment_cannot_stop_an_apply(site: Site) -> None:
    """DL-412: the stop points are a test-only hook; the old variable does nothing."""
    site.prepared("v2")
    done = site.run("apply", "v2", env={"DSL41_RELEASE_STOP_AFTER": "seal"})
    assert done.returncode == 0, done.stderr
    assert site.record() is None and site.active.read_text() == "v2\n"


def test_apply_refuses_an_unprepared_changed_or_active_release(site: Site) -> None:
    shutil.copytree(SMALL, site.releases / "raw")
    refused = site.run("apply", "raw")
    assert refused.returncode == 2 and "raw is installed but not prepared." in refused.stderr
    assert "run `dsl41 release requalify raw` to check it in place." in refused.stderr
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


def test_a_log_record_without_its_attempt_number_stops_the_release_of_holds(site: Site) -> None:
    """DL-420: an attempt number is the join between a request and its decision. Another
    operator's ON_HOLD without `seq` and a decision without `index` must not join on nothing
    and hide that hold: the history refuses them as unreadable, the apply stops with its
    record, exit 3, and no OFF_HOLD is sent."""
    site.prepared("v2")
    site.edit(seal_outcomes=["unknown"])
    assert site.run("apply", "v2").returncode == 4
    segment = site.root / "wal" / "000001.jsonl"
    with segment.open("a") as stream:
        other = {"rec": "input", "kind": "ON_HOLD", "request_id": "x", "payload": {"job": "NB_B"}}
        stream.write(json.dumps(other) + "\n")
        undone = {"rec": "decision", "decision": "rejected", "legacy_batch": False}
        stream.write(json.dumps(undone) + "\n")
    resumed = site.run("resume")
    assert resumed.returncode == 3, resumed.stderr
    assert "without its attempt number" in resumed.stderr
    assert [verb for verb, _ in site.state()["sent"] if verb == "OFF_HOLD"] == []
    record = site.record()
    assert record is not None and record["step"] == "verified"
