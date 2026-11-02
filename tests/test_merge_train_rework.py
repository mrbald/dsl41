"""Rebase acceptance, recovery, queue concurrency and strict tested-base protection."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import runpy
import signal
import subprocess
import sys
import time

import pytest

from test_merge_train import (
    GIT_ROUTING,
    SCRIPT,
    Fixture,
    accept,
    call,
    checks,
    conflict,
    git,
    write,
)
from test_merge_train import train as train
from test_merge_train import train_seed as train_seed


def queue_data(train: Fixture) -> dict:
    return json.loads((train.records / "queue.json").read_text())


def save_queue(train: Fixture, data: dict) -> None:
    (train.records / "queue.json").write_text(json.dumps(data))


def branch_protection(*, strict: object = True, admins: object = True) -> dict:
    return {"required_status_checks": {"strict": strict}, "enforce_admins": {"enabled": admins}}


def review(train: Fixture) -> None:
    candidate = train.state()["candidate"]
    queue = queue_data(train)
    queue["entries"][0].update(
        accepted_commit=candidate["head"],
        accepted_base=candidate["base"],
        integration_reviewed_commit=candidate["head"],
    )
    save_queue(train, queue)


def advance_main(train: Fixture) -> str:
    write(train.root, "upstream.txt", "upstream\n")
    git(train.root, "add", "--", "upstream.txt")
    git(train.root, "commit", "-qm", "upstream")
    head = git(train.root, "rev-parse", "HEAD")
    git(train.root, "push", "-q", str(train.bare), "trunk")
    return head


def test_production_stops_without_an_atomic_base_precondition(train: Fixture) -> None:
    train.configure(protection=branch_protection(strict=False))
    result = train.run(production=True)
    assert result.returncode != 0
    state = train.state()
    assert state["reason"] == "atomic-base-guard-unavailable", result.stderr
    assert "server-side" in state["detail"]
    assert (
        state["ci"]
        == state["tested"]
        == {"head": train.head, "base": git(train.slice, "rev-parse", "HEAD^")}
    )
    assert not any("merge" in c for c in train.calls())
    assert train.slice.exists() and len(queue_data(train)["entries"]) == 1


@pytest.mark.parametrize("initial", [[], checks()[:-1], checks(status="REQUESTED", conclusion="")])
def test_incomplete_checks_become_green_inside_the_deadline(
    train: Fixture, initial: list[dict]
) -> None:
    # publish views once before the first CI poll.
    train.configure(check_sequence=[initial, initial, checks()])
    result = train.run(check_timeout="2")
    assert result.returncode == 0, result.stderr
    assert len([c for c in train.calls() if "view" in c and "pr" in c]) >= 4


@pytest.mark.parametrize("initial", [[], checks()[:-1], checks(status="REQUESTED", conclusion="")])
def test_incomplete_checks_have_a_bounded_deadline(train: Fixture, initial: list[dict]) -> None:
    train.configure(checks=initial)
    result = train.run(check_timeout="0.05")
    assert result.returncode != 0
    assert train.state()["reason"] == "checks-timeout"
    assert not any("merge" in c for c in train.calls())


def test_detached_extra_worktree_is_not_a_queue_failure(train: Fixture) -> None:
    extra = train.root.parent / "detached"
    git(train.root, "worktree", "add", "--detach", str(extra), "HEAD")
    result = train.run()
    assert result.returncode == 0, result.stderr
    assert extra.exists()


def test_clean_rebase_preserves_whole_range_and_uses_the_recorded_lease(train: Fixture) -> None:
    write(train.slice, "docs/second.md", "# Second commit\n")
    git(train.slice, "add", "--", "docs/second.md")
    git(train.slice, "commit", "-qm", "second accepted commit")
    accept(train)
    accepted = queue_data(train)["entries"][0]
    base = advance_main(train)
    result = train.run()
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in (train.records / "log.jsonl").read_text().splitlines()]
    rebase = next(e for e in events if e.get("event") == "rebased")
    assert rebase["original_acceptance"] == accepted
    assert (
        rebase["old_head"] == accepted["accepted_commit"]
        and rebase["old_base"] == accepted["accepted_base"]
    )
    assert rebase["base"] == base and rebase["head"] != rebase["old_head"]
    assert rebase["equivalent"] and len(rebase["range_proof"]["old"]) == 2
    assert rebase["range_proof"]["new"] == rebase["range_proof"]["old"]
    assert any(
        f"--force-with-lease=refs/heads/slice:{accepted['accepted_commit']}" in c
        for c in train.calls()
    )
    merge = next(c for c in train.calls() if "merge" in c)
    assert merge[merge.index("--match-head-commit") + 1] == rebase["head"]
    assert json.loads(train.config.read_text())["merge_attempts"][-1]["base"] == base


def test_two_accepted_entries_rebase_and_publish_without_manual_push(train: Fixture) -> None:
    original = queue_data(train)
    second_path = train.root.parent / "second"
    git(train.root, "worktree", "add", "-qb", "second", str(second_path), "trunk")
    write(second_path, "docs/second.md", "# Second\n")
    git(second_path, "add", "--", "docs/second.md")
    git(second_path, "commit", "-qm", "second")
    head = git(second_path, "rev-parse", "HEAD")
    git(train.root, "push", "-q", str(train.bare), "second")
    original["entries"].append(
        {**original["entries"][0], "branch": "second", "accepted_commit": head}
    )
    save_queue(train, original)
    train.configure(prs=[dict(number=1, branch="slice", head=train.head, state="OPEN")])
    result = train.run()
    assert result.returncode == 0, result.stderr
    assert queue_data(train)["entries"] == [] and not second_path.exists()
    merges = [c for c in train.calls() if "merge" in c]
    assert len(merges) == 2
    assert merges[1][merges[1].index("--match-head-commit") + 1] != head
    assert any(f"--force-with-lease=refs/heads/second:{head}" in c for c in train.calls())
    calls = train.calls()
    rebases = [c for c in calls if c[:2] == ["git", "rebase"] and "--onto" in c]
    assert len(rebases) == 1
    assert calls.index(merges[0]) < calls.index(rebases[0]) < calls.index(merges[1])


def test_unproved_clean_rebase_pauses_then_publishes_the_reviewed_head(train: Fixture) -> None:
    content = "".join(f"line {i}\n" for i in range(40))
    write(train.root, "shared.txt", content)
    git(train.root, "add", "--", "shared.txt")
    git(train.root, "commit", "-qm", "shared base")
    git(train.root, "push", "-q", str(train.bare), "trunk")
    git(train.slice, "rebase", "trunk")
    queue = queue_data(train)
    queue["entries"][0]["accepted_base"] = git(train.root, "rev-parse", "HEAD")
    save_queue(train, queue)
    write(train.slice, "shared.txt", content.replace("line 35\n", "accepted 35\n"))
    git(train.slice, "add", "--", "shared.txt")
    git(train.slice, "commit", "-qm", "accepted same-file edit")
    # The synthetic branch was rebased while preparing its original acceptance.
    train.head = git(train.slice, "rev-parse", "HEAD")
    queue = queue_data(train)
    queue["entries"][0]["accepted_commit"] = train.head
    save_queue(train, queue)
    old_remote = git(train.root, "ls-remote", str(train.bare), "refs/heads/slice").split()[0]
    git(
        train.root,
        "push",
        f"--force-with-lease=refs/heads/slice:{old_remote}",
        str(train.bare),
        "slice",
    )
    train.configure(head=train.head, docs=False)
    write(train.root, "shared.txt", content.replace("line 2\n", "upstream 2\n"))
    git(train.root, "add", "--", "shared.txt")
    git(train.root, "commit", "-qm", "main same-file edit")
    git(train.root, "push", "-q", str(train.bare), "trunk")
    result = train.run()
    assert result.returncode != 0 and train.state()["reason"] == "rebase-review-required", (
        result.stderr
    )
    candidate = train.state()["candidate"]
    assert not candidate["equivalent"] and not candidate["conflict"]
    review(train)
    result = train.run()
    assert result.returncode == 0, result.stderr
    assert any(f"--force-with-lease=refs/heads/slice:{train.head}" in c for c in train.calls())


def test_publication_lease_rejects_a_remote_race(train: Fixture) -> None:
    advance_main(train)
    train.configure(change_before_push=git(train.root, "rev-parse", "trunk"))
    train.refused("command-failed")
    assert train.state()["intent"]["kind"] == "push"


@pytest.mark.parametrize("when", ["poll", "before_merge", "atomic_write"])
def test_base_movement_never_merges_an_untested_composition(train: Fixture, when: str) -> None:
    write(train.root, "advance.txt", "advance\n")
    git(train.root, "add", "--", "advance.txt")
    git(train.root, "commit", "-qm", "advance later")
    advanced = git(train.root, "rev-parse", "HEAD")
    # Store the future base as an object without advancing the remote default ref.
    git(train.root, "push", "-q", str(train.bare), "trunk:refs/heads/future")
    train.configure(
        advanced_base=advanced,
        advance_on_view={"poll": 2, "before_merge": 3}.get(when),
        advance_at_merge=when == "atomic_write",
    )
    result = train.run()
    assert result.returncode == 0, result.stderr
    assert train.state()["status"] == "idle"
    cfg = json.loads(train.config.read_text())
    assert cfg["state"] == "MERGED" and not train.slice.exists()
    attempts = cfg["merge_attempts"]
    assert attempts[-1]["base"] == advanced and attempts[-1]["head"] != train.head
    if when == "atomic_write":
        assert len(attempts) == 2 and not attempts[0]["contains_base"]
    else:
        assert len(attempts) == 1
    assert len([c for c in train.calls() if "--docs" in c]) == 2


@pytest.mark.parametrize(
    "protection", [None, {}, branch_protection(strict=1), branch_protection(admins="true")]
)
def test_strict_protection_must_be_proven_before_any_merge_intent(
    train: Fixture, protection: object
) -> None:
    train.configure(protection=protection)
    train.refused("atomic-base-guard-unavailable")
    assert not train.state().get("intent")
    assert train.state()["ci"] == train.state()["tested"]
    assert any(c[:2] == ["gh", "api"] for c in train.calls())


@pytest.mark.parametrize(("strict", "admins"), [(False, True), (True, False), (False, False)])
def test_atomic_guard_requires_strict_and_admin_enforcement(
    train: Fixture,
    strict: bool,
    admins: bool,
) -> None:
    train.configure(protection=branch_protection(strict=strict, admins=admins))
    train.refused("atomic-base-guard-unavailable")
    assert not train.state().get("intent")


def test_protection_api_failure_refuses_without_a_merge_intent(train: Fixture) -> None:
    train.configure(protection_failure=True)
    train.refused("atomic-base-guard-unavailable")
    assert not train.state().get("intent")


def test_strict_protection_enables_the_unmodified_cli_path(train: Fixture) -> None:
    result = train.run(production=True)
    assert result.returncode == 0, result.stderr
    assert train.state()["status"] == "idle"
    merge = next(c for c in train.calls() if "merge" in c)
    assert "--match-head-commit" in merge and "--rebase" in merge
    assert not {"--admin", "--auto", "--delete-branch"}.intersection(merge)


def test_server_rejects_a_head_that_does_not_contain_main_despite_a_forged_tested_base(
    train: Fixture,
) -> None:
    base = advance_main(train)
    train.configure(record_base_without_rebase=True)
    result = train.run()
    assert result.returncode != 0 and train.state()["reason"] == "command-failed", result.stderr
    assert train.state()["tested"] == train.state()["ci"] == {"head": train.head, "base": base}
    cfg = json.loads(train.config.read_text())
    assert cfg["merge_attempts"] == [{"head": train.head, "base": base, "contains_base": False}]
    assert cfg["state"] == "OPEN" and train.slice.exists()
    assert git(train.bare, "rev-parse", "trunk") == base


@pytest.mark.parametrize("log_conflict", [False, True])
def test_conflict_pauses_for_the_implementer_and_preserves_waiting_branches(
    train: Fixture,
    log_conflict: bool,
) -> None:
    conflict(train, other=not log_conflict)
    second_path = train.root.parent / "waiting"
    base = queue_data(train)["entries"][0]["accepted_base"]
    git(train.root, "worktree", "add", "-qb", "waiting", str(second_path), base)
    write(second_path, "docs/waiting.md", "# Waiting\n")
    git(second_path, "add", "--", "docs/waiting.md")
    git(second_path, "commit", "-qm", "waiting")
    waiting = git(second_path, "rev-parse", "HEAD")
    queue = queue_data(train)
    queue["entries"].append(
        {**queue["entries"][0], "branch": "waiting", "accepted_commit": waiting}
    )
    save_queue(train, queue)
    train.refused("rebase-conflict")
    state = train.state()
    assert state["rebase_pending"]["old_head"] == train.head
    assert "git fetch upstream" in state["detail"]
    assert "git rebase --autostash upstream/trunk" in state["detail"]
    assert "--diff-filter=U at each conflicting commit" in state["detail"]
    assert "git push --force-with-lease --force-if-includes upstream slice" in state["detail"]
    assert state["rebase_pending"]["conflicting_paths"] == (
        ["docs/decision-index.md", "docs/decision-log.md"] if log_conflict else ["README.md"]
    )
    assert "integration_reviewed_commit" in state["detail"]
    assert str(train.slice) in state["detail"]
    assert git(train.slice, "rev-parse", "HEAD") == train.head
    assert not git(train.slice, "status", "--porcelain")
    assert git(second_path, "rev-parse", "HEAD") == waiting
    assert git(second_path, "rev-parse", "HEAD^") == base
    assert len([c for c in train.calls() if c[:2] == ["git", "rebase"] and "--onto" in c]) == 1
    assert not any(c[:2] == ["git", "add"] for c in train.calls())
    train.refused("integration-review-required")


def implementer_rebase(train: Fixture, contents: str = "# Upstream\n\nAccepted.\n") -> str:
    result = call(["git", "fetch", "upstream"], train.slice, train.env)
    assert result.returncode == 0, result.stderr
    result = call(["git", "rebase", "--autostash", "upstream/trunk"], train.slice, train.env)
    assert result.returncode == 1, result.stderr
    for attempt in range(3):
        assert git(train.slice, "diff", "--name-only", "--diff-filter=U") == "README.md"
        write(train.slice, "README.md", contents + f"\nResolution {attempt + 1}.\n")
        git(train.slice, "diff", "--check")
        result = call(
            ["uv", "run", "pytest", "-q", "tests/test_docs_hygiene.py"], train.slice, train.env
        )
        assert result.returncode == 0, result.stderr
        git(train.slice, "add", "--", "README.md")
        git(train.slice, "diff", "--cached", "--check")
        result = call(
            ["git", "rebase", "--continue"], train.slice, {**train.env, "GIT_EDITOR": "true"}
        )
        if result.returncode == 0:
            return git(train.slice, "rev-parse", "HEAD")
        assert result.returncode == 1, result.stderr
    pytest.fail("implementer fixture exceeded its three conflicting commits")


def implementer_push(train: Fixture) -> None:
    result = call(
        ["git", "push", "--force-with-lease", "--force-if-includes", "upstream", "slice"],
        train.slice,
        train.env,
    )
    assert result.returncode == 0, result.stderr


def record_integration_review(train: Fixture, head: str, base: str) -> None:
    queue = queue_data(train)
    queue["entries"][0].update(
        accepted_commit=head,
        accepted_base=base,
        integration_reviewed_commit=head,
    )
    save_queue(train, queue)


def test_reviewed_conflict_needs_the_implementer_push_before_recovery(train: Fixture) -> None:
    conflict(train, other=True)
    train.refused("rebase-conflict")
    base = train.state()["rebase_pending"]["base"]
    head = implementer_rebase(train)
    record_integration_review(train, head, base)
    result = train.run(recover=True)
    assert result.returncode != 0 and train.state()["reason"] == "implementer-push-required"
    assert not any(c[:2] == ["git", "push"] for c in train.calls())
    implementer_push(train)
    result = train.run(recover=True)
    assert result.returncode == 0, result.stderr


def test_conflict_recovery_refuses_an_unexpected_remote_head(train: Fixture) -> None:
    conflict(train, other=True)
    train.refused("rebase-conflict")
    base = train.state()["rebase_pending"]["base"]
    head = implementer_rebase(train)
    record_integration_review(train, head, base)
    git(train.bare, "update-ref", "refs/heads/slice", base)
    result = train.run(recover=True)
    assert result.returncode != 0 and train.state()["reason"] == "remote-head-changed"
    assert git(train.bare, "rev-parse", "slice") == base
    assert train.state()["rebase_pending"] is not None
    assert not any(c[:2] == ["git", "push"] for c in train.calls())


def test_withdrawn_conflict_exits_idle_without_a_journal_edit_or_publication(
    train: Fixture,
) -> None:
    conflict(train, other=True)
    train.refused("rebase-conflict")
    queue = queue_data(train)
    queue["entries"] = []
    save_queue(train, queue)
    result = train.run()
    assert result.returncode == 0, result.stderr
    assert train.state()["status"] == "idle" and train.state()["reason"] is None
    assert train.state()["candidate"] is None and train.state()["rebase_pending"] is None
    assert queue_data(train) == queue
    assert train.slice.exists() and git(train.slice, "rev-parse", "HEAD") == train.head
    assert not any(c[:2] == ["git", "push"] for c in train.calls())


def test_withdrawing_a_conflicted_tip_allows_the_next_tip_to_merge(train: Fixture) -> None:
    conflict(train, other=True)
    train.refused("rebase-conflict")
    base = train.state()["rebase_pending"]["base"]
    path = train.root.parent / "second"
    git(train.root, "worktree", "add", "-qb", "second", str(path), base)
    write(path, "docs/second.md", "# Second\n")
    git(path, "add", "--", "docs/second.md")
    git(path, "commit", "-qm", "second")
    head = git(path, "rev-parse", "HEAD")
    queue = queue_data(train)
    queue["entries"] = [
        {**queue["entries"][0], "branch": "second", "accepted_commit": head, "accepted_base": base}
    ]
    save_queue(train, queue)
    train.configure(prs=[])
    result = train.run()
    assert result.returncode == 0, result.stderr
    assert queue_data(train)["entries"] == []
    assert train.slice.exists() and not path.exists()
    assert train.state()["rebase_pending"] is None


def test_reordered_pending_conflict_has_a_named_recovery(train: Fixture) -> None:
    conflict(train, other=True)
    train.refused("rebase-conflict")
    queue = queue_data(train)
    queue["entries"].insert(0, {**queue["entries"][0], "branch": "other"})
    save_queue(train, queue)
    train.refused("pending-conflict-not-tip")
    assert "restore slice to the queue tip" in train.state()["detail"]
    assert train.state()["rebase_pending"] is not None


@pytest.mark.parametrize("failure", ["base", "head"])
def test_conflict_recovery_names_an_unproven_ancestry_check(train: Fixture, failure: str) -> None:
    conflict(train, other=True)
    train.refused("rebase-conflict")
    pending = train.state()["rebase_pending"]
    base, head = pending["old_base"], pending["base"]
    if failure == "head":
        base = pending["base"]
        tree = git(train.slice, "rev-parse", "HEAD^{tree}")
        head = git(train.root, "commit-tree", tree, "-p", pending["old_base"], "-m", "reviewed")
    record_integration_review(train, head, base)
    result = train.run(recover=True)
    assert result.returncode != 0 and train.state()["reason"] == "rebase-ancestry-unproven"
    assert base in train.state()["detail"]
    assert train.state()["rebase_pending"] == pending
    assert not train.state().get("intent")


def test_accepted_range_with_an_unproven_base_has_a_named_stop(train: Fixture) -> None:
    base = advance_main(train)
    queue = queue_data(train)
    queue["entries"][0]["accepted_base"] = base
    save_queue(train, queue)
    train.refused("rebase-ancestry-unproven")
    assert not train.state().get("intent")


@pytest.mark.parametrize("advance_after_pause", [False, True])
def test_conflicting_base_move_resumes_only_after_implementer_and_integration_review(
    train: Fixture,
    advance_after_pause: bool,
) -> None:
    write(train.root, "README.md", "# Upstream\n")
    git(train.root, "add", "--", "README.md")
    git(train.root, "commit", "-qm", "future conflict")
    base = git(train.root, "rev-parse", "HEAD")
    git(train.root, "push", "-q", str(train.bare), "trunk:refs/heads/future")
    train.configure(advance_at_merge=True, advanced_base=base)
    result = train.run()
    assert result.returncode != 0 and train.state()["reason"] == "rebase-conflict", result.stderr
    assert not train.state().get("intent")
    assert train.state()["tested"] is None and train.state()["ci"] is None
    assert json.loads(train.config.read_text())["state"] == "OPEN"
    assert git(train.slice, "rev-parse", "HEAD") == train.head
    if advance_after_pause:
        base = advance_main(train)
    head = implementer_rebase(train)
    implementer_push(train)
    queue = queue_data(train)
    queue["entries"][0].update(accepted_commit=head, accepted_base=base)
    save_queue(train, queue)
    train.refused("integration-review-required")
    queue["entries"][0]["integration_reviewed_commit"] = head
    save_queue(train, queue)
    train.refused("recovery-required")
    result = train.run(recover=True)
    assert result.returncode == 0, result.stderr
    assert train.state()["status"] == "idle" and not train.slice.exists()
    cfg = json.loads(train.config.read_text())
    assert len(cfg["merge_attempts"]) == 2
    assert cfg["merge_attempts"][-1]["head"] == head and cfg["merge_attempts"][-1]["base"] == base
    pushes = [c for c in train.calls() if c[:2] == ["git", "push"]]
    assert pushes == [
        ["git", "push", "--force-with-lease", "--force-if-includes", "upstream", "slice"]
    ]


@pytest.mark.parametrize("kind", ["push", "create", "merge"])
def test_recovery_retries_only_after_proving_absence(train: Fixture, kind: str) -> None:
    if kind == "push":
        train.configure(pr=False)
        git(train.root, "--git-dir", str(train.bare), "update-ref", "-d", "refs/heads/slice")
    if kind == "create":
        train.configure(pr=False)
    train.configure(fail=f"git {kind}" if kind == "push" else f"pr {kind}")
    assert train.run().returncode != 0
    train.configure(fail=None)
    result = train.run(recover=True)
    assert result.returncode == 0, result.stderr
    assert not train.slice.exists()


@pytest.mark.parametrize("tail", [False, True])
def test_recovery_after_queue_pop_handles_idle_and_next_entry(train: Fixture, tail: bool) -> None:
    if tail:
        queue = queue_data(train)
        queue["entries"].append({**queue["entries"][0], "branch": "later"})
        save_queue(train, queue)
    train.configure(kill_pop=True)
    assert train.run().returncode != 0
    assert train.state()["intent"]["kind"] == "pop"
    result = train.run(recover=True)
    if tail:
        assert result.returncode != 0 and train.state()["reason"] == "worktree-missing"
        assert queue_data(train)["entries"][0]["branch"] == "later"
    else:
        assert result.returncode == 0 and train.state()["status"] == "idle", result.stderr
    assert len([c for c in train.calls() if "merge" in c]) == 1


def test_enqueue_while_running_preserves_the_tail_after_merge(train: Fixture) -> None:
    entry = {**queue_data(train)["entries"][0], "branch": "later"}
    path = train.root.parent / "enqueue.json"
    path.write_text(json.dumps(entry))
    train.configure(enqueue_on_view=2, enqueue_file=str(path), script=str(SCRIPT))
    result = train.run()
    assert result.returncode != 0 and train.state()["reason"] == "worktree-missing", result.stderr
    assert queue_data(train)["entries"] == [entry]
    assert not train.slice.exists()


def test_enqueue_uses_its_own_lock_and_keeps_the_active_journal(train: Fixture) -> None:
    train.configure(protection=branch_protection(strict=False))
    assert train.run(production=True).returncode != 0
    state = (train.records / "state.json").read_bytes()
    entry = {**queue_data(train)["entries"][0], "branch": "later"}
    path = train.root.parent / "enqueue.json"
    path.write_text(json.dumps(entry))
    with (train.records / "train.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = call([sys.executable, str(SCRIPT), "--enqueue", str(path)], train.root, train.env)
    assert result.returncode == 0, result.stderr
    assert (train.records / "state.json").read_bytes() == state
    assert queue_data(train)["entries"][-1] == entry


def test_unrelated_prs_do_not_stop_the_train(train: Fixture) -> None:
    train.configure(other_pr=True)
    result = train.run()
    assert result.returncode == 0, result.stderr


def test_another_queued_pr_still_stops_the_train(train: Fixture) -> None:
    queue = queue_data(train)
    queue["entries"].append({**queue["entries"][0], "branch": "second"})
    save_queue(train, queue)
    train.configure(
        prs=[
            dict(number=1, branch="slice", head=train.head, state="OPEN"),
            dict(number=2, branch="second", head=train.head, state="OPEN"),
        ]
    )
    train.refused("other-queued-pr")


@pytest.mark.parametrize("value", ["inf", "nan", "0", "-1"])
def test_ci_deadline_must_be_finite_and_positive(train: Fixture, value: str) -> None:
    result = train.run(production=True, check_timeout=value)
    assert result.returncode == 2 and "positive and finite" in result.stderr


def test_ci_default_has_margin_for_the_slowest_job(train: Fixture) -> None:
    result = call([sys.executable, str(SCRIPT), "--help"], train.root, train.env)
    assert result.returncode == 0 and "default: 7200" in result.stdout
    assert "accepted_commit and accepted_base in queue.json" in result.stdout


@pytest.mark.parametrize("fault", ["timeout", "signal"])
def test_stopped_command_kills_its_grandchild(train: Fixture, fault: str) -> None:
    ready = train.root.parent / "ready.json"
    train.configure(hang="uv", ready=str(ready))
    env = {k: v for k, v in train.env.items() if k not in GIT_ROUTING}
    with subprocess.Popen(
        [sys.executable, str(SCRIPT), "--command-timeout", "1"],
        cwd=train.root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as process:
        try:
            deadline = time.monotonic() + 15
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert ready.exists()
            ids = json.loads(ready.read_text())
            if fault == "signal":
                process.send_signal(signal.SIGTERM)
            _, stderr = process.communicate(timeout=10)
            assert process.returncode != 0, stderr
        finally:
            if ready.exists():
                ids = json.loads(ready.read_text())
                try:
                    os.killpg(ids["group"], signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(ids["child"], 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        pytest.fail("grandchild survived process-group termination")
    assert train.state()["reason"] == ("command-failed" if fault == "timeout" else "interrupted")


@pytest.mark.parametrize("difference", ["whitespace", "order", "omitted"])
def test_range_proof_checks_earlier_commits_and_order(train: Fixture, difference: str) -> None:
    base = git(train.root, "rev-parse", "trunk")
    git(train.slice, "reset", "--hard", base)
    write(train.slice, "first.py", "    x = 1\n")
    git(train.slice, "add", "--", "first.py")
    git(train.slice, "commit", "-qm", "first")
    write(train.slice, "second.txt", "second\n")
    git(train.slice, "add", "--", "second.txt")
    git(train.slice, "commit", "-qm", "second")
    old = git(train.slice, "rev-parse", "HEAD")
    git(train.slice, "reset", "--hard", base)
    operations = ["first", "second"] if difference != "order" else ["second", "first"]
    if difference == "omitted":
        operations = ["second"]
    for operation in operations:
        name = "first.py" if operation == "first" else "second.txt"
        content = "x = 1\n" if difference == "whitespace" else "    x = 1\n"
        write(train.slice, name, content if operation == "first" else "second\n")
        git(train.slice, "add", "--", name)
        git(train.slice, "commit", "-qm", operation)
    new = git(train.slice, "rev-parse", "HEAD")
    module = runpy.run_path(str(SCRIPT))
    instance = module["Train"](argparse.Namespace(command_timeout=5))
    instance.root, instance.env = train.root, train.env
    before = instance.signatures(train.slice, base, old)
    after = instance.signatures(train.slice, base, new)
    assert before != after
    if difference == "whitespace":
        assert before[-1][0] == after[-1][0], "the tip alone would miss this change"
    if difference == "order":
        assert git(train.slice, "rev-parse", old + "^{tree}") == git(
            train.slice, "rev-parse", new + "^{tree}"
        )
