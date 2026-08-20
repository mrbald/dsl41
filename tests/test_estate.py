"""The estate verbs: `seal`, `audit`, `verify`, `estate adopt`, `estate
reclaim` and the physical roll (period-model ss1.3, ss7, ss11; DL-134).

Obligations in ss13 exercised here: PR-01c, PR-02a, PR-02d, PR-02e,
PR-02f, PR-47a, PR-47b and PR-48's readiness, drain, idempotency and
`adopting`-head rows.

House style follows test_boundary.py: every refusal asserts the message
fragment that only its own rule produces, and every gate has a passing
counterpart beside it -- an assertion that something refuses proves
nothing on its own, because a build where the operation never works at all
would produce the same result.

The live-seal and physical-roll tiers run REAL processes (the `engine`
helper from test_runner_leadership), because "the engine exits code 3" and
"a second root opens the period" are claims about processes and neither is
observable from inside one interpreter.
"""

from __future__ import annotations

import asyncio
import json
import uuid
import shutil
from pathlib import Path
from typing import Any

import pytest

from typer.testing import CliRunner

from dsl41.ast_jil import parse, render_preserve
from dsl41.attest import Attestation, audit_period, read_attestation, rederive_seal
from dsl41.boundary import (
    ClaimedHead,
    ClosedHead,
    EstateAnchor,
    OpenHead,
    default_anchor_dir,
    read_seal,
)
from dsl41.cli import app
from dsl41.ir import CatalogIR, lower_catalog
from dsl41.oracle_state import Event
from dsl41.period import (
    RuntimeProfile,
    Sentinel,
    SourceFile,
    attestation_path,
    read_period_manifest,
    read_sentinel,
    seal_path,
    stage_manifest,
    wal_path,
    write_bundle,
)
from dsl41.runner_adapters import FileWatcherAdapter, LocalCommandAdapter
from dsl41.runner_clock import EngineError, RealClock
from dsl41.runner_journal import read_journal
from dsl41.runner_ledger import STATE_MACHINE_VERSION
from dsl41.runner_startup import start_run

from test_runner_leadership import cli, engine, short_root  # noqa: F401  (fixture)

C1_JIL = "insert_job: a\njob_type: c\ncommand: sleep 600\n"
C2_JIL = (
    "insert_job: a\njob_type: c\ncommand: sleep 600\n\n"
    "insert_job: b\njob_type: c\ncommand: echo two\n"
)
C3_JIL = (
    "insert_job: a\njob_type: c\ncommand: sleep 600\n\n"
    "insert_job: b\njob_type: c\ncommand: echo three\n"
)

runner = CliRunner()


# ------------------------------------------------------------- fixtures


def _estate(base: Path) -> tuple[Path, Path, Path]:
    """Write the three catalogs a boundary test needs and return them."""
    base.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, text in (("c1.jil", C1_JIL), ("c2.jil", C2_JIL), ("c3.jil", C3_JIL)):
        path = base / name
        path.write_text(text)
        paths.append(path)
    return paths[0], paths[1], paths[2]


def _native_root(run_root: Path, jil: Path, *, admit: bool = False) -> CatalogIR:
    """A period-1 root exactly as `dsl41 run` opens one, in the REAL clock
    domain -- the domain every CLI verb below runs in.

    `admit` puts one externally requested input through it, so a test that
    needs an ADMITTED attempt to damage has one."""
    parsed = [parse(jil.read_text(), file=str(jil))]
    catalog = lower_catalog(parsed)
    staged = stage_manifest(
        catalog,
        source_bundle_hash=write_bundle(
            run_root, [SourceFile(path=str(jil), text=render_preserve(parsed[0]))]
        ),
        profile=RuntimeProfile(),
        state_machine_version=STATE_MACHINE_VERSION,
    )
    started = start_run(
        catalog,
        run_root,
        clock=RealClock(),
        adapters={"CMD": LocalCommandAdapter(), "FW": FileWatcherAdapter()},
        staged=staged,
    )
    if admit:
        # the ENGINE's clock, not the wall: `RealClock.now()` is naive UTC
        # and `datetime.now()` is naive local, so an event stamped from the
        # latter is due whenever the machine's offset says it is
        started.inject(
            Event(at=started.clock.now(), kind="SET_GLOBAL", payload={"name": "G", "value": "1"})
        )
    asyncio.run(started.run_until_quiescent(started.clock.now()))
    asyncio.run(started.shutdown())
    assert started.journal is not None
    started.journal.close()
    return catalog


def _legacy_root(
    run_root: Path, jil: Path, *, admit: bool = False, unfold: bool = False
) -> CatalogIR:
    """A run root as the pre-DL-130 build wrote one: a `header` journal and
    `manifest/`, and nothing the periodized layout added.

    `legacy_twin` downgrades the log and the inputs; the rest is what a
    genuine legacy root does NOT have -- no `wal/`, no anchor. A fixture
    that left either would be testing adoption against a root no legacy
    build could produce. `unfold` goes one step further and puts the
    body back into the pre-DL-118 `result`+`effect` dialect."""
    from test_period_identity import legacy_twin

    catalog = _native_root(run_root, jil, admit=admit)
    legacy_twin(run_root, catalog)
    shutil.rmtree(run_root / "wal", ignore_errors=True)
    shutil.rmtree(default_anchor_dir(run_root), ignore_errors=True)
    if unfold:
        _unfold_decisions(run_root)
    return catalog


def _unfold_decisions(run_root: Path) -> None:
    """Split every DL-118 `decision` back into the pre-DL-118 dialect it
    replaced: a `result` record plus one standalone `effect` per intent.

    That dialect is what a LEGACY root really holds -- DL-118 is younger
    than every root adoption exists for -- and it is the only shape that
    reaches `fold_legacy`'s `result` branch. `legacy_twin` downgrades the
    header and the inputs and leaves the body native, so a fixture built
    on it alone would leave the fold untested on the one verb that writes
    a durable period-1 WAL."""
    from dsl41.canon import canonical_bytes

    path = run_root / "journal.jsonl"
    out: list[dict[str, Any]] = []
    for record in read_journal(path):
        if record.get("rec") != "decision":
            out.append(record)
            continue
        out.append(
            {
                "rec": "result",
                "index": record["index"],
                "request_id": record["request_id"],
                "decision": record["decision"],
                "reason": record.get("reason"),
                "revisions": record.get("revisions") or {},
            }
        )
        for effect in record.get("effects") or []:
            # the legacy writer minted no `generation` and its run ids came
            # from the adapter, not the decision (DL-118 moved both)
            out.append({"rec": "effect", **{k: v for k, v in effect.items() if k != "generation"}})
    path.write_bytes(b"".join(canonical_bytes(record) + b"\n" for record in out))


def _invoke(*args: str):
    return runner.invoke(app, list(args), catch_exceptions=False)


def _seal_offline(run_root: Path, next_jil: Path, *anchor: str, **kw: Any):
    return _invoke("seal", "--run-root", str(run_root), "--next", str(next_jil), *anchor, **kw)


def _anchor_of(run_root: Path) -> EstateAnchor:
    return EstateAnchor(default_anchor_dir(run_root))


def _head(run_root: Path):
    stored = _anchor_of(run_root).read()
    assert stored is not None
    return stored.head


# --------------------------------------------------- ss7 the seal verb


def test_the_offline_seal_commits_and_the_period_reopens(tmp_path: Path) -> None:
    """ss7 offline mode end to end: no engine, so the CLI takes
    `leader.lock` and `anchor.lock`, appends a `leader` record, runs the
    same-root barrier and performs the boundary as that offline leader.

    The evidence is the estate's, not the exit code's: a committed sidecar,
    a `seal` record naming it, and a head that moved `open -> closed`."""
    c1, c2, _ = _estate(tmp_path / "estate")
    run_root = tmp_path / "run"
    _native_root(run_root, c1)

    result = _seal_offline(run_root, c2)
    assert result.exit_code == 0, result.output
    seal = read_seal(run_root, 1)
    records = read_journal(wal_path(run_root, 1))
    assert records[-1]["rec"] == "seal" and records[-1]["digest"] == seal.digest
    head = _head(run_root)
    assert isinstance(head, ClosedHead) and head.seal_digest == seal.digest
    assert seal.boundary_request.source == "request"
    assert seal.next_period.period_id == 2


def test_the_offline_seal_refuses_and_leaves_the_period_open(tmp_path: Path) -> None:
    """ss7's exit code 2: the boundary did NOT commit and C1 is still open.

    The counterpart of the test above, over the same root, so the refusal
    is not a build in which the seal never works: the same root seals
    afterwards."""
    c1, c2, _ = _estate(tmp_path / "estate")
    broken = tmp_path / "estate" / "broken.jil"
    # a machine no `insert_machine` declares: ss8's preflight ERROR, which
    # phase 1 runs before the current period closes
    broken.write_text("insert_job: a\njob_type: c\ncommand: x\nmachine: nowhere\n")
    run_root = tmp_path / "run"
    _native_root(run_root, c1)

    refused = _seal_offline(run_root, broken)
    assert refused.exit_code == 2
    assert "does not pass preflight" in refused.output
    assert not seal_path(run_root, 1).exists()
    assert isinstance(_head(run_root), OpenHead)

    assert _seal_offline(run_root, c2).exit_code == 0  # the gate, not the machinery


def test_the_offline_seal_reads_c1_from_the_root_not_the_command_line(tmp_path: Path) -> None:
    """ss7: in both modes the sealer holds C1 to run the barrier -- and it
    loads it from the ROOT's own bundle.

    The run root outlives the estate files it was launched from, so an
    offline seal that needed them on the command line would be unusable
    exactly when it matters. Proved by deleting them first."""
    c1, c2, _ = _estate(tmp_path / "estate")
    run_root = tmp_path / "run"
    _native_root(run_root, c1)
    c1.unlink()

    assert _seal_offline(run_root, c2).exit_code == 0
    assert read_seal(run_root, 1).period_id == 1


def test_pr30b_a_live_seal_asks_the_engine_and_it_exits_code_three(short_root: Path) -> None:  # noqa: F811
    """ss7 live mode, between two real processes: the CLI stages C2 and
    asks the LEADING engine over the control socket, the engine performs
    the cutoff in its single-writer loop and exits code 3, and no detached
    command is signalled.

    Which mode you get is decided by the LOCK: an engine that holds
    `leader.lock` is a live engine, and probing a socket file would answer
    a different question -- a socket outlives the process that made it."""
    _, c2, _ = _estate(short_root)
    with engine(short_root) as running:
        answer = cli(
            "seal",
            "--run-root",
            str(running.run_root),
            "--next",
            str(c2),
            "--claimed-actor",
            "alice@ops",
        )
        assert answer.returncode == 0, answer.stderr
        payload = json.loads(answer.stdout.splitlines()[0])
        assert payload["ok"] is True and payload["kind"] == "seal"
        assert payload["period_id"] == 1 and payload["next_period_id"] == 2
        running.proc.wait(timeout=30)
        assert running.proc.returncode == 3
    seal = read_seal(running.run_root, 1)
    assert seal.digest == payload["digest"]
    assert seal.boundary_request.claimed_actor == "alice@ops"


def test_a_live_seal_refusal_leaves_the_engine_running(short_root: Path) -> None:  # noqa: F811
    """ss7's exit code 2 on the live path: a readiness refusal is C1's
    business and C1 keeps running.

    An engine that died on a refused boundary would turn every operator
    typo into an outage, which is the opposite of what running readiness
    before the cutoff is for."""
    broken = short_root / "broken.jil"
    broken.write_text("insert_job: a\njob_type: c\ncommand: x\nmachine: nowhere\n")
    with engine(short_root) as running:
        answer = cli("seal", "--run-root", str(running.run_root), "--next", str(broken))
        assert answer.returncode == 2
        assert "preflight" in answer.stdout + answer.stderr
        assert running.proc.poll() is None
        status = cli("query", "status", "--socket", str(running.run_root / "control.sock"))
        assert status.returncode == 0


# ------------------------------------------- ss1.3 audit and attestation


#: PR-08b's golden vector: the EXACT canonical bytes and the EXACT digest
#: of one attestation. Fixed here rather than recomputed, because the point
#: is that a producer and a consumer on two patch versions agree byte for
#: byte -- a vector the code derives proves only that the code agrees with
#: itself.
GOLDEN_ATTESTATION = (
    b'{"artifact_format_version":1,"audited_at":"2026-08-20T00:00:00.000000",'
    b'"chain_through_period":2,'
    b'"digest":"sha256:8ad560df2e5b84595460288c4f2a53154f1b719b37a972b2099374366437edca",'
    b'"dsl41_version":"1.2.3","period_id":2,'
    b'"prev_attestation_digest":'
    b'"sha256:2222222222222222222222222222222222222222222222222222222222222222",'
    b'"scope":"full",'
    b'"seal_digest":'
    b'"sha256:1111111111111111111111111111111111111111111111111111111111111111",'
    b'"state_machine_version":1}'
)
GOLDEN_DIGEST = "sha256:8ad560df2e5b84595460288c4f2a53154f1b719b37a972b2099374366437edca"


def _golden() -> Attestation:
    return Attestation(
        seal_digest="sha256:" + "11" * 32,
        period_id=2,
        chain_through_period=2,
        prev_attestation_digest="sha256:" + "22" * 32,
        state_machine_version=1,
        dsl41_version="1.2.3",
        audited_at="2026-08-20T00:00:00.000000",
    )


def test_pr08b_the_attestation_golden_vector(tmp_path: Path) -> None:
    """PR-08b: fixed canonical bytes and a fixed digest for one
    `audit.json`, so a producer and a consumer on two patch versions agree
    byte for byte.

    `dsl41_version` is a FIELD of the artifact, so the vector pins one
    value rather than the installed one -- which is exactly the property
    under test: the version rides on the document and never on the
    canonical form.

    The round trip goes through the file the way `verify` reads it, and a
    single flipped byte in the stamped digest refuses."""
    golden = _golden()
    assert golden.to_bytes() == GOLDEN_ATTESTATION
    assert golden.digest == GOLDEN_DIGEST
    assert Attestation.from_bytes(GOLDEN_ATTESTATION, where="golden") == golden

    tampered = GOLDEN_ATTESTATION.replace(b'"period_id":2', b'"period_id":3', 1)
    with pytest.raises(EngineError, match="disagrees with itself"):
        Attestation.from_bytes(tampered, where="golden")
    with pytest.raises(EngineError, match="artifact_format_version"):
        Attestation.from_bytes(
            GOLDEN_ATTESTATION.replace(
                b'"artifact_format_version":1', b'"artifact_format_version":2'
            ),
            where="golden",
        )


def test_verify_refuses_a_checkpoint_filed_under_another_period(tmp_path: Path) -> None:
    """ss1.3's CONSUMER rule has exactly three checks, and each of them
    decides.

    The digest is `from_bytes`'s (PR-08b above). These two are `verify`'s
    own: an attestation whose `period_id` is not the one it was asked for,
    and one whose `chain_through_period` claims an induction with a gap.
    Both are re-STAMPED, so neither trips the digest gate on the way --
    which is what makes them tests of the rules rather than of the
    envelope."""
    run_root = _two_periods(tmp_path)
    assert _invoke("audit", "--run-root", str(run_root)).exit_code == 0
    good = read_attestation(run_root, 2)
    assert good is not None

    # period 3, not 1: a restamp to period 1 with a non-null predecessor
    # now trips the model's base-case rule first (null prev <=> period 1),
    # and this test is about verify's OWN period check
    _restamp(run_root, 2, period_id=3)
    refused = _invoke("verify", "--run-root", str(run_root), "--period", "2")
    assert refused.exit_code == 2 and "attests period 3, not 2" in refused.output

    _restamp(run_root, 2, period_id=2, chain_through_period=1)
    gapped = _invoke("verify", "--run-root", str(run_root), "--period", "2")
    assert gapped.exit_code == 2 and "induction has a gap" in gapped.output

    attestation_path(run_root, 2).write_bytes(good.to_bytes())
    assert _invoke("verify", "--run-root", str(run_root), "--period", "2").exit_code == 0


def _restamp(run_root: Path, at: int, **fields: Any) -> None:
    """Rewrite an attestation THROUGH the model, so its own digest is
    recomputed and only the rule under test can object."""
    stored = read_attestation(run_root, at)
    assert stored is not None
    attestation_path(run_root, at).write_bytes(stored.model_copy(update=fields).to_bytes())


def test_audit_re_derives_the_seal_and_writes_the_checkpoint(tmp_path: Path) -> None:
    """ss11: "verified" means RE-DERIVED, not self-consistent.

    The attestation is durable, canonical, digest-stamped, and bound to the
    seal it attests -- and period 1's `prev_attestation_digest` is null,
    because it is the base case of the induction."""
    c1, c2, _ = _estate(tmp_path / "estate")
    run_root = tmp_path / "run"
    _native_root(run_root, c1)
    assert _seal_offline(run_root, c2).exit_code == 0

    result = _invoke("audit", "--run-root", str(run_root))
    assert result.exit_code == 0, result.output
    attestation = read_attestation(run_root, 1)
    assert attestation is not None
    assert attestation.seal_digest == read_seal(run_root, 1).digest
    assert attestation.chain_through_period == 1
    assert attestation.prev_attestation_digest is None
    assert attestation.scope == "full"
    # the anchor row flips, and only after the artifact is durable
    row = _anchor_of(run_root).read().periods["1"]  # type: ignore[union-attr]
    assert row.attested is True
    assert _invoke("verify", "--run-root", str(run_root)).exit_code == 0


def test_audit_refuses_a_sidecar_that_does_not_re_derive(tmp_path: Path) -> None:
    """A sidecar whose digest matches its own canonical form proves
    integrity, not derivation.

    So: rewrite one digest-covered field CONSISTENTLY -- the sidecar
    re-stamps its own digest and the `seal` record is updated to name it,
    so every self-consistency check in the tree passes -- and audit still
    refuses, because the period's own evidence produces something else."""
    c1, c2, _ = _estate(tmp_path / "estate")
    run_root = tmp_path / "run"
    _native_root(run_root, c1)
    assert _seal_offline(run_root, c2).exit_code == 0
    assert _invoke("audit", "--run-root", str(run_root)).exit_code == 0

    _rewrite_closes_at_index(run_root, 1)
    attestation_path(run_root, 1).unlink()
    refused = _invoke("audit", "--run-root", str(run_root))
    assert refused.exit_code == 2
    assert "does not re-derive" in refused.output
    assert "closes_at_index" in refused.output


def _rewrite_closes_at_index(run_root: Path, period_id: int) -> None:
    """Move one digest-covered field and repair every artifact that names
    it, so nothing but the DERIVATION can tell."""
    from dsl41.boundary import seal_record
    from dsl41.canon import canonical_bytes
    from dsl41.seal import Seal

    seal = read_seal(run_root, period_id)
    forged = Seal(
        **{
            **seal.to_payload(),
            "closes_at_index": seal.closes_at_index + 1,
            "next_period": {
                **seal.next_period.model_dump(mode="json"),
                "first_index": seal.closes_at_index + 2,
            },
        }
    )
    seal_path(run_root, period_id).write_bytes(forged.to_bytes())
    records = read_journal(wal_path(run_root, period_id))
    records[-1] = seal_record(forged)
    wal_path(run_root, period_id).write_bytes(
        b"".join(canonical_bytes(record) + b"\n" for record in records)
    )


def test_pr02e_producing_an_attestation_needs_the_one_below_it(tmp_path: Path) -> None:
    """ss1.3's PRODUCER rule, negative: producing N requires the
    predecessor attestation present AND verified.

    There is deliberately no "or re-derive everything below" alternative --
    without the requirement a checkpoint is emitted over an unaudited
    opening seal, and earlier roots then get deleted on a chain that was
    never established. Three cases: missing, corrupt, and naming another
    seal."""
    run_root = _two_periods(tmp_path)

    missing = _invoke("audit", "--run-root", str(run_root), "--period", "2")
    assert missing.exit_code == 2 and "is not attested" in missing.output

    assert _invoke("audit", "--run-root", str(run_root), "--period", "1").exit_code == 0
    good = attestation_path(run_root, 1).read_bytes()

    corrupt = json.loads(good)
    corrupt["chain_through_period"] = 2
    attestation_path(run_root, 1).write_bytes(json.dumps(corrupt, sort_keys=True).encode())
    broken = _invoke("audit", "--run-root", str(run_root), "--period", "2")
    assert broken.exit_code == 2 and "disagrees with itself" in broken.output

    stranger = json.loads(good)
    stranger["seal_digest"] = "sha256:" + "0" * 64
    attestation_path(run_root, 1).write_bytes(
        Attestation.model_validate({k: v for k, v in stranger.items() if k != "digest"}).to_bytes()
    )
    mismatched = _invoke("audit", "--run-root", str(run_root), "--period", "2")
    assert mismatched.exit_code == 2 and "not this boundary's" in mismatched.output

    attestation_path(run_root, 1).write_bytes(good)
    assert _invoke("audit", "--run-root", str(run_root), "--period", "2").exit_code == 0


def _two_periods(tmp_path: Path) -> Path:
    """A root holding two closed periods, sealed offline both times."""
    c1, c2, c3 = _estate(tmp_path / "estate")
    run_root = tmp_path / "run"
    _native_root(run_root, c1)
    assert _seal_offline(run_root, c2).exit_code == 0
    _open_in_place(run_root, c2)
    assert _seal_offline(run_root, c3).exit_code == 0
    return run_root


def _open_in_place(run_root: Path, jil: Path) -> None:
    """ss7 step 9's in-place opener, driven the way the runbook drives it:
    resume the root, then stop. Runs in-process because the engine only has
    to OPEN the period, not serve it."""
    from dsl41.runner_startup import resume_run

    parsed = [parse(jil.read_text(), file=str(jil))]
    catalog = lower_catalog(parsed)
    opened = asyncio.run(
        resume_run(
            catalog,
            run_root,
            clock=RealClock(),
            adapters={"CMD": LocalCommandAdapter(), "FW": FileWatcherAdapter()},
        )
    )
    asyncio.run(opened.shutdown())
    assert opened.journal is not None
    opened.journal.close()


def test_the_chain_records_its_predecessor_and_reaches_through(tmp_path: Path) -> None:
    """ss1.3: auditing N verifies the predecessor, then records
    `chain_through_period` and `prev_attestation_digest` -- which is what
    makes the checkpoint an induction rather than an assertion."""
    run_root = _two_periods(tmp_path)
    assert _invoke("audit", "--run-root", str(run_root)).exit_code == 0
    first, second = read_attestation(run_root, 1), read_attestation(run_root, 2)
    assert first is not None and second is not None
    assert second.prev_attestation_digest == first.digest
    assert (first.chain_through_period, second.chain_through_period) == (1, 2)


def test_pr47a_audit_refuses_a_state_machine_version_it_does_not_implement(
    tmp_path: Path,
) -> None:
    """ss11: auditing an old period runs the interpreter that produced it.

    Cross-version audit inside one binary is a non-goal, so the refusal
    names the version and the `dsl41_version` that produced the period --
    which is what an operator installs to audit it."""
    c1, c2, _ = _estate(tmp_path / "estate")
    run_root = tmp_path / "run"
    _native_root(run_root, c1)
    assert _seal_offline(run_root, c2).exit_code == 0
    seal = read_seal(run_root, 1)
    _pin_version(run_root, seal.state_machine_version + 1)
    with pytest.raises(EngineError, match="audit runs the interpreter"):
        audit_period(run_root, 1)


def _pin_version(run_root: Path, version: int) -> None:
    """Make the STORED seal claim another state-machine version, through
    the model and with every derived value recomputed -- so nothing but the
    version gate can object.

    `state_machine_version` is inside `stage_digest`, and `baseline_id` is
    derived from it, so a lazy rewrite trips PR-47d's derivation check
    instead of the one this test is about."""
    from dsl41.seal import Seal, StagedNextPeriod, baseline_id_for

    seal = read_seal(run_root, 1)
    payload = seal.to_payload()
    payload["state_machine_version"] = version
    opening = {**payload["next_period"], "state_machine_version": version}
    staged = StagedNextPeriod(**{name: opening[name] for name in StagedNextPeriod.model_fields})
    opening["baseline_id"] = baseline_id_for(
        estate_id=seal.estate_id,
        period_id=opening["period_id"],
        stage_digest=staged.stage_digest,
    )
    payload["next_period"] = opening
    seal_path(run_root, 1).write_bytes(Seal(**payload).to_bytes())


def test_pr47b_audit_derives_source_and_re_derives_an_adoptions_request_id(
    tmp_path: Path,
) -> None:
    """ss11: `source` is audit's to DERIVE, never to read, and an
    adoption's `request_id` is re-derived too.

    A boundary is `adopt` iff the closing segment is period 1 with
    `catalog_hash_v1` non-null AND the root's sentinel `adopted_from`
    non-null. A pair that disagrees refuses -- otherwise a consistent
    rewrite of `adopt -> request` plus a new id would have told audit to
    treat the id as authoritative."""
    c1, c2, _ = _estate(tmp_path / "estate")
    run_root = tmp_path / "run"
    _legacy_root(run_root, c1)
    assert _adopt(run_root, c2).exit_code == 0
    seal = read_seal(run_root, 1)
    assert seal.boundary_request.source == "adopt"
    assert rederive_seal(run_root, 1).digest == seal.digest

    sentinel = read_sentinel(run_root)
    assert sentinel is not None
    from dsl41.period import write_sentinel

    write_sentinel(run_root, Sentinel(estate_id=sentinel.estate_id, adopted_from=None))
    with pytest.raises(EngineError, match="sets both or neither"):
        rederive_seal(run_root, 1)


# ------------------------------------------------- ss7 the physical roll


def test_a_physical_roll_opens_the_next_period_in_a_fresh_root(short_root: Path) -> None:  # noqa: F811
    """ss7's second opener, end to end between real processes: A seals, A
    is audited, B opens period 2 from the seal, and B is resumable on its
    own imported artifacts (PR-02a).

    The registry keeps BOTH roots (PR-02f), the new root's sentinel names
    the claim that first opened it (ss1.1), and `verify` of period 1 passes
    in B -- a full `audit` there is impossible and is not asked for."""
    _, c2, _ = _estate(short_root)
    with engine(short_root) as running:
        assert cli("seal", "--run-root", str(running.run_root), "--next", str(c2)).returncode == 0
        running.proc.wait(timeout=30)
    root_a = running.run_root
    anchor_dir = default_anchor_dir(root_a)
    assert cli("audit", "--run-root", str(root_a)).returncode == 0

    root_b = short_root / "b"
    with engine(
        short_root,
        run_root=root_b,
        files=[c2],
        extra=["--open-from", str(anchor_dir)],
    ) as opened:
        assert opened.proc.poll() is None
    assert wal_path(root_b, 2).exists()

    sentinel = read_sentinel(root_b)
    assert sentinel is not None and sentinel.claim_id is not None
    stored = EstateAnchor(anchor_dir).read()
    assert stored is not None and isinstance(stored.head, OpenHead)
    assert stored.head.period_id == 2
    assert stored.periods["1"].root == str(Path(root_a).resolve())
    assert stored.periods["2"].root == str(Path(root_b).resolve())
    # B holds the imported pair and verifies the chain below its own seal
    assert seal_path(root_b, 1).exists() and attestation_path(root_b, 1).exists()
    assert cli("verify", "--run-root", str(root_b), "--period", "1").returncode == 0
    assert read_period_manifest(root_b, 2) is not None
    # PR-02a's second half: B is resumable on its OWN imported artifacts,
    # and A -- restored -- refuses to open the same seal
    with engine(
        short_root,
        run_root=root_b,
        files=[c2],
        resume=True,
        extra=["--estate-anchor", str(anchor_dir)],
    ) as resumed:
        assert resumed.proc.poll() is None
    refused = cli(
        "run",
        "--resume",
        "--run-root",
        str(root_a),
        "--estate-anchor",
        str(anchor_dir),
        str(c2),
        timeout=30,
    )
    assert refused.returncode == 2
    # named, not generic: A is told the head moved to B and which period
    assert "cannot claim the successor" in refused.stderr
    assert str(Path(root_b).resolve()) in refused.stderr


def test_pr02d_a_roll_refuses_a_closing_period_that_is_not_attested(tmp_path: Path) -> None:
    """ss1.3: `run --open-from` refuses unless `seals/<N>.audit.json`
    exists in the closing root AND passes `verify`.

    A file that merely exists is not enough. Draft 5 let B import a seal it
    could never verify and then required it to audit C1 with none of C1's
    inputs."""
    c1, c2, _ = _estate(tmp_path / "estate")
    root_a = tmp_path / "a"
    _native_root(root_a, c1)
    assert _seal_offline(root_a, c2).exit_code == 0
    anchor_dir = default_anchor_dir(root_a)
    root_b = tmp_path / "b"

    missing = _invoke("run", "--open-from", str(anchor_dir), "--run-root", str(root_b), str(c2))
    assert missing.exit_code == 2 and "is not attested" in missing.output
    assert not root_b.exists() or read_sentinel(root_b) is None

    assert _invoke("audit", "--run-root", str(root_a)).exit_code == 0
    intact = attestation_path(root_a, 1).read_bytes()
    corrupt = json.loads(intact)
    corrupt["seal_digest"] = "sha256:" + "1" * 64
    attestation_path(root_a, 1).write_bytes(
        Attestation.model_validate({k: v for k, v in corrupt.items() if k != "digest"}).to_bytes()
    )
    stranger = _invoke("run", "--open-from", str(anchor_dir), "--run-root", str(root_b), str(c2))
    assert stranger.exit_code == 2 and "not this boundary's" in stranger.output

    # the counterpart: restored, the same roll goes through -- the gate,
    # not a build in which the roll never works
    attestation_path(root_a, 1).write_bytes(intact)
    _roll(root_b, anchor_dir, c2)
    assert wal_path(root_b, 2).exists()


def test_a_roll_refuses_a_closing_period_that_holds_live_work(tmp_path: Path) -> None:
    """ss8's mode table: a physical roll while jobs are live is REFUSED.

    The supervisor is one per run root and a new-root engine cannot reach
    the old root's work; the bridge that lifts this is a non-goal. The
    refusal names the runs."""
    from dsl41.estate import roll_into_root

    c1, c2, _ = _estate(tmp_path / "estate")
    root_a = tmp_path / "a"
    _native_root(root_a, c1)
    assert _seal_offline(root_a, c2).exit_code == 0
    assert _invoke("audit", "--run-root", str(root_a)).exit_code == 0
    _forge_live_execution(root_a, 1)

    with pytest.raises(EngineError, match="carries live execution"):
        roll_into_root(
            tmp_path / "b",
            anchor_dir=default_anchor_dir(root_a),
            catalog_of=lambda _root, _m: lower_catalog([parse(c2.read_text(), file=str(c2))]),
        )


def _forge_live_execution(run_root: Path, period_id: int) -> None:
    """Put a `bound` execution into the stored sidecar, through the model,
    and re-point the anchor head at the new digest -- so the artifact stays
    self-consistent, the lineage still names it, and only the ss8 gate has
    anything to object to.

    Forged rather than run, because the alternative is a detached estate
    with a live supervised command driven through the CLI, and what is
    under test here is the GATE, not how a run becomes live."""
    from dsl41.seal import Seal

    seal = read_seal(run_root, period_id)
    payload = seal.to_payload()
    payload["state"]["jobs"]["a"] = {
        **payload["state"]["jobs"]["a"],
        "status": "RUNNING",
        "run_number": 1,
    }
    payload["executions"] = [
        {
            "kind": "bound",
            "job": "a",
            "run_number": 1,
            "effect_id": "e-1",
            "index": 1,
            "run_id": "11111111-1111-4111-8111-111111111111",
            "executor_id": "local",
            "generation": 0,
            "run_dir": "runs/a.1",
        }
    ]
    forged = Seal(**payload)
    seal_path(run_root, period_id).write_bytes(forged.to_bytes())
    anchor = _anchor_of(run_root)
    anchor.acquire()
    stored = anchor.require()
    assert isinstance(stored.head, ClosedHead)
    anchor.write(
        stored.model_copy(
            update={"head": stored.head.model_copy(update={"seal_digest": forged.digest})}
        )
    )
    anchor.release()


def _roll(new_root: Path, anchor_dir: Path, jil: Path, *, stop_at: str | None = None):
    from dsl41.estate import roll_into_root

    def crash_point(stage: str) -> None:
        if stage == stop_at:
            raise _Stopped(stage)

    return roll_into_root(
        new_root,
        anchor_dir=anchor_dir,
        catalog_of=lambda _root, _m: lower_catalog([parse(jil.read_text(), file=str(jil))]),
        crash_point=crash_point,
    )


class _Stopped(Exception):
    """The crash matrix's seam: the operation stops exactly between two
    durable writes, rather than a process being killed and hoped about."""


def test_pr45_a_roll_interrupted_after_its_import_re_runs_to_completion(
    tmp_path: Path,
) -> None:
    """ss11's matrix row: crash after the import, before the first
    `segment` in the new root -- the import is idempotent by content
    address, so re-import and open.

    The re-run finds its own sentinel (same estate, same claim), resumes
    its own claim, re-imports the same bytes and opens. Then a THIRD run
    over the finished root finds the head already `open` and refuses,
    because a completed roll is reopened with `--resume`, not rolled
    again."""
    c1, c2, _ = _estate(tmp_path / "estate")
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    _native_root(root_a, c1)
    assert _seal_offline(root_a, c2).exit_code == 0
    assert _invoke("audit", "--run-root", str(root_a)).exit_code == 0
    anchor_dir = default_anchor_dir(root_a)

    with pytest.raises(_Stopped):
        _roll(root_b, anchor_dir, c2, stop_at="after_import")
    assert seal_path(root_b, 1).exists()
    assert not wal_path(root_b, 2).exists()  # stopped before the segment
    imported = seal_path(root_b, 1).read_bytes()

    rolled = _roll(root_b, anchor_dir, c2)
    assert rolled.seal.digest == read_seal(root_a, 1).digest
    assert seal_path(root_b, 1).read_bytes() == imported  # same bytes, re-written
    assert wal_path(root_b, 2).exists()

    with pytest.raises(EngineError, match="is already open in"):
        _roll(root_b, anchor_dir, c2)


def test_a_roll_refuses_an_imported_sidecar_that_did_not_arrive_intact(
    tmp_path: Path, monkeypatch
) -> None:
    """The COPY is what gets checked, not the original.

    What this root will resume from, audit against and hand to a second
    roll is the bytes that landed HERE, so the import reads them back. The
    damage goes in at the write, because that is the only place it can
    happen: a re-import would otherwise overwrite it with the source's own
    bytes, which is exactly what makes the import idempotent."""
    import dsl41.estate as estate_mod

    c1, c2, _ = _estate(tmp_path / "estate")
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    _native_root(root_a, c1)
    assert _seal_offline(root_a, c2).exit_code == 0
    assert _invoke("audit", "--run-root", str(root_a)).exit_code == 0

    real = estate_mod.durable_write

    def damaging(path: str, data: bytes) -> None:
        if path.endswith("000001.json"):
            data = data.replace(b'"artifact_format_version":1', b'"artifact_format_version":1 ', 1)
        real(path, data)

    monkeypatch.setattr(estate_mod, "durable_write", damaging)
    with pytest.raises(EngineError):
        _roll(root_b, default_anchor_dir(root_a), c2)


def test_pr01c_a_roll_refuses_a_target_that_already_holds_an_estate(tmp_path: Path) -> None:
    """ss1.1's ownership rule, at the roll: a target root that already
    holds another estate's sentinel refuses.

    The counterpart is the test above, where the SAME estate's sentinel for
    the SAME claim resumes -- absent creates, our own incomplete
    transaction resumes, anything else refuses."""
    c1, c2, _ = _estate(tmp_path / "estate")
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    _native_root(root_a, c1)
    assert _seal_offline(root_a, c2).exit_code == 0
    assert _invoke("audit", "--run-root", str(root_a)).exit_code == 0
    _native_root(root_b, c1)  # root_b is somebody else's estate entirely

    with pytest.raises(EngineError, match="already exists and is not ours"):
        _roll(root_b, default_anchor_dir(root_a), c2)


def test_a_roll_into_the_closing_root_is_refused_by_name(tmp_path: Path) -> None:
    """Rolling a root into itself is `--resume` spelled dangerously, and
    the ownership rule would refuse it later with a sentence about claims
    that does not name the mistake."""
    c1, c2, _ = _estate(tmp_path / "estate")
    run_root = tmp_path / "run"
    _native_root(run_root, c1)
    assert _seal_offline(run_root, c2).exit_code == 0
    refused = _invoke(
        "run",
        "--open-from",
        str(default_anchor_dir(run_root)),
        "--run-root",
        str(run_root),
        str(c2),
    )
    assert refused.exit_code == 2
    assert "is the root this lineage closed period 1 in" in refused.output


def test_open_from_and_resume_are_the_two_openers_and_you_get_one(tmp_path: Path) -> None:
    """Both flags name an opener, and a caller that passed both would be
    asking for two lineages at once."""
    c1, _, _ = _estate(tmp_path / "estate")
    both = _invoke(
        "run",
        "--resume",
        "--open-from",
        str(tmp_path / "anchor"),
        "--run-root",
        str(tmp_path / "run"),
        str(c1),
    )
    assert both.exit_code == 2 and "the two OPENERS" in both.output


# ------------------------------------------------ ss1.3 estate reclaim


def test_reclaim_refuses_without_force(tmp_path: Path) -> None:
    """The break-glass is destructive by design and says so: this is the
    one operation here that can fork a lineage."""
    refused = _invoke("estate", "reclaim", "--estate-anchor", str(tmp_path / "anchor"))
    assert refused.exit_code == 2
    assert "refusing without --force" in refused.output


def test_reclaim_moves_a_foreign_claim_and_the_next_opening_records_it(tmp_path: Path) -> None:
    """ss1.3: a stale claim is break-glass, not garbage.

    A second root's claim blocks this one -- correctly, because a claimed
    head whose target is unreachable cannot be told from one whose target
    is paused. `reclaim --force` moves it back to `closed`, the anchor
    keeps the record, and the next opening `segment` carries it in
    `reclaimed` with the actor who claimed to authorize it."""
    c1, c2, _ = _estate(tmp_path / "estate")
    run_root = tmp_path / "run"
    _native_root(run_root, c1)
    assert _seal_offline(run_root, c2).exit_code == 0
    anchor_dir = default_anchor_dir(run_root)

    stranger = _anchor_of(run_root)
    stranger.acquire()
    seal = read_seal(run_root, 1)
    claim = stranger.claim_successor(
        estate_id=seal.estate_id,
        seal_digest=seal.digest,
        next_period=2,
        target_root=tmp_path / "elsewhere",
    )
    stranger.release()
    assert isinstance(_head(run_root), ClaimedHead)

    with pytest.raises(EngineError, match="is held by"):
        _open_in_place(run_root, c2)

    reclaimed = _invoke(
        "estate",
        "reclaim",
        "--estate-anchor",
        str(anchor_dir),
        "--force",
        "--claimed-actor",
        "carol@ops",
    )
    assert reclaimed.exit_code == 0, reclaimed.output
    assert claim.claim_id in reclaimed.output
    assert isinstance(_head(run_root), ClosedHead)

    _open_in_place(run_root, c2)
    segment = read_journal(wal_path(run_root, 2))[0]
    assert segment["reclaimed"] is not None
    assert segment["reclaimed"]["claimed_actor"] == "carol@ops"
    assert segment["reclaimed"]["claim_id"] == claim.claim_id


def test_reclaim_never_moves_a_head_that_is_doing_its_job(tmp_path: Path) -> None:
    """It moves a CLAIM and nothing else: an `open` head is a period that
    is live, and forcing that one is not break-glass, it is vandalism."""
    c1, _, _ = _estate(tmp_path / "estate")
    run_root = tmp_path / "run"
    _native_root(run_root, c1)
    refused = _invoke(
        "estate", "reclaim", "--estate-anchor", str(default_anchor_dir(run_root)), "--force"
    )
    assert refused.exit_code == 2 and "not a claim" in refused.output


# ----------------------------------------------------- ss11 adoption


def _adopt(run_root: Path, next_jil: Path, *extra: str):
    return _invoke("estate", "adopt", str(run_root), "--next", str(next_jil), *extra)


def test_pr48_adoption_fences_translates_and_seals_period_one(tmp_path: Path) -> None:
    """ss11's seven steps, end to end.

    The evidence is every step's own artifact: the tombstone with
    `adopted_from`, the hard-linked original, the translated segment
    pinning `catalog_hash_v1`, `catalogs/` and `periods/000001/` split out
    of `manifest/`, the seal with the DERIVED boundary request, and a head
    that went `absent -> adopting -> closed` with period 1's registry row
    flipped in that same write (PR-02c)."""
    c1, c2, _ = _estate(tmp_path / "estate")
    run_root = tmp_path / "run"
    _legacy_root(run_root, c1)
    legacy = read_journal(run_root / "journal.jsonl")

    result = _adopt(run_root, c2, "--claimed-actor", "bob@ops")
    assert result.exit_code == 0, result.output

    sentinel = read_sentinel(run_root)
    assert sentinel is not None and sentinel.adopted_from == "legacy/journal.jsonl"
    assert read_journal(run_root / "legacy" / "journal.jsonl") == legacy

    segment = read_journal(wal_path(run_root, 1))[0]
    assert segment["rec"] == "segment" and segment["period_id"] == 1
    assert segment["catalog_hash_v1"] == legacy[0]["catalog_hash"]
    assert segment["catalog_hash_version"] == 2
    assert segment["baseline_id"] == legacy[0]["baseline_id"]  # every fingerprint's
    assert (run_root / "catalogs").is_dir() and read_period_manifest(run_root, 1) is not None

    seal = read_seal(run_root, 1)
    assert seal.boundary_request.source == "adopt"
    assert seal.boundary_request.claimed_actor == "bob@ops"
    head = _head(run_root)
    assert isinstance(head, ClosedHead) and head.period_id == 1
    row = _anchor_of(run_root).read().periods["1"]  # type: ignore[union-attr]
    assert row.segment_durable is True  # adoption's finalize is folded into its close


def test_pr48_the_result_and_effect_records_fold_into_one_decision(tmp_path: Path) -> None:
    """ss11 step 5: every `result` plus its same-index `effect` records
    fold into ONE `decision` line, marked `legacy_batch: true`.

    Marked because those records were separate fsyncs and a fold cannot
    make a torn batch atomic after the fact -- audit knows the difference,
    and the ADOPTER's own decisions, written natively after the
    translation, are not marked.

    The fold is checked against the retained original, record by record
    and not only at the fold, because the whole translation is what period
    1 replays from. The `run_id` reconstruction is the next test's: this
    estate's admitted input plans no effect, which is the ordinary case
    and the one a fixture can build without a spool."""
    c1, c2, _ = _estate(tmp_path / "estate")
    run_root = tmp_path / "run"
    _legacy_root(run_root, c1, admit=True, unfold=True)
    original = read_journal(run_root / "journal.jsonl")
    assert [r["rec"] for r in original].count("result") >= 1
    assert not [r for r in original if r.get("rec") == "decision"]

    assert _adopt(run_root, c2).exit_code == 0
    assert read_journal(run_root / "legacy" / "journal.jsonl") == original

    translated = read_journal(wal_path(run_root, 1))
    results = [r for r in original if r.get("rec") == "result"]
    indices = {r["index"] for r in results}
    decisions = [r for r in translated if r.get("rec") == "decision"]
    folded = [r for r in decisions if r["index"] in indices]
    # every FOLDED batch is marked, and the adopter's own -- written
    # natively by the barrier after the translation -- is not
    assert folded and all(r["legacy_batch"] is True for r in folded)
    assert all(r["legacy_batch"] is False for r in decisions if r["index"] not in indices)
    assert all(e["generation"] == 0 for r in folded for e in r["effects"])
    # lossless: one decision per result, at the same index, carrying the
    # same effects in the same order
    assert [r["index"] for r in folded] == [r["index"] for r in results]
    for record in folded:
        source = [
            {k: v for k, v in e.items() if k not in ("rec", "generation")}
            for e in original
            if e.get("rec") == "effect" and e["index"] == record["index"]
        ]
        assert [
            {k: v for k, v in e.items() if k not in ("rec", "generation")}
            for e in record["effects"]
        ] == source
    # and everything else crossed verbatim
    verbatim = [r for r in original if r.get("rec") not in ("header", "result", "effect")]
    assert all(record in translated for record in verbatim)
    # the translated period audits, which is the whole point of a fold
    assert _invoke("audit", "--run-root", str(run_root)).exit_code == 0


def test_pr48_a_fold_refuses_a_spawn_it_cannot_identify(tmp_path: Path) -> None:
    """ss11: `run_id: null` is legal ONLY for a run that provably never
    reached an adapter -- no `spawn.json`, and an outcome of `retired` or
    `indeterminate`.

    A legacy SPAWN that a drain and a KILL retired before it reached an
    adapter legitimately has no run and no file, and adoption must not
    refuse an estate for a run that never existed. One with any other
    outcome is a run the fold cannot name, and inventing an id for it
    would be a guess in a durable record."""
    from dsl41.estate import fold_legacy

    records = [
        {"rec": "result", "index": 4, "request_id": "r", "decision": "applied", "revisions": {}},
        {
            "rec": "effect",
            "effect_id": "e-4",
            "kind": "SPAWN",
            "job": "a",
            "run_number": 1,
            "executor_id": "local",
            "index": 4,
            "at": "2026-08-20T00:00:00",
            "run_id": None,
        },
    ]
    retired = [*records, {"rec": "effect_result", "effect_id": "e-4", "state": "retired"}]
    [decision] = [r for r in fold_legacy(tmp_path, retired) if r["rec"] == "decision"]
    assert decision["legacy_batch"] is True
    assert decision["effects"][0]["run_id"] is None

    applied = [*records, {"rec": "effect_result", "effect_id": "e-4", "state": "applied"}]
    with pytest.raises(EngineError, match="never reached an adapter"):
        fold_legacy(tmp_path, applied)


def test_pr48_a_c2_that_fails_readiness_refuses_before_the_fence(tmp_path: Path) -> None:
    """ss11 step 1: readiness runs FIRST, over an in-memory reconstruction
    of the legacy state, and a failure refuses with the sentinel, the
    legacy WAL and the anchor untouched.

    Draft 15 let adoption fence the legacy root and commit period 1 without
    ever running C2's readiness, so an unsupported artifact version or a
    failing preflight surfaced only when period 2 refused to open -- a
    committed, unopenable boundary with the old engine already fenced."""
    c1, c2, _ = _estate(tmp_path / "estate")
    broken = tmp_path / "estate" / "broken.jil"
    broken.write_text("insert_job: a\njob_type: c\ncommand: x\nmachine: nowhere\n")
    run_root = tmp_path / "run"
    _legacy_root(run_root, c1)
    before = (run_root / "journal.jsonl").read_bytes()

    refused = _adopt(run_root, broken)
    assert refused.exit_code == 2 and "does not pass preflight" in refused.output
    assert (run_root / "journal.jsonl").read_bytes() == before  # not fenced
    assert read_sentinel(run_root) is None
    assert not (run_root / "legacy").exists()
    assert _anchor_of(run_root).read() is None
    assert not wal_path(run_root, 1).exists()

    assert _adopt(run_root, c2).exit_code == 0  # the gate, not the machinery


def test_pr48_adoption_refuses_an_undecided_input(tmp_path: Path) -> None:
    """ss11 step 2: every admitted input must hold a durable decision, and
    this is a REFUSAL rather than a repair.

    Replay recovers only the `ApplyResult` of a result-less input and
    discards the emitted events that would plan its effects, so an adopter
    that "gave it a decision" either dispatched a recovered SPAWN before
    its decision was durable or wrote `effects: []` and failed a start the
    old estate had committed. Neither is acceptable."""
    c1, c2, _ = _estate(tmp_path / "estate")
    run_root = tmp_path / "run"
    _legacy_root(run_root, c1, admit=True)
    _drop_last_decision(run_root)

    refused = _adopt(run_root, c2)
    assert refused.exit_code == 2
    assert "have no durable `result`" in refused.output
    assert read_sentinel(run_root) is None


def _drop_last_decision(run_root: Path) -> None:
    """Leave the last admitted input without its decision -- the crash
    window a legacy engine can really die in."""
    from dsl41.canon import canonical_bytes

    path = run_root / "journal.jsonl"
    records = read_journal(path)
    decided = [r for r in records if r.get("rec") in ("decision", "result")]
    assert decided, "the fixture must admit at least one input"
    records.remove(decided[-1])
    path.write_bytes(b"".join(canonical_bytes(r) + b"\n" for r in records))


def test_pr48_run_resume_refuses_while_the_head_is_adopting(tmp_path: Path) -> None:
    """ss11: `adopting` is what gives adoption ONE recovery owner. While it
    stands, `run --resume` refuses and names the verb that can finish it.

    Draft 14 handed recovery to `--resume` "once a segment exists", which
    is after the translation and before the seal, so a crash there had two
    owners and neither could finish."""

    c1, c2, _ = _estate(tmp_path / "estate")
    run_root = tmp_path / "run"
    _legacy_root(run_root, c1)
    _stage_for_adopt(run_root, c2)

    with pytest.raises(EngineError, match="re-run `dsl41 estate adopt`"):
        _open_in_place(run_root, c2)
    assert _head(run_root).state == "adopting"

    assert _adopt(run_root, c2).exit_code == 0  # the re-run finishes it
    assert isinstance(_head(run_root), ClosedHead)


def _stage_for_adopt(run_root: Path, next_jil: Path) -> None:
    """Run ss11 steps 1-6 and stop, leaving the head `adopting` -- the
    state a crash between the translation and the seal really leaves."""
    from dsl41.boundary import stage_next_period
    from dsl41.estate import adopt_legacy_root

    parsed = [parse(next_jil.read_text(), file=str(next_jil))]
    catalog = lower_catalog(parsed)
    staged_manifest = stage_manifest(
        catalog,
        source_bundle_hash=write_bundle(
            run_root, [SourceFile(path=str(next_jil), text=render_preserve(parsed[0]))]
        ),
        profile=RuntimeProfile(),
        state_machine_version=STATE_MACHINE_VERSION,
    )
    stage_next_period(run_root, staged_manifest=staged_manifest)
    adopt_legacy_root(
        run_root,
        anchor_dir=default_anchor_dir(run_root),
        profile=RuntimeProfile(),
        staged_manifest=staged_manifest,
        claimed_actor="tester@ops",
    )


def test_pr48_adoption_is_idempotent_and_mints_one_estate_id(tmp_path: Path) -> None:
    """ss11: every step is idempotent -- a re-run finds the tombstone,
    reads `estate_id` BACK rather than minting a second, and continues from
    wherever it stopped."""
    c1, c2, _ = _estate(tmp_path / "estate")
    run_root = tmp_path / "run"
    _legacy_root(run_root, c1)
    _stage_for_adopt(run_root, c2)
    first = read_sentinel(run_root)
    assert first is not None
    translated = wal_path(run_root, 1).read_bytes()

    assert _adopt(run_root, c2).exit_code == 0
    again = read_sentinel(run_root)
    assert again is not None and again.estate_id == first.estate_id
    # the translation is not re-written over the adopter's own records
    assert wal_path(run_root, 1).read_bytes().startswith(translated.splitlines()[0])


def test_pr48_a_re_run_after_the_committed_seal_performs_the_head_cas(
    tmp_path: Path,
) -> None:
    """ss11's matrix row: adoption's `seal` record present, head still
    `adopting`.

    The boundary committed and the process died before the anchor CAS.
    `adopting` names exactly one recovery owner, so a re-run of `estate
    adopt` performs the CAS -- and reports a FINISHED adoption rather than
    sealing a period that is already closed. `run --resume` refuses until
    it has."""
    from dsl41.boundary import AdoptingHead

    c1, c2, _ = _estate(tmp_path / "estate")
    run_root = tmp_path / "run"
    _legacy_root(run_root, c1)
    assert _adopt(run_root, c2).exit_code == 0
    digest = read_seal(run_root, 1).digest

    anchor = _anchor_of(run_root)
    anchor.acquire()
    stored = anchor.require()
    anchor.write(
        stored.model_copy(update={"head": AdoptingHead(period_id=1, root=stored.periods["1"].root)})
    )
    anchor.release()
    with pytest.raises(EngineError, match="re-run `dsl41 estate adopt`"):
        _open_in_place(run_root, c2)

    rerun = _adopt(run_root, c2)
    assert rerun.exit_code == 0, rerun.output
    assert "was already sealed" in rerun.output
    head = _head(run_root)
    assert isinstance(head, ClosedHead) and head.seal_digest == digest
    # and nothing was sealed a second time
    assert [r for r in read_journal(wal_path(run_root, 1)) if r.get("rec") == "seal"] != []
    assert len([r for r in read_journal(wal_path(run_root, 1)) if r.get("rec") == "seal"]) == 1


def test_adoption_reads_the_legacy_launch_options_the_manifest_recorded(
    tmp_path: Path,
) -> None:
    """DL-66's `manifest/manifest.json` recorded four launch options, and
    adoption reads them rather than asking the operator to remember.

    The estate's own record of how it ran beats anyone's memory of it, and
    the WIRING is built from the result -- so the pin and the machine agree
    by construction. What the block never held stays the flags'."""
    from dsl41.estate import legacy_profile

    c1, _, _ = _estate(tmp_path / "estate")
    run_root = tmp_path / "run"
    _legacy_root(run_root, c1)
    manifest = run_root / "manifest" / "manifest.json"
    payload = json.loads(manifest.read_bytes())
    payload["options"] = {
        "timezone": "Europe/Zurich",
        "as_machine": ["beta", "alpha"],
        "machine_policy": "local-eligible",
        "detached": True,
    }
    manifest.write_text(json.dumps(payload, sort_keys=True))

    attested = RuntimeProfile(cmd_grace_us=7_000_000)
    read = legacy_profile(run_root, attested)
    assert read.default_tz == "Europe/Zurich"
    assert read.as_machine == ("alpha", "beta")  # the model normalizes
    assert read.machine_policy == "local-eligible"
    assert read.execution_mode == "detached"
    assert read.cmd_grace_us == 7_000_000  # never recorded: the flag's

    # a root whose manifest holds no options block yields the attestation
    payload.pop("options")
    manifest.write_text(json.dumps(payload, sort_keys=True))
    assert legacy_profile(run_root, attested) == attested


def test_a_native_root_is_not_adopted_twice(tmp_path: Path) -> None:
    """Adoption translates a LEGACY `header` journal. A root that has been
    through it, or was born native, is already periodized."""
    c1, c2, _ = _estate(tmp_path / "estate")
    run_root = tmp_path / "run"
    _native_root(run_root, c1)
    refused = _adopt(run_root, c2)
    assert refused.exit_code == 2 and "already a periodized estate" in refused.output


def test_pr01c_a_root_with_a_wal_and_no_sentinel_refuses_adoption(tmp_path: Path) -> None:
    """A legacy layout has no `wal/`. A root with both is neither legacy
    nor adopted, and translating into a segment this transaction did not
    write would adopt a stranger's records under this estate's name."""
    c1, c2, _ = _estate(tmp_path / "estate")
    run_root = tmp_path / "run"
    catalog = _native_root(run_root, c1)
    from test_period_identity import legacy_twin

    legacy_twin(run_root, catalog)  # leaves `wal/` where it was
    shutil.rmtree(default_anchor_dir(run_root), ignore_errors=True)
    refused = _adopt(run_root, c2)
    assert refused.exit_code == 2 and "a legacy layout has no `wal/`" in refused.output


def test_pr02f_the_registry_finds_period_one_after_adoption(tmp_path: Path) -> None:
    """ss1.3: the registry maps every period to the root that holds it, and
    period 1's row is written when the root first owns it -- at `absent ->
    adopting`, provisional, flipping in `adopting -> closed`."""
    c1, c2, _ = _estate(tmp_path / "estate")
    run_root = tmp_path / "run"
    _legacy_root(run_root, c1)
    _stage_for_adopt(run_root, c2)
    provisional = _anchor_of(run_root).read()
    assert provisional is not None
    assert provisional.periods["1"].segment_durable is False  # ignored by every reader

    assert _adopt(run_root, c2).exit_code == 0
    final = _anchor_of(run_root).read()
    assert final is not None
    assert final.periods["1"].segment_durable is True
    assert final.periods["1"].root == str(Path(run_root).resolve())


def test_a_re_run_audit_returns_the_stored_checkpoint_unchanged(tmp_path: Path) -> None:
    """ss1.3: attestation N+1 records N's digest as its
    `prev_attestation_digest`; a re-run that rewrote N with a fresh
    `audited_at` would silently re-digest the checkpoint that link names.
    Audit is idempotent: the stored, verifying checkpoint IS the answer."""
    c1, c2, _ = _estate(tmp_path / "estate")
    run_root = tmp_path / "run"
    _native_root(run_root, c1)
    assert _seal_offline(run_root, c2).exit_code == 0
    first = audit_period(run_root, 1)
    stored = read_attestation(run_root, 1)
    assert stored is not None and stored.digest == first.digest
    again = audit_period(run_root, 1)
    assert again.digest == first.digest
    assert again.audited_at == first.audited_at  # byte-identical, not re-produced


# ------------------------------------------ peer-review round-1 pins (DL-134)


def _rewrite_seal_record_field(run_root: Path, period_id: int, **fields: Any) -> None:
    """Rewrite the WAL's `seal` record only -- the sidecar stays intact --
    so nothing but the record<->sidecar comparison can tell."""
    from dsl41.canon import canonical_bytes

    records = read_journal(wal_path(run_root, period_id))
    assert records[-1].get("rec") == "seal"
    records[-1] = {**records[-1], **fields}
    wal_path(run_root, period_id).write_bytes(
        b"".join(canonical_bytes(record) + b"\n" for record in records)
    )


def test_audit_refuses_a_seal_record_that_does_not_name_the_sidecar(tmp_path: Path) -> None:
    """ss2.2/ss11: the record's duplicated fields must equal the sidecar's
    BEFORE re-derivation -- a rewritten record over an untouched sidecar
    would otherwise be ignored, and audit would attest a seal the WAL does
    not name."""
    c1, c2, _ = _estate(tmp_path / "estate")
    run_root = tmp_path / "run"
    _native_root(run_root, c1)
    assert _seal_offline(run_root, c2).exit_code == 0
    _rewrite_seal_record_field(run_root, 1, claimed_actor="mallory@ops")
    refused = _invoke("audit", "--run-root", str(run_root))
    assert refused.exit_code == 2
    assert "vs record 'mallory@ops'" in refused.output  # the comparison, by name


def test_audit_refuses_a_segment_whose_pins_left_its_manifest(tmp_path: Path) -> None:
    """PR-22 at audit: the manifest and the segment are one object written
    twice. A closed segment whose pins were rewritten under an untouched,
    self-consistent manifest refuses before either is used as evidence."""
    from dsl41.canon import canonical_bytes

    c1, c2, _ = _estate(tmp_path / "estate")
    run_root = tmp_path / "run"
    _native_root(run_root, c1)
    assert _seal_offline(run_root, c2).exit_code == 0
    records = read_journal(wal_path(run_root, 1))
    assert records[0].get("rec") == "segment"
    records[0] = {**records[0], "baseline_id": str(uuid.uuid4())}
    wal_path(run_root, 1).write_bytes(
        b"".join(canonical_bytes(record) + b"\n" for record in records)
    )
    refused = _invoke("audit", "--run-root", str(run_root))
    assert refused.exit_code == 2 and "manifest" in refused.output


def test_an_attestation_has_exactly_one_byte_form(tmp_path: Path) -> None:
    """ss3.2: a payload that omits a defaulted key still model-validates
    and still digests right (the digest is computed over the FILLED
    model), so byte equality with the canonical serialization is what
    forbids a second byte form -- and the model refuses coercions and a
    base-case link anywhere but period 1."""
    from dsl41.canon import canonical_bytes as canon_bytes

    good = _golden()
    payload = json.loads(good.to_bytes())
    del payload["scope"]  # defaulted: the reduced payload still validates
    reduced = canon_bytes(payload)
    with pytest.raises(EngineError, match="one byte form"):
        Attestation.from_bytes(reduced, where="reduced")
    body = {k: v for k, v in json.loads(good.to_bytes()).items() if k != "digest"}
    with pytest.raises(Exception, match="valid integer"):
        Attestation.model_validate({**body, "period_id": True})  # strict: no bool coercion
    with pytest.raises(Exception, match="base case"):
        Attestation.model_validate({**body, "prev_attestation_digest": None})  # period 2, null
    with pytest.raises(Exception, match="fractional"):
        Attestation.model_validate({**body, "audited_at": "2026-08-21T10:00:00"})


def test_a_crashed_audit_retry_still_flips_the_attested_row(tmp_path: Path) -> None:
    """ss1.3: the artifact lands before the CAS, so a crash between the
    two leaves the row unflipped -- and an idempotent retry that returned
    the stored checkpoint EARLY would leave it unflipped forever."""
    c1, c2, _ = _estate(tmp_path / "estate")
    run_root = tmp_path / "run"
    _native_root(run_root, c1)
    assert _seal_offline(run_root, c2).exit_code == 0
    first = audit_period(run_root, 1, anchor=None)  # the crash window: artifact, no CAS
    row = _anchor_of(run_root).read().periods["1"]  # type: ignore[union-attr]
    assert row.attested is False
    again = audit_period(run_root, 1, anchor=_anchor_of(run_root))
    assert again.digest == first.digest  # idempotent artifact...
    row = _anchor_of(run_root).read().periods["1"]  # type: ignore[union-attr]
    assert row.attested is True  # ...and the transition still finishes


def test_adoption_refuses_an_occupied_anchor_before_the_fence(tmp_path: Path) -> None:
    """ss1.3/ss11: fencing first and refusing at `create_adopting` would
    leave the legacy root rewritten under an anchor that was never this
    estate's."""
    c1, c2, _ = _estate(tmp_path / "estate")
    other_root = tmp_path / "other"
    _native_root(other_root, c1)  # its anchor holds another estate
    run_root = tmp_path / "legacy"
    _legacy_root(run_root, c1)
    before = (run_root / "journal.jsonl").read_bytes()
    refused = _adopt(run_root, c2, "--estate-anchor", str(default_anchor_dir(other_root)))
    assert refused.exit_code == 2 and "somebody's" in refused.output
    assert (run_root / "journal.jsonl").read_bytes() == before  # the fence never ran
    assert read_sentinel(run_root) is None


def test_an_adopted_root_refuses_a_fresh_anchor(tmp_path: Path) -> None:
    """ss1.3: re-running a COMPLETED adoption against a second empty
    anchor would mint a second closed authority over one root -- the fork
    the anchor exists to prevent."""
    c1, c2, _ = _estate(tmp_path / "estate")
    run_root = tmp_path / "legacy"
    _legacy_root(run_root, c1)
    assert _adopt(run_root, c2).exit_code == 0
    fresh = tmp_path / "second.anchor"
    replayed = _adopt(run_root, c2, "--estate-anchor", str(fresh))
    assert replayed.exit_code == 2 and "ORIGINAL --estate-anchor" in replayed.output
    assert not (fresh / "anchor.json").exists()


def test_the_fence_refuses_a_foreign_file_under_the_archived_name(tmp_path: Path) -> None:
    """ss11 step 3: the archived name must BE the legacy journal (same
    inode) -- a foreign file would be trusted as the legacy WAL forever,
    and the rename would delete the only real copy."""
    from dsl41.estate import fence_legacy_root, legacy_journal

    c1, _, _ = _estate(tmp_path / "estate")
    run_root = tmp_path / "legacy"
    _legacy_root(run_root, c1)
    target = legacy_journal(run_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("a stranger's bytes\n")
    with pytest.raises(EngineError, match="not the same file"):
        fence_legacy_root(run_root, "e-1", anchor_dir=default_anchor_dir(run_root))
    assert (run_root / "journal.jsonl").exists()  # nothing was replaced


def test_adoption_refuses_an_unanswering_supervisor_socket(tmp_path: Path) -> None:
    """ss11 step 2 / ss8: a supervisor socket that exists and does not
    answer means the process that owns the live-wrapper evidence is
    unreachable -- a drained estate cannot be proved."""
    c1, c2, _ = _estate(tmp_path / "estate")
    run_root = tmp_path / "legacy"
    _legacy_root(run_root, c1)
    (run_root / "supervisor.sock").write_text("")  # exists, is not a socket
    refused = _adopt(run_root, c2)
    assert refused.exit_code == 2 and "does not answer" in refused.output


def test_adoption_refuses_a_supervisor_that_lists_a_live_wrapper(tmp_path: Path) -> None:
    """ss11 step 2: a legacy supervisor can hold a live wrapper the local
    spool never recorded, so the spool alone is not the drain proof."""
    import socket as socket_mod
    import tempfile
    import threading

    c1, c2, _ = _estate(tmp_path / "estate")
    # AF_UNIX sun_path is ~104 bytes and pytest's tmp_path is deep -- the
    # socket (and therefore the legacy root) needs a short base
    short = Path(tempfile.mkdtemp(prefix="dsl41a-", dir="/tmp"))
    run_root = short / "legacy"
    _legacy_root(run_root, c1)
    server = socket_mod.socket(socket_mod.AF_UNIX)
    server.bind(str(run_root / "supervisor.sock"))
    server.listen(1)

    def answer() -> None:
        conn, _ = server.accept()
        conn.recv(65536)
        conn.sendall(
            (
                json.dumps(
                    {
                        "ok": True,
                        "version": 1,
                        "runs": [{"job": "ghost", "run_number": 7, "wrapper_alive": True}],
                    }
                )
                + "\n"
            ).encode()
        )
        conn.close()

    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    try:
        refused = _adopt(run_root, c2)
    finally:
        server.close()
        thread.join(timeout=5)
        shutil.rmtree(short, ignore_errors=True)
    assert refused.exit_code == 2 and "still lists live wrapper(s) ghost.7" in refused.output


def test_the_fold_never_takes_a_strangers_spawn_record(tmp_path: Path) -> None:
    """DL-118 at the fold: a spawn.json naming another (job, run_number)
    is a stranger's record, and copying its run_id would forge a durable
    binding every later identity check then trusts."""
    from dsl41.estate import _folded_effect

    root = tmp_path / "legacy"
    run_dir = root / "runs" / "a.1"
    run_dir.mkdir(parents=True)
    (run_dir / "spawn.json").write_text(
        json.dumps({"job": "b", "run_number": 9, "run_id": str(uuid.uuid4())})
    )
    effect = {"kind": "SPAWN", "job": "a", "run_number": 1, "effect_id": "e-1", "run_id": None}
    with pytest.raises(EngineError, match="null run_id is legal only"):
        _folded_effect(root, effect, {"e-1": "applied"})


def test_recovery_of_a_committed_adoption_validates_the_sidecar(tmp_path: Path) -> None:
    """ss11's matrix row acts on a COMMITTED boundary, and committed means
    the sidecar exists, mirrors the record, and is this adoption's -- a
    shape-valid record alone must not close the head."""
    c1, c2, _ = _estate(tmp_path / "estate")
    run_root = tmp_path / "legacy"
    _legacy_root(run_root, c1)
    assert _adopt(run_root, c2).exit_code == 0
    # re-open the recovery row: head back to adopting, record mutated
    anchor = _anchor_of(run_root)
    anchor.acquire()
    stored = anchor.require()
    from dsl41.boundary import AdoptingHead

    anchor.write(stored.model_copy(update={"head": AdoptingHead(period_id=1, root=str(run_root))}))
    anchor.release()
    _rewrite_seal_record_field(run_root, 1, claimed_actor="mallory@ops")
    refused = _adopt(run_root, c2)
    assert refused.exit_code == 2 and "disagrees" in refused.output
    head = _head(run_root)
    assert isinstance(head, AdoptingHead)  # the CAS did not run over the forgery


def test_reclaim_refuses_a_claim_whose_body_its_name_does_not_bind(tmp_path: Path) -> None:
    """ss1.3: the claim id is derived from {prev_seal_digest, next_period,
    target_root}; a swapped canonical body under the head's filename
    recomputes to a different id, and a reclaim that trusted it would
    rewrite the head to a lineage this claim id never bound."""
    from dsl41.canon import canonical_bytes

    c1, c2, jil3 = _estate(tmp_path / "estate")
    root_a = tmp_path / "a"
    _native_root(root_a, c1)
    assert _seal_offline(root_a, c2).exit_code == 0
    assert _invoke("audit", "--run-root", str(root_a)).exit_code == 0
    root_b = tmp_path / "b"
    with pytest.raises(_Stopped):
        _roll(root_b, default_anchor_dir(root_a), jil3, stop_at="after_claim")
    anchor = _anchor_of(root_a)
    anchor.acquire()
    stored = anchor.require()
    head = stored.head
    from dsl41.boundary import ClaimedHead

    assert isinstance(head, ClaimedHead)
    claim = anchor.read_claim(head.claim_id)
    assert claim is not None
    forged = claim.model_copy(update={"next_period": claim.next_period + 1})
    anchor.claim_path(head.claim_id).write_bytes(
        canonical_bytes(forged.model_dump(mode="json")) + b"\n"
    )
    try:
        with pytest.raises(EngineError, match="does not bind its body"):
            anchor.reclaim(estate_id=stored.estate_id, claimed_actor="ops@test")
    finally:
        anchor.release()


# ------------------------------------------ peer-review round-2 pins (DL-134)


def _adopt_stopped_at(run_root: Path, next_jil: Path, stage: str, *, anchor_dir: Path) -> None:
    """Run adoption up to `stage` and crash there, via its own seam."""
    from dsl41.boundary import stage_next_period
    from dsl41.estate import adopt_legacy_root

    parsed = [parse(next_jil.read_text(), file=str(next_jil))]
    catalog = lower_catalog(parsed)
    staged_manifest = stage_manifest(
        catalog,
        source_bundle_hash=write_bundle(
            run_root, [SourceFile(path=str(next_jil), text=render_preserve(parsed[0]))]
        ),
        profile=RuntimeProfile(),
        state_machine_version=STATE_MACHINE_VERSION,
    )
    stage_next_period(run_root, staged_manifest=staged_manifest)

    def crash(name: str) -> None:
        if name == stage:
            raise _Stopped()

    with pytest.raises(_Stopped):
        adopt_legacy_root(
            run_root,
            anchor_dir=anchor_dir,
            profile=RuntimeProfile(),
            staged_manifest=staged_manifest,
            claimed_actor="tester@ops",
            crash_point=crash,
        )


def test_a_fence_crash_retry_needs_the_original_anchor_and_gets_it(tmp_path: Path) -> None:
    """ss1.3/ss11: a crash between the fence and `create_adopting` leaves
    an adopted sentinel with the correct anchor still EMPTY. The binding
    the fence wrote is what tells that window from a retry pointed at the
    wrong anchor -- one proceeds, the other is the fork."""
    c1, c2, _ = _estate(tmp_path / "estate")
    run_root = tmp_path / "legacy"
    _legacy_root(run_root, c1)
    original = default_anchor_dir(run_root)
    _adopt_stopped_at(run_root, c2, "after_fence", anchor_dir=original)
    sentinel = read_sentinel(run_root)
    assert sentinel is not None and sentinel.adopted_anchor is not None  # bound IN the sentinel
    assert not (run_root / "legacy" / "anchor.json").exists()  # never a swappable side file
    assert _anchor_of(run_root).read() is None  # ...and no authority yet

    wrong = _adopt(run_root, c2, "--estate-anchor", str(tmp_path / "wrong.anchor"))
    assert wrong.exit_code == 2 and "ORIGINAL --estate-anchor" in wrong.output
    assert str(original) in wrong.output  # the refusal NAMES the bound one

    retried = _adopt(run_root, c2)  # the original anchor: the crash window resumes
    assert retried.exit_code == 0, retried.output
    head = _head(run_root)
    from dsl41.boundary import ClosedHead

    assert isinstance(head, ClosedHead)


def test_adoption_refuses_a_list_error_envelope(tmp_path: Path) -> None:
    """ss11 step 2: an error envelope ("unsupported_version", a refusal)
    is NOT an empty estate -- reading it as drained would fence over live
    work."""
    import socket as socket_mod
    import tempfile
    import threading

    c1, c2, _ = _estate(tmp_path / "estate")
    short = Path(tempfile.mkdtemp(prefix="dsl41e-", dir="/tmp"))
    run_root = short / "legacy"
    _legacy_root(run_root, c1)
    server = socket_mod.socket(socket_mod.AF_UNIX)
    server.bind(str(run_root / "supervisor.sock"))
    server.listen(1)

    def answer() -> None:
        conn, _ = server.accept()
        conn.recv(65536)
        conn.sendall((json.dumps({"ok": False, "error": "unsupported_version"}) + "\n").encode())
        conn.close()

    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    try:
        refused = _adopt(run_root, c2)
    finally:
        server.close()
        thread.join(timeout=5)
        shutil.rmtree(short, ignore_errors=True)
    assert refused.exit_code == 2 and "well-formed LIST" in refused.output


def test_the_attested_cas_refuses_a_strangers_anchor(tmp_path: Path) -> None:
    """ss1.3: the row is a claim about one estate's one period in one
    root -- flipping it on a stranger's anchor would mark a period
    attested whose proof lives in another lineage entirely."""
    c1, c2, _ = _estate(tmp_path / "estate")
    root_a = tmp_path / "a"
    _native_root(root_a, c1)
    assert _seal_offline(root_a, c2).exit_code == 0
    root_b = tmp_path / "b"
    _native_root(root_b, c1)
    assert _seal_offline(root_b, c2).exit_code == 0
    with pytest.raises(EngineError, match="two geneses are two estates"):
        audit_period(root_a, 1, anchor=_anchor_of(root_b))
    row = _anchor_of(root_b).read().periods["1"]  # type: ignore[union-attr]
    assert row.attested is False  # the stranger's row never flipped


def test_recovery_refuses_a_replacement_successor_manifest(tmp_path: Path) -> None:
    """ss11: presence is not agreement -- a self-consistent replacement
    manifest passes every shape check and refuses only at the opening, so
    the full seal-to-opening validation runs before the recovery CAS."""
    c1, c2, _ = _estate(tmp_path / "estate")
    run_root = tmp_path / "legacy"
    _legacy_root(run_root, c1)
    assert _adopt(run_root, c2).exit_code == 0
    other_root = tmp_path / "other"
    _legacy_root(other_root, c1)
    assert _adopt(other_root, c2).exit_code == 0
    anchor = _anchor_of(run_root)
    anchor.acquire()
    stored = anchor.require()
    from dsl41.boundary import AdoptingHead

    anchor.write(stored.model_copy(update={"head": AdoptingHead(period_id=1, root=str(run_root))}))
    anchor.release()
    # a SELF-CONSISTENT manifest -- another adoption's -- under this root's name
    from dsl41.period import period_dir

    shutil.copyfile(
        period_dir(other_root, 2) / "manifest.json", period_dir(run_root, 2) / "manifest.json"
    )
    refused = _adopt(run_root, c2)
    assert refused.exit_code == 2
    head = _head(run_root)
    assert isinstance(head, AdoptingHead)  # the CAS did not run over the replacement


def test_reclaim_refuses_a_registry_row_that_never_closed(tmp_path: Path) -> None:
    """ss1.3: exact equality, null included -- a claimed lineage whose
    registry names no closing seal must not get a closed head minted from
    the claim's digest alone."""
    c1, c2, jil3 = _estate(tmp_path / "estate")
    root_a = tmp_path / "a"
    _native_root(root_a, c1)
    assert _seal_offline(root_a, c2).exit_code == 0
    assert _invoke("audit", "--run-root", str(root_a)).exit_code == 0
    root_b = tmp_path / "b"
    with pytest.raises(_Stopped):
        _roll(root_b, default_anchor_dir(root_a), jil3, stop_at="after_claim")
    anchor = _anchor_of(root_a)
    anchor.acquire()
    stored = anchor.require()
    row = stored.periods["1"]
    anchor.write(
        stored.model_copy(
            update={
                "periods": {**stored.periods, "1": row.model_copy(update={"seal_digest": None})}
            }
        )
    )
    try:
        with pytest.raises(EngineError, match="the head would go back"):
            anchor.reclaim(estate_id=stored.estate_id, claimed_actor="ops@test")
    finally:
        anchor.release()


# ------------------------------------------ peer-review round-3 pins (DL-134)


def test_audit_refuses_a_sidecar_the_successor_did_not_open_from(tmp_path: Path) -> None:
    """ss11: the successor segment's `opens_from_seal` is the INDEPENDENT
    artifact that pins the sidecar under audit -- a coherent re-forge
    (sidecar AND record restamped together) passes every self-consistency
    check and refuses here, before any evidence is folded."""
    run_root = _two_periods(tmp_path)  # period 2 OPENED: wal/000002 names period 1's seal
    _rewrite_closes_at_index(run_root, 1)  # the coherent forge
    refused = _invoke("audit", "--run-root", str(run_root))
    assert refused.exit_code == 2
    assert "opened its successor from" in refused.output


def test_adoption_refuses_a_list_row_missing_the_liveness_flag(tmp_path: Path) -> None:
    """ss11 step 2: a row shape without `wrapper_alive` would read as
    false -- and fence over an unspooled live wrapper."""
    import socket as socket_mod
    import tempfile
    import threading

    c1, c2, _ = _estate(tmp_path / "estate")
    short = Path(tempfile.mkdtemp(prefix="dsl41f-", dir="/tmp"))
    run_root = short / "legacy"
    _legacy_root(run_root, c1)
    server = socket_mod.socket(socket_mod.AF_UNIX)
    server.bind(str(run_root / "supervisor.sock"))
    server.listen(1)

    def answer() -> None:
        conn, _ = server.accept()
        conn.recv(65536)
        conn.sendall(
            (
                json.dumps({"ok": True, "version": 1, "runs": [{"job": "g", "run_number": 1}]})
                + "\n"
            ).encode()
        )
        conn.close()

    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    try:
        refused = _adopt(run_root, c2)
    finally:
        server.close()
        thread.join(timeout=5)
        shutil.rmtree(short, ignore_errors=True)
    assert refused.exit_code == 2 and "well-formed LIST" in refused.output


def test_the_attested_cas_refuses_a_provisional_or_undurable_row(tmp_path: Path) -> None:
    """ss1.3: a `seal` record can land before the close CAS, leaving the
    registry row provisional (`seal_digest: null`) -- and a reclaimable
    period can lose `segment_durable`. Neither row is attested."""
    c1, c2, _ = _estate(tmp_path / "estate")
    run_root = tmp_path / "run"
    _native_root(run_root, c1)
    assert _seal_offline(run_root, c2).exit_code == 0
    first = audit_period(run_root, 1, anchor=None)
    anchor = _anchor_of(run_root)
    anchor.acquire()
    stored = anchor.require()
    row = stored.periods["1"]
    for broken in (
        row.model_copy(update={"seal_digest": None}),
        row.model_copy(update={"segment_durable": False}),
    ):
        anchor.write(stored.model_copy(update={"periods": {**stored.periods, "1": broken}}))
        with pytest.raises(EngineError, match="only a committed, durable row"):
            anchor.attest(
                1,
                estate_id=stored.estate_id,
                root=run_root,
                seal_digest=first.seal_digest,
            )
    anchor.release()


def test_reclaim_refuses_an_undurable_closing_row(tmp_path: Path) -> None:
    """ss1.3: the head the reclaim mints must go back to a closing row
    that is committed AND durable -- a matching digest over an undurable
    segment is a period archive readers would skip while a successor may
    open."""
    c1, c2, jil3 = _estate(tmp_path / "estate")
    root_a = tmp_path / "a"
    _native_root(root_a, c1)
    assert _seal_offline(root_a, c2).exit_code == 0
    assert _invoke("audit", "--run-root", str(root_a)).exit_code == 0
    root_b = tmp_path / "b"
    with pytest.raises(_Stopped):
        _roll(root_b, default_anchor_dir(root_a), jil3, stop_at="after_claim")
    anchor = _anchor_of(root_a)
    anchor.acquire()
    stored = anchor.require()
    row = stored.periods["1"]
    anchor.write(
        stored.model_copy(
            update={
                "periods": {
                    **stored.periods,
                    "1": row.model_copy(update={"segment_durable": False}),
                }
            }
        )
    )
    try:
        with pytest.raises(EngineError, match="the head would go back"):
            anchor.reclaim(estate_id=stored.estate_id, claimed_actor="ops@test")
    finally:
        anchor.release()


def test_two_racing_audits_publish_exactly_one_checkpoint(tmp_path: Path, monkeypatch) -> None:
    """ss1.3: attestation publication is CREATE-ONLY -- two racers would
    otherwise publish two digests for one period (`audited_at` differs),
    and a roll could import one while the closing root keeps the other: a
    forked chain. The loser verifies and returns the winner unchanged."""
    c1, c2, _ = _estate(tmp_path / "estate")
    run_root = tmp_path / "run"
    _native_root(run_root, c1)
    assert _seal_offline(run_root, c2).exit_code == 0
    winner = audit_period(run_root, 1, anchor=None)

    # the racer: it read "no checkpoint" before the winner published, so
    # its create must LOSE, not overwrite
    from dsl41 import attest as attest_mod

    real = attest_mod.read_attestation
    calls = {"n": 0}

    def first_sees_nothing(*args: Any, **kwargs: Any):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(*args, **kwargs)

    monkeypatch.setattr(attest_mod, "read_attestation", first_sees_nothing)
    loser = audit_period(run_root, 1, anchor=_anchor_of(run_root))
    monkeypatch.undo()
    assert loser.digest == winner.digest  # the winner IS the checkpoint
    stored = read_attestation(run_root, 1)
    assert stored is not None and stored.digest == winner.digest
    row = _anchor_of(run_root).read().periods["1"]  # type: ignore[union-attr]
    assert row.attested is True  # and the loser still finished the CAS


def test_the_race_loser_makes_the_winner_durable_before_the_cas(
    tmp_path: Path, monkeypatch
) -> None:
    """ss1.3: the winner links, THEN fsyncs -- a loser who flips the
    `attested` row over the merely-visible file leaves a durable row
    pointing at a checkpoint a power cut can drop. The loser fsyncs the
    winning file and its directory first, on both the EEXIST path and the
    idempotent early return."""
    import os as os_mod

    c1, c2, _ = _estate(tmp_path / "estate")
    run_root = tmp_path / "run"
    _native_root(run_root, c1)
    assert _seal_offline(run_root, c2).exit_code == 0
    winner = audit_period(run_root, 1, anchor=None)
    target_ino = os_mod.stat(attestation_path(run_root, 1)).st_ino
    dir_ino = os_mod.stat(attestation_path(run_root, 1).parent).st_ino

    real_fsync = os_mod.fsync
    synced: set[int] = set()

    def recording(fd: int) -> None:
        synced.add(os_mod.fstat(fd).st_ino)
        real_fsync(fd)

    # idempotent early return: the existing checkpoint is fsynced before the flip
    monkeypatch.setattr("os.fsync", recording)
    again = audit_period(run_root, 1, anchor=_anchor_of(run_root))
    monkeypatch.undo()
    assert again.digest == winner.digest
    assert target_ino in synced and dir_ino in synced

    # EEXIST path: a racer that saw "no checkpoint" also fsyncs the winner
    from dsl41 import attest as attest_mod

    real_read = attest_mod.read_attestation
    calls = {"n": 0}

    def first_sees_nothing(*args: Any, **kwargs: Any):
        calls["n"] += 1
        return None if calls["n"] == 1 else real_read(*args, **kwargs)

    synced.clear()
    monkeypatch.setattr(attest_mod, "read_attestation", first_sees_nothing)
    monkeypatch.setattr("os.fsync", recording)
    loser = audit_period(run_root, 1, anchor=None)
    monkeypatch.undo()
    assert loser.digest == winner.digest
    assert target_ino in synced and dir_ino in synced
