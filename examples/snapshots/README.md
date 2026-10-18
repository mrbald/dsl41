# Offline snapshot examples

These files attach invented observations to the existing synthetic Nightbank
and fulfilment graphs. The values are independent examples, not a captured run.
A box's supplied state is separate from the observations supplied for its
descendant leaf jobs.

Each sidecar contains only `nodes` and a string `state` for every entry.
No source, timestamp, version or state vocabulary is required. IDs match the
emitted graph exactly. Optional metadata is described in the viewer's Format
section. Replacement drops observations omitted from the new file.

## Nightbank

From the repository root, emit either profile:

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

Open the generated HTML locally. Open Snapshot, choose Import JSON, and select
[nightbank-small.json](nightbank-small.json) or
[nightbank-bank.json](nightbank-bank.json) for the matching profile.
Display starts at Generic. Select AutoSys to see its built-in display aids for
literal codes such as `SU`, `FA` and `RU`. Raw strings remain unchanged.

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

Emit the existing eight-node fulfilment graph:

```sh
uv run dsl41 viz --format explore \
  -p examples/snapshots/viewer.properties \
  -o /tmp/fulfilment-generic-explore.html \
  examples/fulfilment/estate.jil
```

Open the HTML and import [fulfilment-generic.json](fulfilment-generic.json).
Keep Display at Generic. Four of eight catalog nodes have observations.
`FF_WAVE_B` has its own `awaiting handover` value and leaf coverage 3 / 7.
Its supplied leaf values are `stock allocated`, `parcel packed` and
`carrier label pending`, once each. The box value is excluded from that list.
There are no external matches, unknown IDs or ineligible IDs.

These are business observations associated with existing emitted nodes.
The viewer preserves the words as supplied; it does not interpret them as
scheduler states or load a different topology.

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
