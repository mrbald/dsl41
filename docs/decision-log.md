# Decision log

- DL-01 New repo, dsl42 as quarry; forcing-function-first design (round-trip +
  equivalence as honesty checks) instead of constitution-first.
- DL-02 IR is AutoSys-shaped first; vendor neutrality emerges at Layer G.
- DL-03 Feature order: round-trip fidelity -> linter -> Mermaid -> equivalence
  validator -> DSL (extracted from corpus patterns, not designed up front).
- DL-04 Kept from dsl42 verbatim: pure compiler, no runtime; failed translation is
  a loud classified error, never silent loss.
- DL-05 Statement layer = hand scanner (spec: jil-statement-syntax.md); lark only
  for condition expressions. CFG for raw-to-EOL values is design theater.
- DL-06 Q1 precedence: both candidate grammars ship behind a switch; sentinel test
  proves they differ; losing rule deleted after live verification.
- DL-07 passthrough is strict-by-default: unknown attribute = lowering error unless
  allow-listed as inert (ir-design ss4).
- DL-08 UC backend client generated from /resources/openapi.json (UCS-12), not
  hand-written.
- DL-09 AGPL + commercial dual license; CLA from day one; clean-room corpus policy.
- DL-10 Repo codename dsl41; public product name decided later.
- DL-11 Linter phase readings (lint.py docstring is normative detail): L001
  checks local refs against the catalog and cross-instance refs only for a
  declared xinst (SEM-07 boundary markers are phase-5 material, not dangling
  refs); L002 scopes to $$VAR sites with a textual SET_GLOBAL producer
  heuristic over commands (value() atoms exempt -- runtime SET_GLOBAL from
  outside the catalog is routine); L003/L004 are enforced upstream (grammar/
  model/lowering) and stay registered as defensive scans so the ss9 code
  space holds. CLI exit contract: 0 clean, 1 findings (warn fails only with
  --strict), 2 parse/lowering refusal.
- DL-12 Derive phase readings (derive.py docstring is normative detail):
  n() mutex pairs, never components (exclusion is not transitive); the
  instantaneous mutual-exclusion reading applies ONLY to unqualified local
  n() in `condition` -- a lookback-qualified n() stays an edge (M03) so the
  qualifier is never silently lost. Same-cycle (M01) requires same top-level
  box, or equal trigger cadence with BOTH jobs unboxed -- two identically
  scheduled boxes are two UC workflows (M02), a signature collision is not a
  stream. Box-override membership is transitive (SEM-12 "inside the box"):
  M15 for any descendant, M16 otherwise. Edges whose producer is undefined
  in the catalog are cls=redesign (row M02 context): compiling an A-row edge
  to a nonexistent vertex would be silent loss; L001 carries the error.
  Structural passes run over local condition edges only; chain members
  inside a reported cycle are not double-reported as chains.
- DL-13 Oracle phase readings (oracle.py docstring is normative detail):
  SEM-11 fold gate is literal -- a box cannot complete while any non-bypassed
  member has not run, even if that member's condition is currently false
  (the hung-box pattern is real behavior, not a defect to smooth over).
  Re-evaluation is edge-triggered: a transition/SET_GLOBAL/ON_ICE wakes only
  jobs whose `condition` references the changed entity; completed consumers
  re-run on each fresh satisfaction (self-referencing conditions may loop --
  that is AutoSys's own re-trigger pattern, L010's concern, not the
  oracle's to prevent). A member's own date_conditions schedule gates with
  AND on top of the box gate (SEM-31/L013): members with schedules start
  only on their script-injected ticks. Iced jobs satisfy EVERY atom kind
  (f/t/e included) per SEM-05's blanket wording -- chosen over SEM-20's
  "as though it succeeded" reading; Q6-adjacent, revisit live -- but only
  once not RUNNING (ice on a running job takes effect at completion).
  FORCE_STARTJOB overrides hold and the box-RUNNING gate but never ice
  (SEM-20 "removed from all logic" wins). SEM-15 idle-box recompute: member
  transitions re-derive a non-running box's status (TERMINATED stays sticky
  per SEM-13). Injected STATUS may overwrite terminal states (CHANGE_STATUS
  analog; script-authoring hazard, documented not guarded).
- DL-14 Equivalence phase readings (equiv.py docstring is normative detail):
  tier b enumerates per-job STATE SPACE (status x lookback age buckets x
  Q2 same-day flag x exit-code cutpoints; globals over literal/cutpoint/
  UNSET/OTHER domains), not independent atom booleans -- independent atoms
  cannot see the s(x)&f(x) contradiction L006 exists for. The model
  deliberately decouples status from last exit code: unreachable states
  can only yield false INEQUIVALENCE or a missed warn, never a false
  equivalence claim. The ss6 BDD fallback (dd) is not taken v1 -- state
  spaces past 2^18 report too_large (tier-c only). In the free model every
  condition is falsifiable (all-RUNNING/unset-globals state), so L007
  evaluates box members with unstarted siblings pinned to NEVER_RAN (the
  "at box start" moment); plain tautology is vacuous by construction.
  Rename maps cover job names/box links/condition refs; globals and
  external instances are identity v1. Tier-b graph check compares
  canonical edge multisets + mutex groups + box tree (the v1 stand-in for
  ss6's bisimulation). Tier c excludes `cause` strings from trace
  comparison and applies the rename to catalog A's trace.
- DL-14a Amendment after the phase-8 adversarial review (both findings
  were confirmed false-equivalence claims, violating DL-14's own invariant):
  (1) string-global domains now carry region representatives for BOTH
  comparison behaviors of the oracle ("", lit+NUL string cutpoints plus
  v+-1 numeric cutpoints) -- the old single OTHER token made every ordered
  string comparison vacuously false and declared v(G)<"m" equivalent to
  v(G)>"m"; (2) the state space gained a per-job iced flag (SEM-05 parity)
  so s(x)&f(x) is no longer "equivalent" to s(y)&f(y). L006 deliberately
  keeps the ice-FREE question (icing is intervention, not scheduling) and
  its message says so; conds_equivalent always enumerates ice. L007 models
  box start with the oracle's catalog-order member starts (earlier siblings
  may be NEVER_RAN or RUNNING, later ones NEVER_RAN). too_large is
  inconclusive ("tier-c only"), never DIVERGENT. equiv_scripts covers the
  out-of-band event kinds and runtime-set globals (declared AND referenced,
  literal +- off-literal values); tier c guards rename collisions. Tier b/c
  are schedule-blind by scope -- tier a owns schedules (documented).
- DL-15 UC-backend U3-independent slice (backend_uc.py docstring is
  normative detail): compile_to_uc() raises BlockedOnU3 unconditionally --
  emitting records against a guessed schema would be silent loss with extra
  steps. classify_edges partitions E/A/R per Part II requirement 1; the
  migration report (requirement 3) pins catalog_hash + tool version, is
  deterministic (no timestamps), lists every refused edge with its source
  location, every assumption, M27 flags, M07 mutex groups, M12 OR shapes
  with lowering suggestions, the M33 boundary, and an open-question ledger
  filtered to the U-questions whose M-rows the catalog actually uses.
  `dsl41 report` always exits 0 on a generated report -- the report is the
  loud channel, the linter is the gate.
- DL-16 UC twin (backend_uc.UcModel/compile_twin + uc_oracle.py; docstrings
  are normative detail): compile_twin lowers E/A rows to an in-memory UC
  model (the structure the backend serializes post-U3) -- R rows, M27
  windows, notrunning-via edges, cross-workflow edges (Task Monitor
  territory), and unattachable global gates are excluded and recorded.
  M12 Or gets the NAIVE lowering (branch edges attach to the consumer;
  UC's skip-join makes that OR only for common-ancestor diamonds) --
  divergence is P-M12's point; restructure lowerings are U1-gated. The
  interpreter implements UCS-01/02/03/09/13 with documented U-defaults
  (M31 exit boundary, U8 read-at-evaluation variables); STARTJOB launches
  the containing workflow (one open instance v1); ice = Skip-at-start
  (M19), hold = M20, KILLJOB = Cancelled, FORCE starts within the open
  instance. The comparator drops STARTING (cosmetic AutoSys/UC lifecycle
  difference) and compares RUNNING/terminal/SKIPPED milestones per job;
  P-Mxx pairs assert divergence IS found where the mapping table predicts
  it and convergence holds for faithful shapes (chain, M19 contrast).
- DL-16a Amendment after the UC-twin adversarial review (three MAJORs,
  all confirmed): (1) UcEdgeCondition gained `cancelled` -- UC separates
  Cancelled from Failed (UCS-01/M06), so failure edges no longer fire on
  kills and M04's f() keeps its EXACT class; t() maps to `cancelled`.
  (2) Workflows are addressable by their own (box) name and by nested-box
  aliases (UCS-0 "workflows are themselves tasks"), so AutoSys-style
  STARTJOB(box) scripts drive both engines unchanged. (3) Global gates
  that cannot attach (every predecessor edge already carrying an M08
  var_condition) or attach only to some paths are RECORDED in the
  exclusion ledger, never silently dropped. Also: instance launch records
  INSTANCE->Running so box-named workflows compare cleanly against the
  AutoSys box lifecycle; FORCE_STARTJOB with no open instance launches the
  containing workflow then forces (M22 Launch analog); the comparator
  drops SKIPPED entries when comparing (an explicit UC Skip equals an
  AutoSys never-evaluated job; SKIPPED-vs-ran still diverges, raw payload
  kept for reporting); self-exclusion mutex is documented as subsumed by
  the one-open-instance rule.
- DL-17 DSL phase readings (dsl.py docstring is normative detail): the
  surface is exactly the four D2-named builders (job/box/sequence/
  parallel) plus record declarations, extracted from corpus patterns. The
  builder GENERATES JIL and lowers through the tested pipeline -- no
  second semantics path; values are validated against JIL's line
  discipline and refused loudly. Conditions stay strings in the existing
  condition language; cond_to_source renders Cond trees back with full
  structural fidelity (nested groups parenthesized for the flat parse).
  sequence()/parallel() refuse to merge onto jobs that already carry a
  condition (silent loss); the decompiler emits sequence() only for
  chains whose followers carry exactly s(prev) -- adjacency alone is not
  enough (the corpus's own mutex chain proves it) -- and leaves everything
  else as explicit job(condition=...) calls. The round-trip property
  (decompile -> exec -> canonical-hash equality) holds corpus-wide and is
  the phase's mechanical adversarial check.
- DL-18 Estate-shape hardening after the first real-sample dry run
  (2026-07-09). The failing shapes and the decisions they forced:
  (1) scanner -- insert_resource/update_resource/delete_resource are
  statement boundaries (rule 3 amendment), and an attribute-position key
  shaped like a subcommand ((insert|update|delete|override)_*) that is NOT
  in the recognized set is a scanner ERROR: the observed failure was
  insert_resource silently folding into the preceding insert_machine, and
  statement-boundary loss is silent structural loss. (2) ir -- ResourceIR
  carried opaquely (name + res_type + verbatim attrs), mirroring
  MachineIR's documented opaque-v1 stance; UCS-09/M34 map resources to UC
  Virtual Resources when the backend lands. `status:` on insert lowers to
  Semantics.initial_status restricted to INACTIVE/ON_HOLD/ON_ICE/ON_NOEXEC
  (SEM-24 [A]); anything else -- especially run states, which would
  interact with the SEM-01 latch -- is a loud lowering error.
  alarm_if_terminated joins the annotation class beside alarm_if_fail.
  (3) oracle -- initial_status seeds the SEM-20/21/22 flags before the
  first event, no trace entry (T24a/b). (4) backend_uc -- compile_twin
  records definition-time state in the exclusion ledger rather than
  modeling it v1 (UC "Hold on Start" / M20 is the eventual E-class
  target); the AutoSys-vs-twin comparator therefore diverges on such
  catalogs, which is the correct polarity (divergence surfaces, silent
  agreement never fabricated). (5) dsl -- resource() builder + status=
  kwarg emission keep the corpus-wide decompile round-trip property.
- DL-19 `~{$NAME}~` placeholder resolver as a NON-CORE preprocessor
  (placeholders.py + `dsl41 resolve`, 2026-07-09). Estate JIL is templated
  by an external properties mechanism BEFORE `jil` sees it; we reproduce
  that step standalone so the compiler core never models templating
  (nothing in the core imports the module; the scanner keeps treating
  unresolved tokens as opaque name characters per the DL-18 fixtures).
  Pinned semantics (module docstring is normative detail): properties are
  KEY=VALUE split on the first '=', '#'/'!' comments; references are legal
  in both key and value; resolution is an order-independent fixpoint, so
  use-before-define and nested tokens work; later files override earlier
  by RAW key (layering is the point of 1+ files) while within-file
  duplicates and same-resolved-key collisions are errors; anything still
  `~{...}~`-shaped after substitution -- undefined name or malformed
  lookalike -- is a loud error with file:line, escapable per DL-07
  convention via --permit-unresolved (carried verbatim + reported).
- DL-20 Estate-scale hardening (2026-07-09; root-caused from a field report
  of `dsl41 lint` being OOM-killed). Three defects, one cause each:
  (1) derive computed backward-reachability ancestor sets for EVERY catalog
  job although only the Or-shape pass consumes them -- Theta(n^2) memory on
  chain-shaped estates (741MB at 5k jobs, measured; OOM kill at real estate
  sizes). Ancestors are now computed iteratively and only for Or-branch
  producers: a catalog with no `|` pays nothing, and the iterative closure
  is additionally COMPLETE on cyclic graphs where the old memoized
  recursion returned order-dependent partial sets. (2) the same recursion
  made success-vs-RecursionError depend on declaration order (reverse-
  declared chains crashed) -- gone with the iterative walk. (3) the
  condition Tree->Cond transformer recursed once per operator down the
  LALR left spine, so ~1000-atom flat chains crashed with a RecursionError
  traceback AND exit code 1, which the CLI contract reserves for lint
  findings. The spine walk is iterative now (long flat chains are fine and
  stay n-ary/shallow downstream); genuinely deep GROUPING beyond the
  walker budget is refused as a loud ConditionParseError (exit-2 class)
  because every downstream Cond walker recurses per nesting level --
  admitting the parse would only move the crash.
- DL-21 `resources:` job attribute -- the 11.3+ resource-object job side
  (2026-07-09). Root cause of the gap: the dossier ss5 inventory was NOT
  based on an old spec (it cites TechDocs 12.x) but had parked the whole
  resource/placement class as opaque and only ENUMERATED the legacy
  pre-11.3 load-balancing attributes (job_load/priority/QUE_WAIT), so the
  DL-07 firewall refused `resources:` as unknown; DL-18 had added only the
  definition side (insert_resource statements). Now, verified against
  TechDocs 12.x/24.0: `resources: (name, QUANTITY=n[, FREE=Y|N|A]) AND
  (...)` (FREE: Y=free on success, N=never, A=unconditional; res_type
  D|R|T; amount required; optional agent-level machine). Decisions:
  lowering types each group into JobIR.resources (ResourceRef), keywords
  and the AND separator case-insensitive (estates write lowercase `and`);
  malformed groups, unknown group keywords, non-integer/absent QUANTITY,
  and FREE outside Y/N/A are loud lowering errors; FREE absent stays None
  (the engine default is not guessed); no oracle gate semantics v1
  (Resource Wait/QUE_WAIT out of interpreter scope, dossier ss5 row);
  resource references are NOT validated against catalog.resources
  (mirrors machine refs -- estates split definitions across files); the
  compile twin records requirements in the exclusion ledger on the M34
  row (its target column is already Virtual Resources / UCS-09) rather
  than minting a new M row outside a Part II review; the decompiler
  renders groups canonically so the corpus round-trip holds.
- DL-22 Preprocessing as a first-class CLI step (2026-07-09). Field
  finding: templated values in TYPED lanes (start_times etc.) correctly
  refuse `~{$NAME}~` tokens at lowering -- and per DL-19 the core must
  never learn templating -- so processing a templated estate needs the
  resolve step fused into the workflow, not looser typing. Decisions:
  (1) every catalog-consuming command (lint/equiv/report/decompile/viz)
  accepts --properties/-p and resolves each input before parsing;
  substitution is within-line, so diagnostics keep real file:line
  positions; placeholder failures join the exit-2 class ("input never
  reached the tool"); equiv applies one binding set to both catalogs.
  (2) `dsl41 resolve` accepts several files and concatenates them in
  argument order into one output -- a missing final newline between
  inputs is completed in that input's own newline style, and merging LF
  with CRLF inputs is refused loudly (rule 10 would make the merged text
  unparseable anyway). Typed lanes stay strict on unresolved tokens:
  preprocessing IS the supported path for templated estates.
- DL-23 `dsl41 lint --suppress CODE` (2026-07-09). Field finding: estates
  that carry `timezone:` on every job as a convention drown the report in
  L005. Re-verified first that L005's premise holds for timezone: the
  TechDocs date_conditions page itself lists timezone (plus run_window
  and must_*_times) among the attributes it gates, and secondary sources
  corroborate "date_conditions unset => Days/Time attributes ignored";
  one Q2-adjacent corner (does per-job timezone re-base the lookback-0
  midnight anchor?) is recorded as [?] on SEM-35, not guessed. Decisions:
  suppression is per-CODE and CLI-level only (lint_catalog stays
  complete -- suppression is a reporting choice, not a semantics one);
  suppressed codes drop from both the output and the exit code; unknown
  codes in --suppress are an exit-2 error, because a typo silently
  suppressing nothing would be its own silent loss.
- DL-24 L015 severity split (2026-07-09, field calibration). Estates use
  bare-hours lookbacks (`s(job, 12)` = 12 hours) deliberately; the shape
  is valid, Broadcom-documented, and unambiguous to the ENGINE -- the
  only risk is an author who believed minutes. It is now INFO (printed,
  never gates the exit code, --strict included; ir-design ss9 row
  amended). Single-digit minutes (`2.5` = 2h05m, not two-and-a-half
  hours) stays WARN -- that token genuinely reads as a decimal. Full
  silence for either remains `--suppress L015` (DL-23).
- DL-25 Dangling-name audit (2026-07-09; field report: unknown resource
  references were silent). Every named cross-reference reviewed; the
  catalog-assembly linter is the home for existence checks (lowering
  stays per-file tolerant, DL-21). Already covered: condition job refs +
  undeclared ^INSTANCE (L001 error), box_name (lowering hard error),
  $$VAR substitution sites (L002 error). Gaps closed: (1) L016 warn --
  `resources:` names a resource with no insert_resource in the set (warn
  not error: AutoSys resolves against its DB, but the UC backend cannot
  size the Virtual Resource, M34/UCS-09). (2) L017 warn -- `machine:`
  outside the set's machine records, fired ONLY when the set defines at
  least one machine (job-only slices keep machine defs out of scope by
  convention and stay quiet); comma lists (legacy load-balancing) are
  checked per name; boxes skipped (inert passthrough). The corpus now
  models a complete estate (machines_base.jil). (3) L002 extended to
  v(NAME) condition atoms at WARN, not error -- an unset global read can
  be an INTENDED cross-system gate (sem12's external gate is exactly
  that); the extension immediately surfaced two real dangling v() reads
  the corpus already carried. (4) Calendars are autocal territory, not
  definable in JIL, so "unknown calendar" is undecidable for the linter:
  the migration report inventories referenced calendars per job and adds
  M24 (and M26 for schedule timezones) to the used rows so the U6 parity
  question finally surfaces when calendars are actually in play.
  Documented out of scope: owner/profile (OS/machine-side names), the
  machine-record `machine:` attr (opaque v1, DL-18), watch_file paths.
- DL-26 L007 vacuous-pin false positive (2026-07-09; field report: L007
  fired on a vanilla s(prev) chain inside a box). Root cause in
  cond_truth_profile, not the box-start pinning: rule_l007 pins ALL
  siblings, but a sibling the condition never references is not in the
  condition's truth-table alphabet, so the fixed-status check read None,
  failed the allowed-set test, and skipped EVERY state -- zero states
  enumerated, falsifiable=False by vacuity, every conditioned member of
  any 3+-member box reported as a tautology. The two-member quiet tests
  never had an unreferenced sibling, which is how it survived phase 8's
  review. Fix: a pin on an unreferenced job is vacuous (it cannot affect
  that condition's truth) and is ignored; regression tests pin the chain
  shape quiet and a genuine n(later) tautology still firing beside an
  unreferenced bystander. Also corrected in the same session: the L007
  message's justification -- member conditions ARE re-evaluated
  event-driven during the box run (that is how in-box sequencing works);
  first-evaluation pinning is sound because a member runs at most once
  per box execution (SEM-10), so a condition that cannot be false at
  first evaluation never gets a second evaluation at all.
- DL-27 `rename_job` recognized at the statement layer (2026-07-10; found by
  the Broadcom 12.x doc sweep, not a field report). TechDocs 12.0.01+
  documents `rename_job` ("renames an existing job and updates
  dependencies"); its `rename_` verb was outside the DL-18 guard shape, so
  the scanner silently folded the statement -- and everything after it --
  into the preceding statement: the exact failure class DL-18 exists to
  stop, reintroduced by an incomplete verb list. Fix: `rename_job` added to
  SUBCOMMANDS, `rename` added to the guard verbs, lowering keeps refusing
  it loudly (rename is merge semantics like update/delete/override, out of
  compile scope). Lesson recorded in the spec: the guard's verb list is
  part of the subcommand inventory it protects; re-check it against the
  vendor subcommand page whenever the recognized set changes. The
  companion new-name attribute is carried generically (its exact name is
  immaterial to scanning and unverified against the page body).
- DL-28 insert_xinst plumbing carried opaquely (2026-07-10; 12.x doc sweep).
  TechDocs 12.1 documents six insert_xinst attributes -- xtype, xmachine
  (required in all cases), xport (required for xtype a/e), xmanager
  (required for xtype e), optional xcrypt_type/xkey_to_manager. The v1
  lowering refused everything except xtype, so no documented-valid
  external-instance JIL could lower at all, blocking SEM-07 cross-instance
  estates. Resolution: `XinstIR` (name, typed xtype, verbatim attrs, span),
  the exact MachineIR/ResourceIR boundary-record stance -- xtype is the one
  field conditions/L001 depend on; connection plumbing is the engine's
  concern. Required-ness of xmachine/xport is NOT enforced (lowering stays
  per-file tolerant, DL-21/DL-25 line); an L-rule can add that check if a
  field report ever asks for it. DSL builder/decompiler grew **attrs
  passthrough to match.
- DL-29 Full 12.1 subcommand inventory at the statement layer (2026-07-10;
  doc sweep). TechDocs 12.1 documents monbro (insert/update/delete),
  job_type objects (insert/update/delete), delete_blob, insert/delete_glob,
  and connectionprofile (insert/update/delete) subcommands the scanner did
  not recognize. They match the DL-18 guard shape, so the failure was loud
  -- but a loud stop on a VALID estate file means F1 fidelity is impossible
  over input the engine accepts. Resolution: the scanner recognizes the
  complete documented inventory (statement boundaries, byte-faithful
  round-trip); lowering refuses the out-of-scope object classes with the
  classified error. Scan-everything/lower-selectively is the layering DL-05
  always intended.
- DL-30 Rule 4b: one attribute pair per attribute line, loudly (2026-07-10;
  doc sweep). Broadcom's JIL syntax rules permit several `attr: value`
  statements on one line and require value colons to be escaped (`\:`) or
  quoted. The scanner took everything after the first colon as value, so a
  legal second pair was swallowed silently -- invisible to DL-07 because it
  hides inside another attribute's value (e.g. `machine: prod priority: 5`
  lowers as machine="prod priority: 5"). Resolution: the rule-4 inline-pair
  detector now also runs over attribute values; a whitespace-preceded,
  unescaped, unquoted `key:`-shaped token is a scanner error naming the fix
  (split the line / escape / quote). On valid JIL this costs nothing (the
  DL-18 argument): valid values never contain that shape. F4's
  "key:-lookalikes are value text" pin narrows to non-pair shapes (path
  colons, digit-led times, quoted/escaped text) -- fixtures kept proving
  those; autorep -q emits one attribute per line, so estate exports are
  unaffected.
- DL-31 Mid-line `#` is value text (2026-07-10; doc sweep). Broadcom
  defines `#` line comments "in the first column" and lists `#` among
  valid name AND value characters; the scanner's whitespace-preceded
  `#`-to-EOL trailing-comment strip therefore silently changed values
  relative to the engine's parse (`command: run.sh # nightly` lowered as
  "run.sh"). Resolution: `#` comments are full-line only (first
  non-whitespace character; leading indentation accepted as harmless
  leniency); a mid-line `#` stays in the value. Trailing comments remain
  supported via `/* ... */` only. Preserve-mode rendering was never
  affected (text kept verbatim); this changes the TYPED lane. Residual [?]
  in rule 5: live-jil behavior for indented and mid-line `#` -- flip back
  deliberately if a live check ever contradicts the doc.
- DL-32 12.x attribute lanes completed for CMD/BOX/FW scope (2026-07-10;
  doc sweep). Attributes TechDocs 12.x documents as valid on the three
  in-scope job types were DL-07 hard errors. Routed by semantics class:
  ANNOTATION (observability, no control flow): heartbeat_interval
  (MISSING_HEARTBEAT alarm) + the notification-services family
  (notification_alarm_types/_template/_emailaddress_on_*). PASSTHROUGH
  (inert carry): machine_method (joins job_load/priority placement row),
  job_class, avg_runtime, ulimit, elevated, interactive, and chk_files --
  chk_files has teeth (unmet space -> alarm, no start) but is Resource-Wait
  class like `resources` (DL-21): typed/oracle treatment deferred until a
  consumer exists. TYPED (ExecSpec, CMD-only): std_in_file (stdin redirect,
  may name a blob) and envvars (NAME=value list) -- verbatim values,
  $$VAR-indexed (SEM-08); error on FW like command, inert on BOX like the
  base exec cluster. Extended-job-type attribute families (ftp_*, i5_*,
  hadoop_*, oracle_*, ps_*, j2ee_*, ...) stay out: those job_types are
  refused at lowering, so their attributes are unreachable -- no allow-list
  entries for unreachable semantics. SEM-24 upgraded [A]->[V] (existence)
  in the same sweep: TechDocs 12.0.01 documents `status:` on insert with
  the no-update/override constraint; full documented value set still [?].
- DL-33 success_codes / fail_codes complete the SEM-09 boundary (2026-07-10;
  doc sweep). TechDocs documents both on Command/i5/Micro Focus/z/OS jobs:
  comma lists of codes and lo-hi ranges; absence-defaults "0 is success" /
  "non-zero is failure". Both were DL-07 errors, and SEM-09 modeled the
  boundary as max_exit_success alone -- the one true semantics gap the
  sweep found. Resolution: typed Semantics fields (sorted ranges, never
  merged; CMD-only via model validator -- a box's verdict is the SEM-11
  fold); ONE verdict function `ir.exit_is_success` shared by the oracle
  and the UC twin (M31's same-boundary assumption now holds by
  construction, U4 unchanged); compile_twin exports the sets. The docs do
  NOT state the composition -- that is the new Q7 (dossier ss9), pinned to
  the conservative direction (never invent a SUCCESS): fail_codes wins;
  a present success_codes replaces the success rule (unmatched -> FAILURE,
  threshold ignored); fail_codes alone falls through to the threshold.
  Trace tests T09b/c/d pin the corners; replace defaults only from a live
  instance, per the Q-discipline.
- DL-34 Accepted leniencies vs the 12.x syntax pages (2026-07-10; doc
  sweep, deliberately NOT changed -- relitigate here, not in code review):
  (1) grammar JOB_NAME/INSTANCE_NAME accept @/$ beyond the documented
  object-name charset [a-zA-Z0-9._#-] -- superset reading of migration
  input, harmless; (2) documented `\,` escaped commas in values are not
  honored by list splitting -- only bites list attrs whose member names
  contain commas (calendars), none observed; (3) the 4096-character
  statement limit is unenforced -- matters only if canonical output is fed
  back to a real jil binary, lint-candidate if that becomes a flow;
  (4) `machine` is doc-required on FW jobs but lowering does not require
  it -- requiredness is the engine's concern (DL-28 line), lint-candidate.
  Also recorded: Broadcom's own lookback example writes the word `AND`
  inside a condition, vindicating the grammar's word-operator support
  (grammars/condition.lark already accepts and/or case-insensitively).
- DL-34a Adversarial-review addendum (2026-07-10). Two findings from the
  post-sweep Opus review: (1) FIXED -- rule 4b fired on a `key:` shape
  inside a retained closed inline block comment (rule 5 keeps those as
  opaque value text); the 4b scan now masks closed `/*...*/` spans first.
  (2) ACCEPTED-LATENT -- the DSL decompiler emits record attrs as builder
  kwargs, so a machine/resource/xinst attribute literally named `name`
  would collide with the positional param when the decompiled module is
  executed; pre-existing pattern shared by all three builders, no
  documented JIL attribute has that name, and the failure is a loud
  TypeError. Fix if it ever fires: dict-splat fallback in decompile.
- DL-35 Viz emits a Markdown report, not a bare Mermaid body (2026-07-10;
  UI/UX + graph-layout consult). Motivations: one monolithic dagre chart of
  a whole estate is unreadable; triggers and locks were visually silent;
  admin-wrapper singleton jobs are noise. Decisions, each pinned by a test:
  (1) `dsl41 viz` renders per-component charts inside one Markdown document
  (summary line, folded legend, appendices); `to_mermaid` stays the public
  single-chart function. (2) Component connectivity = dependency edges
  between catalog jobs + box co-membership; mutex links do NOT connect
  (a shared lock would glue unrelated streams -- cross-component pairs get
  a "Shared locks" section); pseudo-sources replicate per component.
  (3) Standalone jobs (size-1 component, no edges, no mutex membership --
  structural rule, no command-text sniffing) are dropped from charts but
  enumerated in Appendix A with kind/schedule/command; reversible via
  --include-singletons. Loud, enumerated loss per the DL-07/DL-12 spirit.
  (4) Visual grammar: shape/line-style primary, color redundant (dark-mode
  `color:` on every classDef), Unicode symbols not FontAwesome (hosts do
  not ship FA CSS). FW jobs = stadium+page symbol; schedule digests as a
  second label line (trigger fields only, mirroring _trigger_signature);
  externals move [[..]] -> hexagon so the subroutine shape is exclusively
  collapsed boxes; undefined producers gain a warning-sign prefix.
  (5) Edge-label thinning: via letter only when != success, lookback raw
  always, mapping row only on redesign edges (+ red linkStyle); assumed
  rows/assumptions move to Appendix B. (6) Mutex: pairs stay pairwise
  x-. lock .-x; a COMPLETE clique >= 3 renders as one shared lock hub
  (completeness checked -- the hub never claims an unstated exclusion);
  self-mutex is a label badge, not a self-loop. (7) DerivedGraph gains
  node_meta (kind, trigger digest, command/watch detail) carried verbatim
  from IR-F -- display facts, no analysis; ir-design ss5 sketch amended.
  (8) ELK layout stays opt-in (--elk): GitHub/GitLab do not register
  Mermaid's ELK package (2026-07 check); VS Code >= 1.121 renders it.
  Graphviz/D2/hand-rolled SVG backends rejected for now: dot is the
  fallback if a real estate defeats dagre after the split (revisit here).
- DL-35a Adversarial-review addendum (2026-07-10). Findings from the Opus
  review of the DL-35 landing, all fixed same-day: (1) BLOCKER -- a mutex
  pair naming an undefined job (unqualified n(ghost); derive's M07 detector
  has no catalog-membership check, and L001 owns the loud finding) crashed
  to_markdown with a KeyError in the shared-locks table. Undefined lock
  members now render as undefined pseudo-nodes in their partner's chart,
  scoped like edge pseudo-sources. (2) MAJOR, silent loss -- a mutex pair
  (or self-lock badge) wholly inside a collapsed box was drawn nowhere and
  enumerated nowhere. The cross-component-only "Shared locks" section is
  replaced by an unconditional "Locks" section listing EVERY mutex group
  with kind and chart ids ("not in catalog" for dangling members), same
  pattern as Appendix B for non-exact edges. (3) MINOR -- <br/> in subgraph
  TITLES renders inconsistently across hosts; expanded-box titles are now
  one-line with middle-dot separators (node labels keep <br/>).
  (4) MINOR -- collapse hid FW/schedule facts with no fallback; collapsed
  labels now count hidden scheduled jobs and watchers. (5) NIT -- viz --out
  now write_bytes like render's -o (exact line endings). Accepted as-is:
  markdown metacharacters other than pipes/backticks are not escaped in
  table cells and headings (documented JIL name charset is markdown-safe);
  _mutex_plan assumes derive's sorted/deduped pairs (sole producer).
- DL-36 Calendar exports accepted; L018 dangling-calendar rule (2026-07-10;
  field report: calendar definitions passed as a separate file exited 2
  with "attribute line 'extended_calendar' before any statement", and
  unknown calendars were never detected). Vendor verification (TechDocs
  12.1): calendars are NOT jil subcommands -- DL-29's inventory was correct
  -- they are managed by autocal_asc, whose -E/-I text format carries three
  statement kinds: `calendar:` (bare date rows), `cycle:` (start_date/
  end_date), `extended_calendar:` (rule attributes: workday, non_workday,
  holiday, holcal, cyccal, adjust, condition). Decisions: (1) the scanner
  accepts the three export verbs as statement boundaries (rule 11) rather
  than growing a second scanner -- the format is JIL-shaped except
  standard-calendar date rows, and a parallel scanner would duplicate the
  trivia/fidelity machinery; F1/F2 hold over calendar exports. Date rows
  are verbatim statement body; an attribute after a date row is a loud
  error (re-render would reorder). No documented JIL attribute shares the
  three names, so boundary recognition costs nothing on valid JIL (the
  DL-18 argument). (2) CalendarIR/CycleIR carried opaquely (MachineIR
  precedent, DL-18): generating dates from extended rules is autocal's
  semantics (U6/M24 parity), not this compiler's; standard + extended
  share one namespace (run_calendar cannot disambiguate), cycles get their
  own. Names are unquoted at lowering so they compare equal to unquoted
  run_calendar refs. (3) This retires DL-25's "unknown calendar is
  undecidable" clause: once the set carries any calendar/cycle definition,
  existence IS decidable -- L018 (warn) checks job run_calendar/
  exclude_calendar plus extended-calendar holcal/cyccal, gated on >= 1
  definition in the set exactly like L017's machine convention (job-only
  slices stay quiet). DL-25's M24/M26 report rows are unchanged, and the
  migration report's calendar inventory now states per row whether the set
  carries a definition (kind, or NO DEFINITION). Follow-up candidate, not
  done: validating date-row shapes against the -f date_format inventory
  (formats vary; verbatim carry is the honest v1).
- DL-37 Decompiler completeness + parallel() emission (2026-07-10; design
  review before first estate use, plus a field requirement: parallel boxes
  with >10 same-producer members exist at least twice in the target
  estate). Findings and decisions: (1) BLOCKER, silent loss -- _job_kwargs
  predated the DL-32/DL-33 doc sweep and dropped success_codes, fail_codes,
  std_in_file, envvars on decompile; no corpus fixture carried them, so the
  corpus-wide round-trip test was blind. Fixed; kitchen_sink.jil now
  witnesses every decompiler-visible typed lane the corpus lacked, keeping
  the round-trip guard honest against future model growth. (2) The
  decompiler now emits parallel() (the module docstring promised it; DL-17
  had recorded sequence()-only): fan-out = >= 2 jobs whose entire condition
  is exactly s(p) for one in-catalog producer p, grouped by exact condition
  shape rather than derive's (preds, succs) signatures -- extra outgoing
  edges do not disqualify a member, and any looser incoming shape stays an
  explicit job(condition=...); fan-in = the unique job whose condition is
  exactly the conjunction of the members' plain successes (zero or
  ambiguous candidates stay explicit). Disjointness with sequence() is
  structural, not filtered: a fan-out member gives p >= 2 successors, so
  derive's single-successor chain linkage can never claim it. (3) decompile
  --check (default on): the CLI executes the emitted module and verifies
  canonical-hash equality on the user's actual catalog, turning any
  residual decompiler gap into exit 1 with tier-a detail instead of a
  silently lossy module; the module is still emitted for inspection.
  Annotations sit outside the hash (ss6 softer tier) and are the check's
  documented blind spot. (4) The emitted module ends with an
  `if __name__ == "__main__"` footer printing to_jil(), so
  `python module.py > rebuilt.jil` + `dsl41 equiv` is the whole iterate-
  and-diff loop; section comments (records/jobs/wiring) make regeneration
  diffs readable. (5) Calendar names with spaces (TechDocs' own example)
  are quoted on emission and the calendar builders accept them; record
  builders take `name` positional-only and attr keys colliding with Python
  keywords or `name` emit through a **{} splat, so opaque-record attrs can
  never produce a module that fails to compile; a standard-calendar attr
  literally named `dates` is refused loudly (would bind the builder
  parameter; no such attr exists in the export format).
- DL-37a Adversarial-review addendum (2026-07-10). The Opus review of the
  DL-37 landing confirmed the decompiler logic (parallel/sequence
  disjointness held in both grammar modes across ~20 adversarial
  catalogs, including a cyclic join-is-producer case) and found three
  fixable gaps, all in the --check error path, all fixed same-day:
  (1) MAJOR -- the CLI ran the check's exec BEFORE emitting the module,
  and neither the exec nor decompile() itself was guarded, so a module
  the builder refuses to execute (e.g. a lowered calendar name with outer
  spaces, legal in IR but not calendar-name-shaped) died as an uncaught
  traceback with NO module written -- the exact opposite of the DL-37
  item-3 contract. The module is now emitted before the check; an
  exec-time exception reports cleanly and exits 1; a decompile-time
  DslError (the calendar-'dates' refusal) is a clean exit-2 refusal.
  (2) MINOR -- the no-tier-a-detail fallback message blamed annotations,
  which are hash-EXEMPT and can never reach that branch; the branch was
  real for resources/external instances, which catalog_hash covered but
  tier (a) did not diff (the ss8 short-circuit and tier (a) disagreed,
  the same defect class DL-36 fixed for calendar spans). Tier (a) now
  diffs resources (res_type+attrs) and external instances (xtype+attrs)
  like machines, and the fallback message no longer names a suspect.
  (3) MINOR -- machine_type=""/res_type="" were dropped by truthiness
  guards in decompile (now `is not None`); --check had made the loss
  loud, with an accurate message for machines and, pre-fix-2, the
  misleading fallback for resources. Separately, the test agent's corpus
  completeness sweep (the DL-37 structural guard, first run) reported six
  unwitnessed decompiler-visible fields -- FwSpec owner/profile/
  std_out_file/std_err_file and box_terminator/job_terminator -- all now
  witnessed in kitchen_sink.jil; the sweep's skip-list is expected to
  stay empty.
- DL-38 Closed fold registry: T-001..T-007, opt-out, composition (2026-07-10;
  design debate on decompiler transform scope before first estate use).
  Every decompiler transform beyond verbatim emission is a CLOSED, coded
  set (dsl.FOLDS), each derivable from graph shape or typed lanes alone --
  no naming or domain knowledge. Estate idioms (a receive-file quintuple,
  etc.) are NOT built-ins; they wait for the custom-pattern door
  (recognizer + verify-by-expansion, designed in-session, unbuilt).
  Decisions: (1) Fold detection runs on RESIDUAL conditions -- T-005 strips
  symmetric top-level bare n() pairs first -- so folds COMPOSE: the
  corpus's own mutex chain (`n(mutex_a) & s(mutex_feeder)`) now folds as
  sequence() + mutex(). The emitted wiring order (sequences/parallels,
  then mutex) re-conjoins; mutex() parenthesizes the existing condition so
  both Q1 grammar modes preserve the tree, and canonical conjunct sorting
  is what makes conjoin order irrelevant to the hash gate. Stripping never
  invalidates derive's chains (bare n() contributes no edges, M07).
  (2) T-002 splits chains into maximal same-letter runs; run heads keep
  their own condition, so the emission model needed nothing new. NO length
  threshold: sugar is hash-neutral and thresholds destabilize regeneration
  diffs. Every disqualified link is reported with a reason (lookback/Q2,
  cross-instance/M33, exit-code atom, compound) -- the explicit-links
  worklist is the migration audit trail. (3) T-004 admits uniform f/d/t
  links via sequence(link=)/parallel(on=); joins stay s-based. (4) T-005
  decompile detection is STRICTER than derive's M07: top-level And
  conjuncts only, symmetric only, non-self only; one-way, nested, or
  self n() stays an explicit condition. mutex() composes with existing
  conditions BY DESIGN (conjoining is its declared operation, not a silent
  merge) and marks its jobs conditioned, so chain builders refuse them
  afterward -- wire chains first. (5) T-006 folds only whole-lane-identical
  single-group resources into contend() (partial merge would have to
  reproduce group order -- exactly the ambiguity contend() refuses); it
  makes contention VISIBLE with no mutex semantics claim (capacity lives
  in opaque ResourceIR attrs; QUE_WAIT out of v1 scope, DL-21). (6) T-007
  factors emission-identical schedule blocks into shared module-level
  dicts with content-derived deterministic names -- pure Python factoring,
  no new DSL surface. (7) T-003 mirrors DL-37's and-join as
  parallel(then_any=): unique or-join over exactly the member set, zero
  or ambiguous stays explicit. CLI: --no-fold (comma-separated codes,
  unknown refused, exit 2), `dsl41 folds` lists the registry, fold
  inventory + diagnostics on stderr. Additions to the set require a DL
  entry; "basic-looking" estate-relative shapes do not qualify.
- DL-38a Adversarial-review addendum (2026-07-10). The Opus review of the
  DL-38 landing confirmed the fold machinery with NO defects found -- ~70
  hand-built adversarial catalogs (composition collisions incl. joins that
  are mutex partners, boxes as producers/partners/chain members, three-way
  cliques, residual-enabled chains, ambiguous joins) plus 12,500+ fuzzed
  catalogs, each checked for canonical-hash equality, module exec-ability,
  and determinism under default + random --no-fold subsets, in both Q1
  grammar modes. Two observations pinned here: (1) Paren-wrapped joins
  never fold, BY DESIGN: _plain_success_combo requires a bare top-level
  And/Or, so `(s(a) | s(b))` written with explicit outer parens stays an
  explicit job(condition=...) even where the bare form would fold to
  parallel(then_any=). Consequence of Paren-node fidelity retention, and
  asymmetric under T-005 stripping (a stripped top-level And re-flattens
  and can fold; a retained Paren(Or) residual cannot). Both directions
  round-trip; the conservatism stands -- folding through Paren would trade
  fidelity structure for sugar. (2) LATENT, PRE-EXISTING, outside DL-38
  scope, confirmed and flagged for its own fix: escaped-colon job names
  never participate in derive edges or fold detection, because the scanner
  keeps the backslash in the catalog job KEY (`alpha\:one`) while
  condition lowering unescapes atom names (`alpha:one`) -- key and atom
  can never match. Not silent loss (everything stays explicit and
  round-trips verbatim), but cross-references on colon-named jobs are
  invisible to derive/lint/viz/folds until the name normalization is
  unified in the ast_jil/conditions layer. Separately, the test-suite
  landing added corpus witnesses for all seven folds (trigger +
  non-trigger each) and closed two regression gaps the mutation check
  exposed: _link_verdict's lookback disqualification and T-005's
  lookback-n() exclusion had no failing test before; the new fixtures
  legitimately grew the whole-corpus derive edge count (18 -> 36) and made
  the U1 open-question ledger fire through a genuine M12 OR-join shape.
- DL-39 Job-name identity: semantic (unescaped) everywhere in IR
  (2026-07-10; fix for DL-38a observation 2). The scanner preserved `\:`
  verbatim in subjects and box_name values while the condition transformer
  unescaped references, so colon-named jobs never joined: no derive edges,
  no mutex pairs, no box linkage, no folds -- not silent loss (everything
  stayed explicit and round-tripped verbatim), but the semantic layer was
  blind to those references. Decision: rule 7's discipline ("semantic
  unquoting happens at lowering") now covers the `\:` escape for the
  JOB-NAME lane. conditions.unescape_job_name/escape_job_name are the ONE
  owner pair of surface<->semantic transcoding: lowering funnels insert_job
  subjects and box_name values through the same unescape the condition
  transformer applies, and every JIL-emitting path (builder subjects and
  box_name lines, cond_to_source references, the sequence/parallel/mutex
  wiring strings) escapes on the way out. Both estate spellings -- raw
  `a:b` subject (legal value text: rule 4b only flags whitespace-preceded
  key-shaped colons) and vendor-canonical `a\:b` -- converge on the same
  catalog key. escape/unescape are exact inverses (escape inserts one
  backslash per colon, unescape removes exactly one), so identity holds
  even for pathological backslash runs; a name with a backslash-adjacent
  colon cannot enter via parsing (the JOB_NAME token admits `\` only as
  `\:`), and hand-built ones fail loudly at reparse. Scope is deliberately
  the job-name lane ONLY: machine, resource, xinst, calendar, and global
  names stay verbatim on BOTH sides (their reference lanes never
  unescaped, so they were and remain self-consistent); whether the engine
  unescapes `\:` inside general values (command, std_*_file) is unknown --
  verbatim carry stands until a live instance answers it (rule 2
  amendment, [?] marker). Witness: names_colon_join.jil exercises the
  whole lane (keys, box tree, edges, mutex fold, decompile round trip);
  corpus pins updated deliberately (37 edges, M01 13, L012 3, 6 viz
  subgraphs).
- DL-40 xhigh-review fixes: wiring name gate, fold-gating contract,
  worklist completeness (2026-07-11; 21 verified findings, all confirmed,
  none refuted). Decisions: (1) Names the wiring builders interpolate into
  GENERATED condition atoms must be carryable by the grammar's JOB_NAME
  token -- colon is the only escapable metachar (DL-39); whitespace,
  `( ) , ^ & |`, and backslash are refused loudly (_check_wirable) at
  sequence()/parallel()/mutex() for interpolated positions ONLY: a
  metachar name may still END a chain or fan out (it only receives a
  condition). Before, mutex("J^2", b) silently emitted n(J^2) -- a
  cross-instance reference to a DIFFERENT job (M33), a no-silent-loss
  violation. (2) The statement lane is wider than the condition lane:
  subjects the lowerer accepts but job() refuses (embedded whitespace)
  make decompile REFUSE upfront (exit 2) instead of emitting a module
  that raises at execution -- the T-006 resource-name gate applied to the
  job-name lane. (3) _conjoin_condition splits statements on newline
  ONLY: \x0b/\x0c/\x85/U+2028 are legal value bytes (the scanner delimits
  on \n alone) and splitlines() rewrote them into real newlines,
  silently truncating values. _CTRL_RE stays [\r\n\x00] -- builder and
  pipeline agree on what a line is. (4) The FOLDS dependency note is now
  ENFORCED, not aspirational: disabling T-001 keeps every fan-in join
  (then= AND then_any=) explicit, including on T-004 f/d/t groups, which
  still fold join-less. (5) --no-fold is a repeatable list option
  (comma-separated values still accepted); the scalar form silently kept
  only the last flag. decompile(disable=) also accepts a bare code string
  (a str IS a Collection[str]; iterating it char-wise produced a
  gibberish refusal). (6) passthrough=/annotations= refuse `condition`
  and `resources` keys: verbatim lines would bypass the _declared/
  _resourced registries the no-merge guards read. parallel(after='')
  is refused like every other undeclared name (falsy-vs-None check).
  (7) The stays-explicit worklist is COMPLETE: chain-link verdicts only
  covered links inside derived chains; every other job whose residual
  condition survives the folds (fan-out hangers-on, singleton groups,
  ambiguous joins, chain heads, disabled lanes) now gets a note in
  DL-38's reason vocabulary (_explicit_notes). (8) Structure per
  CLAUDE.md style: decompile()'s seven inline passes extracted into
  small pure functions (_fold_mutex/_fold_chains/_fold_fanout/
  _fold_schedules/_fold_contends), join detection precomputed in one
  O(N) shape pass (was O(groups x N)), builder statement lookup through
  a name->index map (was O(N) scan per wiring call), T-005 target sets
  built once. Test-suite gap closed: the U-question ledger regained its
  negative gate (a question whose M-rows the catalog never uses stays
  OUT of the report).
- DL-41 Runner (phase 11): prod-grade single-node executor as a sans-IO
  shell over the oracle (2026-07-11; design frozen in
  docs/runner-design.md; user resolved E1=prod grade, E2=both clocks,
  E3=web behind proxy/tunnel). Decisions: (1) The oracle is the ONLY
  semantics authority. The runner adds effects, wall time, durability,
  and a control surface -- never semantics. Emitted STATUS(STARTING) is
  the dispatch instruction; completions are injected as STATUS with raw
  exit_code only (SEM-09/DL-33 verdict stays oracle-side); KILLJOB and
  term_run_time kills are the oracle's decisions, the shell's pgid
  signal. Adapters implement NO retries (Q4 parity -- a shell-side retry
  would fork semantics from the simulator) and NO timeouts. (2) Two
  oracle additions only: next_timer_due() and advance(now), factored
  from feed()'s lazy timer drain so a wall-clock shell can sleep until
  the next due timer; bisimulation pins feed-only vs advance+feed
  equivalence. (3) Prod grade (E1) = WAL journal: inputs-only JSONL
  (emitted events/trace replay from oracle determinism -- one source of
  truth) + dispatch records (pgid, run_number) + fsync-before-feed;
  resume = catalog-hash gate (refuse silent semantic drift), replay,
  then reconcile orphaned RUNNING jobs by killing recorded pgids and
  injecting TERMINATED "orphaned by runner restart" (adoption = E4,
  future). (4) Stale-completion gate lives in the SHELL: injected
  STATUS may legally overwrite terminal statuses (CHANGE_STATUS
  parity), so the engine drops-and-journals completions whose
  run_number mismatches or whose job is already terminal -- closes the
  natural-exit vs KILLJOB race. (5) The runner owns the calendar the
  oracle deliberately lacks: a scheduler injects STARTJOB at
  days_of_week + start_times/start_mins ticks (zoneinfo timezones),
  firing unconditionally -- SEM-32 abandonment (Q3) and SEM-33
  run_window stay oracle-side. run_calendar/exclude_calendar refused
  (definitions unmodeled). (6) Preflight extends the backend_uc R/A
  discipline to execution: ERROR on non-{CMD,BOX,FW} job_type,
  non-local machine, foreign owner, custom calendars, unresolvable
  timezone; WARN on n_retrys (runs without, Q4), job_load/priority
  (no resource manager), and AND-success-skeleton cycles -- cycles are
  LEGAL AutoSys (DL-13 edge-triggering, L010), so graphlib is bounded
  to that warning plus the acyclic-only `plan` view, never the engine.
  (7) Two time domains (E2), one engine path: run = RealClock + real
  adapters + control socket; rehearse = VirtualClock + FakeAdapter +
  scenario, exits at quiescence -- rehearsal is evidence because the
  code path is identical. (8) UI split is FORCED, not stylistic:
  textual-serve spawns one app instance per browser session, so the
  engine is a daemon behind a unix-socket control plane (sendevent
  parity verbs, explain-with-atom-truth, subscribe streaming journal
  records) and the Textual app is a thin client -- same app in the
  terminal and served to the web (E3: no auth in textual-serve; deploy
  behind reverse proxy/SSH tunnel, documented not built). textual is an
  optional [ui] extra; the engine stays on the existing three runtime
  deps. (9) Acceptance gate = bisimulation: every SEM trace test
  parametrized over Oracle-direct vs Engine(VirtualClock, FakeAdapter)
  with identical traces -- equiv tier c between simulator and executor
  over the whole existing corpus; that suite is 11a's definition of
  done. (10) Flat house layout (runner.py + runner_tui.py), CLI verbs
  run/rehearse/sendevent/serve/journal; phases 11a-11e (engine+bisim,
  real adapters+journal+crash tests, scheduler+preflight+control,
  TUI, serve). New open questions E4 (orphan adoption), E5 (profile
  failure semantics [?]), E6 (FW steady-size + default interval [?]);
  no new switches for inherited Q3/Q4.
- DL-41a Lifecycle amendment: per-run wrapper shim + supervisor tier;
  E4 dissolved, E7 opened (2026-07-11; user-driven design session on
  orphan lifecycle; user proposed the env-tag identification trick and
  the dumb-supervisor tier and asked for a codex (GPT-5.6) adversarial
  consult, which found one real bug and several hardenings; two claims
  settled empirically on the dev machine). Decisions: (1) The durability
  primitive is a per-run WRAPPER shim (runner_wrapper.py, stdlib-only,
  parent-agnostic; containerd-shim/slurmstepd/HTCondor-starter pattern):
  wait() is Unix's single-shot status channel, so the one process that
  cannot miss the observation writes status.json durably — exit status
  now survives arbitrary engine downtime, the gap the env tag could
  never close. spawn.json is likewise written by the process that
  spawns, closing the spawn-vs-journal crash window; the engine's
  dispatch journal record demotes to audit/ordering. (2) Codex-found
  bug, fixed in design: the wrapper must sit OUTSIDE the pgid it
  signals — kill(-pgid, SIGKILL) would kill the recorder before it
  records. wrapper setsid(); command setpgid(0,0); signals target the
  command pgid only. (3) Parent-death detection is the inherited
  lifeline pipe (EOF fires even on -9; kernel closes fds), with a hard
  fd-hygiene invariant — the write end lives in exactly ONE process,
  leak test ships in 11b; PR_SET_PDEATHSIG is Linux-only belt-and-braces
  (thread-tied, exec-cleared), never primary. On wakeup the wrapper
  checks child-exit BEFORE lifeline EOF (a completion racing parent
  death must record as completion); waitid(WNOWAIT) observes before
  reaping so the observe-to-record hole shrinks to a few syscalls.
  (4) Durability liturgy on every record: same-dir temp, fsync(file),
  rename, fsync(dir); runs dir fsync'd at creation; run_dir must be a
  local filesystem (NFS rename ambiguity). (5) PID-reuse guard pivots
  from env-tag to (pid, start-time) verification: KERN_PROCARGS2 env is
  unreadable for restricted binaries like /bin/sh on stock macOS
  (empirical probe: 32-byte stub; XNU source confirms), /proc/environ is
  ptrace-gated initial-env; ps -o lstart= works unprivileged for
  arbitrary pids (verified, 1s resolution, +/-2s tolerance; Linux
  starttime is tick-exact). DSL41_RUN env tag stays as forensics only;
  encryption rejected (same-uid threat model: peers can already
  ptrace/kill; uuid run_id covers collision). (6) Reconciliation is now
  mostly READING: tethered engine death makes wrappers kill-and-record,
  so resume follows a ladder — settle for live wrappers, inject real
  completions from status.json (late injection at max(ended_at, last
  journal at), true time in payload), kill verified survivors of a dead
  wrapper (TERMINATED, truthful), else E7. (7) NEW E7: unobservable exit
  status maps to FAILURE cause exit_status_unobservable, never
  TERMINATED (reserved for kills that happened) and never anything
  satisfying success-dependent downstreams; f()-recovery is the common
  estate path. (8) E4 RESOLVED by architecture, not solved as posed:
  non-child adoption never happens; the 11f supervisor (dumb
  postmaster/s6-style, SPAWN/SIGNAL/LIST/SHUTDOWN over socketpair,
  Linux subreaper hardening) keeps parenthood alive across engine
  restarts so survival is reattachment. Tethered 11a-11e is a documented
  semantic choice; per codex (and accepted): detached is table stakes
  for long-running prod estates, so 11f is part of the prod-grade story,
  not optional. (9) Containment honesty: pgid kill misses setsid/
  double-fork escapees (vendor agents share this); documented Linux
  hardening is per-run transient systemd scopes (cgroup kill), future
  --scope option, not MVP. kqueue NOTE_EXIT/pidfd are live-monitoring
  aids only — registration dies with the watcher; files are the truth
  across restarts. Residual accepted matrix: -9 of a wrapper alone or
  of a whole tree at once -> detected at resume, reported truthfully
  (E7), never guessed.
- DL-42 Lifecycle tier spin-off: extract-on-trigger, not now (2026-07-11;
  user proposed repo+pipeline+package immediately and asked both
  reviewers to push back; Claude and codex (GPT-5.6) independently
  converged on the same verdict). Decisions: (1) The niche is REAL but
  small: no existing package is an embeddable run-to-completion
  process-lifecycle recorder for scheduler builders (supervisord/circus/
  pm2 are service supervisors; pueue/tsp/nq are user-facing queues;
  systemd-run transient units are the strongest competitor but
  Linux-only; containerd-shim/slurmstepd/HTCondor starter are the
  architectural comparables and are all embedded, not reusable; tini is
  the adoption precedent for tiny-dumb-correct process tools). The
  package's value would be the crash semantics, not the socket — and
  publishing before the failure matrix is implemented and dogfooded
  freezes API promises around the least-informed version of the design.
  (2) Extraction TRIGGER (any of): a second real consumer appears; an
  external adopter is ready to integrate against the protocol; the
  executor has run real workloads through the tier for a while; or the
  AGPL parent materially blocks adoption. Counter-fence: if the tier's
  scope ever grows toward queueing/scheduling/web-UI/auth/policy, it
  stops being extractable as "the lifecycle shim" — scope creep kills
  the spin-off, not enables it. (3) Until the trigger: flat house
  modules (runner_wrapper.py, runner_supervisor.py) under an ENFORCED
  boundary — stdlib-only imports, nothing from dsl41, import-graph
  test — with the socket protocol + spool format (spawn.json/
  status.json) frozen in docs/supervisor-protocol.md as the future
  public API. Deviation from codex's subdir-with-own-pyproject
  recommendation, deliberately: pre-extraction the dsl41 wheel must
  ship these modules, and a two-package monorepo buys packaging
  friction without more isolation than the import test already proves.
  (4) Supervisor socket is a NAMED unix socket (0600 + peer-cred) with
  a versioned protocol and a single-controller LEASE (controller_id,
  expiry, fencing token; mutations carry token + idempotency key;
  observers unlimited; CLI read-only by default) — v1 correctness, not
  ceremony: racing SPAWN/SIGNAL from engine+TUI+script corrupts
  scheduler semantics before it is a security issue. The engine's OWN
  socket keeps no lease: sendevent is multi-writer by AutoSys nature
  and the single-writer engine loop serializes it. (5) spawn.json
  gains boot_id (kern.bootsessionuuid / /proc/.../boot_id): reboot
  recycles the (pid, start-time) identity space; mismatch voids
  liveness AND proves nothing survived (reconciliation shortcut).
  (6) Scope fence for UI: the dashboard of meaning (conditions, boxes,
  explain) is dsl41's; the tier ships at most a JSON CLI + read-only
  top; "free dashboard via textual-serve" is free as a demo only —
  auth/audit/history/redaction are orchestrator concerns. (7) License
  earmark recorded in LICENSING.md item 6: Apache-2.0 on extraction
  (patent grant; GPLv3-family compatible; AGPL-depends-on-permissive is
  the safe direction), no per-file headers meanwhile, no external
  contributions to earmarked files before CLA + relicense disclosure.
  (8) 11b/11f test plan expanded with the phase-boundary kill matrix
  (before/after spawn.json, post-fork pre-exec, post-wait pre-write,
  post-write pre-reap, ENOSPC, stale socket, spoofed spawn.json,
  boot_id flip).
- DL-43 Phase 11a landed: engine determinism pins (2026-07-11; found and
  decided during implementation, all within DL-41's frame; runner.py's
  module docstring is normative detail). Decisions: (1) The engine's event
  queue is TIME-ORDERED -- (at, arrival seq) heap, not FIFO. Found as a
  bug: pre-injected script events carry future timestamps while adapter
  completions enqueue at the processed frontier, so FIFO feeds a
  later-stamped external ahead of an earlier completion and trips the
  oracle's non-decreasing-feed guard. Arrival seq keeps same-instant
  ordering deterministic: an injected event beats the completion that
  enqueues after it. (2) Oracle.advance(now) adopts "the clock reached
  now": _now advances to `now` even when no timer fires, so a later
  feed/advance before it errors -- the same discipline feed() applies,
  extended to idle time. (3) Under VirtualClock the natural-exit vs kill
  race always resolves to the kill (a terminal decision cancels the
  adapter task in the gap between sleep resolution and completion
  enqueue), so the DL-41 stale-completion gate is structurally
  unreachable through honest virtual flows; it guards the real time
  domain (11b) and is white-box tested in 11a. (4) FakeAdapter grows an
  INERT mode (default=None: park forever on a datetime.max sleep) --
  the bisimulation suite runs it so the SEM scripts keep driving
  completions themselves, exactly as oracle-direct; instant-success
  stays the constructor default per the design. (5) Quiescence is
  decidable via a settle contract: under VirtualClock adapters may block
  only through ctx.clock.sleep_until, so "every live task is done or
  holds a pending sleep" (live == pending) means nothing can move
  without the clock; RealClock (11b) sidesteps settling by blocking on
  real IO. (6) The ss13 bisimulation gate is realized as an autouse
  parametrized fixture in test_oracle.py: all SEM trace tests run twice
  (Oracle-direct vs Engine via tests/bisim_harness.py) with zero test
  rewrites; the harness caps the virtual clock at each event's timestamp
  (horizon = ev.at) so the engine never runs ahead of the script --
  matching the oracle's lazy timer discipline by construction.
  Post-review amendments (same day; xhigh adversarial review confirmed
  four bisimulation breaks + one fail-loud violation, all fixed and
  regression-pinned in test_runner.py ss5): (7) GHOST-RUN GATE: dispatch
  spawns only on an oracle-DECIDED start, recognized by the run_number
  bump every real start performs -- an injected CHANGE_STATUS-parity
  STARTING overwrite re-emits STARTING without bumping and launches
  nothing (vendor parity: sendevent CHANGE_STATUS rewrites the DB
  status, no process). (8) FRONTIER RULE: a timer due at or before the
  already-processed instant stays lazy until the horizon moves time
  past that instant, then fires back-dated to its due time via
  advance(frontier) -- zero-delta deadlines (term_run_time 0) match the
  oracle's post-feed state, and past-due timers (negative offsets lower
  fine today; possible future lint) no longer trip advance()'s
  backwards-time check. (9) ZENO GUARD: a condition cycle over instant
  completions generates unbounded work at one frozen virtual instant
  (the L010 tight-loop compressed to zero duration); the engine refuses
  with EngineError after a catalog-scaled same-instant event budget
  instead of hanging -- a shell-level refusal, never a semantics
  verdict. (10) FAIL-LOUD CANCELLATION: cancelled adapter tasks move to
  a reaping list _settle collects; anything a task dies with other than
  the cancellation itself re-raises (shutdown inspects its gather
  results the same way) -- no silent loss on teardown paths. (11) The
  stale gate checks precede clock movement, so a dropped completion is
  fully inert (no time advance, no sleeper wakes; in the 11b real
  domain the single-writer loop must not sleep toward a bogus timestamp
  it will discard). Test-gate hardening from the same review: the
  feed-only arm of the advance-parity property flushes tail timers by
  FEEDING (not advancing), emitted-event parity compares the full
  model_dump including `at`, the engine-bisim vocabulary covers
  non-terminal injected statuses, harnesses close per hypothesis
  example, teardown survives a failing close, and a meta-test enforces
  that every SEM test routes through the oracle() helper (the gate
  cannot silently shrink).
- DL-44 Phase 11b landed: lifecycle tier + WAL + resume (2026-07-11; found
  and decided during implementation, all within DL-41a/DL-42's frame;
  runner.py's 11b docstring block and runner_wrapper.py's docstring are
  normative detail; spool format frozen in docs/supervisor-protocol.md).
  Decisions: (1) SIG_IGN INHERITS ACROSS EXEC -- found by the 11b smoke,
  not review: the wrapper ignores TERM/INT/HUP/QUIT to protect the
  recorder, and without a child-side pre-exec reset to SIG_DFL the
  command (via non-interactive sh) silently ignores the graceful SIGTERM
  and every kill escalates to SIGKILL; regression-pinned. (2) The wrapper
  is spawned BY FILE PATH (sys.executable <runner_wrapper.py>), never
  `-m`: -m imports the dsl41 package __init__ and drags pydantic into the
  recorder's runtime, hollowing the DL-42 stdlib-only boundary; the
  import test parses the AST against sys.stdlib_module_names. (3) Adapter
  results widen from int to int | Terminated | Failed: a wrapper-observed
  signal death or parent-loss kill maps to STATUS TERMINATED (DL-41a item
  7 reserves it for kills that happened) IDENTICALLY live and at resume
  -- one _outcome_from_status shared by both paths so they can never
  diverge; spawn_failed and the E7 absence map to STATUS FAILURE with
  cause. Raw exit codes stay ints (SEM-09 oracle-side). (4) New wrapper
  status outcome spawn_failed(error): /bin/sh unspawnable is OBSERVED,
  not unobservable, and must not masquerade as E7. On a spawn.json write
  failure (ENOSPC) the wrapper kills what it started, still attempts a
  status record, and exits 3 -- running unrecorded is refused. (5) Engine
  real-domain time basis is NAIVE UTC (RealClock): DST must never run
  feed()'s non-decreasing discipline backwards; wrapper records aware-UTC
  ISO and the engine normalizes. Resume additionally refuses a journal
  whose last timestamp exceeds wall-now (machine clock stepped back).
  (6) Journal dispatch records carry wrapper_pid + run_dir, NOT the ss7
  sketch's pgid: the engine never observes the pgid (the wrapper's child
  sets it); spawn.json is the authoritative spawn record per DL-41a's
  demotion. read_journal drops a torn FINAL line (write-ahead: the feed
  it preceded never ran) and refuses interior corruption. Timer advances
  are not journaled -- the DL-43 advance-parity property is what makes
  inputs-only replay converge. (7) Resume never re-executes work: a start
  with no spool trace (crash between feed and spawn) resolves FAILURE
  "dispatch lost to engine crash", distinct cause from E7's
  exit_status_unobservable -- both route f()-recovery, neither can
  satisfy s(); re-dispatch was rejected (double-run risk on an invariant
  we cannot re-verify at resume). EXCEPTION: FW watchers re-dispatch
  (polling is an idempotent read). (8) Reconciliation completions pass
  the ss4 stale gate like adapter completions: if replay already reached
  a terminal state (term_run_time TERMINATED fired during replay), the
  late real record is dropped AND journaled -- CHANGE_STATUS parity is
  for operators, not for the ladder. (9) The kill matrix is realized via
  a wrapper self-SIGSTOP test hook (DSL41_WRAPPER_TEST_PAUSE; inert in
  production): before/after spawn.json, post-wait pre-status, post-status
  pre-reap, settle-window release, spoofed spawn.json (innocent pid never
  signaled), boot_id flip (liveness voided despite a matching token).
  DL-42's "post-fork pre-exec" boundary is deliberately folded into
  post_spawn_pre_record: recovery semantics depend only on "command pid
  exists, spawn.json does not". The engine-SIGKILL integration test also
  proves the lifeline fd-hygiene invariant through the real adapter path
  (two concurrent wrappers both EOF). (10) start_run refuses a run_root
  that already holds a journal (resume or re-baseline, never silently
  overwrite); resume refuses a clock-domain flip. CLI verb `journal`
  (render-by-replay) ships in 11b since the WAL does; run/sendevent stay
  11c per DL-41.
  Post-review amendments (same day; Opus adversarial review, one confirmed
  BLOCKER + five minors fixed, eleven hunt areas confirmed sound):
  (11) BLOCKER B1: an advance()-fired term_run_time TERMINATED was
  journaled nowhere, and a command that traps SIGTERM and exits 0 leaves
  an exited/0 spool record; on resume, replay left the job RUNNING with
  the timer merely re-armed, the stale gate passed at pop time, and
  feed() fired the timer THEN applied the record -- CHANGE_STATUS-parity
  overwrite resurrected a killed job as SUCCESS, cascading to downstream
  s() jobs. Fixed with BOTH halves: (a) the input alphabet gains time
  observations -- an `advance` journal record (shared seq, WAL-first)
  written before every Oracle.advance, replayed by replay_inputs, so
  advance-fired kills survive crashes; the ss7 inputs-only principle now
  reads "external events plus time observations". (b) Kill-wins gate
  ordering: before gating a completion the engine advances the oracle to
  the completion's timestamp (firing exactly the timers feed() would fire
  anyway), so the gate SEES every kill decision and drops-and-journals
  the late natural exit; the DL-43 item 11 "gate precedes clock movement"
  pin narrows to the ENGINE clock -- a dropped completion still moves no
  wall/virtual time and wakes no sleeper. Bisim-invisible: the gate only
  guards engine-made completions and the harness runs an inert adapter.
  (12) NEW E8 (review M2): an EXTERNAL signal death (engine alive, no
  oracle decision) maps to TERMINATED per DL-41a's recorded-signal
  reading, but no SEM entry supports it and real AutoSys may mark
  FAILURE; pinned as an open question (# PENDING: E8 in
  _outcome_from_status), needs a live instance. (13) Review M3: malformed
  status records (exited without integer exit_code; signaled without
  signal) map to truthful causes -- FAILURE "malformed status record" /
  TERMINATED "killed by signal (unrecorded)" -- never a false verdict.
  (14) Review M4: an incomplete FW run at resume with no FW adapter
  registered is a loud EngineError, not a silently-hanging RUNNING job;
  non-FW types without an adapter row keep live-engine parity (no row =
  nothing dispatches) and are left untouched. (15) Review M5: start_run
  fsyncs run_root after creating the journal -- the WAL's directory entry
  is a record too. (16) Review M6: an engine-side wrapper-spawn glitch
  (EMFILE/ENOMEM, or the wrapper dying while reading its spec -- pre-spawn
  by construction) fails THAT job with FAILURE "wrapper spawn failed",
  symmetric with the wrapper's own spawn_failed outcome, instead of
  crashing the whole engine loop. Review M1 (advance-fired alarms absent
  from replay/render) is subsumed by (11a). Confirmed sound by the same
  review: real-loop wakeup races (no await between queue read and
  activity clear), double-cancel tether backstop, wrapper fd hazards,
  superseded-run skip, dotted job names in the runs/ sweep, box-member
  run_number reconciliation, resumed-journal seq/header handling, ghost
  gate seeding, FW single-completion, (pid, start-time) token parsing on
  both platforms, catalog-hash order sensitivity (a real oracle-cascade
  tie-break, so reorder => re-baseline is correct), and the 11a
  bisimulation surface.
