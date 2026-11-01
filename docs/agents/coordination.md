# Coordinate agent work

For the operator and boss, and maintainers running those roles.
Read the [shared contract](../../CLAUDE.md) first.
The operator dispatches and records work. The boss makes binding rulings.
This guide owns role bindings, allocation, dispatch and the merge train.

## Operating model

Owner's policy: 2026-11-01; review on 2026-11-08.
The owner is the human project owner. Six agent roles have fixed authority
and replaceable bindings. In maintained instructions, keep model names and
effort levels in this table; procedures and skills refer to roles.
Execution records still name the actual selection and returned identity.

| Role | Authority | Model and effort | Stand-in |
| --- | --- | --- | --- |
| boss | definition of done, scope, allocation, binding rulings, release decisions | Astra (`gpt-6-astra`), high; xhigh for one named hard question | owner appoints |
| advisor | one bounded hard question; recommendation only; read-only | Fable, xhigh | fresh Astra, xhigh |
| operator | interactive session: briefs, dispatch, receipts, records, merge train, Linux-cell gate; applies rulings | Opus, medium | owner appoints |
| builder | settled approach; routine details | Sol (`gpt-6.1-sol`), medium | boss appoints |
| engineer | settled behavior; method needs judgment | Sol (`gpt-6.1-sol`), xhigh | boss appoints |
| reviewer | independent acceptance against the definition of done: code, tests, evidence provenance; fresh context, read-only | Opus, high | fresh Astra, high |

The implementer is either the builder or the engineer, working in the slice
worktree. They are alternatives, never stages. The advisor is used rarely
and has no acceptance authority.

The operator takes definitions, exceptions, disagreements and release decisions
to the boss and returns the ruling unchanged. The boss may consult the advisor.
The implementer gives the candidate and evidence to the reviewer. The operator
routes `rework` to that implementer, `accepted` to the merge train, and `blocked`
to the boss. Keep one findings queue per change. Only the raiser or a named
stand-in closes a finding.

A ruling that changes a spec, frozen contract, scope, policy or binding, a
release decision, or a destructive or outward action gets one additional
adversarial self-review turn by the deciding seat before the operator acts on it.
Routine dispatch, rework routing and receipts do not. This extra turn is separate
from independent acceptance.

The reviewer owns conformance to the spec. The operator reads the status,
diffstat, implementer's judgment calls and unmet requirements, and hunks named
by a ruling or finding. It neither reads the full diff nor repeats the gates.
Hooks are deterministic checks, not reviews. For an area the owner declares
sealed, log each late finding as one line under Known bugs in `TODO.md`, without
rework.
Escalate a finding that contradicts release acceptance to the owner.

Use the table's stand-in when a seat is unavailable; never substitute silently.
Record the reason and stand-in, including acceptance by the implementer's vendor when that occurs.
A quota fallback requires exhausted quota and work that cannot wait for reset.
If no qualified reviewer is available, acceptance waits; other work continues.
A timeout is not a verdict. Judge bindings by accepted work, escaped defects,
rework and elapsed time.

The reviewer checks the release candidate against the project's definition of
done. The boss makes the release decision; accepted slices alone do not finish
the project. The target for Monday 2026-11-02 is one box: restart, reload and
drain as built and tested. Active/standby on HA Oracle and the 200-job estate
are the next milestone.

Read [required review passes](review.md#required-passes) when allocating a change.
The reviewer owns the detailed round and finding rules.

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

- The operator commits a slice as soon as its gates are green and its required
  review is accepted. Do not wait for permission to commit; only ask before
  pushing to protected branches or merging PRs.
- Push after each logically complete slice rather than batching many commits.
- The pull, staging, delete and command-timeout rules are in the shared
  contract's "Shared work safeguards". Untracked scratch here is often
  estate-derived.

## Workflow rules

1. Keep one open PR: the tip of the merge train. Other branches wait with
   no PR. When the tip merges, fetch, rebase the next branch onto
   `origin/main`, run its gates, open its PR. Announce parallel work before
   it starts and list it in the train order.
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
5. Force-push only your own rebased branch, never main:
   `git push --force-with-lease --force-if-includes origin BRANCH`. If only
   the forced update is blocked, push as `BRANCH-v2`, close the old PR, open
   its replacement.
6. Remove worktrees with `wt remove --discard-ignored`. The deletes that need
   the owner's yes are in the shared contract's "Shared work safeguards".
7. Resolve a conflict and inspect the files. In a later command, run
   `git diff --check` and, for Markdown, `uv run pytest -q
   tests/test_docs_hygiene.py`. Stage explicit paths after both pass. Run
   `git diff --cached --check` before `--continue`.
8. Read the failed CI step. Rerun once for a diagnosed transient cause:
   `gh run rerun ID --failed`. Investigate everything else. Split a test
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
exact artifact, allowed paths, side effects, required gates, and a short report limit.
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

Treat tool-output filters such as RTK as optional local tooling.
Check exit codes, warnings, collection counts, and skipped tests against raw output.
Use raw output for file-by-file hygiene review and whenever a filter hides evidence.
Command-output byte savings are not whole-session token or cost savings.

Revisit the setup after a repeated failure, a model/harness upgrade, or a failed
loading check. Record evidence before adding another standing instruction.
