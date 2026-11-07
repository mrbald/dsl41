# Control subscription

## Purpose

A `subscribe` feed streams the journal's records to one client: a backfill from the client's cursor, then each record as it is appended.
It is how a monitor or a UI follows an estate without polling.
A client that stops reading is removed, and it resumes from its cursor.
There is no second durable feed.

## Fate

`Subscription` and the fan-out in `Journal` are in memory.
Backfill reads retained store records in store-sequence order, up to the seam captured when the feed joins.
The cursor is an admission index, not a store sequence ([DL-428](../decision-log.md); [control-protocol §5](../control-protocol.md#5-streaming-verb-subscribe)).
The feed needs ordered reads and publication after commit. PR-03's lineage fence resolves to [DL-424](../decision-log.md).

## Interface

- Wire: the `subscribe` verb with an optional cursor, on a connection of its own. The request and ack shapes are [control-protocol §5](../control-protocol.md#5-streaming-verb-subscribe)'s.
- Refusals: `no_journal` comes before the ack. `backfill_refused` comes on the stream after it. A cursor below what the store retains gets a gap marker line first.
- Server: `ControlServer._subscribe` in `runner_control.py` sends the ack, the backfill and the live records, and drops a seq'd record the backfill already sent.
- Journal: `Journal.subscribe(machines, on_overflow, budget=...)` returns a `Subscription`; `machines` is the engine's executor. `Journal.unsubscribe` leaves the fan-out. Each append offers the record to every feed (`Journal._publish`).
- `Subscription`: `offer`, `get`, `go_live`, `close`, `tell_owner`, `backlog_bytes` and `state`. It is the record and the key of one instance of the feed's chart. `offer`, `go_live` and `close` post its events, and `state` is the instance's active leaf.
- Backfill: `read_backfill(store, since=..., upto=...)` returns a `Backfill`, with `gap_from` and the retained wire records after the cursor, up to the captured store sequence.
- Budget: `SUBSCRIBER_BACKLOG_BYTES`, sized in [control-protocol §5](../control-protocol.md#5-streaming-verb-subscribe).
- Clients: `ControlClient.subscribe` and the CLI's `subscribe_lines`. A stream line past the budget raises `StreamLineTooLong`.

## States

One feed's states are [subscription](../state-machines.md#subscription). The anchor holds the generated diagram and transition table.
The instance starts in `backfill` when the feed joins the fan-out; before that there is no instance, and no state.
It ends in `removed` or `closed`, and the journal forgets it at once.
An unsubscribe of a removed feed finds nothing to leave and posts nothing.

## Invariants

- The delivery guarantees, exactly once for seq'd records and at least once for the rest across the backfill seam, are [control-protocol §5](../control-protocol.md#5-streaming-verb-subscribe)'s. This card does not restate them.
- The seam is sampled before the ack is written ([DL-45](../decision-log.md); [control-protocol §5](../control-protocol.md#5-streaming-verb-subscribe)).
- The ack names the cursor, so a client that reads no seq'd record can still resume ([DL-267](../decision-log.md)).
- The backfill spans the retained store; pruning of records above the cursor is reported as a gap ([DL-135](../decision-log.md), [DL-428](../decision-log.md)).
- The stream carries `decision` records, keyed by `index` and not `seq` ([DL-89](../decision-log.md); [DL-118](../decision-log.md)).
- A feed's backlog is bounded by the budget or one record, whichever is larger, and an overflow removes the feed ([DL-267](../decision-log.md)).
- Nothing a removal does reaches the append, the engine loop or another feed ([DL-267](../decision-log.md)); a statechart error is the one exception (exit 7).
- Records reach the feeds only after the store transaction commits. The `segment` wire record projects an applied configuration; seal publication under [DL-133](../decision-log.md) resolves to [DL-424](../decision-log.md) ([control-protocol §5](../control-protocol.md#5-streaming-verb-subscribe)).
- A gap in the feed's chart is a statechart error, and the engine exits 7 ([statechart-engine §5](../statechart-engine.md#5-the-step-and-the-worklist)).

## Failure and recovery

- A slow reader overflows: the journal drops its backlog, the server aborts the connection, and the client resumes from its cursor ([control-protocol §6](../control-protocol.md#6-client-obligations)).
- The backfill meets an unreadable store record: it refuses on the stream after the ack, never skipping records ([DL-135](../decision-log.md); [control-protocol §5](../control-protocol.md#5-streaming-verb-subscribe)).
- An operator revokes the peer's access: the stream ends at once ([access-model §7](../access-model.md#7-reload-and-revocation)).
- A shutdown gives a reading client its queued bytes for up to `HANGUP_GRACE_S`, and a stalled one does not hold the shutdown ([DL-267](../decision-log.md)).
- Retention prunes the store behind a snapshot. A cursor below what remains gets the gap marker ([DL-428](../decision-log.md)).

## Owning modules

- `src/dsl41/runner_journal.py`: `Subscription`, the `SUBSCRIPTION` chart, the fan-out, `Backfill` and `read_backfill`.
- `src/dsl41/runner_control.py`: the `subscribe` handler, the budget, the connection ends and the client.
- `src/dsl41/cli_control.py`: `dsl41 query subscribe` and its resume advice.

## Tests

- `test_subscribe_backfills_since_zero_then_streams_a_live_record_once`
- `test_subscribe_forwards_decision_once_across_the_seam`
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
- `test_the_completeness_table_is_section_5`
- `test_every_event_the_posters_send_is_in_the_alphabet`
- `test_a_statechart_error_in_a_control_handler_stops_the_run_with_exit_7`
- `test_the_old_belt_would_have_answered_internal_error`
- `test_a_statechart_error_from_an_owner_reaches_the_append`
- `test_a_statechart_error_while_the_server_closes_still_exits_7`

## Open findings

See the row "Control exchange and subscription" in [the risk map](../risk-map.md).
It has no finding of its own.

## Gaps found

None.
