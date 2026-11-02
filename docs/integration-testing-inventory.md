# Preserve scenario assertions across the v2 cut

For the maintainer choosing which v1 assertion to preserve or extend in v2.
Use the [integration guide](integration-testing.md) for the maintainer track,
assertion authorities, fixture rules, lane triggers and evidence states.
This inventory leaves the complete semantic trace catalog to
[semantics §8](autosys-semantics.md#8-trace-test-index-oracle-regression-set-one-per-sem-unless-noted).

## Read a row and retrieve its source

Each row records one observable obligation, rather than one script or test
function. A function with several outcomes has several rows. Measurement and untested
guard rows preserve obligations, but do not claim v1 asserted an outcome. `A` cites an
AutoSys semantic rule; a reference to semantics §5 means its load, priority
or control-flow attribute rule, with the vendor citations there. `O` uses the guide's
[operational contract map](integration-testing.md#1-select-an-assertion-before-selecting-a-harness).
Example business-result checks are `O`: they check synthetic worker effects,
not vendor semantics. A scenario can contain both kinds of assertion.

The source baseline is `839e0e0^`, resolved to
`f873d7d9334ab325fdd3d1c951c67f83c4cd0ad0`. Each old-source cell names an immutable Git object and its line.
The catalog below groups the examined files. Historical objects may be
absent from the current checkout. Read one with
`git show f873d7d:tests/test_operator_recipes.py | nl -ba`.
The line number locates the check or helper containing it. Current v2
replacement links are relative paths into this checkout.

Earlier history was checked at `1085bfa` (restore), `cfe29fd` (soak),
`64a6878` (DST), `e71a976` and `4f12e7d` (workflow lane and checker fixes),
`fa0715f` (offline rehearsal), `af4221e`,
`81a1598` and `569d731` (wargame relocation and delivery), `a2db2ae`
(upgrade/rollback expansion), `ba4a7c2` (application releases) and `98b1f42`
(supervisor drain). Rows use the last pre-cut main source below. Guard and measurement rows
explicitly say when v1 had no assertion.

| Source key | Old commit | Old path |
| --- | --- | --- |
| S | `f873d7d` | `f873d7d:examples/nightbank/deploy/drill-steps.sh` |
| O | `f873d7d` | `f873d7d:examples/nightbank/deploy/offline-scenarios.sh` |
| F | `f873d7d` | `f873d7d:examples/nightbank/deploy/offline-faults.sh` |
| P | `f873d7d` | `f873d7d:scripts/offline/rpm-lifecycle.sh` |
| B | `f873d7d` | `f873d7d:examples/nightbank/deploy/offline-target.sh` |
| R | `f873d7d` | `f873d7d:tests/test_restore_drill.py` |
| K | `f873d7d` | `f873d7d:tests/test_soak_example.py` |
| C | `f873d7d` | `f873d7d:examples/soak/compressed.py` |
| L | `f873d7d` | `f873d7d:examples/soak/soak.sh` |
| Z | `f873d7d` | `f873d7d:scripts/release_smoke.sh` |
| T | `f873d7d` | `f873d7d:examples/dst-drill/compare.py` |
| J | `f873d7d` | `f873d7d:examples/workflows/tests/test_fulfilment.py` |
| M | `f873d7d` | `f873d7d:examples/workflows/tests/test_media.py` |
| E | `f873d7d` | `f873d7d:examples/workflows/tests/test_energy.py` |
| W | `f873d7d` | `f873d7d:tests/test_nightbank_deploy.py` |
| V | `f873d7d` | `f873d7d:scripts/wargame.sh` |
| I | `f873d7d` | `f873d7d:tests/test_offline_release_inputs.py` |
| Q | `f873d7d` | `f873d7d:tests/test_offline_packaging.py` |
| N | `f873d7d` | `f873d7d:tests/test_nightbank_boundary.py` |
| A | `f873d7d` | `f873d7d:tests/test_nightbank_example.py` |
| X | `f873d7d` | `f873d7d:tests/test_file_processing_runner.py` |
| Y | `f873d7d` | `f873d7d:tests/test_rehearse_check.py` |
| D | `f873d7d` | `f873d7d:tests/test_operator_recipes.py` |

## Bound the deleted-module inventory

This is an inventory of scenario obligations, not every unit assertion in
the v1 implementation. The deleted modules below were considered at
`f873d7d`. In-scope modules drive a worked estate, installation, recovery or
printed procedure. Out-of-scope unit modules isolate the excluded mechanism;
their surviving safety properties remain explicit gaps or scenario rows.
An out-of-scope module does not imply its safety properties are retired.
The table names historical objects, not current modules. DL-424 defines the exclusions;
DL-268's printed-procedure obligation is not retired.

| Deleted modules | Scope and surviving obligation |
| --- | --- |
| `f873d7d:tests/test_operator_recipes.py` | In: D rows below cover printed recipes, shell syntax, command/path agreement and recovery. Missing v2 recipe execution is G-recipes. |
| `f873d7d:tests/test_nightbank_boundary.py`, `f873d7d:tests/test_nightbank_deploy.py`, `f873d7d:tests/test_restore_drill.py`, `f873d7d:tests/test_soak_example.py`, `f873d7d:tests/test_dst_drill.py` | In: N/S/H, R, K and T rows cover their scenario assertions. The DST module is restored in v2. |
| `f873d7d:tests/test_runner_supervisor.py`, `f873d7d:tests/test_supervisor_backlog.py`, `f873d7d:tests/test_supervisor_branches.py`, `f873d7d:tests/test_supervisor_client_chart.py`, `f873d7d:tests/test_supervisor_drain.py`, `f873d7d:tests/test_supervisor_idempotency.py`, `f873d7d:tests/test_supervisor_launch.py`, `f873d7d:tests/test_supervisor_lease_chart.py`, `f873d7d:tests/test_supervisor_maintenance.py`, `f873d7d:tests/test_supervisor_process_chart.py` | Out: supervisor/lease/maintenance unit machinery is excluded. Wrapper evidence, single-box exclusion and bounded stop remain S08/S19/S43/S49, O07/O08 and G-restart/G-storage. This inventory does not claim a complete port of every fault cell in these unit modules. |
| `f873d7d:tests/test_release_apply.py`, `f873d7d:tests/test_release_apply_answers.py`, `f873d7d:tests/test_release_apply_holds.py`, `f873d7d:tests/test_release_apply_horizon.py`, `f873d7d:tests/test_release_apply_seal.py`, `f873d7d:tests/test_release_prepare.py`, `f873d7d:tests/test_release_rollback.py`, `f873d7d:tests/test_release_rollback_holds.py`, `f873d7d:tests/test_release_rollback_takeover.py`, `f873d7d:tests/test_release_service.py`, `f873d7d:tests/test_release_status.py` | Out: release-train units are excluded. The installed release scenarios are U rows; accepted-plan identity, interrupted apply, holds and rollback remain G-update/G-config/G-upgrade. |
| `f873d7d:tests/test_retention.py`, `f873d7d:tests/test_retention_branches.py` | Out: period archive, attestation floors and operator prune units are excluded. No-loss retention is N06/N08 and G-history/G-soak under the SQLite contract. |
| `f873d7d:tests/test_journal_replay.py` | Out as a unit module for the replaced JSONL journal. Its replay/durability obligations remain N01–N03/N17 and R rows. State transfer belongs to G-restore, history and crash durability to G-history, pending dispatch replay to G-restart. SQLite tests supply partial source replacements; G-storage covers ENOSPC and writer exclusion only. |
| `f873d7d:tests/test_resume_root_authority.py`, `f873d7d:tests/test_run_reattach.py` | Out: anchor authority and detached reattachment units are excluded. Wrong-state refusal, no mutation, interruption identity and missing-spool evidence remain R05/R07, U21 and G-restart/G-restore. |
| `f873d7d:tests/test_estate_quiesce.py` | Out: two-unit seal/audit/maintenance orchestration is excluded. Writer-free backup and bounded stopping remain S14/S49 and G-restore. |
| `f873d7d:tests/test_estate.py`, `f873d7d:tests/test_estate_branches.py`, `f873d7d:tests/test_estate_build.py`, `f873d7d:tests/test_estate_wide.py` | Out: retired estate verbs and multi-root readers are unit scope. Printed definition-change scenarios are D rows; adopted inputs and retained history remain U rows and G-update/G-history. |
| `f873d7d:tests/test_anchor_machines.py`, `f873d7d:tests/test_boundary.py`, `f873d7d:tests/test_boundary_branches.py`, `f873d7d:tests/test_classification.py`, `f873d7d:tests/test_attest_branches.py`, `f873d7d:tests/test_seal_artifact.py`, `f873d7d:tests/test_seal_boundary_chart.py`, `f873d7d:tests/test_seal_branches.py` | Out: anchor/seal/lineage/classifier units are excluded. Their live-state boundary scenarios are N rows; mutation-free refusal and coherent state transfer remain G-update/G-config/G-restore. |
| `f873d7d:tests/test_backend_uc.py`, `f873d7d:tests/test_uc_oracle.py` | Out: the UC scheduler target is excluded. No AutoSys conformance assertion is retired with that target; semantics §8 remains the trace catalog. |

`kept` means the check at the old path is still present and asserts the
same observable property. Edits to setup or implementation do not by
themselves establish that equivalence. `ported` means the old check is
removed or replaced and the linked v2 test asserts the preserved property.
A script with no asserted verdict is neither kept nor ported coverage;
`retired` needs the [charter exclusion](integration-testing.md#1-select-an-assertion-before-selecting-a-harness);
`gap` has no sufficient v2 replacement. A retirement below names the excluded
behavior. A neighboring row preserves its surviving safety requirement.
No row asserts a passing execution or release qualification.
Historical locators and current line links locate the check, not an execution record.
The unchanged assertion bodies of kept tests were compared against `f873d7d`.

Lane keys are defined in the [guide](integration-testing.md#3-choose-the-lane-that-exercises-the-boundary).
The guide owns every lane key and trigger, including conditional packaging
collection. This table does not define another routing policy. A gap ID in the last
column names a bounded acceptance task below. A dash means no additional
assertion-specific gap found in this inspection, not universal coverage.

## Preserve service and deployment safety

| ID | Old commit, path and line | Assertion | Disposition | V2 replacement or retirement reason | CI lane | Outstanding gap |
| --- | --- | --- | --- | --- | --- | --- |
| S01 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L76 | O: installed estate lints clean | ported | [drill-steps.sh:20](../examples/nightbank/deploy/drill-steps.sh#L20) | S | — |
| S02 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L76 | O: unit is enabled | ported | [drill-steps.sh:20](../examples/nightbank/deploy/drill-steps.sh#L20) | S | — |
| S03 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L148 | O: installed engine starts a fresh root | ported | [drill-steps.sh:122](../examples/nightbank/deploy/drill-steps.sh#L122) | S | — |
| S04 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L148 | O: supervisor is in a separate cgroup | retired | Charter removes the supervisor tier | — | — |
| S05 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L172 | O: restart resumes the same root | ported | [drill-steps.sh:136](../examples/nightbank/deploy/drill-steps.sh#L136) | S | — |
| S06 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L172 | O: operator holds survive restart | ported | [drill-steps.sh:136](../examples/nightbank/deploy/drill-steps.sh#L136) | S | — |
| S07 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L188 | O: detached command outlives an engine stop | retired | Charter requires tethered jobs and one drain path | — | See S08 |
| S08 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L565 | O: engine crash recovers work without starting a new logical run | ported | [drill-steps.sh:164](../examples/nightbank/deploy/drill-steps.sh#L164) New attempt of the same run replaces v1 detached survival. | S | G-restart |
| S09 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L565 | O: systemd restarts the killed engine | ported | [drill-steps.sh:164](../examples/nightbank/deploy/drill-steps.sh#L164) | S | — |
| S10 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L565 | O: holds survive a crash | ported | [drill-steps.sh:395](../examples/nightbank/deploy/drill-steps.sh#L395) The all sequence checks the hold in upgrade after kill-engine/start-limit. | U, S | G-restart |
| S11 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L212 | O: an unadopted source edit cannot silently change resumed work | ported | [drill-steps.sh:338](../examples/nightbank/deploy/drill-steps.sh#L338) | U, S | G-config |
| S12 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L212 | O: a configuration refusal stays failed without a restart loop | ported | [drill-steps.sh:338](../examples/nightbank/deploy/drill-steps.sh#L338) | S | — |
| S13 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L222 | O: a sealed period stays stopped until an explicit open | retired | Charter excludes periods, seals and opening a successor | — | See S14 |
| S14 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L246 | O: stop reaches a writer-free backup boundary | gap | Supervisor/seal recipe retires; one-unit drain needs an independent no-writer check | — | G-restore |
| S15 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L257 | O: restored state preserves operator holds | gap | Restart tests do not delete and restore a complete stopped root | — | G-restore |
| S16 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L285 | O: enabled service resumes the root at boot | ported | [drill-steps.sh:155](../examples/nightbank/deploy/drill-steps.sh#L155) | S | G-boot |
| S17 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L310 | O: unit state exposes status and restart count | gap | [service] uses systemctl; no equivalent full monitoring row | — | G-access |
| S18 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L310 | O: supervisor log and LIST expose completed wrappers | retired | Charter excludes supervisor monitoring; v2 attempt records use runs | — | — |
| S19 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L310 | O: leader note identifies the current engine | ported | [test_runner_leadership.py:179](../tests/test_runner_leadership.py#L179) | U | G-access |
| S20 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L310 | O: leader note carries host epoch | retired | Charter excludes host routing and epoch | — | — |
| S21 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L310 | O: control status answers while engine serves | ported | [drill-steps.sh:122](../examples/nightbank/deploy/drill-steps.sh#L122) | S | — |
| S22 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L480 | O: policy_loaded records the access-map digest | gap | Policy digest parsing is tested; no matching policy_loaded receipt digest equality assertion identified | — | G-access |
| S23 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L310 | O: free space can be inspected on every durable-state path | ported | [storage space] inspects capacity on the store, SQLite sidecars, every spool path and logs. | S | — |
| S24 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L310 | O: subscribe delivers an admitted status event | gap | [control tests] stream hold/off-hold records; installed adapter STATUS delivery lacks an equivalent assertion | — | G-access |
| S25 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L310 | O: trace since a cursor includes the failure once | gap | [control tests] check cursor sequences; the installed failure transition exactly once is not reproduced | — | G-history |
| S26 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L310 | O: acknowledged stream closure reports a resubscribe cursor | ported | [test_cli_control_branches.py:206](../tests/test_cli_control_branches.py#L206) | U | G-access |
| S27 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L310 | O: no-engine status exits 2 | ported | [test_cli_control_read_errors.py:23](../tests/test_cli_control_read_errors.py#L23) | U | G-access |
| S28 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L310 | O: pre-ack subscribe failure provides no false resume cursor | ported | [test_cli_control_branches.py:193](../tests/test_cli_control_branches.py#L193) | U | G-access |
| S29 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L420 | O: service process and state use the service account | gap | [service] installs one user; no full installed identity/mode assertion row | — | G-access |
| S30 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L420 | O: owner-only socket denies other accounts | gap | Mode and unreadable-socket probes exist; no equivalent cross-account owner-only connection test | — | G-access |
| S31 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L420 | O: a service account outside socket_group refuses arming | ported | [test_access.py:1096](../tests/test_access.py#L1096) Injects the group chown failure; installed group membership remains G-access. | U | G-access |
| S32 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L420 | O: group exposure sets root 0710 | ported | [test_access.py:541](../tests/test_access.py#L541) | U | G-access |
| S33 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L420 | O: group exposure sets socket 0660 | ported | [test_access.py:541](../tests/test_access.py#L541) | U | G-access |
| S34 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L420 | O: missing ancestor traversal denies connection | gap | No full installed multi-account row in the v2 service drill | — | G-access |
| S35 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L420 | O: ops binding permits mutations | ported | [test_access.py:354](../tests/test_access.py#L354) | U | G-access |
| S36 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L420 | O: read binding refuses mutations without applying them | ported | [test_access.py:316](../tests/test_access.py#L316) | U | G-access |
| S37 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L420 | O: unbound group member is denied | ported | [test_access.py:380](../tests/test_access.py#L380) | U | G-access |
| S38 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L420 | O: denial receipt names the principal | ported | [test_access.py:316](../tests/test_access.py#L316) | U | G-access |
| S39 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L420 | O: socket_group change refuses policy reload as a whole | ported | [test_access.py:1077](../tests/test_access.py#L1077) | U | G-access |
| S40 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L420 | O: restart cannot silently change execution semantics | ported | [test_main_config_restart.py:98](../tests/test_main_config_restart.py#L98) Stored semantics stay selected; legacy drift still refuses. | U | G-config |
| S41 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L514 | O: repeated crashes reach StartLimitBurst | ported | [drill-steps.sh:374](../examples/nightbank/deploy/drill-steps.sh#L374) | S | — |
| S42 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L514 | O: reset-failed plus start recovers the same root | ported | [drill-steps.sh:374](../examples/nightbank/deploy/drill-steps.sh#L374) | S | — |
| S43 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L514 | O: exact retry returns the first decision without a second effect | ported | [test_admission.py:170](../tests/test_admission.py#L170); [storage admission] pins the retained first decision and one external effect before and after restart, then admits a fresh run. | U, S | — |
| S44 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L514 | A SEM-23: a fresh force request starts a new run | gap | Plain STARTJOB retry contrast is tested; no matching fresh FORCE_STARTJOB after exact-retry sequence identified | — | G-access |
| S45 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L602 | O: systemd restarts a supervisor while engine survives | retired | Charter excludes the supervisor and its independent unit | — | — |
| S46 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L602 | O: lease renews after a long supervisor outage | retired | Charter excludes supervisor leases and host-unreachable handling | — | — |
| S47 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L602 | O: supervisor loss ends the tracked command without a new logical run | retired | Charter removes supervisor loss; interruption safety is S08 and O06 | — | See S08 |
| S48 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L703 | O: stopping prevents new launches while draining | ported | [test_engine_stops.py:125](../tests/test_engine_stops.py#L125) Replacement plans remain undispatched through the drain. | U | G-restart |
| S49 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L703 | O: drain is bounded and reports unfinished work | ported | [test_runner_attempts.py:1046](../tests/test_runner_attempts.py#L1046) Deadline reports unsettled attempts; service drain also checks TERM grace. | U, S | G-restart |
| S50 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L703 | O: lease renews while supervisor drains | retired | Charter excludes the lease; launch suppression is S48 | — | — |
| S51 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L703 | O: engine stops after supervisor exits | retired | Charter has one unit; S49 preserves bounded stopping | — | — |
| S52 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L703 | O: process termination has durable evidence before recovery | ported | [drill-steps.sh:196](../examples/nightbank/deploy/drill-steps.sh#L196) | S | G-restart |
| S53 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L703 | O: cm-seal opens exactly one successor period | retired | Charter excludes seals and period succession | — | — |
| S54 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L1345 | O: disabled/removed service cannot restart retired work | gap | One-unit retirement needs a boot-target and stray-start refusal drill | — | G-retire |
| S55 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L1345 | O: retained history remains readable after retirement | gap | Source history tests do not exercise uninstall/retirement | — | G-retire |
| S56 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L1345 | O: release sudo grant is removed before retirement | retired | Charter excludes the application release service and grant | — | — |
| S57 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L1345 | O: anchor registry never names a deleted root | retired | Charter excludes anchors and lineage | — | — |

## Preserve definition changes and engine upgrades

| ID | Old commit, path and line | Assertion | Disposition | V2 replacement or retirement reason | CI lane | Outstanding gap |
| --- | --- | --- | --- | --- | --- | --- |
| U01 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L853 | O: added job becomes runnable under adopted configuration | gap | Control-socket test edits two existing jobs; no matching installed added-job-runs assertion identified | — | G-update |
| U02 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L853 | O: changed job runs its adopted command | ported | [drill-steps.sh:224](../examples/nightbank/deploy/drill-steps.sh#L224) Reads back the adopted command under a new revision. | U, S | — |
| U03 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L908 | O: removed job retains its run as a ghost | ported | [test_update_wiring.py:107](../tests/test_update_wiring.py#L107) | U | G-history |
| U04 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L908 | O: applying an earlier definition restores behavior | gap | Revert is another update; no installed add/change/remove/revert sequence | — | G-update |
| U05 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L924 | O: properties-only input edit changes the effective configuration | gap | Captured input and drift tests exist; properties-only effective-value adoption is not asserted by the cited tests | — | G-update |
| U06 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L938 | O: referenced calendar-only edit changes affected scheduling | gap | Changed start-time plans are tested; a referenced calendar-only edit needs its own assertion | — | G-update |
| U07 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L952 | O: a competing apply is refused while one applies | ported | [test_update_wiring.py:1196](../tests/test_update_wiring.py#L1196) | U | — |
| U08 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L1013 | O: interrupted configuration adoption resumes without applying twice | ported | [test_update_wiring.py:811](../tests/test_update_wiring.py#L811) | U | G-config |
| U09 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L1013 | O: interrupted adoption status states what remains to do | gap | Live pending responses are tested; interrupted-adoption status and next-action output lack an exact replacement | — | G-update |
| U10 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L1013 | O: a release apply commits one seal | retired | Charter excludes release records, ACTIVE and seals | — | — |
| U11 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L1026 | O: selecting a compatible binary resumes the same state | ported | [drill-steps.sh:395](../examples/nightbank/deploy/drill-steps.sh#L395) | S | G-upgrade |
| U12 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L1026 | O: rollback selects the old compatible binary | gap | Current service drill upgrades only; no rollback row | — | G-upgrade |
| U13 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L1026 | O: detached command and supervisor survive binary flip | retired | Charter requires draining the engine and tethered commands | — | See S49 |
| U14 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L1107 | O: unclear release compatibility uses a fresh state root | gap | [deploy] documents the decision; no separate-version-pair drill | — | G-upgrade |
| U15 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L1112 | O: rollback uses old binary and a compatible retained root | gap | No installed version-pair rollback drill | — | G-upgrade |
| U16 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L1142 | O: wrapper protocol change replaces both service units | retired | Charter excludes the second unit; wrapper compatibility is U17 | — | — |
| U17 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L1142 | O: old wrappers cannot be paired unsafely with a new engine | gap | Drain first and use compatible state; no real protocol-version pair drill | — | G-upgrade |
| U18 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L1149 | O: protocol-changing rollback retains a usable old binary | gap | No installed protocol-version rollback drill | — | G-upgrade |
| U19 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L1181 | O: old launcher refuses an unsupported CLI interface | retired | Charter excludes the v1 launcher and release verbs | — | — |
| U20 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L1181 | O: actual old/new executables are exercised in both directions | gap | .post1 from one tree is not a tested release pair | — | G-upgrade |
| U21 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L1249 | O: new binary refuses incompatible state without migrating it | ported | [drill-steps.sh:338](../examples/nightbank/deploy/drill-steps.sh#L338) | U, S | G-upgrade |
| U22 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L1249 | O: old binary can still read retained old state | gap | No real old-state reader acceptance after a v2 upgrade | — | G-upgrade |
| U23 | `f873d7d:examples/nightbank/deploy/drill-steps.sh` L1295 | O: rollback restores old binary/state with holds intact | gap | No real state-version-pair rollback drill | — | G-upgrade |

## Preserve installed offline faults and package lifecycle checks

| ID | Old commit, path and line | Assertion | Disposition | V2 replacement or retirement reason | CI lane | Outstanding gap |
| --- | --- | --- | --- | --- | --- | --- |
| O01 | `f873d7d:examples/nightbank/deploy/offline-scenarios.sh` L34 | O: supervisor stop lets a detached job finish without signalling it | retired | Charter replaces supervisor drain with tethered engine stop | — | See S49 |
| O02 | `f873d7d:examples/nightbank/deploy/offline-scenarios.sh` L50 | O: maintenance refuses while engine holds authority | retired | Charter excludes maintenance admission | — | See O08 |
| O03 | `f873d7d:examples/nightbank/deploy/offline-scenarios.sh` L50 | O: maintenance refuses live work without signalling it | retired | Charter excludes maintenance; explicit drain consent follows the engine contract | — | See S49 |
| O04 | `f873d7d:examples/nightbank/deploy/offline-scenarios.sh` L50 | O: runtime flip leaves a detached command/attempt alive | retired | Charter requires old engine drain before binary selection | — | See U11 |
| O05 | `f873d7d:examples/nightbank/deploy/offline-scenarios.sh` L50 | O: explicit termination leaves no writers | gap | [service] tests an individual command; no full offline no-writer acceptance | — | G-restore |
| O06 | `f873d7d:examples/nightbank/deploy/offline-scenarios.sh` L50 | O: interrupted work is reconciled without duplicate business effect | ported | [test_file_processing_runner.py:208](../tests/test_file_processing_runner.py#L208) Independent handler-call and output witnesses; v2 may use another attempt. | U, W | G-restart |
| O07 | `f873d7d:examples/nightbank/deploy/offline-scenarios.sh` L118 | O: concurrent installed startup refuses without replacing leader | ported | [storage unit] refuses `--config` contenders before and after CONT; MainPID, its start token and InvocationID stay fixed. [storage probe child] adds legacy-path coverage. | U, S | — |
| O08 | `f873d7d:examples/nightbank/deploy/offline-scenarios.sh` L118 | O: refused startup changes no durable state | ported | [storage unit] compares the SQLite dump, durable file hashes, lock note and socket across each `--config` refusal. [storage probe child] adds legacy-path coverage. | U, S | — |
| O09 | `f873d7d:examples/nightbank/deploy/offline-scenarios.sh` L131 | O: expired lease cannot override a live leader.lock | ported | [storage probe child] preserves live-lock exclusion with expired diagnostic timestamps. The supervisor lease itself is excluded by DL-424; v2 has no lease authority. | U, S | — |
| O10 | `f873d7d:examples/nightbank/deploy/offline-scenarios.sh` L131 | O: diagnostic CONT targets the observed process identity | ported | [storage unit] verifies MainPID with its kernel start token before STOP/CONT and confirms the same InvocationID and RUNNING. [storage probe child] adds unreaped-child coverage. | U, S | — |
| O11 | `f873d7d:examples/nightbank/deploy/offline-scenarios.sh` L158 | O: unknown residual is never blindly signalled | gap | Cookie/group proof tests exist; unknown installed cgroup residue is not the same fixture | — | G-restart |
| O12 | `f873d7d:examples/nightbank/deploy/offline-scenarios.sh` L158 | O: unresolved residue cannot be declared safely stopped | gap | [attempt tests] unsettled deadline; installed residue/quarantine needs fault drill | U | G-restart |
| O13 | `f873d7d:examples/nightbank/deploy/offline-scenarios.sh` L158 | O: uncertain supervisor retains engine exclusion until incident containment | retired | Charter excludes supervisor custody and maintenance uncertainty state | — | See O12 |
| O14 | `f873d7d:examples/nightbank/deploy/offline-scenarios.sh` L230 | O: exact request retry produces one external file effect | gap | Admission retry and business idempotence are tested separately; exact control retry plus one external file effect lacks a matching integration assertion | — | G-update |
| O15 | `f873d7d:examples/nightbank/deploy/offline-scenarios.sh` L230 | O: add/change/remove/revert produces the exact declared effect sequence | gap | Source reload tests and one service edit do not cover the full sequence | — | G-update |
| O16 | `f873d7d:examples/nightbank/deploy/offline-scenarios.sh` L313 | O: restored root keeps state and holds after original files are removed | gap | No v2 copy/delete/restore drill | — | G-restore |
| O17 | `f873d7d:examples/nightbank/deploy/offline-scenarios.sh` L335 | O: installed access policy enforces multi-account tiers | gap | [access tests] cover source behavior; service configure row absent | U | G-access |
| O18 | `f873d7d:examples/nightbank/deploy/offline-scenarios.sh` L349 | O: bounded real ENOSPC before admission launches no command | ported | [storage admission] fills a dedicated 32 MiB tmpfs to real ENOSPC; the failed request adds no records, spool or external effect. | S | — |
| O19 | `f873d7d:examples/nightbank/deploy/offline-scenarios.sh` L349 | O: failed durable admission remains UNKNOWN to the client | ported | [storage admission] pins CLI exit 4 and same-request retry advice after actual admission SQLITE_FULL. | S | — |
| O20 | `f873d7d:examples/nightbank/deploy/offline-scenarios.sh` L349 | O: restart after ENOSPC preserves run count | ported | [storage admission] restarts fresh after freeing space: run count stays 1, then the failed request starts run 2. | S | — |
| O21 | `f873d7d:examples/nightbank/deploy/offline-scenarios.sh` L349 | O: restart after ENOSPC preserves external attempt count | ported | [storage admission] pins external effects [1] across ENOSPC/restart/exact retry, then [1, 2] after space returns. | S | — |
| O22 | `f873d7d:examples/nightbank/deploy/offline-faults.sh` L17 | O: missing release wheel refuses before candidate creation | ported | [test_offline_packaging.py:188](../tests/test_offline_packaging.py#L188) | U | G-package |
| O23 | `f873d7d:examples/nightbank/deploy/offline-faults.sh` L17 | O: corrupted release wheel refuses before candidate creation | ported | [test_offline_packaging.py:188](../tests/test_offline_packaging.py#L188) | U | G-package |
| O24 | `f873d7d:examples/nightbank/deploy/offline-faults.sh` L17 | O: changed template refuses before candidate creation | ported | [test_offline_packaging.py:188](../tests/test_offline_packaging.py#L188) | U | G-package |
| O25 | `f873d7d:examples/nightbank/deploy/offline-faults.sh` L30 | O: concurrent preparations converge on one completed candidate | ported | [test_offline_packaging.py:161](../tests/test_offline_packaging.py#L161) | U | G-package |
| O26 | `f873d7d:examples/nightbank/deploy/offline-faults.sh` L44 | O: incomplete final candidate is retained and refused | ported | [test_offline_packaging.py:206](../tests/test_offline_packaging.py#L206) | U | G-package |
| O27 | `f873d7d:examples/nightbank/deploy/offline-faults.sh` L53 | O: missing dependency pin fails without a network/cache fallback | ported | [test_offline_packaging.py:300](../tests/test_offline_packaging.py#L300) | U | G-package |
| O28 | `f873d7d:examples/nightbank/deploy/offline-faults.sh` L62 | O: actual manufacture ENOSPC retains an incomplete candidate | gap | Synthetic partial and pip failure do not reproduce real ENOSPC | — | G-package |
| O29 | `f873d7d:examples/nightbank/deploy/offline-faults.sh` L62 | O: retry does not repair or remove the failed candidate | ported | [test_offline_packaging.py:206](../tests/test_offline_packaging.py#L206) | U | G-package |
| O30 | `f873d7d:examples/nightbank/deploy/offline-faults.sh` L62 | O: failed manufacture leaves the active runtime receipt unchanged | gap | Retained synthetic active-link tests are narrower than real ENOSPC | U | G-package |
| O31 | `f873d7d:scripts/offline/rpm-lifecycle.sh` L34 | O: fresh RPM install manufactures a usable runtime | gap | [RPM helper] remains; no v2 real-wheel RPM CI lane | — | G-package |
| O32 | `f873d7d:scripts/offline/rpm-lifecycle.sh` L72 | O: RPM upgrade preserves the selected active runtime | gap | Retained helper alone supplies no v2 execution | — | G-package |
| O33 | `f873d7d:scripts/offline/rpm-lifecycle.sh` L72 | O: same RPM reinstall preserves the active runtime | gap | Retained helper alone supplies no v2 execution | — | G-package |
| O34 | `f873d7d:scripts/offline/rpm-lifecycle.sh` L72 | O: RPM downgrade preserves the active runtime | gap | Retained helper alone supplies no v2 execution | — | G-package |
| O35 | `f873d7d:scripts/offline/rpm-lifecycle.sh` L40 | O: installed RPM after failed post is not a ready runtime | gap | No v2 failed-scriptlet lane | — | G-package |
| O36 | `f873d7d:scripts/offline/rpm-lifecycle.sh` L40 | O: failed post retains incomplete candidate evidence | gap | No v2 failed-scriptlet lane | — | G-package |
| O37 | `f873d7d:scripts/offline/rpm-lifecycle.sh` L40 | O: failed post keeps the prior active runtime usable | gap | No v2 failed-scriptlet lane | — | G-package |
| O38 | `f873d7d:scripts/offline/rpm-lifecycle.sh` L78 | O: RPM owns only its delivery subtree | gap | No v2 RPM ownership acceptance | — | G-package |
| O39 | `f873d7d:scripts/offline/rpm-lifecycle.sh` L72 | O: package lifecycle does not overwrite operator-owned files | gap | Old launcher sentinel can become a v2 site unit/config sentinel | — | G-package |
| O40 | `f873d7d:scripts/offline/rpm-lifecycle.sh` L82 | O: erase preserves retained runtime and its receipt | gap | No v2 erase acceptance | — | G-package |
| O41 | `f873d7d:examples/nightbank/deploy/offline-target.sh` L61 | O: both base/UI profiles install at default and alternate prefixes | gap | Retained [offline packaging] uses synthetic package; no target matrix | U | G-package |
| O42 | `f873d7d:examples/nightbank/deploy/offline-target.sh` L4 | O: disconnected manufacture needs no target-side uv | gap | [offline inputs] tests downloads with a synthetic CLI, not installed v2 | U | G-package |
| O43 | `f873d7d:examples/nightbank/deploy/offline-target.sh` L61 | O: disconnected qualification runs with networking disabled | gap | Service drill's wheelhouse install does not prove disconnected qualification | — | G-package |
| O44 | `f873d7d:examples/nightbank/deploy/offline-target.sh` L61 | O: native architecture is checked before target qualification | gap | No v2 offline target tuple or native qualification record | — | G-package |
| O45 | `f873d7d:tests/test_offline_release_inputs.py` L147 | O: exact wheel is bound to project, lock and profile-export hashes | kept | [test_offline_release_inputs.py:137](../tests/test_offline_release_inputs.py#L137) | U | G-package |
| O46 | `f873d7d:tests/test_offline_release_inputs.py` L242 | O: unsafe requirement options refuse despite matching manifest | kept | [test_offline_release_inputs.py:232](../tests/test_offline_release_inputs.py#L232) | U | G-package |
| O47 | `f873d7d:tests/test_offline_packaging.py` L412 | O: installer child retains exclusion when preparation parent dies | kept | [test_offline_packaging.py:412](../tests/test_offline_packaging.py#L412) | U | G-package |
| O48 | `f873d7d:tests/test_offline_packaging.py` L222 | O: repeat detects changed installed bytes without rewriting receipt | kept | [test_offline_packaging.py:222](../tests/test_offline_packaging.py#L222) | U | G-package |
| O49 | `f873d7d:tests/test_offline_packaging.py` L260 | O: pip environment/config cannot redirect isolated preparation | kept | [test_offline_packaging.py:260](../tests/test_offline_packaging.py#L260) | U | G-package |
| O50 | `f873d7d:tests/test_offline_packaging.py` L317 | O: redirected candidate directory refuses without writing through it | kept | [test_offline_packaging.py:317](../tests/test_offline_packaging.py#L317) | U | G-package |
| O51 | `f873d7d:tests/test_offline_packaging.py` L354 | O: failed installed smoke leaves no success receipt | kept | [test_offline_packaging.py:354](../tests/test_offline_packaging.py#L354) | U | G-package |

## Preserve backup and restore assertions

| ID | Old commit, path and line | Assertion | Disposition | V2 replacement or retirement reason | CI lane | Outstanding gap |
| --- | --- | --- | --- | --- | --- | --- |
| R01 | `f873d7d:tests/test_restore_drill.py` L124 | O: backup begins only after every writer stops | gap | Supervisor teardown retires; engine/store/spool writer exclusion still needs proof | — | G-restore |
| R02 | `f873d7d:tests/test_restore_drill.py` L124 | O: fixture inputs are backed up with the durable state | gap | No complete v2 restore recipe under test | — | G-restore |
| R03 | `f873d7d:tests/test_restore_drill.py` L124 | O: restore succeeds after all originals are deleted | gap | Restart on existing files does not cover this | — | G-restore |
| R04 | `f873d7d:tests/test_restore_drill.py` L124 | O: missing anchor refuses audit | retired | Charter excludes anchors | — | See R05 |
| R05 | `f873d7d:tests/test_restore_drill.py` L124 | O: incomplete required backup refuses without inventing state | gap | [store wiring] damaged-store refusals are partial coverage | U | G-restore |
| R06 | `f873d7d:tests/test_restore_drill.py` L124 | O: relocated lineage refuses because registry paths disagree | retired | Charter excludes registry paths; v2 relocation needs a scoped ruling | — | G-restore |
| R07 | `f873d7d:tests/test_restore_drill.py` L124 | O: refused restore/start mutates nothing | gap | Foreign-root startup checks that no engine.db is made; it does not compare the complete refused restore tree | — | G-restore |
| R08 | `f873d7d:tests/test_restore_drill.py` L124 | O: fresh readers use only restored files | gap | No v2 destructive restore plus fresh CLI reader check | — | G-restore |
| R09 | `f873d7d:tests/test_restore_drill.py` L124 | O: restoring scheduler state does not claim restoration of job side effects | gap | Separate synthetic business-store backup and scheduler restore acceptance | — | G-restore |
| R10 | `f873d7d:tests/test_restore_drill.py` L124 | O: restored lineage retains derivation/attestation verification tiers | retired | Charter excludes lineage and attestation | — | — |
| R11 | `f873d7d:tests/test_restore_drill.py` L124 | O: restoring archived WAL cannot reverse archive status | retired | Charter excludes period archive and its receipt | — | — |
| R12 | `f873d7d:tests/test_restore_drill.py` L124 | O: restored state can continue processing once | gap | Replace successor-period open with one-root restart and an exact effect count | — | G-restore |

## Preserve capacity, long-run and shutdown assertions

| ID | Old commit, path and line | Assertion | Disposition | V2 replacement or retirement reason | CI lane | Outstanding gap |
| --- | --- | --- | --- | --- | --- | --- |
| K01 | `f873d7d:tests/test_soak_example.py` L54 | O: seeded capacity fixture regenerates byte for byte | gap | Deleted generator/estate has no v2 soak replacement | — | G-soak |
| K02 | `f873d7d:tests/test_soak_example.py` L64 | O: another seed changes workload without changing shape | gap | No v2 capacity generator | — | G-soak |
| K03 | `f873d7d:tests/test_soak_example.py` L71 | O: capacity estate has 200 jobs | gap | Charter keeps the 100–200-job ceiling | — | G-soak |
| K04 | `f873d7d:tests/test_soak_example.py` L71 | O: each job has one to five planned starts per day | gap | No v2 capacity fixture | — | G-soak |
| K05 | `f873d7d:tests/test_soak_example.py` L71 | O: planned daily starts stay in the declared 900–1000 range | gap | Old fixture bound must be retained or replaced by a reviewed fixture bound | — | G-soak |
| K06 | `f873d7d:tests/test_soak_example.py` L71 | A semantics §5: machine max_load admits at most 20 concurrent positive-priority unit-load jobs | gap | [capacity tests] cover rules; no full-size fixture assertion | U | G-soak |
| K07 | `f873d7d:tests/test_soak_example.py` L71 | O: every command belongs to the synthetic inert command allow-list | gap | No regenerated v2 soak fixture to inspect | — | G-soak |
| K08 | `f873d7d:tests/test_soak_example.py` L92 | O: a full day actually produces 900–1100 total starts | gap | No v2 day-long composition check | — | G-soak |
| K09 | `f873d7d:tests/test_soak_example.py` L92 | O: every command and box runs during the day | gap | No v2 full-estate liveness check | — | G-soak |
| K10 | `f873d7d:tests/test_soak_example.py` L92 | O: no command exceeds five runs per day | gap | No v2 full-estate count bound | — | G-soak |
| K11 | `f873d7d:tests/test_soak_example.py` L92 | A semantics §5: capacity is reached but never exceeded | gap | [capacity tests] are smaller; peak-at-20 full-estate check absent | U | G-soak |
| K12 | `f873d7d:tests/test_soak_example.py` L92 | A semantics §5: overload enters QUE_WAIT | ported | [test_oracle.py:9203](../tests/test_oracle.py#L9203) Positive-priority overload queues; full-size composition remains G-soak. | U | G-soak |
| K13 | `f873d7d:examples/soak/compressed.py` L176 | Measurement (O): inject an exact retry and record deduped count, with no expected-count assertion | gap | No v2 soak traffic measurement; source admission retry tests do not replace workload measurement | — | G-soak |
| K14 | `f873d7d:examples/soak/compressed.py` L176 | Measurement (O): inject a conflicting request ID and record refusal count, with no expected-count assertion | gap | No v2 soak traffic measurement; source collision tests are separate coverage | — | G-soak |
| K15 | `f873d7d:examples/soak/compressed.py` L176 | Measurement stimulus (O): inject hold and release into the soak trace, without asserting the resulting flags | gap | No v2 soak traffic/trace measurement; SEM-21 tests are separate coverage | — | G-soak |
| K16 | `f873d7d:examples/soak/compressed.py` L249 | O: each synthetic midnight seals and audits a period | retired | Charter excludes seals and period roll | — | See K17 |
| K17 | `f873d7d:examples/soak/compressed.py` L294 | Measurement (O): retain multi-day replay cost and state-growth records | gap | Timing was measurement, not a fixed pass bound; v2 needs SQLite/retention budgets | — | G-soak |
| K18 | `f873d7d:examples/soak/soak.sh` L1 | Measurement (O): retain real-clock RSS, CPU, descriptors and query latency | gap | No v2 real-clock sampler or declared long-run budgets | — | G-soak |
| K19 | `f873d7d:tests/test_soak_example.py` L145 | O: descriptor count excludes lsof non-descriptor rows | gap | Sampler deleted; independent resource accounting needed if ported | — | G-soak |
| K20 | `f873d7d:tests/test_soak_example.py` L151 | O: open-segment bytes are distinct from whole-WAL bytes | retired | Charter/store replacement has no period segments; measure SQLite files instead | — | G-soak |
| K21 | `f873d7d:tests/test_soak_example.py` L159 | O: live commands are counted separately from boxes and queued jobs | gap | No v2 capacity sampler | — | G-soak |
| K22 | `f873d7d:tests/test_soak_example.py` L234 | O: a failed status read is not proof that processes drained | ported | [test_restart.py:385](../tests/test_restart.py#L385) | U | G-restart |
| K23 | `f873d7d:tests/test_soak_example.py` L243 | O: absent recorder is not proof that residual commands are gone | ported | [test_restart.py:594](../tests/test_restart.py#L594) | U | G-restart |
| K24 | `f873d7d:tests/test_soak_example.py` L251 | O: bounded shutdown reports that drain did not finish | ported | [test_runner_attempts.py:1046](../tests/test_runner_attempts.py#L1046) | U | G-restart |
| K25 | `f873d7d:tests/test_soak_example.py` L261 | O: shutdown stops waiting when engine exit budget expires | ported | [test_runner_attempts.py:1046](../tests/test_runner_attempts.py#L1046) | U | G-restart |
| K26 | `f873d7d:tests/test_soak_example.py` L276 | O: shutdown refusal or uncertainty is propagated without blind force | ported | [test_cli_control_branches.py:376](../tests/test_cli_control_branches.py#L376) A delivered failure is not sent again; the CLI returns its failure exit. | U | G-restart |

## Preserve installed-artifact assertions

| ID | Old commit, path and line | Assertion | Disposition | V2 replacement or retirement reason | CI lane | Outstanding gap |
| --- | --- | --- | --- | --- | --- | --- |
| Z01 | `f873d7d:scripts/release_smoke.sh` L153 | O: locked dependency closure installs with required hashes | gap | [offline prepare] retains hashed installation code, but no equivalent real-package matrix test | — | G-artifact |
| Z02 | `f873d7d:scripts/release_smoke.sh` L153 | O: candidate wheel installs without dependency re-resolution | ported | [drill-steps.sh:395](../examples/nightbank/deploy/drill-steps.sh#L395) | S | G-artifact |
| Z03 | `f873d7d:scripts/release_smoke.sh` L153 | O: pip check finds no missing/conflicting requirements | gap | [offline prepare] runs pip check, but retained tests use a synthetic wheel | — | G-artifact |
| Z04 | `f873d7d:scripts/release_smoke.sh` L153 | O: import resolves inside the fresh venv | gap | [offline smoke] asserts candidate import; no tested real-wheel replacement lane | — | G-artifact |
| Z05 | `f873d7d:scripts/release_smoke.sh` L153 | O: installation is not editable | gap | [offline smoke] rejects editable installs; no tested real-wheel replacement lane | — | G-artifact |
| Z06 | `f873d7d:scripts/release_smoke.sh` L153 | O: py.typed is installed | gap | No replacement installed file assertion | — | G-artifact |
| Z07 | `f873d7d:scripts/release_smoke.sh` L153 | O: all four declared license files are installed | gap | No replacement installed license payload assertion | — | G-artifact |
| Z08 | `f873d7d:scripts/release_smoke.sh` L153 | O: installed CLI help exits 0 | ported | [test_installed_cli.py:63](../tests/test_installed_cli.py#L63) Opt-in; the stale public-help expectation is a Known bug. | M | G-artifact |
| Z09 | `f873d7d:scripts/release_smoke.sh` L208 | O: UC bundle has records and candidate tool version | retired | Charter excludes UC/another scheduler as target | — | — |
| Z10 | `f873d7d:scripts/release_smoke.sh` L208 | O: html embeds installed Mermaid/ELK bytes | gap | Source browser tests do not prove installed wheel assets | — | G-artifact |
| Z11 | `f873d7d:scripts/release_smoke.sh` L208 | O: html-chart embeds installed Mermaid/ELK bytes | gap | No installed real-wheel replacement | — | G-artifact |
| Z12 | `f873d7d:scripts/release_smoke.sh` L208 | O: explore embeds installed Cytoscape/custom-element bytes | gap | Source browser tests do not prove installed wheel assets | — | G-artifact |
| Z13 | `f873d7d:scripts/release_smoke.sh` L272 | O: installed engine binds its socket | ported | [drill-steps.sh:122](../examples/nightbank/deploy/drill-steps.sh#L122) | S | G-artifact |
| Z14 | `f873d7d:scripts/release_smoke.sh` L272 | O: installed wrapper runs command to SUCCESS | gap | Workflow CI uses a source/editable install; the service drill tests interrupted commands. No matching real-wheel command-to-SUCCESS smoke is identified. | — | G-artifact |
| Z15 | `f873d7d:scripts/release_smoke.sh` L272 | O: command stdout reaches its run log | gap | No equivalent real-wheel smoke stdout sentinel check | — | G-artifact |
| Z16 | `f873d7d:scripts/release_smoke.sh` L272 | O: supervisor imports from the installed package | retired | Charter excludes supervisor; installed wrapper provenance still needs G-artifact | — | G-artifact |
| Z17 | `f873d7d:scripts/release_smoke.sh` L272 | O: engine stops on SIGINT with exit 0 | gap | Engine-stop and ExecStop drains are tested; no equivalent installed SIGINT plus process exit-0 assertion identified | — | G-artifact |
| Z18 | `f873d7d:scripts/release_smoke.sh` L333 | O: installed ui extra mounts on a live socket | gap | Installed recovery tests execute a generated control command; they do not mount the installed TUI. No equivalent live-socket UI mount smoke is identified. | — | G-artifact |
| Z19 | `f873d7d:scripts/release_smoke.sh` L333 | O: base-only install refuses ui with exit 2 and names the extra | gap | No replacement core-only candidate smoke row in release CI | — | G-artifact |
| Z20 | `f873d7d:scripts/release_smoke.sh` L383 | O: sdist rebuild matches direct wheel payload except RECORD | gap | Release builds both archives but does not compare rebuilt payloads | — | G-artifact |

## Preserve clock and semantic composition checks

| ID | Old commit, path and line | Assertion | Disposition | V2 replacement or retirement reason | CI lane | Outstanding gap |
| --- | --- | --- | --- | --- | --- | --- |
| T01 | `f873d7d:examples/dst-drill/compare.py` L240 | A SEM-32/35: scheduled starts have the expected job and tick instant | ported | [test_dst_drill.py:231](../tests/test_dst_drill.py#L231) [Comparator](../examples/dst-drill/compare.py#L284); source fixture checks the verdict; D executes it in the clock drill. | U, D | G-semantic |
| T02 | `f873d7d:examples/dst-drill/compare.py` L156 | O: maximum dispatch lag stays inside the declared bound | ported | [test_dst_drill.py:562](../tests/test_dst_drill.py#L562) [Comparator](../examples/dst-drill/compare.py#L150); source fixture checks the verdict; D executes it in the clock drill. | U, D | — |
| T03 | `f873d7d:examples/dst-drill/compare.py` L156 | O: median dispatch lag stays inside the declared bound | ported | [test_dst_drill.py:582](../tests/test_dst_drill.py#L582) [Comparator](../examples/dst-drill/compare.py#L150); source fixture checks the verdict; D executes it in the clock drill. | U, D | — |
| T04 | `f873d7d:examples/dst-drill/compare.py` L285 | A SEM-01: producer completion starts the eligible dependent | ported | [test_dst_drill.py:236](../tests/test_dst_drill.py#L236) [Comparator](../examples/dst-drill/compare.py#L323); source fixture checks the verdict; D executes it in the clock drill. | U, D | G-semantic |
| T05 | `f873d7d:examples/dst-drill/compare.py` L285 | O: real follow-on latency stays inside the declared bound | ported | [test_dst_drill.py:236](../tests/test_dst_drill.py#L236) [Comparator](../examples/dst-drill/compare.py#L323); source fixture checks the verdict; D executes it in the clock drill. | U, D | — |
| T06 | `f873d7d:examples/dst-drill/compare.py` L240 | A SEM-34: must alarm fires at its deadline without changing scheduling | ported | [test_dst_drill.py:242](../tests/test_dst_drill.py#L242) [Comparator](../examples/dst-drill/compare.py#L284); source fixture checks the verdict; D executes it in the clock drill. | U, D | G-semantic |
| T07 | `f873d7d:examples/dst-drill/compare.py` L195 | A: term_run_time emits kill at its deadline under semantics §5 | ported | [test_dst_drill.py:242](../tests/test_dst_drill.py#L242) [Comparator](../examples/dst-drill/compare.py#L284); source fixture checks the verdict; D executes it in the clock drill. | U, D | G-semantic |
| T08 | `f873d7d:examples/dst-drill/compare.py` L195 | O: actual wrapper signal occurs within its declared kill lag | ported | [test_dst_drill.py:643](../tests/test_dst_drill.py#L643) [Comparator](../examples/dst-drill/compare.py#L204); source fixture checks the verdict; D executes it in the clock drill. | U, D | — |
| T09 | `f873d7d:examples/dst-drill/compare.py` L285 | A SEM-09/11: final statuses match the semantic expectation | ported | [test_dst_drill.py:242](../tests/test_dst_drill.py#L242) [Comparator](../examples/dst-drill/compare.py#L323); source fixture checks the verdict; D executes it in the clock drill. | U, D | G-semantic |
| T10 | `f873d7d:examples/dst-drill/compare.py` L240 | O: tick missed during downtime is dropped and never fired late | ported | [test_dst_drill.py:253](../tests/test_dst_drill.py#L253) [Comparator](../examples/dst-drill/compare.py#L263); source fixture checks the verdict; D executes it in the clock drill. | U, D | G-semantic |
| T11 | `f873d7d:examples/dst-drill/compare.py` L572 | A SEM-32/35: DST start-time finding matches its stated instant and pass | ported | [test_dst_drill.py:526](../tests/test_dst_drill.py#L526) [Comparator](../examples/dst-drill/compare.py#L703); source fixture checks the verdict; D executes it in the clock drill. | U, D | G-semantic |
| T12 | `f873d7d:examples/dst-drill/compare.py` L572 | A SEM-33: each stated window opening contains the permitted start | ported | [test_dst_drill.py:526](../tests/test_dst_drill.py#L526) [Comparator](../examples/dst-drill/compare.py#L703); source fixture checks the verdict; D executes it in the clock drill. | U, D | G-semantic |
| T13 | `f873d7d:examples/dst-drill/compare.py` L572 | A SEM-32: start_mins follows gap/overlap multiplicity | ported | [test_dst_drill.py:526](../tests/test_dst_drill.py#L526) [Comparator](../examples/dst-drill/compare.py#L703); source fixture checks the verdict; D executes it in the clock drill. | U, D | G-semantic |
| T14 | `f873d7d:examples/dst-drill/compare.py` L572 | A SEM-34: DST alarm finding matches actual deadline presence | ported | [test_dst_drill.py:526](../tests/test_dst_drill.py#L526) [Comparator](../examples/dst-drill/compare.py#L703); source fixture checks the verdict; D executes it in the clock drill. | U, D | G-semantic |

## Preserve workflow effects and checker independence

| ID | Old commit, path and line | Assertion | Disposition | V2 replacement or retirement reason | CI lane | Outstanding gap |
| --- | --- | --- | --- | --- | --- | --- |
| W01 | `f873d7d:examples/workflows/tests/test_fulfilment.py` L40 | O: final stock equals the fixture's exact expected quantities | kept | [test_fulfilment.py:40](../examples/workflows/tests/test_fulfilment.py#L40) | W | — |
| W02 | `f873d7d:examples/workflows/tests/test_fulfilment.py` L40 | O: carrier has exactly three committed effects | kept | [test_fulfilment.py:40](../examples/workflows/tests/test_fulfilment.py#L40) | W | — |
| W03 | `f873d7d:examples/workflows/tests/test_fulfilment.py` L40 | O: conflicting identity returns HTTP 409 | kept | [test_fulfilment.py:40](../examples/workflows/tests/test_fulfilment.py#L40) | W | — |
| W04 | `f873d7d:examples/workflows/tests/test_fulfilment.py` L40 | A SEM-10/11: box and all scheduled jobs finish SUCCESS once | kept | [test_fulfilment.py:40](../examples/workflows/tests/test_fulfilment.py#L40) | W | — |
| W05 | `f873d7d:examples/workflows/tests/test_fulfilment.py` L40 | O: worker attempts are counted independently of scheduler history | kept | [test_fulfilment.py:40](../examples/workflows/tests/test_fulfilment.py#L40) | W | — |
| W06 | `f873d7d:examples/workflows/tests/test_fulfilment.py` L62 | O: committed effect with lost reply is not duplicated by manual rerun | kept | [test_fulfilment.py:62](../examples/workflows/tests/test_fulfilment.py#L62) | W | — |
| W07 | `f873d7d:examples/workflows/tests/test_fulfilment.py` L62 | A SEM-09/23: failed label run then manual force gives FAILURE then SUCCESS | kept | [test_fulfilment.py:62](../examples/workflows/tests/test_fulfilment.py#L62) | W | — |
| W08 | `f873d7d:examples/workflows/tests/test_fulfilment.py` L62 | O: only the failed worker is invoked twice | kept | [test_fulfilment.py:62](../examples/workflows/tests/test_fulfilment.py#L62) | W | — |
| W09 | `f873d7d:examples/workflows/tests/test_fulfilment.py` L93 | O: checker rejects stock damage even when conservation still holds | kept | [test_fulfilment.py:93](../examples/workflows/tests/test_fulfilment.py#L93) | W | — |
| W10 | `f873d7d:examples/workflows/tests/test_fulfilment.py` L111 | O: checker rejects changed idempotency operation payload | kept | [test_fulfilment.py:111](../examples/workflows/tests/test_fulfilment.py#L111) | W | — |
| W11 | `f873d7d:examples/workflows/tests/test_media.py` L56 | O: published artifacts match the fixture's independent content checks | kept | [test_media.py:56](../examples/workflows/tests/test_media.py#L56) | W | — |
| W12 | `f873d7d:examples/workflows/tests/test_media.py` L56 | A SEM-09: each command's first run succeeds | kept | [test_media.py:56](../examples/workflows/tests/test_media.py#L56) | W | — |
| W13 | `f873d7d:examples/workflows/tests/test_media.py` L56 | O: every real worker action is invoked once | kept | [test_media.py:56](../examples/workflows/tests/test_media.py#L56) | W | — |
| W14 | `f873d7d:examples/workflows/tests/test_media.py` L79 | O: stale publication is refused | kept | [test_media.py:79](../examples/workflows/tests/test_media.py#L79) | W | — |
| W15 | `f873d7d:examples/workflows/tests/test_media.py` L79 | A SEM-09/23: failed rendition is manually rerun to SUCCESS | kept | [test_media.py:79](../examples/workflows/tests/test_media.py#L79) | W | — |
| W16 | `f873d7d:examples/workflows/tests/test_media.py` L79 | O: only failed rendition's worker is invoked twice | kept | [test_media.py:79](../examples/workflows/tests/test_media.py#L79) | W | — |
| W17 | `f873d7d:examples/workflows/tests/test_media.py` L113 | O: checker rejects served bytes that disagree with manifest | kept | [test_media.py:113](../examples/workflows/tests/test_media.py#L113) | W | — |
| W18 | `f873d7d:examples/workflows/tests/test_media.py` L149 | O: checker rejects older content even after hashes are recomputed | kept | [test_media.py:149](../examples/workflows/tests/test_media.py#L149) | W | — |
| W19 | `f873d7d:examples/workflows/tests/test_energy.py` L56 | O: late correction preserves initial publication and exact result totals | gap | Deleted energy workload has no v2 correction composition scenario | — | G-correction |
| W20 | `f873d7d:examples/workflows/tests/test_energy.py` L56 | A SEM-09/23: failed ingest then manual rerun has exact two-run history | ported | [test_fulfilment.py:62](../examples/workflows/tests/test_fulfilment.py#L62) | W | G-correction |
| W21 | `f873d7d:examples/workflows/tests/test_energy.py` L86 | O: correction crosses a committed period boundary | retired | Charter excludes period boundaries; correction safety is W19 | — | — |
| W22 | `f873d7d:examples/workflows/tests/test_energy.py` L86 | O: force-seal records the overridden retry horizon | retired | Charter excludes force-seal and its gate | — | — |
| W23 | `f873d7d:examples/workflows/tests/test_energy.py` L86 | O: conflicting reading revision is refused | gap | No v2 correction worker/checker scenario | — | G-correction |
| W24 | `f873d7d:examples/workflows/tests/test_energy.py` L86 | O: noncanonical interval timestamp is refused | gap | No v2 correction worker/checker scenario | — | G-correction |
| W25 | `f873d7d:examples/workflows/tests/test_energy.py` L86 | O: stale reading in a new snapshot is refused | gap | No v2 correction worker/checker scenario | — | G-correction |
| W26 | `f873d7d:examples/workflows/tests/test_energy.py` L138 | O: checker rejects damaged correction delta | gap | No replacement damage-sensitive correction checker | — | G-correction |

## Preserve wargame input and ownership checks

| ID | Old commit, path and line | Assertion | Disposition | V2 replacement or retirement reason | CI lane | Outstanding gap |
| --- | --- | --- | --- | --- | --- | --- |
| H01 | `f873d7d:scripts/wargame.sh` L65 | Unasserted guard (O): v1 refuses a foreign-owned container | gap | V1 guard has no foreign-owner test; v2 keeps the guard without a regression test | M | G-wargame |
| H02 | `f873d7d:scripts/wargame.sh` L65 | Unasserted guard (O): v1 build/start refuses a foreign-owned image | gap | V1 guard has no foreign-owner test; v2 keeps the guard without a regression test | M | G-wargame |
| H03 | `f873d7d:tests/test_nightbank_deploy.py` L1208 | O: start preserves an existing /work checkout | gap | V2 start keeps /work in code; old stub-provider assertion is deleted | M | G-wargame |
| H04 | `f873d7d:tests/test_nightbank_deploy.py` L1208 | O: initial delivery copies only tracked/indexed checkout paths | gap | V2 wargame also selects untracked non-ignored files; tracked-only provenance is not preserved | M | G-wargame |
| H05 | `f873d7d:tests/test_nightbank_deploy.py` L1208 | O: deliver refuses without a running container | gap | V2 deliver checks running state in code; old stub-provider assertion is deleted | M | G-wargame |
| H06 | `f873d7d:tests/test_nightbank_deploy.py` L1208 | O: delivery publishes a complete replacement rather than partial bundle | gap | V2 deliver uses direct podman cp; old rename-based bundle regression is absent | M | G-wargame |

## Preserve composed estate and rehearsal obligations

| ID | Old commit, path and line | Assertion | Disposition | V2 replacement or retirement reason | CI lane | Outstanding gap |
| --- | --- | --- | --- | --- | --- | --- |
| N01 | `f873d7d:tests/test_nightbank_boundary.py` L262 | O: globals survive a durable restart boundary | ported | [test_runner_store.py:1067](../tests/test_runner_store.py#L1067) | U | G-restore |
| N02 | `f873d7d:tests/test_nightbank_boundary.py` L262 | O: completed job status and run count survive restart | ported | [test_store_wiring.py:581](../tests/test_store_wiring.py#L581) | U | G-history |
| N03 | `f873d7d:tests/test_nightbank_boundary.py` L399 | O: ice flags survive recovery | gap | Snapshot equivalence is tested, but no explicit iced-row recovery witness identified in the cited test | — | G-restore |
| N04 | `f873d7d:tests/test_nightbank_boundary.py` L262 | O: seal refuses inside the request retry horizon | retired | Charter excludes seals and their retry-horizon gate | — | — |
| N05 | `f873d7d:tests/test_nightbank_boundary.py` L262 | O: forced seal binds actor, digest and override receipt | retired | Charter excludes seal/attestation records | — | — |
| N06 | `f873d7d:tests/test_nightbank_boundary.py` L262 | O: spool deletion leaves retained run history readable | gap | New store retains history, but no end-to-end artifact-removal replacement is identified | — | G-history |
| N07 | `f873d7d:tests/test_nightbank_boundary.py` L443 | O: prune dry-run removes nothing | retired | Charter excludes the operator period-prune verb; store retention is separate | — | G-soak |
| N08 | `f873d7d:tests/test_nightbank_boundary.py` L443 | O: active work prevents deletion of evidence it still needs | ported | [test_store_wiring.py:639](../tests/test_store_wiring.py#L639) | U | G-history |
| N09 | `f873d7d:tests/test_nightbank_boundary.py` L487 | O: retired historical state refuses by name | ported | [test_restart.py:550](../tests/test_restart.py#L550) | U | G-upgrade |
| N10 | `f873d7d:tests/test_nightbank_boundary.py` L535 | O: unattested closing period cannot roll | retired | Charter excludes roll and attestation | — | — |
| N11 | `f873d7d:tests/test_nightbank_boundary.py` L588 | O: estate-wide readers cover both registered roots | retired | Charter uses one root without lineage | — | — |
| N12 | `f873d7d:tests/test_nightbank_boundary.py` L646 | O: crashed roll's claim is released by an explicit reclaim | retired | Charter excludes roll claims and reclaim | — | — |
| N13 | `f873d7d:tests/test_nightbank_boundary.py` L1195 | O: unchanged work retains its logical run through configuration adoption | ported | [test_update_wiring.py:348](../tests/test_update_wiring.py#L348) | U | — |
| N14 | `f873d7d:tests/test_nightbank_boundary.py` L1195 | O: queued waiter retains its rank through state transfer | ported | [test_update_wiring.py:133](../tests/test_update_wiring.py#L133) The complete queued row equals its uninterrupted counterpart after snapshot restore. | U | G-history |
| N15 | `f873d7d:tests/test_nightbank_boundary.py` L1195 | O: held capacity remains reserved through state transfer | gap | Consumed depletable units are tested; the old live reservation vector through state transfer is not the same assertion | — | G-history |
| N16 | `f873d7d:tests/test_nightbank_boundary.py` L1195 | O: deferred timer remains pending through state transfer | gap | Tick replacement is tested; the old deferred run_window timer needs an explicit transfer/restart assertion | — | G-config |
| N17 | `f873d7d:tests/test_nightbank_boundary.py` L1195 | O: pending launch intent survives crash without double launch | ported | [test_store_wiring.py:497](../tests/test_store_wiring.py#L497) | U | G-restart |
| N18 | `f873d7d:tests/test_nightbank_boundary.py` L1195 | O: filewatch evidence prefix is preserved without a second watch start | gap | V2 restart reopens watches under its recovery rule; exact-prefix compatibility needs explicit coverage scoping | — | G-restart |
| N19 | `f873d7d:tests/test_nightbank_boundary.py` L1195 | O: old baseline request is refused after successor period opens | retired | Charter excludes period baselines; request identity stays in S43 | — | — |
| N20 | `f873d7d:tests/test_nightbank_boundary.py` L1481 | O: every timer exactly at a boundary is processed once | gap | Update tests cover owed ticks; old arbitrary deadline timers at an exact boundary need separate coverage | — | G-config |
| N21 | `f873d7d:tests/test_nightbank_boundary.py` L1624 | O: unsafe changed work cannot launch under an unaccepted definition | ported | [test_update_wiring.py:348](../tests/test_update_wiring.py#L348) | U | G-update |
| N22 | `f873d7d:tests/test_nightbank_boundary.py` L1652 | O: every change in a live box's forward closure refuses | retired | Charter permits changed same-member boxes in place and targeted job stops; structural safety is N23 | — | — |
| N23 | `f873d7d:tests/test_nightbank_boundary.py` L1652 | O: refused live-member edit leaves the engine, member and durable inputs untouched | gap | V2 permits some member edits; mutation-free refusal needs a tested structural-refusal counterpart | — | G-update |
| N24 | `f873d7d:tests/test_nightbank_boundary.py` L1681 | O: supervisor incarnation change prevents seal proof | retired | Charter excludes supervisor incarnation and seal proof | — | — |
| N25 | `f873d7d:tests/test_nightbank_boundary.py` L1770 | O: applied launch with no process record cannot be assumed complete | ported | [test_runner_attempts.py:682](../tests/test_runner_attempts.py#L682) | U | G-restart |
| N26 | `f873d7d:tests/test_nightbank_example.py` L70 | A SEM-10/11: all regional and risk boxes complete SUCCESS | kept | [test_nightbank_example.py:66](../tests/test_nightbank_example.py#L66) | U | — |
| N27 | `f873d7d:tests/test_nightbank_example.py` L70 | A SEM-21: approval stays held until operator release | kept | [test_nightbank_example.py:66](../tests/test_nightbank_example.py#L66) | U | — |
| N28 | `f873d7d:tests/test_nightbank_example.py` L70 | A semantics §5: tape-drive contention queues the archive job | kept | [test_nightbank_example.py:66](../tests/test_nightbank_example.py#L66) | U | — |
| N29 | `f873d7d:tests/test_nightbank_example.py` L70 | A semantics §5: lower-numbered eligible priority waiter starts first | kept | [test_nightbank_example.py:66](../tests/test_nightbank_example.py#L66) | U | — |
| N30 | `f873d7d:tests/test_nightbank_example.py` L124 | O: large synthetic Nightbank estate regenerates byte-stably | kept | [test_nightbank_example.py:120](../tests/test_nightbank_example.py#L120) | U | G-soak |
| N31 | `f873d7d:tests/test_nightbank_example.py` L278 | O: synthetic night remains coherent across injected crash/fault sequences | kept | [test_nightbank_example.py:255](../tests/test_nightbank_example.py#L255) | U | G-soak |
| N32 | `f873d7d:tests/test_nightbank_example.py` L305 | O: synthetic night completes under the harness | kept | [test_nightbank_example.py:282](../tests/test_nightbank_example.py#L282) | U | G-soak |
| N33 | `f873d7d:tests/test_nightbank_example.py` L389 | O: idempotent synthetic publication recovers at every declared crash point | kept | [test_nightbank_example.py:366](../tests/test_nightbank_example.py#L366) | U | — |
| N34 | `f873d7d:tests/test_file_processing_runner.py` L207 | O: delivered files are archived in admission order | kept | [test_file_processing_runner.py:208](../tests/test_file_processing_runner.py#L208) | U | — |
| N35 | `f873d7d:tests/test_file_processing_runner.py` L207 | O: stop with a claimed delivery leaves it recoverable | ported | [test_file_processing_runner.py:208](../tests/test_file_processing_runner.py#L208) | U | G-restart |
| N36 | `f873d7d:tests/test_file_processing_runner.py` L207 | O: recovery never repeats a handler call already committed | kept | [test_file_processing_runner.py:208](../tests/test_file_processing_runner.py#L208) | U | — |
| N37 | `f873d7d:tests/test_file_processing_runner.py` L207 | O: recovered output inode, mtime and bytes stay unchanged | kept | [test_file_processing_runner.py:208](../tests/test_file_processing_runner.py#L208) | U | — |
| N38 | `f873d7d:tests/test_file_processing_runner.py` L207 | O: toxic delivery is explicitly rejected | kept | [test_file_processing_runner.py:208](../tests/test_file_processing_runner.py#L208) | U | — |
| N39 | `f873d7d:tests/test_file_processing_runner.py` L207 | O: completion order follows admission order across runs | kept | [test_file_processing_runner.py:208](../tests/test_file_processing_runner.py#L208) | U | — |
| N40 | `f873d7d:tests/test_file_processing_runner.py` L269 | O: new delivery during a processor run gets a later cycle without an operator | kept | [test_file_processing_runner.py:270](../tests/test_file_processing_runner.py#L270) | U | — |
| N41 | `f873d7d:tests/test_file_processing_runner.py` L349 | O: no progress starts no second cycle | kept | [test_file_processing_runner.py:350](../tests/test_file_processing_runner.py#L350) | U | — |
| N42 | `f873d7d:tests/test_file_processing_runner.py` L421 | O: unreachable stale claim starts no second cycle | kept | [test_file_processing_runner.py:422](../tests/test_file_processing_runner.py#L422) | U | — |
| N43 | `f873d7d:tests/test_rehearse_check.py` L902 | O: cadence deviation exits 3 | kept | [test_rehearse_check.py:917](../tests/test_rehearse_check.py#L917) | U | G-semantic |
| N44 | `f873d7d:tests/test_rehearse_check.py` L924 | O: declared policy exception is explicit and exits 0 | kept | [test_rehearse_check.py:939](../tests/test_rehearse_check.py#L939) | U | — |
| N45 | `f873d7d:tests/test_rehearse_check.py` L963 | O: unsupported status mutation in cadence scenario refuses | kept | [test_rehearse_check.py:978](../tests/test_rehearse_check.py#L978) | U | — |
| N46 | `f873d7d:tests/test_rehearse_check.py` L1207 | O: producer failure sweep reports suppressed success consumers | kept | [test_rehearse_check.py:1222](../tests/test_rehearse_check.py#L1222) | U | G-semantic |
| N47 | `f873d7d:tests/test_rehearse_check.py` L1207 | A SEM-01/02: producer failure wakes failure-conditioned consumers | kept | [test_rehearse_check.py:1222](../tests/test_rehearse_check.py#L1222) | U | G-semantic |
| N48 | `f873d7d:tests/test_rehearse_check.py` L1455 | O: flag sweep exposes the stale global-gated case | kept | [test_rehearse_check.py:1470](../tests/test_rehearse_check.py#L1470) | U | G-semantic |
| N49 | `f873d7d:tests/test_rehearse_check.py` L1492 | O: compound global gates use whole-component assignments | kept | [test_rehearse_check.py:1507](../tests/test_rehearse_check.py#L1507) | U | — |
| N50 | `f873d7d:tests/test_rehearse_check.py` L1643 | O: sweep without cadence check refuses | kept | [test_rehearse_check.py:1658](../tests/test_rehearse_check.py#L1658) | U | — |
| N51 | `f873d7d:tests/test_rehearse_check.py` L1651 | O: sweep with durable run-root refuses | kept | [test_rehearse_check.py:1666](../tests/test_rehearse_check.py#L1666) | U | — |
| N52 | `f873d7d:tests/test_rehearse_check.py` L1814 | O: JSON reports both failure and flag findings distinctly | kept | [test_rehearse_check.py:1829](../tests/test_rehearse_check.py#L1829) | U | — |
| N53 | `f873d7d:tests/test_rehearse_check.py` L1731 | O: progress stays on stderr without corrupting JSON stdout | kept | [test_rehearse_check.py:1746](../tests/test_rehearse_check.py#L1746) | U | — |

## Preserve the printed operator recipes

These rows come from `f873d7d:tests/test_operator_recipes.py` (DL-268). A functional
engine test is partial evidence when it does not extract and execute the
printed command. Such rows stay gaps even when the underlying verb is tested.
The source line identifies the test or shared assertion containing the rule.

| ID | Old commit, path and line | Assertion | Disposition | V2 replacement or retirement reason | CI lane | Outstanding gap |
| --- | --- | --- | --- | --- | --- | --- |
| D01 | `f873d7d:tests/test_operator_recipes.py` L72 | O: each recipe marker opens its fence immediately | gap | No v2 deploy-page fence/recipe extractor assertion | — | G-recipes |
| D02 | `f873d7d:tests/test_operator_recipes.py` L72 | O: each marked kind/name occurs once | gap | No v2 deploy-page unique-marker assertion | — | G-recipes |
| D03 | `f873d7d:tests/test_operator_recipes.py` L244 | O: every marked block has a nonempty body | gap | No v2 printed-block completeness assertion | — | G-recipes |
| D04 | `f873d7d:tests/test_operator_recipes.py` L244 | O: exactly the two v1 process/decision diagrams are present | retired | Charter excludes that supervisor/period topology; generic diagram agreement is D39 | — | — |
| D05 | `f873d7d:tests/test_operator_recipes.py` L250 | O: every recipe the service drill names exists in the runbook | gap | V2 drill implements procedures without comparing their printed blocks | — | G-recipes |
| D06 | `f873d7d:tests/test_operator_recipes.py` L250 | O: drill recipe-use set equals the declared expected set | gap | No v2 recipe-use set assertion | — | G-recipes |
| D07 | `f873d7d:tests/test_operator_recipes.py` L299 | O: every printed recipe is executed or compared | gap | No v2 coverage register for deploy.md command blocks | — | G-recipes |
| D08 | `f873d7d:tests/test_operator_recipes.py` L306 | O: every recipe shell body parses | gap | No v2 sh -n check of deploy.md command blocks | — | G-recipes |
| D09 | `f873d7d:tests/test_operator_recipes.py` L306 | O: expanded recipe shell contains no unresolved template form | gap | No v2 recipe expansion/syntax assertion | — | G-recipes |
| D10 | `f873d7d:tests/test_operator_recipes.py` L345 | O: reboot hold takes disable and releases with enable | gap | No v2 printed disable/enable recipe comparison | — | G-recipes |
| D11 | `f873d7d:tests/test_operator_recipes.py` L345 | O: each temporary reboot hold has a release at its procedure | gap | No v2 printed hold/release pairing assertion | — | G-recipes |
| D12 | `f873d7d:tests/test_operator_recipes.py` L345 | O: retirement explicitly keeps its reboot hold | gap | One-unit retirement procedure needs a retained-disable assertion | — | G-retire |
| D13 | `f873d7d:tests/test_operator_recipes.py` L374 | O: no operator recipe starts a systemd target | gap | No v2 deploy-page target-start guard | — | G-recipes |
| D14 | `f873d7d:tests/test_operator_recipes.py` L399 | O: installed service account equals the unit's User | gap | V2 service install is exercised, but no comparison of the printed account with the unit | — | G-recipes |
| D15 | `f873d7d:tests/test_operator_recipes.py` L399 | O: executable parent directories are root-owned before launcher installation | retired | Charter excludes the v1 launcher installation; v2 unit/config ownership remains D14/D16/D18 and G-package | — | — |
| D16 | `f873d7d:tests/test_operator_recipes.py` L399 | O: printed run-root installation uses the service owner and mode 0700 | gap | Source mode checks do not execute the printed install command | — | G-recipes |
| D17 | `f873d7d:tests/test_operator_recipes.py` L399 | O: printed access-map installation uses service owner and mode 0600 | gap | No v2 printed access-map installation assertion | — | G-recipes |
| D18 | `f873d7d:tests/test_operator_recipes.py` L399 | O: install recipe selects every shipped unit file | gap | V2 ships one unit; no printed-command versus shipped-unit comparison | — | G-recipes |
| D19 | `f873d7d:tests/test_operator_recipes.py` L399 | O: daemon-reload precedes unit enablement | gap | V2 drill follows that order; no printed-recipe order assertion | — | G-recipes |
| D20 | `f873d7d:tests/test_operator_recipes.py` L399 | O: enablement names every shipped unit once | gap | No v2 printed enablement completeness assertion | — | G-recipes |
| D21 | `f873d7d:tests/test_operator_recipes.py` L399 | O: service-print invokes the configured launcher --print | retired | Charter excludes the v1 launcher and --print procedure | — | — |
| D22 | `f873d7d:tests/test_operator_recipes.py` L437 | O: service-start starts the engine and reads both units | retired | Charter has one unit; public status availability survives in S21 and printed command checking in D05 | — | — |
| D23 | `f873d7d:tests/test_operator_recipes.py` L444 | O: retirement retains installed files before removing them | gap | No v2 one-unit retirement/retention recipe assertion | — | G-retire |
| D24 | `f873d7d:tests/test_operator_recipes.py` L444 | O: retirement resets failed units before removing their files | gap | No v2 printed retirement ordering assertion | — | G-retire |
| D25 | `f873d7d:tests/test_operator_recipes.py` L444 | O: retirement disables every shipped unit | gap | No v2 one-unit retirement acceptance | — | G-retire |
| D26 | `f873d7d:tests/test_operator_recipes.py` L444 | O: retirement reloads systemd and reads each retired unit's load/active state | gap | No v2 printed retirement verification assertion | — | G-retire |
| D27 | `f873d7d:tests/test_operator_recipes.py` L477 | O: retirement removes the installed release sudo grant after retaining it | retired | Charter excludes the release service and its grant | — | — |
| D28 | `f873d7d:tests/test_operator_recipes.py` L497 | O: operator variables equal the launcher's exported values | retired | Charter excludes the v1 launcher's vars/ACTIVE interface; v2 config/command consistency is G-recipes | — | — |
| D29 | `f873d7d:tests/test_operator_recipes.py` L513 | O: job-open starts through the engine unit's ExecStart | gap | No v2 printed definition-change/start command comparison | — | G-recipes |
| D30 | `f873d7d:tests/test_operator_recipes.py` L526 | O: seal restates the selected manifest execution profile | retired | Charter excludes seals and their successor profile | — | — |
| D31 | `f873d7d:tests/test_operator_recipes.py` L526 | O: pre-change lint and rehearsal read the same selected manifest | gap | No v2 printed pre-change input-agreement assertion | — | G-recipes |
| D32 | `f873d7d:tests/test_operator_recipes.py` L581 | O: the CM play is one explicit play with the expected two-unit task list | retired | Charter excludes the two-unit supervisor play | — | — |
| D33 | `f873d7d:tests/test_operator_recipes.py` L581 | O: CM stops supervisor before engine and starts supervisor first | retired | Charter excludes the second unit; bounded engine drain remains S49 | — | — |
| D34 | `f873d7d:tests/test_operator_recipes.py` L581 | O: CM readiness wait has a finite retry/delay budget | gap | No v2 printed CM/readiness recipe assertion | — | G-recipes |
| D35 | `f873d7d:tests/test_operator_recipes.py` L581 | O: readiness query runs as the service account | gap | No v2 printed readiness-account comparison | — | G-recipes |
| D36 | `f873d7d:tests/test_operator_recipes.py` L581 | O: reboot hold brackets the CM stop/start operation | gap | No v2 one-unit CM/boot-interlock recipe assertion | — | G-recipes |
| D37 | `f873d7d:tests/test_operator_recipes.py` L651 | O: sealed CM play retries estate quiesce only for its declared exits | retired | Charter excludes estate quiesce; uncertain stop handling remains K26 | — | — |
| D38 | `f873d7d:tests/test_operator_recipes.py` L651 | O: sealed CM wait covers retry horizon plus longest live runtime | retired | Charter excludes period retry-horizon sealing; bounded stopping remains S49 | — | — |
| D39 | `f873d7d:tests/test_operator_recipes.py` L746 | O: diagram commands, modes and paths agree with deployed units and CLI | gap | V1 diagram is excluded; no replacement diagram/command agreement test identified | — | G-recipes |
| D40 | `f873d7d:tests/test_operator_recipes.py` L778 | O: diagrams name only the shipped units | gap | No v2 deployment diagram/unit-name agreement test identified | — | G-recipes |
| D41 | `f873d7d:tests/test_operator_recipes.py` L792 | O: initial printed job recipe opens no successor period | retired | Charter excludes explicit successor-period opening; v2 root startup is S03 | — | — |
| D42 | `f873d7d:tests/test_operator_recipes.py` L792 | O: printed add-job recipe makes the new command visible through spec | gap | V2 reload tests do not extract and execute deploy.md command blocks | — | G-recipes |
| D43 | `f873d7d:tests/test_operator_recipes.py` L792 | O: printed change-job recipe replaces the old command in spec | gap | Behavior is exercised in U02, but printed-recipe execution is absent | — | G-recipes |
| D44 | `f873d7d:tests/test_operator_recipes.py` L792 | O: printed removal keeps an INACTIVE ghost with no job_type | gap | Tombstones are covered in U03; no matching printed removal/spec sequence | — | G-recipes |
| D45 | `f873d7d:tests/test_operator_recipes.py` L792 | O: starting the removed job exits 2 and names the unknown job | gap | Removed-input refusal is tested in source; no matching printed CLI recipe sequence | — | G-recipes |
| D46 | `f873d7d:tests/test_operator_recipes.py` L792 | O: each printed definition change opens the next numbered WAL segment | retired | Charter excludes period segments; configuration revision adoption is U02 | — | — |
| D47 | `f873d7d:tests/test_operator_recipes.py` L792 | O: printed stop sequence finishes its engine process with exit 0 | gap | No matching v2 printed-procedure process-exit assertion | — | G-recipes |
| D48 | `f873d7d:tests/test_operator_recipes.py` L792 | O: audit attests each period the printed job recipe closed | retired | Charter excludes attestation and period audit | — | — |
| D49 | `f873d7d:tests/test_operator_recipes.py` L792 | O: printed prune dry-run lists the retained lineage and removes nothing | retired | Charter excludes lineage prune; retained v2 history is G-history | — | — |
| D50 | `f873d7d:tests/test_operator_recipes.py` L846 | O: printed job-check refuses update_job delta input with exit 2 and its diagnostic | gap | No v2 printed delta-file check; retain the explicit subcommand refusal | — | G-recipes |
| D51 | `f873d7d:tests/test_operator_recipes.py` L857 | O: sealed-not-opened monitoring reports open, closed, then open | retired | Charter excludes anchors and sealed/open period monitoring | — | — |
| D52 | `f873d7d:tests/test_operator_recipes.py` L947 | O: torn-opening recipe refuses a missing segment record | retired | Charter excludes period opening/segments; incomplete v2 state refusal remains R05 | — | — |
| D53 | `f873d7d:tests/test_operator_recipes.py` L947 | O: repeated torn-opening refusal leaves the lineage claim unchanged | retired | Charter excludes lineage claims; refused-start no mutation remains R07 | — | — |
| D54 | `f873d7d:tests/test_operator_recipes.py` L947 | O: recovered opening claims exactly one new root | retired | Charter excludes roll/reclaim; one-root dispatch recovery remains N17 | — | — |
| D55 | `f873d7d:tests/test_operator_recipes.py` L947 | O: opening records whether explicit fallback reclaim occurred | retired | Charter excludes reclaim records | — | — |
| D56 | `f873d7d:tests/test_operator_recipes.py` L947 | O: old root is refused after relocation of the opened lineage | retired | Charter excludes lineage path authority; relocation scope remains G-restore | — | — |

## Close the surviving gaps

Counts describe scenario obligations, not test functions or successful runs.
Measurement and untested-guard rows are included but do not claim a v1 verdict.

| Disposition | Assertions |
| --- | ---: |
| kept | 52 |
| ported | 88 |
| retired | 62 |
| gap | 142 |
| Total | 344 |

Use the [Known bugs](../TODO.md#known-bugs) register when selecting a gap.
The labels below identify its subjects without copying buggy outputs into
expected results. These are coverage tasks, not new runtime decisions.
The operator routes the proposed test scope and the boss resolves exceptions.
Workflow evidence also has a teardown-reporting gap: if setup and tag removal
both fail, pytest reports the removal error; retain session.json for setup
evidence (Known bugs).

| Gap | Bounded acceptance and related Known bugs |
| --- | --- |
| G-semantic | Independently enumerate expected instants/counts for each DST gap, overlap, midnight and restart window from SEM-32–35 and the cited vendor rule. Assert the fixture constants against both traces. Keep existing lag and end-window exclusions explicit. A runtime/rehearsal/L023 three-way agreement can still share a scheduler bug. |
| G-soak | Port the seeded 200-job fixture and a bounded compressed multi-day run, then a real-clock run with declared RSS/CPU/fd/store/query/replay budgets. Keep maximum 20 concurrent jobs and per-day count/liveness checks. Measure SQLite journal/snapshot retention instead of periods/outbox. Known bugs: pruning-floor/history failure and unbounded backward gap. |
| G-storage | Covered: [storage admission] pins UNKNOWN, no uncommitted launch or external attempt, exact retry and recovery after real bounded-tmpfs ENOSPC. [storage unit] pins the systemd engine's MainPID/start-token/InvocationID and no writes from `--config` contenders before and after CONT. [storage probe child] adds legacy-path and expired-note coverage. Implemented and scheduled in U/S as each row specifies; replay/durability remains G-history/G-restore/G-restart, and release qualification is separate. |
| G-restore | Drain and prove no writers; copy the full store, SQLite sidecars when present, spools, inputs and settings; delete originals; restore; use fresh CLI/engine processes and assert holds, run/attempt identity and exact effects. Inject missing/corrupt required files and prove refusal without mutation. Ask the boss to scope relocation; do not inherit v1 anchor-path rules. Scheduler restoration does not restore a business database. |
| G-upgrade | Exercise real old/new binary and state/protocol pairs, drain before selection, refuse incompatible state without migration, retain an old reader and prove a compatible rollback. The service .post1 wheel uses one source tree and establishes mechanics only. Known bug: legacy-unit root to main-config unit has no documented resume path. |
| G-package | Adapt real-wheel offline manufacture, fault and RPM cells to the reference package/config/unit under the charter and Packager contract. Preserve network-disabled base/UI and prefix matrix, native tuple, partial candidates, child lock inheritance, real ENOSPC, failed post, active selection and erase evidence. The RPM helper still expects a retired launcher asset after erase; no disconnected target driver runs in CI. Do not advertise helper presence as a v2 lane. |
| G-artifact | Restore isolated real-wheel and sdist-rebuilt-wheel acceptance for base/UI, assets, typing/license metadata, stdout, wrapper provenance and optional UI refusal. Keep installed terminal/browser qualification separate. Known bugs: installed help expects a removed verb, TUI terminal-control coverage and unexplained sandbox interruption hold. |
| G-restart | Add installed fault orderings for status/spawn loss, residue, quarantine, deadline and stable invocation across interruptions. Known bugs: cookie-less group members missed after quarantine, pidfd-send capability not probed, Darwin EPERM event-loop blocking, mocked zombie evidence and incomplete standalone-wrapper input validation. Do not accept a false no-process conclusion. |
| G-config | Cover resumed update ordering and configuration-order identity independently. Known bugs: future-stamp queued input delays recovered application, first-look barrier does not discriminate, captured-byte recompilation refusal, main-config restart preflight difference, synchronous pending-source scan and ignored unknown engine key. A known bug does not change the required ordering or refusal. |
| G-update | Run installed add/change/remove/revert with exact effect counts, properties/calendar-only edits and interrupted update replay. Known bugs: stopped-result attribution, decoded equality ignoring job order, barrier cleanup orphaning a command group. Assert the correct record/result even if today's implementation fails. |
| G-history | Combine pruning with an open attempt, an old completed run and a removed/re-added job. Fresh runs/journal readers must report the contract's retained history. Known bug: missing opening revision can refuse the whole runs report. Reconcile the deleted journal tests' replay and crash-durability assertions with SQLite fault coverage; missing cells remain gaps here. |
| G-access | Restore installed multi-account permissions, ancestor traversal, tier refusals, map receipts/reload and status/subscribe monitoring checks. Keep source policy tests, but run the filesystem boundary under the one-unit deployment. |
| G-boot | A container restart keeps the kernel boot identity. Qualify an actual host reboot separately with the selected filesystem, unit and process-identity rules. |
| G-retire | Disable/remove the one unit, prove no writers and no restart from a boot target or stray start, then read retained state with the matching binary. Keep uninstall and retained history distinct from obsolete anchor deletion. |
| G-correction | Port a synthetic late-correction workload within one v2 root; pin initial/corrected results, idempotence, conflict/stale/timestamp refusals and checker damage sensitivity. Period transitions retire; data-correction safety remains. |
| G-wargame | Restore stub-provider regression checks for ownership, exact lifecycle verbs, tracked-only provenance (v2 copies untracked files too), retained /work and delivery failure/partial-copy behavior. A manual shell script is implemented tooling, not an executed safety assertion. |
| G-recipes | Restore printed-procedure extraction and shell checks for deploy.md: account/path/unit agreement, command existence, add/change/remove, delta refusal, bounded waits and no systemd target starts. Execute or compare every public recipe; retain a coverage register. Engine tests alone do not protect printed commands from drift. |

Clock coverage also needs a separate extension beyond the inherited rows:
assert restart completion timestamps, large/backward/forward steps and steps
during recovery against the time contract. The clock-step drill checks input
stamps, not monotonicity of every record kind, and cannot distinguish a
completion held behind the frontier from a correctly admitted one. Known
bugs cover those limits. It does not qualify all clock movement.

[service]: ../examples/nightbank/deploy/drill-steps.sh
[restart tests]: ../tests/test_restart.py
[attempt tests]: ../tests/test_runner_attempts.py
[leadership tests]: ../tests/test_runner_leadership.py
[storage probe child]: ../examples/nightbank/deploy/storage_probe.py#L213
[storage unit]: ../examples/nightbank/deploy/storage_probe.py#L271
[storage admission]: ../examples/nightbank/deploy/storage_probe.py#L414
[storage space]: ../examples/nightbank/deploy/storage_probe.py#L392
[main restart]: ../tests/test_main_config_restart.py
[main update]: ../tests/test_main_config_update.py
[update wiring]: ../tests/test_update_wiring.py
[reload tests]: ../tests/test_reload_update.py
[store wiring]: ../tests/test_store_wiring.py
[access tests]: ../tests/test_access.py
[control tests]: ../tests/test_runner_control.py
[subscription tests]: ../tests/test_subscription_chart.py
[CLI read tests]: ../tests/test_cli_control_read_errors.py
[installed CLI]: ../tests/test_installed_cli.py
[installed TUI]: ../tests/test_installed_tui_recovery.py
[oracle tests]: ../tests/test_oracle.py
[capacity tests]: ../tests/test_capacity_branches.py
[engine stop tests]: ../tests/test_cli_control_branches.py
[offline packaging]: ../tests/test_offline_packaging.py
[offline inputs]: ../tests/test_offline_release_inputs.py
[RPM helper]: ../scripts/offline/rpm-lifecycle.sh
[deploy]: deploy.md
[DST]: ../examples/dst-drill/compare.py
[workflow fulfilment]: ../examples/workflows/tests/test_fulfilment.py
[workflow media]: ../examples/workflows/tests/test_media.py
[wargame]: ../scripts/wargame.sh
[admission tests]: ../tests/test_admission.py
[subscriber tests]: ../tests/test_subscriber_bound.py
[night tests]: ../tests/test_nightbank_example.py
[file exercise]: ../tests/test_file_processing_runner.py
[cadence tests]: ../tests/test_rehearse_check.py
[offline prepare]: ../scripts/offline/prepare-locked.sh
[offline smoke]: ../scripts/offline/smoke.py
[CLI branch tests]: ../tests/test_cli_control_branches.py
