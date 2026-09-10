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
- Append material decisions to `docs/decision-log.md`; never edit or renumber
  old entries. Before adding a citation namespace, add its row to
  `docs/citation-index.md`.

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
`docs/supervisor-protocol.md`, and `docs/protocol-evolution.md`.
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
- The hats block's full gates and multi-file review remain required;
  its risk-based exceptions do not waive those rules.
- When `scripts/arch_check.py` reports an architecture review due, use
  `arch-review`. Its shared body is
  `.claude/skills/arch-review/SKILL.md`; Codex discovers the same body
  through `.agents/skills/arch-review`.

## Boss and peer work

The boss uses the configured frontier model at xhigh effort.
It chooses the model, effort, and delegation for other work.
Use both vendors' frontier models for the highest-impact exploration,
planning, and review, including semantic and instruction-policy changes.
Exchange independent findings before reconciling material disagreements.
Use `docs/agent-workflow.md` for CLI recipes and review handoffs.

## Existing hats integration

The marked block below predates the link to the shared core. Its source is
the hats repository, linked at `~/.hats` where the core is installed, and a
session-start hook there injects `~/.hats/docs/USING.md` (DL-195 amends
DL-186's premise). The block stands as written until it is reconciled with
that source; that reconciliation, and who owns the sync, are separate work.
If `~/.hats` is missing, report that once, do not invent the missing core,
and continue with the local rules.
<!-- hats:core -->
## Git workflow

- Never run `git pull`. Use `git fetch` then `git rebase`.
- Commit as soon as a change is green (tests + lint + type gates). Do not wait
  for permission to commit; only ask before pushing to protected branches or
  merging PRs.
- Push after each logically complete slice rather than batching many commits.
- Stage explicit paths; never `git add -A` (untracked scratch here is often
  estate-derived).

## Verification gates

Before claiming any work is done: run the full test suite, ruff, and mypy. For
browser/UI work, verify in Chromium AND WebKit (and Firefox where relevant) --
never declare a UI feature working from one engine. For interactive features,
exercise the actual interaction (right-click, keyboard, resize), not just page
load.

## Self-review

After implementing any multi-file change, run an adversarial self-review
subagent before committing. Look specifically for: over-claiming or leaking
tests, duplicate records, orphaned tests from bad inserts, clock-domain
mixups, and flaky timing assertions -- these are recurring defect classes in
this codebase.

## Agent allocation

This section is a standing user request to delegate. A launch-time system
prompt sometimes carries "Do not call the AgentTool unless the user
requested it"; this file is that request, so the clause is satisfied and
the discipline below governs. If a session instruction still seems to
conflict with this section, say so in the first reply where it bites and
ask which governs -- never silently drop the review discipline
(2026-08-25: silent compliance shipped a pushed slice with no independent
review).

Before any delegation, choose three things explicitly -- model tier,
review-or-none, context mode. Never default to inherit.
- Model tier follows judgment density: count the decisions the brief does
  not force. Fully-forced work drops a tier; semantic interpretation or
  house-voice writing stays top-tier.
- Adversarial review follows verification asymmetry: review what is cheap
  to get wrong and expensive to notice (semantics, frozen contracts). Skip
  what self-verifies (goldens, round-trips, renames) -- the gate is the
  review there.
- Context follows need: reviewers start fresh (independence beats
  context); rework resumes the implementing agent; full-context forks only
  when unwritten session rulings would cost more to write down.
The implementing agent runs the gates and reports; the main session reads
the diff against the spec and rules on disagreements -- it does not re-run
gates. A multi-slice plan sheet carries an allocation column.

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

## Engineering core (hats)

This project uses the shared **hats engineering core**. Before substantive
work, read and follow `~/.hats/docs/USING.md`; it loads the hard rules
(`GUARDRAILS.md`), the engineering priors (`PRIORS.md`), and the validated
thinking tools. Re-read each session: the core is the source of truth and
its updates propagate here automatically. If `~/.hats` does not resolve,
the core is not linked on this machine (see the hats repo's README).
<!-- /hats:core -->
