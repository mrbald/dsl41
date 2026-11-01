# Offline snapshot examples

For the operator inspecting offline observations on an emitted graph.
The dsl41 engine writes no snapshot sidecar (DL-385).
Read live state through the [engine status query](../../docs/deploy.md#watch-the-engine-and-jobs).
Use this page to generate the example HTML, import a matching JSON sidecar,
and inspect raw values and coverage. The rest of the
[operator track](../../README.md#documents) covers engine operation and incidents.
This page leaves engine startup and recovery to the
[deployment guide](../../docs/deploy.md).

These files attach invented observations to the existing synthetic Nightbank
and fulfilment graphs. The values are independent examples, not a captured run.
A box's supplied state is separate from the observations supplied for its
descendant leaf jobs.

Each JSON sidecar contains only `nodes` and a string `state` for every entry.
No source, timestamp, version or state vocabulary is required. IDs match the
emitted graph exactly. Optional metadata is described in the viewer's Format
section. Replacement drops observations omitted from the new file.
Invalid input keeps the previous snapshot.

## Nightbank

1. From the repository root, emit either profile:

   ```sh
   uv run dsl41 viz --format explore \
     -p examples/snapshots/viewer.properties \
     -o /tmp/nightbank-small-explore.html \
     examples/nightbank/estate/small/*.jil

   uv run dsl41 viz --format explore \
     -p examples/snapshots/viewer.properties \
     -o /tmp/nightbank-bank-explore.html \
     examples/nightbank/estate/bank/*.jil
   ```

2. Open the generated HTML locally.
3. Open Snapshot.
4. Choose Import JSON and select
   [nightbank-small.json](nightbank-small.json) or
   [nightbank-bank.json](nightbank-bank.json) for the matching profile.
5. Display starts at Generic. Select AutoSys to see its built-in display aids
   for literal codes such as `SU`, `FA` and `RU`.

Raw strings remain unchanged.

| Observation | Small | Bank |
| --- | --- | --- |
| Catalog supplied / total, including boxes | 6 / 82 | 6 / 520 |
| External matches | 1 | 1 |
| Unknown IDs | 1 | 1 |
| Ineligible IDs | 2 | 2 |
| EMEA_EOD_B leaf supplied / total | 4 / 24 | 4 / 148 |
| EMEA_EXE_B leaf supplied / total | 2 / 3 | 2 / 10 |
| EMEA_ACC_B leaf supplied / total | 1 / 3 | 1 / 10 |

Use the graph's Find field to inspect these exact names:

- `EMEA_EOD_B` has its own `SU` observation. Its four supplied leaf values are
  `SU`, `FA`, `""` and `demo: review required`, once each.
- `EMEA_EXE_B` has its own `RU` observation. Its supplied leaf values are
  `SU` and `FA`; the box's `RU` is excluded from those counts.
- `EMEA_ACC_B` has no own observation. Its descendant
  `EMEA_ACC_GL_CLOSE_C` has an explicitly supplied empty string, displayed as
  `""`. Empty is a supplied value, not a missing observation.
- `EMEA_REC_CUST_F` has no observation. It shows not supplied.
- `GROUP_TREASURY_EOD^PRD` is the exact emitted external ID with a supplied
  `SU`. It contributes to external matches, not catalog or box leaf coverage.

The unfamiliar `demo: review required` state remains opaque. It belongs to
`EMEA_VAL_SHARD1_C` in small and `EMEA_VAL_EQ_SHARD1_C` in bank.
`UNKNOWN_EXAMPLE_NODE` appears in unknown IDs. The synthetic lock hub
`lock:r:GL_DB` and edge `e0` appear in ineligible IDs. Their raw entries remain
inspectable; they receive no observation marker or graph action.

Coverage is partial on purpose. Descendant box scopes overlap, so their counts
must not be added into a global total. Folding or hiding nodes leaves these
hierarchy counts unchanged. A box marker represents only that box's supplied
observation, never a state inferred from children. These counts are not
completion percentages.

## Generic business vocabulary

1. From the repository root, emit the existing eight-node fulfilment graph:

   ```sh
   uv run dsl41 viz --format explore \
     -p examples/snapshots/viewer.properties \
     -o /tmp/fulfilment-generic-explore.html \
     examples/fulfilment/estate.jil
   ```

2. Open the generated HTML locally.
3. Open Snapshot.
4. Choose Import JSON and select [fulfilment-generic.json](fulfilment-generic.json).
5. Keep Display at Generic.

Four of eight catalog nodes have observations.
`FF_WAVE_B` has its own `awaiting handover` value and leaf coverage 3 / 7.
Its supplied leaf values are `stock allocated`, `parcel packed` and
`carrier label pending`, once each. The box value is excluded from that list.
There are no external matches, unknown IDs or ineligible IDs.

These are business observations associated with existing emitted nodes.
The viewer preserves the words as supplied; it does not interpret them as
scheduler states or load a different topology.

## AutoSys summary converter

The standalone standard-library helper converts one bounded English `autorep`
summary table to the same JSON sidecar. It reads stdin or a UTF-8 file. It emits
only JSON on success. `-o` replaces a named output only after the whole input is
validated and JSON is prepared. Invalid input emits no JSON and keeps an existing
output file. No AutoSys connection or project installation is needed.

[autorep-summary.txt](autorep-summary.txt) is manually synthetic text, based on
documented columns and public Nightbank IDs. It is not an AutoSys capture.

1. From the repository root, convert the synthetic table:

   ```sh
   python3 scripts/autorep_snapshot.py examples/snapshots/autorep-summary.txt \
     -o /tmp/nightbank-autorep.json
   ```

2. In the small Nightbank page above, choose Import JSON, or Replace JSON if
   a snapshot is loaded, and select `/tmp/nightbank-autorep.json`.

There are three catalog observations and one exact external match.
`EMEA_EOD_B` keeps `RU/NE`; `EMEA_EXE_B` keeps `RU`; its supplied order-freeze child keeps `SU`.
The external `GROUP_TREASURY_EOD^PRD` keeps the invented `Z?` value. The AutoSys
display preset recognizes only its built-in literal codes/names. Composite
values such as `RU/NE` and unknown values such as `Z?` remain raw and neutral.

For an actual capture, redirect stdout directly to a file outside this checkout.

1. Capture and convert only after producer success and empty, separately
   captured stderr:

   ```sh
   if autorep -J ALL -s > /tmp/autorep-summary.txt 2> /tmp/autorep-summary.err \
     && test ! -s /tmp/autorep-summary.err; then
     python3 scripts/autorep_snapshot.py /tmp/autorep-summary.txt \
       -o /tmp/autorep-snapshot.json
   else
     printf '%s\n' 'autorep failed or wrote stderr; conversion skipped' >&2
     false
   fi
   ```

The supported columns and capture flags come from the English
[AutoSys 12.0 reference](https://techdocs.broadcom.com/us/en/ca-enterprise-software/intelligent-automation/autosys-workload-automation/12-0/reference/ae-commands/monitor-and-report-on-workload/autorep-command-report-job-machine-and-variable-information.html)
and [24.2.00 reference](https://techdocs.broadcom.com/us/en/ca-enterprise-software/intelligent-automation/autosys-workload-automation/24-2-00/reference/ae-commands/monitor-and-report-on-workload/autorep-command-report-job-machine-and-variable-information.html).
Default job names are complete. Do not use `-n`, which permits truncation.
In these editions `-w` expands condition names with `-q`; it is not a wide-name
switch. Do not mix detail, definition, time-zone, previous-run or version output
into this table. Record `autorep -x` separately when identifying a capture.

The helper supports only this subset (DL-387):

- One consecutive table, surrounded by optional blank lines, with literal
  `Job Name`, `Last Start`, `Last End`, `ST/Ex`, `Run/Ntry`, `Pri/Xit` headings.
  Six underscore groups define the columns. The name width varies; the other
  widths are 20, 20, 5, 8 and 7, separated by one space.
- Printable ASCII table characters and LF or CRLF line endings. Non-ASCII,
  tabs, controls, terminal escapes and a UTF-8 BOM are unsupported. The vendor
  examples do not establish Unicode byte/display widths. This helper limit
  does not narrow the generic viewer's arbitrary Unicode JSON strings.
- One nonempty name token after hierarchy indentation and padding are removed.
  Case, punctuation and qualifiers remain exact. No fixed name maximum is
  imposed. Duplicate names, malformed or overflowing rows, shifted separators,
  repeated headers, internal blank lines, diagnostics and mixed reports fail
  the whole conversion. Lines are never skipped or joined.
- Nonempty opaque date cells and the complete trimmed raw `ST/Ex` cell.
  Dates are ignored. No state enumeration, alias conversion or readiness,
  severity, time-zone or elapsed-time inference occurs. `Run/Ntry` must be
  decimal digits separated by `/`; its values are not interpreted. `Pri/Xit`
  is an optional opaque token. Final padding and an absent `Pri/Xit` may be
  omitted. Other row shapes fail rather than being guessed.
- At least one data row. Empty, blank and header-only input are explicit helper
  support limits. They are never converted into an empty overlay.

These references establish a documented subset, not a vendor machine API or
live-version qualification. The summary contains the last status posted to the
database. The references also warn that read restrictions can silently omit
matching objects, and an ALL/wildcard request with no reports can exit zero.
Cropping or omitted rows that still form a valid supported table cannot be
detected. The converter cannot prove producer flags, exit status, authorization,
atomic capture or whole-estate completeness. Check those separately at capture
time.

## Inspection and provenance

Import, replacement, Clear and Show observations keep the current graph
placement, view, folds, selection and highlights. Only explicit finding actions
navigate. Clear removes the observations. A valid `{"nodes":{}}` is a loaded
empty snapshot, distinct from having no snapshot. Native JSON parsing keeps
the last duplicate key; producers must emit unique keys.

The source graphs are the public synthetic
[Nightbank](../nightbank/README.md) and [fulfilment](../fulfilment/README.md)
examples. The observations were written by hand for these examples. They were
not collected from an engine, scheduler report, database or carrier service.

`viewer.properties` supplies fixed synthetic display text for placeholders.
The paths need not exist. These commands emit offline HTML; they start no jobs,
database or carrier process. Generated HTML is kept outside the repository.
