"""DL-407, DL-412: `dsl41 release apply`: horizon and drain refusals, and the latent job. The site and the stub engine are tests/release_site.py's."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from release_site import Site, site_at


@pytest.fixture
def site(tmp_path: Path) -> Iterator[Site]:
    yield from site_at(tmp_path)


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


def test_an_offline_horizon_refusal_is_waited_out_by_its_answer_line(site: Site) -> None:
    """DL-412, DL-420: an offline seal prints the gate's sentence on stderr and its answer line,
    code and time left, on stdout; the apply reads the wait from the field and seals again. The
    stub renders both through the gate and `refuse_seal` themselves."""
    site.prepared("v2")
    site.edit(seal_outcomes=["horizon-offline"])
    done = site.run("apply", "v2")
    assert done.returncode == 0, done.stderr
    assert "waiting 1.5 s for the retry horizon of the last operator request" in done.stderr
    first, second = [call[call.index("--request-id") + 1] for call in site.seals()]
    assert first != second == site.receipt("v2")["request_id"]


def test_a_horizon_wait_is_read_from_its_field_not_its_prose(site: Site) -> None:
    """DL-420: the refusal's prose names a 30 s old attempt under a 60 s horizon, and its
    `retry_horizon_left_us` field says 0.2 s are left. The apply waits by the field."""
    site.prepared("v2")
    site.edit(seal_outcomes=["horizon-misworded"])
    done = site.run("apply", "v2")
    assert done.returncode == 0, done.stderr
    assert "waiting 1.2 s for the retry horizon of the last operator request" in done.stderr
    assert "31.0 s" not in done.stderr
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
