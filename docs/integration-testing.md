# Select, run and extend v2 regression coverage

For the maintainer selecting, running and extending v2 regression coverage.
Follow the [maintainer track](../README.md#documents).
Use the [assertion inventory](integration-testing-inventory.md) to find a
scenario's source, disposition, replacement, CI lane and remaining gap.
This page leaves site operation to [deploy](deploy.md) and live AutoSys
probes to the [live-instance runbook](live-instance-runbook.md).

## 1. Select an assertion before selecting a harness

Keep every v1 integration, rehearsal and wargame assertion that applies to
v2. Run it in CI where a working replacement exists. The inventory separates
an assertion from the machinery that exercised it. Removing a supervisor,
seal or release verb does not remove the need to prove safe stopping,
durability or installation.

The retirement boundary is [DL-424](decision-log.md).
Retire an assertion only when its required behavior depends on something
the charter excludes. Port its surviving safety obligation separately.
For example, a command surviving a detached engine stop is retired.
An interrupted command recovering safely still needs a v2 assertion.

Keep these two kinds of assertion distinct:

| Kind | Authority for the expected outcome | What a passing check establishes |
| --- | --- | --- |
| AutoSys semantic conformance (`A` in the inventory) | A SEM entry and its vendor source or cited decision in [AutoSys semantics](autosys-semantics.md); use [§8](autosys-semantics.md#8-trace-test-index-oracle-regression-set-one-per-sem-unless-noted) to locate the tests | The stated behavior under the recorded semantic settings; a provisional pin remains provisional |
| dsl41 operational guarantee (`O`) | The affected contract below, with explicit records, effects and refusal expectations | The stated stop, recovery, storage, access or installation behavior in the tested environment |

| Operational obligation | Contract to pin before writing the check |
| --- | --- |
| Attempt identity, interruption, drain and process cleanup | [Runner §6a](runner-design.md#6a-process-lifecycle-tiers-dl-41a), [§7](runner-design.md#7-journal-and-recovery-e1-prod-grade), DL-424, DL-429, DL-431 and DL-433; [wrapper protocol](wrapper-protocol.md) |
| Admission, exact request retry, durability, snapshots and retained history | [Runner §7](runner-design.md#7-journal-and-recovery-e1-prod-grade), DL-425 and DL-428; [control protocol](control-protocol.md) |
| Configuration changes, stops and restart selection | [Runner §7](runner-design.md#7-journal-and-recovery-e1-prod-grade), DL-432, DL-437 and DL-438 |
| Time movement and missed ticks | [Runner §9](runner-design.md#9-time-domains-e2), DL-45 and DL-435 |
| Socket access, tiers and receipts | [Access model](access-model.md) and [control protocol](control-protocol.md) |
| Installed package, unit and selected release environment | [Deploy](deploy.md), DL-439 and [CLI/TUI qualification](cli-tui-qualification.md); the charter excludes snapshot migration |

Pin expected outcomes independently of the implementation under test.
Use a hand-worked status sequence, explicit tick instants, exact effect
counts, a fixed refusal or a contract-derived record sequence.
Record the authority beside that expectation. Do not generate the expected
answer by calling the runtime's own evaluator or scheduler.

Runtime-versus-rehearsal agreement is useful integration evidence. Both use
the same oracle and scheduling code, so agreement alone proves neither
vendor conformance nor the absence of a shared bug. The DST comparator's
lag bounds and the workflow checkers' damage tests add independent checks.
Its expected DST tick set still needs independent semantic pins. The
inventory records that gap rather than declaring the comparator a vendor
oracle. Golden replay also preserves an answer; its provenance determines
what that answer proves.

## 2. Use public fixtures and record uncertain readings

[LICENSING.md item 2](../LICENSING.md) forbids proprietary or
production-derived fixtures, names and artifacts in this public repository,
its docs and issue tracker. Renamed copies remain derived artifacts.
Do not use client job definitions, minified production graphs, captured
production profiles, production calendar exports or derived expected traces.
Build synthetic cases from public vendor rules and record their source.
Invent names, commands, calendars, business data and fault payloads.
Read each fixture and retained artifact line by line before publication.

Cover corners by composition: box depth, flags, status writes, queue gates,
calendar boundaries, timezone changes and interruption points. A small
fixture should expose one disputed outcome. Add larger synthetic estates
to test composition and the charter's capacity ceiling.

The authoritative uncertainty register is [semantics §9](autosys-semantics.md#9-open-questions-and-their-pinned-defaults).
Keep the question, default, refusal and available switch with the scenario.
The following open cases need that label; none is established vendor truth.

| Question | Pinned reading or refusal | Selectable alternative |
| --- | --- | --- |
| Q3c | A scheduled latch is scoped to its box run | No switch |
| Q3d | A pre-existing scheduled arm survives ice | No switch |
| Q6 | An iced member's atom reads true in `box_success` | No switch |
| Q8b | Calendar disposition replaces first, then blind adjust shifts | No switch |
| Q8c | N targets a non-holiday workday; W/P do not recheck target holidays; stages do not re-enter | No switch |
| Q8d | Inclusive rules union; exclusions subtract; expressions read left to right; literal AND/OR accepted; an exclusion-only compound reads literally | No switch |
| Defective calendar tokens in SEM-37 | `WORKDXnn` and `CWEK#n/#L/Mn/Xn` refuse outright | No default and no switch |
| Q10 | Lookback atoms on ice read true | `ice-lookback=ordinary` |
| Q13 | A box status write to RUNNING writes only its status; it does not start a box cycle | No switch |
| Q14 | A box status write to FAILURE/TERMINATED kills no member | No switch |
| Q15 | A box starting with no member in its run completes | `box-start-all-members-out=wait` |
| Q16 | An iced member out of the run does not vote in an idle box | `idle-box-iced-member=vote` |
| Q17 | A never-run waiting member given a terminal status counts as done and votes | No switch |

Q12 is descoped by DL-288. No default is pinned for FREE=Y/A on a
depletable resource. Preflight refuses both and accepts FREE=N. No switch
exists. It is not an open case supported by the runner.

Q11 is closed, but its initial partial-week reading is still a chosen pin:
`wekr-first-week=first-full`, with `partial` available.
The [semantic switch registry](runner-design.md#8a-semantic-switches)
also includes documented alternatives and deliberate dsl41 defaults.
Record the complete effective configuration, timezone map and calendar
inputs. Exercise each relevant switch value. Do not invent a switch for
Q6, Q13, Q14 or Q17. A live-probe result needs a boss ruling before it
changes an expectation.

## 3. Choose the lane that exercises the boundary

This section owns lane routing: triggers, path filters, matrices, timeouts
and evidence uploads. The [workflow guide](agent-workflow.md#verify-a-change)
and [release procedure](../README.md#make-a-release) link here.
The workflows on main at `102abec` implement DL-441 in the
[decision log](decision-log.md). The required check is not yet active.
The operator activates `workflow-examples-required` after the configuration-order
slice and green integrated code and docs-only PR evidence. Service and DST
checks are not globally required. The failure procedure in §4 governs merge blocking.

| Lane | As built on main | Job timeout | Evidence |
| --- | --- | --- | --- |
| `U`: ordinary source checks | Existing `ci.yml` gates remain: code checks on Python 3.12/3.13, coverage on 3.14, golden replay and explore-page browser checks; docs gate on every change | Test/goldens/docs 15 min; coverage 25 min; explore-page 30 min | Job output, coverage/transition results and golden verdict; these are not rehearsal archives |
| `W`: workflow examples | Code PRs call `workflow-examples.yml` through `classify`; manual dispatch and nightly on main at 01:17 UTC | Lane 40 min; classifier and required-result job 5 min each | Always upload `workflow-evidence` and JUnit; missing files are an error |
| `S`: installed service | Ubuntu 24.04 and Rocky Linux 8 matrix; manual dispatch, nightly at 01:37 UTC and selected PR paths | 30 min per image | Always attempt `service-evidence-<image>` upload; missing files warn |
| `D`: clock/DST | Manual dispatch, nightly at 02:17 UTC and selected PR paths; three isolated matrix lanes | 45 min per lane | Always pack and upload `dst-drill-evidence-<lane>`, including scenario exits; missing archive warns |
| `M`: manual or opt-in only | Wargame shell and installed CLI/TUI qualification recipes | Declare a run budget before starting | Retain the exact commands, candidate and outputs; manual availability is not CI scheduling |
| `—`: no runnable v2 lane | Gaps in the inventory | None | No execution or qualification claim |

`workflow-examples-required` accepts only a valid classification with either
a skipped docs-only rehearsal or a successful code rehearsal. Classification
failure, invalid output, cancellation or an unexpected lane result fails
the check. An explicit docs-only pass means the rehearsal was unnecessary;
it is not an executed test.

The service PR filter includes `src/**`, `grammars/**`, `pyproject.toml`,
`uv.lock`, `scripts/wargame.*`, `examples/nightbank/**` and its workflow.
The DST filter includes the same source/grammar/lock/project paths,
`.dockerignore`, `examples/dst-drill/**`, `tests/test_dst_drill.py`,
`scripts/dst-drill-local.sh` and its workflow.
Both exclude `examples/**/*.md`. A test-only change outside these paths does
not trigger those PR drills. The nightly runs supply the broader schedule.

Both service and DST matrices set `fail-fast: false`, so a failed leg does
not cancel another leg. The DST lanes keep every scenario and clock setting:

| Matrix lane | Scenarios |
| --- | --- |
| `falls` | `london-fall`, `newyork-fall` |
| `restart` | `london-fall-restart`, `london-spring`, `london-midnight` |
| `clock` | `clock-step`, `newyork-spring` |

Inspect the four workflow files at the candidate being tested before using
this routing for acceptance. Offline packaging, soak and restore gaps do not
belong to a lane that does not run them.

## 4. Run a selected scenario and keep its evidence

1. Select inventory rows and their contract expectations. Record the full
   candidate commit and any working-tree diff. Follow the locked environment
   [setup](agent-workflow.md#setup).
2. Select the boundary and follow its complete recipe:

   | Boundary | Recipe |
   | --- | --- |
   | Source, oracle and store | [Required local checks](agent-workflow.md#verify-a-change); use semantics §8 to select a trace family |
   | Real PostgreSQL/HTTP/FFmpeg workers | [Workflow integration lane](../examples/workflows/README.md#integration-lane) |
   | Installed engine under systemd | [Service drill](../examples/nightbank/deploy/DRILL.md#run-it), on both images |
   | Real wrappers across clock changes | [DST drill](../examples/dst-drill/README.md#run-it) |
   | Interactive Linux exercises | [Wargame host](../examples/nightbank/deploy/DRILL.md#the-wargame-host); use a fresh named container after checkout edits |
   | Installed terminal or browser interaction | [CLI/TUI qualification](cli-tui-qualification.md) |

3. Record reproducible inputs: fixture hashes and seed, dependency lock and
   export hashes, wheel hash, image ID/digest, resolved OS packages, Python,
   kernel, architecture, clock window/rate and semantic configuration.
   Source installs must contain the candidate bytes. Cached dependency
   layers do not identify the source or constitute an OS update.
4. Control each important ordering with an acknowledgement or barrier.
   Cover stop before and after spawn publication, status before and after
   restart, and update intent before and after application. Record the
   released ordering. Sleep alone does not establish it. Use bounded polling
   and an external deadline with process cleanup and diagnostics.
5. Declare tolerances before the run. The [DST verdict](../examples/dst-drill/README.md#verdict)
   defines exact tick/deadline stamps, end-window exclusions and measured lag
   bounds; the [clock-step checks](../examples/dst-drill/README.md#the-clock-step)
   have a different launch bound. Keep those values and rates unchanged when
   selecting a lane. Record a changed budget as a reviewed change.
6. Retain the exit status, elapsed time, commands, stdout/stderr, driver
   diagnostics, fixture/settings hashes and expected/actual comparison.
   For a service failure keep systemd state and journal. For a clock failure
   keep both traces, lint output, scenario exits and the stopped run root.
   For a workflow failure keep the provider record, database dump and run
   volume under the recipe's cleanup rules. Keep stores and attempt spools
   together. Evidence belongs outside Git unless reviewed for public hygiene.
7. Give the operator the failed assertion and evidence. The operator triages
   nightly failures and blocks affected merges until resolved. Before each
   merge train, the operator checks all nightly results, including missing
   runs, and records a fix and owner for failures. At rollout the operator
   verifies the schedule actor's native failure notifications and receipt.
   The boss resolves semantic disputes, exceptions and tolerance changes. Preserve a
   failed run; a green rerun does not explain it.

Missing prerequisites and failed builds must fail a scheduled rehearsal.
Do not turn them into skips. Check collection and scenario exit lists;
a successful wrapper command or absent artifact is not proof that every
selected assertion ran. Ordinary packaging tests can skip without Python
3.12. The retained offline tests use synthetic wheels, including a fake CLI;
they do not qualify installation of the real v2 package.

## 5. State exactly what the evidence covers

Track these states separately for each assertion and target tuple:

| State | Required evidence |
| --- | --- |
| Implemented | A reachable test or scenario with an independently pinned expectation; comparator-only rows identify that limit |
| Scheduled | A workflow trigger and selected lane actually include that assertion; record the candidate workflow and selected assertion |
| Executed | A run ID or retained local record identifies the exact candidate, inputs, environment, selected assertion and exit; a skip is not execution |
| Qualified | The boss accepts the executed evidence for a named release, environment and scope; the owner approves a tag/release |

An inventory disposition describes preservation, not qualification.
`kept` preserves the check at its old path, `ported` names a tested v2 replacement,
`retired` cites the charter exclusion, and `gap` has no sufficient replacement.
A source replacement can have an installed-boundary gap.
This guide records implementations and schedules from source inspection.
It supplies no new rehearsal executions or qualification.

The [gap register](integration-testing-inventory.md#close-the-surviving-gaps)
links [Known bugs](../TODO.md#known-bugs) to missing or incomplete coverage.
A bug report does not authorize its observed output as an expected result.
Pin the contract's intended outcome, retain the failing evidence and keep
the gap open until the fix and independent review satisfy it.
Container success does not qualify host boot with a new boot identity,
native architecture pairs, site mounts, SELinux, untested upgrades or
vendor behavior that remains an open question.
