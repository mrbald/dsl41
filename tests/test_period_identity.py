"""Period-identity tests (period-model ss1.1/ss2.1, DL-130).

Normative spec: `docs/period-model.md` ss1.1 (estate layout,
`source_bundle_hash`, `catalog_hash` v2) and ss2.1 (the `segment` record,
`RuntimeProfile`, the two manifests), with obligations PR-07a, PR-08a,
PR-08c, PR-15, PR-15a and PR-22's U4 half in ss13.

House style follows test_canon.py: the golden vectors pin EXACT bytes and
EXACT digests as literals, because equality and sensitivity tests alone
would pass an implementation that is consistently wrong. The PR-15 sweep is
DERIVED from `RuntimeProfile.model_fields`, so a field added later is tested
by default rather than by somebody remembering to name it (the DL-83
discipline).

DL-138 retired the pre-DL-130 read dialects, so the `legacy_twin` fixture
that built one and the tests over it are gone. What replaces them is the
D4 dispatcher's roster below: `catalog_hash_version` 1 refused BY NAME
through both the journal reader and journal creation, and an unknown
version refused as its own distinct error.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import inspect
import shutil
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from dsl41.canon import canonical_bytes
from dsl41.cli import _observed_profile, _stage_period, app
from dsl41.ir import CatalogIR, CatalogMeta, lower_catalog, lower_source
from dsl41.period import (
    CATALOG_HASH_VERSION,
    CMD_GRACE_S,
    EMPTY_BUNDLE_HASH,
    FW_DEFAULT_INTERVAL_S,
    SPAWN_WINDOW_S,
    Manifest,
    RuntimeProfile,
    SourceFile,
    StagedManifest,
    bundle_dir,
    bundle_source_paths,
    catalog_hash_at,
    catalog_hash_for,
    genesis_manifest,
    catalog_hash_v2,
    check_manifest_against_segment,
    is_hash_address,
    period_dir,
    read_period_manifest,
    runtime_hash,
    runtime_profile_from_cli,
    segment_record,
    source_bundle_hash,
    stage_manifest,
    write_bundle,
    write_period_manifest,
)
from dsl41.ast_jil import parse
from dsl41.period import _SHARED_FIELDS
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_clock import EngineError, VirtualClock
from dsl41.runner_history import read_run_root
from dsl41.runner_journal import read_journal
from dsl41.runner_startup import resume_run, start_run
from test_runner_leadership import engine

T0 = datetime(2026, 7, 1, 8, 0)
_SOLO_JIL = "insert_job: j1\njob_type: c\ncommand: echo hi\nmachine: m1\n"


# ------------------------------------------------------------------ helpers


@pytest.fixture
def short_root():
    """A short base directory for the AF_UNIX control socket the real
    engine binds: pytest's tmp_path overruns sun_path's limit once
    `run/control.sock` is appended -- test_canon.py keeps the same fixture
    for the same reason."""
    directory = tempfile.mkdtemp(prefix="dsl41period-", dir="/tmp")
    try:
        yield Path(directory)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _catalog(text: str = _SOLO_JIL, *, file: str = "estate.jil") -> CatalogIR:
    return lower_source(text, file=file)


def _start(run_root: Path, catalog: CatalogIR, *, staged: StagedManifest | None = None):
    return start_run(
        catalog,
        run_root,
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},
        staged=staged,
    )


async def _resume(run_root: Path, catalog: CatalogIR, *, at: datetime = T0):
    return await resume_run(
        catalog,
        run_root,
        clock=VirtualClock(start=at),
        adapters={"CMD": FakeAdapter(default=None)},
    )


def _close(engine) -> None:
    assert engine.journal is not None
    engine.journal.close()


def _retired_v1(catalog: CatalogIR) -> str:
    """The RETIRED `catalog_hash` v1 recipe: a bare hexdigest over the whole
    model, `meta` included. DL-138 deleted it from the product, so the tests
    that still need to show what a v1 root held spell it here."""
    return hashlib.sha256(catalog.model_dump_json().encode("utf-8")).hexdigest()


# ------------------------------------------------- 1. catalog_hash v2 (PR-08a)


def _golden_catalog() -> CatalogIR:
    """A catalog with `source_files`, a non-null `tool_version`, a non-null
    `parsed_at` and a span -- exactly what PR-08a asks the vector to
    exercise."""
    catalog = _catalog()
    return catalog.model_copy(
        update={
            "meta": CatalogMeta(
                source_files=["estate.jil"],
                tool_version="1.2.3",
                parsed_at="2026-08-20T00:00:00",
            )
        }
    )


GOLDEN_CATALOG_BYTES = (
    b'{"calendars":{},"cycles":{},"external_instances":{},"globals_declared":{},'
    b'"ir_version":"0.2","jobs":{"j1":{"annotations":{},"box":{"box_name":null,'
    b'"box_terminator":false,"job_terminator":false},"exec_":{"command":"echo hi",'
    b'"envvars":null,"kind":"cmd","machine":"m1","owner":null,"profile":null,'
    b'"std_err_file":null,"std_in_file":null,"std_out_file":null},"job_type":"CMD",'
    b'"name":"j1","passthrough":{},"resources":[],"schedule":null,'
    b'"sem":{"auto_hold":false,"box_failure":null,"box_success":null,"condition":null,'
    b'"fail_codes":null,"initial_status":null,"max_exit_success":0,"n_retrys":0,'
    b'"success_codes":null,"term_run_time_min":null},'
    b'"span":{"byte_end":55,"byte_start":0,"file":"estate.jil","line_end":4,'
    b'"line_start":1},"var_sites":[]}},"machines":{},'
    b'"meta":{"source_files":["estate.jil"]},"resources":{}}'
)
GOLDEN_CATALOG_HASH_V2 = "sha256:1ab5aefe06485a8c60af7b0af4d2112d895d035e09ef09e785d507ebe34bc2df"


def test_pr08a_catalog_hash_v2_golden_vector() -> None:
    """The bytes themselves are the assertion: `meta` projected to
    `{source_files}`, spans in, keys sorted at every depth."""
    catalog = _golden_catalog()
    payload = catalog.model_dump(mode="json")
    payload["meta"] = {"source_files": ["estate.jil"]}
    assert canonical_bytes(payload) == GOLDEN_CATALOG_BYTES
    assert catalog_hash_v2(catalog) == GOLDEN_CATALOG_HASH_V2
    assert catalog_hash_v2(catalog) == "sha256:" + hashlib.sha256(GOLDEN_CATALOG_BYTES).hexdigest()


def test_pr08a_tool_version_and_parsed_at_do_not_move_v2() -> None:
    """The whole reason for v2 (ss1.1): a patch release moves
    `tool_version`, and under the retired v1 recipe -- which hashed the
    whole model -- a seal committed by 1.2.3 could never be opened by
    1.2.4, an outage manufactured by bookkeeping (DL-100). The recipe is
    spelled HERE, because DL-138 deleted it from the product."""
    catalog = _golden_catalog()
    patched = catalog.model_copy(
        update={
            "meta": CatalogMeta(
                source_files=["estate.jil"],
                tool_version="1.2.4",
                parsed_at="2026-08-21T09:15:00",
            )
        }
    )
    assert catalog_hash_v2(patched) == catalog_hash_v2(catalog)
    assert _retired_v1(patched) != _retired_v1(catalog)


def test_v2_still_moves_for_a_source_file_list_and_for_a_span() -> None:
    """Only the two diagnostic keys leave. `source_files` stays -- it is
    the command-line order -- and so do spans."""
    catalog = _golden_catalog()
    reordered = catalog.model_copy(
        update={"meta": CatalogMeta(source_files=["estate.jil", "more.jil"])}
    )
    relocated = _catalog(file="elsewhere/estate.jil")
    assert catalog_hash_v2(reordered) != catalog_hash_v2(catalog)
    assert catalog_hash_v2(relocated) != catalog_hash_v2(_catalog())


def test_the_hash_carries_its_algorithm_and_a_bare_digest_is_not_an_address() -> None:
    """v2 carries its algorithm; the retired v1 recipe never did. Two
    identities that could be mistaken for each other is how a gate compares
    the wrong pair -- and the grammar is what keeps a v1 value from being
    read as an address at all."""
    catalog = _catalog()
    assert catalog_hash_v2(catalog).startswith("sha256:")
    assert not is_hash_address(_retired_v1(catalog))


def test_catalog_hash_for_reads_the_version_the_record_pins() -> None:
    assert catalog_hash_for(
        {"rec": "segment", "catalog_hash_version": 2}, _catalog()
    ) == catalog_hash_v2(_catalog())
    assert CATALOG_HASH_VERSION == 2


# ------------------------------------------------ 2. source_bundle_hash (PR-07a)


GOLDEN_BUNDLE_FRAME = (
    b"\x00\x00\x00\x00\x00\x00\x00\x01a"
    b"\x00\x00\x00\x00\x00\x00\x00\x01x"
    b"\x00\x00\x00\x00\x00\x00\x00\x02bb"
    b"\x00\x00\x00\x00\x00\x00\x00\x02yy"
)
GOLDEN_BUNDLE_HASH = "sha256:f5aff2233de17c7c09866ad6a65ff6ac7497db2606e083081af521f7d38d061e"


def test_pr07a_source_bundle_hash_golden_vector() -> None:
    """`len(path) || path || len(bytes) || bytes` per file, 8-byte
    big-endian lengths, in command-line order -- the framing pinned as
    bytes, not only as a digest."""
    sources = [SourceFile(path="a", text="x"), SourceFile(path="bb", text="yy")]
    assert source_bundle_hash(sources) == GOLDEN_BUNDLE_HASH
    assert GOLDEN_BUNDLE_HASH == "sha256:" + hashlib.sha256(GOLDEN_BUNDLE_FRAME).hexdigest()


def test_pr07a_length_framing_separates_ab_c_from_a_bc() -> None:
    """ss1.1's own example. Without the lengths these are one bundle."""
    left = [SourceFile(path="p", text="ab"), SourceFile(path="q", text="c")]
    right = [SourceFile(path="p", text="a"), SourceFile(path="q", text="bc")]
    assert source_bundle_hash(left) != source_bundle_hash(right)


def test_pr07a_reversing_command_line_order_moves_the_address() -> None:
    """Order is included, not sorted away: `catalog_hash` covers
    `CatalogMeta.source_files`, so two orderings are two catalogs, and one
    bundle address for both would map one directory to two of them."""
    sources = [
        SourceFile(path="a.jil", text="insert_job: j1\n"),
        SourceFile(path="b.jil", text="insert_job: j2\n"),
    ]
    assert source_bundle_hash(sources) != source_bundle_hash(list(reversed(sources)))


def test_pr07a_the_same_bytes_from_two_paths_are_two_bundles() -> None:
    """The path is framed in, so a relocation is a different bundle --
    which is what `catalog_hash` covering `SourceSpan.file` already means."""
    one = [SourceFile(path="site-a/estate.jil", text=_SOLO_JIL)]
    two = [SourceFile(path="site-b/estate.jil", text=_SOLO_JIL)]
    assert source_bundle_hash(one) != source_bundle_hash(two)


def test_pr07a_each_ordering_reopens_to_its_own_catalog(tmp_path: Path) -> None:
    """The reopening half of PR-07a: each bundle records its own order and
    rebuilds to its own catalog, so one directory never stands for two.

    Byte-equality with the ORIGINAL journal's `catalog_hash` is not
    asserted and cannot be: `SourceSpan.file` names the stored path, so a
    rebuilt catalog hashes differently by construction -- the
    relocation-independent hashing that would close it is a deliberate
    defer (runner-design ss7)."""
    first = SourceFile(path="a.jil", text="insert_job: j1\njob_type: b\n")
    second = SourceFile(path="b.jil", text="insert_job: j2\njob_type: b\n")
    rebuilt = []
    for order in ([first, second], [second, first]):
        address = write_bundle(tmp_path, order)
        paths = bundle_source_paths(tmp_path, address)
        assert [path.name for path in paths] == [Path(source.path).name for source in order]
        rebuilt.append(
            lower_catalog(
                [parse(path.read_text(), file=str(path)) for path in paths], permit_unknown=False
            )
        )
    assert rebuilt[0].meta.source_files != rebuilt[1].meta.source_files
    assert catalog_hash_v2(rebuilt[0]) != catalog_hash_v2(rebuilt[1])


def test_the_empty_bundle_has_an_address_of_its_own() -> None:
    assert EMPTY_BUNDLE_HASH == source_bundle_hash([])
    assert EMPTY_BUNDLE_HASH != source_bundle_hash([SourceFile(path="", text="")])


# ------------------------------------------- 3. RuntimeProfile (PR-08c, PR-15)


def _full_profile() -> RuntimeProfile:
    return RuntimeProfile(
        default_tz="Europe/Zurich",
        tz_aliases={"CET": "Europe/Zurich", "EST": "America/New_York"},
        as_machine=("alpha", "beta"),
        machine_policy="local-eligible",
        execution_mode="detached",
        deadman_us=90_000_000,
        fw_default_interval_us=30_000_000,
        cmd_grace_us=15_500_000,
        reconcile_settle_us=0,
        spawn_window_us=0,
        retry_horizon_us=120_000_000,
    )


GOLDEN_PROFILE_BYTES = (
    b'{"as_machine":["alpha","beta"],"cmd_grace_us":15500000,"deadman_us":90000000,'
    b'"default_tz":"Europe/Zurich","execution_mode":"detached",'
    b'"fw_default_interval_us":30000000,"machine_policy":"local-eligible",'
    b'"reconcile_settle_us":0,"retry_horizon_us":120000000,"spawn_window_us":0,'
    b'"tz_aliases":{"CET":"Europe/Zurich","EST":"America/New_York"}}'
)
GOLDEN_RUNTIME_HASH = "sha256:b00ad496ee749327ba93592ad9b0e4adc4098882dcf919f2f525225a91823d02"


def test_the_profile_defaults_are_the_engine_s_own() -> None:
    """A profile that disagreed with the running engine would pin a
    fiction. The four durations with an engine counterpart are asserted
    against it, so moving an adapter default fails here rather than
    quietly making every later manifest untrue."""
    from dsl41.runner_adapters import (
        FileWatcherAdapter,
        LocalCommandAdapter,
        SupervisedCommandAdapter,
    )
    from dsl41.runner_startup import resume_run

    assert LocalCommandAdapter().grace_seconds == CMD_GRACE_S
    assert FileWatcherAdapter().default_interval_s == FW_DEFAULT_INTERVAL_S
    assert SupervisedCommandAdapter._SPAWN_WINDOW_S == SPAWN_WINDOW_S
    parameters = inspect.signature(resume_run).parameters
    # None = "resolve from the period's pin" (U4R3-02): the manifest is the
    # default, and only an explicit caller override departs from it. The
    # legacy fallbacks inside resume are the same numbers as the profile
    # defaults, which the two assertions above already tie to the engine.
    assert parameters["settle_seconds"].default is None
    assert parameters["grace_seconds"].default is None


def test_the_v2_projection_accounts_for_every_meta_field() -> None:
    """ss1.1 names the three fields `CatalogMeta` had when the recipe was
    frozen: `source_files` stays, `tool_version` and `parsed_at` leave. A
    fourth is a decision about what a period's identity IS, so it fails
    here rather than dropping out of the hash unnoticed."""
    assert set(CatalogMeta.model_fields) == {"source_files", "tool_version", "parsed_at"}


def test_pr08c_runtime_hash_golden_vector() -> None:
    """One fully populated profile, its canonical bytes and its hash."""
    profile = _full_profile()
    assert canonical_bytes(profile.model_dump(mode="json")) == GOLDEN_PROFILE_BYTES
    assert runtime_hash(profile) == GOLDEN_RUNTIME_HASH


#: One alternate value per field, for the PR-15 sweep. Keyed by field name
#: and checked against `RuntimeProfile.model_fields` below, so a field added
#: later fails loudly here instead of going quietly unhashed.
_ALTERNATES: dict[str, Any] = {
    "default_tz": "Europe/Zurich",
    "tz_aliases": {"CET": "Europe/Zurich"},
    "as_machine": ("alpha",),
    "machine_policy": "local-eligible",
    "execution_mode": "detached",
    "deadman_us": 90_000_000,
    "fw_default_interval_us": 30_000_000,
    "cmd_grace_us": 11_000_000,
    "reconcile_settle_us": 0,
    "spawn_window_us": 0,
    "retry_horizon_us": 120_000_000,
}


def test_pr15_every_field_of_the_model_has_a_case() -> None:
    """The sweep is derived from the model, so hashing a named subset
    cannot pass: a field added later has no alternate and this fails."""
    assert set(_ALTERNATES) == set(RuntimeProfile.model_fields)


@pytest.mark.parametrize("field", sorted(RuntimeProfile.model_fields))
def test_pr15_runtime_hash_moves_for_every_field(field: str) -> None:
    """ss2.1: a hash that omitted `cmd_grace_us` passed PR-15's old named
    list while a patch quietly changed how a live command is killed."""
    base = RuntimeProfile()
    changed = RuntimeProfile(**{**base.model_dump(), field: _ALTERNATES[field]})
    assert changed != base, f"the alternate for {field} does not change the value"
    assert runtime_hash(changed) != runtime_hash(base)


def test_the_profile_is_frozen_and_forbids_extras() -> None:
    profile = RuntimeProfile()
    with pytest.raises(Exception):
        profile.default_tz = "Europe/Zurich"  # type: ignore[misc]
    with pytest.raises(Exception):
        RuntimeProfile(machine_map={"a": "b"})  # type: ignore[call-arg]


# ------------------------------------------- 3a. CLI -> profile (PR-15a)


def test_pr15a_omitted_options_resolve_to_the_stated_defaults() -> None:
    profile = runtime_profile_from_cli()
    assert profile == RuntimeProfile()
    assert profile.default_tz == "UTC"
    assert profile.tz_aliases == {} and profile.as_machine == ()
    assert profile.machine_policy == "strict" and profile.execution_mode == "tethered"
    assert profile.deadman_us is None
    assert profile.fw_default_interval_us == 60_000_000
    assert profile.cmd_grace_us == 10_000_000
    assert profile.reconcile_settle_us == 5_000_000
    assert profile.spawn_window_us == 5_000_000
    assert profile.retry_horizon_us == 60_000_000


def test_pr15a_an_absent_timezone_is_utc_never_null() -> None:
    assert runtime_profile_from_cli(timezone=None).default_tz == "UTC"
    assert runtime_profile_from_cli(timezone="Europe/Zurich").default_tz == "Europe/Zurich"


def test_pr15a_local_eligible_and_detached_round_trip() -> None:
    profile = runtime_profile_from_cli(machine_policy="local-eligible", detached=True)
    assert profile.machine_policy == "local-eligible"
    assert profile.execution_mode == "detached"
    assert runtime_hash(profile) != runtime_hash(runtime_profile_from_cli())


def test_pr15a_duplicate_as_machine_collapses_and_sorts() -> None:
    """Two spellings of one machine set are one profile, so one estate
    relaunched with the flags in the other order is not a new period."""
    left = runtime_profile_from_cli(as_machine=["beta", "alpha", "beta"])
    right = runtime_profile_from_cli(as_machine=["alpha", "beta"])
    assert left.as_machine == ("alpha", "beta")
    assert runtime_hash(left) == runtime_hash(right)


def test_pr15a_fractional_seconds_round_to_microseconds() -> None:
    profile = runtime_profile_from_cli(deadman_s=1.5, cmd_grace_s=0.1, retry_horizon_s=2.25)
    assert profile.deadman_us == 1_500_000
    assert profile.cmd_grace_us == 100_000
    assert profile.retry_horizon_us == 2_250_000


def test_pr15a_tz_aliases_are_the_resolved_contents_and_never_null() -> None:
    assert runtime_profile_from_cli(tz_aliases=None).tz_aliases == {}
    assert runtime_profile_from_cli(tz_aliases={"CET": "Europe/Zurich"}).tz_aliases == {
        "CET": "Europe/Zurich"
    }


@pytest.mark.parametrize(
    "field", ["fw_default_interval_us", "cmd_grace_us", "retry_horizon_us", "deadman_us"]
)
def test_pr15a_zero_is_refused_where_the_bound_is_strictly_positive(field: str) -> None:
    with pytest.raises(ValueError):
        RuntimeProfile(**{**RuntimeProfile().model_dump(), field: 0})


@pytest.mark.parametrize("field", ["reconcile_settle_us", "spawn_window_us"])
def test_pr15a_zero_is_legal_where_the_bound_allows_it(field: str) -> None:
    profile = RuntimeProfile(**{**RuntimeProfile().model_dump(), field: 0})
    assert getattr(profile, field) == 0


@pytest.mark.parametrize(
    "field",
    [
        "deadman_us",
        "fw_default_interval_us",
        "cmd_grace_us",
        "reconcile_settle_us",
        "spawn_window_us",
        "retry_horizon_us",
    ],
)
def test_pr15a_a_negative_duration_is_refused_everywhere(field: str) -> None:
    with pytest.raises(ValueError):
        RuntimeProfile(**{**RuntimeProfile().model_dump(), field: -1})


def test_deadman_is_the_only_nullable_field() -> None:
    assert RuntimeProfile(deadman_us=None).deadman_us is None
    for field in RuntimeProfile.model_fields:
        if field == "deadman_us":
            continue
        with pytest.raises(ValueError):
            RuntimeProfile(**{**RuntimeProfile().model_dump(), field: None})


# --------------------------------------------------- 4. the manifests + layout


def _staged(catalog: CatalogIR, **kwargs: Any) -> StagedManifest:
    return stage_manifest(
        catalog,
        source_bundle_hash=kwargs.get("source_bundle_hash", EMPTY_BUNDLE_HASH),
        profile=kwargs.get("profile", RuntimeProfile()),
        state_machine_version=kwargs.get("state_machine_version", 1),
    )


def test_the_staged_manifest_carries_nothing_the_engine_owns() -> None:
    """ss2.1: the launcher's half is exactly seven fields. `baseline_id`
    and `first_index` cannot be staged -- one is minted at the opening and
    the other is attempt output."""
    staged = _staged(_catalog())
    assert set(staged.model_dump()) == {
        "artifact_format_version",
        "catalog_hash",
        "catalog_hash_version",
        "source_bundle_hash",
        "runtime_profile",
        "runtime_hash",
        "state_machine_version",
    }
    assert staged.catalog_hash == catalog_hash_v2(_catalog())
    assert staged.runtime_hash == runtime_hash(RuntimeProfile())


def test_the_committed_manifest_is_the_staged_fields_plus_five() -> None:
    manifest = _staged(_catalog()).commit(
        period_id=1, baseline_id="b", clock_domain="real", segment_no=1, first_index=1
    )
    assert set(manifest.model_dump()) - set(StagedManifest.model_fields) == {
        "period_id",
        "baseline_id",
        "clock_domain",
        "segment_no",
        "first_index",
    }
    # extra="forbid" both ways: a committed manifest is never read as a
    # staged one, which is what stops a reader accepting the wrong file
    with pytest.raises(ValueError):
        StagedManifest.model_validate(manifest.model_dump())


def test_the_bundle_is_content_addressed_and_reused_never_rewritten(tmp_path: Path) -> None:
    """ss1.1: a period that reverts to earlier bytes references the
    directory already there. A rewrite would be a second authority over
    bytes that are their own address."""
    sources = [SourceFile(path="a/estate.jil", text=_SOLO_JIL)]
    address = write_bundle(tmp_path, sources)
    directory = bundle_dir(tmp_path, address)
    (directory / "witness").write_text("still here")
    assert write_bundle(tmp_path, sources) == address
    assert (directory / "witness").read_text() == "still here"
    assert address == source_bundle_hash(sources)


def test_the_bundle_holds_the_byte_exact_inputs_and_its_ordered_vector(tmp_path: Path) -> None:
    sources = [
        SourceFile(path="one/a.jil", text=_SOLO_JIL),
        SourceFile(path="two/b.jil", text="insert_job: j2\njob_type: b\n"),
    ]
    address = write_bundle(tmp_path, sources)
    vector = json.loads((bundle_dir(tmp_path, address) / "sources.json").read_text())
    assert vector["source_bundle_hash"] == address
    assert [entry["path"] for entry in vector["sources"]] == ["one/a.jil", "two/b.jil"]
    assert vector["sources"][0]["sha256"] == (
        "sha256:" + hashlib.sha256(_SOLO_JIL.encode("utf-8")).hexdigest()
    )
    stored = bundle_source_paths(tmp_path, address)
    assert [path.read_text() for path in stored] == [source.text for source in sources]


def test_two_inputs_with_one_basename_are_stored_apart(tmp_path: Path) -> None:
    sources = [
        SourceFile(path="one/estate.jil", text=_SOLO_JIL),
        SourceFile(path="two/estate.jil", text="insert_job: j2\njob_type: b\n"),
    ]
    address = write_bundle(tmp_path, sources)
    stored = bundle_source_paths(tmp_path, address)
    assert len({path.name for path in stored}) == 2
    assert [path.read_text() for path in stored] == [source.text for source in sources]


def test_the_period_manifest_round_trips_through_its_file(tmp_path: Path) -> None:
    manifest = _staged(_catalog()).commit(
        period_id=1, baseline_id="b-1", clock_domain="virtual", segment_no=1, first_index=1
    )
    path = write_period_manifest(tmp_path, manifest)
    assert path == tmp_path / "periods" / "000001" / "manifest.json"
    assert path.read_bytes() == canonical_bytes(manifest.model_dump(mode="json"))
    assert read_period_manifest(tmp_path) == manifest


def test_the_period_artifacts_are_owner_only(tmp_path: Path) -> None:
    """DL-66's discipline, on the artifacts that replaced `manifest/`: the
    manifest names the estate's inputs and its launch options."""
    manifest = _staged(_catalog()).commit(
        period_id=1, baseline_id="b-1", clock_domain="virtual", segment_no=1, first_index=1
    )
    path = write_period_manifest(tmp_path, manifest)
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert os.stat(period_dir(tmp_path, 1)).st_mode & 0o777 == 0o700
    address = write_bundle(tmp_path, [SourceFile(path="a.jil", text=_SOLO_JIL)])
    assert os.stat(bundle_dir(tmp_path, address)).st_mode & 0o777 == 0o700
    assert os.stat(bundle_dir(tmp_path, address) / "a.jil").st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "mutate,expected",
    [
        (lambda payload: {**payload, "artifact_format_version": 999}, "999"),
        (lambda payload: {**payload, "invented_field": 1}, "invented_field"),
        (
            lambda payload: {key: payload[key] for key in payload if key != "baseline_id"},
            "baseline",
        ),
    ],
)
def test_pr08d_an_unreadable_manifest_is_refused_by_name(
    tmp_path: Path, mutate, expected: str
) -> None:
    """A manifest this binary cannot read is a WRONG artifact, not a
    missing one -- and it is refused as an `EngineError` naming the file,
    the exception every door here already guards against. A bare decoder
    or validator error would escape `dsl41 runs` and `dsl41 run` as a
    traceback."""
    manifest = _staged(_catalog()).commit(
        period_id=1, baseline_id="b-1", clock_domain="virtual", segment_no=1, first_index=1
    )
    path = write_period_manifest(tmp_path, manifest)
    path.write_text(json.dumps(mutate(json.loads(path.read_text()))))
    with pytest.raises(EngineError, match=expected):
        read_period_manifest(tmp_path)


def test_an_unreadable_manifest_refuses_dsl41_runs_rather_than_escaping(tmp_path: Path) -> None:
    """The same refusal at the door an operator meets: one bad root exits
    2 with a message, never a traceback out of a multi-root invocation."""
    run_root = tmp_path / "run"
    _close(_start(run_root, _catalog()))
    path = run_root / "periods" / "000001" / "manifest.json"
    path.write_text(json.dumps({"artifact_format_version": 999}))
    result = CliRunner().invoke(app, ["runs", str(run_root)])
    assert result.exit_code == 2
    assert "999" in result.output


def test_an_unknown_catalog_hash_version_is_refused_by_name(tmp_path: Path) -> None:
    """A version this binary does not implement is not "the estate
    changed": recomputing it under the only recipe left would tell an
    operator to abandon a live estate over a log this build cannot read.

    UNKNOWN, and not retired: the message says what this binary implements
    and names no decision-log entry (docs/protocol-evolution.md ss6)."""
    catalog = _catalog()
    for call in (
        lambda: catalog_hash_at(3, catalog),
        lambda: catalog_hash_for({"rec": "segment", "catalog_hash_version": 3}, catalog),
    ):
        with pytest.raises(EngineError, match="catalog_hash_version 3") as caught:
            call()
        assert "DL-138" not in str(caught.value)
        assert "RETIRED" not in str(caught.value)


def test_the_retired_catalog_hash_recipe_is_refused_by_name(tmp_path: Path) -> None:
    """D4/D9 (DL-138): version 1 is RETIRED, and the one dispatcher every
    owner asks says so by name -- a different error from the unknown case
    above, because "this used to be legal" and "this was never legal" send
    an operator to different places."""
    catalog = _catalog()
    with pytest.raises(EngineError, match="RETIRED") as caught:
        catalog_hash_at(1, catalog)
    assert "DL-138" in str(caught.value)
    with pytest.raises(EngineError, match="DL-138"):
        catalog_hash_for({"rec": "segment", "catalog_hash_version": 1}, catalog)


def test_an_incomplete_bundle_is_completed_not_reused(tmp_path: Path) -> None:
    """`sources.json` is written last, so a crash before it leaves a
    directory whose address promises bytes it does not hold. Reuse is
    gated on that file, never on the directory -- otherwise the run root
    would be missing its own inputs for the rest of its life."""
    sources = [SourceFile(path="a/estate.jil", text=_SOLO_JIL)]
    address = write_bundle(tmp_path, sources)
    directory = bundle_dir(tmp_path, address)
    (directory / "sources.json").unlink()  # the crash window, exactly
    assert write_bundle(tmp_path, sources) == address
    assert (directory / "sources.json").exists()
    assert [path.read_text() for path in bundle_source_paths(tmp_path, address)] == [_SOLO_JIL]
    assert not [child for child in (tmp_path / "catalogs").iterdir() if child.name.endswith(".tmp")]


# ------------------------------------------------------ 5. the segment record


def test_genesis_writes_the_bundle_the_manifest_and_a_segment(tmp_path: Path) -> None:
    """ss1.1's genesis, as far as this unit goes: the inputs are
    materialized, period 1's manifest is installed, and the log opens with
    the `segment` that names it."""
    run_root = tmp_path / "run"
    jil = parse(_SOLO_JIL, file="estate.jil")
    catalog = _catalog()
    staged = _stage_period(run_root, [jil], catalog, RuntimeProfile())
    _close(_start(run_root, catalog, staged=staged))

    manifest = read_period_manifest(run_root)
    assert manifest is not None
    segment = read_journal(run_root / "journal.jsonl")[0]
    assert segment["rec"] == "segment"
    assert segment["source_bundle_hash"] == staged.source_bundle_hash
    assert segment["catalog_hash"] == catalog_hash_v2(catalog)
    assert bundle_dir(run_root, staged.source_bundle_hash).is_dir()
    assert not (run_root / "manifest").exists()  # the legacy layout is not written
    check_manifest_against_segment(manifest, segment)


def test_the_manifest_and_the_segment_cannot_disagree_at_birth(tmp_path: Path) -> None:
    """Both are derived from ONE object, so there is no path that writes
    two identities."""
    run_root = tmp_path / "run"
    catalog = _catalog()
    _close(_start(run_root, catalog))
    manifest = read_period_manifest(run_root)
    segment = read_journal(run_root / "journal.jsonl")[0]
    assert manifest is not None
    for field in ("catalog_hash", "source_bundle_hash", "runtime_hash", "baseline_id"):
        assert getattr(manifest, field) == segment[field]


def test_a_segment_journal_replays_resumes_and_folds(tmp_path: Path) -> None:
    """The whole reader surface over a current journal: read_journal,
    replay through `dsl41 journal`, resume, and the run-history fold."""
    run_root = tmp_path / "run"
    jil_path = tmp_path / "estate.jil"
    jil_path.write_text(_SOLO_JIL)
    catalog = _catalog(file=str(jil_path))
    staged = _stage_period(
        run_root, [parse(_SOLO_JIL, file=str(jil_path))], catalog, RuntimeProfile()
    )
    engine = _start(run_root, catalog, staged=staged)
    _close(engine)

    resumed = asyncio.run(_resume(run_root, catalog, at=T0 + timedelta(minutes=1)))
    _close(resumed)
    assert [r["rec"] for r in read_journal(run_root / "journal.jsonl")][:1] == ["segment"]

    rendered = CliRunner().invoke(app, ["journal", str(run_root / "journal.jsonl"), str(jil_path)])
    assert rendered.exit_code == 0
    assert read_run_root(run_root) == []  # nothing ran; the fold still reads the root


def test_pr22_a_manifest_that_is_not_this_segments_refuses_naming_both(tmp_path: Path) -> None:
    """PR-22's U4 half: the committed manifest is the engine's own output
    and is checked against the record that names it at resume."""
    run_root = tmp_path / "run"
    catalog = _catalog()
    _close(_start(run_root, catalog))
    path = run_root / "periods" / "000001" / "manifest.json"
    payload = json.loads(path.read_text())
    payload["baseline_id"] = "someone-elses-baseline"
    path.write_text(json.dumps(payload))

    with pytest.raises(EngineError) as refusal:
        asyncio.run(_resume(run_root, catalog, at=T0 + timedelta(minutes=1)))
    message = str(refusal.value)
    assert "someone-elses-baseline" in message  # the manifest's value
    assert read_journal(run_root / "journal.jsonl")[0]["baseline_id"] in message


#: One disagreeing value per shared field, for the PR-22 sweep. Checked
#: against `period._SHARED_FIELDS` below, which is itself checked against
#: what the two shapes really share -- so a field added to either one has
#: a case by construction rather than by memory.
_DISAGREEMENTS: dict[str, Any] = {
    "catalog_hash": "sha256:another",
    "catalog_hash_version": 1,
    "source_bundle_hash": "sha256:another",
    "runtime_hash": "sha256:another",
    "state_machine_version": 2,
    "period_id": 2,
    "baseline_id": "someone-elses-baseline",
    "clock_domain": "real",
    "segment_no": 2,
    "first_index": 2,
}


def test_pr22_the_shared_field_list_is_every_field_the_two_really_share() -> None:
    """Derived, not remembered: the checked set is the intersection of the
    manifest's fields and the segment record's keys, so a field added to
    either shape is checked the day it appears."""
    manifest = _staged(_catalog()).commit(
        period_id=1, baseline_id="b-1", clock_domain="virtual", segment_no=1, first_index=1
    )
    segment = segment_record(manifest, estate_id="e", at=T0)
    assert set(_SHARED_FIELDS) == set(Manifest.model_fields) & set(segment)
    assert set(_DISAGREEMENTS) == set(_SHARED_FIELDS)


@pytest.mark.parametrize("field", sorted(_SHARED_FIELDS))
def test_pr22_every_shared_field_is_checked(field: str) -> None:
    """Not a chosen few: a disagreement in any shared field means the
    manifest is not this segment's."""
    manifest = _staged(_catalog()).commit(
        period_id=1, baseline_id="b-1", clock_domain="virtual", segment_no=1, first_index=1
    )
    segment: dict[str, Any] = {
        **manifest.model_dump(mode="json"),
        field: _DISAGREEMENTS[field],
    }
    with pytest.raises(EngineError, match=field):
        check_manifest_against_segment(manifest, segment)


def test_a_manifest_for_another_catalog_never_opens_a_log(tmp_path: Path) -> None:
    """The journal's own guard: two identities handed to one opening is a
    refusal, not a coin toss."""
    from dsl41.runner_journal import Journal

    manifest = _staged(_catalog()).commit(
        period_id=1, baseline_id="b-1", clock_domain="virtual", segment_no=1, first_index=1
    )
    other = _catalog(_SOLO_JIL.replace("echo hi", "echo bye"))
    with pytest.raises(EngineError, match="not this catalog's"):
        Journal.create(
            tmp_path / "journal.jsonl",
            catalog=other,
            clock_domain="virtual",
            started_at=T0,
            manifest=manifest,
        )


# --------------------------------------------- 6. the resume gate's comparison


def test_the_resume_gate_compares_like_for_like(tmp_path: Path) -> None:
    """A root resumes under the recipe its own `segment` pins; a CHANGED
    estate refuses. The recipe is read from the record and never assumed,
    which is what stops a gate from refusing an estate that did not
    change (DL-100)."""
    catalog = _catalog()
    changed = _catalog(_SOLO_JIL.replace("echo hi", "echo bye"))

    current = tmp_path / "current"
    _close(_start(current, catalog))
    _close(asyncio.run(_resume(current, catalog, at=T0 + timedelta(minutes=1))))
    with pytest.raises(EngineError, match="catalog hash mismatch"):
        asyncio.run(_resume(current, changed, at=T0 + timedelta(minutes=2)))


def test_the_run_cli_stages_the_bundle_and_the_profile_it_was_launched_with(
    short_root: Path,
) -> None:
    """End to end through the real `dsl41 run` process: the launch options
    reach `runtime_hash`, the inputs reach the bundle, and period 1's
    manifest agrees with the segment the engine opened."""
    with engine(
        short_root,
        extra=[
            "--timezone",
            "Europe/Zurich",
            "--machine-policy",
            "local-eligible",
            "--as-machine",
            "beta",
            "--as-machine",
            "alpha",
            "--as-machine",
            "beta",
        ],
    ) as proc:
        manifest = read_period_manifest(proc.run_root)
        assert manifest is not None
        assert manifest.runtime_profile.default_tz == "Europe/Zurich"
        assert manifest.runtime_profile.machine_policy == "local-eligible"
        assert manifest.runtime_profile.as_machine == ("alpha", "beta")
        assert manifest.runtime_profile.execution_mode == "tethered"
        assert manifest.runtime_hash == runtime_hash(manifest.runtime_profile)
        stored = bundle_source_paths(proc.run_root, manifest.source_bundle_hash)
        assert [path.read_text() for path in stored] == [proc.jil.read_text()]
        assert not (proc.run_root / "manifest").exists()
        check_manifest_against_segment(manifest, read_journal(proc.run_root / "journal.jsonl")[0])


def test_a_rehearsal_run_root_records_the_profile_it_rehearsed_under(tmp_path: Path) -> None:
    """A rehearsal is evidence about production behavior, so its run root
    is a self-contained artifact too: the timezone it interpreted the
    estate under is in the manifest, not only in the invocation."""
    jil = tmp_path / "estate.jil"
    jil.write_text(_SOLO_JIL)
    run_root = tmp_path / "run"
    result = CliRunner().invoke(
        app,
        [
            "rehearse",
            str(jil),
            "--run-root",
            str(run_root),
            "--start",
            "2026-07-01T08:00:00",
            "--hours",
            "1",
            "--timezone",
            "Europe/Zurich",
        ],
    )
    assert result.exit_code == 0, result.output
    manifest = read_period_manifest(run_root)
    assert manifest is not None
    assert manifest.runtime_profile.default_tz == "Europe/Zurich"
    assert manifest.clock_domain == "virtual"
    stored = bundle_source_paths(run_root, manifest.source_bundle_hash)
    assert [path.read_text() for path in stored] == [_SOLO_JIL]
    check_manifest_against_segment(manifest, read_journal(run_root / "journal.jsonl")[0])


def test_the_manifest_pins_the_deadman_the_supervisor_runs_not_the_one_asked_for() -> None:
    """A reattaching engine meets a supervisor it did not start, and one
    already up cannot change its interval -- so the engine runs the
    supervisor's value and the manifest has to say so. Recording the
    request would pin a bound the estate does not have."""
    staged = _staged(_catalog(), profile=runtime_profile_from_cli(detached=True, deadman_s=90.0))
    observed = _observed_profile(staged, 60.0)
    assert observed is not None
    assert observed.runtime_profile.deadman_us == 60_000_000
    assert observed.runtime_hash == runtime_hash(observed.runtime_profile)
    assert observed.runtime_hash != staged.runtime_hash
    # agreement changes nothing at all, including the bytes
    assert _observed_profile(staged, 90.0) is staged
    assert _observed_profile(_staged(_catalog()), None) is not None
    assert _observed_profile(None, 60.0) is None


def test_a_journal_less_engine_is_untouched_by_any_of_this() -> None:
    """The bisimulation harness runs an Engine with no journal at all;
    period identity is a property of a log, and there is none here."""
    from dsl41.runner import Engine

    engine = Engine(_catalog(), clock=VirtualClock(start=T0), adapters={"CMD": FakeAdapter()})
    assert engine.journal is None


# ------------------------------------------------ round-1 review pins (DL-130)


def test_a_malformed_catalog_hash_version_refuses_not_coerces() -> None:
    """A segment that cannot say which recipe it means must not have one
    picked for it: `"2"`, `true`, `2.7` and an absent field all refuse."""
    catalog = lower_source(_SOLO_JIL)
    for bogus in ("2", True, 2.7, None):
        with pytest.raises(EngineError, match="catalog_hash_version"):
            catalog_hash_for({"rec": "segment", "catalog_hash_version": bogus}, catalog)


def test_a_tampered_profile_beside_the_original_hash_refuses(tmp_path: Path) -> None:
    """PR-22: the segment pins only the runtime_hash, so a manifest whose
    profile was edited but whose hash was not would pass every shared-field
    comparison while the period runs different semantics. The manifest must
    agree with itself."""
    catalog = lower_source(_SOLO_JIL)
    manifest = genesis_manifest(catalog, clock_domain="virtual", state_machine_version=1)
    write_period_manifest(tmp_path, manifest)
    path = period_dir(tmp_path, 1) / "manifest.json"
    payload = json.loads(path.read_bytes())
    payload["runtime_profile"]["cmd_grace_us"] = 1  # edited profile, original hash
    path.write_bytes(canonical_bytes(payload))
    with pytest.raises(EngineError, match="disagrees with itself"):
        read_period_manifest(tmp_path)


def test_an_unreadable_manifest_is_never_read_as_absent(tmp_path: Path) -> None:
    """Absent means exactly ENOENT: an EACCES manifest is a broken root, not
    a root that has none, and degrading on it would resume past a pin that
    is right there."""
    catalog = lower_source(_SOLO_JIL)
    manifest = genesis_manifest(catalog, clock_domain="virtual", state_machine_version=1)
    path = write_period_manifest(tmp_path, manifest)
    path.chmod(0o000)
    try:
        with pytest.raises(EngineError, match="unreadable"):
            read_period_manifest(tmp_path)
    finally:
        path.chmod(0o600)


def test_an_input_named_sources_json_never_shadows_the_metadata(tmp_path: Path) -> None:
    """The metadata name is reserved: an input whose basename is
    sources.json stores under a positional prefix, and the vector still
    lands intact."""
    sources = [SourceFile(path="estate/sources.json", text="insert_job: j\n")]
    address = write_bundle(tmp_path, sources)
    [stored] = bundle_source_paths(tmp_path, address)
    assert stored.name != "sources.json"
    assert stored.read_text() == "insert_job: j\n"


def test_a_complete_bundle_is_never_destroyed_by_a_racing_writer(tmp_path: Path) -> None:
    """Publication is serialized under catalogs/.lock, so the completeness
    check is authoritative: a writer that finds a complete bundle under the
    lock reuses it byte-for-byte, and only a provably-incomplete leftover
    is ever removed. The lock-free alternative had a window in which
    completeness arrived between the check and the rmtree -- the one way a
    writer could delete a bundle a sealed period already references."""
    sources = [SourceFile(path="a.jil", text="insert_job: j\n")]
    address = write_bundle(tmp_path, sources)
    marker = bundle_dir(tmp_path, address) / "sources.json"
    before = marker.read_bytes()
    assert write_bundle(tmp_path, sources) == address  # reuse, no rewrite
    assert marker.read_bytes() == before
    # and the crash leftover (no sources.json) is repaired under the lock:
    # the pre-existing test above pins that half
    assert (bundle_dir(tmp_path, address).parent / ".lock").exists()


def test_a_changed_bundle_refuses_before_a_reader_consumes_it(tmp_path: Path) -> None:
    """The directory is immutable by contract, and the reader checks the
    contract: edited bytes, an edited vector hash, and a traversal name all
    refuse."""
    sources = [SourceFile(path="a.jil", text="insert_job: j\n")]
    address = write_bundle(tmp_path, sources)
    directory = bundle_dir(tmp_path, address)
    [stored] = bundle_source_paths(tmp_path, address)

    stored.write_text("insert_job: k\n")  # edited bytes
    with pytest.raises(EngineError, match="not the one its address names"):
        bundle_source_paths(tmp_path, address)
    stored.write_text("insert_job: j\n")
    assert bundle_source_paths(tmp_path, address) == [stored]  # intact again

    vector = json.loads((directory / "sources.json").read_bytes())
    vector["sources"][0]["file"] = "../escape"
    (directory / "sources.json").write_bytes(canonical_bytes(vector))
    with pytest.raises(EngineError, match="unsafe stored name"):
        bundle_source_paths(tmp_path, address)


def test_a_resume_with_different_launch_options_refuses(tmp_path: Path) -> None:
    """PR-22's runtime half: the same estate resumed under a different
    --timezone refuses NAMING the moved field -- a resume that quietly
    rebuilt the scheduler under new options would change period semantics
    with every identity gate green. A legacy root (no manifest) has no pin
    to hold; a deadman difference compares at its OBSERVED value; the
    matching profile passes."""
    from dsl41.cli import _resume_profile_error

    catalog = lower_source(_SOLO_JIL)
    pinned = runtime_profile_from_cli(timezone="UTC")
    manifest = genesis_manifest(
        catalog,
        clock_domain="real",
        state_machine_version=1,
        staged=stage_manifest(
            catalog,
            source_bundle_hash=EMPTY_BUNDLE_HASH,
            profile=pinned,
            state_machine_version=1,
        ),
    )
    write_period_manifest(tmp_path, manifest)

    moved = _resume_profile_error(
        tmp_path, runtime_profile_from_cli(timezone="Europe/Zurich"), None
    )
    assert moved is not None and "runtime-profile mismatch" in moved and "default_tz" in moved
    assert _resume_profile_error(tmp_path, runtime_profile_from_cli(timezone="UTC"), None) is None
    # the ASKED deadman does not matter; the observed one does
    asked_only = runtime_profile_from_cli(timezone="UTC", deadman_s=90.0)
    assert _resume_profile_error(tmp_path, asked_only, None) is None
    observed = _resume_profile_error(tmp_path, runtime_profile_from_cli(timezone="UTC"), 90.0)
    assert observed is not None and "deadman_us" in observed
    # a legacy root has no pin
    assert (
        _resume_profile_error(
            tmp_path / "legacy", runtime_profile_from_cli(timezone="Tokyo/Nope"), None
        )
        is None
    )


def test_the_pin_is_derived_from_the_wiring_not_the_flags(tmp_path: Path) -> None:
    """An embedder that builds a Europe/Zurich scheduler and stages nothing
    must open a period whose profile SAYS Zurich: the pin describes the
    machine that runs. And a staged profile the wiring disagrees with is a
    fiction, refused before it is durable."""
    from dsl41.runner_adapters import FakeAdapter
    from dsl41.runner_scheduler import Scheduler
    from dsl41.runner_startup import start_run

    catalog = lower_source(_SOLO_JIL)
    t0 = datetime(2026, 7, 1, 8, 0)
    scheduler = Scheduler(catalog, start=t0, default_tz="Europe/Zurich")
    engine = start_run(
        catalog,
        tmp_path / "zurich",
        clock=VirtualClock(start=t0),
        adapters={"CMD": FakeAdapter(default=None)},
        scheduler=scheduler,
    )
    assert engine.journal is not None
    engine.journal.close()
    pinned = read_period_manifest(tmp_path / "zurich")
    assert pinned is not None and pinned.runtime_profile.default_tz == "Europe/Zurich"

    # the staged-fiction refusal
    fiction = stage_manifest(
        catalog,
        source_bundle_hash=EMPTY_BUNDLE_HASH,
        profile=runtime_profile_from_cli(timezone="UTC"),
        state_machine_version=1,
    )
    with pytest.raises(EngineError, match="default_tz"):
        start_run(
            catalog,
            tmp_path / "fiction",
            clock=VirtualClock(start=t0),
            adapters={"CMD": FakeAdapter(default=None)},
            scheduler=Scheduler(catalog, start=t0, default_tz="Europe/Zurich"),
            staged=fiction,
        )


def test_resume_refuses_wiring_that_moved_off_the_pin(tmp_path: Path) -> None:
    """The core half of the runtime gate: the same root resumed with a
    scheduler wired to another timezone refuses in `resume_run` itself --
    no CLI in the loop -- naming the moved field."""
    import asyncio as _asyncio

    from dsl41.runner_adapters import FakeAdapter
    from dsl41.runner_scheduler import Scheduler
    from dsl41.runner_startup import resume_run, start_run

    catalog = lower_source(_SOLO_JIL)
    t0 = datetime(2026, 7, 1, 8, 0)
    engine = start_run(
        catalog,
        tmp_path / "root",
        clock=VirtualClock(start=t0),
        adapters={"CMD": FakeAdapter(default=None)},
        scheduler=Scheduler(catalog, start=t0, default_tz="UTC"),
    )
    assert engine.journal is not None
    engine.journal.close()

    async def scenario() -> None:
        with pytest.raises(EngineError, match="default_tz"):
            await resume_run(
                catalog,
                tmp_path / "root",
                clock=VirtualClock(start=t0 + timedelta(minutes=1)),
                adapters={"CMD": FakeAdapter(default=None)},
                scheduler=Scheduler(
                    catalog, start=t0 + timedelta(minutes=1), default_tz="Europe/Zurich"
                ),
                settle_seconds=0.0,
                grace_seconds=0.0,
            )

    _asyncio.run(scenario())


def test_a_segment_with_a_boolean_where_an_int_belongs_refuses(tmp_path: Path) -> None:
    """`true == 1` in Python, so a lax reader would pass a malformed
    identity record through every value-comparing gate. The exact ss2.1
    schema runs where the record is first read."""
    from dsl41.runner_adapters import FakeAdapter
    from dsl41.runner_journal import read_journal
    from dsl41.runner_startup import start_run

    engine = start_run(
        lower_source(_SOLO_JIL),
        tmp_path / "root",
        clock=VirtualClock(start=datetime(2026, 7, 1, 8, 0)),
        adapters={"CMD": FakeAdapter(default=None)},
    )
    assert engine.journal is not None
    engine.journal.close()
    path = tmp_path / "root" / "journal.jsonl"
    lines = path.read_text().splitlines()
    segment = json.loads(lines[0])
    segment["state_machine_version"] = True
    path.write_text("\n".join([json.dumps(segment, sort_keys=True), *lines[1:]]) + "\n")
    with pytest.raises(EngineError, match="state_machine_version"):
        read_journal(path)


def test_sources_json_must_claim_the_address_it_sits_under(tmp_path: Path) -> None:
    """A falsified top-level claim is a malformed artifact even when the
    files happen to verify -- and a versionless vector is unsupported
    evidence."""
    sources = [SourceFile(path="a.jil", text="insert_job: j\n")]
    address = write_bundle(tmp_path, sources)
    meta_path = bundle_dir(tmp_path, address) / "sources.json"
    vector = json.loads(meta_path.read_bytes())

    lied = {**vector, "source_bundle_hash": "sha256:" + "0" * 64}
    meta_path.write_bytes(canonical_bytes(lied))
    with pytest.raises(EngineError, match="claims"):
        bundle_source_paths(tmp_path, address)

    unversioned = {k: v for k, v in vector.items() if k != "artifact_format_version"}
    meta_path.write_bytes(canonical_bytes(unversioned))
    with pytest.raises(EngineError, match="artifact_format_version"):
        bundle_source_paths(tmp_path, address)


def test_a_manifest_with_a_coerced_or_missing_field_refuses(tmp_path: Path) -> None:
    """Strict in the JSON sense: `"10000000"` never coerces into an int
    field, and a MISSING field must not silently take the model's default
    -- a defaulted pin is no pin."""
    catalog = lower_source(_SOLO_JIL)
    manifest = genesis_manifest(catalog, clock_domain="virtual", state_machine_version=1)
    path = write_period_manifest(tmp_path, manifest)
    payload = json.loads(path.read_bytes())

    coerced = json.loads(json.dumps(payload))
    coerced["runtime_profile"]["cmd_grace_us"] = "10000000"
    path.write_bytes(canonical_bytes(coerced))
    with pytest.raises(EngineError):
        read_period_manifest(tmp_path)

    absent = {k: v for k, v in payload.items() if k != "catalog_hash_version"}
    path.write_bytes(canonical_bytes(absent))
    with pytest.raises(EngineError, match="catalog_hash_version"):
        read_period_manifest(tmp_path)


# ------------------------------------------------ round-3 review pins (DL-130)


def test_a_segment_root_missing_its_manifest_refuses_resume(tmp_path: Path) -> None:
    """Genesis installs the manifest before the log opens, so a segment root
    without one LOST it -- degrading would skip every profile gate. Only a
    legacy header root may degrade: it never had one to lose."""
    import asyncio as _asyncio

    from dsl41.runner_adapters import FakeAdapter
    from dsl41.runner_startup import resume_run, start_run

    catalog = lower_source(_SOLO_JIL)
    t0 = datetime(2026, 7, 1, 8, 0)
    engine = start_run(
        catalog,
        tmp_path / "root",
        clock=VirtualClock(start=t0),
        adapters={"CMD": FakeAdapter(default=None)},
    )
    assert engine.journal is not None
    engine.journal.close()
    (period_dir(tmp_path / "root", 1) / "manifest.json").unlink()

    async def scenario() -> None:
        with pytest.raises(EngineError, match="pin is missing"):
            await resume_run(
                catalog,
                tmp_path / "root",
                clock=VirtualClock(start=t0 + timedelta(minutes=1)),
                adapters={"CMD": FakeAdapter(default=None)},
                settle_seconds=0.0,
                grace_seconds=0.0,
            )

    _asyncio.run(scenario())


def test_a_pinned_scheduled_root_refuses_a_schedulerless_resume(tmp_path: Path) -> None:
    """The profile cannot see a scheduler's ABSENCE (default_tz inherits the
    pin and reports no drift), so the refusal is its own: a pinned root
    whose catalog schedules jobs, resumed with no scheduler, would silently
    stop firing them."""
    import asyncio as _asyncio

    from dsl41.runner_adapters import FakeAdapter
    from dsl41.runner_scheduler import Scheduler
    from dsl41.runner_startup import resume_run, start_run

    scheduled = lower_source(
        "insert_job: tick\njob_type: c\ncommand: x\nmachine: m1\n"
        "date_conditions: 1\ndays_of_week: all\nstart_mins: 0\n"
    )
    t0 = datetime(2026, 7, 1, 8, 0)
    engine = start_run(
        scheduled,
        tmp_path / "root",
        clock=VirtualClock(start=t0),
        adapters={"CMD": FakeAdapter(default=None)},
        scheduler=Scheduler(scheduled, start=t0),
    )
    assert engine.journal is not None
    engine.journal.close()

    async def scenario() -> None:
        with pytest.raises(EngineError, match="wired no scheduler"):
            await resume_run(
                scheduled,
                tmp_path / "root",
                clock=VirtualClock(start=t0 + timedelta(minutes=1)),
                adapters={"CMD": FakeAdapter(default=None)},
                settle_seconds=0.0,
                grace_seconds=0.0,
            )

    _asyncio.run(scenario())


def test_a_staged_spawn_window_or_sm_version_fiction_refuses(tmp_path: Path) -> None:
    """The spawn window derives from the constant the machine actually runs,
    and the staged state-machine version must be the running one -- a v2 pin
    over a v1 engine would leave it running beneath a manifest it can never
    satisfy."""
    from dsl41.runner_adapters import FakeAdapter
    from dsl41.runner_startup import start_run

    catalog = lower_source(_SOLO_JIL)
    t0 = datetime(2026, 7, 1, 8, 0)

    fiction = stage_manifest(
        catalog,
        source_bundle_hash=EMPTY_BUNDLE_HASH,
        profile=RuntimeProfile(spawn_window_us=0),
        state_machine_version=1,
    )
    with pytest.raises(EngineError, match="spawn_window_us"):
        start_run(
            catalog,
            tmp_path / "window",
            clock=VirtualClock(start=t0),
            adapters={"CMD": FakeAdapter(default=None)},
            staged=fiction,
        )

    future_sm = stage_manifest(
        catalog,
        source_bundle_hash=EMPTY_BUNDLE_HASH,
        profile=RuntimeProfile(),
        state_machine_version=999,
    )
    with pytest.raises(EngineError, match="state_machine_version 999"):
        start_run(
            catalog,
            tmp_path / "smv",
            clock=VirtualClock(start=t0),
            adapters={"CMD": FakeAdapter(default=None)},
            staged=future_sm,
        )


def test_a_nested_profile_field_restored_from_its_default_refuses(tmp_path: Path) -> None:
    """The presence rule one level down: a profile field absent on the wire
    would be restored by pydantic, hash back to the recorded runtime_hash,
    and pass every gate while pinning nothing."""
    catalog = lower_source(_SOLO_JIL)
    manifest = genesis_manifest(catalog, clock_domain="virtual", state_machine_version=1)
    path = write_period_manifest(tmp_path, manifest)
    payload = json.loads(path.read_bytes())
    del payload["runtime_profile"]["cmd_grace_us"]
    path.write_bytes(canonical_bytes(payload))
    with pytest.raises(EngineError, match="runtime_profile missing cmd_grace_us"):
        read_period_manifest(tmp_path)


def test_a_segment_with_an_unknown_key_refuses(tmp_path: Path) -> None:
    """Exact means exact: required-fields-present alone would bless a record
    this schema does not describe."""
    from dsl41.period import check_segment_record

    catalog = lower_source(_SOLO_JIL)
    manifest = genesis_manifest(catalog, clock_domain="virtual", state_machine_version=1)
    record = segment_record(manifest, estate_id="e", at=datetime(2026, 7, 1, 8, 0))
    check_segment_record(record)  # the real writer's shape passes
    with pytest.raises(EngineError, match="unknown surprise"):
        check_segment_record({**record, "surprise": 1})


# ------------------------------------------------ round-4 review pins (DL-130)


def test_a_profile_mutated_after_hashing_refuses_at_the_write(tmp_path: Path) -> None:
    """frozen stops attribute assignment, not mutation of a dict field: a
    tz_aliases entry added after the hash was taken would commit a false pin
    that refuses its own resume. The write re-checks."""
    catalog = lower_source(_SOLO_JIL)
    manifest = genesis_manifest(catalog, clock_domain="virtual", state_machine_version=1)
    manifest.runtime_profile.tz_aliases["sneaky"] = "Europe/Zurich"
    with pytest.raises(EngineError, match="disagrees with itself"):
        write_period_manifest(tmp_path, manifest)


def test_a_pinned_root_refuses_resume_with_a_missing_adapter(tmp_path: Path) -> None:
    """The profile inherits pinned values for wiring it cannot see, so a
    missing CMD adapter drifts nothing -- and the job would reach RUNNING
    with no process behind it. A pinned estate refuses instead."""
    import asyncio as _asyncio

    from dsl41.runner_adapters import FakeAdapter
    from dsl41.runner_startup import resume_run, start_run

    catalog = lower_source(_SOLO_JIL)
    t0 = datetime(2026, 7, 1, 8, 0)
    engine = start_run(
        catalog,
        tmp_path / "root",
        clock=VirtualClock(start=t0),
        adapters={"CMD": FakeAdapter(default=None)},
    )
    assert engine.journal is not None
    engine.journal.close()

    async def scenario() -> None:
        with pytest.raises(EngineError, match="no adapter"):
            await resume_run(
                catalog,
                tmp_path / "root",
                clock=VirtualClock(start=t0 + timedelta(minutes=1)),
                adapters={},
                settle_seconds=0.0,
                grace_seconds=0.0,
            )

    _asyncio.run(scenario())


def test_an_unimplemented_manifest_artifact_version_refuses(tmp_path: Path) -> None:
    """(Round-4 refutation, kept as its pin.) The manifest is read through
    `canon.decode`, whose ingress refuses an artifact_format_version this
    binary does not implement (PR-08d) -- version 2 never reaches the
    identity checks at all."""
    catalog = lower_source(_SOLO_JIL)
    manifest = genesis_manifest(catalog, clock_domain="virtual", state_machine_version=1)
    path = write_period_manifest(tmp_path, manifest)
    payload = json.loads(path.read_bytes())
    payload["artifact_format_version"] = 2
    path.write_bytes(canonical_bytes(payload))
    with pytest.raises(EngineError, match="artifact_format_version 2"):
        read_period_manifest(tmp_path)


def test_journal_create_refuses_the_retired_recipe_by_name(tmp_path: Path) -> None:
    """The SECOND owner of the D4 question (DL-138): journal creation. A new
    log pinned under the retired v1 recipe would refuse this unchanged
    estate at the next patch release -- the exact outage v2 exists to end --
    and the refusal names the recipe and the entry that retired it.

    Driven at `Journal.create` itself, and again at the manifest gate that
    runs before it on the `start_run` path: FOUR owners ask the D4 question
    and every one of them must answer it the same way."""
    from dsl41.runner_adapters import FakeAdapter
    from dsl41.runner_journal import Journal
    from dsl41.runner_startup import start_run

    catalog = lower_source(_SOLO_JIL)
    v1_pin = Manifest(
        catalog_hash=_retired_v1(catalog),
        catalog_hash_version=1,
        source_bundle_hash=EMPTY_BUNDLE_HASH,
        runtime_profile=RuntimeProfile(),
        runtime_hash=runtime_hash(RuntimeProfile()),
        state_machine_version=1,
        period_id=1,
        segment_no=1,
        baseline_id="sha256:" + "0" * 64,
        clock_domain="virtual",
        first_index=1,
    )
    with pytest.raises(EngineError, match="RETIRED") as caught:
        Journal.create(
            tmp_path / "journal.jsonl",
            catalog=catalog,
            clock_domain="virtual",
            started_at=datetime(2026, 7, 1, 8, 0),
            manifest=v1_pin,
        )
    # the `where` names WHICH owner refused -- without it this assertion is
    # also satisfied by `catalog_hash_at` one line further on, and deleting
    # the gate under test would leave the test green
    assert "DL-138" in str(caught.value) and "a new log's manifest" in str(caught.value)
    staged = StagedManifest(
        catalog_hash=_retired_v1(catalog),
        catalog_hash_version=1,
        source_bundle_hash=EMPTY_BUNDLE_HASH,
        runtime_profile=RuntimeProfile(),
        runtime_hash=runtime_hash(RuntimeProfile()),
        state_machine_version=1,
    )
    with pytest.raises(EngineError, match="RETIRED") as caught:
        start_run(
            catalog,
            tmp_path / "root",
            clock=VirtualClock(start=datetime(2026, 7, 1, 8, 0)),
            adapters={"CMD": FakeAdapter(default=None)},
            staged=staged,
        )
    # the manifest gate runs first on this path, and it says so
    assert "DL-138" in str(caught.value)
    assert "periods/000001/manifest.json" in str(caught.value)


def test_the_model_itself_normalizes_as_machine() -> None:
    """ss2.1 says the field IS sorted and de-duplicated -- whoever
    constructed it. Two spellings of one machine set are one profile and
    one hash."""
    a = RuntimeProfile(as_machine=("b", "a", "b"))
    b = RuntimeProfile(as_machine=("a", "b"))
    assert a.as_machine == ("a", "b")
    assert runtime_hash(a) == runtime_hash(b)


# ------------------------------------------------ round-5 review pins (DL-130)


def test_genesis_refuses_a_catalog_it_cannot_dispatch(tmp_path: Path) -> None:
    """The adapter gate runs at GENESIS too: an estate that opens unable to
    dispatch its own catalog is the same silent no-process hole one gate
    later."""
    from dsl41.runner_startup import start_run

    with pytest.raises(EngineError, match="genesis wired no adapter"):
        start_run(
            lower_source(_SOLO_JIL),
            tmp_path / "root",
            clock=VirtualClock(start=datetime(2026, 7, 1, 8, 0)),
            adapters={},
        )


def test_a_staged_wrong_artifact_version_refuses_at_the_write(tmp_path: Path) -> None:
    """A staged artifact_format_version 2 would open an estate whose own
    resume refuses it at the ingress -- caught at the write instead."""
    catalog = lower_source(_SOLO_JIL)
    manifest = genesis_manifest(
        catalog, clock_domain="virtual", state_machine_version=1
    ).model_copy(update={"artifact_format_version": 2})
    with pytest.raises(EngineError, match="artifact_format_version 2"):
        write_period_manifest(tmp_path, manifest)


def test_the_normalizer_never_launders_a_wrong_typed_machine() -> None:
    """A `7` in as_machine is refused, not spelled "7": the before-validator
    must not convert what strict validation exists to catch."""
    from pydantic import ValidationError as _VE

    with pytest.raises(_VE, match="as_machine"):
        RuntimeProfile(as_machine=(7,))  # type: ignore[arg-type]


def _native_opening(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    """A genesis WAL and the `segment` record it opens with -- the real
    writer's, so a D4 pin rewrites ONE field of a valid file rather than
    hand-building a record the writer would never emit."""
    from dsl41.runner_adapters import FakeAdapter
    from dsl41.runner_startup import start_run

    engine = start_run(
        lower_source(_SOLO_JIL),
        tmp_path / "root",
        clock=VirtualClock(start=datetime(2026, 7, 1, 8, 0)),
        adapters={"CMD": FakeAdapter(default=None)},
    )
    assert engine.journal is not None
    engine.journal.close()
    path = tmp_path / "root" / "wal" / "000001.jsonl"
    segment = json.loads(path.read_text().splitlines()[0])
    assert segment["rec"] == "segment"  # the WAL, not the sentinel beside it
    return path, segment


def _rewrite_journal(path: Path, *records: dict[str, Any]) -> None:
    path.write_text("\n".join(json.dumps(r, sort_keys=True) for r in records) + "\n")


def test_the_journal_reader_refuses_the_retired_recipe_by_name(tmp_path: Path) -> None:
    """The THIRD owner of the D4 question (DL-138): the journal reader. A
    segment written under `catalog_hash_version: 1` is a root written under
    a retired recipe, and it is told so BY NAME rather than left to a later
    gate reporting "the estate changed".

    The record here is the one v1 REALLY wrote: a bare hexdigest
    `catalog_hash` -- v1 predates the `sha256:` grammar -- and the
    `catalog_hash_v1` field D3 deleted from the schema. Both trip a
    CURRENT-dialect check, and either verdict would tell an operator
    holding a pre-DL-138 root that their bytes are malformed. The version
    verdict owns the file's fate, so it is dispatched first (DL-138, the L2
    review)."""
    from dsl41.runner_journal import read_journal

    path, segment = _native_opening(tmp_path)
    segment["catalog_hash_version"] = 1
    segment["catalog_hash"] = "0" * 64  # v1 spelled the address bare
    segment["catalog_hash_v1"] = "0" * 64  # and carried the field D3 deleted
    _rewrite_journal(path, segment)
    with pytest.raises(EngineError) as caught:
        read_journal(path)
    named = str(caught.value)
    # only the D4 dispatcher can say this; the grammar check and the
    # unknown-key check each say something else entirely
    assert "catalog_hash_version 1 is a RETIRED dialect" in named
    assert "DL-138" in named and "segment record" in named
    assert "not a sha256" not in named and "carries unknown" not in named


def test_the_openings_version_is_dispatched_before_any_record_is_read(
    tmp_path: Path,
) -> None:
    """Ordering, pinned (DL-138, the L2 review): the opening `segment` names
    the dialect the WHOLE file is written in, so its version verdict is taken before any
    record -- the opening's own fields included -- is validated.

    A reader that validated records first answered a question about a
    dialect it cannot read: an unsupported opening followed by a record kind
    this build never heard of reported the unknown KIND, and the operator
    went looking for corruption instead of for the retirement their root
    predates.

    The control is the same file at the CURRENT version, twice: an unknown
    kind is still refused as an unknown kind, and the ss2.1 schema still
    runs over the opening. Dispatching first narrows what the schema is
    asked about; it does not switch it off."""
    from dsl41.runner_journal import read_journal

    path, native = _native_opening(tmp_path)
    stranger = {"rec": "wat", "seq": 2, "at": "2026-07-01T08:01:00"}

    _rewrite_journal(path, {**native, "catalog_hash_version": 1}, stranger)
    with pytest.raises(EngineError) as retired:
        read_journal(path)
    assert "catalog_hash_version 1 is a RETIRED dialect" in str(retired.value)
    assert "unknown record kind" not in str(retired.value)

    _rewrite_journal(path, {**native, "catalog_hash_version": 7}, stranger)
    with pytest.raises(EngineError) as unsupported:
        read_journal(path)
    assert "catalog_hash_version 7: this binary implements" in str(unsupported.value)
    assert "unknown record kind" not in str(unsupported.value)

    _rewrite_journal(path, native, stranger)
    with pytest.raises(EngineError, match="unknown record kind 'wat'"):
        read_journal(path)

    _rewrite_journal(path, {**native, "catalog_hash": "0" * 64})
    with pytest.raises(EngineError, match="segment record: catalog_hash is"):
        read_journal(path)


# ------------------------------------------------ round-6 review pins (DL-130)


def test_genesis_refuses_a_scheduled_catalog_with_no_or_the_wrong_scheduler(
    tmp_path: Path,
) -> None:
    """The scheduler gate runs at genesis too -- and it checks the CATALOG
    the scheduler compiled, because one built over another estate's plans
    passes every timezone comparison while firing nothing of this one's."""
    from dsl41.runner_adapters import FakeAdapter
    from dsl41.runner_scheduler import Scheduler
    from dsl41.runner_startup import start_run

    scheduled = lower_source(
        "insert_job: tick\njob_type: c\ncommand: x\nmachine: m1\n"
        "date_conditions: 1\ndays_of_week: all\nstart_mins: 0\n"
    )
    t0 = datetime(2026, 7, 1, 8, 0)
    with pytest.raises(EngineError, match="genesis wired no scheduler"):
        start_run(
            scheduled,
            tmp_path / "none",
            clock=VirtualClock(start=t0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
    other = lower_source(_SOLO_JIL)
    with pytest.raises(EngineError, match="different catalog"):
        start_run(
            scheduled,
            tmp_path / "wrong",
            clock=VirtualClock(start=t0),
            adapters={"CMD": FakeAdapter(default=None)},
            scheduler=Scheduler(other, start=t0),
        )


def test_a_float_spelled_version_never_passes_the_segment_schema() -> None:
    """JSON loads `2.0` as a float that compares equal to 2; the schema is
    int-first."""
    from dsl41.period import check_segment_record

    catalog = lower_source(_SOLO_JIL)
    manifest = genesis_manifest(catalog, clock_domain="virtual", state_machine_version=1)
    record = segment_record(manifest, estate_id="e", at=datetime(2026, 7, 1, 8, 0))
    with pytest.raises(EngineError, match="catalog_hash_version"):
        check_segment_record({**record, "catalog_hash_version": 2.0})


def test_an_address_outside_the_grammar_refuses_at_the_write(tmp_path: Path) -> None:
    """`source_bundle_hash: "x"` is not an address; a native period must
    never open under one."""
    catalog = lower_source(_SOLO_JIL)
    bad = stage_manifest(
        catalog,
        source_bundle_hash=EMPTY_BUNDLE_HASH,
        profile=RuntimeProfile(),
        state_machine_version=1,
    ).model_copy(update={"source_bundle_hash": "x"})
    manifest = bad.commit(
        period_id=1, baseline_id="b", clock_domain="virtual", segment_no=1, first_index=1
    )
    with pytest.raises(EngineError, match="not a sha256 address"):
        write_period_manifest(tmp_path, manifest)


def test_a_catalog_mutated_after_the_plans_compiled_refuses(tmp_path: Path) -> None:
    """The scheduler pins its catalog's v2 hash AT COMPILE TIME, so an
    object mutated afterwards cannot pass by identity: the plans are stale
    the moment the catalog moved, whoever holds the reference."""
    from dsl41.runner_adapters import FakeAdapter
    from dsl41.runner_scheduler import Scheduler
    from dsl41.runner_startup import start_run

    scheduled = lower_source(
        "insert_job: tick\njob_type: c\ncommand: x\nmachine: m1\n"
        "date_conditions: 1\ndays_of_week: all\nstart_mins: 0\n"
    )
    t0 = datetime(2026, 7, 1, 8, 0)
    scheduler = Scheduler(scheduled, start=t0)
    scheduled.jobs["tick"].exec_.command = "y"  # mutated AFTER the compile
    with pytest.raises(EngineError, match="different catalog"):
        start_run(
            scheduled,
            tmp_path / "stale",
            clock=VirtualClock(start=t0),
            adapters={"CMD": FakeAdapter(default=None)},
            scheduler=scheduler,
        )


def test_a_scheduler_over_a_detriggered_catalog_still_refuses(tmp_path: Path) -> None:
    """Removing the catalog's last trigger after the compile must not skip
    the hash comparison: the stale plans would still fire the removed
    jobs. Every supplied scheduler is held to its compile-time hash."""
    from dsl41.runner_adapters import FakeAdapter
    from dsl41.runner_scheduler import Scheduler
    from dsl41.runner_startup import start_run

    scheduled = lower_source(
        "insert_job: tick\njob_type: c\ncommand: x\nmachine: m1\n"
        "date_conditions: 1\ndays_of_week: all\nstart_mins: 0\n"
    )
    t0 = datetime(2026, 7, 1, 8, 0)
    scheduler = Scheduler(scheduled, start=t0)
    scheduled.jobs["tick"].schedule = None  # the last trigger, removed post-compile
    with pytest.raises(EngineError, match="different catalog"):
        start_run(
            scheduled,
            tmp_path / "stale",
            clock=VirtualClock(start=t0),
            adapters={"CMD": FakeAdapter(default=None)},
            scheduler=scheduler,
        )


def test_the_schedulers_compile_time_inputs_are_read_only() -> None:
    """A post-compile edit of default_tz or tz_aliases would pin one
    timezone while the plans execute under another: the attributes are
    properties over compile-time copies, and mutating the returned mapping
    moves nothing."""
    catalog = lower_source(_SOLO_JIL)
    from dsl41.runner_scheduler import Scheduler

    scheduler = Scheduler(catalog, start=datetime(2026, 7, 1, 8, 0), default_tz="UTC")
    with pytest.raises(AttributeError):
        scheduler.default_tz = "Europe/Zurich"  # type: ignore[misc]
    scheduler.tz_aliases["x"] = "y"  # a copy: the compile-time inputs are unmoved
    assert scheduler.tz_aliases == {}


# --------------------------------------------------- arch-review pins (DL-137)


def test_the_resume_sweep_rejects_a_noncanonical_run_directory(tmp_path: Path) -> None:
    """DL-137: `runs/b.01` aliases `b.1`, and the resume sweep's old inline
    parser accepted it -- sorted-first, it could answer the ss7 ladder for
    a run this estate never wrote. One parser now (period.split_run_dir),
    and it refuses the spelling."""
    from dsl41.period import split_run_dir

    assert split_run_dir("b.1") == ("b", 1)
    assert split_run_dir("a.b.12") == ("a.b", 12)
    assert split_run_dir("b.01") is None  # non-canonical: never a run
    assert split_run_dir("b.") is None and split_run_dir(".1") is None


def test_a_bad_machine_policy_is_a_refusal_on_every_verb(tmp_path: Path) -> None:
    """DL-137: `--machine-policy bogus` was a clean exit-2 on `run` and an
    uncaught ValidationError (exit 1, documented as an estate failure) on
    `seal` and `estate adopt`. One guard now, in `_next_profile`."""
    from typer.testing import CliRunner

    from dsl41.cli import app

    result = CliRunner().invoke(
        app,
        [
            "seal",
            "--run-root",
            str(tmp_path / "nowhere"),
            "--next",
            str(tmp_path / "nowhere.jil"),
            "--next-machine-policy",
            "bogus",
        ],
    )
    assert result.exit_code == 2
    assert "expected strict|local-eligible" in result.output
