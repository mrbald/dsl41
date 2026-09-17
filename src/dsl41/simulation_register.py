"""The simulation coverage register (DL-209): the row model, the closed
surface list, and the markdown renderer.

Normative prose: `docs/simulation-coverage.md`. The rows themselves are
literal data in `simulation_register_rows.py`; this module gives them a
type, an order, and one rendering. `tests/test_simulation_register.py`
derives every surface's domain from the code's own inventories and fails
when a row is missing, stale, or disagrees with the doc.

The register states EXPOSURE, not application: a row says what the
simulation does with a behaviour it can meet, never that a particular run
met it. Collectors that record applications arrive in later slices, which
is why `render_markdown` prints `detector: none` for every row that has
scope fixtures and no collector yet.

Rows are data, so they carry no code: a row's `trigger`/`quiet` fixtures are
strings the test interprets against the surface's fixture kind. Keeping the
interpretation in the test is deliberate -- the register must not grow a
second, drifting copy of the scanner, the oracle or the calendar parser.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict

from dsl41.simulation_register_rows import ROWS


class Klass(Enum):
    """What the simulation does with a behaviour it can meet."""

    #: modelled; the citation says how
    SUPPORTED = "supported"
    #: a pinned default under an open question; the label names the question
    PROVISIONAL = "provisional"
    #: refused loudly; the citation is the refusing site
    REFUSED = "refused"
    #: carried verbatim, effect not implemented; `effect` names what is not
    PASSTHROUGH = "passthrough"


#: Every surface the register covers, in rendering order. CLOSED: a surface
#: that is not here has no rows, and the test refuses a row naming one.
#: Most are DERIVED -- the test computes the member set from a code object
#: (an inventory, a Literal, a grammar file) and fails on a missing OR a
#: stale member. `runtime` and `adapter_policy` are free: they name
#: behaviours no inventory enumerates, so their rows are hand-listed.
SURFACES: tuple[str, ...] = (
    "statement",
    "job_attr",
    "machine_attr",
    "resource_attr",
    "xinst_attr",
    "global_attr",
    "calendar_attr",
    "job_type",
    "bool_spelling",
    "day_token",
    "initial_status",
    "machine_type",
    "res_type",
    "free_code",
    "release_policy",
    "cond_rule",
    "cond_terminal",
    "lookback_kind",
    "atom_status",
    "cal_keyword",
    "cal_family",
    "cal_operator",
    "cal_action",
    "event",
    "status",
    "timer",
    "profile_field",
    "profile_alt",
    "adapter_outcome",
    "runtime",
    "adapter_policy",
)


class Behaviour(BaseModel):
    """One row: a behaviour the simulation can meet, and what it does with
    it. Frozen and closed -- a misspelled field is an import-time error, not
    a silently ignored key."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: member of SURFACES
    surface: str
    #: the domain key inside the surface (an attribute, a token, a kind)
    member: str
    #: sub-behaviour tag; the id carries it after a `#`
    facet: str = ""
    #: bumps when the semantic branch behind this row changes
    revision: int = 1
    klass: Klass
    #: SEM-xx / DL-xx / "<doc> ssN", or `module.function` for a refusal site
    cite: str
    #: the open question this row pins a default for (Q3c, Qr4, E7, ...)
    label: str | None = None
    #: True when `src/dsl41` carries a `PENDING: <label>` marker for it
    marker: bool = False
    #: substring of a `### ` heading in `docs/live-instance-runbook.md`
    protocol: str | None = None
    #: a bounded search states its bound ("731 days", "60 years")
    bound: str | None = None
    #: one sentence: what is modelled, or what is not
    effect: str
    #: scope fixture where the behaviour MAY apply
    trigger: str
    #: scope fixture where it does not; None = the surface's base fixture
    quiet: str | None = None

    @property
    def id(self) -> str:
        return f"{self.surface}:{self.member}" + (f"#{self.facet}" if self.facet else "")


REGISTER: tuple[Behaviour, ...] = tuple(Behaviour.model_validate(row) for row in ROWS)

#: Surfaces whose member set no inventory enumerates (see SURFACES).
FREE_SURFACES: frozenset[str] = frozenset({"runtime", "adapter_policy"})


def by_id() -> dict[str, Behaviour]:
    """Every row by its id. Ids are unique (the test pins it)."""
    return {row.id: row for row in REGISTER}


def rows_for(surface: str) -> tuple[Behaviour, ...]:
    """This surface's rows, id-ordered -- the order `render_markdown` prints."""
    return tuple(sorted((r for r in REGISTER if r.surface == surface), key=lambda r: r.id))


def detector_of(row: Behaviour) -> str:
    """What reads this row at runtime today. A member row on a derived
    surface has the surface's generic detector; everything else waits for a
    collector, and until one lands no run output may claim the row was
    assessed."""
    if row.facet == "" and row.surface not in FREE_SURFACES:
        return "generic"
    return "none"


def _cell(text: str | None) -> str:
    return (text or "-").replace("|", "\\|")


def render_markdown() -> str:
    """The register as one markdown table per surface, in SURFACES order.
    Pure and deterministic: same rows in, same bytes out. The generated
    block of `docs/simulation-coverage.md` is exactly this string, and the
    test fails when the two differ."""
    out: list[str] = []
    for surface in SURFACES:
        rows = rows_for(surface)
        if not rows:
            continue
        out.append(f"### {surface}")
        out.append("")
        out.append("| id | class | cite | label | detector | effect |")
        out.append("| --- | --- | --- | --- | --- | --- |")
        for row in rows:
            out.append(
                f"| {_cell(row.id)} | {row.klass.value} | {_cell(row.cite)}"
                f" | {_cell(row.label)} | {detector_of(row)} | {_cell(row.effect)} |"
            )
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"
