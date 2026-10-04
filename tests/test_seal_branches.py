"""Branch tests for `dsl41.seal` that the sweep in test_seal_artifact.py
does not reach (S6, group c).

Each test holds one rule of `docs/period-model.md` that a branch of
`seal.py` implements, with a twin that does not trigger it where the rule
is a refusal. The fixtures are the golden seal's, imported rather than
copied.
"""

from __future__ import annotations

from typing import Any

import pytest
from test_seal_artifact import (
    RUN_WATCHER,
    _document,
    _rederive_baseline,
    _seal,
)

from dsl41.canon import with_digest
from dsl41.ast_jil import parse
from dsl41.ir import CatalogIR, lower_catalog
from dsl41.runner_clock import EngineError
from dsl41.seal import (
    Seal,
    check_executions_dispatchable,
)

#: a uuid4 in the ss11a grammar, the shape period 1's minted baseline takes
MINTED = "7d9c2f4e-3b1a-4c5d-8e6f-0a1b2c3d4e5f"


def _from_document(document: dict[str, Any]) -> Seal:
    return Seal.from_payload(with_digest({k: v for k, v in document.items() if k != "digest"}))


def _period_one(document: dict[str, Any], baseline: str) -> None:
    """The first seal of a lineage: period 1, no predecessor, opening
    period 2 as segment 2."""
    document["period_id"] = 1
    document["prev_seal_digest"] = None
    document["next_period"]["period_id"] = 2
    document["next_period"]["segment_no"] = 2
    document["baseline_id"] = baseline
    _rederive_baseline(document)


def test_ss1_2_period_ones_baseline_is_the_minted_uuid() -> None:
    """ss1.2: period 1's baseline is the minted uuid4. Free text, or a
    derived sha256 address (a LATER period's shape), is refused."""
    for wrong in ("free text", "sha256:" + "ab" * 32):
        document = _document()
        _period_one(document, wrong)
        with pytest.raises(EngineError, match="period 1's baseline is the minted uuid4"):
            _from_document(document)


def test_ss1_2_a_minted_baseline_opens_for_period_one() -> None:
    """The twin of the refusal above."""
    document = _document()
    _period_one(document, MINTED)
    assert _from_document(document).baseline_id == MINTED


def test_i2_an_execution_below_the_first_wal_position_is_refused() -> None:
    """I2: an execution entry's index is a WAL position in [1, cutoff]."""
    document = _document()
    bound = next(e for e in document["executions"] if e["kind"] == "bound")
    bound["index"] = 0
    with pytest.raises(EngineError, match=r"at index .* outside \[1, 5310\]"):
        _from_document(document)


def test_i2_an_execution_at_the_first_wal_position_opens() -> None:
    """The twin: index 1 is the lowest position a WAL record can hold, and
    the entry stays first in `(index, effect_id)` order."""
    document = _document()
    bound = next(e for e in document["executions"] if e["kind"] == "bound")
    bound["index"] = 1
    assert _from_document(document).executions[0].index == 1


def test_pr22_the_pair_join_compares_only_the_fields_an_fw_entry_defines() -> None:
    """PR-22: the exact-match join between a pending effect and its execution
    entry compares the fields the entry has. An `fw_watch` entry has no
    `executor_id` or `generation`, so the join skips those two.

    The input is a hand-edited sidecar: a KILL filed under the watch's SPAWN
    effect id. No engine writes one, because `effect_id` carries the kind. This
    test holds how the reader's join treats the missing fields, not a rule
    that such a sidecar is legal."""
    document = _document()
    watch = next(e for e in document["executions"] if e["kind"] == "fw_watch")
    kill = {
        "at": document["closed_at"],
        "effect_id": watch["effect_id"],
        "executor_id": "local",
        "generation": 0,
        "index": watch["index"],
        "job": watch["job"],
        "kind": "KILL",
        "run_id": watch["run_id"],
        "run_number": watch["run_number"],
    }
    assert watch["run_id"] == RUN_WATCHER
    document["outbox_pending"] = sorted(
        [*document["outbox_pending"], kill], key=lambda e: (e["index"], e["effect_id"])
    )
    seal = _from_document(document)
    assert watch["effect_id"] in {e.effect_id for e in seal.outbox_pending}


def test_pr22_the_pair_join_still_compares_the_shared_fields_of_an_fw_entry() -> None:
    """The twin: the skip covers only the fields the entry lacks. The shared
    fields (here `job`) still have to agree with an `fw_watch` entry."""
    document = _document()
    watch = next(e for e in document["executions"] if e["kind"] == "fw_watch")
    kill = {
        "at": document["closed_at"],
        "effect_id": watch["effect_id"],
        "executor_id": "local",
        "generation": 0,
        "index": watch["index"],
        "job": "nightly",
        "kind": "KILL",
        "run_id": watch["run_id"],
        "run_number": 7,
    }
    document["outbox_pending"] = sorted(
        [*document["outbox_pending"], kill], key=lambda e: (e["index"], e["effect_id"])
    )
    with pytest.raises(EngineError, match=r"job 'nightly' but its execution says 'watcher'"):
        _from_document(document)


_CATALOG_TEXT = (
    "".join(f"insert_job: {name}\njob_type: c\ncommand: x\n\n" for name in ("nightly", "extract"))
    + "insert_job: watcher\njob_type: f\nwatch_file: /tmp/x\n\n"
)


def test_ss3_5_an_execution_naming_a_job_the_catalog_lacks_is_refused() -> None:
    """ss3.5 / ss10.1: a legal boundary never carries an execution for a
    job the opening catalog dropped. The refusal names the entry and the
    job, and says to check the estate files the resume was launched with."""
    with pytest.raises(EngineError, match=r"names job .* which this catalog does not define"):
        check_executions_dispatchable(_seal(), CatalogIR())


def test_ss3_5_executions_naming_defined_jobs_pass_the_dispatchable_check() -> None:
    """The twin: every execution's job is defined and none is a box."""
    catalog = lower_catalog([parse(_CATALOG_TEXT, file="estate.jil")])
    check_executions_dispatchable(_seal(), catalog)
