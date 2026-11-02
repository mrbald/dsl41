# Call the application and group selection helpers

For maintainers wiring application and group selection into the control plane
and TUI. Follow the maintainer track under [Documents](../README.md#documents).
This page lists the pure preparation interfaces. Runner admission, replay and
transport behavior belong to the [control protocol](control-protocol.md).
These helpers are not wired into commands or widgets and are not independently releasable.

## Resolve one applied catalog

A box declaring application `Pay` and group `Ops` passes both labels to its
children. A child declaring group `Batch` keeps application `Pay` from the box.
Its application origin is the box; its group origin is the child.

Pass the applied catalog's `Mapping[str, JobIR]` to
`resolve_effective_labels(catalog)` in [job_selection.py](../src/dsl41/job_selection.py).
It returns name-keyed `EffectiveLabels`. Each `EffectiveLabel` holds its value
and the name of the job that declared it. An absent label has value and origin
`None`. Resolution uses the shared JIL unquoter and keeps the catalog's
declared attributes unchanged.

**AF decision text (operator ruling):** an empty `application` or `group`
value counts as absent after JIL unquoting. The job inherits that label from
its box. Only a non-empty value overrides inheritance. An empty value on a
root job leaves it unlabelled. The integration slice records this rule in
the AF decision entry.

Application and group inherit independently from the nearest declaring ancestor.
The helper reads their case-insensitive JIL attribute keys from `passthrough`.
It compares label values exactly, with case preserved. It does not build a
registry relating applications to groups. Invalid ancestry raises `ValueError`.

`JobSelectors(application=None, group=None)` omits both selectors.
A supplied empty string selects unlabelled jobs for a query or presentation
filter. Both supplied selectors must match. Wildcards and whitespace have no
special meaning in label selectors.

## Freeze the selection from one read

Call `selection_snapshot(catalog, selectors, *, baseline_id, applied_index,
config_rev, job_revisions)`. The caller supplies all values from one coherent
read. The helper performs no I/O and cannot make separately obtained reads coherent.
`job_revisions` uses job names as keys. Supply revision zero explicitly for a
catalog job that has no runtime row. A selected job with no supplied revision
raises `ValueError`. Store-only ghosts are excluded.

The immutable `SelectionSnapshot` holds the baseline, applied index,
configuration revision, selectors, catalog count and a tuple of `SelectedJob`
values. Each selected job carries its name, parent, effective labels, origins
and revision. `snapshot.targets` gives the ordered names. `snapshot.by_name()`
gives the name-keyed collection for selected queries. An empty query succeeds.
`snapshot.as_dict()` serializes a detached JSON object with the ordered jobs,
labels, origins, selectors and pins. `SelectionSnapshot.from_dict(value)`
reconstructs the immutable value and rejects malformed or unknown fields.
Each job has `name`, `box_name`, `labels` and `state_rev`; labels contain
application and group objects with `value` and `origin`.
`EffectiveLabel`, `EffectiveLabels` and `JobSelectors` expose matching
`as_dict()` and `from_dict(value)` codecs.
The integration caller supplies the full spec values when serializing a selected
`query spec`; these helpers do not replace its existing single-job response.

`ordered_targets(catalog, targets)` returns a tuple in depth-first box order.
Ancestors precede descendants and siblings sort lexically, including roots.
Unselected ancestors still determine their descendants' position. Duplicate or
unknown targets raise `ValueError`. An empty target iterable returns an empty tuple.

`query_selectors(verb, *, job=None, application=None, group=None)` in
[cli_selection.py](../src/dsl41/cli_selection.py) rejects label selectors outside
`status` and `spec`, and rejects their combination with `-J`. The wiring caller
maps `--application/-I` and `--group/-B` to these values.

## Prepare one reusable bulk request

Call `bulk_request(snapshot, *, request_id, verb, arguments,
claimed_actor=None)` in [bulk_selection.py](../src/dsl41/bulk_selection.py).
`BulkVerb` covers the existing job verbs, including `CHANGE_STATUS` and
`KILLJOB`. Shared verb arguments go in `arguments`, without an individual `job`.
CHANGE_STATUS arguments and the KILLJOB override survive unchanged.
The integration caller validates each verb's argument schema.

`BulkRequest` freezes selectors, ordered targets, baseline, configuration
revision, arguments, claimed actor and one `TargetExpectation` per target. Arguments
are stored as canonical JSON; returned argument and wire objects are detached
copies. The request refuses SET_GLOBAL, absent or empty selectors, duplicate
targets, zero targets and incomplete expectations. Revisions must be
non-negative integers, excluding booleans.

`request.fingerprint(actor=...)` returns a SHA-256 digest over selectors,
ordered targets, verb, arguments, baseline, configuration revision, every
expectation and the explicit actor. Admission hashes the authenticated actor
after the server replaces the claimed one under the [access policy](access-model.md).
The actor argument is required; the helper never implicitly hashes the
client's `claimed_actor`. A different client claim with the same authenticated
actor does not change the digest. A different authenticated actor does.
Object key order does not affect it. Request ID and wire version are framing
and do not enter it. `request.as_request(version=...)` builds one
`sendevent_bulk` candidate with a `job:<name>` expectation map.
The integration slice must supply and enforce the AF-capable wire dialect.
No default dialect is chosen here.

`BulkRequest.from_request(value)` decodes an admission input or a printed
preview through the model's validation. It restores the original targets,
arguments, pins and claim without consulting a catalog. It requires exactly
one `job:<name>` expectation for each target and rejects bare-name keys.
An absent `claimed_actor` and an explicit null both decode to `None`.
Expectation tuples are normalized to target order, so JSON object key ordering
does not change request equality or the fingerprint. The wiring must validate
the AF dialect before admission; the codec checks only that its version is positive.

`request.frame(*, version, max_request_bytes)` serializes the complete request
as a JSON line. The limit counts encoded bytes, including the newline.
The caller supplies the transport budget; the existing control stream budget
is defined in [control protocol framing](control-protocol.md#2-transport-and-framing-frozen).
Oversized requests raise `ValueError` without splitting the selection.

`prepare_bulk_request(snapshot, *, request_id, verb, arguments, version,
max_request_bytes, claimed_actor=None, scalar_expect=None, job=None)` in
[cli_selection.py](../src/dsl41/cli_selection.py) rejects a scalar bulk
`--expect` or `-J` and returns `PreparedBulkRequest`.
Print its complete `preview` before sending its `wire_bytes`.
Retain those bytes for an uncertain retry. Decode a saved preview with
`BulkRequest.from_request(json.loads(preview))` when a request model is needed.
Retain the preview's wire version when framing it again.
Do not rebuild the request from a fresh query.
The later transport caller preserves mutation exit codes 0, 2, 3 and 4.

`check_selection(request, current_snapshot)` compares baseline, configuration,
selectors and the complete ordered membership. Resolve the current snapshot
against the applied catalog at admission. `expectation_failures(request,
job_revisions)` returns every missing or stale job pin in target order.
The admission caller runs that check after its normal time half.
An exact retained retry must be answered before either selection check.
These helpers implement no deduplication, transaction, cascade or external effect.
The runner must apply and persist one admitted request atomically and replay
its recorded targets. Selection limits direct targets; cascades can affect others.

## Refresh the presentation filters

`TuiFilters(selectors=JobSelectors(), name_filter="", view_mode="all")` in
[tui_selection.py](../src/dsl41/tui_selection.py) is immutable.
`active_filters` lists the active controls. `clear(control="all")` clears one
of `name`, `application`, `group`, `view`, or all controls. A cleared label is
`None`; an explicit empty label filter still selects unlabelled jobs.

The live TUI uses `matches_row(name, state, labels, filters)` for one status
row, or `filter_rows(states, effective_labels, filters, *, selected_job=None)`
for a full refresh. `states` maps names to `TuiJobState(status, has_alarms=False)`.
`effective_labels` maps every status row's name to precomputed `EffectiveLabels`,
or explicitly `None` for a store-only ghost. The status read supplies these
labels from the shared resolver. These entries need no JobIR catalog or second read.

A ghost stays visible when no application or group selector is active, subject
to the name and view filters. Any application or group selector excludes ghosts,
including an explicitly empty selector. A ghost is not an unlabelled catalog job.

The name filter retains case-insensitive, space-separated substring matching.
Application, group, name and the existing all/problems/active view are ANDed.
Problems include failure, termination, queue wait and active alarms.
Pass the same `filters` after refresh or update to preserve the controls.

`filter_rows` returns a `TuiSelection` with flat, lexical `TuiRow` values, the
same filters, matching and total status-row counts, and the selected job if
still visible. The total includes ghosts. Each returned ghost has `labels=None`.
Collapsed boxes cannot hide a matching row. Filtering changes no action targets.

`filter_jobs(catalog, states, filters, *, selected_job=None)` remains a catalog-only
convenience entry. It resolves labels from JobIR, counts catalog jobs and excludes
ghosts. Missing catalog runtime state is inactive. Live TUI wiring uses the
status-row entries above. Widgets and keyboard/web interaction qualification
remain with the [TUI integration lane](cli-tui-qualification.md#tui-interaction-inventory).

## Check the pure behavior

The synthetic tests are [test_job_selection.py](../tests/test_job_selection.py),
[test_bulk_selection.py](../tests/test_bulk_selection.py) and
[test_tui_selection.py](../tests/test_tui_selection.py).
They cover inheritance, origins, independent overrides, unlabelled jobs, case,
intersection, query/bulk ghost exclusion, TUI ghost visibility, depth-first
prefix-sibling ordering, empty and duplicate targets, request size, explicit
actor hashing, pins, filter clearing and refresh. Property tests vary catalog
and argument insertion order and round-trip snapshots and bulk requests through
JSON while preserving the fingerprint.
They do not qualify admission atomicity, durable replay, authorization or real UI interactions.
