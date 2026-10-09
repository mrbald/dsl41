# Risk map of the runner machines

Measured on src at main `c30ad12`, with this pack's tests, on 2026-10-05.
The table, the ranking and the pragma count are from `c30ad12`; the findings are current to DL-307.

This page lists each runner machine with its branch coverage and its open findings.
Low coverage and open findings show a reviewer where to look first.
It is a measurement, not a contract: the linked contracts hold the rules.
Branch coverage is branches only: covered over total, from `scripts/branch_coverage.py`.
The last column lists the open findings of each machine.

| Machine | Owning modules | Contract | Branch coverage per module | Open findings |
| --- | --- | --- | --- | --- |
| Job lifecycle and flags | `oracle_state.py`, `oracle.py`, `conditions.py` | [autosys-semantics §0](autosys-semantics.md#0-execution-model-the-frame-everything-else-hangs-on); [runner-design §3](runner-design.md#3-architecture--functional-core-imperative-shell) | `oracle_state.py` 104/104 (100.00%); `oracle.py` 594/594 (100.00%); `conditions.py` 44/46 (95.65%) | [RELEASE_RESOURCE no-op trace lines](#release_resource-no-op-trace-lines); [unbounded trace and counters](#unbounded-trace-and-counters) |
| Box execution | `oracle.py`, `oracle_state.py` | [autosys-semantics §2](autosys-semantics.md#2-boxes); [period-model §3.5](period-model.md#35-executions--a-discriminated-lifecycle-not-one-row) | `oracle.py` 594/594 (100.00%); `oracle_state.py` 104/104 (100.00%) | [open vendor readings](#open-vendor-readings-for-a-box-and-its-members); [a completed box can flip, and the ancestor walk has no run binding](#a-completed-box-can-flip-and-the-ancestor-walk-has-no-run-binding); [instant cascades recurse](#instant-cascades-recurse) |
| Capacity waiter and reservation | `capacity.py`, `oracle.py`, `oracle_state.py` | [period-model §5](period-model.md#5-capacity-decomposed); DL-50, DL-255, DL-256 | `capacity.py` 84/84 (100.00%); `oracle.py` 594/594 (100.00%); `oracle_state.py` 104/104 (100.00%) | [held-unit circular wait](#held-unit-circular-wait) |
| Scheduler and timer frontier | `runner_scheduler.py`, `runner.py`, `runner_clock.py`, `runner_startup.py`, `runner_journal.py`, `autocal.py` | [runner-design §5](runner-design.md#5-scheduler--the-calendar-the-oracle-deliberately-lacks); [period-model §6](period-model.md#6-the-cutoff-barrier) | `runner_scheduler.py` 66/66 (100.00%); `runner.py` 196/196 (100.00%); `runner_clock.py` 14/16 (87.50%); `runner_startup.py` 182/182 (100.00%); `runner_journal.py` 148/148 (100.00%); `autocal.py` 207/224 (92.41%) | none |
| Engine work choice | `runner.py` | [runner-design §4](runner-design.md#4-engine-loop--single-writer) | `runner.py` 196/196 (100.00%) | [unbounded trace and counters](#unbounded-trace-and-counters) |
| Admission and idempotency | `runner_admission.py`, `runner_codes.py`, `runner.py`, `runner_journal.py` | [concurrency-model §4](concurrency-model.md#4-admission-and-application) | `runner_admission.py` 84/84 (100.00%); `runner_codes.py` no branches; `runner.py` 196/196 (100.00%); `runner_journal.py` 148/148 (100.00%) | [a replayed oracle fault](#a-replayed-oracle-fault-stops-every-resume); [instant cascades recurse](#instant-cascades-recurse) |
| Effect outbox | `runner_effects.py`, `runner_startup.py`, `runner.py`, `runner_journal.py`, `boundary.py` | [concurrency-model §5](concurrency-model.md#5-effects); [period-model §11](period-model.md#11-resume-replay-and-recovery) | `runner_effects.py` 48/48 (100.00%); `runner_startup.py` 182/182 (100.00%); `runner.py` 196/196 (100.00%); `runner_journal.py` 148/148 (100.00%); `boundary.py` 284/284 (100.00%) | none |
| Leadership and takeover | `runner_ledger.py`, `runner_startup.py`, `runner_procid.py` | [concurrency-model §7](concurrency-model.md#7-leadership-relay-takeover); [period-model §2.4](period-model.md#24-leader-and-the-epoch) | `runner_ledger.py` 22/22 (100.00%); `runner_startup.py` 182/182 (100.00%); `runner_procid.py` 26/32 (81.25%) | [one-host limit](#stated-limits); [a journal stays open when genesis fails late](#a-journal-stays-open-when-genesis-fails-late); [a host's boot_id check](#a-hosts-boot_id-check-reads-a-foreign-live-run-as-dead) |
| Host routing | `oracle_state.py`, `runner_hosts.py` | [concurrency-model §8](concurrency-model.md#8-host-lifecycle-active-passive-quarantined-evicted) | `oracle_state.py` 104/104 (100.00%); `runner_hosts.py` 32/32 (100.00%) | [one-host limit](#stated-limits) |
| Control exchange and subscription | `runner_control.py`, `runner_codes.py`, `runner_journal.py`, `cli_control.py` | [control-protocol §2](control-protocol.md#2-transport-and-framing-frozen); [§5](control-protocol.md#5-streaming-verb-subscribe) | `runner_control.py` 312/312 (100.00%); `runner_codes.py` no branches; `runner_journal.py` 148/148 (100.00%); `cli_control.py` 80/94 (85.11%) | [control-protocol known gaps](#stated-limits) |
| Access policy and stream authorization | `runner_access.py` | [access-model §5](access-model.md#5-the-enforcement-point); [§7](access-model.md#7-reload-and-revocation) | `runner_access.py` 90/90 (100.00%) | none |
| Supervisor ownership, transport, lease | `runner_supervisor.py`, `runner_adapters.py`, `runner_procid.py` | [supervisor-protocol §1](supervisor-protocol.md#1-roles); [§5](supervisor-protocol.md#5-supervisor-socket-protocol-frozen--phase-11f-dl-48) | `runner_supervisor.py` 284/344 (82.56%); `runner_adapters.py` 268/330 (81.21%); `runner_procid.py` 26/32 (81.25%) | none |
| SPAWN idempotency | `runner_supervisor.py`, `runner_adapters.py`, `runner_procid.py`, `canon.py`, `runner_startup.py` | [supervisor-protocol §3](supervisor-protocol.md#3-spool-format-frozen); [period-model §11a](period-model.md#11a-spawn-idempotency-that-outlives-the-supervisor) | `runner_supervisor.py` 284/344 (82.56%); `runner_adapters.py` 268/330 (81.21%); `runner_procid.py` 26/32 (81.25%); `canon.py` 57/58 (98.28%); `runner_startup.py` 182/182 (100.00%) | none; [a host's boot_id check](#a-hosts-boot_id-check-reads-a-foreign-live-run-as-dead) |
| Wrapper and command | `runner_wrapper.py`, `runner_adapters.py`, `runner_procid.py` | [supervisor-protocol §2](supervisor-protocol.md#2-wrapper-input-spec-frozen); [§4](supervisor-protocol.md#4-wrapper-behavior-frozen-semantics) | `runner_wrapper.py` 37/52 (71.15%); `runner_adapters.py` 268/330 (81.21%); `runner_procid.py` 26/32 (81.25%) | none |
| FW observation | `runner_adapters.py`, `runner.py`, `runner_startup.py`, `boundary.py` | [runner-design §6](runner-design.md#6-adapters); [period-model §13.6 (PR-34)](period-model.md#136-live-execution) | `runner_adapters.py` 268/330 (81.21%); `runner.py` 196/196 (100.00%); `runner_startup.py` 182/182 (100.00%); `boundary.py` 284/284 (100.00%) | none |
| Seal and successor lineage | `boundary.py`, `seal.py`, `period.py`, `estate.py` | [period-model §1.3](period-model.md#13-the-successor-fence); [§7](period-model.md#7-the-seal-operation) | `boundary.py` 284/284 (100.00%); `seal.py` 186/186 (100.00%); `period.py` 164/164 (100.00%); `estate.py` 24/32 (75.00%) | [torn sole opening segment](#torn-sole-opening-segment-of-a-rolled-root); [anchor machine items](#anchor-machine-items) |
| Audit, archive, retention | `attest.py`, `retention.py` | [period-model §11](period-model.md#11-resume-replay-and-recovery); [§12a](period-model.md#12a-the-archive--pr-q3s-answer-dl-144) | `attest.py` 73/82 (89.02%); `retention.py` 318/318 (100.00%) | none |

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

## Closed by DL-289..DL-306

- DL-289, DL-290, DL-291, DL-293, DL-295: the runner's state machines have transition tables, 14 in all, registered in `src/dsl41/machines.py`. The generated [state-machines page](state-machines.md) lists each one's source state, trigger, guard, effect and target. An event the oracle ignores is a declared internal transition, except the two no-op trace lines of `RELEASE_RESOURCE` (see [RELEASE_RESOURCE no-op trace lines](#release_resource-no-op-trace-lines)) (DL-293). `scripts/transition_coverage.py` fails when an unmarked transition has no hit from a test. One transition is marked `spec-only`: `host.12`, the relay's re-registration, which has no code yet.
- DL-296: every machine has a test that builds each state its builder reaches and applies every event the code accepts. It asserts a declared transition or the code's own refusal. This closes the old finding "no transition inventory". The runner parts that are policies or calculations have no table, and [the policies page](blocks/policies.md) says why (DL-302).
- DL-295: the outbox refuses a second outcome for a resolved effect, before anything is written. The old finding "outbox outcome overwrite" is closed. No live writer resolves a resolved effect. A log that holds two outcomes for one effect stops resume with an error that names the effect.
- DL-294, DL-298, DL-299: every owning module that has branches is in the 100% branch gate (`[tool.coverage.report] include` in `pyproject.toml`). The three steps added the supervisor, the wrapper and process identity, then seven more modules, then the engine-side adapters. DL-299 closes DL-269's list of modules still outside. `runner_codes.py` has no branches and is not listed. `state_machine.py` and `machines.py` (DL-289) are in the gate and are not owning modules of a row in the table. A branch that only forged state reaches carries a pragma that names the invariant.
- DL-303: the service drill runs the rollbacks of upgrade rows 2, 3 and 4, the stop and recover bullets, the configure recipe, the command of every row in the runbook's watch table, and a SIGKILL of the engine unit and of the supervisor unit. What it does not reach stays open under [operator procedures not exercised](#operator-procedures-not-exercised).
- DL-292, DL-297, DL-304, DL-306: part of the old finding "a replayed oracle fault" is closed. The engine dry-applies each control input on a fork before it admits it, so a faulting operator input is refused and never reaches the log (DL-292). Nothing raises on replay for an oracle transition violation, and a replay that faults names its input. A client request id that starts with `engine:` is refused, so it cannot overwrite an engine-made input's decision (DL-297). A start of a job that already has two starts in progress on the call stack is refused, so a nested re-trigger loop cannot raise `RecursionError` (DL-304). The engine and every replay fit the recursion limit to the job count, so a long instant cascade no longer fails every resume (DL-306). What stays open is under [a replayed oracle fault](#a-replayed-oracle-fault-stops-every-resume).

## Open findings

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

### Held-unit circular wait

Two jobs in one catalog can each hold a resource unit the other needs, and neither can start.
A reproduction, with renewable resources X and Y of amount 1: job `a` (priority 1) needs X with `FREE=N` and Y with `FREE=A`; job `b` (priority 2) needs Y with `FREE=Y`.
After each job runs once and fails, `a` holds X and `b` holds Y.
When both start again, `a` waits in `QUE_WAIT` for Y, and `b` waits behind `a`'s priority (DL-255).
Only an operator act ends the wait: `RELEASE_RESOURCE` on `b`, which holds the unit `a` lacks, or `KILLJOB` on either job and then `FORCE_STARTJOB` on it.
A forced start of a holder runs on its held units and does not check or take the unit it lacks (DL-256's reuse rule), so a forced `a` runs without Y.
`RELEASE_RESOURCE` on `a` frees X, but `a` is still short on Y and still blocks `b`.
[DL-286](decision-log.md) records the cycle as a stated limit, with no static check.
`test_dl256_a_circular_wait_over_held_units_breaks_by_an_operator_act` and `test_dl256_releasing_the_blockers_own_held_unit_leaves_the_circular_wait` reproduce it.
The [capacity card](blocks/capacity.md) lists it under its gaps.

### A replayed oracle fault stops every resume

An attempt admitted with no decision is applied through the gate at resume (DL-156).
`replay_inputs` in `src/dsl41/runner_journal.py` runs `apply_attempt` on it again.
If the oracle raised on that attempt because of the attempt itself, it raises again, and the engine fails at every resume.
DL-292 closes this for control inputs, and the [closed section](#closed-by-dl-289dl-306) lists the other closed causes.
Two cases stay open:

- An engine-made input, such as a timer, an adapter completion or a scheduled start, is written to the log before it is applied and has no dry run (DL-292, DL-304). An exception while it is applied stops every resume.
- A fault that a code change introduces stops every resume of a log the earlier build wrote (DL-292).

In both, the estate stays down until a fixed build ships, or the release rolls back to the one that wrote the log. The resume error names the input and says so (DL-292).

### Instant cascades recurse

The oracle evaluates the starts that follow one another in one instant by recursion. A box that completes at its start can start the next box in the same call.
DL-304 refuses a start of a job that already has two starts in progress, which ends a loop.
The depth of a chain that is not a loop is bounded by Python's recursion limit.
DL-306 fits that limit to the estate's job count in the engine and in every replay, so a long chain no longer fails resume.
Iterative cascade dispatch, which needs no recursion, is not built (DL-306).

### Open vendor readings for a box and its members

These rest on readings of the vendor manuals or on no vendor statement. Each but SEM-23 has a probe in [the live-instance runbook](live-instance-runbook.md). The question numbers are in [autosys-semantics §9](autosys-semantics.md).

- Q13 (DL-301): what CHANGE_STATUS RUNNING does to a box. The oracle writes the status. There is no switch.
- Q14 (DL-301): what CHANGE_STATUS FAILURE or TERMINATED does to a running box and to its `job_terminator` members. There is no switch.
- Q15 (DL-304): whether a box whose start leaves no member in the run completes at its start. No vendor page names the case. The switch `box-start-all-members-out` defaults to `complete`.
- SEM-23 (DL-301): the oracle models the `RESTRICT_FORCE_STARTJOB` configuration. An estate with that variable unset can diverge.
- SEM-20 (DL-301): the default `off-ice-in-running-box=next-run` follows the reference pages. The 24.0 Web UI help says the opposite.

### A completed box can flip, and the ancestor walk has no run binding

Both are open in DL-304. The flip behaves the same before and after it.

- SEM-15's idle recompute reads an iced member's status. A completed box can change from SUCCESS to FAILURE when a job inside an iced subbox, or an iced member given a status, fails later. By then the box's success consumers have launched, and its failure consumers launch too. A candidate fix makes the recompute skip iced members. The vendor reading needs a ruling or a probe first.
- The walk from a job's transition up to an ancestor box has no run binding. A restarted box run can complete through a job of the earlier run.

### A journal stays open when genesis fails late

`start_run` does not close the journal when a step after `Journal.create` raises (DL-305).
Every caller exits on that error, the file holds no lock, and its bytes were flushed.
A later fix detaches the journal as DL-305 does in `open_next_period`.

### RELEASE_RESOURCE no-op trace lines

`RELEASE_RESOURCE` writes two no-op trace lines that are not yet internal transitions of `job_holding` (DL-293).
Every other event the oracle ignores is a declared internal transition.

### Anchor machine items

Two items stay open (DL-290):

- `anchor_head.06` declares a move that the refusals in `reclaim` and in the resume step call unreachable. One rule should replace the two.
- `open_claimed` does not check its `period_id` against the claim's `next_period`. Both production callers pass the claim's own, so this is a hardening item.

### Unbounded trace and counters

The oracle's trace, and the engine's `drops`, `deduped` and `refusals` lists, grow for the life of the process with no bound (DL-307).
A capacity measurement, not yet run, must state a limit.

### A host's boot_id check reads a foreign live run as dead

Resume and the spool ladder read a different `boot_id` as "the process is dead".
On a spool written by another host, that reading is false, and a live run on a partitioned host would read as dead (DL-307).
It matters only when more than one host exists, which is not built.

### Operator procedures not exercised

DL-303 added drilled steps for the rollbacks of upgrade rows 2, 3 and 4, the stop and recover bullets, the configure recipe and the watch table. What it does not reach stays open:

- The runbook lists the watch-table meanings that the drill does not reach, after the table.
- The steps that DL-303 added or changed have run only in the local drill, on arm64. The GitHub service drill runs only on dispatch and has not run them.
- The drill runs the resume-safe row with two installs of one build. No release pair qualifies for that row today (DL-266).
- Auditing an estate of an older release with this build refuses with the seal's canonical-form message. The message does not name the version. A refusal that names the version first is open (DL-303).

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
