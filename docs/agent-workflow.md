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
`.claude/settings.json`. `CLAUDE.md` "Boss and peer work" names the boss
model and effort for each vendor.
Codex loads project configuration only in trusted projects.
Check effective model and effort after a CLI or model upgrade.
Launch flags, environment, user settings, and managed settings may affect them.
The boss records the model and effort it selects for itself and for each
agent or peer, plus returned identity when exposed.
If identity is not exposed, record that gap; selection is not server verification.
Do not silently substitute another model if the requested one is unavailable.

The pre-push guard is `scripts/prepush_checks.py`, installed by hand into
`.git/hooks/pre-push`. Git hooks do not travel with a clone.
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
uv run coverage run -m pytest -q -n 4 --dist loadfile
uv run coverage combine
uv run coverage report
uv run python scripts/branch_coverage.py
uv run python scripts/transition_coverage.py
uv run python tests/oracle_goldens.py replay
```

A comment-only or docstring-only change adds one gate before the suite:
`uv run python scripts/comment_equivalence.py <base-rev>` proves the diff
changed no code and lost no citation token (DL-362). It replaces the logic
review for that change, not the suite run.

The mypy line names `tests/uc_oracle.py` beside `src`: it is a fully
annotated executable spec under `tests/`, and nothing else holds it to the
type gate (DL-193).

Every test runs under a leak detector (`tests/leak_detector_plugin.py`,
DL-355). A test that leaves global process state changed fails at teardown,
naming the state before and after. The probes are listed in the plugin: among
them an environment variable, the working directory, sys.path, a signal
handler, a child process not yet reaped and a live non-daemon thread. A restore
fixture puts back a production path's own change and nothing else: the
oracle's recursion limit, the supervisor's subreaper flag and signal handlers,
and the defaults `dsl41 run` leaves on SIGINT, SIGTERM and SIGHUP. The check
compares each test with itself, so the order of the tests and the worker do not
matter. Two limits: a child or thread that ends on its own a moment after the
test is reported only when it is still there at the check, and a module- or
session-scoped fixture is outside the check.

The erase comes first because parallel data files from an earlier run would
merge into the next combine and could hide a branch the last run missed.
The coverage invocation runs the full suite once, in four pytest-xdist
workers that each take whole test files (DL-354). The worker count is pinned
at 4, never `auto`, so the run is the configuration CI measured. The flags live
in the commands, not in `addopts`, so a run of one test stays one process.
It also measures every Python subprocess the suite starts, the supervisor and
the wrapper included (DL-265). Each process writes its own data file, so
`coverage combine` must run before `coverage report`. A process that a test
ends with SIGKILL loses its data.
The coverage report enforces the scoped branch-coverage requirement.
The branch report prints branch-only numbers for every module of `src/dsl41`,
most missed branches first, then the package total. It is a report, not a gate.
The transition gate reads `.transition-hits.json`, which the test run writes at
the repository root. It exits 1 when a required item has no recorded hit: a row
without a mark, a choice branch, each outcome of an unmarked row's or a
branch's guard, the defer or the release of a deferred event, or the implicit
ignore of an event in a final state. A false
outcome declared unreachable is not required. It also exits 1 when a forbidden
item was hit: a false outcome declared unreachable, or a marked row or its
guard's true outcome (the mark is stale or wrong), or when the hits file is not JSON or
has no `hits` key. It exits 2 when the hits file is missing. It must run after the pytest step
of the same session.
Under xdist the controller writes that file from every worker's hits.
The golden replay runs the oracle over the whole harness sample, about a minute,
and names the first run, entry and component that differ from
`tests/oracle_goldens.txt`; CI runs it as its own `goldens` job (DL-351).
Keep this list aligned with `.github/workflows/ci.yml`. CI also runs what this
list does not: the `docs` job, `systemd-analyze verify` over
`examples/nightbank/deploy/*.service` (DL-218, Linux only), the
`explore-page` browser job, and the `audit`, `secrets`, `release`,
`dst-drill` and `service-drill` workflows.
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

Use the allocation policy in `CLAUDE.md`. "Boss and peer work" names the
models and "Self-review" names each change's reviewers.

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
Use the configured model explicitly when launching from another directory:
from a nested directory the installed CLI can select a different model, and
the cause is not established.
Do not infer settings loading from memory loading.
The restricted tool list excludes skill invocation; add `Skill` only when needed.
If the brief names evidence outside the checkout, grant that specific directory
with `--add-dir`; permission denials are not review findings.
If reusing a peer session matters, omit `--no-session-persistence` at creation
and resume the returned session ID for rework.
Keep raw logs in scratch; pass only the verdict and relevant evidence to the boss.

For a read-only Codex peer:

```sh
codex exec --sandbox read-only --ephemeral --json -m gpt-6-astra \
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

For a Sol implementer (DL-395), put the complete brief in a scratch file:

```sh
codex exec --sandbox workspace-write --ephemeral --json -m gpt-6.1-sol \
  -c model_reasoning_effort='"xhigh"' - < brief.md > build.jsonl
```

Run from the checkout or worktree the slice owns.
The brief names the allowed paths and the gates to run, as for a peer.
Drop `--ephemeral` if a rework round will resume the session.
The workspace-write sandbox may block the uv cache and the network, so the
gates may need `--add-dir ~/.cache/uv` or a `UV_CACHE_DIR` inside the
worktree. This is untested.
The JSON output does not identify the served model; record the selection and
that gap, as Setup says.

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
