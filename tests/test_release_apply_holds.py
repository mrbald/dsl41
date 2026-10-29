"""DL-407, DL-412: `dsl41 release apply` and `resume`: the helper's holds and their release. The site and the stub engine are tests/release_site.py's."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from release_site import Site, site_at


@pytest.fixture
def site(tmp_path: Path) -> Iterator[Site]:
    yield from site_at(tmp_path)


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
