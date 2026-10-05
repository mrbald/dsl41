# Risk map of the runner machines

Measured on src at main `c30ad12`, with this pack's tests, on 2026-10-05.

This page lists each runner machine with its branch coverage and its open findings.
Low coverage and open findings show a reviewer where to look first.
It is a measurement, not a contract: the linked contracts hold the rules.
Branch coverage is branches only: covered over total, from `scripts/branch_coverage.py`.
Every machine has one common open finding, [no transition inventory](#no-transition-inventory).
The last column lists the others.

| Machine | Owning modules | Contract | Branch coverage per module | Open findings |
| --- | --- | --- | --- | --- |
| Job lifecycle and flags | `oracle_state.py`, `oracle.py`, `conditions.py` | [autosys-semantics §0](autosys-semantics.md#0-execution-model-the-frame-everything-else-hangs-on); [runner-design §3](runner-design.md#3-architecture--functional-core-imperative-shell) | `oracle_state.py` 104/104 (100.00%); `oracle.py` 594/594 (100.00%); `conditions.py` 44/46 (95.65%) | [outside the 100% gate](#owning-modules-outside-the-100-gate) |
| Box execution | `oracle.py`, `oracle_state.py` | [autosys-semantics §2](autosys-semantics.md#2-boxes); [period-model §3.5](period-model.md#35-executions--a-discriminated-lifecycle-not-one-row) | `oracle.py` 594/594 (100.00%); `oracle_state.py` 104/104 (100.00%) | none beyond the common one |
| Capacity waiter and reservation | `capacity.py`, `oracle.py`, `oracle_state.py` | [period-model §5](period-model.md#5-capacity-decomposed); DL-50, DL-255, DL-256 | `capacity.py` 84/84 (100.00%); `oracle.py` 594/594 (100.00%); `oracle_state.py` 104/104 (100.00%) | [held-unit circular wait](#held-unit-circular-wait) |
| Scheduler and timer frontier | `runner_scheduler.py`, `runner.py`, `runner_clock.py`, `runner_startup.py`, `runner_journal.py`, `autocal.py` | [runner-design §5](runner-design.md#5-scheduler--the-calendar-the-oracle-deliberately-lacks); [period-model §6](period-model.md#6-the-cutoff-barrier) | `runner_scheduler.py` 66/66 (100.00%); `runner.py` 196/196 (100.00%); `runner_clock.py` 14/16 (87.50%); `runner_startup.py` 182/182 (100.00%); `runner_journal.py` 148/148 (100.00%); `autocal.py` 207/224 (92.41%) | [outside the 100% gate](#owning-modules-outside-the-100-gate) |
| Engine work choice | `runner.py` | [runner-design §4](runner-design.md#4-engine-loop--single-writer) | `runner.py` 196/196 (100.00%) | none beyond the common one |
| Admission and idempotency | `runner_admission.py`, `runner_codes.py`, `runner.py`, `runner_journal.py` | [concurrency-model §4](concurrency-model.md#4-admission-and-application) | `runner_admission.py` 84/84 (100.00%); `runner_codes.py` no branches; `runner.py` 196/196 (100.00%); `runner_journal.py` 148/148 (100.00%) | [outside the 100% gate](#owning-modules-outside-the-100-gate); [a replayed oracle fault](#a-replayed-oracle-fault-stops-every-resume) |
| Effect outbox | `runner_effects.py`, `runner_startup.py`, `runner.py`, `runner_journal.py`, `boundary.py` | [concurrency-model §5](concurrency-model.md#5-effects); [period-model §11](period-model.md#11-resume-replay-and-recovery) | `runner_effects.py` 48/48 (100.00%); `runner_startup.py` 182/182 (100.00%); `runner.py` 196/196 (100.00%); `runner_journal.py` 148/148 (100.00%); `boundary.py` 284/284 (100.00%) | [outcome overwrite](#outbox-outcome-overwrite) |
| Leadership and takeover | `runner_ledger.py`, `runner_startup.py`, `runner_procid.py` | [concurrency-model §7](concurrency-model.md#7-leadership-relay-takeover); [period-model §2.4](period-model.md#24-leader-and-the-epoch) | `runner_ledger.py` 22/22 (100.00%); `runner_startup.py` 182/182 (100.00%); `runner_procid.py` 26/32 (81.25%) | [one-host limit](#stated-limits); [outside the 100% gate](#owning-modules-outside-the-100-gate) |
| Host routing | `oracle_state.py`, `runner_hosts.py` | [concurrency-model §8](concurrency-model.md#8-host-lifecycle-active-passive-quarantined-evicted) | `oracle_state.py` 104/104 (100.00%); `runner_hosts.py` 32/32 (100.00%) | [one-host limit](#stated-limits) |
| Control exchange and subscription | `runner_control.py`, `runner_codes.py`, `runner_journal.py`, `cli_control.py` | [control-protocol §2](control-protocol.md#2-transport-and-framing-frozen); [§5](control-protocol.md#5-streaming-verb-subscribe) | `runner_control.py` 312/312 (100.00%); `runner_codes.py` no branches; `runner_journal.py` 148/148 (100.00%); `cli_control.py` 80/94 (85.11%) | [control-protocol known gaps](#stated-limits); [outside the 100% gate](#owning-modules-outside-the-100-gate) |
| Access policy and stream authorization | `runner_access.py` | [access-model §5](access-model.md#5-the-enforcement-point); [§7](access-model.md#7-reload-and-revocation) | `runner_access.py` 90/90 (100.00%) | none beyond the common one |
| Supervisor ownership, transport, lease | `runner_supervisor.py`, `runner_adapters.py`, `runner_procid.py` | [supervisor-protocol §1](supervisor-protocol.md#1-roles); [§5](supervisor-protocol.md#5-supervisor-socket-protocol-frozen--phase-11f-dl-48) | `runner_supervisor.py` 284/344 (82.56%); `runner_adapters.py` 268/330 (81.21%); `runner_procid.py` 26/32 (81.25%) | [outside the 100% gate](#owning-modules-outside-the-100-gate) |
| SPAWN idempotency | `runner_supervisor.py`, `runner_adapters.py`, `runner_procid.py`, `canon.py`, `runner_startup.py` | [supervisor-protocol §3](supervisor-protocol.md#3-spool-format-frozen); [period-model §11a](period-model.md#11a-spawn-idempotency-that-outlives-the-supervisor) | `runner_supervisor.py` 284/344 (82.56%); `runner_adapters.py` 268/330 (81.21%); `runner_procid.py` 26/32 (81.25%); `canon.py` 57/58 (98.28%); `runner_startup.py` 182/182 (100.00%) | [outside the 100% gate](#owning-modules-outside-the-100-gate) |
| Wrapper and command | `runner_wrapper.py`, `runner_adapters.py`, `runner_procid.py` | [supervisor-protocol §2](supervisor-protocol.md#2-wrapper-input-spec-frozen); [§4](supervisor-protocol.md#4-wrapper-behavior-frozen-semantics) | `runner_wrapper.py` 37/52 (71.15%); `runner_adapters.py` 268/330 (81.21%); `runner_procid.py` 26/32 (81.25%) | [outside the 100% gate](#owning-modules-outside-the-100-gate) |
| FW observation | `runner_adapters.py`, `runner.py`, `runner_startup.py`, `boundary.py` | [runner-design §6](runner-design.md#6-adapters); [period-model §13.6 (PR-34)](period-model.md#136-live-execution) | `runner_adapters.py` 268/330 (81.21%); `runner.py` 196/196 (100.00%); `runner_startup.py` 182/182 (100.00%); `boundary.py` 284/284 (100.00%) | [outside the 100% gate](#owning-modules-outside-the-100-gate) |
| Seal and successor lineage | `boundary.py`, `seal.py`, `period.py`, `estate.py` | [period-model §1.3](period-model.md#13-the-successor-fence); [§7](period-model.md#7-the-seal-operation) | `boundary.py` 284/284 (100.00%); `seal.py` 186/186 (100.00%); `period.py` 164/164 (100.00%); `estate.py` 24/32 (75.00%) | [torn sole opening segment](#torn-sole-opening-segment-of-a-rolled-root); [outside the 100% gate](#owning-modules-outside-the-100-gate) |
| Audit, archive, retention | `attest.py`, `retention.py` | [period-model §11](period-model.md#11-resume-replay-and-recovery); [§12a](period-model.md#12a-the-archive--pr-q3s-answer-dl-144) | `attest.py` 73/82 (89.02%); `retention.py` 318/318 (100.00%) | [outside the 100% gate](#owning-modules-outside-the-100-gate) |

`runner_supervisor.py` and `runner_wrapper.py` run as subprocesses of the engine.
Their numbers depend on the suite measuring subprocesses (DL-265).
A process a test ends with SIGKILL loses its data, so these numbers are floors.
`runner_codes.py` holds the error-code registry (DL-272) and has no branches, so it adds nothing to a pooled total.
Totals exclude branches that a `pragma: no branch` or `pragma: no cover` comment, `if TYPE_CHECKING:` or `raise AssertionError` excludes (DL-105, DL-269).
The 17 gated modules carry 25 such pragma comments.
So 100% means every remaining branch ran, not that each one was asserted.

## Least-tested machines

Ranking rule: a machine ranks by the lowest branch percent among its owning modules.
A tie goes to the lower pooled percent, which is the sum of covered branches over the sum of total branches of its owning modules.
A remaining tie keeps the table's order.

| Rank | Machine | Weakest module | Pooled |
| --- | --- | --- | --- |
| 1 | Wrapper and command | `runner_wrapper.py` 37/52 (71.15%) | 331/414 (79.95%) |
| 2 | Seal and successor lineage | `estate.py` 24/32 (75.00%) | 658/666 (98.80%) |
| 3 | Supervisor ownership, transport, lease | `runner_adapters.py` 268/330 (81.21%) | 578/706 (81.87%) |
| 4 | SPAWN idempotency | `runner_adapters.py` 268/330 (81.21%) | 817/946 (86.36%) |
| 5 | FW observation | `runner_adapters.py` 268/330 (81.21%) | 930/992 (93.75%) |

Machines 6 to 10 follow:

6. Leadership and takeover: `runner_procid.py` 26/32 (81.25%); pooled 230/236 (97.46%)
7. Control exchange and subscription: `cli_control.py` 80/94 (85.11%); pooled 540/554 (97.47%)
8. Scheduler and timer frontier: `runner_clock.py` 14/16 (87.50%); pooled 813/832 (97.72%)
9. Audit, archive, retention: `attest.py` 73/82 (89.02%); pooled 391/400 (97.75%)
10. Job lifecycle and flags: `conditions.py` 44/46 (95.65%); pooled 742/744 (99.73%)

The other 7 machines have every owning module with branches at 100.00%.

## Closed by DL-263..DL-275

- DL-263: a hand-edited or corrupted anchor or claim file can no longer pass with a coerced field value, because both reads are strict.
- DL-264: control no longer accepts a status change the oracle cannot replay; `QUE_WAIT` is refused before the log append, so it cannot stop a resume.
- DL-265: the supervisor and wrapper subprocesses are measured, and branch-only numbers for the whole package are printed.
- DL-266: a managed supervisor can be stopped for a backup without restarting, the upgrade section has one decision table, and the service drill runs locally.
- DL-267: a subscriber that stops reading is removed at a fixed byte budget, and a peer that stops reading can no longer hang a handler or a shutdown.
- DL-268: an operator has a short path at the front of the runbook, a retirement procedure and a monitoring table.
- DL-269: the 100% branch gate grew from nine to seventeen modules, adding the oracle, capacity, scheduler, control, seal, period, retention and boundary modules.
- DL-270: the release note states the three facts that select an upgrade row, and citations in tests added by DL-264, DL-267, DL-268 and DL-269 were corrected.
- DL-271: the drill's steps run as an unprivileged user with sudo, as on the GitHub runner, so a failure that showed only there now shows locally.
- DL-272: every `ok: false` control answer carries a stable `code`, and a rejected decision stores it; control-protocol §7 gap 4, errors as prose, is closed.
- DL-274: during a seal, an exception while an admitted attempt is not fully applied, a failed WAL append, or a `clock_regressed` on an engine-made input stops the engine instead of refusing the seal. This closes the OPEN item of DL-272. A request refused on `clock_regressed` during a seal is now answered.
- DL-273: closes nothing; it specifies a gateway as proposed (see [Stated limits](#stated-limits)).
- DL-275: the engine and the supervisor install their signal handlers before they publish a socket, so, with an access map, a SIGHUP sent once `control.sock` answers is a reload, never the default action.

## Open findings

### No transition inventory

No list of source state, event, guard and target exists for any machine.
A count of transitions covered by tests cannot be made, so no claim of full transition coverage is made.
The owner deferred the list and a gate on its coverage (DL-270, "CORRECTIONS").
[simulation-coverage.md](simulation-coverage.md) lists exposure, not executed transitions.
DL-264 added a composition test at one seam only: every payload the control framing accepts must apply to the oracle.

### Torn sole opening segment of a rolled root

A rolled root holds exactly one segment.
If that segment's first line is torn or empty, resume refuses.
The refusal names the missing segment record.
It is a refusal, not damage.
`_drop_never_opened_segment` in `src/dsl41/runner_startup.py` returns without repair when fewer than two segments exist.
An identical retry of the roll refuses the same way.
The rule is in the [period-model §11](period-model.md#11-resume-replay-and-recovery) recovery table and DL-144.
The runbook has [a recipe](deployment-runbook.md#recipe-recover-a-rolled-root-whose-opening-is-torn) (DL-278): the operator removes the torn segment and the identical opener reopens in place.
[Period-model §1.3](period-model.md#13-the-successor-fence) says an ordinary crash between the claim and the head move never needs `--force`.
A fix would let the opener recreate a torn sole segment itself when the head is claimed by that root.
DL-278 records it as not built.

### Owning modules outside the 100% gate

The 100% branch gate lists 17 modules in `[tool.coverage.report] include` in `pyproject.toml` (DL-105, DL-269).
These owning modules in the table are not in that list:

| Module | Branches | Machines |
| --- | --- | --- |
| `runner_wrapper.py` | 37/52 (71.15%) | Wrapper and command |
| `estate.py` | 24/32 (75.00%) | Seal and successor lineage |
| `runner_adapters.py` | 268/330 (81.21%) | Supervisor; SPAWN idempotency; Wrapper and command; FW observation |
| `runner_procid.py` | 26/32 (81.25%) | Leadership and takeover; Supervisor; SPAWN idempotency; Wrapper and command |
| `runner_supervisor.py` | 284/344 (82.56%) | Supervisor; SPAWN idempotency |
| `cli_control.py` | 80/94 (85.11%) | Control exchange and subscription |
| `runner_clock.py` | 14/16 (87.50%) | Scheduler and timer frontier |
| `attest.py` | 73/82 (89.02%) | Audit, archive, retention |
| `autocal.py` | 207/224 (92.41%) | Scheduler and timer frontier |
| `conditions.py` | 44/46 (95.65%) | Job lifecycle and flags |
| `canon.py` | 57/58 (98.28%) | SPAWN idempotency |
| `runner_codes.py` | no branches | Admission and idempotency; Control exchange and subscription |

DL-269 names three of them (`runner_supervisor.py`, `runner_wrapper.py`, `runner_adapters.py`) under "STILL OUTSIDE".
Widening the gate to any of them is the owner's call.

### Held-unit circular wait

Two jobs in one catalog can each hold a resource unit the other needs, and neither can start.
A reproduction, with renewable resources X and Y of amount 1: job `a` (priority 1) needs X with `FREE=N` and Y with `FREE=A`; job `b` (priority 2) needs Y with `FREE=Y`.
After each job runs once and fails, `a` holds X and `b` holds Y.
When both start again, `a` waits in `QUE_WAIT` for Y, and `b` waits behind `a`'s priority (DL-255).
Only `RELEASE_RESOURCE`, or `KILLJOB` and then `FORCE_STARTJOB`, ends the wait.
DL-256 accepts hold-and-wait as the vendor's behavior; no entry records this cycle, and no test checks liveness with held units.
The [capacity card](blocks/capacity.md) lists it under its gaps.
Whether it needs a warning or a change is the owner's call.

### A replayed oracle fault stops every resume

An attempt admitted with no decision is applied through the gate at resume (DL-156).
`replay_inputs` in `src/dsl41/runner_journal.py` runs `apply_attempt` on it again.
If the oracle raised on that attempt because of the attempt itself, it raises again, and the engine fails at every resume.
No contract names a remedy.
DL-274 records this under "NOT IN SCOPE": the case predates it, and its seal fail-stop only makes it show at once.

### Outbox outcome overwrite

`Outbox.resolve` in `src/dsl41/runner_effects.py` checks association, then replaces any earlier outcome for the effect.
Every live writer resolves only effects from `Outbox.pending()`, which skips resolved effects.
Those writers are the engine in `src/dsl41/runner.py` and the resume reconciliation in `src/dsl41/runner_startup.py`.
`src/dsl41/boundary.py` resolves an effect it recorded just before.
Only replay in `src/dsl41/runner_journal.py` resolves once per log record.
So an overwrite needs a log with two outcome records for one effect.
No entry yet.

### Operator procedures not exercised

The rollbacks of rows 2, 3 and 4 of the runbook's upgrade table are not drilled.
Row 1's rollback is drilled (DL-270, "CORRECTIONS").
The stop and recover bullets, the configure recipe and the monitoring commands other than the sealed-period check are not run by a test (DL-268).
The torn-opening recipe that the recover bullet links is run by a test (DL-278).
No release pair qualifies for a resume-safe row today (DL-266).

### Stated limits

- Leadership rests on a local file lock and holds for one host ([concurrency-model §1](concurrency-model.md#1-storage--frozen)).
- Host routing has one local host; rerouting to a second host closes with the relay, which is not built ([concurrency-model §9](concurrency-model.md#9-the-proving-ground), CM-14).
- Control is Unix-domain only ([control-protocol §7](control-protocol.md#7-known-gaps-recorded-not-fixed--dl-78)). An HTTP and WebSocket [gateway](gateway.md) is specified as proposed, and nothing of it is built (DL-273).
- On an estate with no access map, the socket's mode and file ownership are the whole access control; a configured estate closes this (DL-146, [control-protocol §7](control-protocol.md#7-known-gaps-recorded-not-fixed--dl-78)).
- `CHANGE_STATUS` refuses `QUE_WAIT` as a stated support limit (DL-264).

## Refresh

Run from the repository root, in this order:

```sh
uv sync --frozen --extra dev
uv run coverage erase
uv run coverage run -m pytest -q
uv run coverage combine
uv run coverage report
uv run python scripts/branch_coverage.py
git fetch
git diff --quiet origin/main -- src && git rev-parse --short origin/main
```

Take each module's `covered` and `branches` columns from the coverage table.
Percent is 100 times covered over branches.
Stamp the main commit whose `src/` was measured, never a branch commit: a rebase merge gives a branch's commits new ids.
The last command prints that commit when the tree's `src/` matches main; when it prints nothing, measure on a tree that matches.
Then update the stamp, the table and the ranking.
Run `uv run pytest -q tests/test_docs_links.py` to check the links.
