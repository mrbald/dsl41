# Write an estate for rollout

This page is for the job author who writes an estate or brings an AutoSys estate in.
It takes you from JIL or the Python DSL to checks, the estate manifest, the rollout handoff and the interruption contract every job meets.
The rest of your track is under **Job author** in the
[README's Documents section](../README.md#documents).

## 1. Write JIL or use the Python DSL

Write JIL files directly, or write a Python module whose `estate()` function returns a `CatalogBuilder`.
Every job in hand-written JIL needs `job_type`: `c` for CMD, `b` for BOX, or `f` for FW.
The DSL sets this attribute.
The DSL uses the same pipeline: it emits JIL, parses it, and lowers it into a catalog.
`dsl41 build` turns the module into a JIL file.
The main config can select JIL and trusted Python modules together.
Preparation renders each module to captured JIL; the engine runs those bytes ([§6](#6-hand-over-for-rollout)).

The [README's DSL example](../README.md#dsl---jil-build) declares an estate:

```python
from dsl41.dsl import CatalogBuilder


def estate() -> CatalogBuilder:
    b = CatalogBuilder()
    b.machine("prod1")
    with b.box("nightly"):
        b.job("extract", command="/opt/etl/extract.sh", machine="prod1")
        b.job("transform", command="/opt/etl/transform.sh", machine="prod1")
        b.job("load", command="/opt/etl/load.sh", machine="prod1")
    b.sequence("extract", "transform", "load")
    return b
```

`machine()` declares a machine record.
`job()` declares a job with JIL attribute names as keyword arguments.
`box()` makes jobs declared inside its context members of that box.
`sequence()` gives each follower a condition on the preceding job, using `s()` by default.
`parallel()` can fan out from `after` and join with `then` or `then_any`.
`mutex(*names)` takes job names as separate arguments and adds pairwise bare `n()` guards.
Call it after `sequence()` or `parallel()`.
`contend()` assigns a shared resource group to the named jobs.
`global_()` declares a global.
`resource()` declares a resource.
`xinst()` declares an instance.
`calendar()` declares a calendar.
`cycle()` declares a cycle.
`extended_calendar()` declares an extended calendar.
Write conditions as strings using [§3](#3-write-conditions-with-explicit-grouping).
`sequence()` and `parallel()` refuse to replace an existing condition.

Inside `estate()`, you can add a file watcher:

```python
b.job(
    "INBOX_FW",
    job_type="f",
    machine="prod1",
    watch_file="/tmp/inbox.ready",
    watch_interval=1,
    watch_file_min_size=1,
)
```

`dsl41 build` imports the module, which runs its code, so use it only on modules you trust.
It lowers the JIL through the compiler before it writes.

1. Save the README example as a Python file named after your estate (the commands below
   call it catalog.py).

2. Write the JIL.

   ```sh
   dsl41 build catalog.py -o jobs.jil
   ```

3. In CI, fail when the checked-in JIL is stale.
   This step prints a diff, exits `1` on a difference, and writes nothing.

   ```sh
   dsl41 build catalog.py --check jobs.jil
   ```

`dsl41 build` exits `2` when the module has no `estate()`, when it raises, when `estate()` returns something else, or when the JIL does not lower.

An estate of several JIL files can be a package with one module per file (DL-418).
Its `__init__.py` composes the modules in the manifest's `files` order:

```python
from dsl41.dsl import CatalogBuilder

from . import calendars, eod


def estate() -> CatalogBuilder:
    files = {
        "calendars.jil": calendars.estate(),
        "eod.jil": eod.estate(),
    }
    return CatalogBuilder.compose(files)
```

`dsl41 build estate --out .` writes `calendars.jil` and `eod.jil`, and `dsl41 build estate --check .` compares both.
The estate lowers as one catalog, so a job in `eod.jil` may name a job in `calendars.jil` in its condition.
A job declared in two files is refused.
Then use the [checks in §4](#4-check-the-estate).

The [examples index](../examples/README.md) links the worked estates.
For JIL, start with [nightbank](../examples/nightbank/README.md).
The [file-processing Python estate](../examples/file_processing/estate.py) uses the DSL.

### 1.1. Add file processing to an estate

`add_file_processing_jobs` adds two command jobs to a builder that already has a file watcher.
The archiver moves each inbox delivery into the root's object store and records it in the ledger.
The processor passes each archived file to your handler and records the outcome.
[The file-processing README](../examples/file_processing/README.md) states the protocol and its limits.

```python
from dsl41.file_processing_dsl import add_file_processing_jobs

worker = ["/opt/site/venv/bin/python", "-m", "dsl41.file_processing_worker"]
common = ["--root", "/srv/fp", "--processor-id", "invoices-v1"]
add_file_processing_jobs(
    b,
    inbox_watcher="INBOX_FW",
    machine="prod1",
    archive_argv=[*worker, "archive", *common],
    process_argv=[*worker, "process", *common, "--handler", "invoices.handler:make"],
)
```

| Job | Condition | Command role |
| --- | --- | --- |
| `ARCHIVER` | `s(INBOX_FW)` | `archive` |
| `PROCESSOR` | `s(ARCHIVER, 0)` | `process` |

The call refuses with a `ValueError`, and adds neither job, when:

- `inbox_watcher` does not name an FW job already in the builder;
- an argv list is empty;
- a job name is taken, or the two names are equal;
- the call is inside a `box()` context;
- `archiver_attrs` or `processor_attrs` sets `job_type`, `box_name`, `machine`, `command`, `condition`, `name` or a statement verb such as `insert_job`, at the top level or inside `passthrough` or `annotations`;
- a name, an argv element or an attribute is one the builder refuses, such as a name with a space or an argv element with a newline.

Pass other JIL attributes, such as `owner` or `std_out_file`, in `archiver_attrs` and `processor_attrs`.
`archiver=` and `processor=` rename the two jobs.

On the host, each job's `command:` is the argv joined with shell quoting:

```text
/opt/site/venv/bin/python -m dsl41.file_processing_worker archive --root /srv/fp --processor-id invoices-v1
/opt/site/venv/bin/python -m dsl41.file_processing_worker process --root /srv/fp --processor-id invoices-v1 --handler invoices.handler:make
```

The command needs dsl41 and your handler's module installed in that Python environment, and no checkout.
`MODULE:FACTORY` names a callable that takes the root as a `Path` and returns the handler.
`--order oldest` and `--batch all` are the defaults and the production policy.

| Worker exit | Meaning |
| --- | --- |
| 0 | The selected snapshot completed. |
| 1 | The run stopped; the ledger holds the reason, or stderr does for a busy ledger. Exit 1 also covers a Python without dsl41 and any uncaught error; the ledger then holds no reason, so read stderr. |
| 2 | The command line or the handler could not be loaded; nothing ran. |
| 65 | A refusal: corrupt, inconsistent or unsupported state for an operator. |
| 75 | The role's lock was busy; nothing ran. |

The helper adds no timer and no rearm.
How the jobs run again is your choice: [the contract's activation ruling](../examples/file_processing/CONTRACT.md#activation-is-external-owner) lists the options and the zero-lookback edge.

## 2. Check which constructs dsl41 accepts

Use the [simulation coverage register](simulation-coverage.md) to check a construct's class, effect and refusing site.
Lowering refuses unknown attributes outside the inert allow-list under DL-07.
`--permit-unknown` carries those attributes verbatim.
In Python, `build(permit_unknown=True)` selects that permission.
The [entity contract](ir-design.md#4-ir-f-entities) defines the passthrough routes.
Lowering also refuses duplicate job names.
At definition time, `status:` accepts only INACTIVE, ON_HOLD, ON_ICE and ON_NOEXEC.
Lowering refuses resource groups using `QUANTITY=ALL` or `VALUEOP`/`VALUE`.
Write whole job definitions rather than `update_job`, `delete_job` or `override_job`.
Hand over changed definitions through [§6](#6-hand-over-for-rollout).
The operational boundary for `deployment-runbook.md` is [DL-424](decision-log.md).

Use these SEM entries for a construct's meaning:

| Construct | Contract |
| --- | --- |
| Condition state | [SEM-01: Conditions are latching state predicates, not edges](autosys-semantics.md#sem-01--conditions-are-latching-state-predicates-not-edges-v) |
| Atoms | [SEM-02: Condition atoms](autosys-semantics.md#sem-02--condition-atoms-v) |
| Grouping | [SEM-03: Operators and grouping](autosys-semantics.md#sem-03--operators-and-grouping-v) |
| Lookbacks | [SEM-04: Lookback qualifiers](autosys-semantics.md#sem-04--lookback-qualifiers-v) |
| Ice and lookbacks | [SEM-05: ON_ICE predecessors inside lookback conditions](autosys-semantics.md#sem-05--on_ice-predecessors-inside-lookback-conditions-v) |
| Missing jobs | [SEM-06: Undefined jobs in conditions](autosys-semantics.md#sem-06--undefined-jobs-in-conditions-v) |
| Instances | [SEM-07: Cross-instance atoms](autosys-semantics.md#sem-07--cross-instance-atoms-v) |
| Globals | [SEM-08: Global variables](autosys-semantics.md#sem-08--global-variables-v) |
| Exit codes | [SEM-09: max_exit_success shifts SUCCESS/FAILURE boundary](autosys-semantics.md#sem-09--max_exit_success-shifts-successfailure-boundary-v) |
| Box members | [SEM-10: Box membership and start rule](autosys-semantics.md#sem-10--box-membership-and-start-rule-v) |
| Box completion | [SEM-11: Box RUNNING/completion](autosys-semantics.md#sem-11--box-runningcompletion-v) |
| Box overrides | [SEM-12: box_success / box_failure override](autosys-semantics.md#sem-12--box_success--box_failure-override--with-evaluation-gating-v) |
| Box termination | [SEM-13: Box TERMINATED is sticky](autosys-semantics.md#sem-13--box-terminated-is-sticky-v) |
| Terminators | [SEM-14: box_terminator / job_terminator](autosys-semantics.md#sem-14--box_terminator--job_terminator-vc) |
| Member changes | [SEM-15: Member status changes can ripple upward](autosys-semantics.md#sem-15--member-status-changes-can-ripple-upward-vc) |
| Added members | [SEM-16: Jobs added to a RUNNING box](autosys-semantics.md#sem-16--jobs-added-to-a-running-box-v) |
| Nested boxes | [SEM-17: Deep nesting](autosys-semantics.md#sem-17--deep-nesting-c) |
| Box reset | [SEM-18: CHANGE_STATUS INACTIVE on a box cascades](autosys-semantics.md#sem-18--change_status-inactive-on-a-box-cascades-v) |
| Ice | [SEM-20: ON_ICE](autosys-semantics.md#sem-20--on_ice-v) |
| Hold | [SEM-21: ON_HOLD](autosys-semantics.md#sem-21--on_hold-v) |
| Noexec | [SEM-22: ON_NOEXEC](autosys-semantics.md#sem-22--on_noexec-v) |
| Start events | [SEM-23: FORCE_STARTJOB vs STARTJOB](autosys-semantics.md#sem-23--force_startjob-vs-startjob-c) |
| Initial status | [SEM-24: status: at definition time](autosys-semantics.md#sem-24--status-at-definition-time-v) |
| Time switch | [SEM-30: date_conditions is the master switch](autosys-semantics.md#sem-30--date_conditions-is-the-master-switch-v) |
| Schedule exclusions | [SEM-31: Mutual exclusivity](autosys-semantics.md#sem-31--mutual-exclusivity-v) |
| Start schedules | [SEM-32: start_times / start_mins](autosys-semantics.md#sem-32--start_times--start_mins-v) |
| Run windows | [SEM-33: run_window is a gate, not a trigger](autosys-semantics.md#sem-33--run_window-is-a-gate-not-a-trigger-v) |
| Must times | [SEM-34: must_start_times / must_complete_times are alarms only](autosys-semantics.md#sem-34--must_start_times--must_complete_times-are-alarms-only-v) |
| Job timezone | [SEM-35: timezone](autosys-semantics.md#sem-35--timezone-v) |
| Calendar records | [SEM-36: Calendar definitions: the autocal_asc record model](autosys-semantics.md#sem-36--calendar-definitions-the-autocal_asc-record-model-v) |
| Calendar grammar | [SEM-37: Extended-calendar date-condition keyword grammar](autosys-semantics.md#sem-37--extended-calendar-date-condition-keyword-grammar-v-defective-tokens-) |
| Calendar generation | [SEM-38: Extended-calendar generation: dispositions and adjust](autosys-semantics.md#sem-38--extended-calendar-generation-dispositions-and-adjust-v-core--corners) |
| Cycles | [SEM-39: Cycles](autosys-semantics.md#sem-39--cycles-v) |

`term_run_time` and resources have no SEM entry.
Their rows are in the [control-flow attribute inventory](autosys-semantics.md#5-attributes-with-control-flow-teeth-quick-inventory).

## 3. Write conditions with explicit grouping

For example, write `s(extract) & s(transform, 00.30)` to require both predicates.
The grammar applies to `condition`, `box_success` and `box_failure` values.
The [statement syntax](jil-statement-syntax.md#tokenization-rules) governs the surrounding JIL.

Use a status test such as `s(extract)`, an exit-code comparison such as `e(extract) = 0`,
or a global comparison such as `v(READY) = yes`.
[SEM-02](autosys-semantics.md#sem-02--condition-atoms-v) defines the atoms and their long names.
Keywords ignore case.
Use `&` or `and` for conjunction.
Use `|` or `or` for disjunction.
Both operators have equal precedence and associate from left to right.
Thus `a | b & c` means `(a | b) & c`.
Use parentheses to choose another grouping.
The grammar has no `NOT` operator.
Use status atoms such as `n()` or `f()` to express the required status test.
For `e()` and `v()`, use `=`, `!=`, `<`, `>`, `<=` or `>=`.
Global comparisons use integers when both values parse as integers, and strings otherwise.
Write a cross-instance reference as `job^INSTANCE` and declare the instance with `insert_xinst`.
Escape a colon in a JIL condition's job name as `\:`.
The DSL escapes that colon for you.

Write a lookback after the job name, as in `s(transform, 00.30)`.
The token accepts hours and minutes with a dot or escaped colon, or bare hours.
Write leading hours for sub-hour values; `.30` is invalid.
Status and exit-code atoms accept lookbacks; `v()` does not.
[SEM-04](autosys-semantics.md#sem-04--lookback-qualifiers-v) defines the ranges and the zero and indefinite lookbacks.

## 4. Check the estate

Pass all estate files to each command as one catalog.
You may include `autocal_asc` calendar exports.
Use repeated `-p/--properties PATH` options to resolve `~{$NAME}~` before parsing.
Later properties files override earlier files.
Use the [unknown-attribute policy in §2](#2-check-which-constructs-dsl41-accepts) for loading refusals.

### 4.1. Read lint findings

`dsl41 lint` reports the rule, file and line for each finding.
Use the [L001–L025 rule contract](ir-design.md#9-linter-architecture-findings-not-treatments) for meanings and severities.

| Lint exit | Meaning |
| --- | --- |
| 0 | No errors, and no warnings if `--strict` applies. |
| 1 | Errors, or warnings with `--strict`. |
| 2 | dsl41 could not read, parse or lower the input. |

Info findings never change the exit code.
`--suppress L005` removes that rule from the report and exit code.
Repeat `--suppress` or pass comma-separated codes to suppress other rules.
For L023, a job's `timezone` takes precedence over `--timezone`.
`--timezone` defaults to UTC.
`--timezone-map PATH` supplies the site's timezone aliases for L023.

### 4.2. Play the intended night

`dsl41 rehearse` uses the same engine as `run` under a virtual clock with scripted outcomes.
Without `--scenario`, every run succeeds in 0 s.
For the unscheduled `nightly` box in [§1](#1-write-jil-or-use-the-python-dsl), supply a start event.
Save this minimal scenario as `scenario.json`:

```json
{
  "events": [
    {
      "at": "2026-01-15T01:00:00",
      "kind": "STARTJOB",
      "payload": {"job": "nightly"}
    }
  ]
}
```

A scenario event's `at` may carry a zone and is read as UTC when it has none, as `--start` is.
For `STARTJOB`, use `{"job": NAME}` with the job's name as a JSON string.
Choose the job names, event times and `--start` date for your estate.

| Option | Use |
| --- | --- |
| `--scenario PATH` | Supply JSON with optional `adapter` and `events` keys, as in the example above. |
| `--start ISO` | Choose the start instant; the default is the current time in UTC. A value with no zone is read as UTC. |
| `--hours FLOAT` | Set the horizon; the default is `24.0`. |
| `--format text\|summary\|json` | Choose text, trace plus counts and statuses, or a JSON document; text is the default. |
| `--check-cadence` | Compare run counts with computed bounds; report unbounded jobs as unchecked. |
| `--cadence-policy PATH` | Supply JSON shaped as `{"schema_version":1,"policies":{"JOB":{"max_runs":2,"reason":"expected runs"}}}`; requires `--check-cadence`. |
| `--sweep fail` | Replay with each upstream job's first run failing; requires `--check-cadence`. |
| `--sweep flags` | Replay each global's tested values with and without reset; requires `--check-cadence`. |
| `--timezone`, `--timezone-map` | Use the manifest's timezone settings ([§6.1](#61-declare-the-estate-in-estatetoml)). |
| `--semantics NAME=VALUE` | Use the manifest's switches; the [switch contract](runner-design.md#8a-semantic-switches). |
| `--run-root PATH` | Also write the journal there; incompatible with a sweep. |
| `--permit-unknown`, `-p/--properties` | Use the loading options described above and in [§2](#2-check-which-constructs-dsl41-accepts). |

The scenario's `adapter.default` sets `duration_s` and `exit_code` for unscripted runs.
To park a run means to let it start without ever completing.
A null default parks unscripted runs.
`adapter.park` lists jobs to park; `adapter.runs` selects jobs by `job` and `run_number`.
Each selected run sets a duration and exit code, or `park: true`.
An `adapter.runs` entry overrides `adapter.park`, which overrides the default.
Repeat `--sweep` to include both sweep checks.
Cadence checks accept scenario events `STARTJOB`, `FORCE_STARTJOB` and `SET_GLOBAL`.
They park unscripted file watchers.
Check latch effects across days with `--check-cadence` and a longer horizon.
Minimum horizon for that check: `48` hours.

| Rehearse exit | Meaning |
| --- | --- |
| 0 | The estate went quiet: no work remains before the horizon. |
| 1 | The engine or estate failed during rehearsal. |
| 2 | The rehearsal could not start. |
| 3 | The cadence check found deviations. |

Exit `0` does not establish that the night ran as intended.
Compare the summary's trace, run counts and final statuses with the intended runs.

### 4.3. Inspect the graph

`dsl41 viz` draws the dependency graph.
Its default `report` format writes Markdown with a chart per independent workflow.
Check the file-watcher and schedule triggers, condition paths and box membership.
Read the appendices for standalone jobs, assumptions, redesign flags, OR shapes and cycles.
Edges use solid, dashed and thick red lines for E, A and R classes.
The [graph contract](ir-design.md#5-ir-g-derived-graph) defines the edge classes.
The UC backend boundary is [DL-288](decision-log.md), as ruled by DL-424.
Other formats are `chart`, `html`, `html-chart` and `explore`.
Use `-o/--out PATH` to write a file rather than stdout.
The `html` format writes a self-contained offline page.
The `explore` format writes an interactive navigation page.
Use `--collapse-threshold`, `--direction`, `--include-singletons`, `--elk` or `--fixed-scale` to shape output.
Each format accepts only the shaping options it can honor.
`viz` has no `--timezone` or `--timezone-map` option.
It exits `0` after writing output, or `2` for rejected input or shaping options.

### 4.4. Run the checks in order

These commands use `jobs.jil` and the scenario from [§4.2](#42-play-the-intended-night).
Replace `<base-zone>` with the engine's zone from the operator.
Add `--strict` where the site requires it; an estate brought in under [§5](#5-bring-an-existing-autosys-estate-in) always lints with `--strict`, the [migration gate](../README.md#lint-a-catalog).
Use [§6](#6-hand-over-for-rollout) to match the rollout profile.

1. Lint the catalog; fix findings and repeat until the exit code passes.

   ```sh
   dsl41 lint jobs.jil --timezone <base-zone>
   ```

2. Rehearse the chosen night using [§4.2](#42-play-the-intended-night).

   ```sh
   dsl41 rehearse jobs.jil --scenario scenario.json --start 2026-01-15T00:00:00 \
       --hours 24 --timezone <base-zone> --format summary
   ```

3. Inspect the graph using [§4.3](#43-inspect-the-graph).

   ```sh
   dsl41 viz jobs.jil --format report -o graph.md
   ```

## 5. Bring an existing AutoSys estate in

`decompile` writes a Python module over the [builders in §1](#1-write-jil-or-use-the-python-dsl), with the `estate()` function that §1 describes.
Its default `--check` calls `estate()` and compares the catalog's canonical hash with the input.
The canonical form is the compiler's normalized representation for comparison.
This check compares canonical form rather than original bytes.
The [fidelity contract](jil-statement-syntax.md#fidelity-tests-normative) defines byte preservation separately.

Decompile the template JIL, with its `~{$NAME}~` placeholders, not a copy bound with `-p`.
The module keeps placeholders as written, but it cannot recover them from bound values.
`--params` adds a `Params` dataclass with one field per placeholder, and a comment above each field names the attributes it fills.
`estate()` with no argument keeps every token.
`dsl41 build estate.py --stdout --param HOST=prod1` fills `HOST` for a local look.
For rollout, the main config supplies the properties layers ([§6](#6-hand-over-for-rollout)).
The authority for `estate build` and its `--param` boundary is [DL-413](decision-log.md), as ruled by DL-424.
Decompile refuses a placeholder named as a Python keyword, starting with `__`, or named as a name the module itself uses, such as `str` or `Params`.

Give several files, in the manifest's `files` order, and an output directory to get a package, one module per file ([§1](#1-write-jil-or-use-the-python-dsl)):

1. Decompile the files.

   ```sh
   dsl41 decompile --params calendars.jil eod.jil -o estate/
   ```

2. Rewrite the JIL files once from the package.
   They are equal to the inputs in canonical form, not byte for byte: the builder orders attributes its own way and drops comments.

   ```sh
   dsl41 build estate --out .
   ```

3. In CI, fail when a checked-in file is stale.

   ```sh
   dsl41 build estate --check .
   ```

Each module holds its own file's jobs and folds within that file.
A condition on another file's job stays an explicit string.
The package names each file by its base name, so a manifest name with a directory, such as `jobs/eod.jil`, does not fit it.
Decompile refuses two inputs whose names differ only in case, and a box whose jobs are in another file.

1. Decompile the input catalog.

   ```sh
   dsl41 decompile old.jil -o estate.py
   ```

2. Write the JIL from the module.

   ```sh
   dsl41 build estate.py -o rebuilt.jil
   ```

3. Compare the rebuilt catalog with the input.

   ```sh
   dsl41 equiv rebuilt.jil --against old.jil
   ```

The decompiler reports folds and constructs left explicit on stderr.
`dsl41 folds` lists the recognized patterns.
Use repeated `--no-fold CODE` options, or comma-separated codes, to disable folds.
The decompiler leaves unmatched conditions as explicit `job(condition=...)` calls.
It does not fold lookbacks, cross-instance references, exit-code atoms or box overrides.
It also leaves one-way or nested `n()` conditions and `run_window` explicit.
It refuses job names that the builder cannot express.
The decompiler exits `0` after writing the module.
It exits `1` when its check differs, but still writes the module.
Report that difference as a decompiler bug.
It exits `2` for rejected input or a decompiler refusal.

`equiv` checks the requested tiers; `--tier all` is the default.

| Tier | What it checks | Limit |
| --- | --- | --- |
| a | Structural equality of canonical form, including schedules. | It compares normalized catalogs. |
| b | Per-job condition logic and the derived graph. | It defers without failing if the state space is too large; it does not check schedules. |
| c | Oracle traces on shared, seeded event scripts. | The oracle has no calendar, so this tier does not check schedules. |

Identical canonical hashes let `equiv` return equivalent without further tier checks.
`--scripts` sets the script count for tier c.
Use `--rename OLD=NEW` and `--case-fold` for a renamed estate.
`-p` applies the same properties to both catalogs.
`equiv` exits `0` when the requested checks pass, `1` for a difference, or `2` for rejected input.
These checks establish their stated comparisons, not live AutoSys equivalence for every event.

Keep production JIL out of the repository and issues.
The [corpus policy](jil-statement-syntax.md#corpus-policy) permits synthetic or documentation-derived JIL only.
`dsl41 minify` writes a de-identified copy that keeps structure.
Read its output before sharing it.
See the [minify instructions](../README.md#de-identify-an-estate-minify) for its refusals.

## 6. Hand over for rollout

Give the operator the checked estate inputs, their source revision, the intended
schedule and outcomes, and every interruption exception in [§6.2](#62-write-every-job-to-survive-interruption).
Use the [checks in §4.4](#44-run-the-checks-in-order) before the handoff.
Agree the properties, environment, timezone, aliases and semantic switches with the operator.

The engine's main config selects the rollout inputs (DL-437).
Its default path is `/etc/dsl41/dsl41.toml`; `--config` selects another absolute path.
The operator owns the engine root and host settings.
For example, this complete config selects the `jobs.jil` from §1:

```toml
format_version = 1

[engine]
run_root = "/srv/dsl41/runs/payments"
as_machine = ["prod1"]

[estate]
include = ["jobs.jil"]
timezone = "UTC"

[lint]
refuse = "error"
ignore = []
```

Relative input paths start at the config's directory.
Place `jobs.jil` beside this example config, or agree absolute input paths with the operator.
The optional `estate.properties` list supplies properties layers in order.
Later layers override earlier ones.
`estate.timezone_map` selects the alias file; `[estate.semantics]` sets the
[switches](runner-design.md#8a-semantic-switches).
`engine.environment` selects a declared `[estate.environments.NAME]` table.
It is required when the config declares environments.
Its properties follow the base layers.

`estate.include` accepts ordinary globs, with selected inputs compiled in sorted order
as one catalog. Recursive `**` patterns are refused.
A glob that matches nothing is reported, not refused, unless the whole selection is empty.
Check reread's removed list.
A missing literal input path is refused.
An unreadable input, duplicate selection or duplicate job is refused with its origins.
A condition may refer to a job in another selected input.
Select each JIL or Python definition once; do not include a module and its generated JIL together.

Selecting Python executes trusted authoring code during preparation.
Preparation captures the generated JIL for the plan; update does not import Python.
Imported dependencies are not fully tracked.
Use prebuilt JIL from [§1](#1-write-jil-or-use-the-python-dsl) when the handoff needs a reproducible build.

The following checks use the installed candidate config.
The operator supplies access to its paths; a job author can use an agreed absolute
config path in a separate check area instead.
With `--config`, dsl41 refuses `--strict`, `--suppress`, positional files and `-p` with exit 2.
Use `[lint]` for policy and the config for the input files and properties.

1. Check the selected inputs and lint findings.

   ```sh
   dsl41 lint --config /etc/dsl41/dsl41.toml
   ```

2. Rehearse the intended night, using a scenario for these job names as in [§4.2](#42-play-the-intended-night).

   ```sh
   dsl41 rehearse --config /etc/dsl41/dsl41.toml --scenario scenario.json \
       --start 2026-01-15T00:00:00 --hours 24 --format summary
   ```

The main config's lint policy defaults to `refuse = "none"` and `ignore = []`.
`error` blocks unignored errors; `warn` blocks warnings too; info never blocks.
Under `none`, exit 0 can include findings.
Lint exits 1 for blocking findings and 2 for refused inputs.
Blocking findings make `rehearse --config` and `reread` refuse with exit 2.
For an estate brought in under [§5](#5-bring-an-existing-autosys-estate-in),
set `refuse = "warn"` to apply the `--strict` migration gate in [§4.4](#44-run-the-checks-in-order).
Input, lowering, exception and preflight errors cannot be suppressed by this policy.
A rehearsal output `--run-root` must neither equal, contain nor lie inside the engine root.
Compare the rehearsal's trace, counts and statuses with the intended runs before handoff.

The operator follows [Reload a changed estate](deploy.md#reload-a-changed-estate).
The operator owns engine restarts and host settings.
Reread captures a plan and prints the changed jobs, their sources, and running jobs it would stop.
Update applies the captured bytes against the revision reread saw, even if source files change.
A later source edit needs another reread and reviewed update.

The operator must consent with `--stop-running` when the update would stop running jobs.
Without that consent, the engine refuses the update (DL-427, DL-432).
Unchanged jobs keep running.
An edit to `estate.timezone`, `estate.timezone_map`, `[estate.semantics]` or
`insert_xinst` records marks every job changed and can stop every running job.
Affected jobs wait while the engine stops their attempts and applies the plan.
A changed job's replacement uses the new definition and exception set;
a removed job gets no replacement.
[§6.2](#62-write-every-job-to-survive-interruption) defines what follows an interruption.

The authority for `release prepare/apply`, `seal`, `estate quiesce`,
`estate build/diff` and the release-train part of DL-422 is [DL-424](decision-log.md).

### 6.1. Declare the estate in estate.toml

`estate.toml` declares a file list and interpretation for tests and rehearsal
(DL-407, DL-437).
It is separate from the engine's main config in [§6](#6-hand-over-for-rollout).
This example declares four JIL files, a base properties layer and two environments:

```toml
name = "payments"
requires_dsl41 = ">=1.13"
files = ["calendars.jil", "global.jil", "eod.jil", "reports.jil"]
properties = ["base.properties"]
timezone = "UTC"

[semantics]
# NAME = "VALUE", one line per switch the estate sets

[environments.prod]
properties = ["prod.properties"]

[environments.uat]
```

| Key | Required | What it holds |
| --- | --- | --- |
| `name` | yes | The estate's name; dsl41 does not use it. |
| `requires_dsl41` | no | The lowest dsl41 version the estate needs, as `">=X.Y"` or `">=X.Y.Z"`. |
| `files` | yes | The catalog files, in command-line order. |
| `properties` | no | The base properties layers; a later layer overrides an earlier one. |
| `timezone` | no | The base zone; UTC when omitted. |
| `timezone_map` | no | The site's timezone alias file. |
| `semantics` | no | A table of the [semantic switches](runner-design.md#8a-semantic-switches), each `NAME = "VALUE"`. |
| `environments.NAME.properties` | no | Layers applied after the base layers for environment `NAME`. |

Use the file order consistently; it participates in the catalog hash.
Every named path is relative to the manifest, in normal form: no `.` or `..` part,
no `//`, and no `/` at either end.
Each names an existing file under the manifest's directory.
A `.py` path is refused here; render it with `dsl41 build` first (DL-410, DL-413).
Unknown keys and duplicate paths within the files or the selected properties layers are refused.
`requires_dsl41` accepts only `>=` and compares leading version integers;
a pre-release or dev build of X counts as X.

When the manifest declares environments, choose one with `--environment`.
An undeclared environment is refused.
An empty table such as `[environments.uat]` selects only the base layers.
Repeated `-p/--properties` options add site layers after the manifest's layers.

For this example, put the manifest and all its named inputs under `/work/payments`.
Choose a minimum version supported by the installed runtime before checking it.

1. Lint the declared catalog with the base properties and UTC.

   ```sh
   dsl41 lint --manifest /work/payments/estate.toml --environment uat
   ```

2. Rehearse it with a scenario for its job names and the intended night.

   ```sh
   dsl41 rehearse --manifest /work/payments/estate.toml --environment uat \
       --scenario scenario.json --start 2026-01-15T00:00:00 --hours 24 --format summary
   ```

`lint`, `rehearse`, `viz` and the explicit-manifest form of `run` accept
`--manifest` with `--environment`.
The manifest path must be absolute.
Do not combine it with positional files or command-line timezone, timezone-map or semantics options.
`viz` uses the file list and properties, with no timezone options.
A manifest refusal exits 2 before work begins.
The `--manifest` expansion rule is [DL-422](decision-log.md); DL-424 rules its excluded verbs.

### 6.2. Write every job to survive interruption

Write each command so repeating an invocation after a kill leaves the result of one invocation.
A **run** is the job invocation the scheduler or operator starts.
An **attempt** is one launch of that run's command.
An interruption can cause another attempt of the same run automatically (DL-424).

For example, a report command can write a temporary file and rename it at the end:

```sh
make_report > /data/out/report.csv.tmp && mv /data/out/report.csv.tmp /data/out/report.csv
```

A kill before `mv` leaves the temporary file; the replacement attempt overwrites it.
The rename is atomic when both paths are on one filesystem.
Appending without an invocation marker is unsafe:

```sh
extract_rows >> /data/out/ledger.csv
```

A replacement can append rows already written by the interrupted attempt.
Use a durable invocation key to prevent duplicate database writes, messages and other side effects.
A checkpoint is sufficient only if resuming it also prevents duplicate effects.

Every command gets these variables:

| Variable | Meaning across attempts of one run |
| --- | --- |
| `DSL41_JOB` | Job name; unchanged. |
| `DSL41_RUN_NUMBER` | Run number; unchanged. |
| `DSL41_SCHEDULED_AT` | Invocation instant in ISO-8601 UTC with `Z`, truncated to seconds; unchanged. |
| `DSL41_ATTEMPT` | Attempt ordinal; changes for each replacement. |

Use the first three as the stable invocation key.
Do not use `DSL41_ATTEMPT` to deduplicate the run's work.
Each attempt also has a distinct `run_id` and evidence directory
`runs/<job>.<run_number>/<attempt>/` (DL-429).

An operator stop or engine upgrade drains running attempts.
SIGINT, SIGTERM and `dsl41 engine stop` use that drain.
Engine death ends the running attempts through their wrappers' lifelines;
a host reboot can also leave no exit record.
An update stops the running attempts of jobs it changes or removes while the engine stays up.
KILLJOB, `term_run_time`, terminator cascades and CHANGE_STATUS use the oracle's stop rule;
they do not request an automatic replacement (DL-429).
[SEM-14](autosys-semantics.md#sem-14--box_terminator--job_terminator-vc) defines terminator cascades.

The engine asks a command to stop with TERM, then KILL after the command grace.
Your SIGTERM cleanup must finish within that grace.
A stop already recorded remains an interruption even if the command catches TERM and exits 0.
It does not become a successful completion that releases successors.
A command that ended naturally before the stop follows its real outcome through
[SEM-09](autosys-semantics.md#sem-09--max_exit_success-shifts-successfailure-boundary-v).
A spawn failure is FAILURE; an ordinary application failure gets no automatic retry.

The [attempt outcome contract](control-protocol.md#5-streaming-verb-subscribe)
and [recovery contract](runner-design.md#7-journal-and-recovery-e1-prod-grade)
govern the evidence (DL-433).
At restart, the engine reads each open command attempt once without waiting for its wrapper.
A missing exit record is an interrupted, unobservable attempt with cause `engine_death`
or its recorded first stop cause.
E7's `exit_status_unobservable` applies only outside the interruption rule (DL-424).
For a job still in the applied catalog whose run is still STARTING or RUNNING:

- an idempotent job gets the next attempt of the same run and invocation;
- a job marked non-idempotent gets FAILURE with cause `interrupted` and ON_HOLD together;
- an interruption at the attempt limit also gets FAILURE and ON_HOLD, with the limit in its evidence;
- unreadable or mismatched attempt records are quarantined and hold the job with evidence.

A removed job, an oracle kill or a run the oracle already ended gets no replacement.
A replacement does not report a failure to the oracle, fail its box or fire a successor condition.
Held FAILURE is a real status; dependent failure conditions can observe it.
The engine starts no next attempt or run while the job's previous attempt remains unsettled.
A failed process scan or surviving process keeps cleanup held and polled; it is not record quarantine.
The [status response](control-protocol.md#status-job) shows `attempt` and `interrupted` evidence.

| Bound | Default | Where the operator sets it |
| --- | --- | --- |
| TERM to KILL | 10 s | `grace_term_s` in the run root's `engine.toml`. |
| Confirmation after KILL | 5 s | `grace_kill_s` in the run root's `engine.toml`. |
| Attempts per run | 5 | Engine limit; an interruption on attempt 5 holds the job. |

The explicit-file or manifest form of `run` accepts `--grace-term` and `--grace-kill`
as overrides for one engine start.
The main-config form takes the graces from `engine.toml`.
Ask the operator for the site's grace and unit stop timeout.

Declare a job that cannot safely repeat in `non-idempotent.toml`:

```toml
[jobs]
"ledger_post" = "posts duplicate ledger entries if interrupted and repeated"
```

Each key names a command job defined in the estate, and each value is a non-empty, one-line reason.
A box, file watcher or unknown job name is refused by L025, whatever the lint policy.
Quote names containing dots.
The default file is beside the main config; `estate.non_idempotent` can select another path.
With a manifest, the file is beside `estate.toml`.
An absent default file means every job is idempotent.
A selected exception file that cannot be read or parsed is refused.
The applied configuration stores the exception set; editing the file alone changes no running policy.
Include exception edits in the reviewed reread/update handoff.

Give the operator a recovery procedure for each marked job.
It must establish whether the interrupted invocation already committed its side effects,
repair or complete them, and explain when another invocation is safe.
After checking the evidence, the operator can use FORCE_STARTJOB to clear the interruption hold
and start a new run (DL-243).
OFF_HOLD clears the hold; the job starts at once if its starting conditions already hold
([SEM-21](autosys-semantics.md#sem-21--on_hold-v)).
An operator's deliberate new run can repeat business work too; its invocation key is different.

The engine does not implement `n_retrys` application retries
([attribute inventory](autosys-semantics.md#5-attributes-with-control-flow-teeth-quick-inventory),
[Q4](autosys-semantics.md#9-open-questions-and-their-pinned-defaults)).
That limit does not prevent interruption replacements within one run.
Start times missed during downtime are skipped and reported (DL-45).
A backward wall-clock step keeps admission time at the recorded frontier;
it does not refuse operator inputs or startup (DL-435).
A frontier ahead of the wall can delay ticks and timers until the wall catches up.

Keep the inherited `DSL41_COOKIE` when launching helpers.
On Linux, the engine finds live members of the recorded process group and readable processes
carrying the attempt's cookie, including helpers that call `setsid` (DL-431, DL-436).
This scan works with or without pidfd support.
A helper outside the group that drops the cookie, becomes unreadable or changes uid can escape it.
Off Linux, the process group and validated leader identity decide.
The job must stop its own helpers; the unit's control group is the stronger final boundary.
The [wrapper's cookie contract](wrapper-protocol.md#dsl41_cookie-env-variable--the-process-cookie-an-addition)
defines the limits.
The `process-leak` [semantic switch](runner-design.md#8a-semantic-switches) decides whether
processes left after a natural exit are stopped or left running with a warning.
Failed scans and survivors under cleanup block another launch.

The authority for DL-205, DL-390, DL-394, DL-401, DL-416 and the supervisor,
deadman, forced-maintenance, `supervisor-protocol.md` and `site-package.md` references is [DL-424](decision-log.md).
The same ruling covers `--deadman`, `--force=terminate`, `DSL41_DRAIN_S`,
`CMD_GRACE_S`, `MAX_JOBS` and `MAX_GRACE_S`.
Runner E4, E7, E8 and E9 resolve through [§15](runner-design.md#15-open-questions-e-series),
with interruption governed by DL-424 and DL-433.
The process-group limit in DL-403 is governed by DL-431 and DL-436 on Linux.

## 7. Follow the other tracks for engine work

This page leaves installation and engine operation to the [operator guide](deploy.md),
compiler internals to the [architect track](architecture.md),
and each `--semantics` choice to the [switch contract](runner-design.md#8a-semantic-switches).
The authority for `stonebranch-semantics.md` and the UC backend is [DL-288](decision-log.md)
as ruled by DL-424.
