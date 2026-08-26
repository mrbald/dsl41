"""`dsl41 journal` across a boundary (period-model ss11; DL-142).

ss11 says replay across periods "walks segments and switches catalogs at
each `segment` record". DL-136 named that a unit of its own and DL-141
printed a stop where the boundary was; this is the unit, and these are the
claims it makes:

* the state FOLDS through the seal -- period 2 sees period 1's rows, which
  the trace shows as the FROM half of a transition (`j1 SUCCESS->STARTING`,
  not `INACTIVE->STARTING`);
* the CATALOG switches -- period 2 runs a job that exists only in C2, so a
  replay that kept C1 could not narrate it at all;
* a boundary is crossed only over a seal that proves out, and every way it
  can fail to refuses BY NAME.

The estate is built by the real machinery -- a real subprocess run, the
offline `seal` verb, a real resume -- and every id is read back off it.
Nothing is written by hand except where the artifact under test IS the
corruption, which cannot be produced any other way.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from typer.testing import CliRunner

from dsl41.boundary import default_anchor_dir
from dsl41.canon import canonical_bytes
from dsl41.cli import app
from dsl41.period import (
    SourceFile,
    bundle_dir,
    read_period_manifest,
    seal_path,
    wal_path,
    write_bundle,
)
from dsl41.runner_journal import read_journal
from dsl41.seal import Seal

from test_run_history import (  # noqa: F401  (shared by design)
    _resume_real,
    _run_real_and_manifest,
)

MACHINE = "insert_machine: m1\ntype: a\nnode_name: localhost\n\n"
C1_JIL = MACHINE + "insert_job: j1\njob_type: c\ncommand: exit 0\nmachine: m1\n"
#: j2 exists ONLY here. It is the whole catalog-switch pin: a replay that
#: kept period 1's catalog across the boundary would meet a STARTJOB for a
#: job its catalog does not define.
C2_JIL = C1_JIL + "\ninsert_job: j2\njob_type: c\ncommand: exit 0\nmachine: m1\n"
#: and j3 only here, so a THREE-period read has a second boundary whose
#: catalog switch is visible in its own right
C3_JIL = C2_JIL + "\ninsert_job: j3\njob_type: c\ncommand: exit 0\nmachine: m1\n"

runner = CliRunner()


def _invoke(*args: str):
    return runner.invoke(app, list(args), catch_exceptions=False)


def _two_periods(tmp_path: Path) -> tuple[Path, Path, Path]:
    """One root, two periods, in place: j1 runs in period 1 under C1, the
    `seal` verb closes it onto C2, and the resumed period 2 runs j1 AGAIN
    and j2 for the first time.

    j1 in both periods is the carry pin and j2 is the catalog pin, and one
    fixture holds both because they are one boundary."""
    c1, c2 = tmp_path / "c1.jil", tmp_path / "c2.jil"
    c1.write_text(C1_JIL)
    c2.write_text(C2_JIL)
    run_root = tmp_path / "run"
    asyncio.run(_run_real_and_manifest(C1_JIL, run_root, ["j1"], file=str(c1)))
    sealed = _invoke("seal", "--run-root", str(run_root), "--next", str(c2))
    assert sealed.exit_code == 0, sealed.output
    asyncio.run(_resume_real(c2, run_root, ["j1", "j2"]))
    return run_root, c1, c2


def _three_periods(tmp_path: Path) -> tuple[Path, Path]:
    """The same root, sealed twice: j1 in period 1 under C1, j1+j2 in
    period 2 under C2, j3 in period 3 under C3.

    Two boundaries, because a loop that crosses ONE is not shown to carry
    its state and its catalog rule forward past the first: period 3's
    catalog must come from period 3's bundle and not from period 1's
    supplied files, and only a second crossing can say so."""
    run_root, _c1, c2 = _two_periods(tmp_path)
    c3 = tmp_path / "c3.jil"
    c3.write_text(C3_JIL)
    sealed = _invoke("seal", "--run-root", str(run_root), "--next", str(c3))
    assert sealed.exit_code == 0, sealed.output
    asyncio.run(_resume_real(c3, run_root, ["j3"]))
    return run_root, c2


def honest_digest(run_root: Path) -> str:
    """Period 1's digest as the estate's own evidence produces it, read off
    the re-derivation rather than off the artifact under test."""
    from dsl41.attest import rederive_seal

    return rederive_seal(run_root, 1).digest


def _rewrite(segment: Path, records: list[dict]) -> None:
    segment.write_bytes(b"".join(canonical_bytes(record) + b"\n" for record in records))


# ------------------------------------------------- crossing the boundary


def test_dl142_replay_crosses_a_boundary_folding_state_and_switching_catalogs(
    tmp_path: Path,
) -> None:
    """The unit's whole claim, in one trace.

    Two facts are asserted and neither is visible without the other half
    of the fix. `j1 SUCCESS->STARTING` in period 2 is the CARRY: the FROM
    status is period 1's, folded through the seal by the same
    `open_from_seal` an engine opens with, and an oracle built empty would
    print `INACTIVE->STARTING`. `j2` running at all is the CATALOG: it is
    defined only in C2, so a replay still holding C1 could not have
    narrated it.

    The boundary itself is NARRATED. A trace that silently concatenated
    two periods would read as one long period, and the index the older one
    closed at is what an operator matches against the seal."""
    run_root, c1, _c2 = _two_periods(tmp_path)

    out = _invoke("journal", str(run_root), str(c1))
    assert out.exit_code == 0, out.output
    lines = out.output.splitlines()

    # period 1, under C1
    assert "j1 INACTIVE->STARTING [STARTJOB event (control)]" in lines[0]
    boundary = [index for index, line in enumerate(lines) if line.startswith("period 1 sealed")]
    assert len(boundary) == 1, lines
    seal_record = read_journal(wal_path(run_root, 1))[-1]
    assert seal_record["rec"] == "seal"
    assert lines[boundary[0]] == (
        f"period 1 sealed at index {seal_record['closes_at_index']}; period 2 opens in {run_root}"
    )
    after = lines[boundary[0] + 1 :]
    # the CARRY: period 1's status is the FROM half of period 2's start
    assert any("j1 SUCCESS->STARTING" in line for line in after), after
    assert not any("j1 INACTIVE->STARTING" in line for line in after), after
    # the CATALOG: j2 is C2's alone
    assert any("j2 INACTIVE->STARTING" in line for line in after), after
    assert not any("j2" in line for line in lines[: boundary[0]]), lines
    # DL-141's stop is gone: the replay continues instead of naming a command
    assert "DL-136" not in out.output and "the replay stops" not in out.output


def test_dl142_the_supplied_catalog_gates_the_first_period_and_never_the_rest(
    tmp_path: Path,
) -> None:
    """The catalog-argument ruling, both halves.

    Files GIVEN are the FIRST replayed period's catalog and are hash-gated
    against its pin exactly as before; period 2 still comes from its own
    bundle, which is what `j2` proves -- the supplied C1 did not leak
    across the boundary. Files OMITTED, every period including the first
    comes from its bundle: the estate has held its own inputs since DL-130
    and a bundle re-parses under the ORIGINAL paths `sources.json` records,
    so it reproduces the very hash the segment pins.

    Both invocations must produce the SAME trace. A catalog argument that
    changed what the replay narrated would be a second authority over an
    estate that already pins its own."""
    run_root, c1, _c2 = _two_periods(tmp_path)

    given = _invoke("journal", str(run_root), str(c1))
    omitted = _invoke("journal", str(run_root))
    assert given.exit_code == 0 and omitted.exit_code == 0, (given.output, omitted.output)
    assert given.output == omitted.output
    assert "j2 INACTIVE->STARTING" in given.output  # C1 was given and C2 still ruled


def test_dl142_a_later_segment_named_alone_opens_from_its_attested_seal(
    tmp_path: Path,
) -> None:
    """The command DL-141 printed, made to work -- over an ATTESTED
    predecessor, which is the only thing that can stand for it here.

    A later segment replayed from an EMPTY oracle derives revisions and run
    numbers the log never recorded (DL-136 (5a)), so `dsl41 journal
    <wal/000002.jsonl>` was already unreplayable on any estate whose second
    period touched a carried job. Naming ONE segment still replays exactly
    one; it now opens that segment from the seal it names, and the carry is
    visible as the FROM half of period 2's first transition."""
    run_root, _c1, c2 = _two_periods(tmp_path)
    assert _invoke("audit", "--run-root", str(run_root), "--period", "1").exit_code == 0

    out = _invoke("journal", str(wal_path(run_root, 2)), str(c2))
    assert out.exit_code == 0, out.output
    assert "j1 SUCCESS->STARTING" in out.output  # the carry, from the sidecar
    assert "j1 INACTIVE->" not in out.output
    assert "period 1 sealed" not in out.output  # one segment: no boundary to narrate


def test_dl142_a_later_segment_named_alone_refuses_an_unattested_predecessor(
    tmp_path: Path,
) -> None:
    """The hole the "no attestation needed" rationale left open.

    Crossing without an attestation is licensed by the replay READING the
    period's inputs -- ss11's refusal is for a seal "whose period inputs
    are corrupt or pruned", and a lineage walk holds them. Name a later
    segment ALONE and that sentence is simply false: nothing re-derives the
    predecessor seal, so integrity is all that is left and integrity is
    what a correlated forgery has. The attestation is what ss11 puts in its
    place, and its absence is named rather than assumed away."""
    run_root, _c1, c2 = _two_periods(tmp_path)

    out = _invoke("journal", str(wal_path(run_root, 2)), str(c2))
    assert out.exit_code == 2
    assert "period 1 is not attested" in out.output
    assert "dsl41 audit" in out.output  # what to run
    assert "STARTING" not in out.output


def test_dl142_naming_one_segment_replays_exactly_that_period(tmp_path: Path) -> None:
    """Single-segment regression: `wal/000001.jsonl` is period 1 and
    nothing else -- no continuation, no second catalog, no boundary line.
    The root ARGUMENT is what widened; the segment argument did not."""
    run_root, c1, _c2 = _two_periods(tmp_path)

    out = _invoke("journal", str(wal_path(run_root, 1)), str(c1))
    assert out.exit_code == 0, out.output
    assert "j1 INACTIVE->STARTING" in out.output
    assert "j2" not in out.output
    assert not any(
        "sealed at index" in line or line.startswith("period ") for line in out.output.splitlines()
    )  # no boundary line and no per-period labelling on a single segment


def test_dl142_a_three_period_read_crosses_both_boundaries(tmp_path: Path) -> None:
    """The loop carries forward, not just across one seam.

    Period 3's `j3` exists only in C3, so it is narrated only if the SECOND
    boundary also switched catalogs -- and the supplied files, which gate
    period 1 alone, reached neither. Both crossings are announced, in
    order, each naming the index its own seal closed at."""
    run_root, _c2 = _three_periods(tmp_path)
    c1 = tmp_path / "c1.jil"

    out = _invoke("journal", str(run_root), str(c1))
    assert out.exit_code == 0, out.output
    lines = out.output.splitlines()
    crossings = [line for line in lines if "sealed at index" in line]
    closes = [read_journal(wal_path(run_root, period))[-1] for period in (1, 2)]
    assert crossings == [
        f"period 1 sealed at index {closes[0]['closes_at_index']}; period 2 opens in {run_root}",
        f"period 2 sealed at index {closes[1]['closes_at_index']}; period 3 opens in {run_root}",
    ]
    # each period's own catalog ruled, in its own place in the stream
    second = lines.index(crossings[0])
    third = lines.index(crossings[1])
    assert any("j2 INACTIVE->STARTING" in line for line in lines[second:third])
    assert any("j3 INACTIVE->STARTING" in line for line in lines[third:])
    assert not any("j3" in line for line in lines[:third])


def test_dl142_an_estate_wide_read_of_one_period_still_names_it(tmp_path: Path) -> None:
    """A lineage that has not rolled yet is still an estate, and its
    answers name the period they are about. Labelling driven by "more than
    one segment" would have dropped the prefix exactly on the smallest
    estate there is."""
    c1 = tmp_path / "c1.jil"
    c1.write_text(C1_JIL)
    c2 = tmp_path / "c2.jil"
    c2.write_text(C2_JIL)
    run_root = tmp_path / "run"
    asyncio.run(_run_real_and_manifest(C1_JIL, run_root, ["j1"], file=str(c1)))
    anchor = default_anchor_dir(run_root)

    out = _invoke("journal", str(anchor), str(c2))
    assert out.exit_code == 2
    assert f"period 1 in {run_root.resolve()}: catalog hash mismatch" in out.output


# --------------------------------------------------------- the refusals


def test_dl142_a_refused_crossing_is_never_announced_as_a_crossing(tmp_path: Path) -> None:
    """The line says a boundary WAS crossed, so it may not print until one
    was. Printed before the period is opened, it stated as fact the very
    crossing the refusal on the next line denied -- and an operator reading
    a truncated stream would take the last line they saw as the last thing
    that happened.

    The 1/2 crossing really happened and stays; the 2/3 one did not and is
    absent."""
    run_root, _c2 = _three_periods(tmp_path)
    seal_path(run_root, 2).unlink()

    out = _invoke("journal", str(run_root))
    assert out.exit_code == 2
    crossings = [line for line in out.output.splitlines() if "sealed at index" in line]
    assert len(crossings) == 1 and crossings[0].startswith("period 1 sealed")
    assert "j2 INACTIVE->STARTING" in out.output  # period 2 was reached and narrated
    assert "j3" not in out.output


def test_dl142_refuses_a_root_whose_segments_all_name_a_stranger(tmp_path: Path) -> None:
    """A whole root rewritten to another estate's id AGREES WITH ITSELF at
    every boundary, so adjacency cannot see it: the sentinel is the only
    thing left that says whose lineage this is (ss1.2).

    That is the check the subscriber's backfill already made and this verb
    did not, which mattered the moment a root argument stopped meaning one
    segment. Refused at the FIRST segment, before any trace is printed --
    a stranger's transitions narrated under this root's name is the forged
    continuation in another spelling."""
    run_root, c1, _c2 = _two_periods(tmp_path)
    for period in (1, 2):
        segment = wal_path(run_root, period)
        records = read_journal(segment)
        records = [
            {**record, "estate_id": "e-stranger"} if "estate_id" in record else record
            for record in records
        ]
        _rewrite(segment, records)

    out = _invoke("journal", str(run_root), str(c1))
    assert out.exit_code == 2
    assert "a stranger's segment under this estate's name" in out.output
    assert "STARTING" not in out.output


def test_dl142_refuses_a_boundary_whose_chain_is_broken(tmp_path: Path) -> None:
    """ss11: `segment` pins != the preceding seal's -- a spliced lineage.

    The link is rewritten in the OPENING alone, so every per-record schema
    still passes and only the adjacency of the two segments disagrees. A
    read-only replay that crossed it would narrate period 2 as the
    continuation of a seal it never opened from."""
    run_root, c1, _c2 = _two_periods(tmp_path)
    segment = wal_path(run_root, 2)
    records = read_journal(segment)
    records[0] = {
        **records[0],
        "opens_from_seal": {**records[0]["opens_from_seal"], "digest": "sha256:" + "9" * 64},
    }
    _rewrite(segment, records)

    out = _invoke("journal", str(run_root), str(c1))
    assert out.exit_code == 2
    assert "does not open from the seal that closes the one before it" in out.output
    assert "period 2 in" in out.output  # named, so an operator knows WHICH boundary
    assert "j1 INACTIVE->STARTING" in out.output  # period 1 still narrated, then it stops
    assert "j2" not in out.output  # and nothing past the refused boundary


def test_dl142_refuses_a_closed_segment_whose_tail_is_torn(tmp_path: Path) -> None:
    """`read_journal` tolerates a torn FINAL line, which is right for the
    file an appender is still appending to and wrong for a closed one: a
    tolerated tail there is a hole in the MIDDLE of the lineage.

    It is the SAME sentence the subscriber's backfill produces
    (tests/test_boundary.py's torn-tail pin): one corruption, one text,
    whichever reader met it (DL-139). It is POSITIONED per caller, and this
    reader knows a segment is closed by its place in the list it was given
    -- so the refusal lands BEFORE period 1's trace rather than after it.

    Two faults, one answer: a segment that is both torn AND mis-identified
    names the tail here exactly as it does in the backfill, because the
    tail is what makes the stream a lie."""
    run_root, c1, _c2 = _two_periods(tmp_path)
    segment = wal_path(run_root, 1)
    lines = segment.read_bytes().splitlines()
    assert json.loads(lines[-1])["rec"] == "seal"
    segment.write_bytes(b"\n".join(lines[:-1]) + b"\n" + lines[-1][:20])

    out = _invoke("journal", str(run_root), str(c1))
    assert out.exit_code == 2
    assert f"journal {segment}: a closed segment ends in a `seal`" in out.output
    assert "a backfill across it would skip records silently" in out.output
    assert "STARTING" not in out.output  # ahead of the trace, not after it

    both = [json.loads(line) for line in segment.read_bytes().splitlines()[:-1]]
    both[0] = {**both[0], "estate_id": "e-stranger"}
    _rewrite(segment, both)
    two_faults = _invoke("journal", str(run_root), str(c1))
    assert two_faults.exit_code == 2
    assert "a backfill across it would skip records silently" in two_faults.output
    assert "stranger's segment" not in two_faults.output


def test_dl142_refuses_a_correlated_forgery_of_the_whole_boundary(tmp_path: Path) -> None:
    """The forgery every INTEGRITY check passes, and the one this unit
    exists to stop.

    The sidecar is rewritten canonically -- a carried job's status moved,
    so period 2 would open onto state no run ever produced -- its digest
    RECOMPUTED over the new bytes, and that digest copied into both the
    closing `seal` record and the successor's `opens_from_seal`. The
    artifact is self-consistent, the record names it, the opening names the
    record, and the sidecar is the one the opening stands on: every binding
    on this path agrees, because all four were forged together.

    What does not agree is the period's own evidence. `attest.prove_derived`
    rebuilds the seal from the WAL, the spool and the manifests and refuses
    when the stored one is not what they produce -- ss11's "verified means
    RE-DERIVED, not self-consistent", asked here because a read-only replay
    that crossed this would narrate the forged state as confidently as the
    true state."""
    run_root, c1, _c2 = _two_periods(tmp_path)
    honest = _invoke("journal", str(run_root), str(c1))
    assert honest.exit_code == 0 and "j1 SUCCESS->STARTING" in honest.output

    sidecar = seal_path(run_root, 1)
    payload = json.loads(sidecar.read_text())
    jobs = payload["state"]["jobs"]
    assert jobs["j1"]["status"] == "SUCCESS"
    jobs["j1"] = {**jobs["j1"], "status": "FAILURE"}  # state no run produced
    payload.pop("digest")
    forged = Seal(**payload)
    sidecar.write_bytes(forged.to_bytes())
    assert forged.digest != honest_digest(run_root)  # the forgery really moved it

    first, second = wal_path(run_root, 1), wal_path(run_root, 2)
    closing = read_journal(first)
    closing[-1] = {**closing[-1], "digest": forged.digest}
    _rewrite(first, closing)
    opening = read_journal(second)
    opening[0] = {
        **opening[0],
        "opens_from_seal": {**opening[0]["opens_from_seal"], "digest": forged.digest},
    }
    _rewrite(second, opening)

    out = _invoke("journal", str(run_root), str(c1))
    assert out.exit_code == 2
    assert "does not re-derive" in out.output
    assert "proves integrity, not derivation" in out.output
    assert "state:" in out.output  # the FIELD that disagrees, named
    assert "sealed at index" not in out.output  # never narrated as crossed
    assert "j2" not in out.output


def test_dl142_refuses_a_seal_record_edited_under_an_honest_sidecar(tmp_path: Path) -> None:
    """ss2.2: the `seal` RECORD duplicates the fields recovery selects the
    sidecar by, and ss11 requires each to agree.

    Every other gate on this path reads ONE side of that pair. The opening
    is checked against the sidecar (`prove_opening`) and against the
    record's digest (`check_segment_adjacency`), so a valid-shaped edit to
    a record field neither of them reads -- `next_baseline_id` here --
    passed both and produced a boundary narration built on a record the
    lineage does not stand on. `check_record_names_sidecar` is where that
    pair is compared, and the crossing proof runs it."""
    run_root, c1, _c2 = _two_periods(tmp_path)
    segment = wal_path(run_root, 1)
    records = read_journal(segment)
    assert records[-1]["rec"] == "seal"
    # a well-formed baseline id of the right SHAPE (ss2.2 types it as a
    # hash address), so `check_seal_record`'s schema passes it through and
    # only the record-vs-sidecar comparison can catch it
    records[-1] = {**records[-1], "next_baseline_id": "sha256:" + "1" * 64}
    _rewrite(segment, records)

    out = _invoke("journal", str(run_root), str(c1))
    assert out.exit_code == 2
    assert "disagrees with the `seal` record that names it" in out.output
    assert "next_baseline_id" in out.output  # the FIELD, not just "they differ"
    assert "sealed at index" not in out.output  # never narrated as crossed
    assert "j2" not in out.output


def test_dl142_refuses_when_the_sidecar_is_not_the_seal_the_opening_names(
    tmp_path: Path,
) -> None:
    """ss11's identity binding, on the path where it is the deciding gate:
    ONE segment named alone, its predecessor attested, and the opening's
    `opens_from_seal` pointing at a digest the sidecar does not have.

    The attestation proves the predecessor seal was re-derived, so the
    crossing proof is satisfied -- and the opening still has to BE this
    seal's. A digest that matches its own canonical form proves integrity,
    never derivation, and a link rewritten to a stranger's digest proves
    neither."""
    run_root, _c1, c2 = _two_periods(tmp_path)
    assert _invoke("audit", "--run-root", str(run_root), "--period", "1").exit_code == 0
    segment = wal_path(run_root, 2)
    records = read_journal(segment)
    records[0] = {
        **records[0],
        "opens_from_seal": {
            **records[0]["opens_from_seal"],
            "digest": "sha256:" + "7" * 64,
        },
    }
    _rewrite(segment, records)

    out = _invoke("journal", str(segment), str(c2))
    assert out.exit_code == 2
    assert "identity graft" in out.output and "unproved opening" in out.output
    assert "STARTING" not in out.output


def test_dl142_refuses_when_the_opening_seals_sidecar_is_gone(tmp_path: Path) -> None:
    """ss11's recovery matrix, "committed `seal`, sidecar missing: refuse".
    The boundary is unrecoverable, and a replay has nothing to fold."""
    run_root, c1, _c2 = _two_periods(tmp_path)
    seal_path(run_root, 1).unlink()

    out = _invoke("journal", str(run_root), str(c1))
    assert out.exit_code == 2
    assert "a committed seal names a sidecar that is not there" in out.output
    assert "j2" not in out.output


def test_dl142_refuses_when_the_next_periods_bundle_is_missing(tmp_path: Path) -> None:
    """ss11's recovery matrix, "catalog directory missing: refuse naming
    the hash". The next period's catalog comes from the bundle its opening
    `segment` pins, so a pruned or absent bundle is a period this build
    cannot narrate -- and the supplied files may NOT stand in for it, which
    is exactly the substitution that would replay C2's records under C1."""
    run_root, c1, _c2 = _two_periods(tmp_path)
    manifest = read_period_manifest(run_root, 2)
    assert manifest is not None
    directory = bundle_dir(run_root, manifest.source_bundle_hash)
    (directory / "sources.json").unlink()

    out = _invoke("journal", str(run_root), str(c1))
    assert out.exit_code == 2
    assert "no readable sources.json" in out.output
    assert str(directory) in out.output  # naming the hash: the address IS the directory
    assert "j1 INACTIVE->STARTING" in out.output  # period 1 answered before the refusal
    assert "j2" not in out.output


def test_dl142_refuses_a_bundle_whose_catalog_is_not_the_hash_the_segment_pins(
    tmp_path: Path,
) -> None:
    """Like for like (ss1.1): the recipe is the one the `segment` pins,
    never the one the reader happened to find at the address it was
    pointed at.

    The bundle here is HONEST -- written by the estate's own writer, and it
    reproduces its own content address -- and the segment's
    `source_bundle_hash` is redirected to it while its `catalog_hash`
    stays period 2's. Every address check passes and the catalog is still
    not this period's, which is exactly the residue the address cannot
    catch: a build whose lowering moved would land here too. So "the
    estate holds its own catalogs" never becomes "the estate decides what
    it ran under"."""
    run_root, c1, _c2 = _two_periods(tmp_path)
    stranger = tmp_path / "stranger.jil"
    address = write_bundle(
        run_root, [SourceFile(path=str(stranger), text=C1_JIL.replace("j1", "jx"))]
    )
    segment = wal_path(run_root, 2)
    records = read_journal(segment)
    assert records[0]["source_bundle_hash"] != address
    records[0] = {**records[0], "source_bundle_hash": address}
    _rewrite(segment, records)

    out = _invoke("journal", str(run_root))
    assert out.exit_code == 2
    assert f"the bundle {address} does not reproduce the catalog hash" in out.output
    assert "j2" not in out.output


def test_dl142_refuses_a_bundle_that_is_not_the_one_its_address_names(
    tmp_path: Path,
) -> None:
    """The address check, met through this verb: a stored file edited under
    an unchanged `sources.json` is refused before it is ever parsed, so the
    replay never runs on bytes the estate did not store."""
    run_root, c1, _c2 = _two_periods(tmp_path)
    manifest = read_period_manifest(run_root, 2)
    assert manifest is not None
    directory = bundle_dir(run_root, manifest.source_bundle_hash)
    vector = json.loads((directory / "sources.json").read_text())
    stored = directory / vector["sources"][0]["file"]
    stored.write_text(C2_JIL.replace("command: exit 0", "command: exit 7"))

    out = _invoke("journal", str(run_root), str(c1))
    assert out.exit_code == 2
    assert "the bundle is not the one its address names" in out.output
    assert "j2" not in out.output


def test_dl142_refuses_a_boundary_whose_opening_manifest_is_gone(tmp_path: Path) -> None:
    """ss11 names the opening period's committed manifest as what the seal
    is folded against (PR-22), and ss12 forbids pruning it. Gone, the
    boundary cannot be opened -- and the reader says which artifact is
    missing rather than replaying period 2 from an empty state."""
    run_root, c1, _c2 = _two_periods(tmp_path)
    (run_root / "periods" / "000002" / "manifest.json").unlink()

    out = _invoke("journal", str(run_root), str(c1))
    assert out.exit_code == 2
    assert "periods/000002/manifest.json is not there" in out.output
    assert "j2" not in out.output


def test_dl142_refuses_a_supplied_catalog_that_disagrees_with_the_pin(tmp_path: Path) -> None:
    """The supplied files never win over a pin. C2 is a real catalog of
    this very estate and it is still not period 1's, so the first period
    refuses before any trace is printed -- the gate DL-130 put on this verb
    is unchanged by the estate learning to answer without it."""
    run_root, _c1, c2 = _two_periods(tmp_path)

    out = _invoke("journal", str(run_root), str(c2))
    assert out.exit_code == 2
    assert "catalog hash mismatch" in out.output
    assert "period 1 in" in out.output
    assert "STARTING" not in out.output  # refused before the trace, not after it
