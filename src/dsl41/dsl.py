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
  mutex_b chain shows adjacency alone is not enough); parallel() only where
  every member's condition is exactly s(common producer). Everything else
  stays an explicit job(condition=...) call. Decompile output is a runnable
  Python module: executing it rebuilds a catalog whose canonical form
  equals the original's (the round-trip property, tested corpus-wide).
- FW jobs round-trip through job(watch_file=...) kwargs -- mirroring the
  existing IR model is mechanical, not combinator design.
"""

from __future__ import annotations

import re

from dsl41.conditions import (
    And,
    Cond,
    ExitCodeAtom,
    Lookback,
    Or,
    Paren,
    StatusAtom,
)
from dsl41.derive import DerivedGraph, derive_graph
from dsl41.ir import CatalogIR, JobIR, ScheduleBlock, SlaSpec, Time, lower_source

_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_CTRL_RE = re.compile(r"[\r\n\x00]")

_STATUS_LETTER = {
    "SUCCESS": "s",
    "FAILURE": "f",
    "DONE": "d",
    "TERMINATED": "t",
    "NOTRUNNING": "n",
}

_BARE_GLOBAL_VALUE_RE = re.compile(r"[A-Za-z0-9_.\-]+\Z")


class DslError(ValueError):
    pass


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
    name = atom.job.name.replace(":", "\\:")
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

    # ------------------------------------------------------------------ records

    def global_(self, name: str, value: str) -> CatalogBuilder:
        _check_name("global", name)
        _check_value("global", value)
        self._statements.append(f"insert_global: {name}\nvalue: {value}\n")
        return self

    def machine(self, name: str, *, type: str | None = None, **attrs: str) -> CatalogBuilder:
        _check_name("machine", name)
        lines = [f"insert_machine: {name}"]
        if type is not None:
            lines.append(f"type: {_check_value('machine type', type)}")
        for key, value in attrs.items():
            if not _KEY_RE.match(key):
                raise DslError(f"machine attribute key {key!r} is not JIL-key-shaped")
            lines.append(f"{key}: {_check_value('machine attribute', value)}")
        self._statements.append("\n".join(lines) + "\n")
        return self

    def resource(self, name: str, *, res_type: str | None = None, **attrs: str) -> CatalogBuilder:
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

    def xinst(self, name: str, *, xtype: str, **attrs: str) -> CatalogBuilder:
        _check_name("external instance", name)
        lines = [f"insert_xinst: {name}", f"xtype: {_check_value('xtype', xtype)}"]
        for key, value in attrs.items():
            if not _KEY_RE.match(key):
                raise DslError(f"xinst attribute key {key!r} is not JIL-key-shaped")
            lines.append(f"{key}: {_check_value('xinst attribute', value)}")
        self._statements.append("\n".join(lines) + "\n")
        return self

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
        lines = [f"insert_job: {name}", f"job_type: {job_type}"]
        if self._box_stack:
            lines.append(f"box_name: {self._box_stack[-1]}")
        for key, value in attrs.items():
            if key in ("annotations", "passthrough"):
                if not isinstance(value, dict):
                    raise DslError(f"{key}= expects a dict of verbatim attributes")
                for attr_key, attr_value in value.items():
                    if not _KEY_RE.match(attr_key):
                        raise DslError(f"attribute key {attr_key!r} is not JIL-key-shaped")
                    lines.append(f"{attr_key}: {_check_value(attr_key, str(attr_value))}")
                continue
            if not _KEY_RE.match(key):
                raise DslError(f"attribute key {key!r} is not JIL-key-shaped")
            lines.append(f"{key}: {self._render_value(key, value)}")
        if condition is not None:
            lines.append(f"condition: {_check_value('condition', condition)}")
        self._statements.append("\n".join(lines) + "\n")
        self._declared[name] = condition is not None
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

    def sequence(self, *names: str) -> CatalogBuilder:
        """Wire already-declared jobs into an s()-chain: each follower gets
        condition s(previous). Followers must have been declared without a
        condition -- merging would be silent loss (DL-17)."""
        if len(names) < 2:
            raise DslError("sequence() needs at least two job names")
        for name in names:
            if name not in self._declared:
                raise DslError(f"sequence() references undeclared job {name!r}")
        for prev, follower in zip(names, names[1:]):
            if self._declared[follower]:
                raise DslError(
                    f"sequence() follower {follower!r} already has a condition;"
                    " refusing to merge (silent loss)"
                )
            self._amend_condition(follower, f"s({prev})")
            self._declared[follower] = True
        return self

    def parallel(
        self, names: list[str], *, after: str | None = None, then: str | None = None
    ) -> CatalogBuilder:
        """Fan already-declared jobs out from a common producer (each member
        gets s(after)) and optionally back in (`then` gets the conjunction
        of member successes). Same no-merge rule as sequence()."""
        if len(names) < 2:
            raise DslError("parallel() needs at least two member names")
        for name in [*names, *([after] if after else []), *([then] if then else [])]:
            if name not in self._declared:
                raise DslError(f"parallel() references undeclared job {name!r}")
        if after is not None:
            for member in names:
                if self._declared[member]:
                    raise DslError(
                        f"parallel() member {member!r} already has a condition;"
                        " refusing to merge (silent loss)"
                    )
                self._amend_condition(member, f"s({after})")
                self._declared[member] = True
        if then is not None:
            if self._declared[then]:
                raise DslError(
                    f"parallel() join {then!r} already has a condition;"
                    " refusing to merge (silent loss)"
                )
            self._amend_condition(then, " & ".join(f"s({m})" for m in names))
            self._declared[then] = True
        return self

    # ----------------------------------------------------------------- plumbing

    def _amend_condition(self, job: str, condition: str) -> None:
        needle = f"insert_job: {job}\n"
        for index, statement in enumerate(self._statements):
            if statement.startswith(needle):
                self._statements[index] = statement + f"condition: {condition}\n"
                return
        raise DslError(f"internal: statement for {job!r} not found")  # pragma: no cover

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


def _job_kwargs(job: JobIR, *, include_condition: bool) -> list[str]:
    out: list[str] = []
    exec_ = job.exec_
    if exec_ is not None:
        for field in ("command", "watch_file"):
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
        out.extend(_schedule_kwargs(job.schedule))
    sem = job.sem
    if include_condition and sem.condition is not None:
        out.append(f"condition={_py(cond_to_source(sem.condition))}")
    if sem.box_success is not None:
        out.append(f"box_success={_py(cond_to_source(sem.box_success))}")
    if sem.box_failure is not None:
        out.append(f"box_failure={_py(cond_to_source(sem.box_failure))}")
    if sem.max_exit_success:
        out.append(f"max_exit_success={sem.max_exit_success}")
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
    if job.resources:
        groups = " AND ".join(
            f"({r.name}, QUANTITY={r.quantity}" + (f", FREE={r.free}" if r.free else "") + ")"
            for r in job.resources
        )
        out.append(f"resources={_py(groups)}")
    if job.annotations:
        rendered = ", ".join(f"{_py(k)}: {_py(v)}" for k, v in job.annotations.items())
        out.append(f"annotations={{{rendered}}}")
    if job.passthrough:
        rendered = ", ".join(f"{_py(k)}: {_py(v)}" for k, v in job.passthrough.items())
        out.append(f"passthrough={{{rendered}}}")
    return out


def _plain_s_condition(job: JobIR, producer: str) -> bool:
    cond = job.sem.condition
    return (
        isinstance(cond, StatusAtom)
        and cond.status == "SUCCESS"
        and cond.lookback is None
        and cond.job.instance is None
        and cond.job.name == producer
    )


def decompile(catalog: CatalogIR, graph: DerivedGraph | None = None) -> str:
    """Emit a runnable Python module of builder calls; executing it rebuilds
    a catalog canonically equal to this one (the round-trip property)."""
    if graph is None:
        graph = derive_graph(catalog)
    lines: list[str] = [
        "# Decompiled by dsl41 (phase 10); executing this module rebuilds the catalog.",
        "from dsl41.dsl import CatalogBuilder",
        "",
        "c = CatalogBuilder()",
    ]
    for name, value in catalog.globals_declared.items():
        lines.append(f"c.global_({_py(name)}, {_py(value)})")
    for name, machine in catalog.machines.items():
        kwargs = [f"type={_py(machine.machine_type)}"] if machine.machine_type else []
        kwargs += [f"{k}={_py(v)}" for k, v in machine.attrs.items()]
        lines.append(f"c.machine({_py(name)}{', ' if kwargs else ''}{', '.join(kwargs)})")
    for name, resource in catalog.resources.items():
        kwargs = [f"res_type={_py(resource.res_type)}"] if resource.res_type else []
        kwargs += [f"{k}={_py(v)}" for k, v in resource.attrs.items()]
        lines.append(f"c.resource({_py(name)}{', ' if kwargs else ''}{', '.join(kwargs)})")
    for name, xinst in catalog.external_instances.items():
        kwargs = [f"xtype={_py(xinst.xtype)}"]
        kwargs += [f"{k}={_py(v)}" for k, v in xinst.attrs.items()]
        lines.append(f"c.xinst({_py(name)}, {', '.join(kwargs)})")

    # sequence() candidates: derived chains whose followers carry EXACTLY
    # s(prev) -- adjacency alone is not enough (module docstring)
    sequenced: set[str] = set()
    sequences: list[list[str]] = []
    for chain in graph.chains:
        if all(
            _plain_s_condition(catalog.jobs[follower], prev)
            for prev, follower in zip(chain, chain[1:])
        ):
            sequences.append(chain)
            sequenced.update(chain[1:])  # heads keep their own condition (if any)

    def emit_job(job: JobIR, indent: str, method: str = "job") -> None:
        include_condition = job.name not in sequenced
        kwargs = _job_kwargs(job, include_condition=include_condition)
        prefix = f"{indent}{'b' if indent else 'c'}.{method}({_py(job.name)}"
        if job.job_type == "FW":
            kwargs.insert(0, "job_type='f'")
        lines.append(f"{prefix}{', ' if kwargs else ''}{', '.join(kwargs)})")

    emitted: set[str] = set()

    def emit_box(box_name: str, indent: str) -> None:
        job = catalog.jobs[box_name]
        kwargs = _job_kwargs(job, include_condition=box_name not in sequenced)
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

    for root in graph.box_tree.roots:
        emit_box(root, "")
    for name, job in catalog.jobs.items():
        if name not in emitted:
            emit_job(job, "")
            emitted.add(name)
    for chain in sequences:
        args = ", ".join(_py(name) for name in chain)
        lines.append(f"c.sequence({args})")
    lines += ["", "catalog = c.build()", ""]
    return "\n".join(lines)
