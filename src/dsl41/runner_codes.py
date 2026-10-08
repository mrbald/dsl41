"""The stable codes of the control protocol's `ok: false` answers
(docs/control-protocol.md §2, the code table).

A code names the reason, never the outcome: `refused` and `decision` stay
the outcome markers, and a code rides beside them and beside the prose
`error`. The table in control-protocol.md and `CODES` hold the same set,
and a test compares them.

Codes that a rejected `decision` record stores are WAL facts (period-model
§2.3), and `STORED_CODES` names them. That set is append-only: a reader
treats a stored code as opaque and never checks it against this registry,
so renaming or retiring one is an evolution event with its own
decision-log entry.

This module imports nothing from dsl41. It sits at the bottom of the
import graph so the exceptions that carry a code, and the gates that mint
one, can all name `Code`.
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
    "lineage_lost",
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
    "stale_epoch",
    "period_sealing",
    "engine_shutting_down",
    "decision_timeout",
    # the dry apply (concurrency-model ss4): refused before the log
    "apply_faulted",
    "transition_violation",
    # rejected decisions, stored in the WAL
    "precondition_failed",
    "unknown_host",
    "host_quarantined",
    "host_evicted",
    "host_already_evicted",
    "force_needs_actor",
    "host_not_quarantined",
    "host_no_deadman",
    "host_never_contacted",
    "eviction_bound_pending",
    # seal
    "seal_retry_mismatch",
    "seal_in_flight",
    "no_lineage",
    "stage_digest_mismatch",
    "nothing_staged",
    "seal_input_after_cutoff",
    "seal_not_settling",
    "seal_not_quiescent",
    "seal_supervisor_unproven",
    "seal_run_unaccounted",
    "seal_retry_horizon",
    "clock_regressed",
    "seal_refused",
    "seal_timeout",
    # stored on a rejected decision only; never answered on the wire
    "stale_completion",
    # an engine refusal that carries no code of its own
    "engine_error",
]

#: every code, for the table test and the decision writer
CODES: frozenset[str] = frozenset(get_args(Code))

#: the codes a rejected `decision` record may store: the gates' verdicts.
#: APPEND-ONLY -- each one may sit in a retained WAL for the life of the
#: root, and the writer refuses a rejection whose code is not here
STORED_CODES: frozenset[str] = frozenset(
    {
        "precondition_failed",
        "unknown_host",
        "host_quarantined",
        "host_evicted",
        "host_already_evicted",
        "force_needs_actor",
        "host_not_quarantined",
        "host_no_deadman",
        "host_never_contacted",
        "eviction_bound_pending",
        "stale_completion",
    }
)

#: the codes of the `unknown` outcome: admission is uncertain, so an answer
#: with one of these never carries a refusal marker
UNKNOWN_OUTCOME_CODES: frozenset[str] = frozenset(
    {"internal_error", "decision_timeout", "seal_timeout"}
)


class Rejection(NamedTuple):
    """A gate's verdict against one admitted input: the code and the prose
    the `decision` record stores beside `decision: "rejected"`."""

    code: Code
    reason: str
