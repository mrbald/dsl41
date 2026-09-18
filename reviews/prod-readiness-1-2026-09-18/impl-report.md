# DL-210 implementation report

Branch: `prod-readiness-1`, created with `git switch -c` from local `main`
(`58dd370c1bb07cabbc0e479850bfddf2321063de`). No push. Explicit paths were
staged. No attribution trailers were added. The boss owns the DL-210
entry; this change does not write it or publish an architecture-review tag.

## Commits

| Part | Commit | Subject |
| --- | --- | --- |
| A1 supervisor | `e92520a` | fix: bound supervisor client I/O and retain dropped-push notices |
| A1 client net | `a26b8f2` | fix: feed missed supervisor exits through shared LIST recovery |
| A2 lock | `b21cd4b` | fix: exclude competing supervisors and publish owned sockets atomically |
| A2 verb/runbook | `9c57620` | fix: add foreground supervisor startup and service guidance |
| A3 | Commit containing this report | fix: reset TUI trace state on baseline changes |

The containing-commit reference avoids a self-referential commit hash.
The delivery message records that commit's actual hash.

## Verification

Every required gate uses `uv run --frozen` and `UV_OFFLINE=true`. The
already-synced environment was retained. Each suite subprocess had an
explicit 600-second timeout; other gate subprocesses had 120 seconds.
The tool yields long calls, so each foreground subprocess was polled to
completion. The macOS sandbox made uv panic before collection; approved
escalation allowed the same offline commands to run.

| Command | Final result |
| --- | --- |
| `uv run --frozen ruff check src tests` | PASS |
| `uv run --frozen ruff format --check src tests scripts examples` | PASS; 127 files |
| `uv run --frozen mypy src tests/uc_oracle.py` | PASS; 57 source files |
| `uv run --frozen python scripts/arch_check.py` | Exit 0; seven advisories; architecture review completed |
| `uv run --frozen coverage run -m pytest -q` | `5110 passed, 6 skipped, 2 xfailed in 256.12s (0:04:16)` |
| `uv run --frozen coverage report` | PASS; 100%; 2,382 statements and 842 branches; no misses |

The five requested files were each run alone before combined verification:

| Isolated file | Result |
| --- | --- |
| `tests/test_runner_supervisor.py` | Initially 45 passed, 1 skipped; final CLI pass 125 passed, 1 skipped |
| `tests/test_supervisor_idempotency.py` | 45 passed in 6.87s |
| `tests/test_runner_adapters.py` | 40 passed in 2.75s; close/connect correction later passed 41 |
| `tests/test_ledger.py` | Initially 31 passed; A2 passed 33 |
| `tests/test_runner_tui.py` | 109 passed in 35.11s |

The real resume-prefix regression passed with the 31-test lifecycle file. Later focused client/idempotency checks
passed 92 tests. Final supervisor/backlog checks passed 131 tests with one
platform skip. The pre-CLI A2 full gate was
`5083 passed, 6 skipped, 2 xfailed in 253.11s (0:04:13)`.

Browser collection with `DSL41_BROWSER_TESTS=1` found 324 tests. Final
opt-in result: `324 passed in 434.03s (0:07:14)`. Installed Chromium
151.0.7922.34, WebKit 26.5 and Firefox 153.0 also drove the real browser-served TUI. Keyboard refresh,
reset deferral, next-poll replay, alarm replacement, viewport resizing,
help and clean detach passed in every engine. Replies were synthetic;
real engine replay is covered separately by the lifecycle test. No
browser installation was needed. Linux/systemd service execution was not
run on this macOS host.

Independent adversarial Codex reviews covered supervisor I/O, client/TUI,
startup ownership, and CLI/runbook. Fixed findings included final RELEASE
notice delivery, oversized-refusal stamping, half-close truncation,
close/connect races, malformed pid tokens, and standard-descriptor
inheritance. Architecture review covered all drift since its existing
baseline and a final A2 follow-up. It found no DL-210 architecture blocker.
The final descriptor fix and real-backlog addition were independently
rechecked. The mechanical review-due notice remains because only the boss
may accept and stamp that review. Earlier failed checks were corrected: test cleanup
arguments, one unused import, one formatting issue, a browser-harness API
argument, and nested quoting in the real-exec test. Leaked test supervisors
from the initial cleanup error were identified and terminated before the
final runs.

## Judgments and boundaries

1. With v4 unavailable, oversized input uses `request_too_large`; invalid
   UTF-8 and nested JSON use `malformed_json`. Transient I/O retries and
   terminal I/O drops only its connection. Writes yield after 64 KiB.
   Clients that half-close their writer receive queued replies. No connection cap or
   aggregate memory bound was introduced (`src/dsl41/runner_supervisor.py:668`,
   `:751`, `:791`; `docs/supervisor-protocol.md:354`).
2. A drop counter supplements the lease's sticky boolean so an older
   queued reply cannot clear a later drop. Every holder reply includes
   pending notices, including RELEASE and malformed/oversized refusals;
   pushes do not. A different controller starts clean
   (`src/dsl41/runner_supervisor.py:313`, `:791`, `:1507`).
3. The shared LIST task stays armed and dormant when no waits remain.
   Eligibility begins after SPAWN, duplicate or in-progress replies, or
   reattach. A snapshot excludes waits added during a LIST. Only literal
   `ok: true` is death evidence; malformed rows remain a loud EngineError.
   Existing duplicate/reattach periodic checks and the immediate duplicate
   gate remain. Close waits for an in-flight request before cancellation
   (`src/dsl41/runner_adapters.py:1370`, `:1393`, `:1540`, `:1678`, `:1694`).
4. The client never unlinks refused sockets. The closing-client recheck
   after connect prevents a late connection from reviving a closed client
   (`src/dsl41/runner_adapters.py:1172`). A real full-backlog test also
   proves the live macOS socket survives refusal
   (`tests/test_supervisor_backlog.py:17`).
5. The shared flock helper checks device and inode and uses ESTALE for
   moved paths so LeaderLock preserves its existing error messages
   (`src/dsl41/runner_procid.py:70`; `src/dsl41/runner_ledger.py:124`).
6. Three retries means three total PING attempts over one second. Any
   complete PING reply refuses ownership. Only actual `.s.*` sockets are
   swept. Missing pid evidence beside a published socket, unreadable
   records, failed token lookup with a possibly live pid, and live legacy
   pids refuse conservatively. Canonical token mismatch, known boot
   mismatch, or proven process absence permits reclaim
   (`src/dsl41/runner_supervisor.py:414`, `:439`, `:482`, `:497`).
7. The guards cannot serialize a concurrent old binary that ignores the
   new lock. Do not launch old/new binaries concurrently on one root.
   Ambiguous crash leftovers need operator resolution; they are not
   guessed stale (`docs/supervisor-protocol.md:320`).
8. Foreground start tightens an existing root to 0700, appends the log,
   redirects stdin to the null device and preserves stdio through exec.
   Deadman values must be finite and positive, and are start-only
   (`src/dsl41/cli_control.py:517`). Independent start takes no engine
   leadership or controller lease; the old startup ordering is explicitly
   scoped to run/resume (`docs/concurrency-model.md:712`).
9. The missing v4 service details were reconstructed as a separate unit
   with readiness/dependency ordering, or engine spawning with
   `KillMode=process`. Readiness tries ten times; stop timeout must cover
   command grace. Shape 1 restarts clean shutdowns/deadman exits and
   ownership refusals; stop it with systemctl. Shape 2 gives up whole-cgroup
   stop containment (`docs/deployment-runbook.md:260`). These are deployment
   instructions, not a measured Linux service acceptance result.
10. TUI identity is only baseline_id. A changed or mismatched baseline
    returns before consumption/table update. First observation seeds it;
    headerless refusals and reconnects preserve it. One reset notice may
    accompany an existing connectivity notice. The shorter-trace fallback
    remains. Actual resume preserves the trace prefix
    (`src/dsl41/runner_tui.py:1448`; `tests/test_runner_lifecycle.py:674`;
    `docs/runner-design.md:922`). Startup/journal source needed no edits.
11. Architecture findings outside this slice remain deferred: the
    preexisting minifier workday-vocabulary mismatch
    (`src/dsl41/minify_rules.py:487`) and optional consolidation of older
    per-adapter polling (`src/dsl41/runner_adapters.py:1751`).

## Departures and unverified requirements

- The prompt supplied v5 and v6, but not the v4 text they incorporate.
  A clarification request received no answer. Exact fidelity to v4's
  error taxonomy, numbered test list and service-shape prose cannot be
  established. The concrete interpretations are items 1 and 9 above.
- Both-vendor review could not complete. Claude first reported missing
  authentication in the sandbox; normal-permission retries timed out at
  600 and 180 seconds without a review. Codex peer agreement is recorded;
  no Claude agreement or served-model identity is claimed. This is an
  unmet review rule from `CLAUDE.md:79` and `docs/agent-workflow.md:59`.
- A TUI implementer used `.venv/bin/ruff` for supplemental checks of
  `src/dsl41/runner_tui.py:1448` and its tests. All required final gates
  were rerun through `uv run --frozen`.
- Two reviewers used live web searches and upstream systemd documentation
  on freedesktop.org and GitHub. The parent omitted the no-network
  restriction from those delegated briefs. This violated the task boundary;
  both reviewers stopped network access when notified. Builds and tests
  stayed offline. The affected deployment guidance is at
  `docs/deployment-runbook.md:260`.
- No decision entry or accepted-review tag was created. The prompt reserves
  DL-210 for the boss; code and contract citations intentionally await it.

New committed material was read line by line for public-repository hygiene.
Test jobs, times, service paths and process evidence are synthetic. Scratch
logs, screenshots, peer reports and any authentication diagnostics remain
outside the checkout.
