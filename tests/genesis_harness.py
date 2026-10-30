"""A run root opened the way `dsl41 run` opens one, for tests that need a real genesis.

The helpers here build a two-job catalog, stage its inputs and start the engine on a virtual
clock; `_resume` reopens the same root.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from dsl41.ast_jil import parse, render_preserve
from dsl41.ir import CatalogIR, lower_catalog
from dsl41.run_root import RuntimeProfile, SourceFile, stage_manifest, write_bundle
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_clock import VirtualClock
from dsl41.runner_ledger import STATE_MACHINE_VERSION
from dsl41.runner_startup import resume_run, start_run

T0 = datetime(2026, 7, 1, 8, 0)

C1_JIL = "insert_job: a\njob_type: c\ncommand: x\n\ninsert_job: b\njob_type: c\ncommand: y\n"


def _catalog(text: str, *, name: str = "estate.jil") -> tuple[CatalogIR, list[SourceFile]]:
    parsed = [parse(text, file=name)]
    return lower_catalog(parsed), [SourceFile(path=name, text=render_preserve(parsed[0]))]


def _genesis(
    run_root: Path,
    *,
    text: str = C1_JIL,
    clock: VirtualClock | None = None,
    profile: RuntimeProfile | None = None,
    adapter: FakeAdapter | None = None,
):
    """A run root opened and staged exactly as `dsl41 run` stages it. The default adapter parks
    every run."""
    catalog, sources = _catalog(text)
    staged = stage_manifest(
        catalog,
        source_bundle_hash=write_bundle(run_root, sources),
        profile=profile or RuntimeProfile(),
        state_machine_version=STATE_MACHINE_VERSION,
    )
    return start_run(
        catalog,
        run_root,
        clock=clock or VirtualClock(start=T0),
        adapters={"CMD": adapter or FakeAdapter(default=None)},
        staged=staged,
    )


def _close(engine) -> None:
    if engine.journal is not None:
        engine.journal.close()


def _resume(run_root: Path, text: str, *, clock: VirtualClock | None = None):
    catalog, _ = _catalog(text)
    return asyncio.run(
        resume_run(
            catalog,
            run_root,
            clock=clock or VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
    )
