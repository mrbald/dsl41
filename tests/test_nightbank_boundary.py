"""examples/nightbank across a period boundary: the RUNBOOK's boundary-era
exercises, driven as scenarios (period-model ss1.3, ss7, ss8, ss9, ss11,
ss11a, ss12; DL-133, DL-134, DL-135, DL-136).

These are ACCEPTANCE tests and they assert on what an operator can see: an
exit code, a line of output, a file on disk, an answer over the control
socket. What the boundary does INSIDE is pinned unit by unit in
`tests/test_boundary.py`, `tests/test_estate.py` and
`tests/test_retention.py`; nothing here re-tests any of it. What is new is
the estate: 81 jobs with boxes, calendars, resources, an external instance
and operator holds, instead of the two-job fixtures the units use.

Two tiers, for the reason DL-107 gives. The flagship runs REAL processes,
because "the engine exits code 3" and "period 2 answers the socket with
period 1's globals" are claims about processes and neither is observable
from inside one interpreter. The rest run in-process against the same
estate, which costs a second each instead of ten.

The night is always built by the launcher's own `prepare_night`, never by
a copy of it here: the sandbox is only worth testing if the night under
test is the night the operator starts.

The region anchors are stamped SIX HOURS out, so no region box fires
during a scenario. That is the one thing these tests arrange, and they
arrange it through the launcher's own knob.

ss14's scenarios live here: A (the quiet boundary) and C (the lineage) as
the exercises above, and B (the boundary over a live night) as the block
at the end, which runs its night DETACHED under a real supervisor.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from typer.testing import CliRunner

from dsl41.ast_jil import parse, render_preserve
from dsl41.boundary import (
    ClaimedHead,
    ClosedHead,
    EstateAnchor,
    OpenHead,
    PeriodSealed,
    SealRequest,
    default_anchor_dir,
    read_seal,
    stage_next_period,
)
from dsl41 import boundary
from dsl41.cli import app
from dsl41.ir import lower_catalog
from dsl41.oracle_state import Event
from dsl41.period import (
    RuntimeProfile,
    SourceFile,
    attestation_path,
    read_period_manifest,
    seal_path,
    stage_manifest,
    wal_path,
    write_bundle,
)
from dsl41.placeholders import load_properties, substitute
from dsl41.runner_adapters import FakeAdapter, FileWatcherAdapter, LocalCommandAdapter
from dsl41.runner_admission import (
    PROTOCOL_VERSION,
    EnvelopeError,
    parse_envelope,
)
from dsl41.runner_clock import EngineError, RealClock, VirtualClock
from dsl41.runner_hosts import HostCommand
from dsl41.runner_journal import read_journal
from dsl41.runner_ledger import STATE_MACHINE_VERSION
from dsl41.runner_scheduler import Scheduler
from dsl41.runner_startup import resume_run, start_run, wire_from_profile

from test_nightbank_example import NB, SMALL, _launcher
from test_runner_leadership import cli, engine, wait_for
from test_runner_supervisor import _kill_group

LAUNCHER = _launcher()
SMALL_FILES = [Path(path) for path in SMALL]

runner = CliRunner()


# ------------------------------------------------------------- fixtures


@pytest.fixture
def night_base():
    """A SHORT base directory. The engine binds `<run-root>/control.sock`
    and pytest's `tmp_path` overruns `sun_path`'s 104-byte macOS limit --
    the same workaround the leadership and supervisor tiers use."""
    base = tempfile.mkdtemp(prefix="dsl41nb-", dir="/tmp")
    try:
        yield Path(base)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _night(base: Path) -> tuple[Path, Path]:
    """One night's directories, properties and profile, built by the
    launcher's own `prepare_night`. Returns (run root, properties file).

    Incidents are off: a scripted failure is the subject of exercises 3-6
    and noise here. The anchor is six hours out, so the only clocks in the
    estate stay in the future for the whole scenario."""
    LAUNCHER.prepare_night(
        base,
        "small",
        anchor_utc=datetime.now(UTC) + timedelta(hours=6),
        stagger_mins=3,
        incidents=False,
    )
    return base / "engine", base / "night.properties"


def _load(props: Path):
    """The small estate as the CLI loads it: placeholders substituted from
    the night's properties, files in command-line (sorted) order, because
    order is part of `source_bundle_hash` (ss1.1)."""
    bindings = load_properties([props])
    parsed = []
    for path in SMALL_FILES:
        text, _ = substitute(path.read_text(), bindings, file=str(path))
        parsed.append(parse(text, file=str(path)))
    return lower_catalog(parsed), parsed


def _genesis(run_root: Path, props: Path, *, events: tuple[Event, ...] = ()) -> None:
    """Period 1 of a nightbank estate, opened in process exactly as
    `dsl41 run` opens one -- real clock, the estate's own scheduler, the
    real adapters -- then stopped.

    `events` are the operator's, and they go in through `inject`: an
    injected input carries no `expect`, so it is not an EXTERNALLY
    requested attempt and ss9's retry horizon does not gate the seal that
    follows (`boundary.externally_requested_attempts`). The flagship
    scenario sends the same commands through `dsl41 sendevent` and meets
    that gate, which is where it belongs."""
    catalog, parsed = _load(props)
    sources = [SourceFile(path=jf.file, text=render_preserve(jf)) for jf in parsed]
    staged = stage_manifest(
        catalog,
        source_bundle_hash=write_bundle(run_root, sources),
        profile=RuntimeProfile(),
        state_machine_version=STATE_MACHINE_VERSION,
    )
    # ONE clock: the engine's basis is naive UTC and so is the scheduler's
    # anchor, and two `RealClock()` reads would put a microsecond of skew
    # between an estate and the plans built over it
    clock = RealClock()
    started = start_run(
        catalog,
        run_root,
        clock=clock,
        adapters={"CMD": LocalCommandAdapter(), "FW": FileWatcherAdapter()},
        scheduler=Scheduler(catalog, start=clock.now(), default_tz="UTC"),
        staged=staged,
    )
    for event in events:
        started.inject(event)
    asyncio.run(started.run_until_quiescent(clock.now()))
    asyncio.run(started.shutdown())
    assert started.journal is not None
    started.journal.close()


#: RUNBOOK exercise 15 step 1's set: every scheduled top-level job or box
#: with a future tick. Holding them is what the runbook tells an operator
#: to do before a seal, and it is also what keeps this file's real-clock
#: scenarios deterministic -- a held job starts nothing, so no calendar
#: date can put a live process under a cutoff.
QUIESCE = (
    "APAC_EOD_B",
    "EMEA_EOD_B",
    "AMER_EOD_B",
    "OPS_HEARTBEAT_C",
    "OPS_MONTHLY_ATTRIB_C",
    "OPS_QTR_REG_REPORT_C",
)


#: The operator activity every in-process scenario carries across its
#: boundary: one recon gate flipped, one top-level box parked, and one job
#: taken to SUCCESS. The job is BYPASSED (`ON_NOEXEC`, SEM-22) rather than
#: run -- status flows and no process spawns -- because these scenarios are
#: about what crosses a boundary, not about a `fakework` process. The
#: flagship runs a real one.
def _operator_events() -> tuple[Event, ...]:
    at = RealClock().now()
    return tuple(Event(at=at, kind="ON_HOLD", payload={"job": job}) for job in QUIESCE) + (
        Event(at=at, kind="SET_GLOBAL", payload={"name": "RECON_APAC", "value": "CLEAN"}),
        Event(at=at, kind="ON_HOLD", payload={"job": "OPS_B"}),
        Event(at=at, kind="ON_NOEXEC", payload={"job": "OPS_XINST_DEMO_C"}),
        Event(at=at, kind="FORCE_STARTJOB", payload={"job": "OPS_XINST_DEMO_C"}),
    )


def _open_in_place(run_root: Path, props: Path) -> None:
    """ss7 step 9's in-place opener, driven the way the runbook drives it:
    `run --resume` on the same root, then stop. In process, because the
    engine only has to OPEN period 2 here, not serve it."""
    catalog, _ = _load(props)
    opened = asyncio.run(
        resume_run(
            catalog,
            run_root,
            clock=RealClock(),
            adapters={"CMD": LocalCommandAdapter(), "FW": FileWatcherAdapter()},
            scheduler=Scheduler(catalog, start=RealClock().now(), default_tz="UTC"),
        )
    )
    asyncio.run(opened.shutdown())
    assert opened.journal is not None
    opened.journal.close()


def _next_args(props: Path) -> list[str]:
    """`--next <every estate file> -p <the night's properties>` -- C2, in
    the order that addresses its bundle."""
    args: list[str] = []
    for path in SMALL_FILES:
        args += ["--next", str(path)]
    return args + ["-p", str(props)]


def _invoke(*args: str):
    return runner.invoke(app, list(args), catch_exceptions=False)


def _seal_offline(run_root: Path, props: Path, *extra: str):
    return _invoke("seal", "--run-root", str(run_root), *_next_args(props), *extra)


def _head(run_root: Path):
    stored = EstateAnchor(default_anchor_dir(run_root)).read()
    assert stored is not None
    return stored.head


def _json(result) -> dict:
    return json.loads(result.stdout)


# ------------------------------- RUNBOOK 15-17, 21: the live night sealed


def test_the_night_seals_live_and_the_next_period_carries_it(night_base: Path) -> None:
    """RUNBOOK exercises 15, 16, 17 and 21 as one walk, between real
    processes: a night with operator activity in it is sealed through the
    control verb, period 2 opens in place and answers with period 1's
    state, the morning after attests it, and only then may anything be
    pruned.

    Three claims here need real processes and cannot be made from inside
    one interpreter: the engine EXITS with code 3 when its period is
    sealed (ss7), the seal is REFUSED by ss9's horizon without the engine
    dying with it, and the reopened period serves the carried globals,
    holds and statuses over its control socket to an ordinary `dsl41
    query`.

    The prune at the end is the one destructive act, and it is last for
    the reason ss12 gives: attesting is what licenses it, and once the
    spool is gone the period can no longer be re-derived from its own
    evidence."""
    run_root, props = _night(night_base)
    socket = str(run_root / "control.sock")

    with engine(night_base, run_root=run_root, files=SMALL_FILES, extra=["-p", str(props)]) as c1:
        for job in QUIESCE:  # exercise 15 step 1: quiesce the triggers first
            sent = cli("sendevent", "ON_HOLD", "-J", job, "-S", socket)
            assert sent.returncode == 0, sent.stdout + sent.stderr
        for args in (
            ("SET_GLOBAL", "--global", "RECON_APAC=CLEAN"),
            ("ON_HOLD", "-J", "OPS_B"),
            ("FORCE_STARTJOB", "-J", "OPS_XINST_DEMO_C"),
        ):
            sent = cli("sendevent", *args, "-S", socket)
            assert sent.returncode == 0, sent.stdout + sent.stderr
        # the runbook's own shell glue, and the job is a real `fakework`
        # process: the seal below cannot be taken while it runs (ss8's
        # "in place, tethered -- full drain")
        wait_for(
            lambda: (
                cli("query", "is-success", "-J", "OPS_XINST_DEMO_C", "-S", socket).returncode == 0
            ),
            timeout_s=60,
        )

        # ss9: a boundary within the closing period's retry horizon of the
        # last externally requested attempt is refused, and refusing is
        # C1's business -- the engine keeps serving
        refused = cli("seal", "--run-root", str(run_root), *_next_args(props))
        assert refused.returncode == 2
        assert "retry_horizon_us" in refused.stdout + refused.stderr
        assert c1.proc.poll() is None
        assert cli("query", "status", "--brief", "-S", socket).returncode == 0

        forced = cli(
            "seal",
            "--run-root",
            str(run_root),
            *_next_args(props),
            "--force-seal",
            "--claimed-actor",
            "ops@nightbank",
        )
        assert forced.returncode == 0, forced.stdout + forced.stderr
        payload = json.loads(forced.stdout.splitlines()[0])
        assert payload["ok"] is True and payload["kind"] == "seal"
        assert payload["period_id"] == 1 and payload["next_period_id"] == 2
        c1.proc.wait(timeout=60)
        assert c1.proc.returncode == 3  # "sealed; period 2 is ready to open"

    seal = read_seal(run_root, 1)
    assert seal.digest == payload["digest"]
    assert seal.boundary_request.claimed_actor == "ops@nightbank"
    # forced, and the log alone says so (ss9)
    assert seal.forced_gate is not None and seal.forced_gate.gate == "retry_horizon"
    # ss3.3: the carry is the estate's own -- a recon gate, an operator
    # hold on a top-level box, and a job that ran
    assert seal.state.globals["RECON_APAC"].value == "CLEAN"
    assert seal.state.jobs["OPS_B"].on_hold is True
    assert seal.state.jobs["OPS_XINST_DEMO_C"].status == "SUCCESS"
    assert isinstance(_head(run_root), ClosedHead)

    with engine(
        night_base, run_root=run_root, files=SMALL_FILES, resume=True, extra=["-p", str(props)]
    ) as c2:
        assert c2.proc.poll() is None
        answered = _json(cli("query", "global", "-N", "RECON_APAC", "-S", socket))
        assert answered["globals"]["RECON_APAC"]["value"] == "CLEAN"
        # the period really did turn over: the answers are stamped with
        # the baseline the seal committed, not the one it closed
        assert answered["baseline_id"] == seal.next_period.baseline_id
        assert answered["baseline_id"] != seal.baseline_id
        held = _json(cli("query", "status", "--job", "OPS_B", "-S", socket))
        assert held["jobs"]["OPS_B"]["on_hold"] is True
        ran = _json(cli("query", "status", "--job", "OPS_XINST_DEMO_C", "-S", socket))
        assert ran["jobs"]["OPS_XINST_DEMO_C"]["status"] == "SUCCESS"
        assert ran["jobs"]["OPS_XINST_DEMO_C"]["run_number"] == 1

    # the morning after (ss1.3): re-derive, then check the checkpoint
    attested = cli("audit", "--run-root", str(run_root))
    assert attested.returncode == 0, attested.stdout + attested.stderr
    assert "period 1 attested" in attested.stdout
    assert attestation_path(run_root, 1).exists()
    verified = cli("verify", "--run-root", str(run_root))
    assert verified.returncode == 0, verified.stdout + verified.stderr
    assert "chain through period 1" in verified.stdout

    spool = run_root / "runs" / "OPS_XINST_DEMO_C.1"
    assert spool.is_dir()
    timed = _run_row(run_root, "OPS_XINST_DEMO_C")
    assert timed["clock_source"] == "spool"

    survey = cli("estate", "prune", "--run-root", str(run_root), "--dry-run", "--tombstones")
    assert survey.returncode == 0, survey.stdout + survey.stderr
    assert str(spool) in survey.stdout
    assert "attested [1]" in survey.stdout
    assert spool.is_dir()  # a dry run deletes nothing

    swept = cli("estate", "prune", "--run-root", str(run_root), "--tombstones")
    assert swept.returncode == 0, swept.stdout + swept.stderr
    assert not spool.exists()
    # PR-36b, and DL-135's own statement of the price: the ROW survives
    # and its timings do not -- `dsl41 runs` reads the record, and read
    # the start and end off a spool that is now absent
    pruned = _run_row(run_root, "OPS_XINST_DEMO_C")
    assert pruned["clock_source"] == "journal"
    assert pruned["status"] == "SUCCESS"


def _run_row(run_root: Path, job: str) -> dict:
    listed = cli("runs", str(run_root), "--job", job, "--format", "json")
    assert listed.returncode == 0, listed.stdout + listed.stderr
    rows = json.loads(listed.stdout)
    assert len(rows) == 1, rows
    return rows[0]


# ------------------------------------ RUNBOOK 15 variant: the stopped night


def test_a_stopped_night_seals_offline_and_the_seal_carries_the_night(
    night_base: Path,
) -> None:
    """RUNBOOK exercise 15's second variant: nothing leads the root, so
    `dsl41 seal` takes `leader.lock` itself and performs the boundary as
    the offline leader (ss7).

    The lock is the discriminator, not a flag, and the operator types the
    same command either way. The evidence is the estate's: a committed
    sidecar carrying the night's state, a `seal` record naming it, and a
    head that moved `open -> closed`."""
    run_root, props = _night(night_base)
    _genesis(run_root, props, events=_operator_events())

    sealed = _seal_offline(run_root, props, "--claimed-actor", "night-ops@nightbank")
    assert sealed.exit_code == 0, sealed.output
    assert "sealed period 1" in sealed.output
    assert "dsl41 run --resume" in sealed.output  # the opener it hands you

    seal = read_seal(run_root, 1)
    records = read_journal(wal_path(run_root, 1))
    assert records[-1]["rec"] == "seal" and records[-1]["digest"] == seal.digest
    head = _head(run_root)
    assert isinstance(head, ClosedHead) and head.seal_digest == seal.digest
    # no `expect` rides on an injected input, so ss9's gate never engaged
    assert seal.forced_gate is None and seal.boundary_request.force_seal is False
    assert seal.boundary_request.claimed_actor == "night-ops@nightbank"
    assert seal.state.globals["RECON_APAC"].value == "CLEAN"
    assert seal.state.globals["RECON_EMEA"].value == "PENDING"  # untouched, and carried too
    assert seal.state.jobs["OPS_B"].on_hold is True
    assert seal.state.jobs["OPS_XINST_DEMO_C"].status == "SUCCESS"
    assert seal.state.jobs["OPS_LEGACY_REPORT_C"].on_ice is True  # the estate's own ice

    _open_in_place(run_root, props)
    assert isinstance(_head(run_root), OpenHead)
    assert wal_path(run_root, 2).exists()
    segment = read_journal(wal_path(run_root, 2))[0]
    assert segment["period_id"] == 2
    assert segment["opens_from_seal"]["digest"] == seal.digest


# ------------------------------------------ RUNBOOK 21: the retention floors


def test_prune_names_every_floor_and_deletes_nothing_it_was_not_asked_for(
    night_base: Path,
) -> None:
    """RUNBOOK exercise 21's first half: `--dry-run` is a survey, and a run
    with no class named deletes nothing and says why (ss12).

    The floors an operator meets on a nightbank estate are the ones the
    deployment runbook's ss2a table lists, and the point of asserting them
    here is that they are computed from a REAL estate rather than from a
    fixture assembled to have them."""
    run_root, props = _night(night_base)
    _genesis(run_root, props, events=_operator_events())
    assert _seal_offline(run_root, props).exit_code == 0
    _open_in_place(run_root, props)

    survey = _invoke("estate", "prune", "--run-root", str(run_root), "--dry-run")
    assert survey.exit_code == 0, survey.output
    assert "would remove 0 artifact(s)" in survey.output
    assert "attested none" in survey.output  # nothing is attested yet
    for floored in (
        run_root / "journal.jsonl",  # the sentinel
        default_anchor_dir(run_root) / "anchor.json",
        seal_path(run_root, 1),  # the seal period 2 opened from
        wal_path(run_root, 2),  # the WAL of an unattested period
    ):
        assert str(floored) in survey.output
    assert "floored (the model refuses)" in survey.output

    refused = _invoke("estate", "prune", "--run-root", str(run_root))
    assert refused.exit_code == 2
    assert "nothing selected" in refused.output
    assert (run_root / "journal.jsonl").exists()

    # attesting is what moves period 1's spool off the floor, and the
    # footer is where an operator reads the attested row (ss1.3)
    assert _invoke("audit", "--run-root", str(run_root)).exit_code == 0
    after = _invoke("estate", "prune", "--run-root", str(run_root), "--dry-run")
    assert after.exit_code == 0
    assert "attested [1]" in after.output


# ------------------------------------- RUNBOOK 18: the retirement note


def test_a_night_from_before_the_boundary_era_is_refused_by_name(night_base: Path) -> None:
    """RUNBOOK exercise 18, as DL-138 left it: a run root written before the
    periodized layout does not resume and is not adopted -- the verb is
    gone, and every read dialect it translated is retired.

    What an operator sees is a tombstone rather than a parse error: the
    refusal names the dialect on the disk and the entry that retired it,
    and `estate adopt` is not a command."""
    run_root, props = _night(night_base)
    old = run_root.parent / "old-engine"
    old.mkdir()
    (old / "journal.jsonl").write_text(
        json.dumps(
            {
                "rec": "header",
                "baseline_id": "b",
                "catalog_hash": "0" * 64,
                "state_machine_version": 1,
                "clock_domain": "real",
                "started_at": "2026-07-01T08:00:00",
            },
            sort_keys=True,
        )
        + "\n"
    )

    stranded = _invoke(
        "run",
        "--resume",
        "--run-root",
        str(old),
        *[str(f) for f in SMALL_FILES],
        "-p",
        str(props),
    )
    assert stranded.exit_code == 2
    assert "RETIRED" in stranded.output and "DL-138" in stranded.output

    gone = _invoke("estate", "adopt", str(old), "--next", str(SMALL_FILES[0]))
    assert gone.exit_code != 0
    # the words typer prints for a command it does not have -- "adopt" alone
    # would also appear in the usage line of a verb that merely refused
    assert "No such command" in gone.output


# ------------------------------------------- RUNBOOK 19: the physical roll


def test_the_roll_is_refused_until_the_closing_night_is_attested(night_base: Path) -> None:
    """RUNBOOK exercise 19: `run --open-from` opens period 2 in a FRESH run
    root, and refuses until the closing period is attested (ss1.3).

    The refusal is the exercise. A roll leaves the closing root behind, so
    the new root can never audit period 1 -- it holds none of period 1's
    inputs. The attestation is what it imports instead, and requiring it
    BEFORE the roll is what stops an operator importing a seal nobody can
    verify."""
    run_root, props = _night(night_base)
    _genesis(run_root, props, events=_operator_events())
    assert _seal_offline(run_root, props).exit_code == 0
    anchor_dir = default_anchor_dir(run_root)
    rolled_root = night_base / "roll"

    early = _invoke(
        "run",
        "--open-from",
        str(anchor_dir),
        "--run-root",
        str(rolled_root),
        *[str(f) for f in SMALL_FILES],
        "-p",
        str(props),
    )
    assert early.exit_code == 2 and "is not attested" in early.output
    # the preflight runs before anything is created: the refusal leaves NO
    # residue -- not the target directory, not its leader.lock (the
    # runbook's exercise 19 teaches exactly this)
    assert not rolled_root.exists()

    assert _invoke("audit", "--run-root", str(run_root)).exit_code == 0

    with engine(
        night_base,
        run_root=rolled_root,
        files=SMALL_FILES,
        extra=["--open-from", str(anchor_dir), "-p", str(props)],
    ) as opened:
        assert opened.proc.poll() is None
        rows = cli("query", "status", "--brief", "-S", str(rolled_root / "control.sock"))
        assert rows.returncode == 0 and "OPS_B" in rows.stdout

    assert wal_path(rolled_root, 2).exists()
    # the imported pair, and the chain the new root CAN check
    assert seal_path(rolled_root, 1).exists() and attestation_path(rolled_root, 1).exists()
    assert cli("verify", "--run-root", str(rolled_root), "--period", "1").returncode == 0
    stored = EstateAnchor(anchor_dir).read()
    assert stored is not None and isinstance(stored.head, OpenHead)
    assert stored.periods["1"].root == str(run_root.resolve())
    assert stored.periods["2"].root == str(rolled_root.resolve())


def test_the_estate_wide_reads_cover_both_roots_of_a_rolled_lineage(night_base: Path) -> None:
    """RUNBOOK exercise 19 step 5: after a roll the estate is two
    directories, and which root holds which period is the anchor's
    registry to answer rather than the operator's to remember (PR-02f).

    All four readers are addressed the same way -- the lineage ANCHOR
    named where a run root would go -- and every one of them covers the
    whole estate or refuses. The last step is the one that matters most
    over a real estate: take a registered root away and the total does not
    quietly shrink, it stops and says which root is gone."""
    run_root, props = _night(night_base)
    _genesis(run_root, props, events=_operator_events())
    assert _seal_offline(run_root, props).exit_code == 0
    anchor_dir = default_anchor_dir(run_root)
    assert _invoke("audit", "--run-root", str(run_root)).exit_code == 0
    rolled = night_base / "roll"
    _roll(rolled, anchor_dir, props)

    audited = _invoke("audit", "--estate-anchor", str(anchor_dir))
    assert audited.exit_code == 0, audited.output
    assert f"period 1 in {run_root.resolve()} attested:" in audited.output
    assert f"period 2 in {rolled.resolve()}: not closed, nothing to audit" in audited.output

    listed = _invoke("estate", "prune", "--estate-anchor", str(anchor_dir), "--dry-run")
    assert listed.exit_code == 0, listed.output
    assert f"  {run_root.resolve()}: period 1, attested [1]" in listed.output
    assert f"  {rolled.resolve()}: period 2, attested [1]" in listed.output
    assert "2 root(s) planned" in listed.output

    # DL-142: the estate files are no longer typed at all -- each period's
    # catalog comes from its own bundle -- and the replay CROSSES the roll
    replayed = _invoke("journal", str(anchor_dir))
    assert replayed.exit_code == 0, replayed.output
    assert f"period 2 in {rolled.resolve()}: {wal_path(rolled.resolve(), 2)}" in replayed.output
    assert "(not replayed)" not in replayed.output
    assert "the replay stops" not in replayed.output
    closing = read_journal(wal_path(run_root, 1))[-1]
    assert (
        f"period 1 sealed at index {closing['closes_at_index']};"
        f" period 2 opens in {rolled.resolve()}"
    ) in replayed.output

    assert _invoke("runs", str(anchor_dir)).exit_code == 0

    shutil.move(str(run_root), str(night_base / "archived"))
    for argv in (
        ("audit", "--estate-anchor", str(anchor_dir)),
        ("runs", str(anchor_dir)),
        ("estate", "prune", "--estate-anchor", str(anchor_dir), "--dry-run"),
    ):
        refused = _invoke(*argv)
        assert refused.exit_code == 2, refused.output
        assert f"period 1: registry root {run_root.resolve()} is missing" in refused.output


# -------------------------------------------- RUNBOOK 20: break-glass


def test_reclaim_frees_a_lineage_a_crashed_roll_left_claimed(night_base: Path) -> None:
    """RUNBOOK exercise 20: a roll that died after it claimed the lineage
    blocks every later opener, and `estate reclaim --force` is the one
    verb that moves it (ss1.3).

    The claim is not garbage. A `claimed` head whose target root is
    unreachable cannot be told from one whose target is merely paused, so
    nothing here decides it -- the operator does, and the estate records
    that they did: the next `segment` carries the actor who said so."""
    from dsl41.runner_clock import EngineError

    run_root, props = _night(night_base)
    _genesis(run_root, props, events=_operator_events())
    assert _seal_offline(run_root, props).exit_code == 0
    assert _invoke("audit", "--run-root", str(run_root)).exit_code == 0
    anchor_dir = default_anchor_dir(run_root)

    lost = night_base / "lost"
    with pytest.raises(_Stopped):
        _roll(lost, anchor_dir, props, stop_at="after_import")
    head = _head(run_root)
    assert isinstance(head, ClaimedHead)
    shutil.rmtree(lost)  # the volume the roll was going to is gone

    second = night_base / "roll"
    with pytest.raises(EngineError, match="the head is claimed and this root does not hold it"):
        _roll(second, anchor_dir, props)

    freed = _invoke(
        "estate",
        "reclaim",
        "--estate-anchor",
        str(anchor_dir),
        "--force",
        "--claimed-actor",
        "duty-manager@nightbank",
    )
    assert freed.exit_code == 0, freed.output
    assert head.claim_id in freed.output
    assert isinstance(_head(run_root), ClosedHead)

    _roll(second, anchor_dir, props)
    segment = read_journal(wal_path(second, 2))[0]
    assert segment["reclaimed"] is not None
    assert segment["reclaimed"]["claimed_actor"] == "duty-manager@nightbank"


class _Stopped(Exception):
    """The crash seam: the roll stops exactly between two durable writes,
    rather than a process being killed and hoped about."""


def _roll(new_root: Path, anchor_dir: Path, props: Path, *, stop_at: str | None = None):
    from dsl41.estate import roll_into_root

    def crash_point(stage: str) -> None:
        if stage == stop_at:
            raise _Stopped(stage)

    return roll_into_root(
        new_root,
        anchor_dir=anchor_dir,
        catalog_of=lambda _root, _manifest: _load(props)[0],
        crash_point=crash_point,
    )


# ================ period-model ss14 scenario B: the boundary over a live
# ================ night. B1 commits over it; B2 refuses, one change at a
# ================ time, and leaves the night exactly where it stood.

#: ss14's B estate is A's, run DETACHED. ss8's mode table requires the FULL
#: drain of a tethered estate, so a tethered B could only ever be A with
#: extra steps; detached is what puts a real supervisor under the boundary,
#: and ss8's supervisor clauses are half of what B is about.
DETACHED = RuntimeProfile(execution_mode="detached")

#: ss14 B1's live closure, cast entirely from `examples/nightbank`.
#: `_check_b_estate` holds every id below to the shape its role needs, so
#: an edit to the estate fails there instead of quietly changing what these
#: scenarios test.
B_BOX = "EMEA_MKT_B"  # the live box
B_MEMBER = "EMEA_MKT_MARKS_C"  # its INACTIVE member: B1 carries it, B2 changes it
B_WATCH = "EMEA_MKT_PX_F"  # the FW watch that crosses, inside that box
B_ICED = "EMEA_MKT_SFTP_SIM_C"  # iced, so the watched file never lands
B_HOLDER = "OPS_HOUSEKEEP_C"  # holds the one TAPE_DRIVE
B_WAITER = "OPS_ARCHIVE_C"  # QUE_WAIT behind it
B_LONG = "OPS_SPOOL_C"  # the long command live at T and reattached in C2
B_KILLED = "APAC_EXE_TRADES_C"  # the KILL ladder the sealer waits out
B_DEFERRED = "OPS_HEARTBEAT_C"  # INACTIVE, carrying a run_window timer
B_HELD_SPAWN = "APAC_INV_MACROS_C"  # the pending_spawn on the passive host
B_UNBOUND = "AMER_INV_MACROS_C"  # B2's applied SPAWN with no spawn.json yet
#: B1's C2 change. ss14 says C2 touches "none of the live closure", which
#: means it touches SOMETHING: an identical C2 moves no graph node, so the R
#: gate would pass with nothing to classify and an over-wide closure rule
#: would never be reached. This job is the estate's decommissioned report,
#: iced and depended on by nothing live -- and on an 81-job estate that is a
#: SHORT list, because a shared machine or a shared box puts almost every
#: other job inside some live job's forward closure (ss10.2).
B_OUTSIDE = "OPS_LEGACY_REPORT_C"


def _check_b_estate(catalog) -> None:
    """Every id above, held to the shape its role needs.

    A worked scenario names real jobs, and a job renamed or re-wired in the
    estate must fail here rather than turn one of B's rows into a different
    row nobody notices."""
    jobs = catalog.jobs
    assert jobs[B_BOX].job_type == "BOX"
    assert jobs[B_WATCH].job_type == "FW" and jobs[B_WATCH].box.box_name == B_BOX
    assert jobs[B_MEMBER].box.box_name == B_BOX and jobs[B_ICED].box.box_name == B_BOX
    # the member must NOT be startable when the box starts, or it would run
    # instead of carrying INACTIVE; the iced one must be, or icing it would
    # change nothing
    assert jobs[B_MEMBER].sem.condition is not None
    assert jobs[B_ICED].sem.condition is None
    for name in (B_HOLDER, B_WAITER, B_LONG, B_KILLED, B_HELD_SPAWN, B_UNBOUND):
        assert jobs[name].job_type == "CMD"
    # the pair contends for ONE unit of one resource: that is what makes the
    # second of them a waiter rather than a second run
    wanted = [{ref.name for ref in jobs[name].resources} for name in (B_HOLDER, B_WAITER)]
    assert wanted[0] and wanted[0] == wanted[1]
    assert {catalog.resources[name].capacity_units() for name in wanted[0]} == {1}
    # a run_window is what defers a forced start instead of running it
    assert jobs[B_DEFERRED].schedule is not None
    assert jobs[B_DEFERRED].schedule.run_window is not None
    # C2's one change is a job that is iced and in no live closure
    assert jobs[B_OUTSIDE].sem.initial_status == "ON_ICE"
    # the live commands must OUTLAST the scenario, which is a coupling
    # between this file and the estate's `--sleep` numbers that is otherwise
    # written down nowhere. The floors are ~10x the measured walk
    for name, floor in ((B_LONG, 60), (B_HOLDER, 30), (B_KILLED, 10)):
        assert _sleep_seconds(jobs[name].exec_.command) >= floor, name


def _sleep_seconds(command: str) -> int:
    """The `--sleep N` a nightbank command runs for."""
    match = re.search(r"--sleep (\d+)", command)
    assert match is not None, command
    return int(match.group(1))


@dataclass
class _LiveNight:
    """One nightbank night running detached under a real supervisor.

    `engine` and `wiring` are the CURRENT incarnation's: an in-place
    opening replaces both, and teardown stops whichever pair is live."""

    run_root: Path
    catalog: Any
    parsed: list
    wiring: Any
    engine: Any


async def _start_detached_night(base: Path) -> _LiveNight:
    """`dsl41 run --detached` over the small estate, in process.

    The components come from `wire_from_profile`, the ONE builder a real
    run uses (DL-137): the supervisor is a real process, the CMD adapter
    really spawns through it, and FW really polls a real file."""
    run_root, props = _night(base)
    run_root.mkdir(parents=True, exist_ok=True)
    catalog, parsed = _load(props)
    _check_b_estate(catalog)
    # `wire_from_profile` SPAWNS the supervisor, so the guard goes around
    # that call and not after it. A failure in any later wiring step leaves
    # no `_LiveNight` for a caller to tear down -- and the fixture then
    # removes the run root that holds the pid, so nothing could find it
    # again either
    try:
        wiring = await wire_from_profile(run_root, catalog, DETACHED)
        sources = [SourceFile(path=jf.file, text=render_preserve(jf)) for jf in parsed]
        staged = stage_manifest(
            catalog,
            source_bundle_hash=write_bundle(run_root, sources),
            profile=DETACHED,
            state_machine_version=STATE_MACHINE_VERSION,
        )
        engine = start_run(
            catalog,
            run_root,
            clock=RealClock(),
            adapters=wiring.adapters,
            scheduler=wiring.scheduler,
            staged=staged,
        )
    except BaseException:
        _kill_group(run_root)
        raise
    # ss8's supervisor clauses at the seal need the CLIENT, not just the
    # adapter, and `dsl41 run` wires it the same way (cli_run.py)
    engine.supervisor = wiring.client
    return _LiveNight(run_root, catalog, parsed, wiring, engine)


async def _stop_engine(night: _LiveNight) -> None:
    """The detach-stop `dsl41 run --detached` performs on its way out (spec
    ss3 case b): the jobs keep running under the supervisor."""
    night.engine.detach.stopping = True
    await night.engine.shutdown()
    if night.engine.journal is not None:
        night.engine.journal.close()
    with contextlib.suppress(Exception):
        await night.wiring.close()


async def _end_night(night: _LiveNight) -> None:
    """Stop the engine, then the sandbox. A supervisor with no deadman
    outlives its controller by design, so a test that walked away would
    leave a night's wrappers running on the machine."""
    with contextlib.suppress(Exception):
        await _stop_engine(night)
    _kill_group(night.run_root)


async def _drive(night: _LiveNight) -> None:
    """Run the engine to quiescence at the instant it is already at."""
    await night.engine.run_until_quiescent(night.engine.clock.now())


async def _wait_for_evidence(night: _LiveNight, ready, what: str, timeout_s: float = 30.0) -> None:
    """Drive the loop until the estate's own evidence says so, or fail by
    name. A spool file or a watch line, never a sleep: a sleep asserts a
    duration nobody chose."""
    deadline = time.monotonic() + timeout_s
    while not ready():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout_s}s waiting for {what}")
        await night.engine.run_until_quiescent(
            night.engine.clock.now() + timedelta(milliseconds=50)
        )


def _sendevent(night: _LiveNight, kind: str, **payload) -> None:
    night.engine.inject(Event(at=night.engine.clock.now(), kind=kind, payload=payload))


def _park_spawn(night: _LiveNight, job: str):
    """Hold ONE job's SPAWN inside ss3.5's own window.

    `_apply_spawn` records `effect_result{applied}` when it creates the
    adapter task; the supervisor writes `spawn.json` afterwards. The gate
    keeps that task on this side of the write for as long as the scenario
    needs. Constructed rather than raced, for DL-83's reason: the real
    window is milliseconds wide, and a test that raced it would pass
    vacuously whenever it lost."""
    gate = asyncio.Event()
    client = night.wiring.client
    spawn = client.spawn

    async def held(spec):
        if spec["job"] == job:
            await gate.wait()
        return await spawn(spec)

    client.spawn = held
    return gate


async def _arrange_live_closure(night: _LiveNight, *, park: str | None = None):
    """ss14 B1's live closure, arranged through operator verbs alone.

    Every step is something the RUNBOOK's operator types: exercise 15's
    quiesce holds, an `ON_ICE`, a set of `FORCE_STARTJOB`s and one `host
    drain`. Nothing here writes a row or a spool file by hand."""
    engine, run_root = night.engine, night.run_root
    for job in QUIESCE:  # exercise 15 step 1; the barrier places no holds of its own
        _sendevent(night, "ON_HOLD", job=job)
    # the watched file's producer is iced, so the watch below never
    # completes and really does cross the boundary
    _sendevent(night, "ON_ICE", job=B_ICED)
    await _drive(night)
    for job in (B_BOX, B_HOLDER, B_WAITER, B_LONG, B_KILLED, B_DEFERRED):
        _sendevent(night, "FORCE_STARTJOB", job=job)
    await _drive(night)
    await _wait_for_evidence(
        night,
        lambda: all(
            (run_root / "runs" / f"{job}.1" / "spawn.json").exists()
            for job in (B_HOLDER, B_LONG, B_KILLED)
        ),
        "the supervisor's spool binding for every live command",
    )
    await _wait_for_evidence(
        night,
        lambda: (run_root / "runs" / f"{B_WATCH}.1" / "watch.jsonl").exists(),
        "the FW adapter's `start` line",
    )
    gate = None
    if park is not None:
        gate = _park_spawn(night, park)
        _sendevent(night, "FORCE_STARTJOB", job=park)
        await _drive(night)
    # LAST, so every start above is dispatched first: a drained host routes
    # no NEW effect (ss8), so the next SPAWN is held pending -- ss14 B1's
    # `pending_spawn` on a passive host
    engine.inject_host(HostCommand(verb="drain", host_id=engine.executor_id))
    await _drive(night)
    _sendevent(night, "FORCE_STARTJOB", job=B_HELD_SPAWN)
    await _drive(night)
    return gate


def _c2_sources(parsed, *, change: tuple[str, str] | None = None) -> list[SourceFile]:
    """C2's bytes: C1's, with at most ONE substring changed.

    ss14 B2 is "the same estate, one change at a time", so the edit must be
    unique across the whole estate -- an edit that hit two files would be
    two changes wearing one name."""
    sources = [SourceFile(path=jf.file, text=render_preserve(jf)) for jf in parsed]
    if change is None:
        return sources
    old, new = change
    hits = [source for source in sources if old in source.text]
    assert len(hits) == 1 and hits[0].text.count(old) == 1, f"{old!r} is not one edit"
    return [
        SourceFile(path=source.path, text=source.text.replace(old, new))
        if source is hits[0]
        else source
        for source in sources
    ]


def _stage_c2(night: _LiveNight, *, change: tuple[str, str] | None = None):
    """Stage C2 the way `dsl41 seal`'s live mode stages it: the immutable
    bundle first, then the two staged files under its digest.

    Returns the staged candidate AND the catalog it stages, because the
    opener in step 9 is `dsl41 run --resume` over C2's FILES -- an opener
    handed C1's catalog is refused by the hash gate, and rightly."""
    sources = _c2_sources(night.parsed, change=change)
    catalog = lower_catalog([parse(source.text, file=source.path) for source in sources])
    manifest = stage_manifest(
        catalog,
        source_bundle_hash=write_bundle(night.run_root, sources),
        profile=DETACHED,
        state_machine_version=STATE_MACHINE_VERSION,
    )
    return stage_next_period(night.run_root, staged_manifest=manifest), catalog


def _seal_request(night: _LiveNight, staged, **overrides) -> SealRequest:
    fields: dict[str, Any] = {
        "baseline_id": night.engine.baseline_id,
        "epoch": night.engine.epoch,
        "request_id": "r-nightbank-seal",
        "next_period": staged,
        "stage_digest": staged.stage_digest,
        "force_seal": False,
        "claimed_actor": "ops@nightbank",
    }
    return SealRequest(**{**fields, **overrides})


async def _seal_live(night: _LiveNight, request: SealRequest):
    """Ask the running engine for the boundary, as `dsl41 seal`'s live mode
    asks over the socket. Returns the committed boundary; a refusal is
    raised, and C1 is still open behind it."""
    future = night.engine.submit_seal(request)
    try:
        await night.engine.run_until_quiescent(night.engine.clock.now())
    except PeriodSealed as sealed:
        return sealed.boundary
    assert future.done(), "the loop returned without deciding the boundary"
    future.result()
    raise AssertionError("the boundary neither committed nor refused")


def _record_quiescence_reasons(night: _LiveNight) -> list[str]:
    """Record every reason the sealer gives for not being quiescent YET.

    ss8 says the sealer WAITS three of its clauses out rather than
    refusing, and "waited" is only observable from inside: from outside, a
    ladder waited out and a ladder that had already resolved leave the same
    committed seal."""
    reasons: list[str] = []
    engine = night.engine
    observed = engine._not_quiescent

    def recording(estate):
        reason = observed(estate)
        if reason is not None:
            reasons.append(reason)
        return reason

    engine._not_quiescent = recording
    return reasons


def _wal(night: _LiveNight) -> list[dict]:
    return read_journal(wal_path(night.run_root, 1))


def _record_committed_classification(monkeypatch) -> list:
    """Capture the ss10 map the boundary actually COMMITS.

    `validate_boundary` is where that map comes from (ss7 phase 2), and the
    seal keeps only its verdict-and-assumption projection -- which spells a
    job the classifier IGNORED and a job it saw and carried identically
    (`carry` both times). The changed node set and the per-job closures,
    which are what say the classifier did the work, exist only here."""
    captured: list = []
    original = boundary.validate_boundary

    def recording(ctx):
        verdict = original(ctx)
        captured.append(verdict)
        return verdict

    monkeypatch.setattr(boundary, "validate_boundary", recording)
    return captured


def _assert_c1_still_open(night: _LiveNight, *, was: list[dict], barrier: bool) -> None:
    """The estate half of ss14 B2: the refused seal moved nothing, and it
    is named WHICH of ss8's two refusal points answered.

    The two leave different logs. Readiness runs before the barrier and
    appends nothing at all: C1 is still open AND correct. A refusal after
    the cutoff leaves the cutoff's own admitted work, which is legitimate
    C1 activity and not damage (ss7's exit codes). A row that only asked
    for "no seal record" could not tell the two apart, and an R gate moved
    from phase 1 to phase 2 would pass it.

    EVERY B2 row runs this. `_assert_untouched` adds the half that needs
    live processes, which one row deliberately does not have."""
    assert isinstance(_head(night.run_root), OpenHead)
    assert not seal_path(night.run_root, 1).exists()
    # PR-28b: the abort ran inside the reversible interval, so the freeze is
    # lifted -- admission is open again and the FW tasks are unparked. A
    # refusal that left the barrier down would leave a live engine frozen
    # behind a freeze it would never lift
    assert night.engine.sealing is False
    assert night.engine.barrier.parked is False
    records = _wal(night)
    assert [record for record in records if record.get("rec") == "seal"] == []
    if barrier:
        assert len(records) > len(was)
    else:
        assert records == was


def _assert_untouched(night: _LiveNight, *, was: list[dict], barrier: bool) -> None:
    """`_assert_c1_still_open`, plus the live closure the refusal left
    alone: the long command still running under the supervisor -- the
    process, not just the row -- and the watch still LIVE.

    "Still watching" is the stronger claim and costs a poll interval, so
    exactly one B2 row pays for it, and it is the row that cannot use this
    helper at all (`test_b2_a_restarted_supervisor_cannot_prove_the_seal`:
    the restart took the wrappers, so the watch is the only thing left
    alive and the process checks below are dead there by construction)."""
    _assert_c1_still_open(night, was=was, barrier=barrier)
    store = night.engine.oracle.store
    assert store.runtime(B_LONG).status == "RUNNING"
    assert store.runtime(B_WATCH).status == "RUNNING"
    assert {B_LONG, B_WATCH} <= night.engine.live_jobs()
    spawn = json.loads((night.run_root / "runs" / f"{B_LONG}.1" / "spawn.json").read_text())
    os.kill(spawn["command_pid"], 0)  # the process itself, not just the row


def _watch_lines(run_root: Path, job: str) -> list[dict]:
    path = run_root / "runs" / f"{job}.1" / "watch.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line]


async def _reopen_detached(night: _LiveNight, catalog):
    """ss7 step 9's in-place opener, detached: stop the sealed engine the
    way `dsl41 run` stops it, then resume on the same root over C2's
    catalog. The night's engine and wiring become C2's."""
    await _stop_engine(night)
    wiring = await wire_from_profile(night.run_root, catalog, DETACHED)
    engine = await resume_run(
        catalog,
        night.run_root,
        clock=RealClock(),
        adapters=wiring.adapters,
        scheduler=wiring.scheduler,
        supervisor=wiring.client,
    )
    night.wiring, night.engine = wiring, engine
    return engine


# ------------------------------------- ss14 B1: the live boundary commits


def test_b1_the_boundary_commits_over_a_night_in_flight(night_base: Path, monkeypatch) -> None:
    """period-model ss14 B1, as one walk over the worked estate.

    The night is detached and mid-flight: a long command live under a real
    supervisor (PR-31), a KILL ladder in flight the sealer waits out
    (PR-33a), an unchanged FW watch crossing (PR-34), a live box with an
    INACTIVE member (PR-42's carry half), a QUE_WAIT pair (PR-21), an
    INACTIVE job with a semantic timer (PR-41) and a `pending_spawn` on a
    passive host (PR-32). C2 touches NONE of it, so the R gate passes and
    the boundary commits over live work -- forced, because an external
    attempt is seconds old (PR-30) -- and period 2 opens in place, carries
    every one of those, and refuses the retry composed under C1 (PR-06).

    The exactly-T half of B1's row is its own scenario below: T is
    `clock.now()` at the barrier, so a timer due AT it is arranged in the
    virtual domain, where the instant is a choice rather than an accident.
    """

    async def scenario() -> None:
        night = await _start_detached_night(night_base)
        run_root = night.run_root
        try:
            await _arrange_live_closure(night)
            store = night.engine.oracle.store
            # the closure ss14 asks for, before any of it is sealed
            assert store.runtime(B_BOX).status == "RUNNING"
            assert store.runtime(B_MEMBER).status == "INACTIVE"
            assert store.runtime(B_WATCH).status == "RUNNING"
            assert store.runtime(B_HOLDER).status == "RUNNING"
            assert store.runtime(B_WAITER).status == "QUE_WAIT"
            assert store.runtime(B_LONG).status == "RUNNING"
            assert store.runtime(B_DEFERRED).status == "INACTIVE"
            assert [job for _, job, _ in night.engine.oracle.pending_timers()] == [B_DEFERRED]

            # ss9 measures the boundary against the last EXTERNALLY requested
            # attempt, and only the external door carries an `expect`: this is
            # the runbook's own recon gate, sent as an operator would send it
            recon = {
                "v": PROTOCOL_VERSION,
                "baseline_id": night.engine.baseline_id,
                "epoch": night.engine.epoch,
                "request_id": "r-recon-apac",
                "expect": {"global:RECON_APAC": store.revision("global:RECON_APAC")},
                "claimed_actor": "ops@nightbank",
            }
            envelope = parse_envelope(
                recon, addressed="global:RECON_APAC", baseline_id=night.engine.baseline_id
            )
            decided = night.engine.submit(
                Event(
                    at=night.engine.clock.now(),
                    kind="SET_GLOBAL",
                    payload={"name": "RECON_APAC", "value": "CLEAN"},
                ),
                envelope,
            )
            await _drive(night)
            assert (await decided).decision == "applied"

            # ss14: C2 touches none of the LIVE closure -- and does touch one
            # job outside it, so the R gate classifies a moved graph and its
            # "passes" is evidence rather than an absence of input
            staged, c2 = _stage_c2(night, change=B_CHANGE_OUTSIDE)
            assert staged.catalog_hash != read_period_manifest(run_root, 1).catalog_hash
            was = _wal(night)
            with pytest.raises(EngineError, match="retry_horizon_us"):
                await _seal_live(night, _seal_request(night, staged))
            # refusing is C1's business, and C1 carries on. ss9's gate reads
            # the WAL after the cutoff, so the cutoff's own work is there
            _assert_untouched(night, was=was, barrier=True)

            reasons = _record_quiescence_reasons(night)
            committed = _record_committed_classification(monkeypatch)
            # the ladder is decided in the SAME turn the boundary is asked
            # for, so the barrier's own drain applies the KILL and the
            # sealer meets a ladder that has not resolved (ss8, PR-33a)
            _sendevent(night, "KILLJOB", job=B_KILLED)
            boundary = await _seal_live(
                night,
                _seal_request(night, staged, request_id="r-forced-seal", force_seal=True),
            )
            seal = boundary.seal
            assert seal.digest == read_seal(run_root, 1).digest
            assert isinstance(_head(run_root), ClosedHead)

            # PR-30: forced, and the log alone says so
            assert seal.boundary_request.force_seal is True
            assert boundary.record["force_seal"] is True
            assert seal.forced_gate is not None
            assert seal.forced_gate.gate == "retry_horizon"

            # ss10, from the map the boundary COMMITTED: the classifier saw
            # C2's change, and its closure reached no live row. The seal's
            # own projection cannot say this -- `carry` is what a classifier
            # that IGNORED the change would record for every job here too
            assert seal.next_period.catalog_hash == staged.catalog_hash
            assert len(committed) == 1
            seen = committed[0]
            assert seen.changed_nodes == (f"job:{B_OUTSIDE}",)  # exactly one node moved
            assert B_OUTSIDE in seen.changed_not_live  # not live, and its closure moved
            assert seen.by_job[B_OUTSIDE].changed == (f"job:{B_OUTSIDE}",)
            assert seen.by_job[B_OUTSIDE].tier == "not_live"
            assert seen.refused == ()
            live_rows = (B_LONG, B_HOLDER, B_WAITER, B_WATCH, B_BOX, B_MEMBER, B_HELD_SPAWN)
            # ... and NO live-tier job's forward closure contains a changed
            # node -- every `executing` and `latent` verdict, not a
            # hand-picked list: a latent job this list forgot (B_DEFERRED
            # holds a live timer) whose closure reached the change would
            # commit as an A-assumption unseen. This is the reason the gate
            # passed rather than a restatement of the fact that it did.
            live_tiered = {
                job for job, v in seen.by_job.items() if v.tier in ("executing", "latent")
            }
            assert set(live_rows) <= live_tiered
            assert B_DEFERRED in live_tiered  # the latent row the list forgot
            assert {job: seen.by_job[job].changed for job in live_tiered} == dict.fromkeys(
                live_tiered, ()
            )
            assert {job: seal.classification[job].verdict for job in live_rows} == dict.fromkeys(
                live_rows, "carry"
            )

            # PR-33a: the sealer WAITED the ladder out and the estate carries
            # its spool proof, not a half-run ladder
            assert any("KILL ladder(s) have not resolved" in reason for reason in reasons)
            assert seal.state.jobs[B_KILLED].status == "TERMINATED"
            assert [entry for entry in seal.executions if entry.job == B_KILLED] == []
            killed = json.loads((run_root / "runs" / f"{B_KILLED}.1" / "status.json").read_text())
            assert killed["outcome"] == "signaled"
            assert [effect for effect in seal.outbox_pending if effect.kind == "KILL"] == []

            carried = {entry.job: entry for entry in seal.executions}
            # PR-31/PR-32: the long command is bound, and the seal names its
            # executor, run_id and generation from its own evidence
            bound = carried[B_LONG]
            spawn = json.loads((run_root / "runs" / f"{B_LONG}.1" / "spawn.json").read_text())
            assert bound.kind == "bound" and bound.run_number == 1
            assert bound.run_id == spawn["run_id"]
            assert bound.run_dir == f"runs/{B_LONG}.1"
            assert bound.executor_id == night.engine.executor_id
            assert carried[B_HOLDER].kind == "bound"

            # PR-34: the crossing watch is a pure function of the first
            # `watch_seq` lines, and ss3.5's two timestamps decide next_poll_at
            watch = carried[B_WATCH]
            prefix = _watch_lines(run_root, B_WATCH)[: watch.watch_seq]
            assert watch.kind == "fw_watch" and watch.watch_seq >= 1
            assert prefix[0]["kind"] == "start" and prefix[0]["run_id"] == watch.run_id
            polls = [line for line in prefix if line["kind"] == "poll"]
            interval = night.catalog.jobs[B_WATCH].exec_.watch_interval
            expected = (
                datetime.fromisoformat(prefix[0]["at"])
                if not polls
                else datetime.fromisoformat(polls[-1]["at"]) + timedelta(seconds=interval)
            )
            assert watch.next_poll_at == expected

            # PR-42's carry half: the box is live and its INACTIVE member is
            # carried untouched -- and a live BOX has no execution entry
            assert seal.state.jobs[B_BOX].status == "RUNNING"
            assert seal.state.jobs[B_MEMBER].status == "INACTIVE"
            assert B_BOX not in carried

            # PR-21: waiter ORDER survives, not just the status
            assert seal.state.jobs[B_HOLDER].reservations != ()
            assert seal.state.jobs[B_WAITER].status == "QUE_WAIT"
            waiter_seq = seal.state.jobs[B_WAITER].waiter_seq
            assert waiter_seq is not None

            # PR-41: an INACTIVE row with a live timer is latent intent, and
            # the timer is carried on the seal rather than in the process
            assert seal.state.jobs[B_DEFERRED].status == "INACTIVE"
            deferred = [event for _, _, event in seal.state.timers]
            assert [event.payload["job"] for event in deferred] == [B_DEFERRED]
            assert [due for due, _, _ in seal.state.timers][0] > seal.closed_at

            # PR-32/PR-16c: the held intent crosses as an intent
            held = carried[B_HELD_SPAWN]
            assert held.kind == "pending_spawn"
            assert [effect.effect_id for effect in seal.outbox_pending] == [held.effect_id]
            assert seal.state.hosts[held.executor_id].state == "passive"

            # ---- period 2 opens in place and inherits the whole closure
            opened = await _reopen_detached(night, c2)
            after = opened.oracle.store
            assert opened.baseline_id == seal.next_period.baseline_id
            assert after.runtime(B_LONG).status == "RUNNING"
            assert after.runtime(B_LONG).run_number == 1
            assert after.runtime(B_LONG).start_period == 1  # PR-50: still C1's run
            assert B_LONG in opened.live_jobs()  # reattached, not respawned
            assert not (run_root / "runs" / f"{B_LONG}.2").exists()
            os.kill(spawn["command_pid"], 0)  # the pid is still there ...
            # ... and the supervisor holds ONE wrapper for it, not a second
            # one the boundary spawned beside the run it carried
            listed = await night.wiring.client.list_runs()
            mine = [row for row in listed["runs"] if row["job"] == B_LONG]
            assert [row["run_id"] for row in mine] == [spawn["run_id"]]
            assert after.runtime(B_BOX).status == "RUNNING"
            assert after.runtime(B_MEMBER).status == "INACTIVE"
            assert after.runtime(B_HOLDER).status == "RUNNING"
            assert after.runtime(B_HOLDER).reservations != ()
            assert after.runtime(B_WAITER).status == "QUE_WAIT"
            assert after.runtime(B_WAITER).waiter_seq == waiter_seq
            assert [job for _, job, _ in opened.oracle.pending_timers()] == [B_DEFERRED]
            assert [effect.job for effect in opened.outbox.pending()] == [B_HELD_SPAWN]
            # the watch resumes as ONE watch: C2's lines append after the
            # prefix the seal named, and no second `start` is ever written
            await _wait_for_evidence(
                night,
                lambda: len(_watch_lines(run_root, B_WATCH)) > watch.watch_seq,
                "a C2 poll after the sealed prefix",
            )
            lines = _watch_lines(run_root, B_WATCH)
            assert [line["kind"] for line in lines].count("start") == 1
            assert lines[: watch.watch_seq] == prefix
            assert after.runtime(B_WATCH).status == "RUNNING"

            # PR-06: the retry composed under C1 is refused after C2 opens,
            # even though nothing it addresses moved
            with pytest.raises(EnvelopeError, match="is not this run's"):
                parse_envelope(recon, addressed="global:RECON_APAC", baseline_id=opened.baseline_id)

            # ss11: the period re-derives from its own evidence, with C2's
            # watch lines already appended past the prefix. The engine is
            # stopped first for the reason `audit` prints otherwise -- a
            # live lineage lock leaves the registry row outstanding -- and
            # a detach-stop leaves the wrappers, so the spool is untouched
            await _stop_engine(night)
            attested = _invoke("audit", "--run-root", str(run_root), "--period", "1")
            assert attested.exit_code == 0, attested.output
            assert "period 1 attested" in attested.output
            assert attestation_path(run_root, 1).exists()
        finally:
            await _end_night(night)

    asyncio.run(scenario())


# --------------------------------------- ss14 B1: the cutoff instant itself


#: ss14 B1's "two timers due at exactly T". T is `clock.now()` at the
#: barrier, so the instant is a choice only in the virtual domain -- which
#: is where the estate's own `must_complete_times: "+20"` region boxes are
#: armed here. A box carries no execution entry (ss3.5), so this scenario
#: needs no spool and no supervisor: it is about the cutoff and nothing else.
#: In ARMING order, and deliberately not in name order: the first two share
#: an instant, so an assertion on their firing order would be satisfied by
#: either tie-break if the two happened to sort the way they were armed.
B_DEADLINE_BOXES = ("EMEA_EOD_B", "APAC_EOD_B", "AMER_EOD_B")


def _deadline_minutes(catalog) -> int:
    """The one relative `must_complete_times` offset the three region boxes
    share, read off the estate rather than repeated here: the scenario's
    arithmetic is the estate's number, so an estate that changed it fails
    with a name instead of missing its own cutoff by minutes."""
    offsets = set()
    for box in B_DEADLINE_BOXES:
        schedule = catalog.jobs[box].schedule
        assert schedule is not None and schedule.must_complete is not None
        assert schedule.must_complete.kind == "relative"
        offsets.update(schedule.must_complete.offsets_min or ())
    assert len(offsets) == 1, f"the region boxes disagree on their deadline: {sorted(offsets)}"
    return offsets.pop()


def _region_workers(catalog) -> list[str]:
    """Every non-box job inside a region box.

    Held, they keep their boxes RUNNING without starting anything, so the
    only thing this scenario has to reason about is the deadline."""
    workers = []
    for name, job in catalog.jobs.items():
        parent, seen = job.box.box_name, set()
        while parent is not None and parent not in seen:
            seen.add(parent)
            if parent in B_DEADLINE_BOXES:
                if job.job_type != "BOX":
                    workers.append(name)
                break
            parent = catalog.jobs[parent].box.box_name if parent in catalog.jobs else None
    return sorted(workers)


def test_b1_two_timers_due_at_exactly_t_are_c1s_and_the_next_one_is_c2s(
    night_base: Path,
) -> None:
    """period-model ss6 steps 4-5 over the worked estate: the cutoff
    advances the oracle THROUGH T, firing every timer due at or before it,
    and C2 owns everything after.

    The rule, not the accident: two of the estate's region boxes are armed
    so their `must_complete_times: "+20"` deadlines fall on T exactly, and
    the third one minute later. The two fire inside C1 and leave the
    carried timer set; the third is carried unfired and fires in C2. A
    cutoff that reset EXCLUSIVE of T would lose the first two, and one that
    reached past T would consume the third."""
    run_root, props = _night(night_base)
    run_root.mkdir(parents=True, exist_ok=True)
    catalog, parsed = _load(props)
    sources = [SourceFile(path=jf.file, text=render_preserve(jf)) for jf in parsed]
    staged_manifest = stage_manifest(
        catalog,
        source_bundle_hash=write_bundle(run_root, sources),
        profile=RuntimeProfile(),
        state_machine_version=STATE_MACHINE_VERSION,
    )
    # a whole minute, so "+20" lands on an instant the clock can hold exactly
    t0 = datetime.now(UTC).replace(tzinfo=None, second=0, microsecond=0)
    cutoff = t0 + timedelta(minutes=_deadline_minutes(catalog))
    engine = start_run(
        catalog,
        run_root,
        clock=VirtualClock(start=t0),
        adapters={"CMD": FakeAdapter(default=None), "FW": FakeAdapter(default=None)},
        scheduler=Scheduler(catalog, start=t0, default_tz="UTC"),
        staged=staged_manifest,
    )

    def inject(kind: str, job: str) -> None:
        engine.inject(Event(at=engine.clock.now(), kind=kind, payload={"job": job}))

    async def scenario():
        for job in (*_region_workers(catalog), *QUIESCE):
            inject("ON_HOLD", job)
        await engine.run_until_quiescent(t0)
        for box in B_DEADLINE_BOXES[:2]:
            inject("FORCE_STARTJOB", box)
        await engine.run_until_quiescent(t0)
        # the third box one minute later: its deadline is T + 1min
        await engine.clock.wait_until(t0 + timedelta(minutes=1))
        inject("FORCE_STARTJOB", B_DEADLINE_BOXES[2])
        await engine.run_until_quiescent(engine.clock.now())
        assert [(due, job) for due, job, _ in engine.oracle.pending_timers()] == [
            (cutoff, B_DEADLINE_BOXES[0]),
            (cutoff, B_DEADLINE_BOXES[1]),
            (cutoff + timedelta(minutes=1), B_DEADLINE_BOXES[2]),
        ]
        # to T and not one microsecond past: a timer due AT `now` is held
        # lazy by the frontier rule, so the clock reaches T with all three
        # still armed and the CUTOFF is what fires the two
        await engine.clock.wait_until(cutoff)
        assert len(engine.oracle.pending_timers()) == 3
        staged = stage_next_period(run_root, staged_manifest=staged_manifest)
        request = SealRequest(
            baseline_id=engine.baseline_id,
            epoch=engine.epoch,
            request_id="r-cutoff-seal",
            next_period=staged,
            stage_digest=staged.stage_digest,
            force_seal=False,
            claimed_actor="ops@nightbank",
        )
        engine.submit_seal(request)
        with pytest.raises(PeriodSealed) as sealed:
            await engine.run_until_quiescent(engine.clock.now())
        return sealed.value.boundary

    try:
        boundary = asyncio.run(scenario())
    finally:
        asyncio.run(engine.shutdown())
        if engine.journal is not None:
            engine.journal.close()
    seal = boundary.seal

    assert seal.closed_at == cutoff and seal.state.now == cutoff
    assert seal.scheduler_admitted_through == cutoff
    # the two due AT T fired inside C1 and are gone from the carried set
    alarms = [
        entry.job
        for entry in engine.oracle.trace()
        if entry.transition == "MUST_COMPLETE_ALARM" and entry.at == cutoff
    ]
    assert alarms == list(B_DEADLINE_BOXES[:2])
    # the one due after T is carried unfired, and it is the only one left
    assert [(due, event.payload["job"]) for due, _, event in seal.state.timers] == [
        (cutoff + timedelta(minutes=1), B_DEADLINE_BOXES[2])
    ]
    # ... and C2 is what fires it: the next period opens strictly after T
    catalog2, _ = _load(props)
    opened = asyncio.run(
        resume_run(
            catalog2,
            run_root,
            clock=VirtualClock(start=cutoff),
            adapters={"CMD": FakeAdapter(default=None), "FW": FakeAdapter(default=None)},
            scheduler=Scheduler(catalog2, start=cutoff, default_tz="UTC"),
        )
    )
    try:
        asyncio.run(opened.run_until_quiescent(cutoff + timedelta(minutes=2)))
        assert [
            entry.job
            for entry in opened.oracle.trace()
            if entry.transition == "MUST_COMPLETE_ALARM"
        ] == [B_DEADLINE_BOXES[2]]
    finally:
        asyncio.run(opened.shutdown())
        if opened.journal is not None:
            opened.journal.close()


# ------------------------------ ss14 B2: the same estate, refusing, one
# ------------------------------ change at a time


#: ss14's C2 edits. Each is ONE substring, unique across the estate, and
#: each changes exactly one job's command -- which is what moves that job's
#: fingerprint and nothing else (ss10.2's job node). The first is B1's,
#: outside every live closure; the other two are B2's, inside one.
B_CHANGE_OUTSIDE = (
    f"fakework {B_OUTSIDE} --sleep 5",
    f"fakework {B_OUTSIDE} --sleep 6",
)
B_CHANGE_HELD_SPAWN = (
    f"fakework {B_HELD_SPAWN} --sleep 8",
    f"fakework {B_HELD_SPAWN} --sleep 9",
)
B_CHANGE_MEMBER = (
    f"fakework {B_MEMBER} --sleep 14",
    f"fakework {B_MEMBER} --sleep 15",
)


def test_b2_a_changed_pending_spawn_command_refuses_and_moves_nothing(
    night_base: Path,
) -> None:
    """ss14 B2 row 1 (PR-39a): C2 changes the command of the job whose
    SPAWN is held on the passive host.

    `pending_spawn` is EXECUTING, not latent (ss10.1): the effect carries
    no frozen command, so opening without the R gate would run C2's command
    under C1's run number the moment the host came back."""

    async def scenario() -> None:
        night = await _start_detached_night(night_base)
        try:
            await _arrange_live_closure(night)
            assert night.engine.oracle.store.runtime(B_HELD_SPAWN).status == "RUNNING"
            staged, _ = _stage_c2(night, change=B_CHANGE_HELD_SPAWN)
            was = _wal(night)
            with pytest.raises(EngineError, match=f"classification refuses.*{B_HELD_SPAWN}"):
                await _seal_live(night, _seal_request(night, staged))
            _assert_untouched(night, was=was, barrier=False)
            # and the intent is still an intent, still held, still C1's
            assert [effect.job for effect in night.engine.outbox.pending()] == [B_HELD_SPAWN]
        finally:
            await _end_night(night)

    asyncio.run(scenario())


def test_b2_a_changed_member_of_a_live_box_refuses_and_moves_nothing(
    night_base: Path,
) -> None:
    """ss14 B2 row 2 (PR-42's R half): C2 changes the INACTIVE member of a
    box that is executing.

    A member of an executing box is executing whatever its own row says
    (ss10.3, E19): classify it A and the member starts under C2 inside the
    box's C1 execution."""

    async def scenario() -> None:
        night = await _start_detached_night(night_base)
        try:
            await _arrange_live_closure(night)
            store = night.engine.oracle.store
            assert store.runtime(B_BOX).status == "RUNNING"
            assert store.runtime(B_MEMBER).status == "INACTIVE"
            staged, _ = _stage_c2(night, change=B_CHANGE_MEMBER)
            was = _wal(night)
            with pytest.raises(EngineError, match=f"classification refuses.*{B_MEMBER}"):
                await _seal_live(night, _seal_request(night, staged))
            _assert_untouched(night, was=was, barrier=False)
            assert store.runtime(B_MEMBER).status == "INACTIVE"  # not started by the attempt
        finally:
            await _end_night(night)

    asyncio.run(scenario())


def test_b2_a_restarted_supervisor_cannot_prove_the_seal(night_base: Path) -> None:
    """ss14 B2 row 3 (PR-27): the supervisor is restarted before the seal,
    so its LIST is empty.

    `LIST` shows what THIS incarnation spawned. The engine re-leases the
    restarted supervisor here, exactly as a reattaching operator would, so
    the clause that answers is the RECONCILIATION one -- a carried bound run
    that the leased incarnation's LIST does not account for -- and not the
    incarnation-mismatch clause beside it, which `test_pr27_*` in
    tests/test_boundary.py owns. An empty history is not proof either way."""

    async def scenario() -> None:
        night = await _start_detached_night(night_base)
        try:
            await _arrange_live_closure(night)
            carried = json.loads(
                (night.run_root / "runs" / f"{B_LONG}.1" / "spawn.json").read_text()
            )
            # C2 changes NOTHING here: the LIST is the whole objection
            staged, _ = _stage_c2(night)
            was = _wal(night)
            client = night.wiring.client
            leased = client.incarnation
            _kill_group(night.run_root)  # the operator's restart: -9 and come back
            await client.ensure_running()
            await client.acquire()
            # the fixture's own check that a restart really happened; the
            # engine's objection below is about the LIST, not this value
            assert client.incarnation is not None and client.incarnation != leased
            with pytest.raises(EngineError, match="leased incarnation's LIST"):
                await _seal_live(night, _seal_request(night, staged))
            assert (await client.list_runs())["runs"] == []
            # the ESTATE is untouched, and the supervisor proof is ss6 step
            # 7's -- AFTER the cutoff -- so C1 keeps the cutoff's own
            # admitted work. `_assert_untouched` is the wrong helper here on
            # purpose: the restart took the wrappers with it, which is
            # exactly why the boundary must not commit over their rows
            _assert_c1_still_open(night, was=was, barrier=True)
            assert night.engine.oracle.store.runtime(B_LONG).status == "RUNNING"
            assert night.engine.oracle.store.runtime(B_LONG).run_number == carried["run_number"]
            # the watch is in-engine and owned by no supervisor, so it is the
            # one thing the restart left alive -- and this row is where ss14's
            # "the watch still watches" is taken literally: another durable
            # line after the refusal, not just a live task
            seen = len(_watch_lines(night.run_root, B_WATCH))
            await _wait_for_evidence(
                night,
                lambda: len(_watch_lines(night.run_root, B_WATCH)) > seen,
                "another FW poll after the refusal",
            )
        finally:
            await _end_night(night)

    asyncio.run(scenario())


def test_b2_an_applied_spawn_with_no_binding_refuses(night_base: Path) -> None:
    """ss14 B2 row 4 (PR-27): an applied SPAWN whose adapter task has not
    yet written `spawn.json`.

    ss8 requires every applied CMD SPAWN to be bound or terminal before the
    seal commits, and ss3.5 refuses to invent a fourth execution kind for
    the window in between. The sealer waits it out and then refuses -- so
    the wait is real here, and the bound is what ends it."""

    async def scenario() -> None:
        night = await _start_detached_night(night_base)
        try:
            gate = await _arrange_live_closure(night, park=B_UNBOUND)
            assert not (night.run_root / "runs" / f"{B_UNBOUND}.1" / "spawn.json").exists()
            assert night.engine.oracle.store.runtime(B_UNBOUND).status == "RUNNING"
            # the wait is real and the bound is what refuses. Two seconds and
            # not fifty milliseconds: `QUIESCE_WAIT_S` is engine-wide, so the
            # barrier's three drain passes read the same number, and a value
            # tight enough to be crossed by ordinary socket I/O would refuse
            # with the drain's message instead of the bound's
            night.engine.QUIESCE_WAIT_S = 2.0
            staged, _ = _stage_c2(night)
            was = _wal(night)
            with pytest.raises(EngineError, match=f"{B_UNBOUND}.1: an applied SPAWN"):
                await _seal_live(night, _seal_request(night, staged))
            # the bound is met AFTER the cutoff, so C1 keeps the cutoff's work
            _assert_untouched(night, was=was, barrier=True)
            # release the window and the run binds: what refused was the
            # unbound instant, not the run
            gate.set()
            await _wait_for_evidence(
                night,
                lambda: (night.run_root / "runs" / f"{B_UNBOUND}.1" / "spawn.json").exists(),
                "the binding the seal waited for",
            )
        finally:
            await _end_night(night)

    asyncio.run(scenario())


# ---------------------------------------------- the RUNBOOK is a contract


def test_every_dsl41_verb_the_runbook_types_exists() -> None:
    """The RUNBOOK is a contract, the same way the job names in it are
    (`test_runbook_job_names_exist_in_an_estate`).

    A renamed CLI verb is a refactor nobody thinks of as a documentation
    change, and an exercise that opens with a command that does not exist
    is worse than no exercise. So: every `dsl41 <verb>` the runbook types
    must resolve to a command this build has -- and where the verb is a
    GROUP, its subcommand is resolved too, or `estate prune` would pass on
    the strength of `estate` alone."""
    text = (NB / "RUNBOOK.md").read_text()
    typed = {
        (match.group(1), match.group(2))
        # `\s+` and not a literal space: prose wraps `dsl41 estate` and its
        # subcommand across a line, and a group's subcommand is the half
        # this test exists to check
        for match in re.finditer(r"\bdsl41\s+([a-z][a-z-]*)(?:\s+([a-z][a-z-]*))?", text)
    }
    top = {command.name or command.callback.__name__ for command in app.registered_commands}
    groups = {
        group.name: {
            command.name or command.callback.__name__
            for command in group.typer_instance.registered_commands
        }
        for group in app.registered_groups
    }
    missing = sorted(
        f"{verb} {sub}" if verb in groups else verb
        for verb, sub in typed
        if verb not in top and (verb not in groups or sub not in groups[verb])
    )
    assert not missing, f"RUNBOOK types verbs this build has no command for: {missing}"
    # the extraction itself must keep working, and it must reach the
    # boundary-era exercises rather than only the ones that came before
    verbs = {verb for verb, _ in typed}
    assert {"run", "query", "sendevent", "seal", "audit", "verify"} <= verbs
    assert {("estate", "reclaim"), ("estate", "prune")} <= typed
