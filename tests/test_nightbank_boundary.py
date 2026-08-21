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
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from typer.testing import CliRunner

from dsl41.ast_jil import parse, render_preserve
from dsl41.boundary import (
    ClaimedHead,
    ClosedHead,
    EstateAnchor,
    OpenHead,
    default_anchor_dir,
    read_seal,
)
from dsl41.cli import app
from dsl41.ir import lower_catalog
from dsl41.oracle_state import Event
from dsl41.period import (
    RuntimeProfile,
    SourceFile,
    attestation_path,
    seal_path,
    stage_manifest,
    wal_path,
    write_bundle,
)
from dsl41.placeholders import load_properties, substitute
from dsl41.runner_adapters import FileWatcherAdapter, LocalCommandAdapter
from dsl41.runner_clock import RealClock
from dsl41.runner_journal import read_journal
from dsl41.runner_ledger import STATE_MACHINE_VERSION
from dsl41.runner_scheduler import Scheduler
from dsl41.runner_startup import resume_run, start_run

from test_nightbank_example import NB, SMALL, _launcher
from test_runner_leadership import cli, engine, wait_for

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
