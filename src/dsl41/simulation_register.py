"""The simulation coverage register (DL-209): the row model, the closed surface list and the
markdown renderer.

Normative prose: `docs/simulation-coverage.md`. The rows are literal data in
`simulation_register_rows.py`. `tests/test_simulation_register.py` derives every surface's
domain from the code's own inventories and fails on a missing or stale row, or one that
disagrees with the doc.

The register states exposure, not application: a row says what the simulation does with a
behaviour it can meet, never that a particular run met it. `render_markdown` prints `none` in
the detector column for a reachable row that is not a plain member of a derived surface (it
has scope fixtures and no collector), `generic` for a plain member of a derived surface, and
`unreachable` for a member no input can reach (`detector_of`).

A row's `trigger` and `quiet` fixtures are strings the test interprets against the surface's
fixture kind, so the register holds no second copy of the scanner, the oracle or the calendar
parser.
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


#: Every surface the register covers, in rendering order. The list is closed: the test refuses
#: a row naming another surface. Most surfaces are derived: the test computes the member set
#: from a code object (an inventory, a Literal, a grammar file) and fails on a missing or
#: stale member. `runtime` and `adapter_policy` are free: no inventory enumerates their
#: behaviours, so their rows are hand-listed.
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
    "cal_workday_form",
    "cal_row_form",
    "event",
    "status",
    "timer",
    "profile_field",
    "profile_alt",
    "adapter_outcome",
    "wrapper_outcome",
    "event_source",
    "trace_marker",
    "preflight_code",
    "demand_mode",
    "machine_verdict",
    "literal_alt",
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
    #: substring of a `### ` heading in `docs/live-instance-runbook.md`
    protocol: str | None = None
    #: a bounded search states its bound ("731 days", "60 years")
    bound: str | None = None
    #: the exact regex this row's member is recognised by, pinned verbatim; an edit to the
    #: code's regex fails until the row is re-read
    pattern: str | None = None
    #: `<module>.<qualname>#<n>` for each `PENDING` marker this row claims; the label is the
    #: row's. Every marker site in the sources is claimed by exactly one row, so a second
    #: default under a used label needs its own row.
    sites: tuple[str, ...] = ()
    #: False for a member no input can reach, such as a defensive second gate behind an
    #: earlier refusal: it has a row but no detector, and the test holds the claim.
    reachable: bool = True
    #: one sentence: what is modelled, or what is not
    effect: str
    #: scope fixture where the behaviour MAY apply
    trigger: str
    #: scope fixture where it does not; None = the surface's base fixture
    quiet: str | None = None

    @property
    def id(self) -> str:
        return f"{self.surface}:{self.member}" + (f"#{self.facet}" if self.facet else "")

    @property
    def marker(self) -> bool:
        """True when the row claims `PENDING: <label>` marker sites in `src/dsl41`."""
        return bool(self.sites)


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
    """What reads this row at runtime: the surface's generic detector for a plain member of a
    derived surface, `unreachable` for a member no input reaches, else `none`; no run output
    may claim a `none` row was assessed."""
    if not row.reachable:
        return "unreachable"
    if row.facet == "" and row.surface not in FREE_SURFACES:
        return "generic"
    return "none"


def _cell(text: str | None, empty: str = "-") -> str:
    return (text or empty).replace("|", "\\|")


def render_markdown() -> str:
    """The register as one markdown table per surface, in SURFACES order; deterministic. The
    generated block of `docs/simulation-coverage.md` is exactly this string."""
    out: list[str] = []
    for surface in SURFACES:
        rows = rows_for(surface)
        if not rows:
            continue
        out.append(f"### {surface}")
        out.append("")
        out.append("| id | class | cite | label | marker | protocol | bound | detector | effect |")
        out.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for row in rows:
            out.append(
                f"| {_cell(row.id)} | {row.klass.value} | {_cell(row.cite)}"
                f" | {_cell(row.label)} | {'yes' if row.marker else ''}"
                f" | {_cell(row.protocol, '')} | {_cell(row.bound, '')}"
                f" | {detector_of(row)} | {_cell(row.effect)} |"
            )
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"
