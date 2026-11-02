"""Rebase acceptance, recovery, queue concurrency and the unavailable atomic merge guard."""

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


def queue_data(train: Fixture) -> dict:
    return json.loads((train.records / "queue.json").read_text())


def save_queue(train: Fixture, data: dict) -> None:
    (train.records / "queue.json").write_text(json.dumps(data))


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
    assert merge[merge.index("--expected-base-commit") + 1] == base


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


def test_every_resolved_conflict_requires_an_integration_receipt(train: Fixture) -> None:
    conflict(train)
    train.refused("integration-review-required")
    candidate = train.state()["candidate"]
    queue = queue_data(train)
    queue["entries"][0].update(accepted_commit=candidate["head"], accepted_base=candidate["base"])
    save_queue(train, queue)
    train.refused("integration-review-required")
    review(train)
    result = train.run()
    assert result.returncode == 0, result.stderr


def test_index_only_conflict_is_rendered_and_pauses_for_integration_review(train: Fixture) -> None:
    write(train.slice, "docs/decision-index.md", "# Slice index\n")
    git(train.slice, "add", "--", "docs/decision-index.md")
    git(train.slice, "commit", "-qm", "slice index")
    accept(train)
    write(train.root, "docs/decision-index.md", "# Upstream index\n")
    git(train.root, "add", "--", "docs/decision-index.md")
    git(train.root, "commit", "-qm", "upstream index")
    git(train.root, "push", "-q", str(train.bare), "trunk")
    train.refused("integration-review-required")
    assert train.state()["candidate"]["conflict"]
    assert not git(train.slice, "diff", "--name-only", "--diff-filter=U")


def test_unexpected_remote_change_after_review_is_not_overwritten(train: Fixture) -> None:
    conflict(train)
    train.refused("integration-review-required")
    review(train)
    other = git(train.root, "rev-parse", "trunk")
    git(train.root, "--git-dir", str(train.bare), "update-ref", "refs/heads/slice", other)
    train.refused("remote-head-changed")
    assert git(train.root, "ls-remote", str(train.bare), "refs/heads/slice").startswith(other)


def test_publication_lease_rejects_a_remote_race(train: Fixture) -> None:
    advance_main(train)
    train.configure(change_before_push=git(train.root, "rev-parse", "trunk"))
    train.refused("command-failed")
    assert train.state()["intent"]["kind"] == "push"


@pytest.mark.parametrize("when", ["poll", "atomic_write"])
def test_base_movement_never_merges_an_untested_composition(train: Fixture, when: str) -> None:
    write(train.root, "advance.txt", "advance\n")
    git(train.root, "add", "--", "advance.txt")
    git(train.root, "commit", "-qm", "advance later")
    advanced = git(train.root, "rev-parse", "HEAD")
    # Store the future base as an object without advancing the remote default ref.
    git(train.root, "push", "-q", str(train.bare), "trunk:refs/heads/future")
    train.configure(
        advanced_base=advanced,
        advance_on_view=2 if when == "poll" else None,
        advance_at_merge=when == "atomic_write",
    )
    result = train.run(production=when == "poll")
    assert result.returncode != 0
    assert train.state()["reason"] == ("base-moved" if when == "poll" else "command-failed")
    cfg = json.loads(train.config.read_text())
    assert cfg["state"] == "OPEN" and train.slice.exists()


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
