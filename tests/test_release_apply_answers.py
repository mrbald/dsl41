"""DL-407, DL-412: `dsl41 release apply` and `resume`: a hold or release the engine refuses, rejects or leaves undecided, settled by the log. The site and the stub engine are tests/release_site.py's."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from release_site import Site, site_at


@pytest.fixture
def site(tmp_path: Path) -> Iterator[Site]:
    yield from site_at(tmp_path)


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
