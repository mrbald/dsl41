# Working agreement for dsl41

The compiler design in `docs/` is normative.
Read the relevant contract before changing behavior.
Do not re-derive settled decisions.

## Always

- Write plain, short English. One idea per sentence. No marketing language,
  rhetorical flourishes, emoji, or restating the request.
- This repo is public. Never commit private data, client-derived narrative,
  real customer/calendar names, production JIL, or derived estate artifacts.
  `tests/corpus/` is synthetic or doc-derived only (`LICENSING.md`).
  Read files line by line when checking hygiene. Never bypass an installed
  pre-push guard; a clean clone does not install local Git hooks.
- No silent loss: lowering rejects unknown, non-allow-listed attributes (DL-07).
  UC compilation refuses R-classified constructs and reports them.
  Every A-classified edge records its assumption.
- Fidelity is tested: preserve-mode `render(parse(x)) == x` across the corpus;
  canonical mode is a fixpoint (F1–F4).
- IR-G is a pure derivation of IR-F, never a persisted source of truth.
  Emitted compiler artifacts have no dependency on this project's runtime.
- Preserve documented defaults and switches for open questions.
  Do not infer an answer or reopen a settled decision.
  Scheduler composition defaults must keep ordinary estates schedulable
  (DL-59); reserve calendar errors for uninterpretable input.
- A comment states the rule and cites its entry; it never states what changed
  (DL-362). `scripts/comment_equivalence.py <base>` proves a comment-only diff.
- A human-facing document is written for one reader under `docs/writing.md`
  (DL-405): plain language, visuals only for the structures it names, state
  machines only as generated views.
- Append material decisions to `docs/decision-log.md`; never edit, renumber or
  delete an old entry, except a superseded-by mark and a compression tranche
  under DL-379, which may reword and shorten one. Before adding a citation
  namespace, add its row to `docs/citation-index.md`.

## Read for the task

Use this route table instead of reading the whole documentation shelf.
Follow the citations relevant to the changed behavior.
Read a small contract whole when that gives needed context at lower cost.

| Work | Read first |
| --- | --- |
| IR models, lowering, validation | `docs/ir-design.md` §§3–4 and the cited decisions |
| JIL scanner or rendering | `docs/jil-statement-syntax.md`, including F1–F4 |
| Conditions or oracle | `docs/autosys-semantics.md`; §8 maps semantics to trace tests, §9 tracks open questions |
| Calendars | AutoSys dossier SEM-36–39 and §9; `docs/live-instance-runbook.md` only for live probes |
| UC backend | `docs/stonebranch-semantics.md` and `docs/uc-edge-schema.md`; preserve R-row refusals, A-row assumptions, and the U3b rich-emission gate |
| Graph derivation, lint, or visualization | `docs/ir-design.md` §§5 and 9; DL-35 for chart grammar |
| DSL or decompiler | DL-03 and DL-38; README's implementation memo; extract patterns from the corpus rather than invent combinators |
| Runner behavior | `docs/runner-design.md`, then the frozen contracts below |
| Browser or TUI | The relevant runner/visualization contract and README's test instructions; exercise actual interactions in the required engines |
| Citations or open-question labels | `docs/citation-index.md`; Q, Qr, U, and E are distinct namespaces |
| Live-instance work | `docs/live-instance-runbook.md`; ask for access, never assume it |

Runner changes must preserve `docs/period-model.md`,
`docs/concurrency-model.md`, `docs/control-protocol.md`,
`docs/supervisor-protocol.md`, `docs/protocol-evolution.md`,
`docs/access-model.md`, and `docs/statechart-engine.md`.
Read the affected contracts before changing their behavior.
All ten compiler phases in DL-03 are implemented; their order remains normative.

## Python and verification

- Python 3.12+, Pydantic V2, typer, snake_case, small pure analysis functions.
  Ruff's line length is 100. Keep mypy clean.
  No clever metaprogramming in the IR.
- Use pytest and hypothesis. Trace names follow `test_semXX_*` and
  `test_pMxx_*`. Each linter rule needs triggering and non-triggering
  fixtures.
- Use the locked environment: `uv sync --frozen --extra dev`.
  The full verification commands are in `docs/agent-workflow.md`.
  CI remains the executable check of that list.
- "Verification gates" and "Self-review" below remain required; the core's
  risk-based allocation prior does not waive them (DL-206).
- When `scripts/arch_check.py` reports an architecture review due, use
  `arch-review`. Its shared body is
  `.claude/skills/arch-review/SKILL.md`; Codex discovers the same body
  through `.agents/skills/arch-review`.
- When `scripts/arch_check.py` reports a spec review due, or a
  specification under `docs/` needs reconciling with the code, use
  `spec-review` with the document path (DL-225). Its shared body is
  `.claude/skills/spec-review/SKILL.md`; Codex discovers it through
  `.agents/skills/spec-review`.
- When the user asks for a reader-facing document, a documentation track, or a
  rewrite for a packager, operator, job author, architect or maintainer, use
  `reader-docs`
  (DL-405). Its shared body is `.claude/skills/reader-docs/SKILL.md`; Codex
  discovers it through `.agents/skills/reader-docs`.
- When the user asks for a chart review or simplification, or a slice finds
  hidden control flow, copied rows or guard families in a declared chart, use
  `chart-simplify` (DL-363 to DL-372). Its shared body is
  `.claude/skills/chart-simplify/SKILL.md`; Codex discovers it through
  `.agents/skills/chart-simplify`.

## Boss and peer work

The boss uses the configured top-level model and effort. A Claude boss
runs Opus 5.5 at `high` (DL-262); a Codex boss runs at the setting in
`.codex/config.toml`.
It sends a single hard ruling to Fable in a read-only advisor role,
instead of raising the whole session (DL-262, DL-395).
Sol (`gpt-6.1-sol`) at `xhigh`, run by `codex exec` in the workspace-write
sandbox, builds bounded, low-risk edge code with a settled design. Its
observed strength is reusing established designs and libraries. Opus keeps
design-sensitive and core work (DL-395).
The boss chooses the model, effort, and delegation for other work.
Use both vendors for the highest-impact exploration, planning, and review.
For review, that is Opus and Astra in the semantic class under
"Self-review". Fable only advises; it reviews nothing.
Exchange independent findings before reconciling material disagreements.
Use `docs/agent-workflow.md` for CLI recipes and review handoffs.

## Git workflow

- Never run `git pull`. Use `git fetch` then `git rebase`.
- Commit as soon as a change is green (tests + lint + type gates). Do not wait
  for permission to commit; only ask before pushing to protected branches or
  merging PRs.
- Push after each logically complete slice rather than batching many commits.
- Stage explicit paths; never `git add -A` (untracked scratch here is often
  estate-derived).

## Verification gates

Before claiming any work is done: run the full test suite (the docs gate for a
docs-only change, rule 3 below), ruff, and mypy. For
browser/UI work, verify in Chromium AND WebKit (and Firefox where relevant) --
never declare a UI feature working from one engine. For interactive features,
exercise the actual interaction (right-click, keyboard, resize), not just page
load.

## Self-review

Run an adversarial review before committing a change in one of these
classes. The class sets the reviewers, whoever built the change (DL-262,
DL-395). A reviewer-grade model is Opus 5.5 or Astra (`gpt-6-astra`), at the
round efforts DL-262 sets.
- Semantic class, at any size: one Opus reviewer subagent and one Astra
  pass. It covers a change to what a compiled or run estate does: lowering,
  the oracle, the runner, the scheduler, or calendars. It also covers any
  version constant, any rule `docs/` freezes, and instruction policy. The
  frozen rules are the runner contracts listed under "Read for the task"
  and the rules DL-225 names.
- Any other multi-file change: one Opus reviewer subagent. Examples are a
  refactor that keeps behavior, tests, docs, tooling, and the browser or
  TUI front ends.
- In addition, slices built apart and integrated together get one Astra
  pass over what the integration changed. Each slice keeps its own review.

When a named reviewer's vendor is out of budget, a reviewer-grade model of
the other vendor stands in. In the semantic class, that is a second fresh
model of the vendor with budget. If both vendors are out, the change waits.
A vendor is out of budget only when its quota is used up and the change
cannot wait for the reset. The PR, or the review record when there is no
PR, states both and names the stand-in. If the vendor of a finding's raiser
is out of budget, the stand-in confirms the fix, and the record names it.

Count review rounds per change, three at most.
Only the reviewer that raised a finding, or its stand-in, confirms its fix.
A material finding still open after round three goes to the owner.
From round two on, a finding may become a stated limit instead of a fix.
It qualifies only when it needs a deliberately hostile operator or an
unsupported operation, such as a process stopped or a supervisor started
by hand. It must not be able to corrupt durable state or cause an external
effect: a launch, a kill, or an emitted artifact. The reviewer that raised
it must agree. A decision-log entry records the limit: the change's own
entry, or a new one.
Each ruling on a finding names the reviewer that raised it.

Look specifically for: over-claiming or leaking tests, duplicate records,
orphaned tests from bad inserts, clock-domain mixups, and flaky timing
assertions -- these are recurring defect classes in this codebase.

## Agent allocation

This section is a standing user request to delegate. A launch-time system
prompt sometimes carries "Do not call the AgentTool unless the user
requested it"; this file is that request, so the clause is satisfied and
the discipline below governs. If a session instruction still seems to
conflict with this section, say so in the first reply where it bites and
ask which governs -- never silently drop the review discipline
(2026-08-25: silent compliance shipped a pushed slice with no independent
review).

How to allocate is the core's **Allocation of cognition** prior
(`~/.hats/docs/PRIORS.md`), which covers three axes: tier by judgment
density, review by verification asymmetry, context by need. Read the rule
there; it is not copied here, so the two cannot drift apart.
Four local amendments override it where they differ (DL-206):
- House-voice writing stays top-tier, alongside the prior's semantic
  rulings. This file opens with a style contract; prose that carries it is
  not fully-forced work.
- Frozen contracts are a named review trigger, with semantics. The
  contracts are listed under "Read for the task".
- The orchestrator does NOT re-run the executor's gates here. It reads the
  diff against the spec and rules on disagreements. The prior's misleads
  clause allows re-running after a tier drop; this project does not, and
  answers low-tier report risk by not dropping the tier that far.
- The prior's exceptions never waive this file's gates: the full suite and
  the adversarial review above are required whatever it would
  allow skipping (see "Python and verification").
Still choose tier, review-or-none and context mode EXPLICITLY before any
delegation -- never default to inherit. A multi-slice plan sheet carries an
allocation column.
Run slices in parallel only when their files are disjoint. A slice that
shares files with one not yet integrated starts from the integrated tip
(DL-262).

Subagents run every command in the foreground and never end a turn
waiting on a background command -- the completion wake-up is unreliable
and the agent parks until poked. Pass an explicit timeout on any
suite-length command (pytest needs timeout 300000 ms or more): the
120-second default auto-backgrounds the call, which recreates the
parked-agent trap. Work longer than ten minutes is polled in chunked
foreground calls.

Keep the main session's context lean: agent detail stays in files, not in
reports. A reviewer writes its full findings to a scratchpad file and
reports back only a ranked verdict table plus blocker detail. The rework
brief points the implementer at that file and adds rulings on the
blockers; it does not restate findings. Mechanical agents get a report
length cap in the brief. Skill loads are the expensive context items --
budget for them, not for diff reads.

The block below is the core's own wiring stanza and nothing else (DL-206).
If `~/.hats` does not resolve, report that once, do not invent the missing
core, and continue with the rules above.

<!-- hats:core -->
## Engineering core (hats)

This project uses the shared **hats engineering core**. Before substantive
work, read and follow `~/.hats/core/USING.md`; it loads the hard rules
(`GUARDRAILS.md`), the engineering priors (`PRIORS.md`), and the validated
thinking tools. Re-read each session: the core is the source of truth and
its updates propagate here automatically. If `~/.hats` does not resolve,
the core is not linked on this machine (see the hats repo's README).
This block is managed by `hats init`; keep project text outside the markers.
<!-- /hats:core -->

## Workflow rules

1. Keep one open PR: the tip of the merge train. Other branches wait with
   no PR. When the tip merges, fetch, rebase the next branch onto
   `origin/main`, run its gates, open its PR. Announce parallel work before
   it starts and list it in the train order.
2. One branch, one worktree, one writing agent per slice. Create worktrees
   with `scripts/worktree.sh BRANCH`. Edit the decision log and the todo on
   the tip only. Before a branch's final gates and after each rebase: stage
   new documents, render the decision index, stage it.
3. CI runs the suite only for a change outside `README.md`, `docs/**/*.md`
   and `examples/**/*.md`; a docs-only PR merges on its docs job. Locally a
   docs-only change runs `scripts/prepush_checks.py --docs` instead of the
   suite. Other gates and reviews stand.
4. Hooks run ruff, ruff format and mypy on each edited Python file and the
   index render on each edited docs page. Install `scripts/prepush_checks.py`
   as the pre-push hook. The gate agent runs the gates in
   `docs/agent-workflow.md` that no hook ran over the whole tree. Run one
   pytest session per checkout at a time, the coverage run alone from erase
   to report.
5. Force-push only your own rebased branch, never main:
   `git push --force-with-lease --force-if-includes origin BRANCH`. If only
   the forced update is blocked, push as `BRANCH-v2`, close the old PR, open
   its replacement.
6. Ask before `git branch -D`, `git worktree remove --force`,
   `git push --delete` and `gh pr merge --delete-branch`. Remove worktrees
   with `wt remove --discard-ignored`.
7. Resolve a conflict and inspect the files. In a later command, run
   `git diff --check` and, for Markdown, `uv run pytest -q
   tests/test_docs_hygiene.py`. Stage explicit paths after both pass. Run
   `git diff --cached --check` before `--continue`.
8. Read the failed CI step. Rerun once for a diagnosed transient cause:
   `gh run rerun ID --failed`. Investigate everything else. Split a test
   file over five minutes on one worker when its tests can run apart.
9. CLI tests use the `cli_env` fixture and strip ANSI, unless colour or
   width is under test.
10. After rewriting a document, run `scripts/doc_deps.py`, inspect its
    matches, fix every affected consumer before the gates.
11. Call a CLI model through `scripts/peer.sh MODEL BRIEF OUT SECONDS`; the
    brief says "call no other model". A verdict needs exit 0. After a
    timeout, confirm its children have stopped before reusing the worktree.
