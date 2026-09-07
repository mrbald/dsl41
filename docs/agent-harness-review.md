# Agent harness review

Reviewed on 2026-09-07–08 against Codex CLI 0.153.4 and Claude Code 2.1.263.
This is a dated decision record, not an instruction file to load each session.
The operating procedure is [agent-workflow.md](agent-workflow.md).

## Scope and decisions

- Keep one root body: `AGENTS.md` remains a symlink to `CLAUDE.md`.
- Keep compiler invariants always visible. Route other reading by task.
  Remove the copied open-question history and completed phase descriptions.
  Their normative homes remain the dossiers, design, citation index, and decision log.
- Keep one architecture skill body. Add Codex discovery by symlink.
  Record an architecture review only after acceptance, on the reviewed commit.
  Annotated timestamped tags allow multiple reviews on one day.
- Put model defaults in each provider's native project configuration.
  The boss chooses other allocations. Use fresh cross-vendor peers for
  high-impact decisions, then reconcile against evidence and the exact artifact.
- Keep occasional CLI recipes and the local gate list in the workflow document.
  CI remains executable authority. Do not make another layer for a short document.
- Preserve the hats-marked block byte for byte until its source is located.
  State the review-conflict ruling outside it: full gates and multi-file review
  remain required. This review does not relax them.

The old root was 14,239 bytes in 246 lines.
The candidate is 8,929 bytes in 168 lines, a 37.3% byte reduction.
These are file sizes, not tokenizer counts or whole-session savings.
The old mandatory reading list covered 1,154,665 bytes in seven documents.
That was potential required reading, not content automatically loaded by either CLI.
Task routes reduce that obligation without making the specifications optional.

## Evidence behind the choices

Vendor documentation defines supported mechanics, not measured quality gains.
Installed behavior still needs a probe.

| Source | Relevant guidance | Decision here |
| --- | --- | --- |
| [OpenAI instruction discovery](https://learn.chatgpt.com/docs/agent-configuration/agents-md) | Scoped instruction files and a default combined project limit of 32 KiB. | Keep one shared root, below the limit; inspect actual loading. |
| [OpenAI skills](https://learn.chatgpt.com/docs/build-skills) | Skill bodies load on demand; repository discovery uses `.agents/skills` and supports symlinks. | Link the existing skill instead of copying it. |
| [OpenAI configuration](https://learn.chatgpt.com/docs/config-file/config-basic) | Trusted project configuration overrides user defaults; launch overrides take precedence. | Use native project defaults and verify effective launches. |
| [Anthropic memory](https://code.claude.com/docs/en/memory) | Imports load eagerly; scoped files and skills can defer reading. | Do not disguise eager imports as progressive disclosure. |
| [Anthropic model configuration](https://code.claude.com/docs/en/model-config) | Model and effort have distinct settings and precedence. | Record the selected model; pass effort explicitly for peer calls. |
| [Anthropic best practices](https://code.claude.com/docs/en/best-practices) and [context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) | Give observable acceptance criteria, retrieve relevant context, and separate review context when useful. | Keep handoffs short and verification evidence inspectable. |

Published results disagree in useful ways. None establishes the best instruction
length or the benefit of Astra/Fable agreement for this repository.
Cross-vendor agreement is the chosen working policy, not a measured guarantee.

| Primary study or published evaluation | Reported result | Limit on transfer |
| --- | --- | --- |
| [Lulla et al.](https://arxiv.org/html/2601.20404v1), 124 small PR tasks in ten repositories, GPT-5.2-Codex | Median runtime fell 28.64%; output tokens fell 16.58% with agent instructions. | An efficiency result on small tasks, not proof of unchanged correctness here. |
| [Gloaguen et al.](https://arxiv.org/html/2602.11988v1), SWE-bench Lite and AGENTbench | Generated context increased costs about 20–23% and lowered resolution on average. Human files had mixed gains over no context. | Avoid unnecessary generated summaries, not all instructions. |
| [Khatri](https://arxiv.org/html/2607.27250v1), 17 tasks, three Python repositories, 288 runs | No measurable correctness gain from the tested context strategies. | Small task sample, broad equivalence bounds, and different injection channels; not proof of zero benefit. |
| [Vercel's Next.js 16 evaluation](https://vercel.com/blog/agents-md-outperforms-skills-in-our-agent-evals) | A compressed 8 KB index scored 100%; prompted skill use 79%; baseline and unprompted skill use 53%. | A narrow publisher-run evaluation. It supports testing visible task routes, not abandoning skills. |
| [Anthropic's multi-agent research evaluation](https://www.anthropic.com/engineering/multi-agent-research-system) | Performance improved 90.2% over a single Opus 4; multi-agent use consumed about 15 times chat tokens. | Research tasks, not this compiler. The token denominator is chat, not a single coding agent. |

[RTK's savings explanation](https://raw.githubusercontent.com/rtk-ai/rtk/develop/docs/guide/resources/savings-explained.md)
measures filtered command output, not full-session cost.
Do not turn output-byte reductions into a claim about total token savings.
Preserve raw exit status, collection counts, warnings, skips, and diagnostic text.

## Local verification

### Instruction loading

Eight successful calls tested a synthetic root, nested instructions, one eager
import, and one advertised skill. Sentinel values were absent from the prompt.
No call used tools. The first four mixed layout and working-directory changes.
The last four held the candidate layout fixed and varied only the launch directory
within each vendor. These are observations on the installed versions, not a
general claim about every launch surface.

| Controlled call | Root | Nested | Import | Skill advertised | Model evidence |
| --- | --- | --- | --- | --- | --- |
| Codex, root | yes | no | no | yes | JSON stream did not identify model or effort |
| Codex, nested | yes | yes | no | yes | JSON stream did not identify model or effort |
| Claude, root | yes | no | yes | yes | Fable from the project default |
| Claude, nested | yes | yes | no | yes | Opus, not the requested project default |

The early Claude calls excluded the Skill tool. Their missing skill report was
inconclusive, not a discovery failure. With the normal catalog, Claude advertised
the skill at both launch depths. Codex advertised the linked skill body.
Codex followed the `AGENTS.md` symlinks and did not expand Claude's import syntax.

The scratch project was not persisted as trusted. Its Codex model defaults
matched the user's defaults, so these probes do not establish project-config
precedence. The native keys are documented; effective model and effort still
need verification at launch. No global trust or model settings were changed.
Claude's nested result shows that loaded instructions do not prove loaded settings.
Its cause was not established. Launch peers from the root or select the model
explicitly. The real-checkout policy review returned Fable from its root default.

Claude reported $0.481654 in list-price cost for the eight loading probes'
Claude calls. This is not an invoice. Codex usage was retained without a credit
or currency conversion. Two earlier CLI attempts failed before inference;
neither counts as a successful loading observation.

### Routing pilot

Prompt plus transcript audit, not blind.

The fixed matrix has two cases, two root bodies, and two vendors: eight calls,
one observation per cell. Case A repairs metadata loss in a small CMD-only
lowering adapter and must produce a JSON artifact. Case B reviews an M16
classification change and a skipped regression with an unsupported validation
claim. These are synthetic adapters, not the production compiler.

Only the root body differs within a pair. Both arms receive the same public
contracts from the baseline commit, tools, evidence, prompts, and shared workflow
support. The new harness decision and this report are excluded from the inputs.
Each call starts fresh. Call order is counterbalanced, with two calls at a time.
Astra and Fable are selected explicitly at xhigh. Codex selection is recorded
from arguments because its JSON stream does not attest the returned model.
The baseline commit is `118fbe0b620ad7cfae88fbd407e3f7d9b9ee9751`.
The tested candidate root has SHA-256
`a5a1c157d5f300f5c06c7dbe2342de112af135cc9c3b3fdd587b4d7b7abbe9d1`.

The rubrics were fixed before model calls. Case A has fourteen behavior checks,
plus relevant regressions, contract consultation, and truthful verification.
Several quote-retention checks share one possible contract misread; they are
not independent defects. Case B scores both seeded defects and review discipline.
The seed fails and the gold repair passes. Restoring only B's skipped regression
exposes its classification error. These controls passed in 2.256 seconds.

A later rubric audit found that preserving IR metadata key case is production
behavior, but is not explicit in the supplied prose. Scanner rule 8 preserves
AST keys; IR-design specifies trimmed values and allows syntax normalization.
The frozen checker was not changed after calls began. Its key-only mismatches
must be reported, but cannot establish failure to follow the supplied contract.
This limitation escaped the first two-vendor review.

Fable's fixture review caught an editable production install in the first tool
environment. Before any treatment call, it was replaced with a pilot-only
environment. The production package cannot be imported there. Transcripts and
immutable inputs are audited for source, grader, sibling, and unauthorized reads
or edits. Native global instructions remain a recorded limitation. A write sandbox
does not prove read isolation.

The limit is eight treatment calls, without retries after inference. Each has
a ten-minute wall bound; Claude also has a $5 list-price budget per call.
Codex has no verified whole-task dollar cap. Record its native usage categories
instead of inventing a currency conversion. A model mismatch or contamination
invalidates a sample. A timeout, budget limit, permission failure, or stalled
workflow is incomplete, not a semantic failure.

All eight calls completed. Six have clean observed scope; two are quarantined.
No call was retried. Both Fable A arms attempted CLI outputs outside their
workspace. They receive no clean correctness or efficiency comparison score.
The automatic audit also mistook copied output and shell-wrapper text for scope
violations in the Astra A calls. Manual review cleared those false positives;
the original flags and the rulings were retained.

| Case and vendor | Baseline | Candidate |
| --- | --- | --- |
| A, Astra | Strict checker 11/14; other task requirements pass | Strict checker 11/14; other task requirements pass |
| A, Fable | Quarantined: outside-workspace attempt | Quarantined: outside-workspace attempt |
| B, Astra | 9/9; both defects found; do not merge | 9/9; both defects found; do not merge |
| B, Fable | 9/9; both defects found; do not merge | 9/9; both defects found; do not merge |

Both Astra A calls fail the frozen all-pass threshold. Their three mismatches
share the under-specified key-case requirement; values and quotes are retained.
The scoring threshold was not changed. This is not evidence that either root
caused a contract regression. In B, related symptoms count as one defect.
Both Fable reviews split them into extra findings; the workflow now asks peers
to group by root cause. The candidate Fable reply also exceeded its 400-word cap.
That observation does not change the frozen correctness score.

Codex reported these cumulative token categories for the clean pairs:

| Case and arm | Input | Cached input, included in input | Output | Reasoning output, included in output |
| --- | ---: | ---: | ---: | ---: |
| A baseline | 736,179 | 674,688 | 6,329 | 2,070 |
| A candidate | 417,958 | 379,008 | 5,044 | 1,467 |
| B baseline | 389,152 | 336,000 | 2,974 | 1,197 |
| B candidate | 221,925 | 187,008 | 2,957 | 994 |

Candidate cumulative input was about 43% lower in each Astra pair.
These are repeated API-input totals, not unique context size or dollar savings.
The output change differed by task. Fable B list-price cost was $1.080351 for
baseline and $1.060320 for candidate, a small difference in one pair.
All four Fable treatment calls cost $6.079019, including the quarantined calls.
Planning, loading, and pre-pilot Claude reviews cost another $12.198564.
That subtotal excludes final sign-off, Codex, and the parent/native subagents;
it is not a whole-team cost or an invoice.

The launcher's recorded wall times end at the enclosing batch boundary.
They must not be used as individual-call latency or runtime savings.
This small unreplicated pilot cannot establish production equivalence,
reliability rates, universal cost savings, or a ranking of vendors.
It supports the smaller root as a bounded context experiment, not as a
demonstrated correctness improvement. No instruction-only edit removes the
need for source inspection and observable acceptance checks.

### Repository gates

The full suite passed: 3,578 passed, six skipped, and two xfailed in 247.68 seconds.
Ruff check, Ruff format checking, mypy, and the architecture gate passed.
Scoped branch coverage was 100%: 2,390 statements and 842 branches, with no misses.
This is the configured concurrency scope, not a claim about coverage of all code.
Browser checks were not run because no browser behavior changed.

The shared-root and skill symlinks resolve correctly. Native JSON and TOML parse.
The hats block matches the baseline byte for byte. A scratch Git repository
verified same-day annotated tags and the gate's creation-date selection.
No production tag was created. Final document links, citations, and whitespace
checks passed. Compiler source and tests are unchanged.

## Deferred work and triggers

- Hats: locate the source and its sync ownership before editing the marked block.
  Then reconcile the blanket gates with risk-based allocation in that source.
- Pre-push hygiene: no guard source is shipped or installed in this checkout.
  The gitleaks CI workflow is not a local pre-push guard and cannot detect all
  client-derived narrative. Locate any required guard; do not claim it exists.
- Output filtering and ambient tools: no global settings were changed.
  Measure a real failure or full-task overhead before removing tools or hooks.
- Wider evaluation: repeat with real compiler tasks if routing misses a contract,
  a model or CLI changes, or routine results regress. The small synthetic pilot
  cannot establish production equivalence or universal cost savings.
