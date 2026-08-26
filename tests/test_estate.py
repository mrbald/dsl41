"""The estate verbs: `seal`, `audit`, `verify`, `estate reclaim` and the
physical roll (period-model ss1.3, ss7; DL-134).

Obligations in ss13 exercised here: PR-01c, PR-02a, PR-02d, PR-02e,
PR-02f, PR-47a and PR-47b. PR-48 was retired by DL-138 with the adoption
path this file used to drive; its replacement refusal tests live with the
readers that own them -- the record validator and the D4 dispatcher in
test_decision_record.py and test_period_identity.py, the `claim_root` and
`plan_retention` tombstones in test_boundary.py and test_retention.py, the
retired manifest layout in test_run_history.py, and the pre-parse
`adopting` anchor refusal in test_boundary.py.

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
    SourceFile,
    attestation_path,
    read_period_manifest,
    read_sentinel,
    seal_path,
    sealed_periods,
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


def _bundled_root(run_root: Path, jil: Path, *, stored: str, permit_unknown: bool = False):
    """`_native_root`, with the BUNDLE's stored text under the caller's
    control.

    The bundle is what an offline seal re-parses C1 from (ss7), and the two
    ways it can fail to are what this exposes: bytes that are not JIL at
    all, and bytes that need `--permit-unknown` to lower. Both are stored
    self-consistently -- `write_bundle` addresses whatever it is given --
    so the estate is well-formed and the READ is the thing under test."""
    parsed = [parse(jil.read_text(), file=str(jil))]
    catalog = lower_catalog(parsed, permit_unknown=permit_unknown)
    staged = stage_manifest(
        catalog,
        source_bundle_hash=write_bundle(run_root, [SourceFile(path=str(jil), text=stored)]),
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
    asyncio.run(started.run_until_quiescent(started.clock.now()))
    asyncio.run(started.shutdown())
    assert started.journal is not None
    started.journal.close()
    return catalog


def test_a_boundary_that_fails_outside_the_house_error_still_exits_two(
    tmp_path: Path, capsys
) -> None:
    """DL-145. ss7 publishes 0/2/4 for `seal` and nothing else. The offline
    driver's tail read the exception's TYPE instead -- exit 2 for an
    `EngineError`, exit 1 for anything else -- so an `OSError` out of the
    boundary reported "the estate failed while running" for a period that
    did not close and is still serving C1.

    The refusal goes through the one reading of it (`cli_common.refuse`),
    which is where the 0/2/4 promise lives."""
    import asyncio as asyncio_mod

    from dsl41.cli_estate import _drive_boundary

    class _StubEngine:
        """Just the surface `_drive_boundary` reads."""

        journal = None

        async def submit_seal(self, request: Any) -> Any:
            raise OSError("the volume holding the anchor went away")

        async def run_until_quiescent(self, until: Any) -> None:
            await asyncio_mod.sleep(3600)  # still serving C1, as ss7 says

        async def shutdown(self) -> None:
            return None

    code = asyncio.run(
        _drive_boundary(_StubEngine(), object(), tmp_path, None)  # type: ignore[arg-type]
    )
    assert code == 2
    assert "the volume holding the anchor went away" in capsys.readouterr().err


def test_an_offline_seal_over_a_bundle_that_does_not_reparse_refuses_by_name(
    tmp_path: Path,
) -> None:
    """DL-145. The offline sealer had its OWN loader beside
    `boundary.load_bundle_catalog`, and the copy was weaker in one way that
    mattered: it did not wrap `JilParseError`/`LoweringError` in
    `EngineError`, so the `except EngineError` around it never saw them.
    A root whose stored bundle no longer parses answered with an uncaught
    traceback and exit 1 -- "the estate failed while running", for a period
    that is still open and unharmed.

    One loader now, and the sentence an operator reads is the owner's."""
    c1, c2, _ = _estate(tmp_path / "estate")
    run_root = tmp_path / "run"
    _bundled_root(run_root, c1, stored="this is not JIL at all\n")

    refused = _seal_offline(run_root, c2)
    assert refused.exit_code == 2
    assert "does not load" in refused.output
    assert "cannot be rebuilt from its own bundle" in refused.output
    assert not seal_path(run_root, 1).exists()
    assert isinstance(_head(run_root), OpenHead)  # C1 is still open


def test_an_offline_seal_reads_c1_with_permit_unknown(tmp_path: Path) -> None:
    """DL-145, the other half of the same ruling. The bundle holds the EXACT
    bytes this period ran; the gate that decided whether an unknown
    attribute was acceptable ran once, at launch. Re-asking it at the
    boundary made `dsl41 seal` refuse a root `dsl41 run` was serving --
    with a traceback, because the refusal was not an `EngineError` either.

    Same call `cli_run._period_catalog` makes, for the same stated
    reason."""
    base = tmp_path / "estate"
    base.mkdir(parents=True, exist_ok=True)
    c1 = base / "c1.jil"
    c1.write_text(C1_JIL + "no_such_attribute: 7\n")
    c2 = base / "c2.jil"
    c2.write_text(C2_JIL)
    run_root = tmp_path / "run"
    _bundled_root(run_root, c1, stored=c1.read_text(), permit_unknown=True)

    assert _seal_offline(run_root, c2).exit_code == 0
    assert read_seal(run_root, 1).period_id == 1


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


def test_an_attestation_payload_that_is_not_an_object_refuses() -> None:
    """The preamble's second branch: a canonical JSON document that decodes
    to something other than an object is not an attestation, whatever its
    shape (period-model ss3.2)."""
    with pytest.raises(EngineError, match="not a JSON object"):
        Attestation.from_bytes(b"[]", where="x")


def test_an_attestation_with_no_artifact_format_version_refuses() -> None:
    """The preamble's third branch, in the shape it actually reaches: a
    PRESENT-but-wrong version is already caught inside `decode` itself
    (canon.check_artifact_version), so the only live case here is the key
    being ABSENT -- DL-157's rule, and the message still cites PR-08d."""
    from dsl41.canon import canonical_bytes as canon_bytes

    payload = json.loads(_golden().to_bytes())
    del payload["artifact_format_version"]
    with pytest.raises(EngineError, match="artifact_format_version None") as excinfo:
        Attestation.from_bytes(canon_bytes(payload), where="x")
    assert "(PR-08d)" in str(excinfo.value)


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


def test_pr47b_audit_derives_source_and_never_reads_it(tmp_path: Path) -> None:
    """ss11: `source` is audit's to DERIVE, never to read.

    DL-138 retired the `adopt` value, so the derivation now has one legal
    answer -- and the comparison STAYS, which is the whole obligation:
    a `seal` record whose `source` was rewritten refuses, and the day a
    second value returns the check is already where it belongs. PR-47b's
    request-only audit duties are unchanged; only its adoption clauses
    left."""
    from dsl41.attest import _boundary_request

    c1, c2, _ = _estate(tmp_path / "estate")
    run_root = tmp_path / "run"
    _native_root(run_root, c1)
    assert _seal_offline(run_root, c2).exit_code == 0
    seal = read_seal(run_root, 1)
    assert seal.boundary_request.source == "request"
    assert rederive_seal(run_root, 1).digest == seal.digest

    record = read_journal(wal_path(run_root, 1))[-1]
    assert record["rec"] == "seal" and record["source"] == "request"
    derived = _boundary_request(record)
    assert derived.source == "request" and derived.request_id == record["request_id"]
    with pytest.raises(EngineError, match="audit's to derive, never to read"):
        _boundary_request({**record, "source": "somebody-elses"})


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


# ---------------------------------------------- DL-138: the verb is gone


def test_estate_adopt_is_not_a_command(tmp_path: Path) -> None:
    """D10 (DL-138): the adoption verb went with the path it drove, and the
    `estate` group keeps `reclaim` and `prune`.

    A retired VERB is not a retired dialect: there is no artifact on a disk
    to name, so what an operator gets is typer's own unknown-command exit
    rather than a tombstone (docs/protocol-evolution.md ss6 governs stored
    dialects, not the CLI surface)."""
    gone = _invoke("estate", "adopt", str(tmp_path), "--next", str(tmp_path / "x.jil"))
    assert gone.exit_code != 0
    assert "No such command" in gone.output
    assert _invoke("estate", "--help").exit_code == 0
    assert "adopt" not in _invoke("estate", "--help").output
    for kept in ("reclaim", "prune"):
        assert kept in _invoke("estate", "--help").output


# --------------------------------------------- ss1.3 the registry row


def test_pr02f_the_registry_finds_period_one_after_native_genesis(tmp_path: Path) -> None:
    """ss1.3: the registry maps every period to the root that holds it, and
    period 1's row is written when the root first owns it -- provisional at
    genesis, flipping in the finalize CAS immediately after its segment.

    Amended at DL-138, which retired the other route into this row: the
    obligation stays, and its adoption clause went with the path."""
    c1, _, _ = _estate(tmp_path / "estate")
    run_root = tmp_path / "run"
    _native_root(run_root, c1)
    stored = _anchor_of(run_root).read()
    assert stored is not None
    assert stored.periods["1"].segment_durable is True
    assert stored.periods["1"].root == str(Path(run_root).resolve())


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


def test_the_two_ss3_2_ingresses_ask_one_question() -> None:
    """DL-137: `Seal.from_bytes` and `Attestation.from_bytes` each ended
    with the same rule -- the FILE'S OWN BYTES must be the canonical
    serialization -- and each spelled it itself.

    One predicate now (`canon.is_canonical_file`), asked by both. The
    proof is a padded copy of each artifact: `json.loads` says it is the
    same document, so the digest still matches and every tamper check
    passes, and only this rule stands between one artifact and two byte
    forms of it. A predicate that answered `True` reds both halves here,
    plus each owner's own case. The two REFUSALS stay apart, and the
    assertions below hold them apart: a message that named the wrong
    artifact would send an operator to the wrong file."""
    from test_seal_artifact import GOLDEN_BYTES

    from dsl41.seal import Seal

    padded_seal = GOLDEN_BYTES.decode("utf-8").replace('"epoch":7,', '"epoch": 7,', 1)
    padded_attestation = GOLDEN_ATTESTATION.decode("utf-8").replace(
        '"period_id":2,', '"period_id": 2,', 1
    )
    assert json.loads(padded_seal) == json.loads(GOLDEN_BYTES)  # same document...
    assert json.loads(padded_attestation) == json.loads(GOLDEN_ATTESTATION)
    assert padded_seal.encode() != GOLDEN_BYTES  # ...different file
    assert padded_attestation.encode() != GOLDEN_ATTESTATION

    with pytest.raises(EngineError, match="one byte string"):
        Seal.from_bytes(padded_seal)
    with pytest.raises(EngineError, match="one byte form"):
        Attestation.from_bytes(padded_attestation, where="padded")
    # and the genuine bytes still pass, both doors
    assert Seal.from_bytes(GOLDEN_BYTES).digest == json.loads(GOLDEN_BYTES)["digest"]
    assert Attestation.from_bytes(GOLDEN_ATTESTATION, where="golden") == _golden()


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


# ------------------------ ss2.1/ss2.2 what the CLI composes against (DL-151)


def _seal_next(run_root: Path, next_jil: Path, *extra: str):
    return _invoke("seal", "--run-root", str(run_root), "--next", str(next_jil), *extra)


def test_pr22b_the_launch_gate_reads_the_committed_boundarys_manifest(tmp_path: Path) -> None:
    """ss2.1: the options a resume is held to are the PERIOD IT OPENS
    INTO's, and on a root with a committed boundary that is N+1, not N.

    The gate read the newest SEGMENT's manifest, which a committed-but-
    unopened boundary is one period behind: C2's own options were refused
    against C1's pin, while C1's options passed the gate and opened C2.
    Both halves of one in-place profile change were unreachable in one
    command (DL-151)."""
    from dsl41.cli_run import _resume_profile_error
    from dsl41.period import runtime_profile_from_cli

    c1, c2, _ = _estate(tmp_path / "estate")
    run_root = tmp_path / "run"
    _native_root(run_root, c1)
    assert _seal_next(run_root, c2, "--next-timezone", "Europe/Zurich").exit_code == 0

    c2_options = runtime_profile_from_cli(timezone="Europe/Zurich")
    assert _resume_profile_error(run_root, c2_options, None) is None
    refused = _resume_profile_error(run_root, runtime_profile_from_cli(), None)
    assert refused is not None and "default_tz" in refused


def test_pr22b_the_gate_still_reads_the_open_period_when_no_boundary_is_staged(
    tmp_path: Path,
) -> None:
    """The control: with no committed boundary ahead of it, the period a
    resume opens into is the one already open, and the gate is unchanged."""
    from dsl41.cli_run import _resume_profile_error
    from dsl41.period import runtime_profile_from_cli

    c1, _, _ = _estate(tmp_path / "estate")
    run_root = tmp_path / "run"
    _native_root(run_root, c1)

    assert _resume_profile_error(run_root, runtime_profile_from_cli(), None) is None
    refused = _resume_profile_error(
        run_root, runtime_profile_from_cli(timezone="Europe/Zurich"), None
    )
    assert refused is not None and "default_tz" in refused


def test_pr30e_the_cli_retry_of_a_committed_boundary_closes_no_second_period(
    tmp_path: Path,
) -> None:
    """ss2.2: an exact retry of a committed boundary is answered from the
    next period, and the answer is keyed on the request FINGERPRINT -- which
    covers the `baseline_id` and the `epoch` the original attempt carried.

    The CLI composed from the CURRENT header instead, so the promised route
    was unreachable from `dsl41 seal --request-id`. Offline that was not
    merely a missed deduplication: nothing between the CLI and the cutoff
    would have recognised the retry, so it would have closed a SECOND
    period (DL-151)."""
    c1, c2, _ = _estate(tmp_path / "estate")
    run_root = tmp_path / "run"
    _native_root(run_root, c1)

    first = _seal_next(run_root, c2, "--request-id", "r-1")
    assert first.exit_code == 0, first.output
    digest = read_seal(run_root, 1).digest

    again = _seal_next(run_root, c2, "--request-id", "r-1")
    assert again.exit_code == 0, again.output
    assert digest in again.output and "already closed" in again.output
    assert sealed_periods(run_root) == [1]  # no second boundary
    assert read_seal(run_root, 1).digest == digest


def test_pr30e_a_live_retry_is_answered_by_the_engine_of_the_new_period(
    short_root: Path,  # noqa: F811
) -> None:
    """The same route on the LIVE path, between real processes: the engine
    of period N+1 keeps the `seal` record it opened from and answers an
    exact retry from it -- which the CLI could not reach, because it
    composed the retry under the header the NEW period publishes.

    The evidence is that the second engine keeps serving: an unrecognised
    retry would have been a fresh boundary, and a fresh boundary exits the
    engine with code 3 (DL-151)."""
    _, c2, _ = _estate(short_root)
    with engine(short_root) as first:
        answer = cli(
            "seal", "--run-root", str(first.run_root), "--next", str(c2), "--request-id", "r-live"
        )
        assert answer.returncode == 0, answer.stderr
        first.proc.wait(timeout=30)
    run_root = first.run_root
    digest = read_seal(run_root, 1).digest

    with engine(short_root, resume=True, run_root=run_root, files=[c2]) as second:
        again = cli(
            "seal", "--run-root", str(run_root), "--next", str(c2), "--request-id", "r-live"
        )
        assert again.returncode == 0, again.stderr
        payload = json.loads(again.stdout.splitlines()[0])
        assert payload["decision"] == "applied"
        assert payload["digest"] == digest and payload["next_period_id"] == 2
        assert second.proc.poll() is None  # no second boundary, so no exit 3
    assert sealed_periods(run_root) == [1]


def test_pr30c_the_same_request_id_under_another_envelope_is_a_collision(
    tmp_path: Path,
) -> None:
    """ss2.2's other half, in the same place: force is an authorization and
    the actor is attribution, so neither may be swapped under a retry. The
    id that closed the boundary answers only the command that closed it."""
    c1, c2, _ = _estate(tmp_path / "estate")
    run_root = tmp_path / "run"
    _native_root(run_root, c1)
    assert _seal_next(run_root, c2, "--request-id", "r-1").exit_code == 0

    collision = _seal_next(run_root, c2, "--request-id", "r-1", "--force-seal")
    assert collision.exit_code == 2
    assert "different envelope" in collision.output
    assert sealed_periods(run_root) == [1]


# --------------------------------- ss2.2 the retry route answers by LINEAGE,
# --------------------------------- never by which sidecar file exists (DL-169)


def _staged_manifest_of(run_root: Path, jil: Path) -> tuple[CatalogIR, Any]:
    """The parse -> lower -> bundle -> stage chain `_orphaned_root` needs
    twice (C1, then C2), factored so it carries one copy instead of two.
    Returns the catalog alongside the staged manifest: `start_run` needs
    both and a caller re-parsing to get the catalog back would just be this
    function's own first two lines, inlined a second time."""
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
    return catalog, staged


def _orphaned_root(run_root: Path, c1: Path, c2: Path, request_id: str, actor: str) -> None:
    """A period-1 root staged like `_native_root` builds one (parse, lower,
    bundle, stage, `start_run`), except the FIRST seal attempt crashes right
    after the sidecar write and before the `seal` record append
    (`Engine.crash_point`, `boundary.commit_boundary`'s `after_sidecar`
    stage) -- ss3's crash window, real and not forged. The sidecar this
    leaves on disk is the one `close_runtime` actually wrote; the WAL never
    gets the `seal` record that would name it, and C1 stays open (DL-169).

    Never reaches quiescence and never calls `shutdown` -- the crash IS the
    exit, and `submit_seal`'s own future carries the refusal `_native_root`
    has no equivalent of."""
    from dsl41.boundary import SealRequest, stage_next_period

    catalog, staged = _staged_manifest_of(run_root, c1)
    started = start_run(
        catalog,
        run_root,
        clock=RealClock(),
        adapters={"CMD": LocalCommandAdapter(), "FW": FileWatcherAdapter()},
        staged=staged,
    )

    def crash(name: str) -> None:
        if name == "after_sidecar":
            raise EngineError(f"crash at {name}")

    started.crash_point = crash  # type: ignore[method-assign]
    _, next_manifest = _staged_manifest_of(run_root, c2)
    next_staged = stage_next_period(run_root, staged_manifest=next_manifest)
    request = SealRequest(
        baseline_id=started.baseline_id,
        epoch=started.epoch,
        request_id=request_id,
        next_period=next_staged,
        stage_digest=next_staged.stage_digest,
        force_seal=False,
        claimed_actor=actor,
    )

    async def scenario() -> None:
        future = started.submit_seal(request)
        await started.run_until_quiescent(started.clock.now())
        assert future.done()
        with pytest.raises(EngineError):
            future.result()

    asyncio.run(scenario())
    assert started.journal is not None
    started.journal.close()


def test_dl169_an_orphan_retry_drives_a_fresh_boundary_not_already_closed(
    tmp_path: Path,
) -> None:
    """ss2.2: an orphan sidecar -- the crash window between the sidecar
    write and the `seal` record append -- names nothing until the record
    lands, and only a committed seal is ever deduplicated. Globbing
    `seals/` for the newest FILE (`period.sealed_periods`) reads exactly the
    evidence the rule says to ignore: a retry under the crashed attempt's
    own request_id matched the orphan and answered "already closed" for a
    period that never closed. `_committed_boundary` now answers from
    `boundary.select_seal`'s lineage instead, which finds nothing committed
    here and lets the retry drive a fresh boundary (DL-169)."""
    c1, c2, _ = _estate(tmp_path / "estate")
    run_root = tmp_path / "run"
    _orphaned_root(run_root, c1, c2, "r-orphan", "tester@ci")

    # the crash is real: a genuine sidecar sits on disk, unclaimed by any
    # `seal` record, and C1 is still open
    assert seal_path(run_root, 1).exists()
    assert sealed_periods(run_root) == [1]
    assert read_journal(wal_path(run_root, 1))[-1]["rec"] != "seal"
    assert isinstance(_head(run_root), OpenHead)

    retried = _seal_next(run_root, c2, "--request-id", "r-orphan", "--claimed-actor", "tester@ci")
    assert retried.exit_code == 0, retried.output
    assert "already closed" not in retried.output

    seal = read_seal(run_root, 1)
    assert seal.boundary_request.request_id == "r-orphan"
    records = read_journal(wal_path(run_root, 1))
    assert records[-1]["rec"] == "seal" and records[-1]["digest"] == seal.digest
    head = _head(run_root)
    assert isinstance(head, ClosedHead) and head.seal_digest == seal.digest


def test_dl169_a_rolled_roots_retry_still_answers_from_the_imported_sidecar(
    tmp_path: Path,
) -> None:
    """ss11 step 3's other branch: a rolled root never holds the
    predecessor's WAL or `seal` record, so `select_seal` proves the imported
    sidecar by its successor's `opens_from_seal` digest link instead. This
    is the case the glob got right BY ACCIDENT -- root B holds exactly one
    sidecar and it happens to be the legitimately imported one -- and the
    fix must not lose it (PR-02a/PR-30e)."""
    c1, c2, _ = _estate(tmp_path / "estate")
    root_a = tmp_path / "a"
    _native_root(root_a, c1)
    assert (
        _seal_next(root_a, c2, "--request-id", "r-roll", "--claimed-actor", "tester@ci").exit_code
        == 0
    )
    assert _invoke("audit", "--run-root", str(root_a)).exit_code == 0
    anchor_dir = default_anchor_dir(root_a)
    root_b = tmp_path / "b"
    _roll(root_b, anchor_dir, c2)
    assert wal_path(root_b, 2).exists()
    assert not wal_path(root_b, 1).exists()  # the predecessor WAL, genuinely absent
    assert sealed_periods(root_b) == [1]  # the imported copy is what stands in for it

    retried = _seal_next(
        root_b,
        c2,
        "--request-id",
        "r-roll",
        "--claimed-actor",
        "tester@ci",
        "--estate-anchor",
        str(anchor_dir),
    )
    assert retried.exit_code == 0, retried.output
    assert "already closed" in retried.output and "period 1" in retried.output
    assert "This root is at period 2" in retried.output
    assert sealed_periods(root_b) == [1]  # no second boundary
