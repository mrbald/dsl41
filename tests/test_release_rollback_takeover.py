"""DL-407, DL-412: `dsl41 release rollback` taking over an apply that failed its check or stopped after it. The site and the stub engine are tests/release_site.py's."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from release_site import Site, failed_check, site_at


@pytest.fixture
def site(tmp_path: Path) -> Iterator[Site]:
    yield from site_at(tmp_path)


def test_rollback_takes_over_an_apply_whose_check_failed(site: Site) -> None:
    """DL-412: rollback may take over an apply whose new period opened and failed its check.
    It releases no hold: the rollback's apply owns them, so the jobs stay held through its
    seal, and it releases them at its end. The old apply's receipt is verify-failed-<id>."""
    record = failed_check(site)
    back = site.run("rollback", "v1")
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
    again = site.run("rollback", "v2")
    assert again.returncode == 2 and again.stderr == f"dsl41 release: {refusal}\n"
    assert site.active.read_text() == "v1\n" and len(site.seals()) == 3
    assert site.run("apply", "v2").returncode == 0
    assert site.active.read_text() == "v2\n"


def test_rollback_takes_over_an_apply_stopped_after_its_check(site: Site) -> None:
    """DL-412: an apply stopped while releasing its holds, its check passed, is taken over the
    same way; its receipt is taken-over-<id>, its outcome taken_over (DL-420)."""
    site.prepared("v1")
    assert site.run("apply", "v1").returncode == 0
    site.prepared("v2")
    site.edit(release_outcomes={"NB_FREE_C": [4]})
    assert site.run("apply", "v2").returncode == 3
    assert site.record()["step"] == "verified"  # type: ignore[index]
    back = site.run("rollback", "v1")
    assert back.returncode == 0, back.stderr
    (path,) = (site.releases / "v2" / ".release").glob("taken-over-*.json")
    given_up = json.loads(path.read_text())
    assert "verify_failed" not in given_up and given_up["problems"] == []
    assert given_up["outcome"] == "taken_over" and given_up["released"] == ["NB_B"]
    assert site.active.read_text() == "v1\n" and site.record() is None
    # the take-off in flight is sent again under its id and answered from its decision
    assert site.state()["sent"].count(["OFF_HOLD", "NB_FREE_C"]) == 2
    # a check that passed does not stop a later rollback to v2
    assert site.run("rollback", "v2").returncode == 0 and site.active.read_text() == "v2\n"


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
    killed = site.run("rollback", "v2", stop="takeover")
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
