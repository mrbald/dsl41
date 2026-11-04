# Coordinate agent work

For the operator and boss, and maintainers running those roles.
Read the [shared contract](../../CLAUDE.md) first.
Follow the maintainer track under [Documents](../../README.md#documents).
The operator dispatches and records work. The boss makes binding rulings.
This guide owns authority routing, merge approval, escalation and bindings.
Acceptance belongs to [review.md](review.md); environments and gates belong to
[agent-workflow.md](../agent-workflow.md).

## Operating model

The [approved operating model](operating-model.md) sets authority, role limits and owner escalations.

Owner's policy: 2026-11-01; review on 2026-11-08.
The owner is the human project owner. Six agent roles have fixed authority
and replaceable bindings. In maintained instructions, keep model names and
effort levels in this table; procedures and skills refer to roles.
Execution records still name the actual selection and returned identity.

| Role | Authority | Model and effort | Stand-in |
| --- | --- | --- | --- |
| boss | definition of done, owner-approved scope, allocation, binding rulings, release readiness | Astra (`gpt-6-astra`), high; xhigh for one named hard question | owner appoints |
| advisor | one bounded hard question; recommendation only; read-only | Fable, xhigh | fresh Astra, xhigh |
| operator | interactive session: briefs, dispatch, receipts, records, merge train, Linux-cell gate; applies rulings | Opus, medium | owner appoints |
| builder | settled approach; routine details | Sol (`gpt-6.1-sol`), medium | boss appoints |
| engineer | settled behavior; method needs judgment | Sol (`gpt-6.1-sol`), xhigh | boss appoints |
| reviewer | independent acceptance against the definition of done: code, tests, evidence provenance; fresh context, read-only | Opus, high | fresh Astra, high |

The implementer is either the builder or the engineer, working in the slice
worktree. They are alternatives, never stages. The advisor is used rarely
and has no acceptance authority.

Read [required review passes](review.md#required-passes) when allocating a change.
Obtain any additional reviewer's binding from the boss. Apply the
[fallback procedure](review.md#required-passes) when a reviewer is unavailable.
Judge bindings by accepted work, escaped defects, rework and elapsed time.

### Route a ruling

1. Send definitions, semantic disagreements, compatibility questions, exceptions
   and changes of scope, policy or contract meaning to the boss (DL-440).
   The boss sends any scope expansion to the owner (DL-457).
2. Pass an advisor question only at the boss's request, through the operator.
   Return the recommendation to the boss for its ruling.
3. Record the deciding seat's adversarial self-review before acting on a ruling
   covered by [rule 11](operating-model.md#who-decides).
4. Return the ruling unchanged in the next brief. Quote it, name exactly what it
   supersedes, and leave wording to the implementer unless the ruling fixes it.
5. Check the ruling against owner decisions. Hold the affected action for the
   owner if it reverses one; keep unaffected work moving.

For a release, dispatch a reviewer pass on the release candidate against the
project's definition of done. Send the exact candidate, review verdict and
acceptance evidence to the boss for a readiness ruling. Bring that ruling to
the owner for approval to tag or release.
Accepted slices alone do not establish release readiness.

### Check a handoff

1. Read the status, diffstat, judgment calls, unmet requirements and cited hunks.
   Check every participant's output against the rules, including the operator's
   own output. Return a nonconforming handoff to its author.
2. Send the exact candidate and evidence to the assigned reviewers. Route
   `rework` to its implementer, `accepted` to the merge train, and `blocked` to
   the boss. Keep one findings queue under [review.md](review.md#review-rounds).
3. Report each rule breach to the owner with the evidence and corrective action.
   Follow [Verify a change](../agent-workflow.md#verify-a-change) to decide
   whether checks must run again. Check output is evidence, not acceptance.

### Apply a settled edit or conflict resolution

1. Record the exact edit named by the reviewer or ruling and check it against
   [rules 28-30](operating-model.md#limits-on-the-operator). Send code, test or
   contract-meaning edits to the implementer; send ambiguity to the boss.
2. Apply only that permitted edit. Return it to the raiser for confirmation.
   Render derived files and add accepted Known-bugs lines before final checks.
3. For a merge conflict, compare both reviewed versions. Keep each text unchanged
   in its intended place and regenerate derived files. If that cannot resolve
   it, send the conflict to the implementer or boss.
4. Send the resolved merge for its own review, then run the checks its resolution
   invalidated. Follow the [workflow rules](#workflow-rules) for Git commands.

For an area the owner declares sealed, log each late finding as one line under
Known bugs in `TODO.md`, without rework. Include the raiser and round, behavior
and area; keep private paths out.
Escalate a finding that contradicts release acceptance to the owner.

### Handle an unavailable seat or broken tool

Use [rules 42-46](operating-model.md#when-things-fail) for the limits on these
steps and [rule 38](operating-model.md#limits-on-the-operator) for action authority.

1. Attach the failed invocation, partial output and availability evidence to the
   change's record. Record the named stand-in selected or the seat still waiting.
2. Keep fact notes for a waiting seat under a separate heading with the operator
   as author. Mark the ruling or acceptance that the seat still owes.
3. Stop the broken invocation and save its diagnostics. Write a separate fix
   brief and track its review and check receipts before returning the tool to
   service. List already-authorized manual steps that can proceed meanwhile.
4. For an uncertain write, query the remote candidate or operation identifier
   and attach the observed state before preparing a retry.

### Bring an item to the owner

1. Check the [owner escalation list](operating-model.md#what-escalates-to-the-owner).
   Settle checkable facts within the role's access; route semantic calls and
   unexplained failures to the boss. For an unknown that needs a trial, propose
   a bounded trial and a stop condition within the existing authority.
2. Bring only the remaining owner decision, with facts and a recommendation.
   Use chat, or an authorized notification when the owner is away. Keep the
   operator on all traffic and record the owner's chat answer.
3. If the operator and boss disagree on the rules, send both readings and the
   recommendation to the owner. Keep breach reports explicit.

### Clean up a run

Use [rule 37](operating-model.md#limits-on-the-operator) and the
[owner escalation list](operating-model.md#what-escalates-to-the-owner) to decide
which resources and actions qualify.

1. Make a resource inventory with run identity, ownership evidence, activity and
   shared-resource status. Save the failure records the inventory points to.
2. Check the inventory against those limits and the shared contract's delete
   approvals. Record the permitted actions, skipped resources and any owner
   decision still needed.
3. Compare live resource identities with the inventory immediately before each
   permitted deletion. Record the outcome and the evidence retained.

## Agent allocation

This is a standing user request to delegate under the rules below. If session
constraints prevent required review, report the conflict; do not claim acceptance.

How to allocate is the core's **Allocation of cognition** prior
(`~/.hats/core/PRIORS.md`). Read that rule before delegating.
Four local amendments override it where they differ (DL-206):

- House-voice writing stays top-tier, alongside semantic rulings. The
  `reader-docs` drafting exception (DL-405) leaves voice rulings with the boss.
- Frozen contracts are a named review trigger, with semantics. The
  contracts are listed in the [shared contract](../../CLAUDE.md#read-for-the-task).
- The operator's evidence checks and escalation follow [Operating model](#operating-model).
- The prior's exceptions do not waive the [verification gates](../agent-workflow.md#verify-a-change)
  or [independent review](review.md#required-passes).

Choose tier, required review and context mode explicitly before any
delegation -- never default to inherit. A multi-slice plan sheet carries an
allocation column.
Give a builder or engineer a self-contained brief and only the history the task
needs. Do not clone the orchestration transcript by default. Reviewers start
fresh; rework resumes the implementer and the finding's raiser.
Run slices in parallel only when their files are disjoint. A slice that
shares files with one not yet integrated starts from the integrated tip
(DL-262).

Keep the main session's context lean. Store full findings in a scratch file;
if a restricted peer can only return text, the operator saves that report.
Routine handoffs carry a ranked verdict table plus blocker detail. The rework
brief points the implementer at that file and adds rulings on the
blockers; it does not restate findings. Mechanical agents get a report
length cap in the brief. Skill loads are the expensive context items --
budget for them, not for diff reads.

## Git workflow

- Subject to the brief's side-effect limits, the operator commits a slice once
  its gates pass and its independent review is accepted.
- Apply rule 27's standing owner approval: verify accepted review, CI green on
  the exact candidate and no open blocking ruling, then rebase-merge without
  asking and without deleting a branch. Take a merge outside those conditions
  to the owner. Ask before pushing to a protected branch.
- Follow the owner's commit-message wording. Add no trailers, including
  co-author trailers.
  AI attribution belongs in the single top-level README footer line.
- Push after each logically complete slice rather than batching many commits.
- The pull, staging, delete and command-timeout rules are in the shared
  contract's "Shared work safeguards". Untracked scratch here is often
  estate-derived.

## Workflow rules

1. Run the merge train with `TRAIN_RECORDS=/absolute/outside/path python
   scripts/merge_train.py`; keep `queue.json`, PR bodies and the write journal
   outside every checkout. Keep one open PR: the tip. Other branches wait
   without a PR or rebasing. When a branch reaches the tip, the train rebases
   it if conflict-free (DL-442). It carries acceptance only with proof that
   the whole accepted commit range is equivalent, then reruns gates and CI.
   Unproved equivalence pauses for acceptance of the new head.
   Main requires up-to-date branches: the tip must contain the current main
   and pass gates and CI on that head and base before merging. If main moves,
   repeat this procedure for the tip only. Announce parallel work before it
   starts and list it in train order.
   On `atomic-base-guard-unavailable`, restore strict required-status-check
   protection and `enforce_admins` before restarting. On `rebase-conflict`,
   send the recorded commands to the implementer under rule 7.
   On `rebase-review-required`, obtain acceptance and copy the candidate head
   and base from `state.json` into `queue.json`. On `integration-review-required`,
   record the resolved worktree's reviewed head and base in `queue.json`, with
   `integration_reviewed_commit` for that head, then restart with `--recover`.
   `implementer-push-required` needs rule 5's publication first. Recovery accepts
   only that reviewed remote head. `rebase-ancestry-unproven` requires correcting
   the reviewed base or head and its review receipt before retrying.
   To withdraw a pending conflict, remove its entry from `queue.json` while the
   train is stopped; restart to clear its local journal state. If merely reordered,
   `pending-conflict-not-tip` requires restoring it to the tip or withdrawing it.
   Other refusals retain evidence in `state.json` and `log.jsonl`.
   After an uncertain write, inspect remote state and use `--recover` only
   when the recorded old ref, absent PR or still-open PR proves no delivery.
2. One branch, one worktree, one writing agent per slice. Create worktrees
   with `scripts/worktree.sh BRANCH`. Edit the decision log and the todo on
   the tip only. Before dispatch, create an empty file for each new document the
   brief names and mark it with `git add -N PATH`. Before a branch's final gates and after each rebase: stage
   new documents, render the decision index, stage it.
3. CI runs the suite only for a change outside `README.md`, `CLAUDE.md`,
   `docs/**/*.md`, `examples/**/*.md` and `.claude/skills/**/*.md` (DL-434); a docs-only PR merges on its docs job. Locally a
   docs-only change runs `scripts/prepush_checks.py --docs` instead of the
   suite. Other gates and reviews stand.
4. Hooks run ruff, ruff format and mypy on each edited Python file and the
   index render on each edited docs page. Install `scripts/prepush_checks.py`
   as the pre-push hook. The gate agent runs the gates in
   `docs/agent-workflow.md` that no hook ran over the whole tree, under the
   session rules of [Verify a change](../agent-workflow.md#verify-a-change).
5. The implementer publishes a conflict-resolved tip only with both lease flags:
   `git push --force-with-lease --force-if-includes origin BRANCH`. If only
   the forced update is blocked, push as `BRANCH-v2`, close the old PR, open
   its replacement. The train pins its lease to the recorded remote head;
   `remote-head-changed` requires inspection before resuming. Never force-push main.
6. Remove worktrees with `wt remove --discard-ignored`. The deletes that need
   the owner's yes are in the shared contract's "Shared work safeguards".
7. On `rebase-conflict`, the train aborts and restores the old head. The implementer
   who knows the change runs `git fetch`, then `git rebase --autostash origin/main`
   in that worktree. It resolves and inspects each conflicting commit, or escalates
   to the boss. The operator may apply only the mechanical
   [conflict procedure](#apply-a-settled-edit-or-conflict-resolution), under
   [rule 33](operating-model.md#limits-on-the-operator). In a later command, run
   `git diff --check` and, for Markdown, `uv run pytest -q
   tests/test_docs_hygiene.py`. Stage explicit paths after both pass. Run
   `git diff --cached --check` before each `git rebase --continue`. After the
   rebase finishes, publish under rule 5 and obtain integration-delta review
   before resuming under rule 1. Never edit `state.json` to adopt a remote head.
8. Read the failed CI step and retain its evidence. Send an unexplained failure
   to the boss. Rerun once only for a diagnosed infrastructure transient:
   `gh run rerun ID --failed`. Trace a reported pre-existing failure to its first
   failing revision; neither a stamp nor a later green run closes it. Split a test
   file over five minutes on one worker when its tests can run apart.
9. A document rewrite runs `scripts/doc_deps.py` as
   [Your task](../agent-workflow.md#your-task) says.
10. Call Codex CLI peers through `scripts/peer.sh`, and other clients, as
    [Peer review](#peer-review) says.

## Client setup

Project client defaults live in `.codex/config.toml` and
`.claude/settings.json`. The [role table](#operating-model) owns
model and effort selection. Pass both explicitly; defaults may serve another role.
Codex loads project configuration only in trusted projects.
Check effective model and effort after a CLI or model upgrade.
Launch flags, environment, user settings, and managed settings may affect them.
The operator records each role's selected model and effort, plus returned
identity when exposed.
If identity is not exposed, record that gap; selection is not server verification.
Do not silently substitute another model if the requested one is unavailable.

## Peer review

Use the role table above and [required review passes](review.md#required-passes).
Follow [Review rounds](review.md#review-rounds) for rework and escalation.

Keep scratch files outside the checkout when they may contain private context.
Start each brief with `Role: <role>`. Name the task, role guide, evidence paths,
exact artifact, allowed paths, side effects, required platforms and gates, and a
short report limit. Include the ruling and supersession identified in
[Route a ruling](#route-a-ruling).
Keep routine peer replies under 500 words unless a blocker needs more.
Exchange issue identifiers and changed conclusions rather than repeating the plan.
Group findings by root cause, not by each symptom.
The reviewer reads the diff against the contract. The boss resolves disagreements
from the findings and relevant evidence.

Agree on material decisions and the exact artifact version, recorded by commit
or file hash. Continue on open issues while a useful check or new evidence exists.
If discussion repeats, obtain the missing fact or identify the user tradeoff.
Silence, a failed invocation, and a timeout are not agreement.
Agreement complements tests; it does not replace them.

Every brief says "call no other model". Run from the repository or the slice's
worktree. Set `PEER_MODEL` to the bound model's CLI identifier and `PEER_EFFORT`
to its bound effort for each invocation. Set `PEER_TIMEOUT` in seconds from
the brief and size it to the task; a timeout is not a verdict. Obtain any additional reviewer's binding
from the boss. Do not infer either value from an earlier run.

For a read-only Claude peer, put the complete brief in a scratch file:

```sh
claude --print --model "${PEER_MODEL:?set from role binding}" \
  --effort "${PEER_EFFORT:?set from role binding}" --permission-mode manual \
  --tools 'Read,Glob,Grep' --strict-mcp-config \
  --no-session-persistence --output-format json < brief.md > review.json
```

Check the returned identity when exposed. Do not infer settings loading from
memory loading.
The restricted tool list excludes skill invocation; add `Skill` only when needed.
If the brief names evidence outside the checkout, grant that specific directory
with `--add-dir`; permission denials are not review findings.
If reusing a peer session matters, omit `--no-session-persistence` at creation
and resume the returned session ID for rework.
Keep raw logs in scratch; pass the verdict and relevant evidence to the operator.

For a read-only Codex peer, choose a fresh output directory:

```sh
review_dir=$(mktemp -d)
scripts/peer.sh "${PEER_MODEL:?set from role binding}" brief.md "$review_dir/review" "${PEER_TIMEOUT:?set from the brief}" \
  --effort "${PEER_EFFORT:?set from role binding}" --sandbox read-only
```

The wrapper writes `.jsonl`, `.err`, `.thread` and `.md` files at the output
prefix. Choose a unique scratch prefix for each pass. A verdict requires exit 0;
a partial message from a failed run is not a verdict. After a timeout, confirm
children have stopped before reusing the worktree.
Record the selection and any returned-identity gap, as Client setup says.
Brief it for targeted probes of the change under review.
It runs no full test suite unless the brief asks.
For rework, pass `--resume THREAD` using the thread ID written by the wrapper.
Keep passing the bound effort explicitly; the wrapper's defaults are not role bindings.
Keep existing permission controls. Do not bypass them to make a peer run succeed.

For a Codex builder or engineer, put the complete brief in a scratch file:

```sh
build_dir=$(mktemp -d)
scripts/peer.sh "${PEER_MODEL:?set from role binding}" brief.md "$build_dir/build" "${PEER_TIMEOUT:?set from the brief}" \
  --effort "${PEER_EFFORT:?set from role binding}" --sandbox workspace-write
```

Run from the checkout or worktree the slice owns.
The brief names the allowed paths and the gates to run, as for a peer.
A slice that runs containers passes `--sandbox containers` instead: workspace writes plus
network, so podman, docker and uv work. Inside it, set `UV_CACHE_DIR` under `/tmp`. Apple
`container` stays refused; use podman for a Linux cell.
Resume the same implementer for rework. If the sandbox blocks a required gate,
record the blocked command and request the needed access through the client's
permission controls.

## Keep context useful

Put stable requirements in the shared root and occasional procedures here or
in a skill. A short useful document does not need another routing layer.
Do not copy vendor system prompts or generate a repository map already available
from the files. Keep audit reports and session history out of startup context.
Keep operating rules in their owning git guide; memory holds pointers, private
facts and evidence. Leave private tooling recipes to their own reviewed slices.

Report outstanding work against the current goal, in severity order. Leave
parked work out unless asked. Final handoffs use Result, Done, Running, Needs
you and Next, omitting empty parts; answer a direct question first. Tag unresolved
items as needs-owner, checkable or needs-trial before routing them.

Treat tool-output filters such as RTK as optional local tooling.
Check exit codes, warnings, collection counts, and skipped tests against raw output.
Use raw output for file-by-file hygiene review and whenever a filter hides evidence.
Command-output byte savings are not whole-session token or cost savings.

Revisit the setup after a repeated failure, a model/harness upgrade, or a failed
loading check. Record evidence before adding another standing instruction.
