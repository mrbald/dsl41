"""A run root opened the way `dsl41 run` opens one, for tests that need a real genesis.

The helpers here build a two-job catalog and start the engine on a virtual clock; `_resume`
reopens the same root.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from dsl41.ast_jil import parse
from dsl41.ir import CatalogIR, lower_catalog
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_clock import VirtualClock
from dsl41.runner_config import RuntimeProfile
from dsl41.runner_startup import resume_run, start_run

T0 = datetime(2026, 7, 1, 8, 0)

C1_JIL = "insert_job: a\njob_type: c\ncommand: x\n\ninsert_job: b\njob_type: c\ncommand: y\n"


def _catalog(text: str, *, name: str = "estate.jil") -> tuple[CatalogIR, list[str]]:
    """The catalog of `text`, and its source texts."""
    return lower_catalog([parse(text, file=name)]), [text]


def _genesis(
    run_root: Path,
    *,
    text: str = C1_JIL,
    clock: VirtualClock | None = None,
    profile: RuntimeProfile | None = None,
    adapter: FakeAdapter | None = None,
):
    """A run root opened exactly as `dsl41 run` opens one. The default adapter parks every
    run."""
    catalog, _ = _catalog(text)
    return start_run(
        catalog,
        run_root,
        clock=clock or VirtualClock(start=T0),
        adapters={"CMD": adapter or FakeAdapter(default=None)},
        launched=profile or RuntimeProfile(),
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
