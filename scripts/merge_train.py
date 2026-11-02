#!/usr/bin/env python3
"""Run the accepted merge queue. Python 3.12+, git, wt, gh and uv are required.

Usage: TRAIN_RECORDS=/absolute/outside/path python scripts/merge_train.py
       python scripts/merge_train.py --recover
       python scripts/merge_train.py --enqueue /outside/entry.json

Create queue.json in TRAIN_RECORDS before starting, for example:
{"entries": [{"branch": "slice", "accepted_commit": "FULL_COMMIT_ID",
              "accepted_base": "FULL_BASE_COMMIT_ID",
              "body": "bodies/slice.md", "blocking_ruling": false}]}
An entry attests independent acceptance of exactly that commit and no open
blocking ruling. Keep PR bodies here too; the first line supplies the title.
Use --enqueue to append a JSON entry while running. Direct edits require a stopped
train. Appends preserve the existing entries; a queue lock prevents lost appends.
The train removes only its verified merged first entry, preserving later entries.

owner.json binds records to the git common directory and remote repository.
state.json is a durable write journal and machine-readable status; retain it on
failure. log.jsonl records commands and their results. Do not hand-edit the
journal. A second train, even with another records directory, is refused.
Lock refusals write state-<pid>.json, preserving the running train's journal.
Without a safe records directory, bootstrap refusals report JSON on stderr.
TRAIN_NOTIFY optionally names an executable (no shell or arguments); each stop
or merge is sent as one JSON line on stdin. Notification failures are ignored.

Only one unambiguous git remote is supported. The default branch and repository
identity come from gh. wt's inventory is cross-checked against git. The train
rebases, checks, publishes the accepted head, opens one PR, waits for CI, and
checks the accepted candidate and tested base. It never deletes a branch. Clean
rebases carry acceptance only when every commit, in order, has identical exact
blob/mode changes, message and author. No whitespace normalization or patch IDs
are used. The original acceptance and each old/new head/base are journalled.
Unproved equivalence pauses: record independent acceptance by updating the queued
accepted_commit and accepted_base to the candidate in state.json. Every conflict
resolution additionally requires integration_reviewed_commit equal to that SHA.
Then restart. Publication uses the recorded remote head as an explicit lease.
Only decision-log tail appends and derived index conflicts may be resolved; a
failed resolution aborts the rebase and restores the branch.

GitHub's merge API has no expected-base precondition. This implementation stops
with atomic-base-guard-unavailable after green CI. Enabling merges needs a
server-side compare-and-merge operation that atomically guards both tested head
and base while enforcing branch protection and required checks. Neither a fetch
nor --match-head-commit closes that race. No main push or bypass is attempted.
Only PRs whose branches are queued participate in the one-open-PR rule.

An uncertain push/create/merge is journalled before sending. On restart remote
state must prove completion. --recover also permits retry when remote state
proves no delivery (the old leased ref, no PR, or the same PR still OPEN).
Unexpected remote changes stop. Recovery also completes an interrupted queue pop.
After a verified merge only that entry's clean, unlocked, linked worktree may
be removed, via wt remove --discard-ignored. Failure evidence is retained.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

LOG = "docs/decision-log.md"
INDEX = "docs/decision-index.md"
MARKER = re.compile(rb"^(?:<{7}|>{7}|\|{7}|={7})(?:\s|$)", re.MULTILINE)
REQUIRED = {"classify", "docs", "test (3.12)", "test (3.13)", "goldens", "coverage", "explore-page"}
DOC_SKIPS = REQUIRED - {"classify", "docs"}
GIT_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_COMMON_DIR",
    "GIT_PREFIX",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)


class Stop(Exception):
    def __init__(self, reason: str, detail: str = "", exit_status: int | None = None):
        self.reason, self.detail = reason, detail
        self.exit_status = exit_status


def require(condition: object, reason: str, detail: str = "") -> None:
    if not condition:
        raise Stop(reason, detail)


def read_json(path: Path) -> Any:
    return json.loads(path.read_bytes())


def atomic_json(path: Path, value: Any) -> None:
    require(not path.is_symlink(), "records-symlink", path.name)
    fd, name = tempfile.mkstemp(prefix=".train-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def docs_only(paths: list[str]) -> bool:
    # DL-434: both sides of a rename count; an empty diff proves nothing.
    return bool(paths) and all(
        p in {"README.md", "CLAUDE.md"}
        or p.endswith(".md")
        and p.startswith(("docs/", "examples/", ".claude/skills/"))
        for p in paths
    )


def command(
    cmd: tuple[str, ...],
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """A stopped command leaves no running member of its process group (rule 46)."""
    with subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        start_new_session=True,
        stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as process:
        try:
            stdout, stderr = process.communicate(input_bytes, timeout=timeout)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            raise
        return subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)


class Train:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.env = {k: v for k, v in os.environ.items() if k not in (*GIT_ENV, "VIRTUAL_ENV")}
        self.env.update(GIT_EDITOR="true", GIT_TERMINAL_PROMPT="0")
        self.root = Path.cwd().resolve()
        self.records: Path | None = None
        self.start_records: Path | None = None
        self.state: dict[str, Any] = {}
        self.locks: list[Any] = []

    def event(self, **value: Any) -> None:
        if self.records:
            with (self.records / "log.jsonl").open("a") as stream:
                stream.write(json.dumps({"time": time.time(), **value}) + "\n")

    def notify(self, event: str, **values: Any) -> None:
        line = json.dumps({"event": event, **values}) + "\n"
        executable = self.env.get("TRAIN_NOTIFY")
        if executable:
            try:
                command((executable,), self.root, self.env, 5, line.encode())
            except (OSError, subprocess.TimeoutExpired):
                pass

    def save(self, **values: Any) -> None:
        self.state.update(values)
        if self.records:
            atomic_json(self.records / "state.json", self.state)

    def run(self, *cmd: str, cwd: Path | None = None, ok: tuple[int, ...] = (0,)) -> bytes:
        try:
            result = command(cmd, cwd or self.root, self.env, self.args.command_timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Stop("command-failed", f"{cmd[0]}: {exc}") from exc
        self.event(
            command=cmd,
            status=result.returncode,
            stdout=result.stdout.decode(errors="replace"),
            stderr=result.stderr.decode(errors="replace"),
        )
        if result.returncode not in ok:
            raise Stop("command-failed", f"{cmd}: exit {result.returncode}", result.returncode)
        return result.stdout

    def git(self, *args: str, cwd: Path | None = None) -> bytes:
        return self.run("git", *args, cwd=cwd)

    def gh(self, *args: str) -> Any:
        return json.loads(self.run("gh", "--repo", self.slug, *args))

    def lock(self, path: Path) -> None:
        stream = path.open("a+")
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            stream.close()
            raise Stop("already-running") from exc
        self.locks.append(stream)

    @contextmanager
    def queue_lock(self) -> Iterator[None]:
        assert self.records
        path = self.records / "queue.lock"
        require(not path.is_symlink(), "records-symlink", path.name)
        with path.open("a+") as stream:
            deadline = time.monotonic() + self.args.command_timeout
            while True:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    require(time.monotonic() < deadline, "queue-locked")
                    time.sleep(0.01)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def inventory(self) -> list[dict[str, Any]]:
        listing = json.loads(self.run("wt", "--json", "list"))
        require(listing.get("ok") is True and listing.get("schema") == 1, "worktree-inventory")
        trees = cast(list[dict[str, Any]], listing["result"])
        actual: dict[str, str] = {}
        for block in self.git("worktree", "list", "--porcelain", "-z").decode().split("\0\0"):
            fields = dict(line.split(" ", 1) for line in block.split("\0") if " " in line)
            if "worktree" in fields:
                actual[str(Path(fields["worktree"]).resolve())] = fields.get("branch", "")
        require(len(trees) == len(actual), "worktree-inventory")
        for tree in trees:
            path = str(Path(tree["path"]).resolve())
            require(
                path in actual
                and actual[path]
                == (f"refs/heads/{tree['branch']}" if tree["branch"] is not None else ""),
                "worktree-inventory",
            )
        return trees

    def setup(self) -> None:
        self.root = Path(self.git("rev-parse", "--show-toplevel").decode().strip()).resolve()
        common = Path(
            self.git("rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip()
        ).resolve()
        name = self.env.get("TRAIN_RECORDS")
        require(name and Path(name).is_absolute(), "records-unset-or-relative")
        assert name is not None
        records = Path(name).resolve()
        trees = self.inventory()
        require(
            not any(records.is_relative_to(Path(t["path"]).resolve()) for t in trees),
            "records-inside-checkout",
        )
        records.mkdir(parents=True, exist_ok=True)
        for item in (
            "queue.json",
            "state.json",
            "owner.json",
            "log.jsonl",
            "train.lock",
            "queue.lock",
        ):
            require(not (records / item).is_symlink(), "records-symlink", item)
        self.start_records = records
        if not self.args.enqueue:
            self.lock(records / "train.lock")
            # A common-dir lock also excludes a second records directory (rule 46).
            self.lock(common / "merge-train.lock")
        self.records = records
        if not self.args.enqueue and (records / "state.json").exists():
            state = read_json(records / "state.json")
            require(isinstance(state, dict), "invalid-state")
            self.state = state
        remotes = self.git("remote").decode().splitlines()
        require(len(remotes) == 1, "ambiguous-remote")
        self.remote = remotes[0]
        url = self.git("remote", "get-url", self.remote).decode().strip()
        match = re.fullmatch(r"(?:https?://|ssh://git@|git@)([^/:]+)[/:](.+?)(?:\.git)?", url)
        require(match is not None, "remote-url")
        assert match
        self.slug = f"{match[1]}/{match[2]}"
        repo = json.loads(
            self.run(
                "gh",
                "repo",
                "view",
                self.slug,
                "--json",
                "nameWithOwner,url,defaultBranchRef,deleteBranchOnMerge",
            )
        )
        require(
            repo["nameWithOwner"].casefold() == match[2].casefold(), "remote-repository-mismatch"
        )
        require(repo["deleteBranchOnMerge"] is False, "branch-auto-delete")
        self.base = repo["defaultBranchRef"]["name"]
        self.git("check-ref-format", "--branch", self.base)
        owner = {"common_dir": str(common), "repository": repo["url"], "remote": url}
        if (records / "owner.json").exists():
            require(read_json(records / "owner.json") == owner, "records-owner")
        else:
            require(not self.state, "records-owner-missing")
            atomic_json(records / "owner.json", owner)

    def queue(self) -> tuple[bytes, dict[str, Any]]:
        assert self.records
        raw = (self.records / "queue.json").read_bytes()
        data = json.loads(raw)
        self.validate_queue(data)
        return raw, data

    def validate_queue(self, data: dict[str, Any]) -> None:
        assert self.records
        require(isinstance(data, dict) and isinstance(data.get("entries"), list), "invalid-queue")
        seen = set()
        for entry in data["entries"]:
            require(isinstance(entry, dict), "invalid-entry")
            branch, head = entry.get("branch"), entry.get("accepted_commit", "")
            require(
                isinstance(branch, str) and branch not in seen and branch != self.base,
                "invalid-branch",
            )
            self.git("check-ref-format", "--branch", branch)
            seen.add(branch)
            require(
                isinstance(head, str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head),
                "acceptance-missing",
            )
            require(
                isinstance(entry.get("accepted_base"), str)
                and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", entry["accepted_base"]),
                "acceptance-base-missing",
            )
            require(entry.get("blocking_ruling") is False, "blocking-ruling")
            body = entry.get("body")
            require(isinstance(body, str) and not Path(body).is_absolute(), "body-path")
            path = (self.records / body).resolve()
            require(path.is_relative_to(self.records) and path.is_file(), "body-path")
            require(path.read_text().strip(), "body-empty")
            require(path.read_text().splitlines()[0].lstrip("# "), "body-title-empty")

    def unchanged(self, raw: bytes) -> None:
        assert self.records
        expected = json.loads(raw)
        _, current = self.queue()
        require(
            {k: v for k, v in current.items() if k != "entries"}
            == {k: v for k, v in expected.items() if k != "entries"}
            and current["entries"][: len(expected["entries"])] == expected["entries"],
            "queue-changed",
        )

    def head(self, entry: dict[str, Any]) -> str:
        candidate = self.state.get("candidate")
        if candidate and candidate["entry"]["branch"] == entry["branch"]:
            return str(candidate["head"])
        return str(entry["accepted_commit"])

    def remote_head(self, entry: dict[str, Any]) -> str | None:
        remote = self.git("ls-remote", "--heads", self.remote, f"refs/heads/{entry['branch']}")
        return remote.split()[0].decode() if remote else None

    def worktree(self, entry: dict[str, Any]) -> Path:
        matches = [t for t in self.inventory() if t["branch"] == entry["branch"]]
        require(len(matches) == 1, "worktree-missing")
        tree = matches[0]
        path = Path(tree["path"]).resolve()
        require(
            not any(tree[k] for k in ("is_main", "locked", "prunable", "bare")),
            "worktree-protected",
        )
        require(
            path != self.root and not Path.cwd().resolve().is_relative_to(path), "worktree-active"
        )
        require(
            not self.git("status", "--porcelain", "--untracked-files=all", cwd=path),
            "worktree-dirty",
        )
        require(
            self.git("rev-parse", "HEAD", cwd=path).decode().strip() == self.head(entry),
            "local-head-stale",
        )
        return path

    def resolve(self, path: Path) -> None:
        conflicts = self.git("diff", "--name-only", "--diff-filter=U", "-z", cwd=path)
        names = {p.decode() for p in conflicts.split(b"\0") if p}
        require(names and names <= {LOG, INDEX}, "rebase-conflict")
        expected_log = self.git("show", f"HEAD:{LOG}", cwd=path)
        if LOG in names:
            old = self.git("show", f"REBASE_HEAD^:{LOG}", cwd=path)
            new = self.git("show", f"REBASE_HEAD:{LOG}", cwd=path)
            require(new.startswith(old) and len(new) > len(old), "log-not-tail-append")
            expected_log += new[len(old) :]
            (path / LOG).write_bytes(expected_log)
        else:
            # Rule 34 applies to regeneration even without a decision-log change.
            expected_log = (path / LOG).read_bytes()
        (path / INDEX).write_bytes(self.git("show", f"HEAD:{INDEX}", cwd=path))
        self.run("uv", "run", "python", "scripts/render_decision_index.py", cwd=path)
        # Rule 33 preserves both reviewed byte strings; a renderer may alter only the index.
        require((path / LOG).read_bytes() == expected_log, "log-render-changed")
        self.no_markers(path)
        self.git("diff", "--check", cwd=path)
        self.run("uv", "run", "pytest", "-q", "tests/test_docs_hygiene.py", cwd=path)
        self.git("add", "--", LOG, INDEX, cwd=path)
        self.git("diff", "--cached", "--check", cwd=path)
        self.git("rebase", "--continue", cwd=path)

    def signatures(self, path: Path, base: str, head: str) -> list[tuple[bytes, ...]] | None:
        commits = (
            self.git("rev-list", "--reverse", f"{base}..{head}", cwd=path).decode().splitlines()
        )
        signatures: list[tuple[bytes, ...]] = []
        parent = base
        for commit in commits:
            parents = (
                self.git("rev-list", "--parents", "-n", "1", commit, cwd=path).decode().split()
            )
            if parents[1:] != [parent]:
                return None
            raw = self.git("cat-file", "commit", commit, cwd=path)
            header, _, message = raw.partition(b"\n\n")
            author = next(line for line in header.splitlines() if line.startswith(b"author "))
            delta = self.git(
                "diff-tree",
                "--raw",
                "-z",
                "--no-abbrev",
                "--no-renames",
                "--no-commit-id",
                parent,
                commit,
                cwd=path,
            )
            signatures.append((delta, author, message))
            parent = commit
        return signatures if signatures else None

    def accepted_candidate(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        candidate = self.state.get("candidate")
        if not candidate:
            return None
        require(candidate["entry"]["branch"] == entry["branch"], "candidate-owner-changed")
        if candidate["review_required"]:
            require(
                entry["accepted_commit"] == candidate["head"]
                and entry["accepted_base"] == candidate["base"],
                "integration-review-required"
                if candidate["conflict"]
                else "rebase-review-required",
                f"accept head {candidate['head']} on base {candidate['base']} in queue.json",
            )
            require(
                not candidate["conflict"]
                or entry.get("integration_reviewed_commit") == candidate["head"],
                "integration-review-required",
            )
            candidate = {**candidate, "entry": entry, "review_required": False, "acceptance": entry}
            self.save(candidate=candidate)
            self.event(event="rebase-review-accepted", acceptance=entry)
        else:
            require(candidate["entry"] == entry, "candidate-owner-changed")
        return cast(dict[str, Any], candidate)

    def rebase(self, entry: dict[str, Any], path: Path, base: str) -> None:
        current = self.accepted_candidate(entry)
        old_head = current["head"] if current else entry["accepted_commit"]
        old_base = current["base"] if current else entry["accepted_base"]
        if current and base == old_base:
            return
        self.git("merge-base", "--is-ancestor", old_base, old_head, cwd=path)
        remote_head = current["remote_head"] if current else self.remote_head(entry)
        require(self.remote_head(entry) == remote_head, "remote-head-changed")
        if not current:
            require(remote_head in (None, old_head), "remote-head-stale")
        original = current["original_acceptance"] if current else entry
        candidate = {
            "entry": entry,
            "original_acceptance": original,
            "acceptance": current["acceptance"] if current else entry,
            "old_head": old_head,
            "old_base": old_base,
            "head": old_head,
            "base": base,
            "remote_head": remote_head,
            "conflict": False,
            "review_required": False,
            "equivalent": False,
        }
        self.save(candidate=candidate, tested=None, ci=None)
        conflict = False
        try:
            try:
                self.git(
                    "rebase",
                    "--merge",
                    "--no-update-refs",
                    "--reapply-cherry-picks",
                    "--empty=keep",
                    "--onto",
                    base,
                    old_base,
                    cwd=path,
                )
            except Stop as exc:
                if exc.exit_status != 1:
                    raise
                conflict = True
                self.resolve(path)
            head = self.git("rev-parse", "HEAD", cwd=path).decode().strip()
        except BaseException:
            # A failed rebase restores its branch before any later inventory (rule 46).
            rebase_dir = (
                self.git("rev-parse", "--git-path", "rebase-merge", cwd=path).decode().strip()
            )
            if (path / rebase_dir).exists():
                self.git("rebase", "--abort", cwd=path)
            require(
                self.git("rev-parse", "HEAD", cwd=path).decode().strip() == old_head,
                "rebase-restore-failed",
            )
            self.save(candidate=current)
            raise
        old = self.signatures(path, old_base, old_head)
        new = self.signatures(path, base, head)
        equivalent = old is not None and old == new
        candidate.update(
            head=head,
            conflict=conflict,
            equivalent=equivalent,
            review_required=conflict or not equivalent,
            range_proof={
                "old": [hashlib.sha256(b"\0".join(s)).hexdigest() for s in old or []],
                "new": [hashlib.sha256(b"\0".join(s)).hexdigest() for s in new or []],
            },
        )
        self.save(candidate=candidate)
        self.event(event="rebased", **candidate)
        self.accepted_candidate(entry)

    def no_markers(self, path: Path) -> None:
        for name in self.git("ls-files", "-z", cwd=path).split(b"\0"):
            if name:
                file = path / os.fsdecode(name)
                contents = (
                    os.fsencode(os.readlink(file)) if file.is_symlink() else file.read_bytes()
                )
                require(not MARKER.search(contents), "conflict-marker", os.fsdecode(name))

    def prepare(self, entry: dict[str, Any], raw: bytes) -> bool:
        path = self.worktree(entry)
        self.git("fetch", "--", self.remote)
        base = self.git("rev-parse", f"refs/remotes/{self.remote}/{self.base}").decode().strip()
        self.rebase(entry, path, base)
        self.no_markers(path)
        paths = [
            p.decode()
            for p in self.git(
                "diff", "--name-only", "--no-renames", "-z", f"{base}...HEAD", cwd=path
            ).split(b"\0")
            if p
        ]
        classification = (
            self.run(
                "uv",
                "run",
                "python",
                "scripts/prepush_checks.py",
                "--classify",
                "--base",
                base,
                cwd=path,
            )
            .decode()
            .strip()
        )
        docs = docs_only(paths)
        require(classification == ("code=false" if docs else "code=true"), "classification-invalid")
        self.run(
            "uv",
            "run",
            "python",
            "scripts/prepush_checks.py",
            "--base",
            base,
            *(("--docs",) if docs else ()),
            cwd=path,
        )
        if not docs:
            self.run("uv", "run", "pytest", "-q", "-n", "4", "--dist", "loadfile", cwd=path)
        else:
            for cmd in (
                ("ruff", "check", "src", "tests", "examples"),
                ("ruff", "format", "--check", "src", "tests", "scripts", "examples"),
                ("mypy",),
            ):
                self.run("uv", "run", *cmd, cwd=path)
        require(
            not self.git("status", "--porcelain", "--untracked-files=all", cwd=path),
            "gates-dirtied-worktree",
        )
        require(
            self.git("rev-parse", "HEAD", cwd=path).decode().strip() == self.head(entry),
            "local-head-stale",
        )
        self.unchanged(raw)
        self.save(tested={"head": self.head(entry), "base": base})
        return docs

    def prs(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        prs = self.gh(
            "pr",
            "list",
            "--state",
            "all",
            "--head",
            entry["branch"],
            "--base",
            self.base,
            "--json",
            "number,headRefOid,state",
        )
        return cast(list[dict[str, Any]], prs)

    def view(self, number: int, entry: dict[str, Any]) -> dict[str, Any]:
        pr = self.gh(
            "pr",
            "view",
            str(number),
            "--json",
            "number,state,headRefOid,headRefName,baseRefName,baseRefOid,isCrossRepository,isDraft,"
            "reviewDecision,statusCheckRollup,mergeCommit",
        )
        require(
            pr["headRefName"] == entry["branch"]
            and pr["baseRefName"] == self.base
            and pr["isCrossRepository"] is False,
            "pr-identity",
        )
        require(pr["headRefOid"] == self.head(entry), "pr-head-stale")
        return cast(dict[str, Any], pr)

    def intent(self, kind: str, entry: dict[str, Any], raw: bytes, **values: Any) -> None:
        self.save(
            status="working",
            intent={
                "kind": kind,
                "entry": entry,
                "queue": json.loads(raw),
                "head": self.head(entry),
                **values,
            },
        )

    def publish(self, entry: dict[str, Any], raw: bytes) -> int:
        prs = self.prs(entry)
        require(len(prs) <= 1, "ambiguous-pr")
        self.queued_prs_only(prs[0]["number"] if prs else None)
        candidate = self.state["candidate"]
        remote = self.remote_head(entry)
        head = self.head(entry)
        require(remote == candidate["remote_head"], "remote-head-changed")
        if prs:
            require(prs[0]["headRefOid"] == remote, "pr-head-stale")
        if remote != head:
            self.unchanged(raw)
            self.intent("push", entry, raw, remote_before=remote)
            self.git(
                "push",
                f"--force-with-lease=refs/heads/{entry['branch']}:{remote or ''}",
                "--force-if-includes",
                self.remote,
                f"{head}:refs/heads/{entry['branch']}",
            )
            require(self.remote_head(entry) == head, "push-unverified")
            candidate["remote_head"] = head
            self.save(candidate=candidate, intent=None)
        if not prs:
            assert self.records
            body = self.records / entry["body"]
            self.unchanged(raw)
            self.intent("create", entry, raw)
            self.run(
                "gh",
                "--repo",
                self.slug,
                "pr",
                "create",
                "--base",
                self.base,
                "--head",
                entry["branch"],
                "--title",
                body.read_text().splitlines()[0].lstrip("# "),
                "--body-file",
                str(body),
            )
            prs = self.prs(entry)
            require(len(prs) == 1, "pr-create-unverified")
            self.view(prs[0]["number"], entry)
            self.save(intent=None)
        self.view(prs[0]["number"], entry)
        self.queued_prs_only(prs[0]["number"])
        return int(prs[0]["number"])

    def queued_prs_only(self, number: int | None) -> None:
        _, queue = self.queue()
        branches = {entry["branch"] for entry in queue["entries"]}
        opens = self.gh("pr", "list", "--state", "open", "--json", "number,headRefName")
        require(
            all(p["number"] == number or p["headRefName"] not in branches for p in opens),
            "other-queued-pr",
        )

    def green(self, number: int, entry: dict[str, Any], docs: bool) -> None:
        deadline = time.monotonic() + self.args.check_timeout
        while True:
            pr = self.view(number, entry)
            require(pr["state"] == "OPEN" and not pr["isDraft"], "pr-not-open")
            require(pr["reviewDecision"] in ("", "APPROVED"), "review-not-accepted")
            require(pr["baseRefOid"] == self.state["tested"]["base"], "base-moved")
            checks = pr["statusCheckRollup"]
            require(isinstance(checks, list), "checks-invalid")
            names = [c.get("name") for c in checks]
            require(len(names) == len(set(names)), "checks-duplicate")
            pending = not REQUIRED <= set(names)
            for check in checks:
                require(check.get("__typename") == "CheckRun", "check-unrecognized")
                if check.get("status") in {
                    "QUEUED",
                    "IN_PROGRESS",
                    "PENDING",
                    "WAITING",
                    "REQUESTED",
                }:
                    pending = True
                    continue
                require(check.get("status") == "COMPLETED", "check-unrecognized")
                conclusion = check.get("conclusion")
                require(
                    conclusion == "SUCCESS"
                    or conclusion == "SKIPPED"
                    and docs
                    and check["name"] in DOC_SKIPS,
                    "check-not-green",
                    str(check),
                )
            if not pending:
                self.save(ci=dict(self.state["tested"]))
                return
            require(time.monotonic() < deadline, "checks-timeout")
            time.sleep(min(self.args.poll_interval, max(0, deadline - time.monotonic())))

    def verified_merge(self, number: int, entry: dict[str, Any]) -> None:
        pr = self.view(number, entry)
        require(pr["state"] == "MERGED" and pr.get("mergeCommit"), "merge-unverified")
        self.git("fetch", "--", self.remote)
        self.git(
            "merge-base",
            "--is-ancestor",
            pr["mergeCommit"]["oid"],
            f"refs/remotes/{self.remote}/{self.base}",
        )

    def finish(self, number: int, entry: dict[str, Any], raw: bytes, queue: dict[str, Any]) -> None:
        self.verified_merge(number, entry)
        self.unchanged(raw)
        matches = [t for t in self.inventory() if t["branch"] == entry["branch"]]
        if matches:
            path = self.worktree(entry)
            # Recheck immediately before removal; wt also enforces its own clean/lock checks.
            require(
                Path(self.run("wt", "path", entry["branch"]).decode().strip()).resolve() == path,
                "cleanup-identity",
            )
            self.run("wt", "remove", "--discard-ignored", entry["branch"])
            require(
                not any(t["branch"] == entry["branch"] for t in self.inventory()),
                "cleanup-unverified",
            )
        with self.queue_lock():
            self.unchanged(raw)
            _, latest = self.queue()
            remaining = {**latest, "entries": latest["entries"][1:]}
            self.intent("pop", entry, raw, pr=number, remaining=remaining)
            assert self.records
            atomic_json(self.records / "queue.json", remaining)
            self.save(
                status="merged", reason=None, intent=None, candidate=None, tested=None, ci=None
            )
        self.event(event="merged", branch=entry["branch"], pr=number)
        self.notify("merged", branch=entry["branch"], pr=number)

    def reconcile(self) -> bool:
        intent = self.state.get("intent")
        if not intent:
            return False
        entry = intent["entry"]
        raw, queue = self.queue()
        kind = intent["kind"]
        if kind == "pop":
            self.verified_merge(intent["pr"], entry)
            if queue["entries"] and queue["entries"][0] == entry:
                self.finish(intent["pr"], entry, raw, queue)
            else:
                self.unchanged(json.dumps(intent["remaining"]).encode())
                self.save(
                    status="merged", reason=None, intent=None, candidate=None, tested=None, ci=None
                )
                self.notify("merged", branch=entry["branch"], pr=intent["pr"])
            return True
        self.unchanged(json.dumps(intent["queue"]).encode())
        require(queue["entries"] and queue["entries"][0] == entry, "queue-owner-changed")
        if kind == "push":
            remote = self.remote_head(entry)
            if remote == intent["head"]:
                candidate = self.state["candidate"]
                candidate["remote_head"] = remote
                self.save(candidate=candidate)
            else:
                require(remote == intent["remote_before"], "remote-head-changed")
                require(self.args.recover, "recovery-required", "run with --recover")
        elif kind == "create":
            prs = self.prs(entry)
            require(len(prs) <= 1, "ambiguous-pr")
            if prs:
                self.view(prs[0]["number"], entry)
            else:
                require(self.args.recover, "recovery-required", "run with --recover")
        else:
            require(kind == "merge", "invalid-intent")
            pr = self.view(intent["pr"], entry)
            if pr["state"] == "MERGED":
                self.finish(intent["pr"], entry, raw, queue)
                return True
            require(pr["state"] == "OPEN", "pr-not-open")
            require(self.args.recover, "recovery-required", "run with --recover")
        self.event(event="write-reconciled", kind=kind, recovered=self.args.recover)
        self.save(intent=None)
        return False

    def merge(self, number: int, entry: dict[str, Any], raw: bytes) -> None:
        require(
            self.state.get("ci") == self.state.get("tested")
            and self.state["ci"]["head"] == self.head(entry),
            "ci-candidate-mismatch",
        )
        raise Stop(
            "atomic-base-guard-unavailable",
            "need a server-side expected-base compare-and-merge that preserves branch "
            "protection and required checks; gh --match-head-commit guards only the head",
        )

    def enqueue(self) -> None:
        assert self.records
        entry = read_json(Path(self.args.enqueue))
        with self.queue_lock():
            _, queue = self.queue()
            proposed = {**queue, "entries": [*queue["entries"], entry]}
            self.validate_queue(proposed)
            atomic_json(self.records / "queue.json", proposed)
        self.event(event="enqueued", branch=entry["branch"])

    def execute(self) -> None:
        self.setup()
        if self.args.enqueue:
            self.enqueue()
            return
        while True:
            if self.reconcile():
                continue
            raw, queue = self.queue()
            if not queue["entries"]:
                require(not self.state.get("intent"), "queue-owner-changed")
                self.save(status="idle", reason=None)
                return
            entry = queue["entries"][0]
            self.save(status="working", reason=None, branch=entry["branch"])
            docs = self.prepare(entry, raw)
            number = self.publish(entry, raw)
            self.green(number, entry, docs)
            self.unchanged(raw)
            self.merge(number, entry, raw)
            self.finish(number, entry, raw, queue)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--command-timeout", type=float, default=1800, help="seconds per command")
    parser.add_argument(
        "--check-timeout", type=float, default=7200, help="seconds waiting for CI (default: 7200)"
    )
    parser.add_argument("--poll-interval", type=float, default=10, help="seconds between CI reads")
    recovery = parser.add_mutually_exclusive_group()
    recovery.add_argument(
        "--recover", action="store_true", help="reconcile before retrying a failed write"
    )
    recovery.add_argument(
        "--enqueue", metavar="ENTRY_JSON", help="append one entry under the queue lock"
    )
    args = parser.parse_args()
    if not all(
        math.isfinite(value) and value > 0
        for value in (args.command_timeout, args.check_timeout, args.poll_interval)
    ):
        parser.error("timeouts must be positive and finite")
    train = Train(args)

    def interrupted(signum: int, frame: Any) -> None:
        raise Stop("interrupted", str(signum))

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        train.execute()
        return 0
    except Exception as exc:
        reason = exc.reason if isinstance(exc, Stop) else "invalid-data-or-io"
        detail = exc.detail if isinstance(exc, Stop) else str(exc)
        try:
            if reason == "already-running" and train.start_records:
                atomic_json(
                    train.start_records / f"state-{os.getpid()}.json",
                    {"status": "stopped", "reason": reason, "detail": detail},
                )
            if args.enqueue and train.records:
                atomic_json(
                    train.records / f"state-{os.getpid()}.json",
                    {"status": "stopped", "reason": reason, "detail": detail},
                )
            else:
                train.save(status="stopped", reason=reason, detail=detail)
            train.event(event="stopped", reason=reason, detail=detail)
        finally:
            train.notify("stopped", reason=reason)
            print(
                json.dumps({"status": "stopped", "reason": reason, "detail": detail}),
                file=sys.stderr,
            )
        return 1
    finally:
        for stream in train.locks:
            stream.close()


if __name__ == "__main__":
    sys.exit(main())
