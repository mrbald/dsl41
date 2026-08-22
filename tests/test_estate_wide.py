"""The estate-wide walk: `audit`, `journal`, `runs` and `estate prune`
pointed at a LINEAGE rather than at one of its roots (period-model ss1.3;
PR-02f).

DL-134 deferred this half and DL-135 deferred `prune`'s with it, naming
one unit for all four rather than a fourth private walk. So there is one
walk -- `boundary.walk_estate` -- and these tests hold the four verbs to
it: each finds period 1's root through the archive registry after a
physical roll, each reports one estate-wide result, and each refuses BY
NAME a root the registry names and the disk does not.

The lineage is built by the REAL machinery: a period-1 root with a real
subprocess run in it, the offline `seal` verb, `audit`, and
`estate.roll_into_root` -- the same roll `dsl41 run --open-from` performs.
Nothing here writes an estate artifact by hand except where the artifact
under test is corruption, which cannot be produced any other way.

House style follows test_boundary.py and test_retention.py: every refusal
asserts the fragment only its own rule produces, and every gate has a
passing counterpart beside it -- a build in which the walk never works at
all would satisfy "it refuses" and prove nothing.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest

from typer.testing import CliRunner

from dsl41.ast_jil import parse
from dsl41.boundary import EstateAnchor, PeriodRow, default_anchor_dir, walk_estate
from dsl41.canon import canonical_bytes
from dsl41.cli import app
from dsl41.estate import roll_into_root
from dsl41.ir import lower_catalog
from dsl41.oracle_state import Event
from dsl41.period import (
    Sentinel,
    archive_receipt_path,
    attestation_path,
    read_sentinel,
    seal_path,
    sentinel_path,
    wal_path,
    write_sentinel,
)
from dsl41.runner_adapters import LocalCommandAdapter
from dsl41.runner_clock import EngineError, RealClock
from dsl41.runner_journal import read_journal
from dsl41.runner_procid import durable_write
from dsl41.runner_startup import resume_run

from test_run_history import (  # noqa: F401  (shared by design)
    _resume_real,
    _run_real_and_manifest,
)

MACHINE = "insert_machine: m1\ntype: a\nnode_name: localhost\n\n"
C1_JIL = MACHINE + "insert_job: j1\njob_type: c\ncommand: exit 0\nmachine: m1\n"
C2_JIL = C1_JIL + "\ninsert_job: j2\njob_type: c\ncommand: exit 0\nmachine: m1\n"
C3_JIL = C2_JIL + "\ninsert_job: j3\njob_type: c\ncommand: exit 0\nmachine: m1\n"

runner = CliRunner()


# ------------------------------------------------------------- fixtures


@dataclass(frozen=True)
class Lineage:
    """Two roots of ONE estate: period 1 in `root_a`, period 2 opened into
    `root_b` by a physical roll, and both registered in `anchor`.

    The roots are RESOLVED, because ss1.3 persists `target_root` normalized
    and every assertion here is against what the registry actually holds."""

    anchor: Path
    root_a: Path
    root_b: Path
    c1: Path
    c2: Path
    c3: Path


def _invoke(*args: str):
    return runner.invoke(app, list(args), catch_exceptions=False)


def _write_catalogs(base: Path) -> tuple[Path, Path, Path]:
    base.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, text in (("c1.jil", C1_JIL), ("c2.jil", C2_JIL), ("c3.jil", C3_JIL)):
        path = base / name
        path.write_text(text)
        paths.append(path)
    return paths[0], paths[1], paths[2]


def _run_in_period_two(run_root: Path, catalog, anchor_dir: Path, job: str) -> None:
    """Resume the rolled root and run one real job in period 2, so the new
    root holds evidence of its own and a reader that lost it is visible."""

    async def scenario() -> None:
        clock = RealClock()
        engine = await resume_run(
            catalog,
            run_root,
            clock=clock,
            adapters={"CMD": LocalCommandAdapter()},
            anchor_dir=anchor_dir,
        )
        engine.inject(Event(at=clock.now(), kind="STARTJOB", payload={"job": job}))
        await engine.run_until_quiescent(datetime.max)
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(scenario())


def _lineage(tmp_path: Path) -> Lineage:
    """One estate across two roots, built the way an operator builds one:
    genesis and a real run in A, the `seal` verb, `audit`, the roll into B,
    and a real run in B's period 2.

    Period 2 is left OPEN, which is the ordinary state of a rolled estate
    and the state that makes "what was NOT audited" visible."""
    c1, c2, c3 = _write_catalogs(tmp_path / "estate")
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    asyncio.run(_run_real_and_manifest(C1_JIL, root_a, ["j1"], file=str(c1)))
    assert _invoke("seal", "--run-root", str(root_a), "--next", str(c2)).exit_code == 0
    assert _invoke("audit", "--run-root", str(root_a)).exit_code == 0
    anchor = default_anchor_dir(root_a)
    catalog = lower_catalog([parse(C2_JIL, file=str(c2))])
    roll_into_root(root_b, anchor_dir=anchor, catalog_of=lambda _root, _manifest: catalog)
    _run_in_period_two(root_b, catalog, anchor, "j2")
    return Lineage(
        anchor=anchor,
        root_a=root_a.resolve(),
        root_b=root_b.resolve(),
        c1=c1,
        c2=c2,
        c3=c3,
    )


def _foreign_root(tmp_path: Path) -> Path:
    """A root of a DIFFERENT estate, from a second real genesis.

    Its `estate_id` is minted by that genesis and read off its own
    sentinel, never invented here: "belongs to another estate" is a fact
    about two geneses (ss1.2), and a hand-written id would prove only that
    the string comparison runs."""
    other = tmp_path / "other"
    other.mkdir(parents=True, exist_ok=True)
    jil = other / "c1.jil"
    jil.write_text(C1_JIL)
    root = tmp_path / "stranger"
    asyncio.run(_run_real_and_manifest(C1_JIL, root, ["j1"], file=str(jil)))
    return root


def _rewrite_anchor(anchor_dir: Path, periods: dict[str, PeriodRow]) -> None:
    """One registry rewrite, through the anchor's own lock and liturgy."""
    store = EstateAnchor(anchor_dir)
    store.acquire()
    try:
        store.write(store.require().model_copy(update={"periods": periods}))
    finally:
        store.release()


#: how each of the four verbs addresses the whole estate. The rule is one
#: sentence and it is the same for all four: name the lineage ANCHOR where
#: a run root would go
ESTATE_WIDE = {
    "audit": lambda line: ("audit", "--estate-anchor", str(line.anchor)),
    "journal": lambda line: ("journal", str(line.anchor), str(line.c1)),
    "runs": lambda line: ("runs", str(line.anchor)),
    "prune": lambda line: ("estate", "prune", "--estate-anchor", str(line.anchor), "--dry-run"),
}


# ----------------------------------------------------------- the walk


def test_pr02f_the_walk_names_every_period_with_the_root_that_holds_it(tmp_path: Path) -> None:
    """ss1.3's archive registry, read as a lineage.

    After a physical roll the two periods live in two directories, and the
    registry is the only thing that knows which. The walk answers in period
    order, and answers with the ROOTS deduplicated -- a root that opened
    three periods in place is one directory, and a reader that folded it
    once per period would report every row three times."""
    line = _lineage(tmp_path)
    walk = walk_estate(line.anchor)

    assert walk.estate_id == read_sentinel(line.root_a).estate_id
    assert [(entry.period_id, entry.root) for entry in walk.periods] == [
        (1, line.root_a),
        (2, line.root_b),
    ]
    assert walk.roots() == (line.root_a, line.root_b)
    assert walk.provisional == ()
    # the rows are the registry's own, not re-derived: period 1 is attested
    # and period 2 is not, which is what `audit` reads below
    assert walk.periods[0].row.attested and not walk.periods[1].row.attested


def test_the_walk_refuses_a_root_the_registry_names_and_the_disk_does_not(
    tmp_path: Path,
) -> None:
    """Five ways one registry row can be wrong, and a refusal by name for
    each: which period, which root, and why.

    None of them may degrade to a skip. A reader that quietly dropped a
    root would answer with a smaller estate and give an operator no way to
    tell -- the silent loss this project refuses everywhere else. The walk
    passes between every pair, so this is a gate rather than a build in
    which the walk never works."""
    line = _lineage(tmp_path)
    stranger = _foreign_root(tmp_path)
    assert walk_estate(line.anchor).roots() == (line.root_a, line.root_b)

    moved = tmp_path / "archived"
    line.root_a.rename(moved)
    with pytest.raises(EngineError, match=f"period 1: registry root {line.root_a} is missing"):
        walk_estate(line.anchor)
    moved.rename(line.root_a)
    assert walk_estate(line.anchor).roots() == (line.root_a, line.root_b)

    # a FOREIGN root at the registered path: a second genesis, moved there
    line.root_a.rename(moved)
    stranger.rename(line.root_a)
    with pytest.raises(EngineError, match="belongs to estate .*, and this lineage is"):
        walk_estate(line.anchor)
    line.root_a.rename(stranger)
    moved.rename(line.root_a)

    held = sentinel_path(line.root_a).read_bytes()
    sentinel_path(line.root_a).unlink()
    with pytest.raises(EngineError, match="holds no `journal.jsonl` sentinel"):
        walk_estate(line.anchor)

    # corruption, which is the one state no real machinery produces
    durable_write(
        str(sentinel_path(line.root_a)),
        canonical_bytes({"rec": "period_root", "estate_id": ""}) + b"\n",
    )
    with pytest.raises(EngineError, match="sentinel this binary cannot read"):
        walk_estate(line.anchor)
    durable_write(str(sentinel_path(line.root_a)), held)

    segment = wal_path(line.root_a, 1)
    kept = segment.read_bytes()
    segment.unlink()
    with pytest.raises(EngineError, match="holds no `000001.jsonl`"):
        walk_estate(line.anchor)
    durable_write(str(segment), kept)
    assert walk_estate(line.anchor).roots() == (line.root_a, line.root_b)


def test_the_walk_refuses_an_anchor_it_cannot_read_as_a_registry(tmp_path: Path) -> None:
    """The three states of the anchor DIRECTORY itself, each named.

    A directory with no anchor has no registry; a directory holding both
    an anchor and a sentinel is not one this layout produces (ss1.1 puts
    the anchor outside every archivable root), and guessing which it is
    would read one estate by another's head; a key that is not a period
    number belongs to no period at all."""
    line = _lineage(tmp_path)
    empty = tmp_path / "nowhere"
    empty.mkdir()
    with pytest.raises(EngineError, match="no anchor -- the registry is what says"):
        walk_estate(empty)

    (line.anchor / "journal.jsonl").write_bytes(b"")
    with pytest.raises(EngineError, match="holds both `anchor.json` and `journal.jsonl`"):
        walk_estate(line.anchor)
    (line.anchor / "journal.jsonl").unlink()

    stored = EstateAnchor(line.anchor).require()
    _rewrite_anchor(line.anchor, {**stored.periods, "01": stored.periods["1"]})
    with pytest.raises(EngineError, match="registry key '01' is not a period number"):
        walk_estate(line.anchor)

    _rewrite_anchor(
        line.anchor,
        {
            key: row.model_copy(update={"segment_durable": False})
            for key, row in stored.periods.items()
        },
    )
    with pytest.raises(EngineError, match="names no period whose segment is durable"):
        walk_estate(line.anchor)

    _rewrite_anchor(line.anchor, dict(stored.periods))
    assert walk_estate(line.anchor).roots() == (line.root_a, line.root_b)


def test_a_provisional_registry_row_is_ignored_and_said_out_loud(tmp_path: Path) -> None:
    """ss1.3: a row is provisional until its period's first segment is
    durable, and every cross-period reader ignores it until then.

    Ignored, and reported: "the estate has two periods and this total
    covers one" is a fact an operator needs and cannot otherwise see. The
    note is on the verb, because the note is what a verb owes its
    reader."""
    line = _lineage(tmp_path)
    stored = EstateAnchor(line.anchor).require()
    _rewrite_anchor(
        line.anchor,
        {**stored.periods, "2": stored.periods["2"].model_copy(update={"segment_durable": False})},
    )

    walk = walk_estate(line.anchor)
    assert [entry.period_id for entry in walk.periods] == [1]
    assert walk.provisional == (2,)

    said = _invoke("runs", str(line.anchor))
    assert said.exit_code == 0
    assert "period(s) 2 have a registry row whose first segment is not durable yet" in said.output
    assert "j2" not in said.output  # period 2's run is not in the total
    assert "j1" in said.output


def test_one_root_that_holds_two_periods_is_read_once(tmp_path: Path) -> None:
    """The in-place boundary: root A opens period 2 in itself, so the
    registry names ONE directory twice.

    A walk that handed its consumers a root per registry row would fold
    that root twice and print every run twice -- a table an operator
    cannot reconcile with the estate. The estate-wide read is the
    single-root read, exactly."""
    c1, c2, _ = _write_catalogs(tmp_path / "estate")
    run_root = tmp_path / "run"
    asyncio.run(_run_real_and_manifest(C1_JIL, run_root, ["j1"], file=str(c1)))
    assert _invoke("seal", "--run-root", str(run_root), "--next", str(c2)).exit_code == 0
    asyncio.run(_resume_real(c2, run_root, ["j2"]))
    anchor = default_anchor_dir(run_root)

    walk = walk_estate(anchor)
    assert [entry.period_id for entry in walk.periods] == [1, 2]
    assert walk.roots() == (run_root.resolve(),)

    estate_wide = _invoke("runs", str(anchor))
    assert estate_wide.exit_code == 0
    assert [row.split()[0] for row in estate_wide.output.splitlines()[1:] if row.strip()] == [
        "j1",
        "j2",
    ]
    assert estate_wide.output == _invoke("runs", str(run_root)).output


# ------------------------------------------------------- the four verbs


def test_pr02f_audit_attests_every_period_in_the_root_that_holds_it(tmp_path: Path) -> None:
    """`audit` pointed at the estate re-derives every closed period, each
    in its own root, and says what it did not audit.

    Period 2 is OPEN, so it is named and skipped -- audit re-derives closed
    periods and never did anything else. Seal it and the same command
    attests it IN ROOT B, on the chain root B imported: attestation 2 is
    produced where period 2's evidence is, not where period 1's is."""
    line = _lineage(tmp_path)

    first = _invoke("audit", "--estate-anchor", str(line.anchor))
    assert first.exit_code == 0, first.output
    assert f"period 1 in {line.root_a} attested, derivation-verified:" in first.output
    assert f"period 2 in {line.root_b}: not closed, nothing to audit" in first.output

    assert (
        _invoke(
            "seal",
            "--run-root",
            str(line.root_b),
            "--estate-anchor",
            str(line.anchor),
            "--next",
            str(line.c3),
        ).exit_code
        == 0
    )
    second = _invoke("audit", "--estate-anchor", str(line.anchor))
    assert second.exit_code == 0, second.output
    assert f"period 2 in {line.root_b} attested, derivation-verified:" in second.output
    assert "chain through 2" in second.output  # the chain crossed the roll
    assert attestation_path(line.root_b, 2).exists()
    assert not attestation_path(line.root_a, 2).exists()

    one = _invoke("audit", "--estate-anchor", str(line.anchor), "--period", "1")
    assert one.exit_code == 0
    assert f"period 1 in {line.root_a} attested, derivation-verified:" in one.output
    assert "period 2" not in one.output
    absent = _invoke("audit", "--estate-anchor", str(line.anchor), "--period", "9")
    assert absent.exit_code == 2
    assert "period 9 is in no registry row of this lineage" in absent.output


def test_an_estate_with_no_closed_period_says_what_it_skipped_before_it_refuses(
    tmp_path: Path,
) -> None:
    """The refusal an operator meets on a lineage that has never sealed.

    "No closed period to audit" alone would leave them wondering whether
    the estate was read at all, so what the walk found and did not audit is
    printed first."""
    c1, _, _ = _write_catalogs(tmp_path / "estate")
    run_root = tmp_path / "run"
    asyncio.run(_run_real_and_manifest(C1_JIL, run_root, ["j1"], file=str(c1)))

    anchor = default_anchor_dir(run_root)
    refused = _invoke("audit", "--estate-anchor", str(anchor))
    assert refused.exit_code == 2
    assert f"period 1 in {run_root.resolve()}: not closed, nothing to audit" in refused.output
    assert "no closed period to audit" in refused.output

    # and a lineage of ONE period has no boundary to stop at, so `journal`
    # replays it and says nothing about a stop that did not happen
    replayed = _invoke("journal", str(anchor), str(c1))
    assert replayed.exit_code == 0, replayed.output
    assert "j1 RUNNING->SUCCESS" in replayed.output
    assert "the replay stops" not in replayed.output


def test_the_estate_wide_audit_attests_in_period_order(tmp_path: Path) -> None:
    """Producing attestation N requires attestation N-1 present and
    verified (ss1.3), so the order the walk hands its periods over in is
    load-bearing rather than cosmetic.

    Two closed periods, neither attested, audited by ONE command: the walk
    is ascending, so period 1 establishes the induction that period 2
    stands on. A walk that answered newest-first would refuse here."""
    c1, c2, c3 = _write_catalogs(tmp_path / "estate")
    run_root = tmp_path / "run"
    asyncio.run(_run_real_and_manifest(C1_JIL, run_root, ["j1"], file=str(c1)))
    assert _invoke("seal", "--run-root", str(run_root), "--next", str(c2)).exit_code == 0
    asyncio.run(_resume_real(c2, run_root, ["j2"]))
    assert _invoke("seal", "--run-root", str(run_root), "--next", str(c3)).exit_code == 0
    anchor = default_anchor_dir(run_root)

    audited = _invoke("audit", "--estate-anchor", str(anchor))
    assert audited.exit_code == 0, audited.output
    named = [row for row in audited.output.splitlines() if row.startswith("period ")]
    assert [row.split()[1] for row in named] == ["1", "2"]
    assert "chain through 1" in named[0] and "chain through 2" in named[1]
    assert attestation_path(run_root, 1).exists() and attestation_path(run_root, 2).exists()


def test_a_busy_lineage_lock_does_not_end_the_estate_wide_audit(tmp_path: Path) -> None:
    """`Unattested` is one period's bookkeeping, not the walk's verdict.

    A live engine holds the lineage lock for its whole process lifetime,
    so auditing an estate period by period while a later one runs is the
    ORDINARY case -- it is what the verb's own docstring promises and what
    retention needs. A handler outside the loop turned the first busy lock
    into "every later period unaudited", under exit 0 and a report that
    looked complete.

    Both checkpoints land; only the registry rows are outstanding, and the
    verb says how many."""
    c1, c2, c3 = _write_catalogs(tmp_path / "estate")
    run_root = tmp_path / "run"
    asyncio.run(_run_real_and_manifest(C1_JIL, run_root, ["j1"], file=str(c1)))
    assert _invoke("seal", "--run-root", str(run_root), "--next", str(c2)).exit_code == 0
    asyncio.run(_resume_real(c2, run_root, ["j2"]))
    assert _invoke("seal", "--run-root", str(run_root), "--next", str(c3)).exit_code == 0
    anchor = default_anchor_dir(run_root)

    holder = EstateAnchor(anchor)
    holder.acquire()  # what a live engine holds for its process lifetime
    try:
        audited = _invoke("audit", "--estate-anchor", str(anchor))
    finally:
        holder.release()
    assert audited.exit_code == 0, audited.output
    assert attestation_path(run_root, 1).exists()
    assert attestation_path(run_root, 2).exists()  # the walk did NOT stop at period 1
    assert "2 checkpoint(s) are durable with the registry row outstanding" in audited.output

    # and the re-run with the lock free finishes the rows, idempotently
    finished = _invoke("audit", "--estate-anchor", str(anchor))
    assert finished.exit_code == 0
    assert "outstanding" not in finished.output
    stored = EstateAnchor(anchor).require()
    assert stored.periods["1"].attested and stored.periods["2"].attested


def test_dl142_journal_names_every_segment_and_replays_across_the_roll(
    tmp_path: Path,
) -> None:
    """`journal` pointed at the estate names the whole record stream --
    every period, its root and its segment, in period order -- and REPLAYS
    it, roll included (DL-142).

    A physical roll is not a second kind of boundary: the seal record that
    closes period 1 is in root A and the segment that opens period 2 is in
    root B, and the same adjacency proof binds the pair across two
    directories as binds it inside one. `j2` is defined only in C2 and runs
    only in period 2, so it is narrated at all only if the catalog switched
    in root B -- under the C1 the caller supplied there is no such job."""
    line = _lineage(tmp_path)

    out = _invoke("journal", str(line.anchor), str(line.c1))
    assert out.exit_code == 0, out.output
    assert f"period 1 in {line.root_a}: {wal_path(line.root_a, 1)}" in out.output
    assert f"period 2 in {line.root_b}: {wal_path(line.root_b, 2)}" in out.output
    assert "(not replayed)" not in out.output
    # period 1, in root A, under the catalog the caller supplied
    assert "j1 INACTIVE->STARTING" in out.output and "j1 RUNNING->SUCCESS" in out.output
    # the boundary is narrated, and it names the root the lineage rolled INTO
    closing = read_journal(wal_path(line.root_a, 1))[-1]
    assert closing["rec"] == "seal"
    assert (
        f"period 1 sealed at index {closing['closes_at_index']};"
        f" period 2 opens in {line.root_b}"
    ) in out.output
    # period 2, in root B, under root B's own bundle: j2 is C2's alone
    assert "j2 INACTIVE->STARTING" in out.output and "j2 RUNNING->SUCCESS" in out.output
    # DL-141's stop is gone -- the replay continues instead of naming a command
    assert "the replay stops" not in out.output and "DL-136" not in out.output

    # a REFUSED replay does not shorten the enumeration: the whole stream
    # is named first, and only the trace is missing. C2 is a real catalog
    # of this estate and is still not period 1's, so the supplied files
    # lose to the pin rather than quietly re-baselining the read
    mismatched = _invoke("journal", str(line.anchor), str(line.c2))
    assert mismatched.exit_code == 2
    assert f"period 1 in {line.root_a}: catalog hash mismatch" in mismatched.output
    assert f"period 1 in {line.root_a}: {wal_path(line.root_a, 1)}" in mismatched.output
    assert f"period 2 in {line.root_b}: {wal_path(line.root_b, 2)}" in mismatched.output
    assert "STARTING" not in mismatched.output

    # and with NO files at all the estate answers out of its own bundles,
    # in both roots, with the same trace (DL-130; DL-142's ruling)
    unaided = _invoke("journal", str(line.anchor))
    assert unaided.exit_code == 0, unaided.output
    assert unaided.output == out.output


def test_dl142_journal_refuses_a_rolled_boundary_it_cannot_prove(tmp_path: Path) -> None:
    """The roll is where the trust rule earns its keep: root B holds an
    IMPORTED sidecar and none of period 1's evidence, so the sidecar is the
    only thing standing between the reader and a forged continuation.

    Remove it and the estate-wide replay refuses BY NAME at the period that
    could not be proved -- after period 1's trace, which is honest and
    stays, and before period 2's, which would have been narrated out of an
    opening this root cannot prove (period-model ss11; DL-139)."""
    line = _lineage(tmp_path)
    seal_path(line.root_b, 1).unlink()

    out = _invoke("journal", str(line.anchor), str(line.c1))
    assert out.exit_code == 2
    assert f"period 2 in {line.root_b}" in out.output
    assert "unproved opening" in out.output
    assert "j1 RUNNING->SUCCESS" in out.output  # period 1 answered
    assert "j2" not in out.output  # and the forged continuation did not


def test_pr02f_runs_folds_every_root_into_one_table(tmp_path: Path) -> None:
    """`runs` pointed at the estate needs no list of roots: they come from
    the registry, in period order, and the rows sort into one table.

    j1 ran in period 1 in root A and j2 in period 2 in root B, so a table
    holding both is a table that crossed the roll."""
    line = _lineage(tmp_path)

    table = _invoke("runs", str(line.anchor))
    assert table.exit_code == 0, table.output
    jobs = [row.split()[0] for row in table.output.splitlines()[1:] if row.strip()]
    assert jobs == ["j1", "j2"]

    # naming the same estate as two roots is the same table: the registry
    # is a lookup, never a different reading
    listed = _invoke("runs", str(line.root_a), str(line.root_b))
    assert listed.exit_code == 0 and listed.output == table.output

    mixed = _invoke("runs", str(line.anchor), str(line.root_a))
    assert mixed.exit_code == 2
    assert "is a lineage anchor: name it ALONE" in mixed.output


def test_pr02f_estate_prune_plans_every_root_and_reports_one_result(tmp_path: Path) -> None:
    """`estate prune` pointed at the estate plans every root the registry
    names and reports ONE result.

    Each root is still planned on its own -- the floors, the refusals and
    the descriptor the removal walks are per root, because a plan is bound
    to the (st_dev, st_ino) of the root it was computed over. So period 1's
    tombstone in root A goes and period 2's in root B does not: root B's
    period is unattested, and an unattested period's spool is what `audit`
    re-derives it from.

    The anchor is a SIBLING of both roots and each plan floors it. It is
    reported ONCE: a count an operator cannot reconcile with the disk is a
    report that lies."""
    line = _lineage(tmp_path)

    listed = _invoke("estate", "prune", "--estate-anchor", str(line.anchor), "--dry-run")
    assert listed.exit_code == 0, listed.output
    assert f"  {line.root_a}: period 1, attested [1]" in listed.output
    assert f"  {line.root_b}: period 2, attested [1]" in listed.output
    assert "roots planned (2):" in listed.output
    assert "2 root(s) planned" in listed.output
    assert str(wal_path(line.root_a, 1)) in listed.output
    assert str(wal_path(line.root_b, 2)) in listed.output
    assert listed.output.count(str(line.anchor / "anchor.json")) == 1

    removed = _invoke("estate", "prune", "--estate-anchor", str(line.anchor), "--tombstones")
    assert removed.exit_code == 0, removed.output
    assert not (line.root_a / "runs" / "j1.1").exists()  # period 1 is attested
    assert (line.root_b / "runs" / "j2.1").exists()  # period 2 is not, and is floored
    assert wal_path(line.root_a, 1).exists() and wal_path(line.root_b, 2).exists()


def test_prune_reports_the_roots_it_already_swept_when_a_later_one_refuses(
    tmp_path: Path,
) -> None:
    """Deletion is irreversible, so the report is the point of the verb.

    A refusal on the SECOND root after the first has been swept must still
    say what went: an operator left with a traceback and no list has no
    way to find out. The refusal is still exit 2 and still named."""
    import os as os_mod

    line = _lineage(tmp_path)
    sealed = line.root_b / "runs" / "sealed-off"
    sealed.mkdir()
    (sealed / "inner.txt").write_text("unpinnable without access\n")
    os_mod.chmod(sealed, 0o000)
    try:
        removed = _invoke("estate", "prune", "--estate-anchor", str(line.anchor), "--tombstones")
    finally:
        os_mod.chmod(sealed, 0o700)

    assert removed.exit_code == 2, removed.output
    # root B's refusal, by name
    assert str(line.root_b) in removed.output
    # and root A's sweep, reported rather than thrown away with it
    assert not (line.root_a / "runs" / "j1.1").exists()
    assert str(line.root_a / "runs" / "j1.1") in removed.output
    assert "roots planned (1):" in removed.output
    assert f"  {line.root_a}: period 1, attested [1]" in removed.output


def test_a_run_root_named_where_the_anchor_goes_is_told_what_it_is(tmp_path: Path) -> None:
    """The likeliest typo of all, answered with the fix.

    `--estate-anchor <the run root>` used to be told the directory "holds
    both anchor.json and journal.jsonl", which is false about a run root
    and sends an operator looking for a file that is not there. It holds
    one of the two, and the other one's default place is a fact this can
    state."""
    line = _lineage(tmp_path)
    refused = _invoke("audit", "--estate-anchor", str(line.root_a))
    assert refused.exit_code == 2
    assert f"{line.root_a} is a run ROOT, not a lineage anchor" in refused.output
    assert str(default_anchor_dir(line.root_a)) in refused.output

    # the counterpart: the anchor it named is the one that works
    assert _invoke("audit", "--estate-anchor", str(line.anchor)).exit_code == 0


def test_the_walk_refuses_a_registry_with_a_hole_in_it(tmp_path: Path) -> None:
    """A registry row is inserted when a root first owns a period and is
    never removed, so the keys of a registry this binary wrote are 1..N
    with no gap.

    A hole is an edited anchor, and walking it would report an estate
    whose middle is missing as if it were whole -- the one answer these
    verbs may not give."""
    line = _lineage(tmp_path)
    stored = EstateAnchor(line.anchor).require()
    _rewrite_anchor(line.anchor, {"1": stored.periods["1"], "3": stored.periods["2"]})
    with pytest.raises(EngineError, match=r"names periods \[1, 3\] and a lineage has no holes"):
        walk_estate(line.anchor)

    _rewrite_anchor(line.anchor, dict(stored.periods))
    assert walk_estate(line.anchor).roots() == (line.root_a, line.root_b)


def test_the_retention_plan_reads_the_registry_exactly_as_the_walk_does(tmp_path: Path) -> None:
    """DL-145. ss1.3's registry had TWO readers and the deletion side was
    the lenient one.

    `walk_estate` refuses a non-canonical key and a hole; retention's own
    reader silently DROPPED a row whose key was not canonical and had no
    hole rule at all. A dropped row never reaches `_check_no_silent_loss`,
    so an edited anchor made every estate-wide read refuse while the
    PLAN -- the one that authorizes deletions -- was computed over a
    smaller estate that looked whole.

    One reader now (`boundary.registry_rows`), and this drives the same two
    edits through both doors."""
    from dsl41.retention import plan_retention

    line = _lineage(tmp_path)
    stored = EstateAnchor(line.anchor).require()
    assert set(stored.periods) == {"1", "2"}

    # period 2 reachable ONLY under a non-canonical key: the lenient read
    # answered with a one-period estate, and root B holds period 2
    _rewrite_anchor(line.anchor, {"1": stored.periods["1"], "02": stored.periods["2"]})
    for door in (
        lambda: walk_estate(line.anchor),
        lambda: plan_retention(line.root_b, anchor_dir=line.anchor),
    ):
        with pytest.raises(EngineError, match="registry key '02' is not a period number"):
            door()

    _rewrite_anchor(line.anchor, {"1": stored.periods["1"], "3": stored.periods["2"]})
    for door in (
        lambda: walk_estate(line.anchor),
        lambda: plan_retention(line.root_b, anchor_dir=line.anchor),
    ):
        with pytest.raises(EngineError, match=r"names periods \[1, 3\] and a lineage has no holes"):
            door()

    # and the repaired registry plans, so the refusals above are the rule's
    # and not a fixture that never worked
    _rewrite_anchor(line.anchor, dict(stored.periods))
    assert plan_retention(line.root_b, anchor_dir=line.anchor).estate_id == stored.estate_id


def test_audit_of_a_provisional_period_says_which_state_it_is_in(tmp_path: Path) -> None:
    """ "In no registry row" and "in a row that is not durable yet" are two
    different states, and only one of them is a typo.

    A period whose first segment has not landed HAS a row; saying it has
    none would send an operator looking for a mistake they did not
    make."""
    line = _lineage(tmp_path)
    stored = EstateAnchor(line.anchor).require()
    _rewrite_anchor(
        line.anchor,
        {**stored.periods, "2": stored.periods["2"].model_copy(update={"segment_durable": False})},
    )

    provisional = _invoke("audit", "--estate-anchor", str(line.anchor), "--period", "2")
    assert provisional.exit_code == 2
    assert "period 2 has a registry row whose first segment is not durable yet" in (
        provisional.output
    )
    absent = _invoke("audit", "--estate-anchor", str(line.anchor), "--period", "9")
    assert absent.exit_code == 2 and "period 9 is in no registry row" in absent.output


@pytest.mark.parametrize("verb", sorted(ESTATE_WIDE))
def test_pr02f_every_estate_wide_verb_refuses_a_missing_root_by_name(
    verb: str, tmp_path: Path
) -> None:
    """All four consume the ONE walk, and a verb that did not would answer
    happily about half an estate.

    The refusal names the period, the root and the reason, and points at
    the single-root form for the roots the operator still has."""
    line = _lineage(tmp_path)
    argv = ESTATE_WIDE[verb](line)
    assert _invoke(*argv).exit_code == 0

    moved = tmp_path / "archived"
    line.root_a.rename(moved)
    refused = _invoke(*argv)
    assert refused.exit_code == 2, refused.output
    assert f"period 1: registry root {line.root_a} is missing" in refused.output
    assert "one at a time" in refused.output

    moved.rename(line.root_a)
    assert _invoke(*argv).exit_code == 0


@pytest.mark.parametrize("verb", sorted(ESTATE_WIDE))
def test_pr02f_every_estate_wide_verb_refuses_a_foreign_root_by_name(
    verb: str, tmp_path: Path
) -> None:
    """A root of another estate at a registered path refuses, in every
    verb: two geneses are two estates (ss1.2), and reading one lineage
    through another's head is the one answer none of these verbs may
    give."""
    line = _lineage(tmp_path)
    stranger = _foreign_root(tmp_path)
    argv = ESTATE_WIDE[verb](line)
    assert _invoke(*argv).exit_code == 0

    moved = tmp_path / "archived"
    line.root_a.rename(moved)
    stranger.rename(line.root_a)
    refused = _invoke(*argv)
    assert refused.exit_code == 2, refused.output
    assert f"period 1: registry root {line.root_a} belongs to estate" in refused.output
    assert read_sentinel(line.root_a).estate_id in refused.output

    line.root_a.rename(stranger)
    moved.rename(line.root_a)
    assert _invoke(*argv).exit_code == 0


# ------------------------------------------------ the surface, unchanged


def test_the_single_root_invocations_are_unchanged(tmp_path: Path) -> None:
    """The estate-wide mode is a second way to address these verbs and not
    a change to the first.

    Every line below is the invocation the runbook has typed since DL-134
    and DL-135, against the same rolled estate, answering exactly as it
    did: no root named in an `audit` line, one root's rows in `runs`, one
    segment replayed by `journal`, one plan's tail in `prune`."""
    line = _lineage(tmp_path)

    audited = _invoke("audit", "--run-root", str(line.root_a))
    assert audited.exit_code == 0
    assert audited.output.startswith("period 1 attested, derivation-verified: sha256:")
    assert str(line.root_a) not in audited.output

    # a rolled root holds the seal it opened from and none of that period's
    # WAL, so it has nothing of its own to audit -- named by the root
    nothing = _invoke("audit", "--run-root", str(line.root_b), "--estate-anchor", str(line.anchor))
    assert nothing.exit_code == 2
    assert f"{line.root_b}: no closed period to audit" in nothing.output

    replayed = _invoke("journal", str(wal_path(line.root_a, 1)), str(line.c1))
    assert replayed.exit_code == 0
    assert replayed.output.splitlines()[0].endswith(
        "j1 INACTIVE->STARTING [STARTJOB event (control)]"
    )
    drifted = _invoke("journal", str(wal_path(line.root_a, 1)), str(line.c2))
    assert drifted.exit_code == 2
    assert drifted.output.startswith("catalog hash mismatch:")

    table = _invoke("runs", str(line.root_b))
    assert table.exit_code == 0
    assert [row.split()[0] for row in table.output.splitlines()[1:] if row.strip()] == ["j2"]

    planned = _invoke("estate", "prune", "--run-root", str(line.root_a), "--dry-run")
    assert planned.exit_code == 0
    assert "-- estate " in planned.output and ", period 1, attested [1]" in planned.output
    assert "root(s) planned" not in planned.output


def test_every_estate_wide_verb_is_addressed_the_same_way(tmp_path: Path) -> None:
    """One rule for four verbs: name the lineage ANCHOR where a run root
    would go.

    `audit` and `estate prune` already take `--estate-anchor`, so naming it
    ALONE is their estate-wide form; `runs` and `journal` take their root
    as an argument, so the anchor directory goes there. A verb given
    neither address refuses rather than guessing which estate is meant."""
    line = _lineage(tmp_path)

    for argv in (("audit",), ("estate", "prune", "--dry-run")):
        refused = _invoke(*argv)
        assert refused.exit_code == 2, refused.output
        assert "--estate-anchor alone" in refused.output

    # and the anchor is recognised wherever a root would be named, in both
    # of the verbs whose address is positional
    assert _invoke("runs", str(line.anchor)).exit_code == 0
    assert _invoke("journal", str(line.anchor), str(line.c1)).exit_code == 0


def test_a_foreign_estates_anchor_is_not_a_second_reading_of_this_one(tmp_path: Path) -> None:
    """The sentinel a genesis writes and the anchor it writes are one
    lineage's, and the walk proves the pair rather than assuming it.

    A stranger's sentinel dropped into a registered root is caught by
    `estate_id` (ss1.2) even though every path still resolves and every
    file still parses -- which is the case a reader that only checked
    existence would answer."""
    line = _lineage(tmp_path)
    held = sentinel_path(line.root_b).read_bytes()
    original = read_sentinel(line.root_b)
    write_sentinel(
        line.root_b,
        Sentinel(estate_id=str(uuid.uuid4()), claim_id=original.claim_id),
    )
    with pytest.raises(EngineError, match="period 2: registry root .* belongs to estate"):
        walk_estate(line.anchor)

    durable_write(str(sentinel_path(line.root_b)), held)
    assert walk_estate(line.anchor).roots() == (line.root_a, line.root_b)


# --------------------------------------------- the archive, across a roll (DL-144)


def test_pr56_an_archived_period_in_another_root_reads_through_the_walk(
    tmp_path: Path,
) -> None:
    """DL-144 across a physical roll: period 1's inputs are archived in
    root A while root B holds the checkpoint that covers them.

    Two things a single-root build gets wrong and this pins. The COVER is
    an estate fact -- attestation 2 lives in root B, and a per-root rule
    would floor root A's only period forever. And the WALK is what makes
    the archived row readable at all: `_prove_root` accepts a registered
    period whose segment is gone when the receipt proves it, so all four
    estate-wide verbs still cover the whole estate rather than stopping at
    root A."""
    from dsl41.period import ARCHIVE_CLASS, archive_receipt_path, read_archive_receipt
    from dsl41.retention import plan_retention, prune

    line = _lineage(tmp_path)
    assert _invoke("audit", "--estate-anchor", str(line.anchor)).exit_code == 0
    assert (
        _invoke(
            "seal", "--run-root", str(line.root_b), "--estate-anchor", str(line.anchor),
            "--next", str(line.c3),
        ).exit_code
        == 0
    )
    assert _invoke("audit", "--estate-anchor", str(line.anchor)).exit_code == 0
    assert attestation_path(line.root_b, 2).exists()
    assert not attestation_path(line.root_a, 2).exists()

    # period 1's spool goes first (PR-36b's order), then its inputs
    assert (
        _invoke(
            "estate", "prune", "--run-root", str(line.root_a),
            "--estate-anchor", str(line.anchor), "--tombstones",
        ).exit_code
        == 0
    )
    plan = plan_retention(line.root_a, anchor_dir=line.anchor)
    offered = [item for item in plan.prunable() if item.kind == "wal"]
    assert [item.period_id for item in offered] == [1], plan.artifacts
    report = prune(plan, classes=(ARCHIVE_CLASS,), dry_run=False)
    assert report.refused == () and report.failed == ()
    assert not wal_path(line.root_a, 1).exists()
    receipt = read_archive_receipt(line.root_a, 1)
    assert receipt is not None and receipt.archived == ("wal/000001.jsonl",)

    walk = walk_estate(line.anchor)
    assert [(entry.period_id, entry.root) for entry in walk.periods] == [
        (1, line.root_a),
        (2, line.root_b),
    ]
    assert walk.periods[0].archived is not None and walk.periods[1].archived is None

    audited = _invoke("audit", "--estate-anchor", str(line.anchor))
    assert audited.exit_code == 0, audited.output
    assert f"period 1 in {line.root_a} inputs archived, attestation-verified:" in audited.output
    assert archive_receipt_path(line.root_a, 1).name in audited.output
    assert f"period 2 in {line.root_b} attested, derivation-verified:" in audited.output

    replayed = _invoke("journal", str(line.anchor))
    assert replayed.exit_code == 0, replayed.output
    assert "[inputs archived: unreplayable]" in replayed.stdout
    assert replayed.stdout.count("UNREPLAYABLE GAP") == 1
    # it CROSSED: period 2's trace is there, on the attestation root B holds
    assert "j2 INACTIVE->STARTING" in replayed.stdout

    listed = _invoke("runs", str(line.anchor))
    assert listed.exit_code == 0
    assert "period 1 has no rows -- its inputs were archived" in listed.output
    assert [row.split()[0] for row in listed.stdout.splitlines()[1:] if row.strip()] == ["j2"]

    swept = _invoke("estate", "prune", "--estate-anchor", str(line.anchor), "--dry-run")
    assert swept.exit_code == 0, swept.output
    assert "roots planned (2):" in swept.output


def test_pr56_a_root_whose_every_period_is_archived_still_plans(tmp_path: Path) -> None:
    """DL-144: an old root of a rolled lineage, swept down to its proofs.

    Its sidecar, attestation and receipt are a permanent floor and there is
    still a plan to compute over them. A planner that refused a root with
    no segment -- which is what "an interrupted genesis" used to mean --
    would make one archived root refuse the whole estate-wide sweep."""
    from dsl41.period import ARCHIVE_CLASS, archive_receipt_path
    from dsl41.retention import plan_retention, prune

    line = _lineage(tmp_path)
    assert _invoke("audit", "--estate-anchor", str(line.anchor)).exit_code == 0
    assert (
        _invoke(
            "seal", "--run-root", str(line.root_b), "--estate-anchor", str(line.anchor),
            "--next", str(line.c3),
        ).exit_code
        == 0
    )
    assert _invoke("audit", "--estate-anchor", str(line.anchor)).exit_code == 0
    assert (
        _invoke(
            "estate", "prune", "--run-root", str(line.root_a),
            "--estate-anchor", str(line.anchor), "--tombstones",
        ).exit_code
        == 0
    )
    prune(
        plan_retention(line.root_a, anchor_dir=line.anchor),
        classes=(ARCHIVE_CLASS,),
        dry_run=False,
    )
    assert wal_path(line.root_a, 1).parent.is_dir()
    assert not any(wal_path(line.root_a, 1).parent.glob("*.jsonl"))

    plan = plan_retention(line.root_a, anchor_dir=line.anchor)
    # root A holds `periods/000002/` -- the seal installed it before the
    # roll -- so the plan's newest period is 2 even with no segment at all
    assert plan.current_period == 2 and plan.archived == frozenset({1})
    floored = {item.path for item in plan.floors()}
    assert archive_receipt_path(line.root_a, 1) in floored
    assert attestation_path(line.root_a, 1) in floored
    assert seal_path(line.root_a, 1) in floored

    swept = _invoke("estate", "prune", "--estate-anchor", str(line.anchor), "--dry-run")
    assert swept.exit_code == 0, swept.output
    assert "roots planned (2):" in swept.output

    # `dsl41 runs` on a root with NO segments answers with an empty table,
    # and that answer is bought with the receipts: an unreadable one must
    # not buy the same table, because the two would be indistinguishable
    empty = _invoke("runs", str(line.root_a))
    assert empty.exit_code == 0, empty.output
    assert "period 1 has no rows -- its inputs were archived" in empty.output
    durable_write(str(archive_receipt_path(line.root_a, 1)), b"{not canonical\n")
    unreadable = _invoke("runs", str(line.root_a))
    assert unreadable.exit_code == 2, unreadable.output
    assert "000001.archive.json" in unreadable.output
    assert not unreadable.stdout.strip()  # no table at all, honest or otherwise


def test_pr56_a_lost_period_in_a_fully_archived_root_still_refuses(tmp_path: Path) -> None:
    """DL-144 review: the loss refusal must not collapse when the loss is
    total.

    `audit --run-root` used to build its list from `closed_periods`, which
    is empty exactly when every closed period has lost its segment -- so a
    root whose only period was archived and then lost its receipt answered
    "no closed period to audit" instead of naming the loss. The bound comes
    from the SIDECARS now, and a sidecar is a permanent floor."""
    from dsl41.period import ARCHIVE_CLASS, archive_receipt_path
    from dsl41.retention import plan_retention, prune

    line = _lineage(tmp_path)
    assert _invoke("audit", "--estate-anchor", str(line.anchor)).exit_code == 0
    assert (
        _invoke(
            "seal", "--run-root", str(line.root_b), "--estate-anchor", str(line.anchor),
            "--next", str(line.c3),
        ).exit_code
        == 0
    )
    assert _invoke("audit", "--estate-anchor", str(line.anchor)).exit_code == 0
    assert (
        _invoke(
            "estate", "prune", "--run-root", str(line.root_a),
            "--estate-anchor", str(line.anchor), "--tombstones",
        ).exit_code
        == 0
    )
    prune(
        plan_retention(line.root_a, anchor_dir=line.anchor),
        classes=(ARCHIVE_CLASS,),
        dry_run=False,
    )
    archived = _invoke("audit", "--run-root", str(line.root_a))
    assert archived.exit_code == 0, archived.output
    assert "inputs archived, attestation-verified:" in archived.output

    archive_receipt_path(line.root_a, 1).unlink()  # the archive becomes a loss
    lost = _invoke("audit", "--run-root", str(line.root_a))
    assert lost.exit_code == 2, lost.output
    assert "this is LOSS and not an archive" in lost.output
    assert "no closed period to audit" not in lost.output


def _forge_branch(run_root: Path, period_id: int) -> str:
    """A SECOND valid seal+attestation pair for one period of THIS estate,
    correlated so every artifact agrees with every other.

    The sidecar differs only in a `boundary_request` input scalar -- one of
    the fields period-model §11 exempts from re-derivation and audit
    carries -- so it is a seal this estate could have closed with and did
    not. A fresh attestation is minted over it, so the pair verifies on its
    own terms end to end. What says it is the wrong BRANCH is §1.3's
    registry row, and nothing else on disk does.

    Returns the forged digest."""
    from dsl41.attest import Attestation, read_attestation
    from dsl41.boundary import read_seal
    from dsl41.runner_journal import dsl41_version

    real = read_seal(run_root, period_id)
    forged = real.model_copy(
        update={
            "boundary_request": real.boundary_request.model_copy(
                update={"claimed_actor": "another-branch"}
            )
        }
    )
    assert forged.digest != real.digest
    durable_write(str(seal_path(run_root, period_id)), forged.to_bytes())
    previous = read_attestation(run_root, period_id - 1)
    durable_write(
        str(attestation_path(run_root, period_id)),
        Attestation(
            seal_digest=forged.digest,
            period_id=period_id,
            chain_through_period=period_id,
            prev_attestation_digest=None if previous is None else previous.digest,
            state_machine_version=forged.state_machine_version,
            dsl41_version=dsl41_version(),
            audited_at="2026-08-22T00:00:00.000000",
        ).to_bytes(),
    )
    return forged.digest


def test_b7_a_wrong_branch_pair_in_a_registered_root_releases_nothing(
    tmp_path: Path,
) -> None:
    """B7: §1.3's registry row does not only say WHERE -- its committed
    `seal_digest` is what makes a branch THIS lineage's.

    DL-144's B3 closed the ROOT: a row pointing at another estate's
    directory refuses. It left the row's own digest unread, so a
    SAME-ESTATE, same-period pair that this lineage never closed with
    still granted the cover -- and root A's WAL, evidence of the run that
    did happen, would be deleted on the strength of a checkpoint over a
    branch that did not.

    The forgery lives in root B, which is where a cross-root cover is read
    and where no local `seal` RECORD contradicts a rewritten sidecar. It
    verifies on its own terms end to end, which is the point: nothing but
    the row can tell it apart."""
    from dsl41.attest import verify_attestation
    from dsl41.boundary import EstateAnchor
    from dsl41.period import ARCHIVE_CLASS
    from dsl41.retention import plan_retention, prune

    line = _lineage(tmp_path)
    assert _invoke("audit", "--estate-anchor", str(line.anchor)).exit_code == 0
    assert (
        _invoke(
            "seal", "--run-root", str(line.root_b), "--estate-anchor", str(line.anchor),
            "--next", str(line.c3),
        ).exit_code
        == 0
    )
    assert _invoke("audit", "--estate-anchor", str(line.anchor)).exit_code == 0
    assert (
        _invoke(
            "estate", "prune", "--run-root", str(line.root_a),
            "--estate-anchor", str(line.anchor), "--tombstones",
        ).exit_code
        == 0
    )
    plan = plan_retention(line.root_a, anchor_dir=line.anchor)
    assert [item.period_id for item in plan.prunable() if item.kind == "wal"] == [1]

    committed = EstateAnchor(line.anchor).require().row(2).seal_digest
    forged = _forge_branch(line.root_b, 2)
    assert committed is not None and committed != forged
    assert verify_attestation(line.root_b, 2).seal_digest == forged  # proves out alone

    with pytest.raises(EngineError, match="this LINEAGE never closed with"):
        plan_retention(line.root_a, anchor_dir=line.anchor)
    # the LIVE re-check asks the same question: a plan taken before the
    # forgery may not carry a cover past it
    report = prune(plan, classes=(ARCHIVE_CLASS,), dry_run=False)
    assert report.removed == ()
    assert report.refused and all("never closed with" in why for _, why in report.refused)
    assert wal_path(line.root_a, 1).exists()
    assert not archive_receipt_path(line.root_a, 1).exists()
