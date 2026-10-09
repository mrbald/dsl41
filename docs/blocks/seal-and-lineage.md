# Seal and lineage

## Purpose

A [period](../glossary.md#period) ends with a [seal](../glossary.md#seal), and exactly one root may open the next one.
This block holds the lineage [anchor](../glossary.md#anchor), whose head records which root leads which period, and the registry row of each period.
It also runs the seal inside the engine: freeze, drain, cut off, commit, or leave without a commit.

## Fate

The contract carries over: [period-model §1.3](../period-model.md#13-the-successor-fence) states the successor fence without naming a substrate.
The anchor code does not: `EstateAnchor` is a file under an `flock`, moved by compare-and-swap, with a durable claim file.
A store would provide the same head as a conditional update, and the claim as a row.
The engine half needs epoch-conditional append: `Journal.seal` re-proves the leader lock before it writes the `seal` record.
It needs atomic multi-record commit too: the `seal` record is one line, and the next period's opening is committed with it ([period-model §3.4](../period-model.md#34-next_period--the-seal-commits-the-opening)).
`seal.py`'s artifact and its two pure functions carry over unchanged.

## Interface

- Wire: the `seal` verb ([control-protocol §3, `seal`](../control-protocol.md#seal-dl-133-period-model-22)). `Engine.submit_seal` holds one request, and a second is refused with `seal_in_flight`.
- Engine: `Engine._seal_boundary` runs one seal inside the loop. `_PendingSeal.phase` is its phase, and `_seal_move` names each move. `Engine.sealing` reads `SealBarrier.parked`.
- Operation: `commit_boundary` in `boundary.py` performs the seal's writes in [period-model §7](../period-model.md#7-the-seal-operation)'s order. `Engine.abort_boundary` releases the freeze, and the period stays open. It touches no row.
- Anchor: `EstateAnchor` with `create_open`, `close_period`, `claim_successor`, `open_claimed`, `finalize`, `attest` and `reclaim`. `row_tag` derives a registry row's state from its flags.
- Resume: `act_on_head` repairs the one crash window each head state can leave, before anything is replayed ([period-model §11](../period-model.md#11-resume-replay-and-recovery)).
- Opening: `open_next_period` in place, and the physical roll in `estate.py` into a fresh root.
- Artifacts: the sidecar `seals/<N>.json` and its `Seal` model in `seal.py`, and the anchor file and the claim files under the anchor directory ([period-model §1.1](../period-model.md#11-layout)).

## States

Three declared machines hold this block's states:

- [anchor_head](../state-machines.md#anchor_head): the lineage head, from genesis through each seal, claim and opening.
- [period_row](../state-machines.md#period_row): one period's registry row, provisional at genesis or durable for a successor, then attested.
- [seal_boundary](../state-machines.md#seal_boundary): one seal request's phases in the engine and its three exits.

Each anchor holds the generated diagram and transition table.
A repeat that writes nothing takes no transition.

## Invariants

- Exactly one root may succeed a seal; the head is the fence ([period-model §1.3](../period-model.md#13-the-successor-fence); [DL-133](../decision-log.md)).
- The head moves after the fact it records. A successor's registry row is written in the same anchor write as its opening, after its first segment is durable (PR-02c in [period-model §13.1](../period-model.md#131-lineage)).
- A claim is keyed on its `claim_id`, not on a process, so a crashed claimant's replacement resumes it ([period-model §1.3](../period-model.md#13-the-successor-fence)).
- A move outside the anchor's tables is refused with `EngineError` before anything is written, and it never becomes a replayed fault ([DL-290](../decision-log.md)).
- The seal's writes follow a fixed order, and the `seal` append is the point of no return ([period-model §7](../period-model.md#7-the-seal-operation)).
- During a seal, admission is frozen and the cutoff owns every tick up to T ([period-model §6](../period-model.md#6-the-cutoff-barrier)).
- The seal proves the supervisor before it commits (PR-27 in [period-model §13.5](../period-model.md#135-the-boundary)).
- A physical roll needs the closing period attested first ([period-model §1.3](../period-model.md#13-the-successor-fence); [DL-134](../decision-log.md)).
- One seal request is in flight at a time ([DL-295](../decision-log.md)).
- A violation in the seal boundary's table is one stderr line, and the move proceeds ([DL-295](../decision-log.md)).

## Failure and recovery

- A non-commit exit before the `seal` append aborts the boundary, and the period stays open (PR-28b in [period-model §13.5](../period-model.md#135-the-boundary)).
- A fence loss, or one of [DL-274](../decision-log.md)'s cases in the drain, [fail-stops](../glossary.md#fail-stop) without an abort. Resume rebuilds from the WAL with the period open.
- A failure at or after the `seal` append raises `BoundaryFailStop`, and the outcome is unknown. Recovery commits a complete line and truncates a torn or absent one ([period-model §7](../period-model.md#7-the-seal-operation), PR-28d).
- A crash between the `seal` record and the anchor CAS: resume performs the CAS (PR-45 in [period-model §13.8](../period-model.md#138-recovery-and-stream)).
- A crashed claimant: the same claim resumes, and a different claim against the same seal is refused, naming the holder. `reclaim` is the attributed break-glass ([period-model §1.3](../period-model.md#13-the-successor-fence)).
- `--on-transition-violation stop` in the drain stops the engine after the decision is durable ([DL-292](../decision-log.md)).

## Owning modules

- `src/dsl41/boundary.py`: the anchor, the claim, the seal operation, `act_on_head` and the opening in place.
- `src/dsl41/seal.py`: the seal artifact, CLOSE and OPEN.
- `src/dsl41/period.py`: period identity and the estate layout.
- `src/dsl41/estate.py`: the physical roll.
- `src/dsl41/runner.py`: `SEAL_BOUNDARY`, `_PendingSeal`, `Engine.submit_seal`, `Engine._seal_boundary`.

## Tests

- `test_pr01a_genesis_writes_the_sentinel_before_the_wal`
- `test_pr01b_genesis_against_an_existing_anchor_refuses`
- `test_pr02c_the_registry_row_flips_in_the_finalize_cas`
- `test_pr02_the_claim_is_the_identity_not_the_process`
- `test_pr02b_the_open_to_closed_cas_is_performed_at_resume`
- `test_pr45_a_claim_with_a_durable_segment_moves_the_head_at_resume`
- `test_ss1_3_a_reclaim_goes_back_to_the_closed_head_the_claim_names`
- `test_a_lineage_walk_takes_every_declared_move_and_reports_no_violation`
- `test_a_move_outside_the_table_is_refused_naming_the_machine_and_the_move`
- `test_an_idempotent_repeat_writes_nothing_and_takes_nothing`
- `test_pr28b_every_non_commit_exit_before_the_append_aborts_and_retries`
- `test_pr28b_a_fence_loss_inside_the_interval_fail_stops`
- `test_pr28d_a_failure_at_the_seal_append_fail_stops_without_reopening`
- `test_pr27_the_seal_proves_the_supervisor_before_it_commits`
- `test_pr25_c1_owns_every_tick_at_or_before_t`
- `test_a_second_seal_is_refused_for_the_whole_boundary_and_accepted_after_an_abort`
- `test_every_exit_in_every_seal_phase_takes_a_declared_transition`
- `test_the_offline_seal_commits_and_the_period_reopens`

## Open findings

See the row "Seal and successor lineage" in [the risk map](../risk-map.md).
Its own finding is [torn sole opening segment of a rolled root](../risk-map.md#torn-sole-opening-segment-of-a-rolled-root).

## Gaps found

- [DL-290](../decision-log.md) leaves two items open. `anchor_head.06` declares a move that the refusals in `reclaim` and in the resume step call unreachable. One rule should replace the two.
  `open_claimed` does not check its `period_id` against the claim's `next_period`. Both production callers pass the claim's own.
