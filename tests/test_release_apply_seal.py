"""DL-407, DL-412: `dsl41 release apply` and `resume`: the seal and the apply around it. The site and the stub engine are tests/release_site.py's."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from release_site import ME, Site, site_at


@pytest.fixture
def site(tmp_path: Path) -> Iterator[Site]:
    yield from site_at(tmp_path)


def test_a_seal_that_does_not_commit_promotes_nothing_and_releases_the_holds(
    site: Site,
) -> None:
    site.prepared("v2")
    site.edit(seal_outcomes=["refuse"])
    refused = site.run("apply", "v2")
    assert refused.returncode == 2, refused.stderr
    # the seal's own reason is a line of its own (DL-412)
    assert (
        "dsl41 release: the seal did not commit.\ndsl41 release: the seal said: seal refuse.\n"
        in (refused.stderr)
    )
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
    assert receipt["reason"] == ["the seal did not commit.", "the seal said: seal refuse."]
    shown = site.run("status").stdout
    assert "last apply: v2 over r1, started " in shown
    (line,) = [line for line in shown.splitlines() if line.startswith("last apply:")]
    assert line.endswith(
        ": refused, and nothing was sealed: the seal did not commit. the seal said: seal refuse."
    )
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
    back = site.run("rollback", "r1")
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
        " `dsl41 release rollback r1`, or resume to check again.\n" in stopped.stderr
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
        "`dsl41 release rollback r1` keeps this apply's holds and rolls back to r1 under them;"
        " the holds are released when that rollback ends." in stopped.stderr
    )
    # resume checks again, and stops again with the record kept
    again = site.run("resume")
    assert again.returncode == 3 and site.record() is not None
    shown = site.run("status").stdout
    assert "  the new period does not match the seal: period 2's segment" in shown
    assert "  `dsl41 release resume` will check the new period again" in shown
    assert "  `dsl41 release rollback r1` keeps this apply's holds and rolls back to r1" in shown
