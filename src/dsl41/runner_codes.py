"""The stable codes of the control protocol's `ok: false` answers
(docs/control-protocol.md §2, the code table).

A code names the reason, never the outcome: `refused` and `decision` are
the outcome markers, and a code rides beside them and the prose `error`.
The table in control-protocol.md and `CODES` hold the same set; a test
compares them.

Codes that a rejected `decision` record stores are WAL facts (period-model
§2.3), named by `STORED_CODES`. That set is append-only: a reader treats a
stored code as opaque, so renaming or retiring one is an evolution event
with its own decision-log entry.

This module imports nothing from dsl41, so every exception and gate that
carries a code can name `Code`.
"""

from __future__ import annotations

from typing import Literal, NamedTuple, get_args

Code = Literal[
    # before routing
    "peer_unauthenticated",
    "malformed_request",
    "unsupported_version",
    "access_denied",
    "internal_error",
    # dispatch and queries
    "unknown_cmd",
    "invalid_argument",
    "plan_cycle",
    "unknown_job",
    # subscribe
    "no_journal",
    "backfill_refused",
    # verb framing
    "unknown_verb",
    "unknown_status",
    "status_not_injectable",
    # envelope and admission
    "baseline_mismatch",
    "expect_required",
    "request_id_reused",
    "engine_shutting_down",
    "decision_timeout",
    # the dry apply (concurrency-model ss4): refused before the log
    "apply_faulted",
    # rejected decisions, stored in the WAL
    "precondition_failed",
    # admission's clock
    "clock_regressed",
    # stored on a rejected decision only; never answered on the wire
    "stale_completion",
    # stored on a rejected decision when the input's apply raised and its time
    # half did not: the input is quarantined (concurrency-model ss4); an exact
    # retry of a quarantined control input answers it
    "engine_apply_faulted",
    # an engine refusal that carries no code of its own
    "engine_error",
    # stored on a rejected decision: a mutation admitted after `stop_requested`
    # (spec-wiring 2b); an `update` while draining is refused with it
    "engine_draining",
    # `update`'s refusals (spec-reload): the plan was diffed against another rev, it edits
    # a running box's membership, it stops running jobs without consent, or another update
    # is stopping
    "stale_plan",
    "box_running",
    "would_stop_running",
    "update_in_progress",
]

#: every code, for the table test and the decision writer
CODES: frozenset[str] = frozenset(get_args(Code))

#: the codes a rejected `decision` record may store: the gates' verdicts and
#: the quarantine of an input whose apply raised. Append-only: a retained WAL
#: keeps each for the life of the root, and the writer refuses any other code
STORED_CODES: frozenset[str] = frozenset(
    {
        "precondition_failed",
        "stale_completion",
        "engine_apply_faulted",
        "engine_draining",
    }
)

#: the codes of the `unknown` outcome: admission is uncertain, so an answer
#: with one of these never carries a refusal marker
UNKNOWN_OUTCOME_CODES: frozenset[str] = frozenset({"internal_error", "decision_timeout"})


class Rejection(NamedTuple):
    """A gate's verdict against one admitted input: the code and the prose
    the `decision` record stores beside `decision: "rejected"`."""

    code: Code
    reason: str
