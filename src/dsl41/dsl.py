"""DSL surface + decompiler (phase 10, CLAUDE.md / DL-03 -- deliberately LAST).

The surface is EXTRACTED from patterns the corpus shows, not designed up
front (DL-03: "do not design combinators speculatively"). The corpus shows:
plain CMD jobs with exec/schedule/condition attributes, boxes with members
(one level of the tree exercised; nesting supported because the IR does),
string conditions in the existing condition language, globals, machines,
resources (opaque records, DL-18), external instances, pure-s() chains, and
a same-producer parallel pair. That yields exactly the four builders
ir-design D2 names -- job(), box(), sequence(), parallel() -- plus the
record declarations. Nothing else. Definition-time `status:` (SEM-24)
round-trips as a plain job kwarg -- attribute, not combinator.

Design decisions (each with a test; recorded as DL-17):
- The builder GENERATES JIL and reuses the tested pipeline (to_jil() ->
  parse -> lower). There is no second lowering path: what the DSL builds is
  exactly what the compiler front end accepts, byte for byte. Values are
  validated against JIL's line discipline (no newlines/control chars; keys
  identifier-shaped) and refused loudly otherwise.
- Conditions are STRINGS in the existing condition language -- the corpus
  shows no combinator condition syntax, and inventing one would be
  speculative. cond_to_source() renders a Cond tree back to that language
  such that parse_condition(cond_to_source(c)) equals c modulo spans
  (structure preserved by parenthesizing every nested group; lookback
  emitted from the preserved raw token, reconstructed from kind/minutes
  for hand-built trees).
- sequence(*names) wires already-declared jobs into an s()-chain: each
  follower must have been declared WITHOUT a condition (loud error
  otherwise -- silently merging conditions would be silent loss).
  parallel(names, after=..., then=...) fans out members from a common
  producer (each member gets s(after)) and optionally fans back in (the
  `then` job gets the conjunction of member successes). Both are sugar
  over the same condition strings the corpus shows.
- The decompiler emits sequence() only where the derived chain's followers
  each carry EXACTLY the single atom s(prev) (no lookback, no instance, no
  extra conjuncts -- derive's chains are adjacency-based and the corpus's
  mutex_b chain shows adjacency alone is not enough); parallel() (DL-37)
  where >= 2 jobs' conditions are each exactly s(p) for one in-catalog
  producer p (fan-out), plus the UNIQUE join whose condition is exactly the
  conjunction of the members' plain successes (fan-in; zero or ambiguous
  joins stay explicit). The two sugars are naturally disjoint: a fan-out
  member has >= 2 sibling successors of p, so derive's single-successor
  chain linkage can never claim it. Everything else stays an explicit
  job(condition=...) call. Decompile output is a runnable Python module:
  executing it rebuilds a catalog whose canonical form equals the
  original's (the round-trip property, tested corpus-wide); run as a
  script it prints the rebuilt JIL for diff loops.
- FW jobs round-trip through job(watch_file=...) kwargs -- mirroring the
  existing IR model is mechanical, not combinator design.
- Fold registry (DL-38): every decompiler transform beyond verbatim emission
  is a CLOSED, coded set (FOLDS: T-001..T-007), each derivable from graph
  shape / typed lanes alone (no naming or domain knowledge) and each
  guaranteed by construction: detection is exact-shape (stricter than
  derive's classifiers), reconstruction is canonical-hash-neutral (canonical
  form sorts/dedups conjuncts, so mutex() conjoin order cannot matter), and
  `decompile --check` is the backstop. Callers opt out per code via
  decompile(disable=...) / CLI --no-fold. Detection runs on RESIDUAL
  conditions (T-005 mutex atoms stripped first), so folds compose: a link
  `n(m) & s(prev)` folds as sequence + mutex, and the emitted module's
  wiring order (sequences/parallels, then mutex) re-conjoins them. Standing
  exclusions, never folded: lookback-qualified atoms (Q2), cross-instance
  refs (M33), exit-code atoms, box_success/box_failure overrides (SEM-12),
  one-way or nested n(), run_window (M27). Estate-specific idioms are NOT
  built-ins; they go through the custom-pattern door when it lands.
"""

from __future__ import annotations

import keyword
import re
from collections.abc import Collection, Sequence

from dsl41.conditions import (
    And,
    Cond,
    ExitCodeAtom,
    Lookback,
    Or,
    Paren,
    StatusAtom,
    escape_job_name,
)
from dsl41.derive import DerivedGraph, derive_graph
from dsl41.ir import CatalogIR, JobIR, ScheduleBlock, SlaSpec, Time, lower_source

_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
#: The scanner's rule-1 attribute-line shape (key prefix + unescaped colon);
#: a calendar date row matching it would re-parse as an attribute (DL-36).
_ATTR_LINE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*:")
_CTRL_RE = re.compile(r"[\r\n\x00]")

_STATUS_LETTER = {
    "SUCCESS": "s",
    "FAILURE": "f",
    "DONE": "d",
    "TERMINATED": "t",
    "NOTRUNNING": "n",
}

#: Statuses a chain link / fan-out member may fold through. Bare NOTRUNNING
#: is mutex-classified (M07, T-005), lookback-n() stays an explicit edge.
_FOLDABLE_STATUS = {k: v for k, v in _STATUS_LETTER.items() if k != "NOTRUNNING"}

_BARE_GLOBAL_VALUE_RE = re.compile(r"[A-Za-z0-9_.\-]+\Z")


class DslError(ValueError):
    pass


#: The closed fold registry (DL-38; module docstring). Codes are stable API:
#: `decompile(disable=...)` and the CLI --no-fold validate against this set.
#: Dependencies: disabling T-001 also stops T-002 s-runs, T-003, and ALL
#: fan-in joins (then=/then_any= both refine the sequence/parallel
#: machinery); T-004 alone still folds f/d/t fan-outs, join-less.
FOLDS: dict[str, str] = {
    "T-001": "plain s() chains -> sequence(); same-producer fan-out/fan-in -> parallel()",
    "T-002": "sub-chain splitting: fold maximal qualifying runs inside disqualified chains",
    "T-003": "any-of fan-in: the unique OR-of-member-successes join -> parallel(then_any=)",
    "T-004": "uniform f()/d()/t() links -> sequence(link=) / parallel(on=)",
    "T-005": "symmetric bare top-level n() conjunct pairs -> mutex()",
    "T-006": "identical single-group resources: attrs across jobs -> contend()",
    "T-007": "identical schedule blocks factored into shared module-level dicts",
}


# ------------------------------------------------------------- condition rendering


def _lookback_token(lookback: Lookback) -> str:
    if lookback.raw:
        return lookback.raw
    if lookback.kind == "zero":
        return "0"
    if lookback.kind == "indefinite":
        return "9999"
    assert lookback.minutes is not None
    hours, minutes = divmod(lookback.minutes, 60)
    return f"{hours:02d}.{minutes:02d}"


def _job_ref_source(atom: StatusAtom | ExitCodeAtom) -> str:
    name = escape_job_name(atom.job.name)
    if atom.job.instance is not None:
        name += f"^{atom.job.instance}"
    return name


def cond_to_source(cond: Cond) -> str:
    """Render a Cond tree back to condition-language source; nested groups
    are parenthesized so the flat (equal-precedence) parse preserves the
    tree exactly (see module docstring)."""
    if isinstance(cond, Paren):
        return f"({cond_to_source(cond.inner)})"
    if isinstance(cond, (And, Or)):
        joiner = " & " if isinstance(cond, And) else " | "
        parts = [
            f"({cond_to_source(op)})" if isinstance(op, (And, Or)) else cond_to_source(op)
            for op in cond.operands
        ]
        return joiner.join(parts)
    if isinstance(cond, StatusAtom):
        letter = _STATUS_LETTER[cond.status]
        ref = _job_ref_source(cond)
        if cond.lookback is not None:
            return f"{letter}({ref}, {_lookback_token(cond.lookback)})"
        return f"{letter}({ref})"
    if isinstance(cond, ExitCodeAtom):
        ref = _job_ref_source(cond)
        if cond.lookback is not None:
            return f"e({ref}, {_lookback_token(cond.lookback)}) {cond.op} {cond.value}"
        return f"e({ref}) {cond.op} {cond.value}"
    value = cond.value
    if not _BARE_GLOBAL_VALUE_RE.match(value):
        if '"' in value:
            raise DslError(f"global value {value!r} cannot be rendered (embedded quote)")
        value = f'"{value}"'
    return f"v({cond.name}) {cond.op} {value}"


# ------------------------------------------------------------------------- builder


def _check_value(kind: str, value: str) -> str:
    if _CTRL_RE.search(value):
        raise DslError(f"{kind} value {value!r} carries newline/control characters")
    return value


def _check_name(kind: str, name: str) -> str:
    _check_value(kind, name)
    if not name or " " in name or "\t" in name:
        raise DslError(f"{kind} name {name!r} is not JIL-name-shaped")
    return name


def _check_wirable(fn: str, name: str) -> str:
    """Names the wiring builders interpolate into GENERATED condition atoms
    must be carryable by the grammar's JOB_NAME token: colon is the only
    escapable metachar (DL-39); whitespace, `( ) , ^ & |`, and backslash
    cannot be referenced from condition syntax at all -- emitting them
    would silently re-target the atom (e.g. `^` reads as an instance
    qualifier, M33)."""
    if any(ch in "(),^&|\\" or ch.isspace() for ch in name):
        raise DslError(
            f"{fn}() cannot wire job {name!r}: the name cannot be carried by"
            " a generated condition reference (JOB_NAME metacharacters, DL-39)"
        )
    return name


def _resource_group(name: str, quantity: int, free: str | None) -> str:
    """One `resources:` group in JIL surface syntax (DL-18 opaque records)."""
    return f"({name}, QUANTITY={quantity}" + (f", FREE={free}" if free else "") + ")"


class _BoxScope:
    def __init__(self, builder: CatalogBuilder, name: str) -> None:
        self._builder = builder
        self._name = name

    def __enter__(self) -> CatalogBuilder:
        self._builder._box_stack.append(self._name)
        return self._builder

    def __exit__(self, *exc_info: object) -> None:
        self._builder._box_stack.pop()


class CatalogBuilder:
    """Builds JIL text through the four corpus-extracted combinators plus
    record declarations; build() runs it through the real pipeline."""

    def __init__(self) -> None:
        self._statements: list[str] = []
        self._box_stack: list[str] = []
        self._declared: dict[str, bool] = {}  # job -> has explicit condition
        self._resourced: set[str] = set()  # jobs carrying a resources: attribute
        self._stmt_index: dict[str, int] = {}  # job -> index into _statements

    # ------------------------------------------------------------------ records

    def global_(self, name: str, value: str, /) -> CatalogBuilder:
        _check_name("global", name)
        _check_value("global", value)
        self._statements.append(f"insert_global: {name}\nvalue: {value}\n")
        return self

    def machine(
        self,
        name: str,
        members: list[tuple[str, dict[str, str]]] | None = None,
        /,
        *,
        type: str | None = None,
        **attrs: str,
    ) -> CatalogBuilder:
        # `members` is positional-only so an opaque machine attr literally named
        # `members` routes to **attrs instead of colliding (DL-49, review).
        _check_name("machine", name)
        lines = [f"insert_machine: {name}"]
        if type is not None:
            lines.append(f"type: {_check_value('machine type', type)}")
        for key, value in attrs.items():
            if not _KEY_RE.match(key):
                raise DslError(f"machine attribute key {key!r} is not JIL-key-shaped")
            lines.append(f"{key}: {_check_value('machine attribute', value)}")
        # Virtual pool / real pool members: each `machine:` line plus its own
        # factor/max_load, in order (DL-49). Emitted after machine-level attrs
        # so re-lowering re-attaches per-member attrs to their member.
        for member_name, member_attrs in members or []:
            lines.append(f"machine: {_check_value('machine member', member_name)}")
            for key, value in member_attrs.items():
                if not _KEY_RE.match(key):
                    raise DslError(f"machine member attribute key {key!r} is not JIL-key-shaped")
                lines.append(f"{key}: {_check_value('machine member attribute', value)}")
        self._statements.append("\n".join(lines) + "\n")
        return self

    def resource(
        self, name: str, /, *, res_type: str | None = None, **attrs: str
    ) -> CatalogBuilder:
        _check_name("resource", name)
        lines = [f"insert_resource: {name}"]
        if res_type is not None:
            lines.append(f"res_type: {_check_value('res_type', res_type)}")
        for key, value in attrs.items():
            if not _KEY_RE.match(key):
                raise DslError(f"resource attribute key {key!r} is not JIL-key-shaped")
            lines.append(f"{key}: {_check_value('resource attribute', value)}")
        self._statements.append("\n".join(lines) + "\n")
        return self

    def xinst(self, name: str, /, *, xtype: str, **attrs: str) -> CatalogBuilder:
        _check_name("external instance", name)
        lines = [f"insert_xinst: {name}", f"xtype: {_check_value('xtype', xtype)}"]
        for key, value in attrs.items():
            if not _KEY_RE.match(key):
                raise DslError(f"xinst attribute key {key!r} is not JIL-key-shaped")
            lines.append(f"{key}: {_check_value('xinst attribute', value)}")
        self._statements.append("\n".join(lines) + "\n")
        return self

    def calendar(self, name: str, /, *, dates: Sequence[str] = (), **attrs: str) -> CatalogBuilder:
        """Standard-calendar record (autocal_asc export shape, DL-36):
        attributes first, then bare date rows."""
        lines = self._calendar_lines("calendar", name, attrs)
        for row in dates:
            row = _check_value("calendar date row", row).strip()
            if not row or _ATTR_LINE_RE.match(row):
                raise DslError(f"calendar date row {row!r} would re-parse as an attribute line")
            lines.append(row)
        self._statements.append("\n".join(lines) + "\n")
        return self

    def cycle(self, name: str, /, **attrs: str) -> CatalogBuilder:
        """Cycle record (autocal_asc export shape, DL-36)."""
        self._statements.append("\n".join(self._calendar_lines("cycle", name, attrs)) + "\n")
        return self

    def extended_calendar(self, name: str, /, **attrs: str) -> CatalogBuilder:
        """Extended-calendar record (autocal_asc export shape, DL-36)."""
        lines = self._calendar_lines("extended_calendar", name, attrs)
        self._statements.append("\n".join(lines) + "\n")
        return self

    def _calendar_lines(self, verb: str, name: str, attrs: dict[str, str]) -> list[str]:
        # Calendar names may carry spaces (TechDocs' own example is
        # "shopping days") -- quoted on emission, unquoted at lowering.
        # Tabs, quotes, and outer whitespace would not survive that
        # quote--unquote round trip and are refused.
        _check_value(f"{verb} name", name)
        if not name or name != name.strip() or "\t" in name or '"' in name:
            raise DslError(f"{verb} name {name!r} is not calendar-name-shaped")
        subject = f'"{name}"' if " " in name else name
        lines = [f"{verb}: {subject}"]
        for key, value in attrs.items():
            if not _KEY_RE.match(key):
                raise DslError(f"{verb} attribute key {key!r} is not JIL-key-shaped")
            lines.append(f"{key}: {_check_value(f'{verb} attribute', value)}")
        return lines

    # ----------------------------------------------------------------- the four

    def job(
        self,
        name: str,
        *,
        job_type: str = "c",
        condition: str | None = None,
        **attrs: object,
    ) -> CatalogBuilder:
        """One insert_job statement. Keyword names are JIL attribute names;
        list values join with ', '; bools become 1/0; annotations= and
        passthrough= dicts inline verbatim attribute lines."""
        _check_name("job", name)
        if name in self._declared:
            raise DslError(f"job {name!r} declared twice")
        # DL-39: builder API names are SEMANTIC; `:` is escaped at JIL
        # generation and unescaped again at lowering (vendor rule 4b spelling).
        lines = [f"insert_job: {escape_job_name(name)}", f"job_type: {job_type}"]
        if self._box_stack:
            lines.append(f"box_name: {escape_job_name(self._box_stack[-1])}")
        for key, value in attrs.items():
            if key in ("annotations", "passthrough"):
                if not isinstance(value, dict):
                    raise DslError(f"{key}= expects a dict of verbatim attributes")
                for attr_key, attr_value in value.items():
                    if not _KEY_RE.match(attr_key):
                        raise DslError(f"attribute key {attr_key!r} is not JIL-key-shaped")
                    if attr_key in ("condition", "resources"):
                        # a verbatim line would bypass the _declared/_resourced
                        # registries the wiring builders' no-merge guards read
                        raise DslError(
                            f"{key}= cannot smuggle {attr_key!r};"
                            f" use the {attr_key}= kwarg"
                        )
                    lines.append(f"{attr_key}: {_check_value(attr_key, str(attr_value))}")
                continue
            if not _KEY_RE.match(key):
                raise DslError(f"attribute key {key!r} is not JIL-key-shaped")
            lines.append(f"{key}: {self._render_value(key, value)}")
        if condition is not None:
            lines.append(f"condition: {_check_value('condition', condition)}")
        self._stmt_index[name] = len(self._statements)
        self._statements.append("\n".join(lines) + "\n")
        self._declared[name] = condition is not None
        if "resources" in attrs:
            self._resourced.add(name)
        return self

    def box(
        self,
        name: str,
        *,
        condition: str | None = None,
        **attrs: object,
    ) -> _BoxScope:
        """A box job; use as a context manager -- jobs declared inside become
        members (nesting supported)."""
        self.job(name, job_type="b", condition=condition, **attrs)
        return _BoxScope(self, name)

    def sequence(self, *names: str, link: str = "s") -> CatalogBuilder:
        """Wire already-declared jobs into a status-chain: each follower gets
        condition link(previous) -- s() by default, f()/d()/t() for
        escalation / always-advance / kill-cleanup chains (T-004). Followers
        must have been declared without a condition -- merging would be
        silent loss (DL-17)."""
        if len(names) < 2:
            raise DslError("sequence() needs at least two job names")
        if link not in ("s", "f", "d", "t"):
            raise DslError(f"sequence() link {link!r} is not one of s/f/d/t")
        for name in names:
            if name not in self._declared:
                raise DslError(f"sequence() references undeclared job {name!r}")
        for name in names[:-1]:  # every prev is interpolated into a condition
            _check_wirable("sequence", name)
        for prev, follower in zip(names, names[1:]):
            if self._declared[follower]:
                raise DslError(
                    f"sequence() follower {follower!r} already has a condition;"
                    " refusing to merge (silent loss)"
                )
            self._amend_condition(follower, f"{link}({escape_job_name(prev)})")
            self._declared[follower] = True
        return self

    def parallel(
        self,
        names: list[str],
        *,
        after: str | None = None,
        on: str = "s",
        then: str | None = None,
        then_any: str | None = None,
    ) -> CatalogBuilder:
        """Fan already-declared jobs out from a common producer (each member
        gets on(after) -- s() by default, f()/d()/t() per T-004) and
        optionally back in: `then` gets the conjunction of member successes,
        `then_any` the disjunction (T-003). Same no-merge rule as
        sequence()."""
        if len(names) < 2:
            raise DslError("parallel() needs at least two member names")
        if on not in ("s", "f", "d", "t"):
            raise DslError(f"parallel() on {on!r} is not one of s/f/d/t")
        joins = [j for j in (then, then_any) if j is not None]
        for name in [*names, *([after] if after is not None else []), *joins]:
            if name not in self._declared:
                raise DslError(f"parallel() references undeclared job {name!r}")
        if after is not None:
            _check_wirable("parallel", after)
        if joins:  # member names are interpolated into the join conditions
            for name in names:
                _check_wirable("parallel", name)
        if after is not None:
            for member in names:
                if self._declared[member]:
                    raise DslError(
                        f"parallel() member {member!r} already has a condition;"
                        " refusing to merge (silent loss)"
                    )
                self._amend_condition(member, f"{on}({escape_job_name(after)})")
                self._declared[member] = True
        for join, joiner in ((then, " & "), (then_any, " | ")):
            if join is None:
                continue
            if self._declared[join]:
                raise DslError(
                    f"parallel() join {join!r} already has a condition;"
                    " refusing to merge (silent loss)"
                )
            self._amend_condition(join, joiner.join(f"s({escape_job_name(m)})" for m in names))
            self._declared[join] = True
        return self

    def mutex(self, *names: str) -> CatalogBuilder:
        """Pairwise mutual exclusion (T-005): every pair gets each other's
        bare n() CONJOINED onto whatever condition each job already carries.
        Unlike sequence()/parallel() this composes with existing conditions
        by design -- conjoining is the declared operation, not a silent
        merge. The existing condition is parenthesized so both grammar modes
        (Q1) preserve the tree; canonical form sorts conjuncts, so conjoin
        order cannot affect equivalence. Call AFTER sequence()/parallel()
        wiring: mutex() marks its jobs conditioned, and the chain builders
        refuse conditioned followers."""
        if len(names) < 2:
            raise DslError("mutex() needs at least two job names")
        if len(set(names)) != len(names):
            raise DslError("mutex() names must be distinct")
        for name in names:
            if name not in self._declared:
                raise DslError(f"mutex() references undeclared job {name!r}")
            _check_wirable("mutex", name)  # every name lands in a partner's n()
        for i, a in enumerate(names):
            for b in names[i + 1 :]:
                self._conjoin_condition(a, f"n({escape_job_name(b)})")
                self._conjoin_condition(b, f"n({escape_job_name(a)})")
        for name in names:
            self._declared[name] = True
        return self

    def contend(
        self,
        jobs: Sequence[str],
        *,
        resource: str,
        quantity: int = 1,
        free: str | None = None,
    ) -> CatalogBuilder:
        """One shared `resources:` requirement declared across several jobs
        (T-006): the scattered per-job attribute made visible as contention.
        Refuses jobs that already carry a resources attribute (no-merge)."""
        if len(jobs) < 2:
            raise DslError("contend() needs at least two job names")
        _check_name("resource", resource)
        if any(ch in resource for ch in "(),"):
            raise DslError(f"resource name {resource!r} is not resource-name-shaped")
        if quantity < 1:
            raise DslError("contend() quantity must be >= 1")
        if free not in (None, "Y", "N", "A"):
            raise DslError(f"contend() free {free!r} is not one of Y/N/A")
        for name in jobs:
            if name not in self._declared:
                raise DslError(f"contend() references undeclared job {name!r}")
            if name in self._resourced:
                raise DslError(
                    f"contend() job {name!r} already carries resources;"
                    " refusing to merge (silent loss)"
                )
        group = _resource_group(resource, quantity, free)
        for name in jobs:
            self._append_attr_line(name, f"resources: {group}")
            self._resourced.add(name)
        return self

    # ----------------------------------------------------------------- plumbing

    def _amend_condition(self, job: str, condition: str) -> None:
        self._append_attr_line(job, f"condition: {condition}")

    def _conjoin_condition(self, job: str, atom: str) -> None:
        index = self._stmt_index[job]
        # split on \n ONLY: the statement lane may carry \x0b/\x0c/\x85/U+2028
        # inside values (the scanner delimits on \n alone), and splitlines()
        # would rewrite them into real newlines -- silent statement corruption
        lines = self._statements[index][:-1].split("\n")
        for j, line in enumerate(lines):
            if line.startswith("condition: "):
                existing = line[len("condition: ") :]
                lines[j] = f"condition: ({existing}) & {atom}"
                self._statements[index] = "\n".join(lines) + "\n"
                return
        self._statements[index] += f"condition: {atom}\n"

    def _append_attr_line(self, job: str, line: str) -> None:
        self._statements[self._stmt_index[job]] += line + "\n"

    def _render_value(self, key: str, value: object) -> str:
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, (list, tuple)):
            return ", ".join(str(item) for item in value)
        return _check_value(key, str(value))

    def to_jil(self) -> str:
        return "\n".join(self._statements)

    def build(self, *, permit_unknown: bool = False) -> CatalogIR:
        """Parse + lower the generated JIL through the real pipeline."""
        return lower_source(self.to_jil(), file="<dsl>", permit_unknown=permit_unknown)


# ---------------------------------------------------------------------- decompiler


def _py(value: str) -> str:
    return repr(value)


def _schedule_kwargs(schedule: ScheduleBlock) -> list[str]:
    out: list[str] = ["date_conditions=True"]
    if schedule.days_of_week is not None:
        out.append(f"days_of_week={_py(', '.join(schedule.days_of_week))}")
    if schedule.run_calendar is not None:
        out.append(f"run_calendar={_py(schedule.run_calendar)}")
    if schedule.exclude_calendar is not None:
        out.append(f"exclude_calendar={_py(schedule.exclude_calendar)}")
    if schedule.timezone is not None:
        out.append(f"timezone={_py(schedule.timezone)}")
    if schedule.start_times is not None:
        rendered = ", ".join(_time(t) for t in schedule.start_times)
        out.append(f"start_times='\"{rendered}\"'")
    if schedule.start_mins is not None:
        out.append(f"start_mins={_py(', '.join(str(m) for m in schedule.start_mins))}")
    if schedule.run_window is not None:
        lo, hi = schedule.run_window
        out.append(f"run_window='\"{_time(lo)}-{_time(hi)}\"'")
    if schedule.must_start is not None:
        out.append(f"must_start_times={_py(_sla(schedule.must_start))}")
    if schedule.must_complete is not None:
        out.append(f"must_complete_times={_py(_sla(schedule.must_complete))}")
    return out


def _time(t: Time) -> str:
    return f"{t.hour:02d}:{t.minute:02d}"


def _sla(spec: SlaSpec) -> str:
    if spec.kind == "relative":
        assert spec.offsets_min is not None
        return ", ".join(f"+{offset}" for offset in spec.offsets_min)
    assert spec.times is not None
    return ", ".join(_time(t) for t in spec.times)


def _code_ranges(ranges: list[tuple[int, int]]) -> str:
    """SEM-09/DL-33 exit-code sets back to their surface form; lowering keeps
    the author's partition sorted-not-merged, so render(parse(x)) is stable."""
    return ", ".join(str(lo) if lo == hi else f"{lo}-{hi}" for lo, hi in ranges)


def _record_kwargs(attrs: dict[str, str]) -> list[str]:
    """Opaque record attrs as builder kwargs. Keys are JIL-key-shaped
    (scanner rule 1) but may collide with Python keywords or the builders'
    positional-only `name`; those route through a **{} splat so the emitted
    module always compiles."""
    plain = [f"{k}={_py(v)}" for k, v in attrs.items() if not _needs_splat(k)]
    splat = {k: v for k, v in attrs.items() if _needs_splat(k)}
    if splat:
        plain.append("**{" + ", ".join(f"{_py(k)}: {_py(v)}" for k, v in splat.items()) + "}")
    return plain


def _needs_splat(key: str) -> bool:
    return keyword.iskeyword(key) or key == "name"


def _schedule_var_name(schedule: ScheduleBlock, used: set[str]) -> str:
    """Deterministic, content-derived name for a shared schedule (T-007):
    readable where the block has times/calendar, numeric suffix on
    collision (first-seen order, so regeneration is stable)."""
    parts: list[str] = []
    if schedule.start_times:
        parts.append("_".join(f"{t.hour:02d}{t.minute:02d}" for t in schedule.start_times))
    elif schedule.start_mins:
        parts.append("MINS_" + "_".join(str(m) for m in schedule.start_mins))
    if schedule.run_calendar:
        parts.append(schedule.run_calendar)
    elif schedule.days_of_week:
        parts.append("_".join(schedule.days_of_week))
    stem = re.sub(r"[^A-Za-z0-9]+", "_", "_".join(parts)).strip("_").upper() or "BLOCK"
    stem = f"SCHED_{stem}"[:48].rstrip("_")
    name, counter = stem, 1
    while name in used:
        counter += 1
        name = f"{stem}_{counter}"
    used.add(name)
    return name


def _job_kwargs(
    job: JobIR,
    *,
    condition: Cond | None,
    sched_var: str | None = None,
    fold_resources: bool = False,
) -> list[str]:
    out: list[str] = []
    exec_ = job.exec_
    if exec_ is not None:
        # std_in_file/envvars are the DL-32 CMD-only pair; absent on FwSpec.
        for field in ("command", "watch_file", "std_in_file", "envvars"):
            value = getattr(exec_, field, None)
            if value is not None:
                out.append(f"{field}={_py(value)}")
        for field in ("watch_interval", "watch_file_min_size"):
            value = getattr(exec_, field, None)
            if value is not None:
                out.append(f"{field}={value}")
        for field in ("machine", "owner", "profile", "std_out_file", "std_err_file"):
            value = getattr(exec_, field)
            if value is not None:
                out.append(f"{field}={_py(value)}")
    if job.schedule is not None:
        if sched_var is not None:
            out.append(f"**{sched_var}")
        else:
            out.extend(_schedule_kwargs(job.schedule))
    sem = job.sem
    if condition is not None:
        out.append(f"condition={_py(cond_to_source(condition))}")
    if sem.box_success is not None:
        out.append(f"box_success={_py(cond_to_source(sem.box_success))}")
    if sem.box_failure is not None:
        out.append(f"box_failure={_py(cond_to_source(sem.box_failure))}")
    if sem.max_exit_success:
        out.append(f"max_exit_success={sem.max_exit_success}")
    if sem.success_codes is not None:
        out.append(f"success_codes={_py(_code_ranges(sem.success_codes))}")
    if sem.fail_codes is not None:
        out.append(f"fail_codes={_py(_code_ranges(sem.fail_codes))}")
    if sem.term_run_time_min is not None:
        out.append(f"term_run_time={sem.term_run_time_min}")
    if sem.n_retrys:
        out.append(f"n_retrys={sem.n_retrys}")
    if sem.auto_hold:
        out.append("auto_hold=True")
    if sem.initial_status is not None:
        out.append(f"status={_py(sem.initial_status)}")
    if job.box.box_terminator:
        out.append("box_terminator=True")
    if job.box.job_terminator:
        out.append("job_terminator=True")
    if job.resources and not fold_resources:
        groups = " AND ".join(
            _resource_group(r.name, r.quantity, r.free) for r in job.resources
        )
        out.append(f"resources={_py(groups)}")
    if job.annotations:
        rendered = ", ".join(f"{_py(k)}: {_py(v)}" for k, v in job.annotations.items())
        out.append(f"annotations={{{rendered}}}")
    if job.passthrough:
        rendered = ", ".join(f"{_py(k)}: {_py(v)}" for k, v in job.passthrough.items())
        out.append(f"passthrough={{{rendered}}}")
    return out


def _plain_status_atom(cond: Cond | None) -> tuple[str, str] | None:
    """(producer, letter) iff cond is exactly ONE plain foldable status atom
    (no lookback, no instance, no conjuncts)."""
    if (
        isinstance(cond, StatusAtom)
        and cond.lookback is None
        and cond.job.instance is None
        and cond.status in _FOLDABLE_STATUS
    ):
        return cond.job.name, _FOLDABLE_STATUS[cond.status]
    return None


def _link_verdict(cond: Cond | None, prev: str) -> tuple[str | None, str]:
    """(letter, '') when a chain link folds; (None, reason) otherwise. The
    reasons feed the decompile fold report -- the estate's explicit-link
    worklist."""
    if cond is None:
        return None, "no condition"
    if isinstance(cond, (And, Or, Paren)):
        return None, "compound condition"
    if isinstance(cond, ExitCodeAtom):
        return None, "exit-code atom"
    if isinstance(cond, StatusAtom):
        if cond.job.instance is not None:
            return None, "cross-instance reference (M33)"
        if cond.job.name != prev:
            return None, f"references {cond.job.name!r}, not the predecessor"
        if cond.lookback is not None:
            return None, "lookback-qualified (Q2)"
        letter = _FOLDABLE_STATUS.get(cond.status)
        if letter is None:
            return None, f"{cond.status.lower()} atom"
        return letter, ""
    return None, "global-value atom"


def _success_combo_shape(cond: Cond | None) -> tuple[frozenset[str], bool] | None:
    """(member names, is_and) iff cond is exactly And/Or over plain distinct
    successes (no lookback, no instance, no nesting, no duplicate atoms).
    And = the DL-37 fan-in; Or = the T-003 any-of join; anything looser
    stays an explicit job(condition=...). One O(N) pass classifies every
    candidate join; fan-out groups then look their member set up directly
    (was: a full catalog scan per group)."""
    if not isinstance(cond, (And, Or)):
        return None
    names: set[str] = set()
    for op in cond.operands:
        if not (
            isinstance(op, StatusAtom)
            and op.status == "SUCCESS"
            and op.lookback is None
            and op.job.instance is None
        ):
            return None
        names.add(op.job.name)
    if len(names) != len(cond.operands):  # duplicate atoms never fold
        return None
    return frozenset(names), isinstance(cond, And)


def _is_bare_local_n(op: Cond, targets: set[str]) -> bool:
    return (
        isinstance(op, StatusAtom)
        and op.status == "NOTRUNNING"
        and op.lookback is None
        and op.job.instance is None
        and op.job.name in targets
    )


def _top_level_bare_n_targets(cond: Cond | None, self_name: str, jobs: set[str]) -> set[str]:
    """In-catalog jobs referenced by bare n() atoms sitting as top-level
    conjuncts (or as the whole condition). STRICTER than derive's M07 pass
    (which counts any bare n() occurrence): an n() nested under an Or or a
    group cannot be reconstructed by mutex()'s conjoin, so it never folds.
    `jobs` is the prebuilt catalog name set -- callers pass it once, this
    runs per job."""
    if cond is None:
        return set()
    ops = cond.operands if isinstance(cond, And) else [cond]
    out: set[str] = set()
    for op in ops:
        if _is_bare_local_n(op, jobs):
            assert isinstance(op, StatusAtom)
            if op.job.name != self_name:
                out.add(op.job.name)
    return out


def _without_mutex_atoms(cond: Cond, partners: set[str]) -> Cond | None:
    """The residual condition after removing the folded pairs' bare n()
    conjuncts; None when nothing else remains. Only ever called with
    partners that _top_level_bare_n_targets confirmed, so removal is exact."""
    if isinstance(cond, And):
        kept = [op for op in cond.operands if not _is_bare_local_n(op, partners)]
        if len(kept) == len(cond.operands):
            return cond
        if not kept:
            return None
        if len(kept) == 1:
            return kept[0]
        return And(operands=kept, span=None)
    return None if _is_bare_local_n(cond, partners) else cond


# Analysis passes (CLAUDE.md style: small pure functions), one per fold lane;
# decompile() orchestrates them over the shared residual-condition map.


def _buildable_job_name(name: str) -> bool:
    """True iff CatalogBuilder.job() accepts `name` -- the decompile-side
    gate mirroring T-006's resource-name gate: the lowerer accepts subjects
    (embedded whitespace) the builder's statement discipline refuses, and
    emitting them would produce a module that can never rebuild the
    catalog."""
    try:
        _check_name("job", name)
    except DslError:
        return False
    return True


def _fold_mutex(
    catalog: CatalogIR, residual: dict[str, Cond | None], disabled: set[str]
) -> tuple[list[tuple[str, str]], dict[str, Cond | None]]:
    """T-005: symmetric top-level bare n() pairs -> mutex() (DL-38 decision
    4: stricter than derive's M07). Returns the pairs and a residual map
    with the folded atoms stripped, so the downstream folds compose."""
    if "T-005" in disabled:
        return [], residual
    job_names = set(catalog.jobs)
    n_targets = {
        name: _top_level_bare_n_targets(residual[name], name, job_names)
        for name in catalog.jobs
    }
    pairs = sorted(
        {
            (a, b) if a < b else (b, a)
            for a, targets in n_targets.items()
            for b in targets
            if a in n_targets[b]  # symmetric only; one-way n() stays explicit
        }
    )
    partners: dict[str, set[str]] = {}
    for a, b in pairs:
        partners.setdefault(a, set()).add(b)
        partners.setdefault(b, set()).add(a)
    residual = dict(residual)
    for name, parts in partners.items():
        cond = residual[name]
        assert cond is not None  # a folded pair implies an n() conjunct
        residual[name] = _without_mutex_atoms(cond, parts)
    return pairs, residual


def _fold_chains(
    graph: DerivedGraph, residual: dict[str, Cond | None], disabled: set[str]
) -> tuple[list[tuple[list[str], str]], set[str], int, set[str], list[str]]:
    """sequence() candidates: derived chains whose links each carry EXACTLY
    one plain status atom on the predecessor -- adjacency alone is not
    enough (module docstring). T-001 admits s-links, T-004 f/d/t-links;
    T-002 folds maximal same-letter runs inside chains that mix or break.
    Returns (sequences, folded followers, split-chain count, jobs whose
    stays-explicit note is already written, notes)."""
    notes: list[str] = []
    noted: set[str] = set()
    sequences: list[tuple[list[str], str]] = []
    sequenced: set[str] = set()
    split_chains = 0

    def letter_active(letter: str) -> bool:
        return ("T-001" if letter == "s" else "T-004") not in disabled

    for chain in graph.chains:
        links = list(zip(chain, chain[1:]))
        verdicts = [_link_verdict(residual[follower], prev) for prev, follower in links]
        for (letter, reason), (prev, follower) in zip(verdicts, links):
            if letter is None:
                cond = residual[follower]
                rendered = f" ({cond_to_source(cond)})" if cond is not None else ""
                notes.append(f"explicit: link {prev!r}->{follower!r} -- {reason}{rendered}")
                noted.add(follower)
        runs: list[tuple[int, int, str]] = []  # [start, end) over links
        i = 0
        while i < len(verdicts):
            letter = verdicts[i][0]
            if letter is None or not letter_active(letter):
                i += 1
                continue
            j = i + 1
            while j < len(verdicts) and verdicts[j][0] == letter:
                j += 1
            runs.append((i, j, letter))
            i = j
        whole = len(runs) == 1 and runs[0][:2] == (0, len(verdicts))
        if not whole:
            if "T-002" in disabled:
                if runs:
                    notes.append(
                        f"explicit: chain {chain[0]!r}..{chain[-1]!r}"
                        " left whole (T-002 disabled)"
                    )
                    noted.update(chain[1:])
                continue
            if runs:
                split_chains += 1
        for start, end, letter in runs:
            names = chain[start : end + 1]
            sequences.append((names, letter))
            sequenced.update(names[1:])  # run heads keep their own condition
    return sequences, sequenced, split_chains, noted, notes


def _fold_fanout(
    catalog: CatalogIR, residual: dict[str, Cond | None], disabled: set[str]
) -> tuple[list[tuple[list[str], str, str, str | None, str | None]], set[str]]:
    """parallel() candidates (DL-37): fan-out groups -- >= 2 jobs whose
    entire (residual) condition is exactly letter(p) for one in-catalog
    producer p -- plus the unique fan-in joins. Grouping is by exact
    condition shape, not derive's (preds, succs) signatures: a member with
    extra OUTGOING edges still fans out from p, while any looser incoming
    shape must stay explicit. Disjointness with sequence() is structural:
    a fan-out member gives p >= 2 successors, so no chain link through p
    exists. Fan-in joins (then= and, via T-003, then_any=) refine the
    T-001 parallel machinery, so disabling T-001 keeps EVERY join explicit
    even on T-004 f/d/t groups (the FOLDS dependency contract)."""
    fanout: dict[tuple[str, str], list[str]] = {}
    for name in catalog.jobs:
        plain = _plain_status_atom(residual[name])
        if plain is None:
            continue
        producer, letter = plain
        if producer not in catalog.jobs or producer == name:
            continue
        if ("T-001" if letter == "s" else "T-004") in disabled:
            continue
        fanout.setdefault((producer, letter), []).append(name)
    joins_by_shape: dict[tuple[frozenset[str], bool], list[str]] = {}
    if "T-001" not in disabled:
        for name in catalog.jobs:
            shape = _success_combo_shape(residual[name])
            if shape is not None:
                joins_by_shape.setdefault(shape, []).append(name)
    parallels: list[tuple[list[str], str, str, str | None, str | None]] = []
    paralleled: set[str] = set()
    for (producer, letter), members in fanout.items():
        if len(members) < 2:
            continue
        member_key = frozenset(members)
        then: str | None = None
        then_any: str | None = None
        if "T-001" not in disabled:
            and_joins = joins_by_shape.get((member_key, True), [])
            then = and_joins[0] if len(and_joins) == 1 else None
            if "T-003" not in disabled:
                or_joins = joins_by_shape.get((member_key, False), [])
                then_any = or_joins[0] if len(or_joins) == 1 else None
        parallels.append((members, producer, letter, then, then_any))
        paralleled.update(members)
        for join in (then, then_any):
            if join is not None:
                paralleled.add(join)
    return parallels, paralleled


def _fold_schedules(
    catalog: CatalogIR, disabled: set[str]
) -> tuple[dict[str, str], list[tuple[str, tuple[str, ...]]]]:
    """T-007: identical schedule blocks factored into shared module-level
    dicts -- grouping is by EMISSION equality (the rendered kwargs), the
    exact thing being deduplicated. Pure Python factoring, no new surface.
    Returns (job -> variable name, [(variable, kwargs)] in first-seen
    order)."""
    if "T-007" in disabled:
        return {}, []
    sched_vars: dict[str, str] = {}
    sched_defs: list[tuple[str, tuple[str, ...]]] = []
    sched_groups: dict[tuple[str, ...], list[str]] = {}
    for name, job in catalog.jobs.items():
        if job.schedule is not None:
            sched_groups.setdefault(tuple(_schedule_kwargs(job.schedule)), []).append(name)
    used_vars: set[str] = set()
    for sched_key, group_jobs in sched_groups.items():
        if len(group_jobs) < 2:
            continue
        schedule = catalog.jobs[group_jobs[0]].schedule
        assert schedule is not None
        var = _schedule_var_name(schedule, used_vars)
        sched_defs.append((var, sched_key))
        for name in group_jobs:
            sched_vars[name] = var
    return sched_vars, sched_defs


def _fold_contends(
    catalog: CatalogIR, disabled: set[str]
) -> tuple[list[tuple[list[str], str, int, str | None]], set[str], list[str]]:
    """T-006: identical single-group resources: attrs across >= 2 jobs fold
    into one contend() declaration. Whole-lane equality only (name,
    quantity, free); multi-group jobs stay explicit -- reconstructing a
    partial list would have to reproduce group order, which is exactly the
    merge ambiguity contend() refuses. Returns (contends, folded jobs,
    notes)."""
    if "T-006" in disabled:
        return [], set(), []
    contends: list[tuple[list[str], str, int, str | None]] = []
    resource_folded: set[str] = set()
    notes: list[str] = []
    res_groups: dict[tuple[str, int, str | None], list[str]] = {}
    for name, job in catalog.jobs.items():
        if len(job.resources) == 1:
            ref = job.resources[0]
            res_groups.setdefault((ref.name, ref.quantity, ref.free), []).append(name)
    for (res_name, quantity, free), group_jobs in res_groups.items():
        if len(group_jobs) < 2:
            continue
        if " " in res_name or "\t" in res_name or any(ch in res_name for ch in "(),"):
            notes.append(
                f"explicit: resource {res_name!r} shared by {len(group_jobs)} jobs"
                " -- name is not resource-name-shaped for contend()"
            )
            continue
        contends.append((group_jobs, res_name, quantity, free))
        resource_folded.update(group_jobs)
    return contends, resource_folded, notes


def _explicit_reason(cond: Cond, jobs: Collection[str]) -> str:
    """Reason class for a residual condition no fold claimed, in DL-38's
    vocabulary (lookback/Q2, cross-instance/M33, exit-code, compound)."""
    if isinstance(cond, (And, Or, Paren)):
        return "compound condition"
    if isinstance(cond, ExitCodeAtom):
        return "exit-code atom"
    if isinstance(cond, StatusAtom):
        if cond.job.instance is not None:
            return "cross-instance reference (M33)"
        if cond.lookback is not None:
            return "lookback-qualified (Q2)"
        if cond.job.name not in jobs:
            return "references an undefined job"
        if cond.status == "NOTRUNNING":
            return "one-way or self n()"
        return "no qualifying chain or fan-out group"
    return "global-value atom"


def _explicit_notes(
    catalog: CatalogIR,
    residual: dict[str, Cond | None],
    suppressed: set[str],
    noted: set[str],
) -> list[str]:
    """DL-38 audit-trail completion: chain-link verdicts only cover links
    INSIDE derived chains, but disqualified links also hang off fan-out and
    fan-in nodes (singleton groups, ambiguous joins, lookback members,
    disabled lanes, chain heads). Every job whose residual condition stays
    explicit and has no note yet gets one here -- the explicit-links
    worklist is the migration audit trail, so it must be complete."""
    notes: list[str] = []
    for name in catalog.jobs:
        cond = residual[name]
        if cond is None or name in suppressed or name in noted:
            continue
        notes.append(
            f"explicit: job {name!r} -- {_explicit_reason(cond, catalog.jobs)}"
            f" ({cond_to_source(cond)})"
        )
    return notes


def decompile(
    catalog: CatalogIR,
    graph: DerivedGraph | None = None,
    *,
    disable: Collection[str] = (),
    report: list[str] | None = None,
) -> str:
    """Emit a runnable Python module of builder calls; executing it rebuilds
    a catalog canonically equal to this one (the round-trip property).

    `disable` opts out of individual folds by code (FOLDS registry, DL-38);
    a bare string means that ONE code; unknown codes are refused loudly.
    `report`, when given, collects the fold inventory and the per-link
    explicit-stays diagnostics."""
    if isinstance(disable, str):  # satisfies Collection[str] but iterates chars
        disable = (disable,)
    disabled = {code.strip().upper() for code in disable if code.strip()}
    unknown = disabled - FOLDS.keys()
    if unknown:
        raise DslError(
            f"unknown fold code(s): {', '.join(sorted(unknown))}"
            f" (known: {', '.join(FOLDS)})"
        )
    unbuildable = [name for name in catalog.jobs if not _buildable_job_name(name)]
    if unbuildable:
        preview = ", ".join(repr(n) for n in unbuildable[:5])
        if len(unbuildable) > 5:
            preview += ", ..."
        raise DslError(
            f"{len(unbuildable)} job name(s) the builder cannot express"
            f" ({preview}); the emitted module could never rebuild this catalog"
        )
    if graph is None:
        graph = derive_graph(catalog)
    lines: list[str] = [
        "# Decompiled by dsl41 (phase 10); executing this module rebuilds the catalog.",
        "from dsl41.dsl import CatalogBuilder",
        "",
        "c = CatalogBuilder()",
    ]
    has_records = any(
        (
            catalog.globals_declared,
            catalog.machines,
            catalog.resources,
            catalog.external_instances,
            catalog.calendars,
            catalog.cycles,
        )
    )
    if has_records:
        lines += ["", "# --- records"]
    for name, value in catalog.globals_declared.items():
        lines.append(f"c.global_({_py(name)}, {_py(value)})")
    for name, machine in catalog.machines.items():
        kwargs = [f"type={_py(machine.machine_type)}"] if machine.machine_type is not None else []
        kwargs += _record_kwargs(machine.attrs)
        # Pool members (DL-49) pass as the positional-only 2nd arg -- so an
        # opaque attr named `members` (routed above as a kwarg) never collides.
        pos = ""
        if machine.members:
            member_lits = ", ".join(
                f"({_py(m.name)}, {{"
                + ", ".join(f"{_py(k)}: {_py(v)}" for k, v in m.attrs.items())
                + "})"
                for m in machine.members
            )
            pos = f", [{member_lits}]"
        tail = (", " + ", ".join(kwargs)) if kwargs else ""
        lines.append(f"c.machine({_py(name)}{pos}{tail})")
    for name, resource in catalog.resources.items():
        kwargs = [f"res_type={_py(resource.res_type)}"] if resource.res_type is not None else []
        kwargs += _record_kwargs(resource.attrs)
        lines.append(f"c.resource({_py(name)}{', ' if kwargs else ''}{', '.join(kwargs)})")
    for name, xinst in catalog.external_instances.items():
        kwargs = [f"xtype={_py(xinst.xtype)}"]
        kwargs += _record_kwargs(xinst.attrs)
        lines.append(f"c.xinst({_py(name)}, {', '.join(kwargs)})")
    for name, calendar in catalog.calendars.items():
        call = "calendar" if calendar.kind == "standard" else "extended_calendar"
        kwargs = []
        if calendar.dates:
            kwargs.append(f"dates=[{', '.join(_py(row) for row in calendar.dates)}]")
        if calendar.kind == "standard" and "dates" in calendar.attrs:
            # would bind the builder's dates= parameter; loud beats a
            # silently mangled module (no such attr exists in the vendor
            # export format)
            raise DslError(f"calendar {name!r} carries an attribute named 'dates'")
        kwargs += _record_kwargs(calendar.attrs)
        lines.append(f"c.{call}({_py(name)}{', ' if kwargs else ''}{', '.join(kwargs)})")
    for name, cycle in catalog.cycles.items():
        kwargs = _record_kwargs(cycle.attrs)
        lines.append(f"c.cycle({_py(name)}{', ' if kwargs else ''}{', '.join(kwargs)})")

    # T-005 first: fold detection runs on RESIDUAL conditions (mutex atoms
    # stripped), so folds compose -- the emitted module re-conjoins via
    # mutex() AFTER sequence()/parallel() wiring. Stripping can never
    # invalidate derive's chains: bare n() atoms are mutex-classified and
    # contribute no edges (M07).
    residual: dict[str, Cond | None] = {n: j.sem.condition for n, j in catalog.jobs.items()}
    mutex_pairs, residual = _fold_mutex(catalog, residual, disabled)
    sequences, sequenced, split_chains, noted, notes = _fold_chains(graph, residual, disabled)
    parallels, paralleled = _fold_fanout(catalog, residual, disabled)
    suppressed = sequenced | paralleled
    sched_vars, sched_defs = _fold_schedules(catalog, disabled)
    contends, resource_folded, contend_notes = _fold_contends(catalog, disabled)
    notes += contend_notes
    notes += _explicit_notes(catalog, residual, suppressed, noted)

    def emit_condition(name: str) -> Cond | None:
        return None if name in suppressed else residual[name]

    def kwargs_for(job: JobIR) -> list[str]:
        return _job_kwargs(
            job,
            condition=emit_condition(job.name),
            sched_var=sched_vars.get(job.name),
            fold_resources=job.name in resource_folded,
        )

    def emit_job(job: JobIR, indent: str, method: str = "job") -> None:
        kwargs = kwargs_for(job)
        prefix = f"{indent}{'b' if indent else 'c'}.{method}({_py(job.name)}"
        if job.job_type == "FW":
            kwargs.insert(0, "job_type='f'")
        lines.append(f"{prefix}{', ' if kwargs else ''}{', '.join(kwargs)})")

    emitted: set[str] = set()

    def emit_box(box_name: str, indent: str) -> None:
        job = catalog.jobs[box_name]
        kwargs = kwargs_for(job)
        lines.append(
            f"{indent}with c.box({_py(box_name)}{', ' if kwargs else ''}{', '.join(kwargs)}) as b:"
        )
        emitted.add(box_name)
        members = graph.box_tree.children.get(box_name, [])
        if not members:
            lines.append(f"{indent}    pass")
        for member in members:
            if member in graph.box_tree.children:  # nested box
                emit_box(member, indent + "    ")
            else:
                emit_job(catalog.jobs[member], indent + "    ")
                emitted.add(member)

    if sched_defs:
        lines += ["", "# --- shared schedules (T-007)"]
        for var, sched_key in sched_defs:
            lines.append(f"{var} = dict({', '.join(sched_key)})")
    if catalog.jobs:
        lines += ["", "# --- jobs"]
    for root in graph.box_tree.roots:
        emit_box(root, "")
    for name, job in catalog.jobs.items():
        if name not in emitted:
            emit_job(job, "")
            emitted.add(name)
    if sequences or parallels or mutex_pairs or contends:
        lines += ["", "# --- wiring (the suppressed conditions above live here)"]
    for names, letter in sequences:
        args = ", ".join(_py(name) for name in names)
        tail = f", link={_py(letter)}" if letter != "s" else ""
        lines.append(f"c.sequence({args}{tail})")
    for members, producer, letter, then, then_any in parallels:
        args = "[" + ", ".join(_py(m) for m in members) + "]"
        tail = f", on={_py(letter)}" if letter != "s" else ""
        tail += f", then={_py(then)}" if then is not None else ""
        tail += f", then_any={_py(then_any)}" if then_any is not None else ""
        lines.append(f"c.parallel({args}, after={_py(producer)}{tail})")
    for a, b in mutex_pairs:
        lines.append(f"c.mutex({_py(a)}, {_py(b)})")
    for group_jobs, res_name, quantity, free in contends:
        args = "[" + ", ".join(_py(n) for n in group_jobs) + "]"
        tail = f", free={_py(free)}" if free else ""
        lines.append(f"c.contend({args}, resource={_py(res_name)}, quantity={quantity}{tail})")

    if report is not None:
        s_seq = [s for s in sequences if s[1] == "s"]
        x_seq = [s for s in sequences if s[1] != "s"]
        s_par = [p for p in parallels if p[2] == "s"]
        x_par = [p for p in parallels if p[2] != "s"]
        any_joins = sum(1 for p in parallels if p[4] is not None)
        if s_seq or s_par:
            report.append(
                f"T-001: {len(s_seq)} sequence(s) ({sum(len(s[0]) for s in s_seq)} jobs),"
                f" {len(s_par)} parallel group(s)"
                f" ({sum(len(p[0]) for p in s_par)} members)"
            )
        if split_chains:
            report.append(f"T-002: {split_chains} chain(s) folded as sub-runs")
        if any_joins:
            report.append(f"T-003: {any_joins} any-of join(s)")
        if x_seq or x_par:
            report.append(
                f"T-004: {len(x_seq)} non-s sequence(s), {len(x_par)} non-s parallel group(s)"
            )
        if mutex_pairs:
            report.append(f"T-005: {len(mutex_pairs)} mutex pair(s)")
        if contends:
            report.append(
                f"T-006: {len(contends)} contention group(s)"
                f" ({sum(len(g[0]) for g in contends)} jobs)"
            )
        if sched_defs:
            report.append(f"T-007: {len(sched_defs)} shared schedule(s) ({len(sched_vars)} jobs)")
        report.extend(notes)
    lines += [
        "",
        "catalog = c.build()",
        "",
        'if __name__ == "__main__":',
        "    import sys",
        "",
        "    sys.stdout.write(c.to_jil())",
        "",
    ]
    return "\n".join(lines)
