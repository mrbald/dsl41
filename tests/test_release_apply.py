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


def test_a_refused_hold_is_rejected_and_refuses_the_apply(site: Site) -> None:
    site.prepared("v2")
    site.edit(refuse_send=["NB_FREE_C"])
    refused = site.run("apply", "v2")
    assert refused.returncode == 2, refused.stderr
    assert "the engine refused to hold NB_FREE_C: refused: ON_HOLD NB_FREE_C" in refused.stderr
    assert site.record() is None and site.active.read_text() == "r1\n"
    assert site.state()["sent"] == [["ON_HOLD", "NB_B"], ["OFF_HOLD", "NB_B"]]


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


def test_the_environment_cannot_stop_an_apply(site: Site) -> None:
    """DL-412: the stop points are a test-only hook; the old variable does nothing."""
    site.prepared("v2")
    done = site.run("apply", "v2", env={"DSL41_RELEASE_STOP_AFTER": "seal"})
    assert done.returncode == 0, done.stderr
    assert site.record() is None and site.active.read_text() == "v2\n"


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
