# Write an estate for rollout

This page is for the job author who writes an estate or brings an AutoSys estate in.
It takes you from JIL or the Python DSL to checks, the estate manifest and the rollout handoff.
The rest of your track is under **Job author** in the
[README's Documents section](../README.md#documents).

## 1. Write JIL or use the Python DSL

Write JIL files directly, or write a Python module whose `estate()` function returns a `CatalogBuilder`.
Every job in hand-written JIL needs `job_type`: `c` for CMD, `b` for BOX, or `f` for FW.
The DSL sets this attribute.
The DSL uses the same pipeline: it emits JIL, parses it, and lowers it into a catalog.
`dsl41 build` turns the module into a JIL file.
The engine and run root take JIL bytes only.

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
    "INBOX_FW", job_type="f", machine="prod1",
    watch_file="/tmp/inbox.ready", watch_interval=1, watch_file_min_size=1,
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
Follow the [job-edit recipe](deployment-runbook.md#recipe-add-change-or-remove-a-job).

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
Use the [L001–L023 rule contract](ir-design.md#9-linter-architecture-findings-not-treatments) for meanings and severities.

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
The glossary defines [R/A classification (UC backend)](glossary.md#ra-classification-uc-backend).
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

Use the [gate procedure in §4.4](#44-run-the-checks-in-order) away from the production run.
The manifest declares the timezone, its map and the semantic switches ([§6.1](#61-declare-the-estate-in-estatetoml); DL-407).
The runbook's [check block](deployment-runbook.md#recipe-add-change-or-remove-a-job) passes them to `lint` and `rehearse` through `dsl41 estate args`, with the site's properties file last.

After the checks pass, commit and tag the estate edit under the
[job-edit recipe](deployment-runbook.md#recipe-add-change-or-remove-a-job).
The tag holds the estate's manifest ([§6.1](#61-declare-the-estate-in-estatetoml)).
Give the operator the checked, tagged estate for the rollout window.
The operator follows [deployment-runbook §6](deployment-runbook.md#6-jil-rollout--updating-the-estate)
from there.

### 6.1. Declare the estate in estate.toml

The manifest, `estate.toml`, declares the estate's inputs once, beside its JIL (DL-407).
The launcher, the release helper and your checks take the file list, the properties layers,
the timezone and the semantic switches from it.
This example declares four files, one layer for every environment and one more for `prod`:

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
| `properties` | no | The properties layers every environment applies; a later layer overrides an earlier one. |
| `timezone` | no | The base zone; without it, every verb works in UTC. |
| `timezone_map` | no | The site's timezone alias file. |
| `semantics` | no | A table of the [semantic switches](runner-design.md#8a-semantic-switches), each `NAME = "VALUE"`. |
| `environments.NAME.properties` | no | The layers environment `NAME` applies after the base ones. |

The order of `files` is part of the catalog's hash, so a reorder is a catalog change.
Every name is a path relative to the manifest, with no `..` part, and names an existing file under the manifest's directory.
dsl41 refuses an unknown key at any level, and a path that `files`, or the base and one environment's layers, name twice.
`requires_dsl41` accepts only `>=`, and compares the leading integers of the version, so a pre-release or dev build of X counts as X.

The environment layers follow the base layers.
The launcher and the release helper always pass the host's `ENVIRONMENT`, and `estate args` refuses an environment the manifest does not declare.
Declare every environment a host's `ENVIRONMENT` names, with an empty table when it adds no layer.
`[environments.uat]` above declares one that applies the base layers only.
The site's own properties file comes after every layer, so the site's values win.
One tag serves every environment.

`dsl41 estate args MANIFEST --for VERB` prints the arguments the manifest gives one verb, as one shell-quoted line.
`VERB` is `run`, `seal`, `lint`, `rehearse` or `viz`.
Give `--environment NAME` when the manifest declares environments, and `--format json` for `{"argv": [...]}`.
`MANIFEST` must be an absolute path, because the bundle hash covers each path string as printed.
For the example above at `/work/payments/estate.toml`, `--for lint --environment uat` prints the files, a `-p` per layer and the timezone:

```text
/work/payments/calendars.jil /work/payments/global.jil /work/payments/eod.jil /work/payments/reports.jil -p /work/payments/base.properties --timezone UTC
```

`--for rehearse` adds `--semantics` per switch, `--for viz` stops after the layers, and `--for seal` prints the `--next` forms.
The command exits `0` after printing, or `2` with one sentence that names the refusal.

Run the runbook's [check block](deployment-runbook.md#recipe-add-change-or-remove-a-job) in CI on the commit you are about to tag.
It runs `estate args`, then `lint` and `rehearse` with the printed arguments and the site's properties file.
A manifest that `estate args` refuses fails that check, before the tag exists.

## 7. Follow the other tracks for engine work

This page leaves engine operation to the [operator track](deployment-runbook.md#0-the-operator-path),
compiler internals to the [architect track](architecture.md),
the descoped UC backend to [its specification](stonebranch-semantics.md) ([DL-288](decision-log.md)),
and the meaning of each `--semantics` switch to the [switch contract](runner-design.md#8a-semantic-switches).
