# Control subscription

## Purpose

A `subscribe` feed streams the journal's records to one client: a backfill from the client's cursor, then each record as it is appended.
It is how a monitor or a UI follows an estate without polling.
A client that stops reading is removed, and it resumes from its cursor.
There is no second durable feed.

## Fate

The code and its contract carry over if the storage under the runner changes.
It needs none of the five storage capabilities.
It reads the log in order from a cursor and fans out each record after its append.
A store that keeps records in index order and can be read from a cursor serves it.
`Subscription` and the fan-out in `Journal` are in memory, and the backfill reads the retained segments under `wal/`, which a new store would replace.
Before every response it re-proves the lineage fence, as every revision-bearing read does ([period-model §1.3](../period-model.md#13-the-successor-fence); PR-03 in [§13.1](../period-model.md#131-lineage)).

## Interface

- Wire: the `subscribe` verb with an optional cursor, on a connection of its own. The request and ack shapes are [control-protocol §5](../control-protocol.md#5-streaming-verb-subscribe)'s.
- Refusals: `no_journal` comes before the ack. `backfill_refused` and a displaced leader's refusal come on the stream after it. A cursor below what the root retains gets a gap marker line first.
- Server: `ControlServer._subscribe` in `runner_control.py` sends the ack, the backfill and the live records, and drops a seq'd record the backfill already sent.
- Journal: `Journal.subscribe(on_overflow, budget=...)` returns a `Subscription`. `Journal.unsubscribe` leaves the fan-out. Each append offers the record to every feed (`Journal._publish`).
- `Subscription`: `offer`, `get`, `go_live`, `close`, `tell_owner`, `backlog_bytes` and `state`. `removed` is derived from `overflow`, and the other states are stored.
- Backfill: `read_backfill(path, since=...)` returns a `Backfill`, with `gap_from` and the retained records after the cursor.
- Budget: `SUBSCRIBER_BACKLOG_BYTES`, sized in [control-protocol §5](../control-protocol.md#5-streaming-verb-subscribe).
- Clients: `ControlClient.subscribe` and the CLI's `subscribe_lines`. A stream line past the budget raises `StreamLineTooLong`.

## States

One feed's states are [subscription](../state-machines.md#subscription). The anchor holds the generated diagram and transition table.
An unsubscribe of a removed feed finds nothing to leave and takes no transition.

## Invariants

- The delivery guarantees, exactly once for seq'd records and at least once for the rest across the backfill seam, are [control-protocol §5](../control-protocol.md#5-streaming-verb-subscribe)'s. This card does not restate them.
- The seam is sampled before the ack is written ([DL-45](../decision-log.md); [control-protocol §5](../control-protocol.md#5-streaming-verb-subscribe)).
- The ack names the cursor, so a client that reads no seq'd record can still resume ([DL-267](../decision-log.md)).
- The backfill spans segments and is bounded by the segment holding the cursor ([DL-135](../decision-log.md)).
- The stream carries `decision` records, keyed by `index` and not `seq` ([DL-89](../decision-log.md); [DL-118](../decision-log.md)).
- A feed's backlog is bounded by the budget or one record, whichever is larger, and an overflow removes the feed ([DL-267](../decision-log.md)).
- Nothing a removal does reaches the append, the engine loop or another feed ([DL-267](../decision-log.md)).
- A `seal` record reaches the feeds only after its fsync, so no subscriber hears of a boundary that recovery may discard ([DL-133](../decision-log.md)).
- A violation of the feed's table is one stderr line, and the move proceeds ([DL-295](../decision-log.md)).

## Failure and recovery

- A slow reader overflows: the journal drops its backlog, the server aborts the connection, and the client resumes from its cursor ([control-protocol §6](../control-protocol.md#6-client-obligations)).
- The backfill meets a foreign file or a broken segment chain: it refuses on the stream after the ack, never skipping records ([DL-135](../decision-log.md)).
- The engine loses its lineage: the next response is the refusal, and the stream ends (PR-03).
- An operator revokes the peer's access: the stream ends at once ([access-model §7](../access-model.md#7-reload-and-revocation)).
- A shutdown gives a reading client its queued bytes for up to `HANGUP_GRACE_S`, and a stalled one does not hold the shutdown ([DL-267](../decision-log.md)).
- After a roll, the new root holds none of the closing period's WAL, so an old cursor gets the gap marker ([period-model §1.3](../period-model.md#13-the-successor-fence)).

## Owning modules

- `src/dsl41/runner_journal.py`: `Subscription`, `SUBSCRIPTION`, the fan-out, `Backfill` and `read_backfill`.
- `src/dsl41/runner_control.py`: the `subscribe` handler, the budget, the connection ends and the client.
- `src/dsl41/cli_control.py`: `dsl41 query subscribe` and its resume advice.

## Tests

- `test_subscribe_backfills_since_zero_then_streams_a_live_record_once`
- `test_subscribe_forwards_decision_at_least_once_across_the_seam`
- `test_pr49_the_backfill_spans_segments_after_a_boundary`
- `test_pr49_a_cursor_below_the_earliest_retained_record_gets_the_gap_marker`
- `test_a_foreign_file_under_wal_refuses_the_backfill_on_the_stream`
- `test_pr03_a_subscription_re_proves_the_lineage_before_every_response`
- `test_a_seal_whose_fsync_fails_reaches_no_subscriber`
- `test_dl267_a_feed_is_removed_at_the_fixed_bound_and_never_grows_past_it`
- `test_dl267_one_record_larger_than_the_budget_delays_and_does_not_remove`
- `test_dl267_a_broken_stderr_and_a_raising_owner_never_fail_the_append`
- `test_dl267_a_stalled_subscriber_is_removed_and_the_estate_keeps_running`
- `test_dl267_an_overflow_during_backfill_ends_the_stream_inside_it`
- `test_dl267_clean_shutdown_is_not_held_by_a_stalled_subscriber`
- `test_dl267_revoking_a_stalled_stream_ends_it_at_once`
- `test_dl267_a_stalled_monitoring_pipe_recovers_from_its_cursor`
- `test_a_feed_moves_backfill_live_closed`
- `test_an_overflowed_feed_is_removed_and_its_unsubscribe_takes_nothing`
- `test_every_feed_event_in_every_subscription_state_takes_a_declared_transition`

## Open findings

See the row "Control exchange and subscription" in [the risk map](../risk-map.md).
It has no finding of its own.

## Gaps found

None.
