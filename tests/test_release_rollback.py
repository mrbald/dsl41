"""DL-407, DL-412: `dsl41 release rollback` and its take-over of an apply whose period opened. The site and the stub engine are tests/release_site.py's."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from release_site import Site, _job, site_at


@pytest.fixture
def site(tmp_path: Path) -> Iterator[Site]:
    yield from site_at(tmp_path)


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
    assert failed["outcome"] == "verify_failed" and "verify_failed" not in failed
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
        f"v2's last apply failed its check after the open ({path.name}), so a rollback does"
        " not apply it; to retry it on purpose, run `dsl41 release apply v2`."
    )
    shown = site.run("status").stdout
    assert f"rollback: none, since v2's last apply failed its check ({path.name})." in shown
    assert "would apply v2" not in shown
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


@pytest.mark.parametrize("restored", [True, False])
def test_a_carried_ghost_hold_is_settled_again_by_the_rollback(site: Site, restored: bool) -> None:
    """DL-412: a hold the failed apply kept because its release removed the job is the
    rollback's own; once the rollback defines the job again it releases it, and while the
    job stays a ghost it keeps the hold, names it and exits 1."""
    site.prepared("v1")
    assert site.run("apply", "v1").returncode == 0
    site.prepared("v2")
    state = site.state()
    state["jobs"]["NB_Z_C"] = _job("CMD")
    state["timers"].append({"due": "2026-10-28T02:00:00", "job": "NB_Z_C", "kind": "schedule"})
    state["release_outcomes"] = {"NB_Z_C": [4]}
    site.save(state)
    # the apply of v2 holds the jobs, then v2 drops NB_FREE_C, and NB_Z_C's release is lost
    site.edit(seal_outcomes=["unknown"])
    assert site.run("apply", "v2").returncode == 4
    state = site.state()
    state["jobs"]["NB_FREE_C"]["job_type"] = None
    site.save(state)
    assert site.run("resume").returncode == 3
    record = site.record()
    assert record is not None and record["step"] == "verified"
    (ghost,) = [hold for hold in record["holds"] if hold["job"] == "NB_FREE_C"]
    assert ghost["state"] == "kept" and ghost["kept_because"] == "undefined"
    if restored:
        state = site.state()
        state["jobs"]["NB_FREE_C"]["job_type"] = "CMD"
        site.save(state)
    back = site.run("rollback")
    if restored:
        assert back.returncode == 0, back.stderr
        assert "NB_FREE_C" in site.receipt("v1")["released"]
        assert site.state()["jobs"]["NB_FREE_C"]["on_hold"] is False
    else:
        assert back.returncode == 1, back.stderr
        assert "NB_FREE_C stays on hold: the job is no longer defined." in back.stderr
        assert site.receipt("v1")["kept"] == ["NB_FREE_C"]
        shown = site.run("status").stdout
        assert "  NB_FREE_C stays on hold: the job is no longer defined." in shown


def test_rollback_takes_over_an_apply_stopped_after_its_check(site: Site) -> None:
    """DL-412: an apply stopped while releasing its holds, its check passed, is taken over the
    same way; its receipt is taken-over-<id>, its outcome taken_over (DL-420)."""
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
    assert "verify_failed" not in given_up and given_up["problems"] == []
    assert given_up["outcome"] == "taken_over" and given_up["released"] == ["NB_B"]
    assert site.active.read_text() == "v1\n" and site.record() is None
    # the take-off in flight is sent again under its id and answered from its decision
    assert site.state()["sent"].count(["OFF_HOLD", "NB_FREE_C"]) == 2
    # a check that passed does not stop a later rollback to v2
    assert site.run("rollback").returncode == 0 and site.active.read_text() == "v2\n"


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


def test_status_names_no_release_for_a_hold_the_rollback_owns_mid_drain(site: Site) -> None:
    """DL-412, DL-420: an apply stopped at `verified` kept NB_B (its release was rejected three
    times). The rollback takes it over and owns NB_B. While that rollback drains, `status`
    prints the taken-over receipt but no OFF_HOLD command for NB_B: the rollback settles it."""
    import signal
    import subprocess
    import time

    site.prepared("v1")
    assert site.run("apply", "v1").returncode == 0
    site.prepared("v2")
    site.edit(release_outcomes={"NB_B": [3, 3, 3], "NB_FREE_C": [4]})
    assert site.run("apply", "v2").returncode == 3
    record = site.record()
    assert record is not None and record["step"] == "verified"
    (kept,) = [hold for hold in record["holds"] if hold["job"] == "NB_B"]
    assert kept["state"] == "kept" and kept["kept_because"] == "rejected"
    state = site.state()
    state["jobs"]["NB_IDLE_C"]["status"] = "RUNNING"
    site.save(state)
    rollback = subprocess.Popen(
        site.command("rollback"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=site.environ(),
    )
    try:
        deadline = time.monotonic() + 120
        while (site.record() or {}).get("step") != "held":
            assert rollback.poll() is None and time.monotonic() < deadline
            time.sleep(0.1)
        shown = site.run("status").stdout
    finally:
        rollback.send_signal(signal.SIGINT)
        _, stderr = rollback.communicate(timeout=120)
    assert rollback.returncode == 3, stderr
    assert "  holds taken by this apply: NB_B" in shown
    assert "last apply: v2 over v1" in shown and "a rollback took it over" in shown
    assert "NB_B stays on hold" not in shown and "OFF_HOLD -J NB_B" not in shown
