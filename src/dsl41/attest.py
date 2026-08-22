"""The attestation: what `audit` produces and `verify` consumes.

Normative spec: `docs/period-model.md` ss1.3 (the successor fence's
attestation rules) and ss11 ("verified means re-derived, not
self-consistent"). Built by DL-134 as U7. Obligations PR-02a, PR-02d,
PR-02e, PR-47a in ss13.

**An attestation is a chain checkpoint, by induction and not by
assertion**, and producing one is not the same act as consuming one:

- *Producing* N (`audit`) re-derives the seal from the period's own
  evidence and requires the PREDECESSOR attestation **present and
  verified**. Period 1
  is the base case, with `prev_attestation_digest: null`. There is
  deliberately no "or re-derive everything below" alternative: it left
  `prev_attestation_digest` undefined when no predecessor artifact
  existed, and a wrong implementation then checks only its own digest and
  seal binding, emits a "checkpoint" over an unaudited opening seal, and
  earlier roots get deleted on a chain that was never established.
- *Consuming* N (`verify`) accepts N **alone** -- its own digest, its
  binding to the seal it names, and its `chain_through_period` -- because
  the producing audit already established the induction, and a physical
  roll imports only the current seal and attestation. Draft 8 wrote one
  rule for both and made a second roll impossible.

So a root that imported seal 2 and attestation 2 while its predecessors
are gone verifies the chain below seal 2 *because attestation 2 proves
it*, and `audit` there refuses: full re-derivation needs the period's WAL,
spool and manifests, and a rolled root holds none of its predecessor's
(PR-02a, PR-02e).

**"Verified" means re-derived.** A sidecar whose digest matches its own
canonical form proves integrity, not derivation. `rederive_seal` rebuilds
the sidecar from the four inputs ss11 names -- the opening seal, the
complete ordered WAL of the period, the immutable spool evidence, and the
C1/C2 manifests. The sentinel is NOT one of them since DL-138: it was read
for a single derivation (`boundary_request.source`) over a two-value domain,
and the second value went with the estate-adoption path. The
`boundary_request` input scalars are the
one exception the spec states: they originate in a request no WAL record
independently holds, so audit checks them record-against-sidecar and
carries them.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from dsl41.boundary import (
    EstateAnchor,
    check_seal_record,
    carried_outbox,
    executing_jobs,
    executions_at,
    read_seal,
    retry_horizon_gate,
    seal_fingerprint,
    check_record_names_sidecar,
    load_bundle_catalog,
    staged_next_from,
)
from dsl41.canon import (
    ARTIFACT_FORMAT_VERSION,
    CanonError,
    canonical_bytes,
    decode,
    digest as digest_over,
    is_canonical_file,
    with_digest,
)
from dsl41.classify import Baseline, carried_from_oracle, classify
from dsl41.oracle import Oracle
from dsl41.oracle_state import CarriedRows
from dsl41.period import (
    SENTINEL_NAME,
    ArchiveReceipt,
    Manifest,
    archive_receipt_path,
    attestation_path,
    check_stamp,
    read_period_manifest,
    seal_path,
    wal_path,
    check_manifest_against_segment,
    disagreements,
    is_hash_address,
    opening_at,
    read_archive_receipt,
    read_sentinel,
    wrote_period,
)
from dsl41.runner_clock import EngineError
from dsl41.runner_hosts import LOCAL_EXECUTOR_ID, seed_local_executor
from dsl41.runner_journal import dsl41_version, read_journal, replay_inputs
from dsl41.runner_ledger import STATE_MACHINE_VERSION, next_epoch
from dsl41.runner_procid import durable_create, make_durable
from dsl41.seal import (
    BoundaryRequest,
    Seal,
    SealedHost,
    SealedState,
    close_runtime,
    implicit_routes,
    open_from_seal,
)

#: ss11's word for what `audit` proves. One value today, and a FIELD
#: rather than a constant because the whole point of writing it down is
#: that a later, narrower proof must not be readable as this one.
FULL: Final[str] = "full"

#: ss11's two NAMED tiers of "verified" (DL-144). They are two different
#: proofs of two different strengths, and they are spelled differently on
#: purpose: a reader that printed one word for both would let an estate
#: whose inputs are gone read exactly like an estate that still holds them.
#:
#: - `derivation-verified`: the period's own inputs are present and the
#:   seal was re-derived from them (ss11's four inputs).
#: - `attestation-verified`: seal-only. The inputs were archived under a
#:   named retention class, and what stands for the period is the
#:   attestation, accepted by PR-02e's CONSUMER rule -- its own digest, its
#:   binding to the seal it names, and its `chain_through_period`. NOT a
#:   recursive walk: the induction was established when the checkpoint was
#:   produced.
DERIVATION_VERIFIED: Final[str] = "derivation-verified"
ATTESTATION_VERIFIED: Final[str] = "attestation-verified"


def verify_archive_receipt(
    run_root: Path, period_id: int, *, licensing: Path | None = None
) -> ArchiveReceipt | None:
    """ss12a's receipt, PROVED -- the one door for every reader that treats
    a receipt as authority (DL-144).

    None means there is no receipt, which is a fact about most periods. A
    receipt that is there and does not prove out is not a fact and
    REFUSES: a reader that fell back to "no receipt" over a broken one
    would read a real archive as accidental loss, and one that fell back
    the other way would read loss as an archive.

    SIX bindings, and they are the reason this is one function rather than
    a check each caller writes for itself. Round one of DL-144 had five
    consumers agreeing on none of them, so a receipt with a forged
    attestation digest shortened `runs` output while the planner refused
    it:

    1. the receipt's own bytes and digest, its `retention_class` and its
       filename-vs-`period_id` -- `read_archive_receipt`'s door;
    2. the ESTATE: the receipt's `estate_id` is this root's SENTINEL's, so
       a stranger's receipt copied in excuses nothing here;
    3. the SEAL: `seal_digest` is the sidecar this root holds. The sidecar
       is a permanent floor, so it is always there to be asked;
    4. the ATTESTATION, by PR-02e's CONSUMER rule and not by a recursive
       walk -- `verify_attestation` exactly, because a seal-only period is
       the case that rule was written for;
    5. `attestation_digest` and `chain_through_period` are that
       checkpoint's. The receipt names the proof that LICENSED the
       deletion, and a different one beside it is a swapped proof;
    6. `licensing`, when the caller is excusing a specific absent file:
       the receipt has to name THAT path. A receipt whose list covers only
       a candidate pair does not excuse a missing WAL.
    """
    receipt = read_archive_receipt(run_root, period_id)
    if receipt is None:
        return None
    where = archive_receipt_path(run_root, period_id)
    sentinel = read_sentinel(run_root)
    if sentinel is None:
        raise EngineError(
            f"{where}: an archive receipt in a root with no `{SENTINEL_NAME}` sentinel"
            " -- nothing here says which lineage it belongs to (period-model ss1.1, ss12)"
        )
    if receipt.estate_id != sentinel.estate_id:
        raise EngineError(
            f"{where}: estate {receipt.estate_id} under a sentinel naming"
            f" {sentinel.estate_id} -- a stranger's receipt excuses nothing here"
            " (period-model ss12)"
        )
    if not seal_path(run_root, period_id).exists():
        raise EngineError(
            f"{seal_path(run_root, period_id)}: period {period_id} was archived"
            f" ({where.name}) and its sidecar is gone -- the receipt, the sidecar and"
            " the attestation are ONE permanent floor, and a period with neither"
            " inputs nor proof is loss (period-model ss12)"
        )
    seal = read_seal(run_root, period_id)
    if seal.estate_id != receipt.estate_id or seal.period_id != period_id:
        # `read_seal` parses a sidecar; it does not ask WHOSE. Without this
        # the whole chain below is self-consistent about the wrong estate:
        # drop a foreign seal and its matching attestation into this root,
        # restamp the receipt onto them, and every other binding here
        # agrees -- the receipt names that seal, that seal carries that
        # checkpoint, the checkpoint chains through this period. The one
        # thing none of them says is that the pair belongs to this lineage
        raise EngineError(
            f"{seal_path(run_root, period_id)}: attests period {seal.period_id} of"
            f" estate {seal.estate_id} where this receipt claims period {period_id} of"
            f" {receipt.estate_id} -- a foreign sidecar under this period's name, and"
            " a receipt restamped onto it excuses nothing (period-model ss1.2, ss12)"
        )
    if receipt.seal_digest != seal.digest:
        raise EngineError(
            f"{where}: stands on seal {receipt.seal_digest} and"
            f" {seal_path(run_root, period_id)} digests to {seal.digest} -- the receipt"
            " is not this boundary's (period-model ss12)"
        )
    try:
        attestation = verify_attestation(run_root, period_id)
    except EngineError as exc:
        # PR-02e's consumer rule is what stands in for the deleted inputs,
        # so its refusal is re-raised naming the ARCHIVE. "period N is not
        # attested" is true and sends an operator to `dsl41 audit`, which
        # for an archived period can never succeed -- the message has to
        # say that the period's only remaining proof is gone or broken
        raise EngineError(
            f"{where.name} archived period {period_id} and its attestation is gone or"
            f" does not hold together ({exc}) -- what stands in for the deleted inputs"
            " is that checkpoint, it may never be pruned, and no re-derivation can"
            " replace it (period-model ss12)"
        ) from exc
    if receipt.attestation_digest != attestation.digest:
        raise EngineError(
            f"{where}: stands on attestation {receipt.attestation_digest} and this root"
            f" holds {attestation.digest} -- the proof that stands in for the deleted"
            " inputs is not the one that licensed the deletion (period-model ss12)"
        )
    if receipt.chain_through_period != attestation.chain_through_period:
        raise EngineError(
            f"{where}: says the checkpoint chains through"
            f" {receipt.chain_through_period} and it chains through"
            f" {attestation.chain_through_period} -- the receipt and the proof it names"
            " disagree about how far the induction reached (period-model ss1.3, ss12)"
        )
    if licensing is not None and not receipt.licenses(run_root, licensing):
        raise EngineError(
            f"{licensing} is not there, and {where.name} does not license it"
            f" (it licensed {', '.join(receipt.archived)}) -- this is LOSS and not an"
            " archive: retention writes the receipt before it deletes anything,"
            " exactly so the two can be told apart (period-model ss12)"
        )
    return receipt


def verified_tier(run_root: Path, period_id: int) -> str:
    """Which of ss11's two tiers this period stands at (DL-144).

    THE RECEIPT DECIDES, not the disk. An archive is irreversible: files
    restored beside a receipt do not move a period back to
    `derivation-verified`, because the estate has already published the
    weaker claim and a tier that flickered with what happens to be on disk
    would be no claim at all. The restored inputs may still be READ -- a
    reader is free to look at them -- but the tier this function reports
    stays the receipt's.

    And the receipt has to PROVE OUT before it decides anything: reporting
    the weaker tier on the word of an artifact nobody checked would be the
    one place this design takes a claim on trust."""
    return (
        ATTESTATION_VERIFIED
        if verify_archive_receipt(run_root, period_id) is not None
        else DERIVATION_VERIFIED
    )


class Unattested(EngineError):
    """The checkpoint is written and durable; the registry row is not.

    Raised when `audit` cannot take the lineage lock -- which a live
    engine holds for its process lifetime. The `attestation` rides on the
    exception because it EXISTS: `verify` and `run --open-from` read the
    artifact, not the row, so the caller has a real checkpoint and one
    piece of bookkeeping left to do (ss1.3)."""

    def __init__(self, attestation: Attestation, reason: str) -> None:
        super().__init__(
            f"period {attestation.period_id} is attested at {attestation.digest} and the"
            f" registry row could not be set ({reason}): the checkpoint is durable and"
            " is what `verify` and `run --open-from` read -- re-run `dsl41 audit` when"
            " the lineage lock is free to set the row (period-model ss1.3)"
        )
        self.attestation = attestation


class Attestation(BaseModel):
    """ss11's durable proof: `seals/<period_id>.audit.json`.

    The `digest` key is not a field, on `Seal`'s rule and for its reason:
    it is a pure function of everything else, and a stored copy would be a
    second authority the artifact could disagree with itself about."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    artifact_format_version: int = ARTIFACT_FORMAT_VERSION
    seal_digest: str
    period_id: int = Field(ge=1)
    #: how far the induction reaches. Derived as `prev.chain_through_period
    #: + 1`, and equal to `period_id` on a chain with no gap -- which is
    #: what a consumer reads it to check (ss1.3)
    chain_through_period: int = Field(ge=1)
    #: null only on period 1, the base case of the induction
    prev_attestation_digest: str | None = None
    state_machine_version: int
    #: the interpreter that produced the proof. Diagnostic for the seal's
    #: identity and load-bearing for ss11's "auditing an old period runs
    #: the interpreter that produced it"
    dsl41_version: str
    #: ss3.2's spelling: naive UTC with exactly six fractional digits
    audited_at: str
    scope: Annotated[str, Field(pattern=r"^full$")] = FULL

    @model_validator(mode="after")
    def _artifact_invariants(self) -> Attestation:
        for name in (
            "artifact_format_version",
            "period_id",
            "chain_through_period",
            "state_machine_version",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} is {value!r}: an exact integer, never a coercion")
        if not is_hash_address(self.seal_digest):
            raise ValueError(f"seal_digest {self.seal_digest!r}: not a sha256 address")
        if (self.prev_attestation_digest is None) != (self.period_id == 1):
            raise ValueError(
                f"period {self.period_id} with prev_attestation_digest"
                f" {self.prev_attestation_digest!r}: null is period 1's base case and"
                " nothing else's (period-model ss1.3)"
            )
        if self.prev_attestation_digest is not None and not is_hash_address(
            self.prev_attestation_digest
        ):
            raise ValueError(
                f"prev_attestation_digest {self.prev_attestation_digest!r}: not a sha256 address"
            )
        if not self.dsl41_version:
            raise ValueError("dsl41_version is empty: the producing interpreter is load-bearing")
        check_stamp(self.audited_at, "audited_at")
        return self

    @property
    def digest(self) -> str:
        """`"sha256:" + hexdigest` over the canonical bytes with only the
        top-level `digest` key removed (ss3.2, PR-08b)."""
        return digest_over(self.model_dump(mode="json"))

    def to_bytes(self) -> bytes:
        return canonical_bytes(with_digest(self.model_dump(mode="json")))

    @classmethod
    def from_bytes(cls, data: bytes | str, *, where: str) -> Attestation:
        """Parse and CHECK: the canonical form, the version, and the
        artifact's own digest, in that order.

        A stamped digest that does not match the bytes it stamps is the
        one thing an attestation must never be read past -- the whole
        artifact exists to be a checkpoint somebody trusts."""
        try:
            payload = decode(data)
        except CanonError as exc:
            raise EngineError(f"{where}: not ss3.2-canonical JSON ({exc})") from exc
        if not isinstance(payload, dict):
            raise EngineError(f"{where}: not a JSON object")
        stamped = payload.pop("digest", None)
        version = payload.get("artifact_format_version")
        if version != ARTIFACT_FORMAT_VERSION:
            raise EngineError(
                f"{where}: artifact_format_version {version!r}: this binary implements"
                f" {ARTIFACT_FORMAT_VERSION} (PR-08d)"
            )
        try:
            attestation = cls.model_validate(payload)
        except ValidationError as exc:
            raise EngineError(f"{where}: not an attestation this binary can read ({exc})") from exc
        if stamped != attestation.digest:
            raise EngineError(
                f"{where}: stamped digest {stamped!r} but the bytes digest to"
                f" {attestation.digest} -- an attestation that disagrees with itself"
                " proves nothing (period-model ss11)"
            )
        if not is_canonical_file(data, attestation.to_bytes()):
            # a payload that omits a defaulted key (or otherwise differs
            # from the canonical serialization) still model-validates and
            # still digests right, because the digest is computed over the
            # FILLED model -- so the equality is what forbids a second
            # byte form for one logical artifact (ss3.2, Seal's own rule)
            raise EngineError(
                f"{where}: the file's bytes are not the attestation's canonical"
                " serialization -- one artifact has one byte form (period-model ss3.2)"
            )
        return attestation


def read_attestation(run_root: Path, period_id: int) -> Attestation | None:
    """The attestation for `period_id`, or None when this root holds none.

    Absence is a fact; a file that exists and does not parse is not."""
    path = attestation_path(run_root, period_id)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise EngineError(f"{path}: unreadable: {exc}") from exc
    return Attestation.from_bytes(raw, where=str(path))


def verify_attestation(run_root: Path, period_id: int) -> Attestation:
    """ss1.3's CONSUMER rule: accept attestation N alone.

    Three checks and no more: the artifact's own digest (in
    `from_bytes`), its binding to the seal it names, and the
    `chain_through_period` the producing audit established. Nothing here
    walks the chain -- the induction was proved when the attestation was
    produced, and a physical roll imports only the current seal and its
    attestation, so a consumer that re-walked would make a second roll
    impossible (PR-02e)."""
    attestation = read_attestation(run_root, period_id)
    if attestation is None:
        raise EngineError(
            f"{attestation_path(run_root, period_id)}: period {period_id} is not"
            " attested -- `dsl41 audit` produces the checkpoint (period-model ss1.3)"
        )
    if attestation.period_id != period_id:
        raise EngineError(
            f"{attestation_path(run_root, period_id)}: attests period"
            f" {attestation.period_id}, not {period_id} -- an attestation filed under"
            " another period's name (period-model ss1.3)"
        )
    seal = read_seal(run_root, period_id)
    if attestation.seal_digest != seal.digest:
        raise EngineError(
            f"{attestation_path(run_root, period_id)}: attests seal"
            f" {attestation.seal_digest} and {seal_path(run_root, period_id)} digests to"
            f" {seal.digest} -- the checkpoint is not this boundary's (PR-02d)"
        )
    if attestation.chain_through_period != period_id:
        raise EngineError(
            f"{attestation_path(run_root, period_id)}: chains through period"
            f" {attestation.chain_through_period} while attesting {period_id} -- the"
            " induction has a gap the checkpoint claims to cover (period-model ss1.3)"
        )
    return attestation


def prove_derived(run_root: Path, period_id: int, *, stored: Seal | None = None) -> Seal:
    """ss11's "verified means re-derived", as a GATE rather than as a step
    of one verb.

    A sidecar whose digest matches its own canonical form proves INTEGRITY,
    never DERIVATION: rewrite the artifact canonically, recompute its
    digest, and copy that digest into the `seal` record and the successor's
    opening, and every binding check in the tree still passes. The only
    thing that does not is the period's own evidence, and this is where it
    is asked.

    ONE implementation and ONE refusal, because it is one question (DL-139,
    DL-142). `audit` asks it before it writes a checkpoint; `dsl41
    journal`'s cross-period replay asks it before it crosses a boundary,
    for the reason a diagnosis surface has to: a narrated continuation over
    a correlated forgery reads exactly like a true one.

    `rederive_seal` does the work and the record/sidecar agreement check
    (`check_record_names_sidecar`) on the way -- so a rewritten `seal`
    RECORD over an honest sidecar refuses here too, and earlier, naming the
    fields."""
    if stored is None:
        stored = read_seal(run_root, period_id)
    rederived = rederive_seal(run_root, period_id, stored=stored)
    if rederived.digest != stored.digest:
        raise EngineError(
            f"{seal_path(run_root, period_id)} does not re-derive"
            f" ({'; '.join(_diff(rederived, stored))}): a sidecar whose digest matches"
            " its own canonical form proves integrity, not derivation -- and this one"
            " is not what the period's own evidence produces (period-model ss11)"
        )
    return stored


def audit_period(
    run_root: Path, period_id: int, *, anchor: EstateAnchor | None = None
) -> Attestation:
    """ss1.3's PRODUCER rule: re-derive period N and write its checkpoint.

    Producing N requires the PREDECESSOR attestation **present and
    verified**; period 1
    is the base case with `prev_attestation_digest: null`. The absence of
    an "or re-derive everything below" alternative is the rule, not an
    omission -- without it a wrong implementation checks only its own
    digest and seal binding, emits a checkpoint over an unaudited opening
    seal, and earlier roots get deleted on a chain that was never
    established (PR-02e).

    The `attested` transition is this verb's: `audit.json` lands by the
    liturgy first, and only then does the anchor row flip, so no state has
    an `attested` row without the artifact that justifies it."""
    stored = read_seal(run_root, period_id)
    # PROVED, not read: this receipt decides whether the loss branch below
    # is skipped and an attestation returned, so `audit` may least of all
    # take one on trust. `verified_tier` refusing first in the CLI is not
    # a guard -- it is another caller, and a library function that is only
    # safe behind one of its callers is not safe (DL-144)
    segment = wal_path(run_root, period_id)
    receipt = verify_archive_receipt(
        run_root, period_id, licensing=None if segment.exists() else segment
    )
    if receipt is None and not segment.exists():
        # NOT re-derivable, and this is decided BEFORE the idempotent
        # early-return below: a stored attestation over inputs that are
        # gone would otherwise be reported as derivation-verified by a run
        # that derived nothing. Which absence it is depends on who wrote
        # the period, and only one of the two is a fault (DL-144)
        if wrote_period(run_root, period_id):
            raise EngineError(
                f"{wal_path(run_root, period_id)}: this root ran period {period_id}"
                " and its segment is not here. No archive receipt licenses the"
                f" absence (`{archive_receipt_path(run_root, period_id).name}`), so"
                " this is LOSS and not an archive -- retention writes the receipt"
                " before it deletes anything (period-model ss11, ss12)"
            )
        raise EngineError(
            f"{wal_path(run_root, period_id)}: period {period_id}'s WAL is not in"
            " this root -- audit re-derives from the period's own evidence, and an"
            " imported seal carries none of it. Verify its attestation instead"
            f" (`dsl41 verify --run-root {run_root} --period {period_id}`), or audit"
            " in the root the registry names (period-model ss1.3, ss11)"
        )
    if stored.state_machine_version != STATE_MACHINE_VERSION:
        # ss11: auditing an old period runs the interpreter that produced
        # it. Cross-version audit inside one binary is a non-goal, and the
        # refusal names what to install (PR-47a)
        raise EngineError(
            f"period {period_id} ran state_machine_version"
            f" {stored.state_machine_version} and this binary implements"
            f" {STATE_MACHINE_VERSION}: audit runs the interpreter that produced the"
            f" period -- install dsl41 {_producing_version(run_root, period_id)} and"
            " audit with it (period-model ss11, PR-47a)"
        )
    existing = read_attestation(run_root, period_id)
    if existing is not None:
        # IDEMPOTENT, not re-produced: a later attestation records this
        # one's digest as `prev_attestation_digest`, and a rewrite with a
        # fresh `audited_at` would silently re-digest the checkpoint that
        # link names. A stored attestation that verifies IS the checkpoint;
        # one that does not verify refuses loudly inside verify rather
        # than being papered over by a rewrite. The ANCHOR transition still
        # runs: a crash between the durable artifact and the `attested` CAS
        # leaves the row unflipped, and a retry that returned early would
        # leave it unflipped forever (ss1.3)
        # the checkpoint may be another process's crash window -- linked,
        # not yet fsynced -- and a durable `attested` row over an
        # undurable file survives a power cut the file does not
        make_durable(str(attestation_path(run_root, period_id)))
        verified = verify_attestation(run_root, period_id)
        _flip_attested(run_root, period_id, anchor, verified)
        return verified
    previous: Attestation | None = None
    if period_id > 1:
        previous = verify_attestation(run_root, period_id - 1)
    prove_derived(run_root, period_id, stored=stored)
    # the induction, WRITTEN DOWN: one more than the predecessor reaches,
    # and 1 at the base case. It is `period_id` on a chain with no gap, and
    # `verify_attestation` above is what proved the predecessor had none --
    # so this is derived here and CHECKED there, in the one place a
    # consumer reads it (ss1.3)
    chain_through = 1 if previous is None else previous.chain_through_period + 1
    attestation = Attestation(
        seal_digest=stored.digest,
        period_id=period_id,
        chain_through_period=chain_through,
        prev_attestation_digest=None if previous is None else previous.digest,
        state_machine_version=stored.state_machine_version,
        dsl41_version=dsl41_version(),
        audited_at=datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="microseconds"),
    )
    try:
        durable_create(str(attestation_path(run_root, period_id)), attestation.to_bytes())
    except FileExistsError:
        # a concurrent audit won the create: two racers would otherwise
        # publish two digests for one period (`audited_at` differs), and a
        # physical roll could import one while the closing root keeps the
        # other -- a forked attestation chain. The winner IS the
        # checkpoint; the loser verifies it and finishes the CAS over it.
        winner = verify_attestation(run_root, period_id)
        _flip_attested(run_root, period_id, anchor, winner)
        return winner
    _flip_attested(run_root, period_id, anchor, attestation)
    return attestation


def _flip_attested(
    run_root: Path, period_id: int, anchor: EstateAnchor | None, attestation: Attestation
) -> None:
    """The `attested` CAS, after the artifact is durable (ss1.3).

    The artifact is durable BEFORE the row that points at it: a crash
    between the two leaves a re-runnable audit, and the reverse leaves an
    `attested` period with no proof. The lock is taken HERE and for this
    one write, not around the re-derivation: the lineage lock is held for
    the process lifetime of whoever leads the lineage, so an audit that
    held it for the whole verb could never run while a later period was
    live -- and auditing a CLOSED period while a later one runs is exactly
    what retention needs. The ARTIFACT is the proof that `verify` and
    `run --open-from` read; the registry row is bookkeeping, and a caller
    that could not take the lock is told so rather than losing the
    checkpoint."""
    if anchor is None:
        return
    try:
        anchor.acquire()
    except EngineError as exc:
        raise Unattested(attestation, str(exc)) from exc
    try:
        seal = read_seal(run_root, period_id)
        anchor.attest(
            period_id,
            estate_id=seal.estate_id,
            root=run_root,
            seal_digest=attestation.seal_digest,
        )
    finally:
        anchor.release()


def _producing_version(run_root: Path, period_id: int) -> str:
    """The `dsl41_version` that produced this period, for the PR-47a
    refusal: the attestation's if there is one, else the newest `leader`
    record's. Diagnostic only -- nothing decides on it."""
    existing = None
    try:
        existing = read_attestation(run_root, period_id)
    except EngineError:
        existing = None
    if existing is not None:
        return existing.dsl41_version
    try:
        records = read_journal(wal_path(run_root, period_id))
    except EngineError:
        return "the version named in the period's `leader` records"
    versions = [
        str(record["dsl41_version"])
        for record in records
        if record.get("rec") == "leader" and record.get("dsl41_version")
    ]
    return versions[-1] if versions else "the version named in the period's `leader` records"


# ------------------------------------------------------- re-derivation


def rederive_seal(run_root: Path, period_id: int, *, stored: Seal | None = None) -> Seal:
    """ss11's "verified means re-derived": rebuild period N's sidecar from
    the period's own evidence.

    The inputs are exactly ss11's: the opening seal, the complete ordered
    WAL of the period, the immutable spool evidence, and the C1 and C2
    manifests. FOUR, not five: the sentinel left with DL-138, which reduced
    `boundary_request.source` to one derived value. Nothing is copied out of the stored
    sidecar except the three `boundary_request` input scalars the spec
    exempts, and those are read from the `seal` RECORD and checked against
    the sidecar, never taken from one alone."""
    path = wal_path(run_root, period_id)
    if not path.exists():
        # a ROLLED root holds the seal it opened from and none of that
        # period's evidence, by design: re-deriving C1 there would need
        # C1's whole proof set, and importing that on every roll is
        # retention policy rather than a boundary mechanism. What the root
        # CAN do with an imported checkpoint is `verify` it (ss1.3)
        if verify_archive_receipt(run_root, period_id, licensing=path) is not None:
            # ARCHIVED, not absent: the inputs went under a named retention
            # class and the receipt says so. Two different facts about one
            # missing file, and an operator meeting the roll's wording here
            # would go looking for a root that does not exist (DL-144)
            raise EngineError(
                f"{path}: period {period_id}'s inputs were ARCHIVED"
                f" ({archive_receipt_path(run_root, period_id).name}) -- what stands"
                " for this period is its attestation, at the attestation-verified"
                " tier, and re-derivation is over for it by policy. Verify it instead"
                f" (`dsl41 verify --run-root {run_root} --period {period_id}`)"
                " (period-model ss11, ss12)"
            )
        raise EngineError(
            f"{path}: period {period_id}'s WAL is not in this root -- audit"
            " re-derives from the period's own evidence, and an imported seal"
            " carries none of it. Verify its attestation instead"
            f" (`dsl41 verify --run-root {run_root} --period {period_id}`), or audit"
            " in the root the registry names (period-model ss1.3, ss11)"
        )
    records = read_journal(path)
    opening = records[0]
    # `read_journal` runs ss2.1's segment schema over the opening record,
    # so every field indexed below is present and of its exact type -- and
    # since DL-138 it also guarantees the opening IS a `segment`: a retired
    # `header` is refused BY NAME one frame earlier, by the reader that owns
    # the record registry. A second check here could only say "not a
    # segment", which merges the retired case with the unknown one and is
    # exactly what the tombstone rule forbids. The `seal` record gets the
    # same treatment where it is selected, and both refuse as `EngineError`
    # -- a KeyError traceback out of a CLI verb that catches refusals is a
    # defect, not a diagnosis
    committed = [record for record in records if record.get("rec") == "seal"]
    if not committed:
        raise EngineError(
            f"{wal_path(run_root, period_id)}: no `seal` record -- period {period_id}"
            " is still open and an open period has no boundary to attest"
            " (period-model ss11)"
        )
    record = committed[-1]
    check_seal_record(record)
    if stored is not None:
        # the WAL's `seal` record and the stored sidecar must name each
        # other BEFORE re-derivation: a rewritten record over an untouched
        # sidecar would otherwise be ignored, and audit would attest a
        # seal the WAL does not name (ss2.2/ss11)
        check_record_names_sidecar(stored, record, run_root)
    closing = read_period_manifest(run_root, period_id)
    if closing is None:
        raise EngineError(
            f"{run_root}: periods/{period_id:06d}/manifest.json is not there -- audit"
            " re-derives from the C1 and C2 manifests and cannot invent one"
            " (period-model ss11)"
        )
    # the manifest and the segment are ONE object written twice (PR-22):
    # a closed segment whose pins were rewritten under a self-consistent
    # manifest must refuse before either is used as evidence
    check_manifest_against_segment(closing, opening)
    watch_prefix: dict[tuple[str, int], int] | None = None
    successor_wal = wal_path(run_root, period_id + 1)
    if successor_wal.exists():
        link = read_journal(successor_wal)[0].get("opens_from_seal")
        if stored is not None:
            if not (
                isinstance(link, dict)
                and link.get("period_id") == period_id
                and link.get("digest") == stored.digest
            ):
                # the successor's opening is the INDEPENDENT artifact that
                # pins the sidecar: a re-forged sidecar under an honest
                # successor disagrees here, before any evidence is folded
                raise EngineError(
                    f"{successor_wal}: opens from {link!r}, not from"
                    f" {stored.digest} -- the sidecar being audited is not the one"
                    " this lineage opened its successor from (period-model ss11)"
                )
            watch_prefix = {
                (entry.job, entry.run_number): entry.watch_seq
                for entry in stored.executions
                if entry.kind == "fw_watch"
            }
    c1 = load_bundle_catalog(run_root, closing.source_bundle_hash)
    carried = carried_from_opening(run_root, opening, closing)
    oracle = Oracle(c1, carried=carried)
    seed_local_executor(oracle.store, LOCAL_EXECUTOR_ID, at=opening_at(opening))
    replay = replay_inputs(
        oracle,
        records,
        outbox=carried_outbox(_opened_runtime(run_root, opening, closing), at=opening_at(opening)),
    )
    at = replay.frontiers.at
    if at is None:
        raise EngineError(
            f"{wal_path(run_root, period_id)}: the segment admitted no input, so it"
            " holds no cutoff instant -- every boundary advances through T"
            " (period-model ss6)"
        )
    boundary_request = _boundary_request(record)
    # C2's own committed manifest is where the staged half of the opening
    # comes back from: the `seal` RECORD carries only `next_period_id` and
    # `next_baseline_id`, and ss11 names the C2 manifest as an audit input
    # for exactly this. The five engine-derived fields are re-derived below
    # by `staged.commit`, never read.
    opening_manifest = _opening_manifest(run_root, period_id + 1)
    # ONE projection (DL-137, DL-145): `Manifest` IS a `StagedManifest`, so
    # the owner's derivation reads the staged half back off it and a field
    # added to `StagedNextPeriod` crosses by default
    staged = staged_next_from(opening_manifest)
    fingerprint = seal_fingerprint(
        source=boundary_request.source,
        baseline_id=closing.baseline_id,
        epoch=next_epoch(records) - 1,
        next_period=staged,
        force_seal=boundary_request.force_seal,
        claimed_actor=boundary_request.claimed_actor or None,
    )
    executing = executing_jobs(replay.outbox, oracle.store.job)
    post_barrier = carried_from_oracle(
        oracle,
        now=at,
        pending_spawn=[job for job, state in executing.items() if state == "pending"],
        bound=[job for job, state in executing.items() if state == "applied"],
    )
    return close_runtime(
        closing=closing,
        estate_id=str(opening["estate_id"]),
        epoch=next_epoch(records) - 1,
        prev_seal_digest=_opens_from(opening),
        closes_at_index=replay.frontiers.applied_index,
        closed_at=at,
        scheduler_admitted_through=at,
        state=SealedState(
            jobs=dict(oracle.store.job),
            globals=dict(oracle.store.globals_),
            hosts={host_id: SealedHost.of(row) for host_id, row in oracle.store.hosts.items()},
            routes=implicit_routes(LOCAL_EXECUTOR_ID),
            timers=tuple(oracle.store.timers()),
            timer_seq=oracle.store.timer_seq,
            consumed=dict(oracle.store.consumed),
            enqueue_counter=oracle.store.enqueue_counter,
            now=at,
        ),
        outbox_pending=tuple(replay.outbox.pending()),
        executions=executions_at(
            run_root=run_root,
            outbox=replay.outbox,
            rows=oracle.store.job,
            catalog=c1,
            interval_default=max(
                1, round(closing.runtime_profile.fw_default_interval_us / 1_000_000)
            ),
            # a live watch that crossed the boundary keeps appending to
            # the SAME file in the next period; the closed period's
            # evidence is the positional prefix its sidecar names, and the
            # sidecar was pinned above by the successor segment's
            # `opens_from_seal` -- an artifact INDEPENDENT of the one being
            # re-derived. No successor segment means no successor ever
            # opened, so the whole log is this period's.
            watch_prefix=watch_prefix,
        ),
        classification=classify(
            closing=Baseline(catalog=c1, profile=closing.runtime_profile),
            opening=Baseline(
                catalog=load_bundle_catalog(run_root, staged.source_bundle_hash),
                profile=opening_manifest.runtime_profile,
            ),
            carried=post_barrier,
        ),
        staged=staged,
        boundary_request=boundary_request,
        request_fingerprint=fingerprint,
        forced_gate=retry_horizon_gate(
            records,
            horizon_us=closing.runtime_profile.retry_horizon_us,
            at=at,
            force_seal=boundary_request.force_seal,
        ),
    )


def _opens_from(opening: Mapping[str, Any]) -> str | None:
    link = opening.get("opens_from_seal")
    return str(link["digest"]) if isinstance(link, Mapping) else None


def _opening_manifest(run_root: Path, next_period_id: int) -> Manifest:
    """C2's committed manifest -- ss11's fourth audit input.

    The staged identity the boundary committed and the runtime profile the
    committed classification was taken against both live here, and a
    boundary whose opening manifest was pruned cannot be re-derived: the
    retention floor forbids pruning it, and audit does not invent one."""
    manifest = read_period_manifest(run_root, next_period_id)
    if manifest is None:
        raise EngineError(
            f"{run_root}: periods/{next_period_id:06d}/manifest.json is not there --"
            " audit re-derives the opening from the C2 manifest, and the boundary's"
            " own artifacts may never be pruned (period-model ss11, ss12)"
        )
    return manifest


def carried_from_opening(
    run_root: Path, opening: Mapping[str, Any], closing: Manifest
) -> CarriedRows | None:
    """The rows period N opened with: the predecessor seal's, installed
    verbatim, or None for period 1, which opened from a catalog.

    Public because `runner_history` needs the same rows for the same
    reason: a replay of period N from an EMPTY oracle derives revisions and
    run numbers the log never recorded, and refuses on the first admitted
    input that touches a carried entity (DL-136). One derivation, so audit
    and run history cannot disagree about what a period opened with."""
    opened = _opened_runtime(run_root, opening, closing)
    if opened is None:
        return None
    return opened.carried_rows  # the ONE derivation (seal.py, DL-137)


def _opened_runtime(run_root: Path, opening: Mapping[str, Any], closing: Manifest) -> Any:
    link = opening.get("opens_from_seal")
    if not isinstance(link, Mapping):
        return None
    return open_from_seal(
        read_seal(run_root, int(link["period_id"])),
        expected_digest=str(link["digest"]),
        manifest=closing,
    )


def _boundary_request(record: Mapping[str, Any]) -> BoundaryRequest:
    """The boundary's three carried scalars, and its one derived one.

    `source` is DERIVED, never read (PR-47b). Since DL-138 it derives to
    `request` for every boundary this estate can hold -- `adopt` went with
    the estate-adoption path -- and the comparison STAYS: deriving over a
    one-value domain is still what catches a `seal` record whose `source`
    was rewritten, and the day a second value returns the check is already
    where it belongs."""
    source: Literal["request"] = "request"
    if record.get("source") != source:
        raise EngineError(
            f"the `seal` record says source {record.get('source')!r} and this period"
            f" derives {source!r}: `source` is audit's to derive, never to read"
            " (period-model ss11, PR-47b)"
        )
    return BoundaryRequest(
        source=source,
        request_id=str(record["request_id"]),
        claimed_actor=str(record.get("claimed_actor", "")),
        force_seal=bool(record.get("force_seal", False)),
    )


def _diff(rederived: Seal, stored: Seal) -> list[str]:
    """Which top-level fields disagree -- the refusal's whole value.

    "The digests differ" tells an operator nothing they can act on; the
    field names tell them whether they are looking at a pruned spool, a
    tampered artifact or a version skew."""
    left, right = rederived.to_payload(), stored.to_payload()
    return [
        f"{field}: re-derived {mine!r} vs stored {theirs!r}"
        for field, mine, theirs in disagreements(left, right, sorted(set(left) | set(right)))
    ] or ["the canonical forms differ"]
