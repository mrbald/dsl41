"""Seal-artifact tests (period-model ss3/ss4, DL-132).

Normative spec: `docs/period-model.md` ss3.1 (shape), ss3.2 (canonical
form), ss3.3 (carried and not carried), ss3.4 (`next_period`), ss3.5 (the
execution union) and ss4 (`baseline_id` rotates), with obligations PR-05b,
PR-05c, PR-07's opening half, PR-08, PR-08d, PR-10a..PR-14, PR-16b's
carried half, PR-18a, PR-19a, PR-20, PR-21, PR-22, PR-22a, PR-24a and
PR-47d in ss13.

House style follows test_canon.py and test_period_identity.py: the golden
vector pins EXACT bytes and an EXACT digest as literals, because equality
and sensitivity tests alone would pass a writer that is consistently wrong;
the sweeps are DERIVED from the models' own fields, so a field added later
is tested by default (the DL-83 discipline).

The ss7-step-6 sweep injects one failure per invariant into the golden
document, RE-STAMPS the digest, and then opens it -- otherwise every case
would fail as a digest mismatch and prove only that the digest works.
"""

from __future__ import annotations

import importlib.util
import inspect
from collections.abc import Mapping
import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import dsl41.seal
from dsl41.canon import canonical_bytes, decode, digest, with_digest
from dsl41.classify import ARMED_ASSUMPTION, Classification, JobVerdict
from dsl41.ir import CatalogIR
from dsl41.oracle_state import (
    CapacityReservation,
    Event,
    GlobalRuntime,
    HostRuntime,
    JobRuntime,
    RuntimeState,
)
from dsl41.period import (
    EMPTY_BUNDLE_HASH,
    check_segment_record,
    Manifest,
    RuntimeProfile,
    StagedManifest,
    catalog_hash_v2,
    runtime_hash,
    segment_record,
)
from dsl41.runner_clock import EngineError
from dsl41.runner_effects import Effect
from dsl41.seal import (
    BoundaryRequest,
    BoundRun,
    CommittedNextPeriod,
    FwWatch,
    ForcedGate,
    OpenedRuntime,
    PendingSpawn,
    RouteRuntime,
    Seal,
    SealedHost,
    SealedState,
    SealedVerdict,
    StagedNextPeriod,
    baseline_id_for,
    close_runtime,
    implicit_routes,
    open_from_seal,
)
from dsl41.seal import _SHARED_WITH_MANIFEST

#: T -- the cutoff instant. The three timestamps a seal calls T are one
#: value, and the fractional part is deliberately non-zero nowhere: ss3.2
#: writes six digits either way, and the golden vector pins both spellings.
T = datetime(2026, 8, 19, 2, 0, 0)

RUN_NIGHTLY = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
RUN_EXTRACT = "b0c9a1d2-1111-4222-8333-444455556666"
RUN_WATCHER = "c1d2e3f4-5555-4666-9777-888899990000"

PROFILE = RuntimeProfile()
CATALOG = CatalogIR()


# ------------------------------------------------------------ the fixture


def _closing() -> Manifest:
    """The CLOSING period's committed manifest -- where a seal's own
    identity comes from (one authority, ss2.1)."""
    return StagedManifest(
        catalog_hash=catalog_hash_v2(CATALOG),
        source_bundle_hash=EMPTY_BUNDLE_HASH,
        runtime_profile=PROFILE,
        runtime_hash=runtime_hash(PROFILE),
        state_machine_version=1,
    ).commit(
        period_id=2,
        # a later period's baseline is DERIVED (ss4): the fixture derives it
        # exactly as the boundary that opened period 2 would have
        baseline_id=baseline_id_for(
            estate_id="nightbank/one",
            period_id=2,
            stage_digest="sha256:" + "33" * 32,
        ),
        clock_domain="real",
        segment_no=2,
        first_index=4187,
    )


def _staged() -> StagedNextPeriod:
    """What the client proposes: the identity of WHAT opens next."""
    return StagedNextPeriod(
        catalog_hash=catalog_hash_v2(CATALOG),
        source_bundle_hash=EMPTY_BUNDLE_HASH,
        runtime_hash=runtime_hash(PROFILE),
        state_machine_version=1,
    )


def _state() -> SealedState:
    """One small estate exercising every carried row of ss3.3.

    A live CMD with a capacity vector, a live box with `ran_members` and no
    execution entry of its own, a STARTING row whose SPAWN is still
    pending, a live FW watch, a QUE_WAIT waiter, an armed latent row, two
    hosts (one evicted, forced), two routes, two timers at ONE instant, and
    a ghost bucket whose resource is gone."""
    return SealedState(
        jobs={
            "nightly": JobRuntime(
                status="RUNNING",
                status_at=T - timedelta(minutes=30),
                run_number=7,
                started_by="scheduler",
                reservations=(
                    CapacityReservation(bucket="r:FUEL", units=3, release_policy="never"),
                    CapacityReservation(bucket="m:local", units=1, release_policy="completion"),
                ),
            ),
            "night_box": JobRuntime(
                status="RUNNING",
                status_at=T - timedelta(hours=1),
                run_number=2,
                ran_members=frozenset({"nightly", "extract"}),
            ),
            "extract": JobRuntime(status="STARTING", status_at=T, run_number=4),
            "watcher": JobRuntime(
                status="RUNNING", status_at=T - timedelta(minutes=5), run_number=1
            ),
            "queued": JobRuntime(status="QUE_WAIT", status_at=T, run_number=3, waiter_seq=12),
            # never run: the ghost-run gate must NOT hold a row at run 0
            "idle": JobRuntime(),
            "latent": JobRuntime(
                status="SUCCESS",
                status_at=T - timedelta(days=1),
                last_end_at=T - timedelta(days=1),
                exit_code=0,
                run_number=9,
                armed=True,
            ),
        },
        globals={
            # a control character and non-ASCII in one value, and a `/`
            # that ss3.2 never escapes
            "CALÉNDAR": GlobalRuntime(value="café/ok", state_rev=3),
            "EMPTY": GlobalRuntime(value=""),
        },
        hosts={
            "local": HostRuntime(deadman_s=60.0, last_contact=T, state_rev=2),
            "relay-2": HostRuntime(
                state="evicted", generation=1, forced_by="alice@ops-laptop", state_rev=4
            ),
        },
        routes={
            **implicit_routes("local"),
            "batch": RouteRuntime(executor_id="relay-2", state_rev=5),
        },
        timers=(
            # ONE instant, two tokens: the token is what orders them, and
            # an empty opaque payload is not the same value as a filled one
            (T + timedelta(hours=1), 40, Event(at=T, kind="TIMER", payload={})),
            (
                T + timedelta(hours=1),
                41,
                Event(
                    at=T,
                    kind="MUST_START_ALARM",
                    payload={"job": "nightly", "check": "must_start", "meta": {"digest": "data"}},
                    source="scheduler",
                ),
            ),
        ),
        timer_seq=41,
        consumed={"r:FUEL": 3, "r:GONE": 1},
        enqueue_counter=12,
        now=T,
    )


def _outbox() -> tuple[Effect, ...]:
    """Three intents, handed over in the wrong order on purpose: ss3.2's
    canonical order is `(index, effect_id)` (PR-14).

    `extract.4` carries both a SPAWN and, decided one input later, its own
    KILL -- the pair PR-14 names, and the one an order that ignored the
    index could invert. The two at index 5310 are ordered by `effect_id`
    within it."""
    return (
        Effect(
            effect_id="e5310:KILL:nightly.7",
            kind="KILL",
            job="nightly",
            run_number=7,
            executor_id="local",
            index=5310,
            at=T,
            run_id=RUN_NIGHTLY,
            generation=0,
        ),
        Effect(
            effect_id="e5310:KILL:extract.4",
            kind="KILL",
            job="extract",
            run_number=4,
            executor_id="local",
            index=5310,
            at=T,
            run_id=RUN_EXTRACT,
            generation=0,
        ),
        Effect(
            effect_id="e5309:SPAWN:extract.4",
            kind="SPAWN",
            job="extract",
            run_number=4,
            executor_id="local",
            index=5309,
            at=T,
            run_id=RUN_EXTRACT,
            generation=0,
        ),
    )


def _executions() -> tuple[Any, ...]:
    """One of each ss3.5 kind, also handed over out of order."""
    return (
        FwWatch(
            job="watcher",
            run_number=1,
            effect_id="e5100:SPAWN:watcher.1",
            index=5100,
            run_id=RUN_WATCHER,
            watch_seq=3,
            previous_size=None,
            stable_polls=0,
            next_poll_at=T + timedelta(seconds=90, microseconds=500000),
        ),
        PendingSpawn(
            job="extract",
            run_number=4,
            effect_id="e5309:SPAWN:extract.4",
            index=5309,
            run_id=RUN_EXTRACT,
            executor_id="local",
            generation=0,
        ),
        BoundRun(
            job="nightly",
            run_number=7,
            effect_id="e5001:SPAWN:nightly.7",
            index=5001,
            run_id=RUN_NIGHTLY,
            executor_id="local",
            generation=0,
            run_dir="runs/nightly.7",
        ),
    )


def _classification() -> Classification:
    return Classification(
        verdicts=(
            JobVerdict(job="latent", tier="latent", verdict="A", assumption=ARMED_ASSUMPTION),
            JobVerdict(job="nightly", tier="executing", verdict="carry"),
        ),
        changed_not_live=("latent",),
    )


def _seal(**overrides: Any) -> Seal:
    """The golden seal. Every argument of `close_runtime` is overridable so
    that a case can move exactly one fact."""
    arguments: dict[str, Any] = {
        "closing": _closing(),
        "estate_id": "nightbank/one",
        "epoch": 7,
        "prev_seal_digest": "sha256:" + "11" * 32,
        "closes_at_index": 5310,
        "closed_at": T,
        "scheduler_admitted_through": T,
        "state": _state(),
        "outbox_pending": _outbox(),
        "executions": _executions(),
        "classification": _classification(),
        "staged": _staged(),
        "boundary_request": BoundaryRequest(
            source="request",
            request_id="af7c1fe6-d669-414e-b066-e9733f0de7a8",
            claimed_actor="alice@ops-laptop",
            force_seal=True,
        ),
        "request_fingerprint": "sha256:" + "22" * 32,
        "forced_gate": ForcedGate(
            gate="retry_horizon", horizon_us=60_000_000, observed_age_us=2_000_000
        ),
    }
    arguments.update(overrides)
    return close_runtime(**arguments)


def _opening_manifest(opened: OpenedRuntime) -> Manifest:
    """The committed manifest of the period an opening commits: the staged
    identity the seal carries plus the five engine fields, joined with the
    profile the manifest alone holds (ss2.1)."""
    opening = opened.next_period
    return StagedManifest(
        catalog_hash=opening.catalog_hash,
        source_bundle_hash=opening.source_bundle_hash,
        runtime_profile=PROFILE,
        runtime_hash=opening.runtime_hash,
        state_machine_version=opening.state_machine_version,
    ).commit(
        period_id=opening.period_id,
        baseline_id=opening.baseline_id,
        clock_domain=opening.clock_domain,
        segment_no=opening.segment_no,
        first_index=opening.first_index,
    )


def _manifest_of(document: Mapping[str, Any]) -> Manifest:
    """The opening period's committed manifest, reconstructed from the
    sidecar's own `next_period` plus the profile only a manifest holds --
    what the opener would have installed."""
    opening = document["next_period"]
    return StagedManifest(
        catalog_hash=opening["catalog_hash"],
        source_bundle_hash=opening["source_bundle_hash"],
        runtime_profile=PROFILE,
        runtime_hash=opening["runtime_hash"],
        state_machine_version=opening["state_machine_version"],
    ).commit(
        period_id=opening["period_id"],
        baseline_id=opening["baseline_id"],
        clock_domain=opening["clock_domain"],
        segment_no=opening["segment_no"],
        first_index=opening["first_index"],
    )


def _open(seal: Any, **overrides: Any) -> OpenedRuntime:
    """`open_from_seal` with the two REQUIRED facts supplied from the
    artifact itself -- the honest default for a test whose subject is a
    different rule; the naming-digest and manifest rules have their own
    cases with real disagreements."""
    if isinstance(seal, Seal):
        document = json.loads(seal.to_bytes())
    elif isinstance(seal, (bytes, str)):
        document = json.loads(seal)
    else:
        document = seal
    arguments: dict[str, Any] = {
        "expected_digest": document["digest"],
        "manifest": _manifest_of(document),
    }
    arguments.update(overrides)
    return open_from_seal(seal, **arguments)


def _document(**overrides: Any) -> dict[str, Any]:
    """The golden sidecar as a decoded document, digest included."""
    return json.loads(_seal(**overrides).to_bytes())


def _restamped(document: dict[str, Any]) -> bytes:
    """A mutated document with its digest recomputed -- so a sweep case
    fails on the invariant it injected and not on the digest."""
    return canonical_bytes(with_digest({k: v for k, v in document.items() if k != "digest"}))


# --------------------------------------------------- 1. the golden vector

#: PR-08 for the sidecar: pinned, not computed. A change to any model
#: field, the key order, the escaping, the datetime spelling or the two
#: sort rules reds this test, which is the whole point of shipping a
#: vector rather than a round-trip.
GOLDEN_DIGEST = "sha256:2b0d6653335c2e156b666393ca385c7f16ab629ece09fe0ce3713503cb28087b"

GOLDEN_BYTES = (
    b'{"artifact_format_version":1,"baseline_id":"sha256:d92cb0a1b58f92aad'
    b'65775672e0f75b4583ddb5bb0cb1f30e931864c496ed952","boundary_request":'
    b'{"claimed_actor":"alice@ops-laptop","force_seal":true,"request_id":"'
    b'af7c1fe6-d669-414e-b066-e9733f0de7a8","source":"request"},"catalog_h'
    b'ash":"sha256:b8a587f87459250e3d9f79f48f1f262924576af9d83424b2ce58b2c'
    b'9b557e21d","catalog_hash_version":2,"classification":{"latent":{"ass'
    b'umption":"the C1 trigger survives under C2 gating","class":"A"},"nig'
    b'htly":{"assumption":null,"class":"carry"}},"clock_domain":"real","cl'
    b'osed_at":"2026-08-19T02:00:00.000000","closes_at_index":5310,"digest'
    b'":"sha256:2b0d6653335c2e156b666393ca385c7f16ab629ece09fe0ce3713503cb'
    b'28087b","epoch":7,"estate_id":"nightbank/one","executions":[{"effect'
    b'_id":"e5001:SPAWN:nightly.7","executor_id":"local","generation":0,"i'
    b'ndex":5001,"job":"nightly","kind":"bound","run_dir":"runs/nightly.7"'
    b',"run_id":"3f2504e0-4f89-41d3-9a0c-0305e82c3301","run_number":7},{"e'
    b'ffect_id":"e5100:SPAWN:watcher.1","index":5100,"job":"watcher","kind'
    b'":"fw_watch","next_poll_at":"2026-08-19T02:01:30.500000","previous_s'
    b'ize":null,"run_id":"c1d2e3f4-5555-4666-9777-888899990000","run_numbe'
    b'r":1,"stable_polls":0,"watch_seq":3},{"effect_id":"e5309:SPAWN:extra'
    b'ct.4","executor_id":"local","generation":0,"index":5309,"job":"extra'
    b'ct","kind":"pending_spawn","run_id":"b0c9a1d2-1111-4222-8333-4444555'
    b'56666","run_number":4}],"forced_gate":{"gate":"retry_horizon","horiz'
    b'on_us":60000000,"observed_age_us":2000000},"next_period":{"artifact_'
    b'format_version":1,"baseline_id":"sha256:71814d73d8f38ae1722ee23adb85'
    b'f0ab9b699834876fae39c94ada7e5e2e518d","catalog_hash":"sha256:b8a587f'
    b'87459250e3d9f79f48f1f262924576af9d83424b2ce58b2c9b557e21d","catalog_'
    b'hash_version":2,"clock_domain":"real","first_index":5311,"period_id"'
    b':3,"runtime_hash":"sha256:731f24c225cef1cc9c395adff88780e6c1d6cc40b2'
    b'49f8142b54a9993687702c","segment_no":3,"source_bundle_hash":"sha256:'
    b'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855","s'
    b'tate_machine_version":1},"outbox_pending":[{"at":"2026-08-19T02:00:0'
    b'0.000000","effect_id":"e5309:SPAWN:extract.4","executor_id":"local",'
    b'"generation":0,"index":5309,"job":"extract","kind":"SPAWN","run_id":'
    b'"b0c9a1d2-1111-4222-8333-444455556666","run_number":4},{"at":"2026-0'
    b'8-19T02:00:00.000000","effect_id":"e5310:KILL:extract.4","executor_i'
    b'd":"local","generation":0,"index":5310,"job":"extract","kind":"KILL"'
    b',"run_id":"b0c9a1d2-1111-4222-8333-444455556666","run_number":4},{"a'
    b't":"2026-08-19T02:00:00.000000","effect_id":"e5310:KILL:nightly.7","'
    b'executor_id":"local","generation":0,"index":5310,"job":"nightly","ki'
    b'nd":"KILL","run_id":"3f2504e0-4f89-41d3-9a0c-0305e82c3301","run_numb'
    b'er":7}],"period_id":2,"prev_seal_digest":"sha256:1111111111111111111'
    b'111111111111111111111111111111111111111111111","request_fingerprint"'
    b':"sha256:22222222222222222222222222222222222222222222222222222222222'
    b'22222","runtime_hash":"sha256:731f24c225cef1cc9c395adff88780e6c1d6cc'
    b'40b249f8142b54a9993687702c","scheduler_admitted_through":"2026-08-19'
    b'T02:00:00.000000","source_bundle_hash":"sha256:e3b0c44298fc1c149afbf'
    b'4c8996fb92427ae41e4649b934ca495991b7852b855","state":{"consumed":{"r'
    b':FUEL":3,"r:GONE":1},"enqueue_counter":12,"globals":{"CAL\xc3\x89NDA'
    b'R":{"state_rev":3,"value":"caf\xc3\xa9\\u0001/ok"},"EMPTY":{"state_r'
    b'ev":0,"value":""}},"hosts":{"local":{"deadman_us":null,"forced_by":n'
    b'ull,"generation":0,"state":"active","state_before_quarantine":null,"'
    b'state_rev":2},"relay-2":{"deadman_us":null,"forced_by":"alice@ops-la'
    b'ptop","generation":1,"state":"evicted","state_before_quarantine":nul'
    b'l,"state_rev":4}},"jobs":{"extract":{"armed":false,"exit_code":null,'
    b'"last_end_at":null,"on_hold":false,"on_ice":false,"on_noexec":false,'
    b'"ran_members":[],"reservations":[],"run_number":4,"start_period":1,"'
    b'started_by":null,"state_rev":0,"status":"STARTING","status_at":"2026'
    b'-08-19T02:00:00.000000","waiter_seq":null,"window_skipped_members":['
    b']},"idle":{"armed":false,"exit_code":null,"last_end_at":null,"on_hol'
    b'd":false,"on_ice":false,"on_noexec":false,"ran_members":[],"reservat'
    b'ions":[],"run_number":0,"start_period":1,"started_by":null,"state_re'
    b'v":0,"status":"INACTIVE","status_at":null,"waiter_seq":null,"window_'
    b'skipped_members":[]},"latent":{"armed":true,"exit_code":0,"last_end_'
    b'at":"2026-08-18T02:00:00.000000","on_hold":false,"on_ice":false,"on_'
    b'noexec":false,"ran_members":[],"reservations":[],"run_number":9,"sta'
    b'rt_period":1,"started_by":null,"state_rev":0,"status":"SUCCESS","sta'
    b'tus_at":"2026-08-18T02:00:00.000000","waiter_seq":null,"window_skipp'
    b'ed_members":[]},"night_box":{"armed":false,"exit_code":null,"last_en'
    b'd_at":null,"on_hold":false,"on_ice":false,"on_noexec":false,"ran_mem'
    b'bers":["extract","nightly"],"reservations":[],"run_number":2,"start_'
    b'period":1,"started_by":null,"state_rev":0,"status":"RUNNING","status'
    b'_at":"2026-08-19T01:00:00.000000","waiter_seq":null,"window_skipped_'
    b'members":[]},"nightly":{"armed":false,"exit_code":null,"last_end_at"'
    b':null,"on_hold":false,"on_ice":false,"on_noexec":false,"ran_members"'
    b':[],"reservations":[{"bucket":"m:local","release_policy":"completion'
    b'","units":1},{"bucket":"r:FUEL","release_policy":"never","units":3}]'
    b',"run_number":7,"start_period":1,"started_by":"scheduler","state_rev'
    b'":0,"status":"RUNNING","status_at":"2026-08-19T01:30:00.000000","wai'
    b'ter_seq":null,"window_skipped_members":[]},"queued":{"armed":false,"'
    b'exit_code":null,"last_end_at":null,"on_hold":false,"on_ice":false,"o'
    b'n_noexec":false,"ran_members":[],"reservations":[],"run_number":3,"s'
    b'tart_period":1,"started_by":null,"state_rev":0,"status":"QUE_WAIT","'
    b'status_at":"2026-08-19T02:00:00.000000","waiter_seq":12,"window_skip'
    b'ped_members":[]},"watcher":{"armed":false,"exit_code":null,"last_end'
    b'_at":null,"on_hold":false,"on_ice":false,"on_noexec":false,"ran_memb'
    b'ers":[],"reservations":[],"run_number":1,"start_period":1,"started_b'
    b'y":null,"state_rev":0,"status":"RUNNING","status_at":"2026-08-19T01:'
    b'55:00.000000","waiter_seq":null,"window_skipped_members":[]}},"now":'
    b'"2026-08-19T02:00:00.000000","routes":{"batch":{"executor_id":"relay'
    b'-2","state_rev":5},"local":{"executor_id":"local","state_rev":0}},"t'
    b'imer_seq":41,"timers":[["2026-08-19T03:00:00.000000",40,{"at":"2026-'
    b'08-19T02:00:00.000000","kind":"TIMER","payload":{},"source":null}],['
    b'"2026-08-19T03:00:00.000000",41,{"at":"2026-08-19T02:00:00.000000","'
    b'kind":"MUST_START_ALARM","payload":{"check":"must_start","job":"nigh'
    b'tly","meta":{"digest":"data"}},"source":"scheduler"}]]},"state_machi'
    b'ne_version":1}'
)


def test_pr08_the_seal_golden_vector() -> None:
    """PR-08: the sidecar's own fixed bytes and fixed digest."""
    seal = _seal()
    assert seal.to_bytes() == GOLDEN_BYTES
    assert seal.digest == GOLDEN_DIGEST
    # the bytes decode under ss3.2's own reader, and the stamp is the value
    assert isinstance(decode(GOLDEN_BYTES), dict)
    assert json.loads(GOLDEN_BYTES)["digest"] == GOLDEN_DIGEST


def test_pr08_the_vector_exercises_the_clauses_it_is_for() -> None:
    """A vector that stopped covering a clause would go on passing. Each
    assertion here names one clause of ss3.2 the bytes must exercise."""
    text = GOLDEN_BYTES.decode("utf-8")
    assert "\\u0001" in text  # a control character, escaped lower-case
    assert "café" in text  # non-ASCII, unescaped under ensure_ascii=false
    assert "nightbank/one" in text and "\\/" not in text  # `/` never escaped
    assert '"payload":{}' in text  # an empty opaque payload
    assert '"meta":{"digest":"data"}' in text  # a NESTED digest key (PR-13)
    assert '"previous_size":null' in text  # a typed field, present and null
    assert '"ran_members":["extract","nightly"]' in text  # a set, sorted by value
    assert "2026-08-19T02:01:30.500000" in text  # six fractional digits
    assert '"deadman_us":null' in text  # ss3.3's exclusion, typed


# ----------------------------------------------- 2. close, open, close


def test_close_open_close_reproduces_the_sidecar_bytes() -> None:
    """The round trip is byte-exact: nothing the artifact carries is lost
    or re-spelled by a pass through the reader.

    The carried state, the outbox and the executions come BACK from the
    opening; the boundary's own inputs do not, because the artifact does
    not hold them in a form that rebuilds them -- `classification` records
    the verdict and its sentence, and the report's `tier` and `changed`
    are deliberately not in the seal."""
    original = _seal().to_bytes()
    opened = _open(original)
    reclosed = _seal(
        state=opened.state,
        outbox_pending=opened.outbox_pending,
        executions=opened.executions,
    )
    assert reclosed.to_bytes() == original


def test_pr07_two_openings_of_one_seal_are_byte_identical() -> None:
    """PR-07's opening half: two openings of one seal -- in place and from
    a re-decoded copy -- derive the same opening, so the `segment` records
    they write are byte-identical.

    `at` is T rather than restart wall time, and `next_period` commits
    every non-derived opening field, which is what makes that true."""
    seal = _seal()
    first = _open(seal.to_bytes())
    second = _open(json.loads(seal.to_bytes()))
    assert first == second
    records = [
        segment_record(
            _opening_manifest(opened),
            estate_id=opened.estate_id,
            at=opened.opened_at,
            opens_from_seal=opened.opens_from_seal.model_dump(),
        )
        for opened in (first, second)
    ]
    assert canonical_bytes(records[0]) == canonical_bytes(records[1])
    assert records[0]["at"] == T.isoformat()
    assert records[0]["opens_from_seal"] == {"period_id": 2, "digest": seal.digest}


def test_ss2_1_the_opening_link_is_checked_by_the_segment_schema() -> None:
    """ss2.1: `opens_from_seal` is `{period_id, digest}`, null on segment 1
    and non-null on every later segment -- every later segment opens a
    period, and a period opens from a seal.

    The field became writable with this unit, so the rule that governs it
    is checked where every segment record is read. "Any dict" would let a
    later segment name its seal by a key nothing reads."""
    opened = _open(_seal())
    manifest = _opening_manifest(opened)
    check_segment_record(
        segment_record(
            manifest,
            estate_id=opened.estate_id,
            at=opened.opened_at,
            opens_from_seal=opened.opens_from_seal.model_dump(),
        )
    )
    for wrong in ({"period_id": 2}, {"period_id": 2, "digest": "not-a-hash"}, {"seal": 2}):
        with pytest.raises(EngineError, match="opens_from_seal"):
            check_segment_record(
                segment_record(manifest, estate_id="e", at=T, opens_from_seal=wrong)
            )
    with pytest.raises(EngineError, match="opens_from_seal"):  # a later segment with no link
        check_segment_record(segment_record(manifest, estate_id="e", at=T))


def test_the_opening_names_the_seal_it_opened_from() -> None:
    """ss2.1: `opens_from_seal` is the CLOSING period's id and the
    sidecar's digest -- derived here so the writer above recomputes
    nothing."""
    seal = _seal()
    opened = _open(seal)
    assert opened.opens_from_seal.period_id == seal.period_id
    assert opened.opens_from_seal.digest == seal.digest
    assert opened.opened_at == seal.closed_at
    assert opened.epoch == 7  # the new period's first term is this + 1 (ss2.4)


# ------------------------------------------------------ 3. the digest


@pytest.mark.parametrize("key", sorted(set(Seal.model_fields) | {"digest"}))
def test_pr08_a_mutation_in_any_section_is_caught(key: str) -> None:
    """Every top-level key is under the digest -- derived from the model's
    own fields, so a section added later is covered by default."""
    document = _document()
    document[key] = _mutate(document[key])
    with pytest.raises(EngineError, match="digest|artifact_format_version"):
        _open(canonical_bytes(document))


def _mutate(value: Any) -> Any:
    """One changed bit, whatever the value's shape."""
    if isinstance(value, bool) or value is None:
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, str):
        return value + "x"
    if isinstance(value, list):
        return [*value, "extra"]
    return {**value, "extra": "extra"}


def test_pr13_only_the_top_level_digest_is_stripped() -> None:
    """PR-13: a nested payload key named `digest` is DATA. A recursive
    strip would collide two documents that differ only there."""
    document = _document()
    payload = document["state"]["timers"][1][2]["payload"]
    assert payload["meta"] == {"digest": "data"}
    payload["meta"]["digest"] = "moved"
    assert digest(document) != GOLDEN_DIGEST


def test_pr47_a_sidecar_the_naming_record_does_not_name_is_refused() -> None:
    """ss11: a sidecar whose digest matches its own bytes proves integrity,
    not that it is the one the committed record names."""
    seal = _seal()
    _open(seal.to_bytes(), expected_digest=seal.digest)
    with pytest.raises(EngineError, match="the record naming it"):
        _open(seal.to_bytes(), expected_digest="sha256:" + "ab" * 32)


def test_an_artifact_with_no_digest_is_refused() -> None:
    document = _document()
    del document["digest"]
    with pytest.raises(EngineError, match="no top-level 'digest'"):
        open_from_seal(
            canonical_bytes(document),
            expected_digest="sha256:" + "0" * 64,
            manifest=_manifest_of(_document()),
        )


# ------------------------------------------------ 4. ss3.2 at the ingress


def test_pr12_duplicate_keys_are_rejected_at_seal_decode() -> None:
    """PR-12, at this door: two `period_id` keys in one object."""
    text = GOLDEN_BYTES.decode("utf-8").replace('"period_id":2,', '"period_id":2,"period_id":3,', 1)
    with pytest.raises(EngineError, match="duplicate object key"):
        _open(text)


def test_pr11_a_float_is_refused_at_both_ends() -> None:
    """PR-11: no floats at any depth -- at decode, and at write, where a
    float in an opaque timer payload would make the estate unsealable
    while that timer is armed (PR-09)."""
    text = GOLDEN_BYTES.decode("utf-8").replace('"epoch":7', '"epoch":7.0', 1)
    with pytest.raises(EngineError, match="float"):
        _open(text)
    state = _state()
    armed = state.model_copy(
        update={
            "timers": ((T, 41, Event(at=T, kind="TIMER", payload={"interval": 1.5})),),
        }
    )
    # refused at CONSTRUCTION, not at the first serialization three calls
    # later: canonicalizability is a model invariant
    with pytest.raises(EngineError, match="float"):
        _seal(state=armed)


def test_pr10a_an_unpaired_surrogate_is_refused_at_seal_decode() -> None:
    """PR-10a: the seal never meets one, because every ingress refuses it
    -- this one included."""
    text = GOLDEN_BYTES.decode("utf-8").replace('"EMPTY":', '"\\ud800":', 1)
    with pytest.raises(EngineError, match="Unicode scalar"):
        _open(text)


def test_pr08d_a_foreign_artifact_version_is_refused_by_name() -> None:
    """PR-08d: named, at the top level and inside `next_period`."""
    document = _document()
    document["artifact_format_version"] = 99
    with pytest.raises(EngineError, match=r"artifact_format_version 99"):
        _open(canonical_bytes(document))
    document = _document()
    document["next_period"]["artifact_format_version"] = 99
    _rederive_baseline(document)  # or the baseline rule fires first
    with pytest.raises(EngineError, match=r"next_period.artifact_format_version 99"):
        _open(_restamped(document))
    # and in memory, where no decode ran: the door refuses it, and so
    # does the model a direct constructor would reach
    body = {k: v for k, v in _document().items() if k != "digest"}
    with pytest.raises(EngineError, match=r"artifact_format_version 99"):
        _open({**body, "artifact_format_version": 99, "digest": "sha256:" + "0" * 64})
    with pytest.raises(ValidationError, match=r"artifact_format_version 99"):
        Seal(**{**body, "artifact_format_version": 99})


def test_ss3_2_an_aware_datetime_is_refused() -> None:
    """The artifact is stricter than the encoder: `canon` would convert an
    aware datetime, and a seal refuses it -- the field is named, and the
    cross-field checks that compare instants never meet a mixed pair."""
    aware = datetime(2026, 8, 19, 4, 0, tzinfo=timezone(timedelta(hours=2)))
    with pytest.raises(EngineError, match="naive UTC"):
        _seal(closed_at=aware)
    assert aware.astimezone(UTC).replace(tzinfo=None) == T  # the same instant


def test_pr14_the_two_orders_are_applied_and_required() -> None:
    """PR-14: `(index, effect_id)`. `close_runtime` applies it to whatever
    admission order the outbox had; a document that arrives out of order is
    refused rather than quietly sorted."""
    seal = _seal()
    order = [e.effect_id for e in seal.outbox_pending]
    assert order == [
        "e5309:SPAWN:extract.4",  # a SPAWN precedes its own run's later KILL
        "e5310:KILL:extract.4",
        "e5310:KILL:nightly.7",  # and an index tie breaks on effect_id
    ]
    assert order.index("e5309:SPAWN:extract.4") < order.index("e5310:KILL:extract.4")
    assert [x.effect_id for x in seal.executions] == [
        "e5001:SPAWN:nightly.7",
        "e5100:SPAWN:watcher.1",
        "e5309:SPAWN:extract.4",
    ]
    document = _document()
    document["outbox_pending"].reverse()
    with pytest.raises(EngineError, match="order"):
        _open(_restamped(document))


# ------------------------------------------------- 5. ss3.5's union


@pytest.mark.parametrize(
    ("model", "fields"),
    [
        (
            PendingSpawn,
            {"job", "run_number", "effect_id", "index", "run_id", "executor_id", "generation"},
        ),
        (
            BoundRun,
            {
                "job",
                "run_number",
                "effect_id",
                "index",
                "run_id",
                "executor_id",
                "generation",
                "run_dir",
            },
        ),
        (
            FwWatch,
            {
                "job",
                "run_number",
                "effect_id",
                "index",
                "run_id",
                "watch_seq",
                "previous_size",
                "stable_polls",
                "next_poll_at",
            },
        ),
    ],
)
def test_ss3_5_each_kind_carries_exactly_the_fields_the_table_names(
    model: type, fields: set[str]
) -> None:
    """ss3.5's table, field for field. `kind` is the discriminator and is
    not in the table.

    `start_period` lives on the ROW and on NO execution entry (ss3.5,
    DL-132): draft 9 had it in both places, which is two authorities for
    one fact. Both halves are asserted now that `JobRuntime` carries it."""
    assert set(model.model_fields) - {"kind"} == fields
    assert "start_period" not in model.model_fields
    assert "start_period" in JobRuntime.model_fields  # PR-50: on the row


def test_ss3_5_an_unknown_execution_kind_is_refused() -> None:
    document = _document()
    document["executions"][0]["kind"] = "terminating"  # draft 3 carried one
    with pytest.raises(EngineError, match="kind"):
        _open(_restamped(document))


def test_ss3_5_a_kinds_own_fields_are_not_another_kinds() -> None:
    """`extra="forbid"` on each arm: a `pending_spawn` carrying a
    `run_dir` is an applied-but-unbound state ss8 forbids (PR-27)."""
    document = _document()
    document["executions"][2]["run_dir"] = "runs/extract.4"
    with pytest.raises(EngineError, match="run_dir"):
        _open(_restamped(document))


def test_ss3_5_a_pending_spawn_names_the_effects_run_id() -> None:
    """Every execution's `run_id` is the effect's (ss2.3), and a watch not
    yet dispatched is a `pending_spawn` rather than an `fw_watch`."""
    seal = _seal()
    pending = [x for x in seal.executions if x.kind == "pending_spawn"]
    assert [x.run_id for x in pending] == [RUN_EXTRACT]
    assert [e.run_id for e in seal.outbox_pending if e.kind == "SPAWN"] == [RUN_EXTRACT]


# ---------------------------------------------- 6. ss3.3, carried and not


def test_pr24a_the_seal_carries_every_host_field_but_the_two_ss3_3_excludes() -> None:
    """DERIVED from `HostRuntime`'s own fields: a field added to the host
    row later lands in the seal or reds this test. The two exclusions are
    named, and `deadman_us` is typed so that carrying one is not
    expressible."""
    live = set(HostRuntime.model_fields)
    carried = set(SealedHost.model_fields)
    assert live - carried == {"last_contact", "deadman_s"}
    assert carried - live == {"deadman_us"}  # ss3.2's float-free spelling
    assert SealedHost.model_fields["deadman_us"].annotation is type(None)


def test_pr24a_a_seal_never_carries_a_contact_or_a_deadman() -> None:
    """The bytes, not the model: the row that went in HAD both."""
    text = GOLDEN_BYTES.decode("utf-8")
    assert "last_contact" not in text
    assert "deadman_s" not in text
    assert text.count('"deadman_us":null') == 2  # both hosts
    rows = _open(GOLDEN_BYTES).host_rows
    assert rows["local"].deadman_s is None and rows["local"].last_contact is None
    assert rows["local"].state_rev == 2  # the revision IS carried (PR-24b)
    assert rows["relay-2"].state == "evicted"  # nothing here un-evicts it (PR-24c)


def test_pr19a_a_ghost_bucket_survives_the_boundary() -> None:
    """PR-19a: a `consumed` key whose resource C2 removed is retained, so
    reintroducing the resource does not refund the units."""
    opened = _open(GOLDEN_BYTES)
    assert opened.state.consumed == {"r:FUEL": 3, "r:GONE": 1}


def test_pr21_waiter_ranks_and_the_allocator_survive_the_boundary() -> None:
    opened = _open(GOLDEN_BYTES)
    assert opened.state.jobs["queued"].waiter_seq == 12
    assert opened.state.enqueue_counter == 12


def test_pr20_the_acquired_vector_crosses_with_its_run() -> None:
    opened = _open(GOLDEN_BYTES)
    held = opened.state.jobs["nightly"].reservations
    assert [(r.bucket, r.units, r.release_policy) for r in held] == [
        ("m:local", 1, "completion"),
        ("r:FUEL", 3, "never"),
    ]


def test_the_timer_heap_crosses_with_its_tokens() -> None:
    """An armed deadline is state no status field records, and the token
    carries the cross-job firing order of two timers at one instant."""
    opened = _open(GOLDEN_BYTES)
    assert [token for _due, token, _ev in opened.state.timers] == [40, 41]
    assert opened.state.timer_seq == 41
    assert opened.state.timers[0][2].payload == {}


def test_pr16b_a_route_crosses_with_its_revision() -> None:
    """ss3.3: the route table is authoritative carried state. Today's one
    implicit row is projected by `implicit_routes`; a second row proves the
    shape is the frozen one and not a special case."""
    assert implicit_routes("local")["local"].executor_id == "local"
    assert implicit_routes("local")["local"].state_rev == 0
    opened = _open(GOLDEN_BYTES)
    assert opened.state.routes["batch"].executor_id == "relay-2"
    assert opened.state.routes["batch"].state_rev == 5


def test_pr18a_the_ghost_run_gate_is_rebuilt_from_the_rows() -> None:
    """ss3.3: `_dispatched` is derived and its reconstruction is
    normative -- `{job: run_number for every row with run_number > 0}`,
    exactly as resume seeds it. An opener that left it empty would let a
    `CHANGE_STATUS STARTING` on a completed job plan its run number
    again."""
    opened = _open(GOLDEN_BYTES)
    assert opened.dispatched == {
        "extract": 4,
        "latent": 9,
        "night_box": 2,
        "nightly": 7,
        "queued": 3,
        "watcher": 1,
    }
    # the row that never ran is NOT in the gate: `_dispatched[job]` holds a
    # run number and 0 is not one -- `runner_startup` seeds it the same way
    assert opened.state.jobs["idle"].run_number == 0
    assert "idle" not in opened.dispatched


def test_ss3_3_the_seeding_order_is_stated_where_the_loader_reads_it() -> None:
    """The five rules ss7 step 3-5 pins live on `OpenedRuntime`, because
    the loader that seeds an engine reads them there. A doc-string test,
    deliberately: the rules are the contract this unit hands over."""
    rules = OpenedRuntime.__doc__ or ""
    assert "VERBATIM" in rules
    assert "new rows" in rules
    assert "never renormalized" in rules


# ------------------------------------- 7. ss3.4 and ss4, the opening


def test_pr47d_the_baseline_id_is_derived_and_reproducible() -> None:
    """PR-47d: audit reproduces it from `{estate_id, period_id,
    stage_digest}` -- pre-boundary evidence -- rather than copying it out
    of the seal it is auditing."""
    seal = _seal()
    opening = seal.next_period
    assert opening.baseline_id == baseline_id_for(
        estate_id=seal.estate_id,
        period_id=opening.period_id,
        stage_digest=_staged().stage_digest,
    )
    assert opening.stage_digest == _staged().stage_digest
    # ss4: every transition derives a FRESH one, so a command composed
    # under C1 cannot be accepted against C2 semantics
    assert opening.baseline_id != seal.baseline_id


def test_pr47d_a_moved_stage_digest_moves_the_baseline_id() -> None:
    """A different staged identity is a different boundary, so it cannot
    open under the same baseline."""
    other = _staged().model_copy(update={"state_machine_version": 1, "runtime_hash": "sha256:x"})
    assert other.stage_digest != _staged().stage_digest
    assert baseline_id_for(
        estate_id="nightbank/one", period_id=3, stage_digest=other.stage_digest
    ) != baseline_id_for(
        estate_id="nightbank/one", period_id=3, stage_digest=_staged().stage_digest
    )
    # and the estate and the period are in it too
    assert (
        baseline_id_for(estate_id="other", period_id=3, stage_digest=_staged().stage_digest)
        != _seal().next_period.baseline_id
    )
    assert (
        baseline_id_for(estate_id="nightbank/one", period_id=4, stage_digest=_staged().stage_digest)
        != _seal().next_period.baseline_id
    )


def test_pr05b_first_index_does_not_move_the_stage_digest() -> None:
    """PR-05b: `first_index` is derived boundary OUTPUT, so a retry that
    closes at a different index stages the same identity -- and the
    committed form's digest over the staged half is unchanged."""
    later = _seal(closes_at_index=5399)
    assert later.next_period.first_index == 5400
    assert later.next_period.stage_digest == _seal().next_period.stage_digest
    assert later.next_period.baseline_id == _seal().next_period.baseline_id


def test_pr05c_a_client_cannot_stage_the_engine_derived_fields() -> None:
    """PR-05c: period 2 could otherwise open period 4, and the attestation
    the induction requires could never exist."""
    for field in ("period_id", "segment_no", "baseline_id", "clock_domain", "first_index"):
        with pytest.raises(ValueError, match=field):
            StagedNextPeriod(
                catalog_hash=catalog_hash_v2(CATALOG),
                source_bundle_hash=EMPTY_BUNDLE_HASH,
                runtime_hash=runtime_hash(PROFILE),
                state_machine_version=1,
                **{field: 4},
            )
    opening = _seal().next_period
    assert (opening.period_id, opening.segment_no) == (3, 3)
    assert opening.clock_domain == "real"
    assert opening.first_index == 5311


def test_pr08e_the_two_next_period_models_are_not_one() -> None:
    """PR-08e: the committed form never validates as a staged one, so a
    reader cannot accept the wrong half."""
    committed = _seal().next_period.model_dump()
    with pytest.raises(ValueError, match="period_id"):
        StagedNextPeriod(**committed)
    assert set(CommittedNextPeriod.model_fields) - set(StagedNextPeriod.model_fields) == {
        "period_id",
        "segment_no",
        "baseline_id",
        "clock_domain",
        "first_index",
    }


def test_pr22_the_shared_field_list_is_every_field_the_two_really_share() -> None:
    """Derived here TOO, and independently: parametrizing the sweep below
    over the constant it tests would let a narrowed constant delete its own
    cases silently -- which is the failure DL-83 names."""
    assert set(_SHARED_WITH_MANIFEST) == set(Manifest.model_fields) & set(
        CommittedNextPeriod.model_fields
    )
    assert "runtime_profile" not in _SHARED_WITH_MANIFEST  # the manifest's alone


@pytest.mark.parametrize(
    "field", sorted(set(Manifest.model_fields) & set(CommittedNextPeriod.model_fields))
)
def test_pr22_every_field_the_manifest_shares_with_the_opening_is_checked(field: str) -> None:
    """PR-22: the committed manifest is the engine's own output, and a
    disagreement in ANY shared field means it is not this boundary's. The
    sweep is derived from the two models."""
    seal = _seal()
    manifest = _opening_manifest(_open(seal))
    _open(seal, manifest=manifest)  # agrees
    value = getattr(manifest, field)
    wrong = manifest.model_copy(update={field: value + 1 if isinstance(value, int) else "x"})
    with pytest.raises(EngineError, match=field):
        _open(seal, manifest=wrong)


def test_the_artifact_comparisons_are_one_walk() -> None:
    """DL-137: the tree compared two artifacts field by field in seven
    dialects. One walk now (`period.disagreements`), and this pins that it
    is really shared rather than merely available.

    TWO fields are moved at once, and both owners -- `seal._check_manifest`
    (PR-22) and `period.check_manifest_against_segment` (ss2.1) -- must
    report BOTH, in their own field order, with their own wording. A walk
    that stopped at the first disagreement, dropped a field or reordered
    the pair reds both halves together. The wordings stay apart on
    purpose: which artifact to go and look at is what the message says,
    and only the owner knows that."""
    from dsl41.period import check_manifest_against_segment
    from dsl41.period import disagreements as walk

    seal = _seal()
    manifest = _opening_manifest(_open(seal))
    record = segment_record(manifest, estate_id="e", at=datetime(2026, 8, 20, 4, 0))
    _open(seal, manifest=manifest)  # both agree, both silent
    check_manifest_against_segment(manifest, record)

    moved = manifest.model_copy(
        update={"clock_domain": "wall", "first_index": manifest.first_index + 1}
    )
    detail = (
        f"clock_domain: manifest 'wall' vs {{side}} {manifest.clock_domain!r};"
        f" first_index: manifest {moved.first_index} vs {{side}} {manifest.first_index}"
    )
    with pytest.raises(EngineError) as refused_by_seal:
        _open(seal, manifest=moved)
    assert str(refused_by_seal.value) == (
        "the committed manifest disagrees with the boundary that committed it"
        f" ({detail.format(side='next_period')}): this manifest is not this seal's (PR-22)"
    )
    with pytest.raises(EngineError) as refused_by_period:
        check_manifest_against_segment(moved, record)
    assert str(refused_by_period.value) == (
        "period manifest disagrees with the journal's segment record"
        f" ({detail.format(side='segment')}): this manifest is not this segment's"
        " (period-model ss2.1)"
    )

    # the two sides are read differently ON PURPOSE: a mapping's absent key
    # is a value that disagrees, a model's missing field is a caller bug
    assert walk(manifest, {}, ["clock_domain"]) == [("clock_domain", manifest.clock_domain, None)]
    with pytest.raises(AttributeError):
        walk(manifest, manifest, ["no_such_field"])


# ------------------------------------- 8. ss7 step 6, one failure each

#: One injected failure per load invariant (PR-22), each with the message
#: fragment ONLY the rule it breaks produces. The fragment is the whole
#: point: a bare `pytest.raises(EngineError)` passes when a case trips a
#: different rule, and three of these cases did exactly that before the
#: fragments were pinned -- a duplicate rank caught by the reservation
#: rule, and two `next_period` cases caught by the baseline derivation
#: they also moved.
Mutation = Callable[[dict[str, Any]], None]
_INVARIANTS: dict[str, tuple[Mutation, str]] = {}


def _case(name: str, expected: str) -> Callable[[Mutation], Mutation]:
    """Register one injected failure under the rule it breaks."""

    def register(mutate: Mutation) -> Mutation:
        _INVARIANTS[name] = (mutate, expected)
        return mutate

    return register


def _rederive_baseline(document: dict[str, Any]) -> None:
    """Recompute `next_period.baseline_id` after a case moved something it
    is derived from -- so the case fails on ITS rule and not on PR-47d's.

    A boundary that moved a staged field and left the baseline alone is a
    different case, and it has its own entry ("a minted baseline")."""
    opening = document["next_period"]
    staged = StagedNextPeriod(**{name: opening[name] for name in StagedNextPeriod.model_fields})
    opening["baseline_id"] = baseline_id_for(
        estate_id=document["estate_id"],
        period_id=opening["period_id"],
        stage_digest=staged.stage_digest,
    )


@_case("timer token twice", "appears twice")
def _duplicate_token(document: dict[str, Any]) -> None:
    document["state"]["timers"][0][1] = 41


@_case("timer token above the allocator", "above timer_seq")
def _token_above_seq(document: dict[str, Any]) -> None:
    document["state"]["timer_seq"] = 40


@_case("timer token not positive", "tokens are positive")
def _token_zero(document: dict[str, Any]) -> None:
    document["state"]["timers"][0][1] = 0


@_case("timers out of order", r"not in \(due, token\) order")
def _timers_unsorted(document: dict[str, Any]) -> None:
    document["state"]["timers"].reverse()


@_case("a rank without a queue", "held exactly while QUE_WAIT")
def _rank_without_que_wait(document: dict[str, Any]) -> None:
    document["state"]["jobs"]["nightly"]["waiter_seq"] = 5


@_case("a queue without a rank", "held exactly while QUE_WAIT")
def _que_wait_without_rank(document: dict[str, Any]) -> None:
    document["state"]["jobs"]["queued"]["waiter_seq"] = None


@_case("two jobs at one rank", "share waiter_seq")
def _duplicate_rank(document: dict[str, Any]) -> None:
    # `night_box`, which holds no reservations: flipping a row that did
    # would be caught by the reservation rule instead
    document["state"]["jobs"]["night_box"]["status"] = "QUE_WAIT"
    document["state"]["jobs"]["night_box"]["waiter_seq"] = 12


@_case("a rank that is not positive", "is not positive")
def _rank_not_positive(document: dict[str, Any]) -> None:
    document["state"]["jobs"]["queued"]["waiter_seq"] = 0


@_case("a rank above the allocator", "above enqueue_counter")
def _rank_above_counter(document: dict[str, Any]) -> None:
    document["state"]["enqueue_counter"] = 11


@_case("negative consumption", "invented capacity")
def _negative_consumed(document: dict[str, Any]) -> None:
    document["state"]["consumed"]["r:FUEL"] = -3


@_case("a terminal row still holding units", "still holds")
def _reservations_after_terminal(document: dict[str, Any]) -> None:
    document["state"]["jobs"]["latent"]["reservations"] = [
        {"bucket": "r:FUEL", "units": 1, "release_policy": "never"}
    ]


@_case("one bucket twice in one vector", "reservation vector")
def _duplicate_bucket(document: dict[str, Any]) -> None:
    document["state"]["jobs"]["nightly"]["reservations"][0]["bucket"] = "r:FUEL"


@_case("units not positive", "greater than 0")
def _zero_units(document: dict[str, Any]) -> None:
    document["state"]["jobs"]["nightly"]["reservations"][0]["units"] = 0


@_case("a route naming no host row", "has no host row")
def _route_without_host(document: dict[str, Any]) -> None:
    document["state"]["routes"]["batch"]["executor_id"] = "relay-9"


@_case("an execution with no row", "with no row")
def _execution_without_row(document: dict[str, Any]) -> None:
    document["executions"][0]["job"] = "absent"


@_case("an execution behind a terminal row", "with SUCCESS")
def _execution_behind_terminal(document: dict[str, Any]) -> None:
    document["executions"][0]["job"] = "latent"


@_case("the row and the entry disagree on the run", "which run is live")
def _run_number_disagreement(document: dict[str, Any]) -> None:
    document["executions"][0]["run_number"] = 6


@_case("the effect and the entry disagree on the run_id", "describe one attempt")
def _run_id_disagreement(document: dict[str, Any]) -> None:
    document["executions"][2]["run_id"] = RUN_WATCHER


@_case("a run_id outside the grammar", "outside the")
def _run_id_grammar(document: dict[str, Any]) -> None:
    document["executions"][0]["run_id"] = "watcher-1"


@_case("two entries for one effect", "two execution entries")
def _two_entries_one_effect(document: dict[str, Any]) -> None:
    document["executions"][1]["effect_id"] = document["executions"][0]["effect_id"]


@_case("a pending SPAWN with no counterpart", "in executions")
def _pending_spawn_unmatched(document: dict[str, Any]) -> None:
    document["executions"] = [x for x in document["executions"] if x["kind"] != "pending_spawn"]


@_case("a watch with no start line", "greater than or equal to 1")
def _watch_seq_zero(document: dict[str, Any]) -> None:
    # ss3.5: a dispatched watch always has `watch_seq >= 1`, because the
    # adapter's first durable act is a `start` line
    document["executions"][1]["watch_seq"] = 0


@_case("an absolute run directory", "not relative to the estate root")
def _absolute_run_dir(document: dict[str, Any]) -> None:
    document["executions"][0]["run_dir"] = "/var/lib/estate/runs/nightly.7"


@_case("now past the cutoff", "state.now")
def _now_past_t(document: dict[str, Any]) -> None:
    document["state"]["now"] = "2026-08-19T02:00:01.000000"


@_case("admission past the cutoff", "scheduler_admitted_through")
def _admitted_past_t(document: dict[str, Any]) -> None:
    document["scheduler_admitted_through"] = "2026-08-19T02:00:01.000000"


@_case("an opening this binary cannot implement", "next_period.artifact_format_version 99")
def _foreign_opening_version(document: dict[str, Any]) -> None:
    document["next_period"]["artifact_format_version"] = 99
    _rederive_baseline(document)


@_case("a reused index", "reused index")
def _reused_index(document: dict[str, Any]) -> None:
    document["next_period"]["first_index"] = 5310


@_case("a skipped period", "does not follow")
def _skipped_period(document: dict[str, Any]) -> None:
    document["next_period"]["period_id"] = 4
    document["next_period"]["segment_no"] = 4
    _rederive_baseline(document)


@_case("a segment that is not its period", "one number")
def _segment_no_disagrees(document: dict[str, Any]) -> None:
    document["next_period"]["segment_no"] = 2


@_case("a clock-domain change", "domain change is refused")
def _clock_domain_change(document: dict[str, Any]) -> None:
    document["next_period"]["clock_domain"] = "virtual"


@_case("a state-machine bump", "SM bump")
def _sm_version_change(document: dict[str, Any]) -> None:
    document["next_period"]["state_machine_version"] = 2
    _rederive_baseline(document)


@_case("a minted baseline", "never minted")
def _minted_baseline(document: dict[str, Any]) -> None:
    document["next_period"]["baseline_id"] = "sha256:" + "cd" * 32


@_case("an R verdict in a committed seal", "classified R")
def _committed_r(document: dict[str, Any]) -> None:
    document["classification"]["nightly"]["class"] = "R"


@_case("a pending_spawn entry with no effect behind it", "dispatches nothing")
def _entry_without_intent(document: dict[str, Any]) -> None:
    document["outbox_pending"] = [
        effect for effect in document["outbox_pending"] if effect["kind"] != "SPAWN"
    ]


@_case("the effect and the entry disagree on the executor", "describe one attempt")
def _executor_disagreement(document: dict[str, Any]) -> None:
    # [2] is the pending_spawn (index 5309, canonically last): the loop
    # compares pending EFFECTS against entries, so the paired one must move
    document["executions"][2]["executor_id"] = "relay-2"


@_case("one run_id claimed by two runs", "one identity, one run")
def _one_id_two_runs(document: dict[str, Any]) -> None:
    document["executions"][1]["run_id"] = document["executions"][0]["run_id"]


@_case("one run bound to two run_ids", "one run, one identity")
def _one_run_two_ids(document: dict[str, Any]) -> None:
    entry = dict(document["executions"][0])
    entry["effect_id"] = "e9999:KILL:" + entry["job"] + "." + str(entry["run_number"])
    entry["run_id"] = "d4e5f6a7-7777-4888-9999-aaaabbbbcccc"  # fresh: the RUN is the dup
    entry["kind"] = "bound"
    entry["run_dir"] = "runs/x.1"
    entry["index"] = 9999  # keeps (index, effect_id) order: the claim is the point
    document["executions"].append(entry)


@_case("a forced gate under no force", "explicit force")
def _gate_without_force(document: dict[str, Any]) -> None:
    document["boundary_request"]["force_seal"] = False


@_case("a forced gate recording a passing age", "passing gate is null")
def _gate_with_passing_age(document: dict[str, Any]) -> None:
    document["forced_gate"]["observed_age_us"] = document["forced_gate"]["horizon_us"]


@_case("a catalog recipe this binary cannot audit", "pins the current recipe")
def _foreign_recipe(document: dict[str, Any]) -> None:
    document["catalog_hash_version"] = 99


@_case("two pending effects under one id", "one effect is one intent")
def _duplicate_pending_effect(document: dict[str, Any]) -> None:
    # the LAST one, twice: appending a copy of the first would trip the
    # (index, effect_id) order rule before the uniqueness rule
    document["outbox_pending"].append(dict(document["outbox_pending"][-1]))


@_case("a pending effect past the cutoff", "no WAL position derives it")
def _future_effect(document: dict[str, Any]) -> None:
    # the LAST effect and its paired entry move together: both lists are
    # (index, effect_id)-sorted and both stay last, so only the bound rule
    # fires -- and the shared-field rule stays satisfied
    beyond = document["closes_at_index"] + 1
    moved = document["outbox_pending"][-1]
    moved["index"] = beyond
    for entry in document["executions"]:
        if entry["effect_id"] == moved["effect_id"]:
            entry["index"] = beyond


@_case("a row started in a period that has not happened", "cannot start in a period")
def _future_start_period(document: dict[str, Any]) -> None:
    document["state"]["jobs"]["nightly"]["start_period"] = document["period_id"] + 1


@_case("a negative run_number", "never go back")
def _negative_run_number(document: dict[str, Any]) -> None:
    document["state"]["jobs"]["idle"]["run_number"] = -1


@_case("an address outside the grammar", "not a sha256 address")
def _freehand_address(document: dict[str, Any]) -> None:
    document["request_fingerprint"] = "x"


@_case("a pending effect naming run zero", "names a real run")
def _pending_run_zero(document: dict[str, Any]) -> None:
    # the EFFECT alone: the entry model already requires run_number >= 1
    # on its side, and the close-side native rule runs before the pair
    # comparison, so this meets exactly the rule under test
    spawn = next(e for e in document["outbox_pending"] if e["kind"] == "SPAWN")
    spawn["run_number"] = 0


@_case("a sealed effect with no generation", "binds a real identity")
def _sealed_effect_no_generation(document: dict[str, Any]) -> None:
    # the UNPAIRED KILL: the entry models require identity on their side,
    # so the close-side native rule is the only gate this can meet
    kill = next(e for e in document["outbox_pending"] if e["kind"] == "KILL")
    kill["generation"] = None


@_case("a sealed effect with a negative generation", "binds a real identity")
def _sealed_effect_negative_generation(document: dict[str, Any]) -> None:
    kill = next(e for e in document["outbox_pending"] if e["kind"] == "KILL")
    kill["generation"] = -1


@_case("a sealed SPAWN with no run_id", "carries no run_id")
def _sealed_spawn_no_run_id(document: dict[str, Any]) -> None:
    # the EFFECT alone: the entry model requires its identity, and the
    # native rule fires before the pair comparison could
    for effect in document["outbox_pending"]:
        if effect["kind"] == "SPAWN":
            effect["run_id"] = None


@_case("a pending effect with a freehand run_id", "outside the ss11a grammar")
def _pending_freehand_run_id(document: dict[str, Any]) -> None:
    moved = document["outbox_pending"][-1]
    moved["run_id"] = "freehand"
    for entry in document["executions"]:
        if entry["effect_id"] == moved["effect_id"]:
            entry["run_id"] = "freehand"


@_case("a free-text baseline on a later period", "never free text")
def _free_text_baseline(document: dict[str, Any]) -> None:
    document["baseline_id"] = "not-an-address"


@_case("a mid-lineage seal that terminates it", "terminates a lineage")
def _terminated_lineage(document: dict[str, Any]) -> None:
    document["prev_seal_digest"] = None


@_case("a timestamp with an offset", "naive UTC")
def _aware_timestamp(document: dict[str, Any]) -> None:
    document["closed_at"] = "2026-08-19T04:00:00+02:00"


@_case("an A with no sentence", "records its sentence")
def _a_without_assumption(document: dict[str, Any]) -> None:
    document["classification"]["latent"]["assumption"] = None


@pytest.mark.parametrize("case", sorted(_INVARIANTS))
def test_pr22_open_from_seal_refuses_each_load_invariant(case: str) -> None:
    """PR-22: one injected failure per invariant, each with the digest
    recomputed so the refusal is the invariant's and not the digest's, and
    each matched on the message only its own rule produces."""
    mutate, expected = _INVARIANTS[case]
    document = _document()
    mutate(document)
    with pytest.raises(EngineError, match=expected):
        _open(_restamped(document))


def test_pr22_every_stated_load_rule_has_an_injected_failure() -> None:
    """The rule count is DERIVED from the module's own source, so a load
    rule added later without a case reds this test.

    A count alone would not be worth much -- it cannot say WHICH rule a
    case exercised, and a case that tripped a neighbour would still be
    counted. The `match` fragment above is what says that; this says the
    sweep is not missing a rule."""
    rules = inspect.getsource(dsl41.seal).count("raise ValueError(")
    # EXACT in both directions, by a maintained pair: the count fails when a
    # rule is added OR removed without touching this test, and each case
    # above proves its own rule by the RENDERED message fragment (the sweep
    # would go green on a neighbour otherwise). A source-template match was
    # tried and rejected: f-string concatenation and value-bearing
    # fragments ("artifact_format_version 99") make it archaeology, not a
    # gate. When this fails: add or remove the case, then move the count.
    # DL-178(c): the sha256-address loop's `raise ValueError(` moved to
    # `period.check_addresses`, one owner for the rule three artifacts
    # wrote by hand -- the case stays (it still trips the same message
    # through the shared helper) and only the source-local count drops.
    assert rules == 52, f"{rules} ValueError sites in seal.py; the registry expects 52"
    assert len(_INVARIANTS) == 54  # two cases share the shared-field rule's site,
    # one shares the one-identity site, the two index bounds share one case
    # (the join makes them move together), and canonicalizability is proven
    # by test_pr11 directly -- 48 cases over 46 sweepable sites


def test_pr22a_a_live_row_with_no_execution_entry_opens() -> None:
    """PR-22a: the join is ONE WAY. A `CHANGE_STATUS STARTING` overwrite
    leaves a STARTING row with no intent and no process, and a RUNNING BOX
    has no adapter, no effect and no entry -- a two-way join would refuse
    both legal estates."""
    state = _state()
    lonely = state.model_copy(
        update={
            "jobs": {
                **state.jobs,
                "overwritten": JobRuntime(status="STARTING", status_at=T, run_number=1),
            }
        }
    )
    opened = _open(_seal(state=lonely).to_bytes())
    assert opened.state.jobs["overwritten"].status == "STARTING"
    assert "night_box" not in {x.job for x in opened.executions}  # the live box


def test_ss10_1_a_committed_seal_never_carries_an_r_verdict() -> None:
    """ss10.1: R means the boundary does not commit until the run is done
    or killed, so `close_runtime` refuses to write one down."""
    refusing = Classification(
        verdicts=(
            JobVerdict(job="nightly", tier="executing", verdict="R", changed=("resource:FUEL",)),
        ),
        # the R gate reads `refused`, which the classifier derives; a
        # hand-built Classification must carry it the same way
    )
    with pytest.raises(EngineError, match="refuses the boundary"):
        _seal(classification=refusing)  # close's gate, on the classifier's object
    # and the MODEL's own gate holds even if a projection sneaks past close:
    # the document sweep's "an R verdict in a committed seal" case pins that
    # layer; here the direct construction shows the same refusal
    document = _document()
    document["classification"]["nightly"] = {"class": "R", "assumption": None}
    with pytest.raises(EngineError, match="classified R"):
        Seal.from_payload(with_digest({k: v for k, v in document.items() if k != "digest"}))


def test_the_classification_map_projects_the_verdict_and_the_assumption() -> None:
    """ss3.1: the seal records the ss10 verdict and every A assumption --
    the two fields of `JobVerdict`, and not the report's `tier` and
    `changed`."""
    seal = _seal()
    assert seal.classification == {
        "latent": SealedVerdict(verdict="A", assumption=ARMED_ASSUMPTION),
        "nightly": SealedVerdict(verdict="carry"),
    }
    assert json.loads(seal.to_bytes())["classification"]["latent"] == {
        "class": "A",
        "assumption": ARMED_ASSUMPTION,
    }


# ------------------------------------------------ 9. the shape, in full


def test_ss3_1_the_sidecar_carries_exactly_the_sections_the_spec_names() -> None:
    """ss3.1's top level, pinned. A section added or dropped without the
    spec moving is what this catches -- and no section may be called
    `digest`, because the digest strips only the top-level key of that
    name (PR-13)."""
    assert set(json.loads(GOLDEN_BYTES)) == {
        "artifact_format_version",
        "estate_id",
        "period_id",
        "baseline_id",
        "catalog_hash",
        "catalog_hash_version",
        "source_bundle_hash",
        "runtime_hash",
        "state_machine_version",
        "closes_at_index",
        "closed_at",
        "clock_domain",
        "epoch",
        "prev_seal_digest",
        "scheduler_admitted_through",
        "boundary_request",
        "request_fingerprint",
        "forced_gate",
        "state",
        "outbox_pending",
        "executions",
        "classification",
        "next_period",
        "digest",
    }
    assert set(json.loads(GOLDEN_BYTES)["state"]) == {
        "jobs",
        "globals",
        "hosts",
        "routes",
        "timers",
        "timer_seq",
        "consumed",
        "enqueue_counter",
        "now",
    }
    assert "digest" not in set(Seal.model_fields)


def test_ss3_1_the_boundary_request_is_input_and_the_gate_is_output() -> None:
    """ss3.1's truth table, at the shape level: `force_seal` is what the
    request claimed, and `forced_gate` is null unless a gate was actually
    engaged -- an unnecessary force records the claim and no gate."""
    unforced = _seal(forced_gate=None)
    assert unforced.boundary_request.force_seal is True
    assert unforced.forced_gate is None
    assert json.loads(unforced.to_bytes())["forced_gate"] is None
    assert _seal().forced_gate is not None


def test_ss3_1_the_seal_takes_its_identity_from_the_closing_manifest() -> None:
    """One authority: a caller cannot compose a seal that describes a
    period its committed manifest does not."""
    closing = _closing()
    seal = _seal()
    for field in ("period_id", "baseline_id", "catalog_hash", "runtime_hash", "clock_domain"):
        assert getattr(seal, field) == getattr(closing, field)


def test_pr52_the_timer_allocator_is_published_and_watched() -> None:
    """The seal carries the allocator's high-water mark, so the owner
    publishes it -- the heap can be empty while the allocator stands at
    41, and an opener that restarted from 0 would re-issue tokens.

    Published means owned: PR-52's gate watches a rebind of the scalar
    from outside `RuntimeState`, exactly as it watches `enqueue_counter`,
    which is the other allocator a seal carries."""
    store = RuntimeState()
    assert store.timer_seq == 0
    store.enqueue_timer(T, Event(at=T, kind="TIMER", payload={"job": "nightly"}))
    assert store.timer_seq == 1
    assert store.timers() == [(T, 1, Event(at=T, kind="TIMER", payload={"job": "nightly"}))]

    spec = importlib.util.spec_from_file_location(
        "arch_check", Path(__file__).resolve().parent.parent / "scripts" / "arch_check.py"
    )
    assert spec is not None and spec.loader is not None
    gate = importlib.util.module_from_spec(spec)
    sys.modules["arch_check"] = gate
    spec.loader.exec_module(gate)
    assert {"_timer_seq", "timer_seq"} <= set(gate._STATE_MAPS)


# ------------------------------------------- 10. the doors and the backstops


def test_a_document_that_is_not_an_object_is_refused() -> None:
    """Both doors: the bytes one, where the decoder returns a list, and
    the in-memory one, where a caller hands over the wrong thing."""
    with pytest.raises(EngineError, match="expected an object"):
        open_from_seal(
            b"[]", expected_digest="sha256:" + "0" * 64, manifest=_manifest_of(_document())
        )
    with pytest.raises(EngineError, match="expected an object"):
        open_from_seal(
            [],  # type: ignore[arg-type]
            expected_digest="sha256:" + "0" * 64,
            manifest=_manifest_of(_document()),
        )


def test_the_in_memory_door_runs_the_same_ss3_2_checks() -> None:
    """A caller with a decoded document skips `decode`, so `from_payload`
    runs the version check and the digest over the document itself."""
    body = {k: v for k, v in _document().items() if k != "digest"}
    with pytest.raises(EngineError, match="float"):
        _open({**body, "epoch": 7.5, "digest": "sha256:" + "0" * 64})


def test_a_non_canonical_document_is_refused_even_with_its_own_digest() -> None:
    """ss3.2: the digest is over a CANONICAL serialization. A file whose
    reservations are in acquisition order, stamped with a digest computed
    over those very bytes, passes the tamper check and is still not this
    artifact."""
    document = _document()
    held = document["state"]["jobs"]["nightly"]["reservations"]
    document["state"]["jobs"]["nightly"]["reservations"] = list(reversed(held))
    with pytest.raises(EngineError, match="canonical form"):
        _open(_restamped(document))


def test_an_aware_datetime_inside_a_carried_row_is_caught_by_the_backstop() -> None:
    """`Event`, `Effect` and `JobRuntime` are not this module's models to
    annotate, so the projection is what refuses an offset that reached one
    of them."""
    state = _state()
    aware = state.model_copy(
        update={
            "jobs": {
                **state.jobs,
                "extract": JobRuntime(
                    status="STARTING",
                    status_at=datetime(2026, 8, 19, 4, tzinfo=timezone(timedelta(hours=2))),
                    run_number=4,
                ),
            }
        }
    )
    with pytest.raises(EngineError, match="naive UTC"):
        _seal(state=aware).to_bytes()


def test_a_set_of_mixed_types_has_no_canonical_order() -> None:
    """ss3.2 sorts a set by value. A payload that put two kinds in one set
    has no such order, and the refusal names the site rather than raising
    a `TypeError` out of the encoder."""
    state = _state()
    mixed = state.model_copy(
        update={"timers": ((T, 41, Event(at=T, kind="TIMER", payload={"s": {1, "a"}})),)}
    )
    with pytest.raises(EngineError, match="canonical order"):
        _seal(state=mixed).to_bytes()


def test_a_projected_host_row_is_projected_once() -> None:
    """`SealedState` accepts live rows and carried ones, so a re-close of
    an opened seal does not project a projection."""
    carried = SealedHost(state="passive", generation=2, state_rev=9)
    assert SealedHost.of(carried) is carried
    assert SealedState(now=T, hosts={"h": carried}).hosts["h"] is carried
    assert SealedState(now=T).hosts == {}


def test_a_seal_mutated_after_validation_refuses_to_serialize() -> None:
    """frozen stops attribute assignment, not mutation inside a dict field:
    a writer that stamped a digest over a post-validation mutation would
    emit a self-consistent artifact only the NEXT reader refuses.
    `to_bytes` revalidates on the way out."""
    seal = _seal()
    # in-place, past the frozen model: an R verdict may never be committed,
    # and only the exit revalidation can catch one smuggled in like this
    seal.classification["smuggled"] = SealedVerdict(verdict="R")
    with pytest.raises(EngineError, match="mutated after validation"):
        seal.to_bytes()


def test_a_padded_copy_with_the_same_digest_is_not_the_artifact() -> None:
    """The digest is over the CANONICAL form, so a whitespace-padded or
    key-reordered copy carries the same digest as the real sidecar --
    accepting it would let two byte-forms of one seal circulate under one
    name. The file's bytes must BE the canonical bytes."""
    padded = GOLDEN_BYTES.decode("utf-8").replace('"epoch":7,', '"epoch": 7,', 1)
    assert json.loads(padded) == json.loads(GOLDEN_BYTES)  # same document
    with pytest.raises(EngineError, match="one byte string"):
        Seal.from_bytes(padded)


def test_the_lineage_link_names_the_predecessor(tmp_path: Path) -> None:
    """The lineage link, period.py's half: segment N opens exactly the seal
    that closed N-1 -- a link naming any other period is a graft."""
    from dsl41.period import check_segment_record

    document = _document()
    opened = _open(GOLDEN_BYTES)
    record = segment_record(
        _opening_manifest(opened),
        estate_id=document["estate_id"],
        at=T,
        opens_from_seal={"period_id": document["period_id"], "digest": document["digest"]},
    )
    check_segment_record(record)  # the true link passes
    grafted = {**record, "opens_from_seal": {**record["opens_from_seal"], "period_id": 99}}
    with pytest.raises(EngineError, match="predecessor"):
        check_segment_record(grafted)


# ------------------------------------------------ round-3 review pins (DL-132)


def test_an_identity_less_effect_under_an_identified_entry_refuses() -> None:
    """(red-on-deletion for the exact-null comparison): a None on the effect
    where the entry names an identity is a disagreement, not an unknown --
    a None-skipping comparison would bless an intent that cannot dispatch
    under the identity it claims."""
    document = _document()
    for effect in document["outbox_pending"]:
        if effect["kind"] == "SPAWN":
            effect["run_id"] = None
            effect["generation"] = None
    # since round 4 the NATIVE-identity rule fires first (a sealed effect
    # binds identity at birth) -- one gate earlier than the pair
    # comparison, same refusal
    with pytest.raises(EngineError, match="binds a real identity"):
        Seal.from_payload(with_digest({k: v for k, v in document.items() if k != "digest"}))


def test_an_in_memory_seal_mutated_after_validation_cannot_open() -> None:
    """(red-on-deletion for the opening revalidation): this is the LAST gate
    before an engine seeds itself, and a digest recomputed over the
    mutation would otherwise bless it."""
    seal = _seal()
    seal.classification["smuggled"] = SealedVerdict(verdict="R")
    with pytest.raises(EngineError, match="mutated after validation"):
        _open(seal, expected_digest=digest(seal.to_payload()))


def test_i1_a_segment_declaring_another_period_refuses() -> None:
    """(red-on-deletion for the I1 equality): segment 1 declaring period 3
    would leave a null lineage link over a period that needs one."""
    from dsl41.period import check_segment_record

    document = _document()
    opened = _open(GOLDEN_BYTES)
    record = segment_record(
        _opening_manifest(opened),
        estate_id=document["estate_id"],
        at=T,
        opens_from_seal={"period_id": document["period_id"], "digest": document["digest"]},
    )
    with pytest.raises(EngineError, match="same number"):
        check_segment_record({**record, "segment_no": 1})


def test_two_verdicts_for_one_job_refuse_before_projection() -> None:
    """(red-on-deletion for the duplicate gate): the map build would let a
    later carry silently overwrite an R the model then never sees."""
    doubled = Classification(
        verdicts=(
            JobVerdict(job="nightly", tier="executing", verdict="R", changed=("job:nightly",)),
            JobVerdict(job="nightly", tier="not_live", verdict="carry"),
        )
    )
    with pytest.raises(EngineError, match="refuses the boundary|two verdicts"):
        _seal(classification=doubled)
    quiet_double = Classification(
        verdicts=(
            JobVerdict(job="nightly", tier="not_live", verdict="carry"),
            JobVerdict(job="nightly", tier="not_live", verdict="carry"),
        )
    )
    with pytest.raises(EngineError, match="two verdicts"):
        _seal(classification=quiet_double)


def test_pr50_a_start_in_period_two_stamps_the_row(tmp_path: Path) -> None:
    """`open_period` is the ONE write to the period
    counter -- owner-verb-gated, monotone by one, never inside an input --
    and a start after it stamps the row with the new period, which is what
    crosses the next seal."""
    from dsl41.ir import lower_source
    from dsl41.oracle import Oracle
    from dsl41.oracle_state import Event

    oracle = Oracle(lower_source("insert_job: j\njob_type: c\ncommand: x\n"))
    store = oracle.store
    store.seed_period(2)  # assembly's explicit seed
    oracle.feed(Event(at=T, kind="STARTJOB", payload={"job": "j"}))
    assert store.job["j"].start_period == 2  # the stamp, PR-50's fact
    # touched now: only the live rules remain
    with pytest.raises(ValueError, match="exactly one"):
        store.open_period(5)  # a skip
    with pytest.raises(ValueError, match="exactly one"):
        store.open_period(2)  # a repeat
    store.begin_input()
    try:
        with pytest.raises(ValueError, match="not an input"):
            store.open_period(3)
    finally:
        store.commit_input()
    store.open_period(3)  # the one legal advance
    assert store.period_id == 3


def test_a_fresh_state_seeds_any_period_and_a_used_one_advances_by_one() -> None:
    """A resume assembles period N into a PRISTINE state
    -- a rule that only counted from 1 could never assemble one. The seed
    is legal exactly once: with a row installed, only +1 remains."""
    from dsl41.ir import lower_source
    from dsl41.oracle import Oracle

    fresh = Oracle(lower_source("insert_job: j\njob_type: c\ncommand: x\n")).store
    fresh.seed_period(3)  # assembly's first act, explicit -- never inferred
    assert fresh.period_id == 3
    with pytest.raises(ValueError, match="used state"):
        fresh.seed_period(5)  # seeding is one-shot
    with pytest.raises(ValueError, match="exactly one"):
        fresh.open_period(3)
    fresh.open_period(4)
    assert fresh.period_id == 4
    # and a state whose only mutation was a NON-JOB row still refuses a
    # seed: the latch is explicit, not an inference over job rows
    touched = Oracle(lower_source("insert_job: j\njob_type: c\ncommand: x\n")).store
    touched.begin_input()
    touched.set_global("G", "1")
    touched.commit_input()
    with pytest.raises(ValueError, match="used state"):
        touched.seed_period(3)


def test_finish_genesis_is_one_shot() -> None:
    """A second `finish_genesis` after real inputs would
    launder a used state back to fresh and let `seed_period` skip a live
    lineage -- the exact bypass the latch closes."""
    from dsl41.ir import lower_source
    from dsl41.oracle import Oracle

    store = Oracle(lower_source("insert_job: j\njob_type: c\ncommand: x\n")).store
    store.begin_input()
    store.set_global("G", "1")
    store.commit_input()
    with pytest.raises(ValueError, match="construction happens once"):
        store.finish_genesis()
    with pytest.raises(ValueError, match="used state"):
        store.seed_period(99)


def test_finish_genesis_after_seed_period_is_refused() -> None:
    """The mirror check on the same latch: construction is meant to come
    BEFORE the period is seeded, so a caller who seeds first and only then
    calls `finish_genesis` must be refused too -- else the seed would be
    laundered as part of "construction" it never was."""
    from dsl41.oracle_state import RuntimeState

    store = RuntimeState()
    store.seed_period(4)
    with pytest.raises(ValueError, match="construction comes first"):
        store.finish_genesis()


def test_seed_period_refuses_inside_an_input_and_below_one() -> None:
    """The two guards on `seed_period` that a normal assembly never
    exercises: called mid-input (assembly always precedes inputs), and
    called with a period below 1 (I2 counts periods from 1)."""
    from dsl41.oracle_state import RuntimeState

    store = RuntimeState()
    store.begin_input()
    try:
        with pytest.raises(ValueError, match="assembly precedes inputs"):
            store.seed_period(2)
    finally:
        store.commit_input()

    fresh = RuntimeState()
    with pytest.raises(ValueError, match="periods count from 1"):
        fresh.seed_period(0)
