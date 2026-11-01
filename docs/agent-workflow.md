# Implement and verify a change

For maintainers implementing and verifying a change, including agents assigned
the builder or engineer role. Follow the maintainer track under
[Documents](../README.md#documents).
Read the [shared contract](../CLAUDE.md) and the task's design contracts.
This page leaves document-writing rules to [writing.md](writing.md) and
CLI/TUI evidence limits to [cli-tui-qualification.md](cli-tui-qualification.md).
The builder follows a settled approach. The engineer chooses the method for
settled behavior. Neither role changes scope or policy.

## Your task

- Work in the assigned branch and worktree, within the brief's allowed paths.
  One writer owns each slice. Report missing requirements or needed rulings to
  the operator; do not dispatch a reviewer or another model yourself.
- Implement the brief and run the checks below. Report judgment calls, unmet
  requirements, changed paths, and exact check results to the operator.
- Identify the candidate by commit or file hash. The operator arranges review.
  Address all open findings in each rework round and return them to their raisers.
  Wait for the operator's go-ahead if a round limit or shape note is due.
- Do not commit, stage or push. The operator commits once the gates pass and
  review is accepted or not required, and owns publication and the merge train.
- After rewriting a document, run `uv run python scripts/doc_deps.py`, inspect
  the matches, and fix affected consumers before the gates.
- A new document must be visible to Git before `docs/decision-index.md` renders.
  The brief names each new document; before dispatch the operator creates it
  empty and marks it with `git add -N PATH`. Report any other new document; the operator
  marks it and renders the index again.
- The operator rebases and resolves conflicts.

## Setup

1. Select a trusted checkout in the assigned worktree.
2. Install the locked development environment:

   ```sh
   uv sync --frozen --extra dev
   ```

CI installs with `--locked`, which also checks the lock against `pyproject.toml`;
a stale lock passes `--frozen` locally and fails in CI.

The pre-push guard is `scripts/prepush_checks.py`, installed by hand into
`.git/hooks/pre-push`. Git hooks do not travel with a clone.
Verify any locally required guard and its source before relying on it.
Public-repo hygiene still requires reading changed files line by line.
Never use a guard's absence or a clean secret scan as evidence that estate-derived
material is safe to publish.

## Verify a change

The implementer owns the checks and records their commands and results.
The reviewer checks that evidence and the diff against the contract.
The operator routes disagreements to the boss and does not repeat the gates.
Repeat a check after a relevant edit, a failure, or inadequate evidence.

A docs-only change touches only `README.md`, `CLAUDE.md`, `docs/**/*.md`,
`examples/**/*.md` or `.claude/skills/**/*.md` (DL-434). For that change, run
`uv run python scripts/prepush_checks.py --docs` instead of the suite.
Also run the lint, format and type checks listed below. Review requirements still apply.

For other changes, run the full local gates before claiming completion:

```sh
uv run ruff check src tests examples
uv run ruff format --check src tests scripts examples
uv run mypy
uv run python scripts/arch_check.py
uv run coverage erase
uv run coverage run -m pytest -q -n 4 --dist loadfile
uv run coverage combine
uv run coverage report
uv run python scripts/branch_coverage.py
uv run python scripts/transition_coverage.py
uv run python tests/oracle_goldens.py replay
```

Run one pytest session per checkout at a time. Keep the coverage run alone from
erase through report. For browser/UI work, exercise actual interactions such as
right-click, keyboard and resize in Chromium and WebKit, and Firefox where relevant.

A comment-only or docstring-only change adds one gate before the suite:
`uv run python scripts/comment_equivalence.py <base-rev>` proves the diff
changed no code and lost no citation token (DL-362). It replaces the logic
review for that change, not the suite run.

`uv run mypy` checks `src`, the file set `pyproject.toml` names.

Every test runs under a leak detector (`tests/leak_detector_plugin.py`,
DL-355). A test that leaves global process state changed fails at teardown,
naming the state before and after. The probes are listed in the plugin: among
them an environment variable, the working directory, sys.path, a signal
handler, a child process not yet reaped and a live non-daemon thread.
A restore fixture undoes only a production path's deliberate state change,
so the detector still sees a test's own change (DL-355).
The restore fixtures in `tests/conftest.py` put back the oracle's recursion-limit
raise, the Linux child-subreaper flag, and the defaults `dsl41 run` leaves on
SIGINT, SIGTERM and SIGHUP. Tests must restore any other state they change.
The subreaper fixture remains in the suite; the supervisor runtime is excluded
by DL-424.
The check compares each test with itself, so the order of the tests and the worker do not
matter. Two limits: a child or thread that ends on its own a moment after the
test is reported only when it is still there at the check, and a module- or
session-scoped fixture is outside the check.

The erase comes first because parallel data files from an earlier run would
merge into the next combine and could hide a branch the last run missed.
The coverage invocation runs the full suite once, in four pytest-xdist
workers that each take whole test files (DL-354). The worker count is pinned
at 4, never `auto`, so the run is the configuration CI measured. The flags live
in the commands, not in `addopts`, so a run of one test stays one process.
The subprocess patch in `pyproject.toml` also measures Python child processes,
including engines and wrappers (DL-265). Each measured process writes its own
data file, so
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
Keep this list aligned with `.github/workflows/ci.yml`. Its `docs` job runs
the docs gate on every change; `explore-page` runs the explore-page browser
tests and the autorep viewer-import check on code changes.
The served-TUI browser lane runs on demand through the qualification procedure.
The `audit`, `secrets` and `release` workflows have their own triggers. They are outside the local command list.
The workflow examples lane is opt-in and needs a container engine (DL-236):
`WORKFLOW_CONTEXT=<context> uv run pytest -q -o faulthandler_timeout=0 examples/workflows/tests`.
`scripts/arch_check.py --spec-status` prints every specification with its spec
review status (DL-225); it is advisory, and the `spec-review` skill runs one pass
per document.
CI measures coverage in a separate `coverage` job on Python 3.14 only
(DL-211), and that job is also the suite's only 3.14 leg. `sys.monitoring`
makes branch measurement nearly free there; the same measurement costs 2-3x
on the C tracer every other supported version uses.
For browser changes, follow the `explore-page` job and the
[CLI/TUI interaction inventory](cli-tui-qualification.md#tui-interaction-inventory)
for the affected surface. Collect the opt-in tests and verify that they ran
in Chromium, WebKit and Firefox. A skipped suite is not validation.

## Handoff and rework

Keep detailed evidence in the assigned scratch files. Send the operator the
candidate identity, diffstat, judgment calls, unmet requirements and check results.
Do not describe a skipped or blocked check as passing.

Resume the same implementation context for rework. Do not confirm acceptance of
your own fixes; the raiser or named stand-in does that. Call no other model.

If the sandbox blocks a required gate, record the blocked command and use the
client's permission controls for the needed access.
