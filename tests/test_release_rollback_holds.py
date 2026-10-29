"""DL-407, DL-412: `dsl41 release rollback` takeover: holds it carries or cannot release. The site and the stub engine are tests/release_site.py's."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from release_site import Site, _job, failed_check, site_at


@pytest.fixture
def site(tmp_path: Path) -> Iterator[Site]:
    yield from site_at(tmp_path)


def test_a_refused_takeover_leaves_the_failed_release_active_and_says_so(site: Site) -> None:
    """DL-412: when the rollback's apply is refused, its abort releases the holds it owns,
    the failed release stays active, status says so, and a rollback tries again."""
    failed_check(site)
    state = site.state()
    state["jobs"]["NB_IDLE_C"]["status"] = "RUNNING"
    site.save(state)
    refused = site.run("rollback", "v1", "--drain-timeout", "1")
    assert refused.returncode == 2, refused.stderr
    assert site.active.read_text() == "v2\n" and site.record() is None
    state = site.state()
    assert not state["jobs"]["NB_FREE_C"]["on_hold"] and not state["jobs"]["NB_B"]["on_hold"]
    (path,) = (site.releases / "v2" / ".release").glob("verify-failed-*.json")
    shown = site.run("status").stdout
    assert f"active: v2; its last apply did not finish, see {path.name}." in shown
    assert "rollback: `dsl41 release rollback v1` would apply v1." in shown
    state["jobs"]["NB_IDLE_C"]["status"] = "SUCCESS"
    site.save(state)
    back = site.run("rollback", "v1")
    assert back.returncode == 0, back.stderr
    assert site.active.read_text() == "v1\n"


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
    back = site.run("rollback", "v1")
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


def test_a_takeover_keeps_a_hold_the_engine_will_not_release(site: Site) -> None:
    """DL-412: the rollback owns the failed apply's holds; one the engine will not release at
    its end is kept, named with its command, and the rollback exits 1."""
    failed_check(site)
    site.edit(release_outcomes={"NB_FREE_C": [3] * 3})
    back = site.run("rollback", "v1")
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
