# Agent setup and work

This repo shares `CLAUDE.md` through the `AGENTS.md` symlink.
Keep one body for each instruction or skill.
Do not replace task-specific references with eager imports.

## Setup

Use a trusted checkout and the locked development environment:

```sh
uv sync --frozen --extra dev
```

CI installs with `--locked`, which also checks the lock against `pyproject.toml`;
a stale lock passes `--frozen` locally and fails in CI.

Project model defaults live in `.codex/config.toml` and
`.claude/settings.json`. A Claude boss runs at the Claude defaults: Opus at
`high` since DL-262. A Codex boss runs at the Codex setting.
Codex loads project configuration only in trusted projects.
Check effective model and effort after a CLI or model upgrade.
Launch flags, environment, user settings, and managed settings may affect them.
Record model and effort selections, plus returned identity when exposed.
If identity is not exposed, record that gap; selection is not server verification.
Do not silently substitute another model if the requested one is unavailable.

No pre-push guard is shipped by this repository.
Git hooks do not travel with a clone.
Verify any locally required guard and its source before relying on it.
Public-repo hygiene still requires reading changed files line by line.
Never use a guard's absence or a clean secret scan as evidence that estate-derived
material is safe to publish.

## Verify a change

The implementer owns the checks and records their commands and results.
The boss reviews that evidence and the diff.
Repeat a check after a relevant edit, a failure, or inadequate evidence.

Before marking an implementation complete, run the full local gates:

```sh
uv run ruff check src tests examples
uv run ruff format --check src tests scripts examples
uv run mypy src tests/uc_oracle.py
uv run python scripts/arch_check.py
uv run coverage erase
uv run coverage run -m pytest -q
uv run coverage combine
uv run coverage report
uv run python scripts/branch_coverage.py
uv run python scripts/transition_coverage.py
```

The mypy line names `tests/uc_oracle.py` beside `src`: it is a fully
annotated executable spec under `tests/`, and nothing else holds it to the
type gate (DL-193).

The erase comes first because parallel data files from an earlier run would
merge into the next combine and could hide a branch the last run missed.
The coverage invocation runs the full suite once.
It also measures every Python subprocess the suite starts, the supervisor and
the wrapper included (DL-265). Each process writes its own data file, so
`coverage combine` must run before `coverage report`. A process that a test
ends with SIGKILL loses its data.
The coverage report enforces the scoped branch-coverage requirement.
The branch report prints branch-only numbers for every module of `src/dsl41`,
most missed branches first, then the package total. It is a report, not a gate.
The transition gate reads `.transition-hits.json`, which the test run writes at
the repository root. It exits 1 when a declared transition without a mark has
no recorded hit, when a violation was recorded, or when a marked transition was hit
(the mark is stale). It must run after the pytest step of the same session.
Keep this list aligned with `.github/workflows/ci.yml`. The one CI check not in
it is `systemd-analyze verify` over `examples/nightbank/deploy/*.service`
(DL-218); it needs Linux.
The workflow examples lane is opt-in and needs a container engine (DL-236):
`WORKFLOW_CONTEXT=<context> uv run pytest -q -o faulthandler_timeout=0 examples/workflows/tests`.
`scripts/arch_check.py --spec-status` prints every specification with its spec
review status (DL-225); it is advisory, and the `spec-review` skill runs one pass
per document.
CI measures coverage in a separate `coverage` job on Python 3.14 only
(DL-211), and that job is also the suite's only 3.14 leg. `sys.monitoring`
makes branch measurement nearly free there; the same measurement costs 2-3x
on the C tracer every other supported version uses.
For browser changes, also follow its browser job: collect the opt-in tests,
verify that they ran, and exercise the interactions in Chromium, WebKit,
and Firefox. A skipped suite is not validation.

## Peer review

Use the allocation policy in `CLAUDE.md`.

Keep scratch files outside the checkout when they may contain private context.
The brief names the task, evidence paths, exact artifact, allowed paths,
side effects, and a short report limit.
Keep routine peer replies under 500 words unless a blocker needs more.
Exchange issue identifiers and changed conclusions rather than repeating the plan.
Group findings by root cause, not by each symptom.
The boss reads the diff against the contract and resolves disagreements with evidence.

Agree on material decisions and the exact artifact version, recorded by commit
or file hash. Continue on open issues while a useful check or new evidence exists.
If discussion repeats, obtain the missing fact or identify the user tradeoff.
Silence, a failed invocation, and a timeout are not agreement.
Agreement complements tests; it does not replace them.

For a read-only Claude peer, put the complete brief in a scratch file:

```sh
claude --print --effort xhigh --permission-mode manual \
  --tools 'Read,Glob,Grep' --strict-mcp-config \
  --no-session-persistence --output-format json < brief.md > review.json
```

Run from the repository root and verify the returned model.
Use the configured model explicitly when launching from another directory: a
probe of the installed CLI from a nested directory selected a different model,
and the cause is not established (the harness review of 2026-09-07, now in
the git history before DL-230).
Do not infer settings loading from memory loading.
The restricted tool list excludes skill invocation; add `Skill` only when needed.
If the brief names evidence outside the checkout, grant that specific directory
with `--add-dir`; permission denials are not review findings.
If reusing a peer session matters, omit `--no-session-persistence` at creation
and resume the returned session ID for rework.
Keep raw logs in scratch; pass only the verdict and relevant evidence to the boss.

For a read-only Codex peer:

```sh
codex exec --sandbox read-only --ephemeral --json \
  -c model_reasoning_effort='"xhigh"' - < brief.md > review.jsonl
```

Run from the repository. The JSON output does not identify the served model;
record the selection and that gap, as Setup says.
A Codex pass costs about as much as a Claude reviewer.
Brief it for targeted probes of the change under review.
It runs no full test suite unless the brief asks.
It calls no other model.
Use `xhigh` for a first review round and `high` for a round that confirms
fixes (DL-262).
Use an ordinary persistent session if later resumption is needed.
Keep existing permission controls. Do not bypass them to make a peer run succeed.

Run commands in the foreground.
Use a sufficient timeout when the tool exposes one.
If it returns a running session or cell, poll through completion before ending
the agent's turn. Do not depend on a background completion notification.

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
