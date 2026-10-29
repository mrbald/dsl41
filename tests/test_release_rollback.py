"""DL-407, DL-412: `dsl41 release rollback` and its take-over of an apply whose period opened. The site and the stub engine are tests/release_site.py's."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from release_site import Site, site_at


@pytest.fixture
def site(tmp_path: Path) -> Iterator[Site]:
    yield from site_at(tmp_path)


def test_rollback_applies_the_previous_release_and_keeps_every_receipt(site: Site) -> None:
    site.prepared("v1")
    site.prepared("v2", add_job=True)
    refused = site.run("rollback", "v1")
    assert refused.returncode == 2
    assert "no receipt of r1 names the release it replaced." in refused.stderr
    assert "apply the earlier release by name: `dsl41 release apply NAME`." in refused.stderr
    assert site.run("apply", "v1").returncode == 0
    first = site.receipt("v1")
    assert site.run("apply", "v2").returncode == 0
    back = site.run("rollback", "v1")
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
    assert site.run("rollback", "v2").returncode == 0 and site.active.read_text() == "v2\n"


def test_rollback_refuses_a_name_that_is_not_the_receipts_previous(site: Site) -> None:
    """DL-423: NAME must be the release the active one's latest receipt names; any other name
    is refused with the target printed, and nothing is written."""
    site.prepared("v1")
    site.prepared("v2")
    site.prepared("v3")
    assert site.run("apply", "v1").returncode == 0
    assert site.run("apply", "v2").returncode == 0
    receipts = sorted((site.releases).glob("*/.release/*.json"))
    seals = len(site.seals())
    refused = site.run("rollback", "v3")
    assert refused.returncode == 2
    assert (
        "dsl41 release: v3 is not the rollback's target: the latest receipt of v2 names v1.\n"
        "dsl41 release: run `dsl41 release rollback v1`, or `dsl41 release apply v3` to apply"
        " v3 by name.\n"
    ) in refused.stderr
    assert site.active.read_text() == "v2\n" and site.record() is None
    assert sorted((site.releases).glob("*/.release/*.json")) == receipts
    assert len(site.seals()) == seals


def test_rollback_refuses_a_name_that_is_not_the_recorded_applys_previous(site: Site) -> None:
    """DL-423: with an apply recorded whose period opened, NAME must be the release that apply
    replaced; the record, its holds and ACTIVE stay as they were, and the engine is asked for
    nothing."""
    site.prepared("v1")
    assert site.run("apply", "v1").returncode == 0
    site.prepared("v2")
    site.edit(release_outcomes={"NB_FREE_C": [4]})
    assert site.run("apply", "v2").returncode == 3
    record = site.record()
    assert record is not None and record["step"] == "verified"
    sent, calls = site.state()["sent"], site.state()["calls"]
    refused = site.run("rollback", "v2")
    assert refused.returncode == 2
    assert (
        "dsl41 release: an apply of v2 is recorded with its new period open; a rollback goes"
        " to v1, the release it replaced, not to v2.\n"
        "dsl41 release: run `dsl41 release rollback v1` to take it over, or `dsl41 release"
        " resume` to finish it.\n"
    ) in refused.stderr
    assert site.record() == record and site.record()["step"] == "verified"  # type: ignore[index]
    assert site.state()["sent"] == sent and site.state()["calls"] == calls
    assert site.active.read_text() == "v2\n"


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
        site.command("rollback", "v1"),
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
