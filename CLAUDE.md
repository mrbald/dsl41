# Working agreement for dsl41

The compiler design in `docs/` is normative.
Read the relevant contract before changing behavior.
Do not re-derive settled decisions.

## Read for your role

Every dispatch brief starts with `Role: <role>` from the table below.
Use the assigned role, not the model name. An interactive session without an
assigned role starts as operator. The owner or boss may assign a different role.
Read this shared file, your role guide, and the contracts needed for the task.
Other role guides are loaded only when the task needs them.

| Role | Read next |
| --- | --- |
| operator or boss | [Coordination](docs/agents/coordination.md) |
| builder or engineer | [Implementation and verification](docs/agent-workflow.md) |
| reviewer | [Independent review](docs/agents/review.md) |
| advisor | [Bounded advice](docs/agents/advice.md) |

The coordination guide owns the role-to-model table and the standing delegation
policy. Builders, engineers, reviewers and advisors do not dispatch other agents
unless their brief assigns that duty. Role routing does not waive task contracts,
required verification or independent acceptance.

## Always

- Write plain, short English. One idea per sentence. No marketing language,
  rhetorical flourishes, emoji, or restating the request.
- This repo is public. Never commit private data, client-derived narrative,
  real customer/calendar names, production JIL, or derived estate artifacts.
  `tests/corpus/` is synthetic or doc-derived only (`LICENSING.md`).
  Read files line by line when checking hygiene. Never bypass an installed
  pre-push guard; a clean clone does not install local Git hooks.
- No silent loss: lowering rejects unknown, non-allow-listed attributes (DL-07).
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
| Calendars | `docs/autosys-semantics.md` SEM-36–39 and §9; `docs/live-instance-runbook.md` only for live probes |
| Graph derivation, lint, or visualization | `docs/ir-design.md` §§5 and 9; DL-35 for chart grammar |
| DSL or decompiler | DL-03 and DL-38; README's implementation memo; extract patterns from the corpus rather than invent combinators |
| Runner behavior | `docs/runner-design.md`, then the frozen contracts below |
| Browser or TUI | The relevant runner/visualization contract and README's test instructions; exercise actual interactions in the required engines |
| Citations or open-question labels | `docs/citation-index.md`; Q, Qr, U, and E are distinct namespaces |
| Live-instance work | `docs/live-instance-runbook.md`; ask for access, never assume it |

Runner changes must preserve `docs/control-protocol.md`,
`docs/wrapper-protocol.md`, `docs/protocol-evolution.md`,
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
- Required gates and independent review cannot be waived by the core's
  allocation prior (DL-206). Follow the duties in your role guide.
- CLI tests use `cli_env` and strip ANSI unless colour or width is under test.

Report a triggered review to the operator unless your brief assigns it to you.
The operator coordinates skill steps that require delegation; other roles
execute their assigned steps without taking over orchestration.
Skill bodies live at `.claude/skills/<name>/SKILL.md`.
Codex uses the same bodies through `.agents/skills/<name>` symlinks.

| Trigger | Skill |
| --- | --- |
| `scripts/arch_check.py` reports architecture review due | `arch-review` |
| Spec review due, or a specification needs reconciling with code | `spec-review` with the document path (DL-225) |
| Reader-facing document, documentation track, or rewrite for a named reader | `reader-docs` (DL-405) |
| Chart review or simplification; hidden control flow, copied rows or guard families | `chart-simplify` (DL-363 to DL-372) |

## Shared work safeguards

- Honor the task's allowed paths, read-only limits and approval boundaries.
- Never run `git pull`. Use `git fetch` then `git rebase`.
- Stage explicit paths; never `git add -A`.
- A commit requires green gates and accepted review where review is required.
  Publication follows the coordination guide and the owner's instructions.
- Ask before `git branch -D`, `git worktree remove --force`, `git push --delete`,
  or `gh pr merge --delete-branch`. Never force-push main.
- Use foreground commands and sufficient timeouts where supported. If a tool
  returns a running session or cell, poll to completion before ending the turn.
  Do not rely on background completion notifications.

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
