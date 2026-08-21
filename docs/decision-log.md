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
- DL-18 Estate-shape hardening (2026-07-09). The failing shapes and the decisions they forced:
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
- DL-20 Estate-scale hardening (2026-07-09; `dsl41 lint` OOM-killed
  on a large chain-shaped catalog). Three defects, one cause each:
  (1) derive computed backward-reachability ancestor sets for EVERY catalog
  job although only the Or-shape pass consumes them -- Theta(n^2) memory on
  chain-shaped estates (741MB at 5k jobs, measured; OOM kill at estate
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
- DL-22 Preprocessing as a first-class CLI step (2026-07-09). Templated
  values in TYPED lanes (start_times etc.) correctly
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
- DL-23 `dsl41 lint --suppress CODE` (2026-07-09). Estates
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
- DL-24 L015 severity split (2026-07-09). Estates use
  bare-hours lookbacks (`s(job, 12)` = 12 hours) deliberately; the shape
  is valid, Broadcom-documented, and unambiguous to the ENGINE -- the
  only risk is an author who believed minutes. It is now INFO (printed,
  never gates the exit code, --strict included; ir-design ss9 row
  amended). Single-digit minutes (`2.5` = 2h05m, not two-and-a-half
  hours) stays WARN -- that token genuinely reads as a decimal. Full
  silence for either remains `--suppress L015` (DL-23).
- DL-25 Dangling-name audit (2026-07-09; unknown resource
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
- DL-26 L007 vacuous-pin false positive (2026-07-09; L007
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
  unreferenced bystander. Also corrected: the L007
  message's justification -- member conditions ARE re-evaluated
  event-driven during the box run (that is how in-box sequencing works);
  first-evaluation pinning is sound because a member runs at most once
  per box execution (SEM-10), so a condition that cannot be false at
  first evaluation never gets a second evaluation at all.
- DL-27 `rename_job` recognized at the statement layer (2026-07-10; found by
  the Broadcom 12.x doc sweep). TechDocs 12.0.01+
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
  need ever surfaces. DSL builder/decompiler grew **attrs
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
  post-sweep review: (1) FIXED -- rule 4b fired on a `key:` shape
  inside a retained closed inline block comment (rule 5 keeps those as
  opaque value text); the 4b scan now masks closed `/*...*/` spans first.
  (2) ACCEPTED-LATENT -- the DSL decompiler emits record attrs as builder
  kwargs, so a machine/resource/xinst attribute literally named `name`
  would collide with the positional param when the decompiled module is
  executed; pre-existing pattern shared by all three builders, no
  documented JIL attribute has that name, and the failure is a loud
  TypeError. Fix if it ever fires: dict-splat fallback in decompile.
- DL-35 Viz emits a Markdown report, not a bare Mermaid body (2026-07-10). Motivations: one monolithic dagre chart of
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
  fallback if a large estate defeats dagre after the split (revisit here).
- DL-35a Adversarial-review addendum (2026-07-10). Findings from the
  review of the DL-35 landing, all fixed: (1) BLOCKER -- a mutex
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
  calendar definitions passed as a separate file exited 2
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
  review; parallel boxes with >10 same-producer members occur in real
  estates). Findings and decisions: (1) BLOCKER, silent loss -- _job_kwargs
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
- DL-37a Adversarial-review addendum (2026-07-10). The review of the
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
  misleading fallback for resources. Separately, the corpus
  completeness sweep (the DL-37 structural guard, first run) reported six
  unwitnessed decompiler-visible fields -- FwSpec owner/profile/
  std_out_file/std_err_file and box_terminator/job_terminator -- all now
  witnessed in kitchen_sink.jil; the sweep's skip-list is expected to
  stay empty.
- DL-38 Closed fold registry: T-001..T-007, opt-out, composition (2026-07-10;
  decompiler transform scope).
  Every decompiler transform beyond verbatim emission is a CLOSED, coded
  set (dsl.FOLDS), each derivable from graph shape or typed lanes alone --
  no naming or domain knowledge. Estate idioms (a receive-file quintuple,
  etc.) are NOT built-ins; they wait for the custom-pattern door
  (recognizer + verify-by-expansion, designed, unbuilt).
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
- DL-38a Adversarial-review addendum (2026-07-10). The review of the
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
- DL-40 Review fixes: wiring name gate, fold-gating contract,
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
  docs/runner-design.md; resolved E1=prod grade, E2=both clocks,
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
  E4 dissolved, E7 opened (2026-07-11; the adversarial
  consult on orphan lifecycle found one real bug and several hardenings;
  two claims settled empirically). Decisions: (1) The durability
  primitive is a per-run WRAPPER shim (runner_wrapper.py, stdlib-only,
  parent-agnostic; containerd-shim/slurmstepd/HTCondor-starter pattern):
  wait() is Unix's single-shot status channel, so the one process that
  cannot miss the observation writes status.json durably — exit status
  now survives arbitrary engine downtime, the gap the env tag could
  never close. spawn.json is likewise written by the process that
  spawns, closing the spawn-vs-journal crash window; the engine's
  dispatch journal record demotes to audit/ordering. (2) Review-found
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
  semantic choice; detached is table stakes
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
  immediate repo+pipeline+package extraction was considered; two
  independent reviews converged on the same verdict). Decisions: (1) The niche is REAL but
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
  public API. The subdir-with-own-pyproject
  alternative rejected deliberately: pre-extraction the dsl41 wheel must
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
  Post-review amendments (adversarial review confirmed
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
  Post-review amendments (adversarial review: one confirmed
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
- DL-45 Phase 11c landed: scheduler + preflight + control socket + headless
  CLI (2026-07-11; found and decided during implementation, all within
  DL-41's frame; runner.py's 11c docstring block is normative detail).
  Decisions: (1) ENGINE COMMIT DISCIPLINE -- found by 11c design review,
  latent since 11b: the real-domain loop journaled an advance record and
  then slept UNINTERRUPTIBLY until the timer's instant, so an adapter
  completion (or, now, a control injection) stamped inside that sleep fed
  behind the already-advanced oracle clock and crashed the engine ("feed
  time went backwards"). Fixed structurally: the real domain commits to
  work -- advance journal, scheduler pop, event feed -- only once its
  instant is due (<= now); future work routes to the interruptible wait
  and re-plans on activity. Virtual jumps never yield mid-move, so every
  11a determinism pin is byte-identical; regression-pinned with a
  mid-wait completion + pending timer test. (2) Scheduler ticks are
  INPUTS: STARTJOB stamped at the tick, enqueued like any external event
  (journal-first at feed, source=scheduler); the tick pops before any
  same-or-later-due timer/event commits, so heap order alone keeps feeds
  non-decreasing -- no timestamp clamping, vendor-parity stamps. A
  live-but-stalled engine fires its backlog late but truthfully stamped;
  ticks missed across DOWNTIME are dropped-and-journaled at resume
  (WAL drop records, Engine.drops), never fired late (NEW E9). Resume
  re-anchors the scheduler strictly after the last journal instant --
  replay already fed every journaled tick. (3) Schedule interpretation
  defaults pinned (NEW E10): absent days_of_week = every day; per-job
  timezone else run-level --timezone else UTC (vendor: server zone);
  DST via PEP 495 fold=0, with calendar-date iteration (aware-datetime
  arithmetic can skip a 25h local date) and per-day UTC sorting (a
  fold-0 nonexistent time can land past a later tick inside a
  spring-forward gap). SEM-33 run_window and SLA-only schedule blocks
  trigger nothing; SEM-32/Q3 abandonment stays oracle-side (ss5 fires
  unconditionally). (4) Preflight per ss8, codes stable kebab keys
  (job-type/machine/owner/calendar/timezone/oracle ERROR;
  n-retrys/resources/skeleton-cycle WARN); machine/owner identity rules
  are EXECUTION-scoped and skipped for rehearse (FakeAdapter runs
  nothing; calendars/timezone/oracle still gate -- the scheduler depends
  on them). WARN journals as a new `preflight` record kind ("prints,
  journals, and runs" made literal); replay ignores it. The AND-success
  skeleton (s() atoms reachable through AND/Paren spines only -- an s()
  under OR is an alternative, not a dependency; instance-qualified refs
  skipped) is shared by the cycle WARN and `plan`, so they can never
  disagree. (5) Control socket per ss10: 0600 from birth (umask at
  bind), JSON lines; job arguments validated against the catalog (vendor
  sendevent errors on unknown jobs); CHANGE_STATUS = STATUS injection
  with overwrite parity; explain renders per-atom truth through the
  ORACLE's own predicate evaluation (_cond_true: ice bypass, lookback,
  instances -- never a reimplementation). Stale socket handling: a probe
  connect that fails means a crashed run's leftover (unlink and claim);
  one that succeeds means a live engine (refuse - the ss13 "stale control
  socket" case). subscribe streams journal records via a post-write
  fan-out on the Journal; seq'd records are exactly-once across the
  backfill/live seam, unsequenced dispatch/drop at-least-once in the
  race window (documented; the TUI consumes idempotently). (6) CLI verbs
  run/rehearse/sendevent/query: rehearse ships in 11c, not 11d/e -- its
  ss9 quiescence ("no occurrence within the horizon") needs the
  scheduler; scenario files reuse the oracle event shape plus a
  FakeAdapter script block. `query` is a small addition beyond DL-41's
  verb list (the headless autorep analog); the 11d TUI consumes the same
  ss10 protocol. hold_open keeps a real-domain run serving the socket at
  quiescence instead of returning (run mode is stopped by signal, exit 0;
  engine crash exits 1; preflight/gate refusals exit 2).
  Post-review amendments (adversarial review: two confirmed
  BLOCKERs + one major + six minors fixed, one minor pinned as doc; twelve
  hunt areas confirmed sound incl. brute-forced DST math over 10 zones):
  (7) BLOCKER B1: ControlServer.close() awaited Server.wait_closed()
  BEFORE cancelling handler tasks; since 3.12 wait_closed blocks until
  every handler finishes, and a subscribe handler parks on queue.get()
  until cancelled -- any attached viewer deadlocked the engine's whole
  shutdown path (SIGTERM hang, journal never closed). Fixed: close, cancel
  handlers, gather, THEN wait_closed. (8) BLOCKER B2: resume re-anchored
  the scheduler strictly after last_at, assuming "a tick exactly there was
  replayed" -- false for several jobs sharing one tick instant: a crash
  between the siblings' input appends left the unjournaled sibling
  neither replayed, nor re-armed, nor E9-dropped (silent loss). Fixed:
  re-anchor INCLUSIVE, dedup the sweep against the journal's own
  (job, at) scheduler ticks, drop-and-journal the unjournaled remainder.
  (9) Review M3: the run-level --timezone was validated only inside
  Scheduler.__init__ (raw ZoneInfoNotFoundError traceback, wrong exit
  class); run/rehearse now check it up front and exit 2. (10) Review M4:
  subscribe sampled its live/backfill seam AFTER the ack write (a yield),
  so a record landing mid-ack was skipped as covered -- sample before
  sending. (11) Review M5: a query-handler exception killed the
  connection with no response (client timeout); _respond is now shielded
  and answers ok:false. Remaining minors: FQDN accepted as local
  (getfqdn added, M6), empty days_of_week refused comprehensibly at
  Scheduler construction (M7), signal-handler registration guarded for
  non-main-thread embedding (M8), a bind race between two probing engines
  now refuses as EngineError/exit-2 (M9), and the E9 downtime/live
  boundary is pinned to the resume-sweep instant (M10, doc). Confirmed
  sound: commit-discipline paths (no advance-then-yield, no backwards
  feed), kill-wins gate with the new branches, take_sched non-spin,
  quiescence/horizon edges, journal fan-out reentrancy, payload
  aliasing, old-reader tolerance of preflight/drop records, protocol
  validation, ghost-run gate with scheduler starts, plan/skeleton
  determinism.
  Test-suite amendments (63 tests in test_runner_scheduler.py/test_runner_control.py, two
  findings): (12) T2, real bug predating 11c: run_until_quiescent's
  real-domain "target > horizon: return" shortcut abandoned LIVE adapter
  tasks whose completions carry no due timestamp -- a fast completion
  inside the horizon was never processed when the only KNOWN due instant
  (a term_run_time timer) lay beyond it. Fixed: with live tasks the loop
  waits out the horizon interruptibly and returns at the horizon;
  regression-pinned together with the commit-discipline crash in one
  test. (13) T1: the ss8 "oracle construction failure" ERROR rule is
  currently ARMOR -- Oracle.__init__ has no raise site of its own, so no
  catalog passing CatalogIR validation can trigger it. The rule stays
  (ss8 mandates it; construction refusals may arrive with future SEM
  work); one test pins the no-raise reality (fails loudly the day
  construction refusals appear), another pins the plumbing by injection
  (a constructor refusal surfaces as a preflight ERROR, never a crash).
- DL-46 Phase 11d landed: Textual TUI (2026-07-11; found and decided during
  implementation, all within DL-41 item 8's frame; runner.py's 11d docstring
  block and runner_tui.py's module docstring are normative detail).
  Decisions: (1) NO NEW PROTOCOL VERB: the ss11 views are served by the
  existing ss10 surface -- status, trace --since, explain, sendevent,
  subscribe -- plus two read-only fields the status response grows per job:
  `pending_timers` (from the new Oracle.pending_timers(), whose liveness
  filter MIRRORS _dispatch_timer_check's fire-time staleness rules --
  display truth is the dispatch truth: a heap entry a fire would discard
  as stale is not shown as pending) and `log_out`/`log_err` (the ss6
  append targets of the current run, resolved by job_log_paths(), the one
  resolver the adapter's wrapper spec also uses -- the log tail reads what
  the wrapper writes, the two can never diverge; CMD-only, and a
  never-started job reports only explicit std files). Headless `query
  status` consumers get both fields for free. (2) SUBSCRIBE IS A WAKE-UP
  SIGNAL, NEVER A RENDER SOURCE: DL-45's at-least-once dispatch/drop
  caveat is discharged by construction -- every record only schedules a
  coalesced refresh, and everything the user sees comes from the
  idempotent queries (jobs table from status; running commentary from
  trace, cut by stable trace seqs; explain pane from the oracle's own
  predicate truth). A 2s poll backstops a lost subscription; the
  subscription reconnects while the socket is down; a journal-less run
  drops to polling alone. (3) Alarm counts are TALLIED FROM TRACE
  transitions (MUST_START_ALARM/MUST_COMPLETE_ALARM): the oracle's trace
  is the only alarm authority, the TUI counts and never re-derives.
  (4) LOG TAIL IS A LOCAL FILE READ, deliberately outside the protocol:
  in both postures the app runs on the engine host (terminal;
  textual-serve serves FROM the host, E3), so a log verb would duplicate
  the filesystem for no reach. Byte tail (seed last 8 KiB, follow
  appends, reset on truncation) -- smoke-grade by design. (5) CLI: new
  `ui` verb (attach to a RUNNING engine; quitting detaches and leaves the
  run alone) vs `run --ui` (the terminal owns the run; quitting the app
  stops it -- tethered semantics made visible); guarded textual import
  fails with exit 2 and the pip-install hint BEFORE the engine starts.
  (6) Event console accepts exactly the sendevent verbs (omitted job =
  selected row); the SERVER is the only validator (vendor parity: unknown
  jobs/statuses are refused by ss10, the TUI never pre-judges) -- one
  deliberate parser wrinkle: CHANGE_STATUS's status-first shorthand
  means a catalog job named like a status (case-insensitively -- the
  shorthand upper()s its candidate) must use the explicit
  `CHANGE_STATUS <job> <STATUS>` form. (7) textual pinned >=8 as the
  [ui] extra -- the floor is the family the TUI is developed and tested
  against (8.2.8); textual also joins the dev extra so the ss13.6 pilot
  smoke runs in CI; the core package keeps its three runtime deps.
  Post-review amendments (adversarial review: one confirmed
  BLOCKER + one major + six minors; the blocker and the desync fix
  independently reproduced and validated): (8) BLOCKER B1:
  ControlClient.request caught only OSError, so a CANCELLED exchange (the
  exclusive explain worker superseded by fast row navigation, cancelled
  between write and readline) left the response unread on the live
  connection -- the next request read the stale line and every reply after
  it was offset by one (explain pane showing the wrong job; status/trace
  reading explain-shaped payloads; table frozen). Fixed: any non-clean exit
  of the write->read section drops the connection (BaseException guard);
  _drop() itself detaches before its awaits so a re-delivered cancellation
  cannot leave a half-dead connection looking attached. (9) MAJOR M1: the
  [ui] floor said >=1.0 but add_columns' (label, key) tuple form only
  exists since textual 6.2.0 -- a conforming older install crashed on the
  second refresh (CellDoesNotExist). Fixed twice over: per-column
  add_column(key=) (predates 1.0) AND the honest >=8 floor in item 7 --
  unverified compatibility claims are how UIs break at install time.
  (10) M2: run --ui swallowed a TUI crash as an operator stop (exit 0, no
  traceback); the ui task's exception is now retrieved, reported as "TUI
  failed", exit 1. (11) M3: a torn subscribe record raised JSONDecodeError
  past both retry guards and permanently killed the change-feed worker
  (silent drop to poll-only); malformed lines are now skipped -- the feed
  is only a wake-up signal. (12) M4 REJECTED WITH REASON: pending
  run_window timers are NOT filtered by current status -- unlike the
  deadline checks, whose run-mismatch staleness is permanent, a deferred
  STARTJOB is a real start attempt whose outcome depends on fire-time
  state (a job RUNNING now may have completed by next_open and be legally
  restarted); hiding it would suppress a timer that can still act.
  Comment pinned at the oracle site. (13) M5/M6 documented, not coded:
  relative std_* paths tail correctly only under run --ui (DL-39 verbatim
  carry; `dsl41 ui` from another cwd resolves them against the viewer);
  no client-side timeouts by design (a wedged engine parks the data
  plane, quit stays live). (14) Viewer reattach across a re-baselined run
  root (delete + fresh start on the same socket path): a trace shorter
  than the cut resets the commentary cursor and the alarm tally instead
  of suppressing output until the fresh trace outgrows the stale seq.
  Test-suite amendments (38 tests across test_runner_tui.py (new: parser, ControlClient against a
  real server, five pilot smokes), test_runner_control.py (+5: the DL-46
  status fields), test_runner.py (+6: pending_timers liveness; placed here
  because test_oracle.py's bisim harness doesn't proxy the timer surface
  and its meta-test forbids direct Oracle construction)): (15) the pilot
  teardown race the pass surfaced -- a set_interval tick or a worker
  resuming after an await can outlive the unmounting screen and crash on
  query_one (NoMatches) -- is fixed with teardown guards on every
  widget-touching timer/worker path (_tail_step, _refresh, console
  writes). (16) Pinned by test, worth knowing: job_log_paths resolves
  std_out_file/std_err_file INDEPENDENTLY per stream -- setting only
  std_out_file leaves log_err on the computed default, vendor-parity with
  the wrapper's own spec resolution.
- DL-47 Phase 11e landed: `serve` verb (2026-07-11; found and decided during
  implementation, within DL-41's frame; cli.py's `serve`/
  `_import_textual_serve_or_exit_2` are normative detail). Decisions:
  (1) TEXTUAL-SERVE FLOOR VERIFIED, NOT ASSUMED: `[ui]` gains
  `textual-serve>=1.1` (dev extra too) after installing textual-serve 1.1.3
  alongside the already-pinned textual 8.2.8 and confirming
  `import textual_serve.server` succeeds end to end (real `dsl41 run` +
  `dsl41 serve`, `curl` against the served page returned 200 with the
  expected HTML shell) -- textual-serve declares `textual>=0.66.0` with no
  upper bound, so the pairing was never at risk, but DL-46's own house rule
  (claiming untested floors is how UIs break at install time) applies here
  too. (2) `python -m dsl41` NEEDED A `__main__.py`: none existed.
  textual-serve's `Server(command=...)` runs one app subprocess per browser
  session (ss11's forced thin-client split -- an in-process engine would
  hand every viewer a private universe), and the only import-safe,
  venv-portable way to name that subprocess is
  `<sys.executable> -m dsl41 ui --socket <path>` (a `dsl41` script-entry
  invocation would need the console script on PATH inside whatever
  environment textual-serve itself launched from; `-m` needs only the same
  interpreter). Added a two-line `__main__.py`; the constructed command is
  shlex-quoted, verified against a socket path containing a space.
  (3) IMPORT/REFUSAL ORDER MATCHES `ui`: guarded `textual_serve.server`
  import first (missing extra -> exit 2, pip-install hint), then the
  socket-exists check (missing socket -> exit 2) -- the same
  "refuse before touching anything" shape as `_import_tui_or_exit_2`. A
  bind failure from `Server.serve()` itself (port in use, etc.) is also
  exit 2: cli.py's exit-code contract treats it as "never started," the
  same class as the other two gates. (4) LOOPBACK DEFAULT, PROXY/TUNNEL
  DOCUMENTED NOT BUILT (E3 CLOSED): `--host` defaults to `127.0.0.1` --
  textual-serve ships no authentication of its own, so widening the bind
  is an explicit operator choice, never the default. README gains one
  section: an `ssh -L` tunnel and one nginx `location` block, plus the
  reminder that the control socket's own 0600-from-birth (ss10/DL-45)
  means `serve` cannot see anything its user couldn't already reach
  directly -- it publishes reach, not access. E3 is closed exactly as
  runner-design ss11 scoped it: documented, not built. (5) TESTS MOCK THE
  SERVER, NOT THE VERIFICATION: the manual end-to-end pass in (1)
  established the wiring works; `tests/test_runner_serve.py` (new, 5
  tests) then unit-tests the CLI wrapper against a monkeypatched `Server`
  (CLAUDE.md's "no runtime dependency in any emitted artifact" applied to
  the tests too -- textual-serve's real `serve()` blocks on its own event
  loop and has no place in a unit test): missing-socket exit 2,
  missing-extra exit 2 with the pip hint (forced via
  `sys.modules[...] = None` on both the package and submodule entries --
  an already-imported submodule survives its parent alone being cleared,
  so both need clearing), the quoted-command construction, the loopback
  default, and the bind-failure exit 2. `runner.py`/`runner_wrapper.py`/
  `runner_tui.py` untouched -- 11f (the supervisor tier) is a separate
  unit landing after this one.
- DL-48 Phase 11f landed: supervisor tier + detached mode (2026-07-11; found
  and decided during implementation, all within DL-41a/DL-42's frame;
  runner_supervisor.py's docstring + docs/supervisor-protocol.md ss5 are
  normative detail; the socket protocol is now FROZEN). The availability tier
  (ss6a Tier 1): jobs SURVIVE engine restarts because their parent is the
  supervisor, not the engine (E4 dissolved -- survival is reattachment, never
  adoption). Decisions: (1) `runner_supervisor.py` is stdlib-only, run BY FILE
  PATH like the wrapper, under the same enforced import boundary (AST test
  extended). It copies -- never imports -- the wrapper's durability liturgy
  and (pid, start-time) verify_alive: the stdlib-only boundary forbids the
  dsl41 import, so the SIGNAL PID-reuse guard is reimplemented supervisor-side.
  (2) Single-threaded selectors loop (SIGCHLD self-pipe + listen socket +
  client sockets) -- the wrapper's select+self-pipe shape, no thread-safety
  surface. Wrappers are spawned via os.posix_spawn (NOT subprocess.Popen) so
  the global waitpid(-1) reaper never fights Popen's own bookkeeping; the
  lifeline WRITE END lives in the supervisor only (the ss6a fd-hygiene
  invariant, now anchored one tier up -- that single fact is what detaches job
  lifetime from the engine). (3) Named socket protocol (spec ss5, frozen):
  JSON lines, 0600 + same-uid peer-cred on every accept (Linux SO_PEERCRED,
  macOS LOCAL_PEERCRED/xucred, stdlib struct parse); LIST/PING lease-free;
  SPAWN/SIGNAL/SHUTDOWN carry a monotonic fencing token from ACQUIRE; RENEW/
  RELEASE; async exit pushes to the lease-holding connection (notifications
  only -- the spool is the data channel). Re-acquire by the SAME controller_id
  mints a new token, which is why the engine's controller_id is STABLE per
  run_root (a crashed engine's lease is unexpired for up to ttl_s; resume must
  re-ACQUIRE without waiting it out, and the fresh token fences the dead one).
  Idempotency: run_id is the SPAWN key (replay returns duplicate:true). Linux
  PR_SET_CHILD_SUBREAPER, best-effort. (4) Two cancellation cases, the phase's
  subtlest point (spec ss3): (a) oracle-decided terminal (KILLJOB/
  term_run_time/run_window) -> SIGNAL TERM, grace, SIGNAL KILL, await the exit
  push, identical outcome shape to the tethered kill; (b) engine detach-stop
  (operator SIGINT/SIGTERM of a --detached run, or shutdown for resume) ->
  abandon the await, signal NOTHING, jobs continue under the supervisor. The
  engine flips a DetachSignal before cancelling adapter tasks; the
  SupervisedCommandAdapter's CancelledError path branches on it. Tethered
  adapters ignore it -- the LocalCommandAdapter path is UNCHANGED. (5) The
  run_dir/log/spec construction is shared by both CMD adapters via
  `_build_run_spec` (each fills lifeline_fd from the end that owns the pipe's
  write side); status.json stays the sole outcome channel through one
  `_outcome_from_status`, so tethered and detached, live and reattached, can
  never diverge. (6) Detached resume (spec ss3): resume replays the journal as
  today, then LISTs the supervisor -- a run still wrapper_alive is REATTACHED
  (the adapter task just awaits its exit push, NO reconciliation injection,
  the run never stopped); runs listed dead or unlisted fall through to the ss7
  spool ladder unchanged. On supervisor death mid-run the client's connection
  loss makes the adapter resolve via the SAME ss7 ladder (wrappers EOF'd on
  the supervisor's -9 and record terminated/parent-lost -> TERMINATED). No new
  WAL record kinds: reattachment produces no input (inputs-only holds).
  (7) CLI: `dsl41 run --detached`; `dsl41 supervise list|shutdown` (read-only
  by default per DL-42 item 4 -- shutdown ACQUIREs, failing loudly with holder
  info while an engine holds the lease). The exit-code contract is preserved
  (2 = never started, 1 = failed while running). (8) Kill matrix (spec ss5,
  definition of done): protocol unit tests (unknown verb, bad version,
  malformed line, lease held/expire/re-acquire fencing monotonicity, stale
  token, SPAWN idempotency, SIGNAL pid-reuse refusal, peer-cred, stale-socket
  reclaim) + integration (SIGKILL engine -> survive + reattach no-injection;
  kill -9 supervisor -> spool-resolve TERMINATED, engine survives socket loss;
  SHUTDOWN records signaled never parent-lost; detach-stop SIGINT -> reattach
  SUCCESS; oracle KILLJOB detached -> TERMINATED) + the import-boundary AST
  test + Linux-only subreaper (skipped on darwin, never faked). Bisimulation
  untouched (the FakeAdapter path is unaffected). No new E-question opened --
  the tier implements the DL-41a/DL-42 pins as documented; E4 stays resolved.
  Post-review amendments (adversarial review, verdict "landable
  with named fixes"; the load-bearing guarantees -- lifeline fd hygiene,
  reaper attribution, lease/fencing single-threadedness, the two-case
  cancellation semantics -- confirmed CLEAN by execution):
  (9) MAJOR, confirmed by execution: a SupervisorClient request cancelled
  between write and reply left its pending future dangling, and with no
  correlation ids in the frozen ss5 protocol the reader delivered the
  still-in-flight reply to the NEXT request's future (reproduced: a
  cancelled SPAWN's reply landing in a later ACQUIRE; the real trigger is
  an adapter task cancelled mid-spawn()/signal() with the renew loop or
  another spawn grabbing the orphan). Fixed by POISON-ON-CANCEL, not
  correlation ids -- the frozen protocol stays untouched: on CancelledError
  mid-request the stream state is unknowable (the request may be partially
  written pre-drain), so the client fails the pending future, closes the
  socket, and re-raises; readers are epoch-guarded by their connection's
  own `lost` event, so a superseded reader can neither poison nor deliver
  into its successor. Subsequent calls lazily reconnect -- connect-only,
  never spawning a supervisor -- and re-ACQUIRE with the stable
  controller_id, whose fresh token fences anything the poisoned connection
  had in flight; if reconnect fails, the ss7 spool fallback applies. The
  adapter's await path now tries reconnect() BEFORE falling to the spool:
  post-poison a lost connection no longer implies a dead supervisor, and
  the ladder would kill a healthy detached run. Regression-pinned
  (test_cancelled_request_poisons_and_reconnects, adapted from the
  reviewer's repro: the next request must get ITS OWN reply, via reconnect,
  never the orphan). (10) SHUTDOWN racing a just-spawned wrapper recorded
  "parent lost": _signal_command is a silent no-op while spawn.json has not
  landed, so every signal missed and the wrapper died only by lifeline EOF
  at supervisor exit. _orderly_shutdown now waits (bounded, 5s -- a wait,
  not policy) for missing spawn records before signaling, and _h_shutdown
  replies AFTER the wait-for-wrappers completes, matching the frozen ss5
  order (the earlier reply-then-teardown also double-sent {ok});
  discrimination-checked (the regression test fails against the unfixed
  supervisor). (11) _renew_loop gave up permanently on the first failed
  RENEW, silently lapsing a live engine's lease (pushes stop; only the
  adapters' 1s status.json re-poll saved outcomes). It now retries on a
  short backoff, re-ACQUIREs on a stale/lapsed token with the stable
  controller_id, heals connection loss via the lazy reconnect, and gives
  up -- loudly, once, on stderr -- only after five consecutive failures
  (test_renew_loop_reacquires_after_lease_lapse). Accepted hardening debt,
  recorded not fixed: a blocking sendall to a slow lease-holder can wedge
  the supervisor loop only after thousands of unread pushes (wrappers, the
  recorders, are unaffected; a non-blocking send queue is the named future
  hardening); the unbounded request-line buffer and the blocking spec write
  are same-uid-only surfaces, theoretical; the _refuse_if_live unlink race
  is unreachable while the engine is the only spawner.
- DL-49 insert_machine virtual/real pools + runner machine resolution
  (2026-07-12).
  Two coupled fixes for a `dsl41 run` refusal ("machine X is not this
  host") on a job pinned to a `type: v` pool:
  (1) Lowering: a virtual machine / real pool lists members on repeated
  `machine:` lines, each with its own `factor:`/`max_load:`. `_collect_attrs`
  rejected the repeats. `_lower_machine` now walks attrs in source order --
  `type` stays singular; each `machine:` opens a `MachineMember` (comma-lists
  split); `factor`/`max_load` bind to the most recent member, or are
  machine-level when no member has opened yet (a lone agent's own values);
  every OTHER key stays single (a duplicate is still a lowering error). So
  machines cease to be FULLY opaque (amends DL-18/28): `members` is typed and
  ordered, the rest stays verbatim. equiv compares members order-sensitively
  (a reorder / dropped component is a real difference, never false-equal);
  catalog_hash covers them. The DSL builder gained `machine(members=[...])`
  and the decompiler emits it, so pools round-trip (no DL-40 refusal needed).
  (2) Resolver (runner.resolve_machine, DL-49): before the local-host check,
  a job's `machine:` resolves through insert_machine. Undefined name ->
  literal compare (back-compat). Agent (type a) -> node_name (MISSING
  node_name is an ERROR, never guessed). Real (r/n) -> node_name else the
  record name. Virtual (v) -> resolve every member leaf: ALL local -> run,
  NONE -> refuse (foreign), MIX -> refuse under `--machine-policy strict`
  (default) or run+WARN under `local-eligible` (pool placement is unmodelled
  -- resolution/placement/routing/lifecycle are four layers, kept
  uncollapsed). Bad defs (empty pool, undefined/nested/typeless member,
  unknown/missing type) are ERROR. Resolution is preflight-only and shell-
  side, so the bisimulation gate is untouched (FakeAdapter still emits the
  same completions; rehearse skips machine rules). Dispatch routing +
  journaling the chosen leaf are Goal-2 (distributed) concerns, deferred.
  Distributed engine/agents (a `dsl41 run` on separate boxes) was scoped as
  a FUTURE track: keep the frozen ss5 supervisor as the LOCAL substrate,
  add a new network agent in front of it (do not network-expose ss5 -- its
  spool-as-truth/in-memory-fence/same-uid assumptions are local); machine ->
  endpoint routing lives in operator config, not JIL. No code v1.
- DL-50 Resource/load manager: the oracle HONORS resources; preflight refuses
  the unmodelable (2026-07-13; fidelity doctrine:
  existing prod JIL runs UNMODIFIED, Akin's Law 39 -- change the rider or the
  horse, not both). Un-collapses the oracle's QUE_WAIT non-goal (oracle.py
  docstring + ir-design ss7): a WARN-and-run-unthrottled default silently
  dropped throttling the estate relies on (ss8 "no silent loss"), the one
  outcome worse than refusal. "Phase 2 in phase 1": the honoring manager IS the
  deliverable (a refuse-only increment is useless -- the prod estate HAS locks),
  plus loud refusal for what cannot be modelled faithfully. Decisions
  (oracle.py/ir.py/runner.py docstrings are normative detail):
  (1) CAPACITY-BUCKET MODEL. One bucket per contended entity: `m:<machine>`
  (capacity = machine max_load, demand = job job_load) and `r:<name>` (capacity
  = insert_resource `amount`, demand = `resources:` QUANTITY). A job clearing
  its start gate acquires its ATOMIC FULL demand vector before RUNNING; short on
  any bucket -> QUE_WAIT (a new JobStatus). All-or-nothing acquire => no hold-
  and-wait => deadlock-free BY CONSTRUCTION (Hypothesis-certified across permuted
  admission orders: no over-commit, no deadlock, no leak); QUANTITY=1 shared ==
  mutex for free. Empty demand -> straight to RUNNING, BYTE-IDENTICAL to the
  pre-resource oracle: the whole existing corpus and the ss13 bisimulation gate
  are untouched (semantics live in the oracle, so both bisim arms see resources
  identically -- the 20 DL-50 traces run twice, green). (2) QUE_WAIT is NOT in
  _N_FALSE_STATUSES: a queued job is not running, so n() is TRUE and every
  status/exitcode atom reads false (it never ran). It emits no STARTING and
  bumps no run_number, so the engine's dispatch + ghost gate (DL-43) need NO new
  logic; a queued box member is not in _box_ran, so the SEM-11 literal fold gate
  already keeps the box RUNNING. (3) res_type sets the default release, per-
  request FREE overrides it: R/absent free-on-completion, D never (depletable
  drains -- replenishment is update_resource = SEM-16 non-goal), T is a LEVEL
  GATE that checks free>=q but never acquires/holds/releases; FREE=Y success-
  only, N never, A unconditional. (4) Release + wake on a holder's terminal
  transition, AFTER condition referencers, a fixed deterministic order the
  cross-order property certifies as outcome-invariant. Waiters admit greedily in
  (priority, enqueue-seq, name) order, re-validating box-RUNNING/ice/hold (a
  queued member whose box ended is cancelled to INACTIVE; conditions are NOT
  re-checked). (5) ENFORCEMENT IS PREFLIGHT, MODELLING IS THE ORACLE: the oracle
  builds buckets only for sizeable entities, so oracle-direct over an unrefused
  bad catalog runs it unthrottled; the runner's preflight is the execution gate.
  It ERRORs (fail-closed, both run and rehearse) on an unsized resource (no
  insert_resource / no `amount` -- stricter than L016's warn; a
  `--resource-capacity name=N` override is a documented future escape hatch),
  an unknown res_type (not R/D/T), and a malformed job_load/priority/max_load;
  it WARNs a job_load on a pool machine (throttle unmodelled, Qr3). The old
  WARN-and-run is deleted. Cross-machine shared locks need NO separate refusal:
  the DL-49 foreign-machine ERROR already makes every runnable job local, so
  every honored resource is contended on this one host (revisit when distributed
  execution lands, DL-49 future track). (6) IR carries the numbers as ACCESSORS
  (JobIR.job_load_units/priority_value, MachineIR.max_load_units,
  ResourceIR.capacity_units) over the existing opaque passthrough/attrs -- ZERO
  persisted-field churn, so decompile/equiv/round-trip are untouched; malformed
  values raise ValueError that preflight surfaces (the oracle skips them). Pools
  return no max_load (per-member placement unmodelled, DL-49). (7) FIDELITY
  DOCTRINE: fidelity to AutoSys DOCUMENTED semantics where
  specified, else to explicitly CHARACTERIZED behavior, ambiguity recorded as a
  modeling choice; the deterministic oracle picks ONE representative trace and
  the cross-order property certifies (or, on failure, surfaces as a race) that
  business outcomes are order-invariant; AutoSys quirks are CLASSIFIED (harmless
  / relied-upon / real defect) -- a relied-upon quirk silently "cleaned" is
  silent divergence, so it is surfaced, never auto-cleaned; semantic fidelity
  maximized, runtime/effect fidelity bounded by single-node non-goals.
  (8) AMBIGUITY LEDGER (first-class; resolve via a throwaway
  job on a live instance, per CLAUDE.md): Qr1 FREE-absent renewable release
  (default free-on-completion; risk: masks a hold-on-failure intent; evidence:
  test_dl50_renewable_default_releases_on_failure; switch kept). Qr2 priority
  direction (lower-number-higher assumed; unset sorts last; evidence:
  test_dl50_waiters_admit_in_priority_order). Qr3 pool machine-load throttle
  unmodelled (WARN, not refuse -- resource semaphores still apply). Qr4 absent
  job_load = demand 0 (the attribute opts you in). Qr5 KILLJOB on a QUE_WAIT job
  dequeues + TERMINATEs it (a kill happened -- see amendment 2); ICE on a queued
  job dequeues + INACTIVEs it (an iced job never runs). Qr6 conditions are
  NOT re-checked at admission (a queued job already qualified; a global flipping
  while queued is the corner). Threshold mixed-use (R/D acquirers on a T
  resource reduce the level a gate sees) and the release-vs-referencer cascade
  order are documented deterministic choices. NONE guess-resolved: documented
  default + kept switch + trace evidence, the Q-discipline. (9) Trace tests:
  10 core admission traces in test_oracle.py (mutex, pool, threshold, machine-
  load, box-member-queued, priority order, kill-releases, FREE Y/A/renewable-
  default) run under BOTH bisim arms; test_resources.py adds the cross-order
  safety+liveness property, depletable drain, FREE=N, and the unsized-is-
  unmodelled boundary; preflight negative-conformance in test_runner_scheduler.
  Post-review amendments (2026-07-13; adversarial review: one BLOCKER + one
  MAJOR + two MINORs + one NIT, all CONFIRMED against the code -- the review also
  confirmed the bisimulation gate could NOT catch the BLOCKER, both arms wedged
  identically): (1) BLOCKER, silent-wrong in the dangerous direction: a resource
  holder that RE-TRIGGERS itself inside its own completion cascade (the L010
  tight-loop) stranded a semaphore unit forever -- `_after_transition` woke
  condition-referencers BEFORE releasing the holder's units, so the re-run's
  `_acquire` overwrote `_held[job]` while the prior run's units were still
  charged, and the later `_release` popped the overwritten record, leaking the
  original unit; downstream jobs then wedged in QUE_WAIT forever. Fixed by
  releasing BEFORE `_wake_referencers` (the re-trigger re-acquires against freed
  capacity) and making `_acquire` EXTEND, never overwrite, `_held` (a missed
  release becomes a recoverable over-hold, never a strand). Regression-pinned
  behaviorally under both bisim arms and by a direct used==held invariant.
  (2) MAJOR, fail-open control-loss: KILLJOB on a STANDALONE QUE_WAIT job was
  silently ignored then admitted on the next release -- it RAN despite the
  operator's kill (Qr5's "box-end cancels it" only covered box members). KILLJOB
  now dequeues + TERMINATEs a queued job. (3) MINOR: duplicate `resources:` refs
  over-committed (`_can_admit` per-entry vs `_acquire` summed) and, with
  asymmetric FREE, leaked -- `_demand_vector` now COALESCES duplicate bucket keys
  (summed demand, most-restrictive release) and preflight refuses a repeated
  name. (4) MINOR: a statically unsatisfiable QUANTITY > amount hung forever --
  preflight now refuses it. (5) NIT: ICE on a queued job lingered QUE_WAIT; it
  now dequeues + INACTIVEs immediately. Confirmed CLEAN by the review: cross-job
  over-commit (atomic vector), no negative `used`, no nondeterminism, `_in_wake`
  fixpoint safety, the empty-demand `_start` path byte-identical, depletable/
  FREE=N never releasing, kill/terminate releasing, preflight gating run AND
  rehearse. Deferred (Qr7, NIT): a box that RESTARTS while a member is still
  QUE_WAIT (only reachable via a killed box with no intervening release) adopts
  the member with a stale enqueue-seq -- it still runs, so the impact is
  mispriced waiter ordering only.
- DL-51 Reference-of-record: legacy JIL is the input of record, evolved via the
  Python master, never silently diverged (2026-07-13; the doctrine behind
  DL-50's "run unmodified" goal, softened from an earlier "immutable" framing).
  Prod JIL is what runs in prod; dsl41's job is to run it FAITHFULLY
  or REFUSE loudly -- both leave the JIL bytes untouched, so neither honor nor
  refuse violates the run-unmodified goal (Law 39: refuse != a forced edit).
  Rules: (1) NO SILENT DIVERGENCE -- an unmodelable shape refuses (DL-50 fail-
  closed), it never runs approximately. (2) TRANSFORMS ARE PROVEN-EQUIVALENT --
  field reordering for diffing, canonical form, folds must pass the equiv tiers /
  F2 canonical fixpoint / decompile round-trip; equivalence is of good quality,
  not opportunistic. (3) A REAL DEFECT IS A WIN -- detecting a genuine deficiency
  in unchanged prod JIL is a good thing to spot, fix, test, and release to prod
  quickly; it is a finding, not something to preserve. The modification PATH is
  decompile JIL -> runnable Python DSL (the master source), edit there, equiv-
  prove the result (README/DL-17/DL-37); the closer the simulator + runtime are
  to AutoSys, the more testing confidence the round trip buys.
- DL-52 Explicit machine identity: the runner is TOLD what machine it is; FQDN
  matching is retired (2026-07-13; DL-50 flagged a 30s
  `getfqdn()` startup stall). Rationale: FQDN matching (DL-45 M6) asked the wrong
  question -- "does the OS's reverse-DNS name for this box happen to equal the
  estate's machine name?" -- joining two unrelated namespaces (a cloud hostname
  `ip-10-0-3-42` vs an estate machine `greezy_spoon`) through a reverse-DNS
  lookup that also stalls for tens of seconds on slow resolvers. Placement should
  not be a dispatch decision based on FQDN, even where AutoSys is married
  into it. Decisions: (1) `_local_identity(declared)` REPLACES `_local_names`:
  `getfqdn()` is DELETED (no reverse-DNS anywhere -- this also cures the `dsl41
  run` / rehearse startup stall, the DL-50 review's lone red test). Declared
  non-empty (`--as-machine`) => identity is EXACTLY those names + localhost, pure
  and explicit, no hostname guessing; declared empty => the forward hostname
  (short + full) + localhost, zero-config for estates where machine == hostname.
  So declaring opts into pure-explicit and omitting stays friendly, with no
  second mode flag. (2) `resolve_machine` gains a DIRECT NAME MATCH first: a job
  whose `machine:` IS an identity name runs here without consulting
  insert_machine -- "this runner IS greezy_spoon" is authoritative over whatever
  node_name the record carries. Otherwise the DL-49 resolution is unchanged
  (agent/real -> node_name; virtual -> members; `--machine-policy` for split
  pools), now comparing resolved nodes against the declared identity, so
  declaring by node ALSO works. Undefined-and-not-ours is foreign. (3) CLI:
  `dsl41 run --as-machine NAME` (repeatable), threaded through
  `_preflight_or_exit` -> `preflight(as_machine=...)`. rehearse skips machine
  rules (execution=False), so it needs no flag. (4) Deliberate behavior change:
  an estate that relied on FQDN auto-matching (a job pinned to the
  full FQDN, matched via getfqdn when gethostname returns the short name) now
  needs `--as-machine <fqdn>` -- explicit over magic. (5) Distributed alignment
  (DL-49 future track): each network agent declares its own `--as-machine`; the
  identity model extends without an OS-identity oracle. Tests: resolve_machine
  direct-match / declared-node / foreign; `_local_identity` default-vs-declared
  with getfqdn asserted UNCALLED; the previously-hanging subprocess `dsl41 run`
  integration test now passes unpatched.
- DL-53 Open-questions doc sweep: nine of the fifteen parked questions close from
  public vendor docs (2026-07-28; executes the 2026-07-12 audit, which had never
  been applied to the repo. Method: every claimed citation
  re-fetched and confirmed verbatim BEFORE any edit; a claim that failed verification
  did not close -- no close without a surviving quote). CLOSED [?]->[V]:
  (1) Q1 precedence -- "The parentheses force precedence, and the equation is
  evaluated from left to right" (TechDocs 12.1, condition attribute page): flat
  left-associative wins. Per DL-06's own protocol the C-style candidate grammar,
  the CONDITION_PRECEDENCE env/module switch, and the sentinel tests are DELETED;
  `parse_condition()` loses its precedence parameter; pinning tests
  test_sem03_flat_left_to_right_precedence_pinned (grammar shape) +
  test_sem03_precedence_pinned_model_level (Cond model). Resolves DL-06.
  (2) Q4 n_retrys -- FAILURE-only, application-level: "how many times to attempt
  to restart the job after it exits with a FAILURE status. If a job exits with a
  TERMINATED status, it does not restart"; system/network failures restart via the
  MaxRestartTrys scheduler config instead (cross-confirmed on both TechDocs pages).
  Retry modeling in oracle/runner stays out of scope v1 -- now a recorded SCOPE
  decision, no longer an unknown: the oracle keeps not modeling it, preflight keeps
  its WARN; implementing retries is a future work item unblocked by this entry.
  (3) Q5 event persistence -- yes, by architectural entailment with the inference
  step stated in the dossier: ujo_event "records events that the scheduler has not
  yet processed", the event server (DB) stores all objects, the scheduler scans the
  DB on start; no in-memory-only queue exists to lose. KB 11013 corroborates.
  (4) U2 workflow status -- member failure => Running/Problems (workflow keeps
  running); Success iff all members Success/Finished/Skipped; a workflow instance
  is never Failed (UC 7.4 status table, Failed row = "All (except Workflow)").
  (5) U4 exit codes -- mechanism pinned: per-task "Exit Code Processing" field,
  default Success Exitcode Range. The audit's second sub-claim ("default success
  exit code 0") FAILED verification (exitCodes is a required field with no
  documented default) and is dropped.
  (6) U5 Time Scope -- no documented maximum: hours "must be a positive integer",
  uncapped, +/-124:00 worked example; retention-bound in practice.
  (7) U6 SPLIT: U6a per-trigger Time Zone field RESOLVED ("Triggering by Date and
  Time"); U6b calendar-algebra parity with AutoSys extended calendars stays OPEN.
  (8) U7 -- overrun mechanism is Late Finish + Abort Action (a composite WE label
  "max-run equivalent"; the vendor does not); auto-retry applies to Failed status
  only; "Suppress Intermediate Failures" is a Re-run command modifier suppressing
  failure propagation, NOT a Retry option -- M30's conflating note is corrected.
  (9) U8 -- uc.perform_actions.before_wf_dependency_evaluation defaults to true
  (System Properties); per-controller configurable, so the migration report pins
  the assumed value instead of listing an open question.
  Consequences: backend_uc._U_QUESTIONS shrinks to U1 + U6b (resolved questions
  downgrade into per-edge assumption strings; report-content test pins re-pinned
  deliberately); M02/M08/M26/M29/M30/M31 reclassified or reworded; SEM-03's
  embedded [?] removed, UCS-04/UCS-06/UCS-08 upgraded to [V]. DEFERRED, not an
  oversight: the uc_oracle twin's workflow rollup (Failed if any member
  Failed/Cancelled) is now KNOWN-divergent from resolved U2 (a real UC workflow
  goes Running/Problems, never Failed) -- kept as a labeled approximation until
  the twin models Running/Problems, its own future unit; the trace reason string
  and test docstring say so. E8 (external-signal death) was ALSO swept and is
  genuinely undocumented publicly (TERMINATED is defined mechanism-agnostically as
  "the job was killed"; KillSignals shows exit-code-driven status resolution in
  kill scenarios) -- E8 stays open with its marker upgraded to "swept 2026-07-28,
  needs live instance". NOT touched here: Q2/Q3 (the same sweep found docs
  CONTRADICTING the implemented defaults -- a behavioral change needing its own
  design pass, next unit), Q6/Q7 (genuinely need a live instance), U1
  (version-cutover residue), U3 (the base-serializer downgrade is its own unit;
  DL-08/DL-15 still stand).
- DL-54 Q2/Q3 behavioral unit (2026-07-28; the "next unit" DL-53 deferred).
  Verbatim TechDocs wording captured BEFORE the flip (the DL-53 method: no doc-derived change without a surviving quote).
  (1) Q2 SPLIT, Q2a RESOLVED: zero-lookback anchors to the DEPENDENT job's own
  last end -- "When you specify 0, AutoSys Workload Automation examines the last
  end time of the job first. It then examines the last end time of the condition
  job. If the condition job has run since the last run of the job for which the
  condition is coded for, the job is allowed to start." (12.0.01, condition
  attribute page; the page's own "the job for which the condition is coded for"
  disambiguates the referent). Per the DL-06 protocol the resolved question's
  switch is DELETED (ORACLE_ZERO_LOOKBACK_ANCHOR: midnight default and
  last_change alternate both superseded); test_sem04_zero_lookback_* pin the
  reading against midnight in BOTH directions (same-day-stale must not fire;
  cross-midnight-fresh must). Mechanism: JobRuntime.last_end_at records every
  transition into a terminal status; condition evaluation threads the EVALUATING
  job (the starting job; the box for box_success/box_failure -- runner _explain
  passes the queried job); tier b's per-job bit is reinterpreted as
  job_zero_fresh ("the anchor test passes" -- exact per predecessor because both
  compared conditions share one evaluator). Q2b stays OPEN (# PENDING: Q2b):
  first-run (dependent never ended -> no anchor) is undocumented, pinned as
  unbounded/satisfied. CONSEQUENCE: SEM-35's Q2-adjacent timezone corner
  DISSOLVES (a relational anchor is tz-independent; L005 loses its caveat).
  (2) Q3 default FLIPPED to arm-and-wait, question stays open behind
  ORACLE_SCHEDULED_FALSE_CONDITION (# PENDING: Q3). Evidence is an entailment
  (stated Q5-style, PARTIAL not dispositive): ACTIVATED is a documented WAIT
  state for box members; "All defined starting conditions must be true" is a
  continuously evaluated AND; SEM-21's off-hold wording is now verbatim-pinned
  ("Jobs that meet their date and time conditions while they are on hold start
  immediately after they are taken off hold unless other starting conditions
  apply and are not satisfied"). Mechanism: a scheduled (non-force) tick blocked
  at a RELEASABLE gate -- condition false, or ON_HOLD -- sets JobRuntime.armed
  (trace marker SCHED_ARM); the schedule gate admits non-scheduled attempts
  while armed; ANY start consumes the arm (at most one run per tick; FORCE
  included). PINNED non-arming gates: ON_ICE (SEM-20 conditions-must-reoccur),
  box-not-RUNNING (member ticks only count while the box runs), already-live
  job. PINNED: an unconsumed arm never expires (disarm boundary undocumented --
  the Q3 residue, with the standalone-case confirmation). run_window applies at
  the moment a start goes through, armed or not.
  (3) Consequences swept: P-M07 flips from divergence pin to ALIGNMENT pin (a
  scheduled n() mutex now queues like UC's ExclusiveWait; the historical
  divergence is preserved under the abandon switch) -- M07 note softened, M03
  note sharpened (a relational anchor is inexpressible as a fixed Time Scope).
  NEW L019 (warn; the ir-design ss11 impact-ledger rule): every
  schedule+condition composition is a per-estate verification item while Q3 is
  open; corpus trigger consumer_stale, per-rule fires/quiet pair. SEM-21's
  run-window-at-off-hold sentence ("re-scheduled to their next start time") is
  NOT fully reconciled with SEM-33's verified closer-edge rule -- noted in
  SEM-21, not modeled apart. The sem32 abandon trace test became the
  arm-and-wait pair; the runner's must-start pending test re-pinned (the gate
  edge now starts the armed job and clears the deadline).
  AMENDED (adversarial review: 2 MAJOR behavioral, 1 MAJOR doc
  drift, 7 MINOR, 4 NIT; all verified findings fixed pre-commit): (a) MAJOR --
  arm was job-global and outlived its box execution (a member armed in box run
  N auto-started at the start of run N+1 and its real tick was then swallowed
  by SEM-10 once-per-run); fixed by scoping member arms to the arming box run
  -- box completion clears unconsumed member arms (SCHED_DISARM), nested boxes
  recurse naturally. (b) MAJOR -- the hold gate preceded the box gate, so a
  HELD member of a not-running box armed from its tick; fixed by re-checking
  box-RUNNING inside _arm. (c) MINOR promoted to pin -- arm consumption moved
  from _start's top to the actual start tail (_run + noexec bypass): a
  QUE_WAIT enqueue keeps the arm latched (a cancelled queue attempt no longer
  eats the tick); KILLJOB on the queued job consumes it deliberately. (d)
  MINOR -- zero-lookback now compares the PREDECESSOR'S last_end_at (not
  status_at): both sides of the citation are end times, so an n() predecessor
  bounced to INACTIVE by an injected status is not a fresh run; predecessor
  never-ended => not satisfied. (e) MINOR -- one pending run_window defer
  timer per (job, opening) instant (an armed job's repeated condition edges
  previously queued unbounded duplicate timers into pending_timers()). (f)
  MINOR -- ss10 status response now carries `armed` per job (DL-46
  display-truth rule; an operator sendevent STARTJOB can plant the latch, so
  it must be visible without tailing the trace). (g) MAJOR doc drift --
  runner-design.md's three "SEM-32 abandonment" passages swept; ir-design ss9
  gains the L019 row; README L-rule ranges; L005/equiv docstrings de-lied.
  ACCEPTED, recorded not fixed: an armed run may land in a later run_window
  cycle than its tick (consequence of the latch-until-consumed pin); resume's
  at=max(at, last_at) re-stamp can move the Q2 anchor forward (pre-existing
  status_at behavior, deterministic across re-resumes); P-M07 alignment is
  milestone-level (catalog-order vs FIFO wake residual noted in M07); tier b's
  zero-freshness bit over-approximates conservatively for self-referential
  s(X,0). Pinned-by-test additions from the breadth test round: arm survives
  ON_ICE/OFF_ICE untouched; run_window gates an armed start exactly like an
  unarmed one; the zero-lookback tie (predecessor end == anchor instant) is
  satisfied (>= inclusive).
- DL-55 U3 base-serializer unit (2026-07-28; the DL-53 audit's last actionable
  item). Verbatim wire evidence captured BEFORE the freeze
  (the DL-53 method) from the current docs.stonebranch.com site
  (product selector "8.0", build stamp v2026.07.5), raw-fetched and grepped
  against saved bytes; cross-checked against gomleksiz/uac-api and
  OptionMetrics/terraform-provider-stonebranch (whose community openapi.yaml,
  self-versioned 7.9.1.0, is PARTIAL-confidence corroboration only).
  (1) U3 SPLIT (the U6/Q2 pattern): U3a RESOLVED -- the CREATE-ONLY
  whole-record shape (type "taskWorkflow"; workflowVertices with value-wrapper
  task refs, explicit STRING vertexIds, optional string coordinates;
  workflowEdges with condition/sourceId/targetId value wrappers, straightEdge)
  is frozen in docs/uc-edge-schema.md with verbatim citations; base condition
  tokens confirmed exactly "Success" / "Failure" / "Success/Failure" (forward
  slash, from the doc's own worked examples). U3b OPEN (# PENDING: U3b in
  backend_uc.py): rich condition forms (Exit Code / Step Condition / Variable
  + variableCondition{firstValue, operator, secondValue}, vertex-level
  conditionExpression), the live /resources/openapi.json pull, and write-path
  verification (one live POST + GET readback). DL-08 stands: the API client is
  generated from OpenAPI, never hand-written -- the serializer emits records,
  not calls.
  (2) compile_to_uc() is now REAL: it serializes the twin (DL-16's "structure
  the backend serializes post-U3") into a self-describing UcBundle
  {catalog_hash, tool_version, records, quarantined, notes} -- the exclusion
  ledger travels in the same file as the records, so a pipeline cannot apply
  one without the other. BlockedOnU3 DELETED per the DL-06 protocol (the
  resolved half's guard goes; the open half is per-workflow quarantine, not an
  exception). DL-15's report decisions are otherwise unchanged.
  (3) Safe-freeze rules (2026-07-12 audit, now verified): CREATE-ONLY
  -- retainSysIds pinned false (VERIFIED a record attribute -- JSON top-level
  field / XML attribute -- NOT a query parameter; vendor default true) and
  sysId/version/exportTable/exportReleaseLevel omitted ("read only" /
  Read-and-List-only per the property table). QUARANTINE the WHOLE workflow on
  any edge outside the base tokens -- the twin's `cancelled` (M06 t(); a
  "Cancelled" edge condition confirmed NOT to exist in any public source) or
  any var_condition (M08/M09) -- with every offending edge listed: no partial
  workflow, no silent edge drop (DL-04).
  (4) Scope pins: workflow records ONLY -- task bodies (agents, credentials,
  commands) are estate-specific and not doc-frozen; the bundle's notes carry
  the referenced-task worklist and the verbatim-name assumption (task `name`
  has NO documented character/length constraint -- searched, NOT FOUND -- so
  AutoSys-derived odd names, e.g. embedded colons, fail loudly at create
  rather than silently at compile). Nested boxes stay flattened (M18 v1,
  DL-16) with a per-workflow alias note. Layout is a deterministic cycle-safe
  layered pass (longest-path depth -> y, arrival order -> x); vertexIds are
  catalog-order strings. No timestamps anywhere (DL-15 determinism).
  (5) CLI `dsl41 uc` (stdout / --out; quarantine summary on stderr; exit 0
  once a bundle is generated, --strict flips quarantine to exit 1 -- lint's
  warn/strict pattern). The migration report grew a U3a summary bullet and a
  quarantined-workflows section; the footer now names U3b. _U_QUESTIONS
  deliberately stays U1+U6b: U3b gates emission, not a mapping row -- it
  surfaces through quarantine + the footer instead.
  (6) New facts recorded in the schema doc for future units: "Success/Failure
  and Failure are not valid for Workflow, Timer, and Manual tasks" (our
  source-type reading, consistent with resolved U2) -- unreachable in v1
  flattened output, load-bearing when M18 sub-workflow nesting lands; the
  community openapi.yaml's ConditionWsData {vertexId, type} conflicts with the
  doc's verified {"value": ...} condition shape -- the doc wins, re-check at
  the live pull.
  AMENDED (breadth round: 20 tests, no product bugs, exact
  layout expectations matched; adversarial review: 2 MAJOR, 6 MINOR,
  2 NIT, verdict SHIP-AFTER-FIXES -- every schema-doc quote byte-verified
  against the saved raw sources, quarantine soundness confirmed
  unbreakable; all verified findings fixed pre-merge):
  (a) MAJOR self-found pre-review, reviewer-confirmed -- _vertex_layout's
  recursive DFS blew the interpreter stack on a legal >=~1000-job chain
  declared consumer-first (catalog order is the memo shortcut, so forward
  order masked it), which DL-55's report integration newly extended to
  `dsl41 report`; rewritten as the value-identical iterative DFS.
  (b) MAJOR -- M07 mutex groups (and M31 exit-code boundaries) vanished
  from the bundle with zero trace: twin-MODELED constraints that workflow
  records cannot carry got no note, so a mutex-only catalog produced a
  clean-looking bundle (the exact failure the self-describing-bundle pin
  exists to prevent). Fixed: notes now name every mutex group and every
  exit-code task whenever the twin carries them.
  (c) MINOR -- the twin-exclusion pointer was a count ("N constructs
  excluded -- see the report"); the bundle now carries the twin ledger
  VERBATIM as its own `excluded` field (edges quarantine, node-level
  R-constructs ride the ledger -- the asymmetry is deliberate and now
  visible in the artifact).
  (d) MINOR -- "names pass through verbatim" was false for
  component-synthesized wf_<first task> records: the claim is re-scoped to
  task names and a note lists every synthesized record name.
  (e) MINOR -- duplicate record names (a box literally named wf_x plus a
  standalone job x) would silently clobber under an upsert wrapper: every
  collision party now quarantines.
  (f) MINOR -- report cost: compile_twin's cross-edge scan used list
  membership (quadratic; 12k-job report 0.17s->2.9s) -- set-based now; the
  report reuses the bundle's catalog_hash instead of hashing twice.
  (g) MINOR -- stale bare-U3 pointers swept: the twin-model comment
  ("will serialize once U3 freezes"), L014's "extend when U3 freezes the
  schema" (name constraints are searched-NOT-FOUND, DL-55 item 4),
  ir-design D3 ("after U3" -> U3a shipped / U3b), and the schema doc's
  task_workflow.go citation (file re-fetched, Type literal byte-verified,
  now in the saved evidence set) + the condition-enum "verbatim" label
  (tokens verbatim, list reflowed -- now says so).
  (h) NIT -- empty-box records no longer emit a dangling "(0): "
  worklist note (gated on referenced tasks, not records).
  ACCEPTED, recorded not fixed: on cyclic input the layered layout leaves
  one back edge pointing up and downstream tasks may share a layer with
  cycle members -- inherent to acyclically layering a legal cycle (L010),
  deterministic, cosmetic-only (the reviewer's relaxation suggestion
  reproduces byte-identical coordinates: mid-cycle memoization IS
  back-edge-stripped longest path); degenerate self-loop / duplicate wire
  edges pass through and fail loudly at create at worst; DL-15's
  historical "raises BlockedOnU3" wording stays untouched (append-only
  log; this entry records the supersession).
- DL-56 Runner honors standard calendars; extended stay materialize-only
  (2026-07-28; preflight [calendar] refused every
  run_calendar/exclude_calendar job, but holiday/business-day calendars are
  exactly the production schedules days_of_week cannot express — the
  blanket refusal made the runner unusable on such estates). Decisions:
  (1) NEW SCOPE, not a DL-36 relitigation: DL-36 governs the COMPILER
  carrying calendar definitions opaquely (names for L018, definitions
  verbatim) and is unchanged; the RUNNER consuming a standard calendar's
  explicit date rows adds an interpreter where autocal itself is one — no
  rule expansion involved. Scheduler day-eligibility becomes: day in
  run_calendar's set (SEM-31: XOR days_of_week) minus exclude_calendar's
  set (SEM-31: subtracts from whichever is active), evaluated on the job's
  LOCAL day (per-job zone else --timezone base else UTC); ticks stay
  start_times/start_mins. Date rows parse as mm/dd/yyyy with an optional
  time-of-day tail, ignored — see (3). (2) Extended calendars stay
  unmodeled: workday/holcal/cyccal/adjust/condition expansion is autocal's
  semantics (U6b/M24 parity); preflight ERROR tells the operator to
  materialize the rules into a standard date list on a live instance. (3) run_calendar with neither
  start_times nor start_mins is refused fail-closed — the vendor may fire
  at the calendar row's own time; opened as E11 [?] (runner-design §15),
  needs a live instance. A window/SLA-only block without a calendar stays
  silently inert (E10 pinning) — the asymmetry is deliberate: only the
  calendar rows carry a plausible alternative trigger time. (4) An
  exhausted calendar (no eligible day at/after the anchor) makes the job
  DORMANT — occurrence None, dropped from the tick map, never an error: a
  finite date list running out is the calendar meaning what it says.
  Preflight WARNs when run-minus-exclude is empty and when the last
  eligible date lies before the run start (silent-never-fires is §8's
  business). (5) preflight() gains an optional `start:` anchor (run passes
  wall-now, rehearse its virtual --start); the exhaustion WARN is its only
  consumer — None skips it, so bare-construction callers are unchanged.
  (6) Dangling calendar references — L018's advisory WARN at lint time —
  are runner preflight ERRORs: the same advisory-at-compile/fail-closed-
  at-run strictness split DL-50 set for L016 resources. L018 itself is
  untouched (it still cannot assume the runner's closed world: AutoSys
  resolves calendars against its database).
- DL-57 Extended calendars interpreted: SEM-36..39 doc-freeze, autocal
  interpreter, IR repeat-key lanes (2026-07-29; scope widened to the full
  autocal rule syntax — EOM, weekday tokens, etc. — with the rules read
  out of vendor documentation, never guessed). Supersedes DL-56 decision
  (2): the RUNNER now interprets extended calendars; DL-36's compiler-side
  opaque carry is unchanged — the compiler still never expands rules.
  Evidence: the vendor pages re-fetched under
  verbatim-quote-or-mark-unverified discipline. Decisions: (1) semantics frozen as SEM-36..39 (record model,
  keyword grammar incl. the [V] Jan-1 week anchor and WEEKDAYS-holcal
  auto-subtract, filter-then-replace single-shot disposition pipeline,
  cycles); new open questions Q8a (both-category replacement precedence),
  Q8b (nonzero adjust + N/W/P), Q8c (non_workday replacement targets),
  Q8d (rule-list algebra, precedence, AND/OR words, all-exclusive
  compounds), Q8e (week-of-period anchor), Q9 (-E export bytes); doc-
  defective tokens (WORKDXnn, CWEK family) refused outright, no switch;
  folk tokens (DAY#n/MONTH#n/YEAR#n/CYCLE#L) proven nonexistent — two
  synthetic fixtures had invented them pre-freeze, corrected to
  CWRK#L/EOMWORK. (2) CalendarIR.conditions + CycleIR.periods lanes:
  repeated `condition:` lines and start_date/end_date pairs are legal
  vendor shapes the duplicate-attribute rule had made unloadable;
  pairing is positional with loud mispairing errors; the lanes are
  extended-only — a standard calendar's `condition:` stays an opaque attr
  (review finding; pre-DL-57 behavior). Calendar/cycle attr keys now
  lower-case in the IR (deliberate: L018/autocal index them; scanner
  fidelity untouched). equiv tier (a) and catalog_hash cover the lanes
  (conditions comparison is order-sensitive — stricter, honest). DSL:
  builder conditions=/periods= kwargs, decompiler emits single-value
  kwargs vs lists, like-named-attr collisions refuse loudly (the `dates`
  guard's argument). (3) `ext_calendar:` accepted as a statement verb
  (PENDING: Q9 accepts both spellings + both workday serializations;
  rendering preserves what appeared; a new DL-18-class split trigger,
  accepted — no JIL job attribute has the name). (4) autocal.py: pure
  interpreter, windowed generation (±400-day margin ≥ walk cap 366 +
  adjust 9), Q8a as a COMPILE-TIME gate over holcal's non-workday members
  (the verdict is a property of the calendar, never of a query window),
  60-year dormancy ceiling for unbounded rules, cycle-bound calendars
  carry a `bound`. (5) Scheduler/preflight wiring: extended sources
  resolve through the interpreter with a per-plan windowed cache;
  occurrence spans extend for unbounded run rules AND extended excludes
  (either exhausting to dormancy, never the unreachable-error); preflight
  = compile validation + anchored generator probes (732-day probe bound;
  no anchor, no probe) incl. the days_of_week-under-extended-exclude
  combination; E11 untouched. Review round: 35
  breadth tests, zero bugs; adversarial verdict SHIP-AFTER-FIXES — 1
  BLOCKER fixed (the original both-orders Q8a check was window-dependent:
  preflight-clean catalogs could abort a live run mid-flight over a date
  ~3 years out; root cause of the compile-time-gate redesign) + 4 MAJOR
  fixed (interleaved per-category application refused ordinary vendor
  configs incl. holiday-O + non_workday-W and the docs' own mnthd#15
  scenario — filter-first restores them; non_workday-N contradicted its
  own verbatim "include the next workday" — was +1-day, now a walk, and
  SEM-38's misquote of that sentence corrected; all-exclusive compound
  rules silently inverted to near-universal includes — now refused;
  days_of_week + extended exclude_calendar crashed the "unreachable"
  branch — now dormant with a preflight WARN) + minors (decompiler
  duplicate-kwarg SyntaxError shapes → loud DslError; preflight probe
  errors misattributed to run_calendar; parser recursion capped at 100).
  Accepted, documented: blank `condition:` value = absent = DAILY (the
  SEM-36 empty-value convention); per-plan cache regeneration when jobs
  share a calendar (cheap; revisit on profile). Tests 1590 → 1675.
- DL-58 Citation sweep closes Q2b/Q3/Q7/Q8a/Q8e/E11, flips two wrong pins
  (2026-07-30; EVERY citation verified by raw fetch before any pin
  moved; two candidate closures did NOT survive verification and changed
  nothing: a Q8c "N = workdays-only" misread of thread 778062, which is
  about O, and a Q8e CWRK flag with no surviving source. Source list
  appended to the dossier's Sources section). Resolutions,
  each per the DL-06 protocol (marker retired, switch deleted where one
  existed):
  (1) Q2b RESOLVED, pin confirmed — a never-ended dependent has no anchor
  and `s(A,0)` is satisfied ("working as designed. When a new job is
  inserted it has no initial/previous end time" — CA support, thread
  760251, with the epoch-0 effect corroborated). No code change; marker
  retired, test docstrings cite.
  (2) Q3 RESOLVED, DL-54's arm-and-wait confirmed for the standalone case
  ("The STARTJOB event being processed satisfies the start_times/
  run_calendar dependency", a start "resets" it — CA's Mark Hanson,
  thread 734033, reproduced tests for both trigger kinds) and the disarm
  boundary is no-expiry latch-until-consumed ("no set limit … regardless
  of how far in the future" — Broadcom staff, thread 801986). The
  `ORACLE_SCHEDULED_FALSE_CONDITION` abandon switch is DELETED; its two
  switch tests retired. NEW Q3c opened (`# PENDING: Q3c`, oracle.py):
  801986's box aside hints a member latch may survive into the next box
  run — tension with DL-54's box-run-scoped arm; scoped pin stands.
  (3) Q7 RESOLVED — KB 408778: a present fail_codes decides ALONE (listed
  → FAILURE, "Any other exit code … will be interpreted as a success";
  success_codes and max_exit_success ignored alongside it); absent
  fail_codes, success_codes alone decides; neither → threshold. Corner
  pins (i)(ii)(iii) confirmed; corner (iv) FLIPPED in `ir.exit_is_success`
  (fail_codes-alone unmatched: was threshold-decides, now SUCCESS) along
  with the both-lists fallthrough (success_codes no longer consulted
  after a fail miss) — the old conservative direction invented FAILUREs
  the vendor records as SUCCESS, on both the oracle and the UC twin (M31,
  one shared function, zero twin-side changes).
  (4) Q8a RESOLVED — Define Extended Calendars 12.1: a specified holiday
  action "applies … to all of the dates listed in the [holiday]
  calendar"; non-workday treatment of holcal dates only "when you do not
  specify an action at the Holiday Action prompt". An either/or dispatch:
  holcal dates under a specified holiday action are never seen by the
  non_workday code (filter OR replacement). autocal's compile-time
  disagreement gate (DL-57 item 4) DELETED — it over-refused vendor-valid
  calendars; `_dispose` now routes holcal dates to the holiday action
  outright (behavior change beyond the gate: holiday-O now shields a
  weekend holiday from non_workday-W relocation). Residue: replacement-
  target re-entry folded into Q8c.
  (5) Q8e RESOLVED — CWEEK anchors to consecutive 7-day chunks from each
  period's first day (Broadcom staff worked example: quarterly cycle +
  `CWEEK#01 | CWEEK#02` = "the first 14 days in every quarter"). CWRK is
  nth-workday-of-period, already implemented so. Marker retired; the
  ragged last chunk is an accepted arithmetic implication.
  (6) E11 RESOLVED and FLIPPED — run_calendar with neither start_times
  nor start_mins is a valid vendor shape (thread 734033 worked examples +
  two estate JILs): fires at the calendar row's own HH:MM, 00:00 when no
  time anywhere, job start_times overrides row times. The Scheduler
  refusal and the preflight ERROR are deleted; `standard_rows()` parses
  the row-time tail (strict `mm/dd/yyyy [HH:MM]` now — a garbage tail was
  previously ignored, now loud), `_SchedulePlan.row_times` carries
  per-day ticks, extended (generated) days tick at 00:00; exclusion stays
  day-level. Free corroborations: exhausted calendars log
  CAUAJM_W_10119/10120 and the job silently unschedules (KB 442457 — the
  DL-56 dormancy pin), and the vendor's 365-day materialization horizon
  drops yearly jobs with >366-day gaps — an operational artifact
  (resolution: regenerate) deliberately NOT replicated.
  (7) Kept open, evidence recorded: E8 — KB 230562 shows a spawn-path
  signal-9 abort as agent-level FAILED; directional for FAILURE but not
  the mid-run scenario; TERMINATED pin stands, marker re-swept. Q6 —
  narrowed: the condition-atom half of ON_ICE is now cited (KB 438836,
  the SEM-05 upgrade + cross-instance ON_ICE-not-transmitted caveat) and
  KB 92872 pins the box_success evaluation trigger; the iced-member-in-
  box_success case itself stays uncited. Q8b — refusal stands (the 12.1
  adjust prompt's "enter 0 … if you specified replace days using …
  action values"); KB 280764's nonzero-adjust-with-S vector reproduced
  as a cited fixture. Q8c — sharpened: O filters verified (thread 778062
  preview + CA "documentation was wrong, behavior unchanged"); N's doc
  text churned across eras; 825395's 2012 consecutive-holiday-N report
  keeps the re-entry corner open. Q8d — literal AND now demonstrated in
  an estate calendar (KB 442457; its mask-field narrative is
  internally under-determined — only the AND acceptance taken). Q9 —
  narrowed to one read-only command (`autocal_asc -e ALL -E file`,
  KB 29387).
  Methodology notes for an eventual live session (from the
  job_depends TechDocs page + KBs 135770/14195): `job_depends -c` reports current
  condition satisfaction and `-t -e -F -T` projects start times through
  the Application Server; extended calendars materialize into
  ujo_calendar on save (~365 days) — autocal_asc preview/export is the
  primary Q8 oracle, job_depends the consumption check. Tests 1675 →
  1681 (suite green; ruff check + mypy clean).
- DL-59 Demo-grade determinism: open-composition refusals become pinned
  defaults (2026-07-30; project decision — no live AutoSys instance is
  available to this project, so the DL-58 runbook probes cannot run; the
  priority is a FULLY FUNCTIONAL deterministic scheduler that loads
  ordinary JIL and demos the CLI, accepting documented divergence from
  AutoSys where a corner is open). Policy: in the scheduler path, an
  OPEN question about how documented features COMPOSE gets a documented
  deterministic default, never a refusal — refusing an open corner blocks
  a whole estate run (preflight ERROR) over a calendar the vendor
  accepts. CalendarRuleError is narrowed to what genuinely cannot be
  interpreted: unknown tokens, doc-defective tokens (the vendor's own
  text is garbled — inventing semantics would be a guess, and the folk
  tokens were proven nonexistent in DL-57), missing holcal/cyccal
  dependencies, and degenerate shapes (nowhere-to-walk, walk-cap).
  Changes: (1) Q8b — nonzero adjust + N/W/P replacement no longer
  refuses; pinned to the SEM-38 pipeline order as-is (disposition
  replaces, then the uniform blind adjust shifts every survivor =
  replace-then-shift; the runbook probe pair's signature (Aug 15, Aug 18)
  is pinned in test_q8b_*, so a future vendor diff reads straight off the
  discrimination table). (2) Q8d(iv) — all-exclusive compound rules
  (xtue|xwed) no longer refuse; pinned to literal boolean include
  evaluation, the same complement-in-AND algebra mixed-polarity rules
  already used (near-universal accepted as the honest algebraic reading;
  _has_positive_leaf deleted). Both questions stay OPEN (# PENDING
  markers on the defaults; the DL-58 runbook probes remain the closers if
  access ever appears). NOT changed: doc-defective token refusals
  (WORKDXnn/CWEK*), Q8c/Q3c/Q6/E8 (already deterministic defaults), and
  every vendor-invalid refusal. Demo path documented: `rehearse` skips
  identity preflight; `run --machine-policy local-eligible` maps foreign
  machines onto the local box; calendars ride in via a read-only
  `autocal_asc -E` export loaded alongside the JIL. Tests 1681 (two
  refusal tests became behavior pins; suite green, ruff + mypy clean).
- DL-60 Q9 resolved at the [F] tier; five export-format compatibility
  fixes (2026-07-30; evidence = one observed `autocal_asc` export sample,
  recorded at the new [F] confidence tier — one field observation, not
  verified against TechDocs and not re-verified; every fixture name and
  date in the repo is synthetic). Observed facts (now [F] in SEM-36/37): the
  export writes `extended_calendar:` (Q9's central question; ext_calendar:
  kept as input leniency), fixed attribute order (extended_calendar,
  description, workday, non_workday, holiday, holcal, cyccal, adjust,
  condition) with EMPTY-VALUED KEYS EMITTED and `adjust: 0` always
  present, workday as comma day codes plus the literal `all`, condition
  token case preserved as authored (no normalization -- F1's
  preserve-what-appeared is exactly right), condition grouping written
  with BRACES `{MNTHD#7} | {MNTHD#21}` where TechDocs shows parens
  (parens stay accepted, so no behavior rides on this read), `WORKD#L`
  in use, `holiday: S` with holcal EMPTY (against the 12.1 prompt-flow
  wording), standard rows stamped `mm/dd/yyyy 00:00:00`, and a cycle
  record as name/description plus repeated start_date/end_date pairs
  (the CycleIR shape verbatim). Five gaps fixed -- each one would have
  refused an ordinary export (the DL-59 functional goal made concrete):
  (1) standard_rows accepts
  HH:MM:SS tails (seconds truncate; ticks are minute-grained);
  (2) `workday: all` = every day; (3) `{`/`}` tokenize as `(`/`)`
  (synonyms, mixed pairs tolerated); (4) `#L` (= from-end-1) extended
  uniformly across the ordinal families (workd/weekd/wekr/week/mnthd/
  mmm/ddd/cycl -- CWRK/Cddd/CWEEK already had it); (5) the holcal
  requirement narrowed to O/N/W/P (S is a pass-through consuming no
  holiday set). ast_jil's Q9 marker retired; a synthetic clone of the
  observed shapes is pinned end-to-end incl. byte-identical F1
  (`test_q9_*`). Caveats recorded in §9: one sample, AE version
  unpinned, not re-verified; KB 29387's export command stays the
  byte-exact re-verification if a live instance becomes available.
  Tests 1681 → 1687.
- DL-61 `dsl41 viz --whole-graph` (2026-08-07). DL-35 dropped the
  monolithic chart from the CLI (unreadable for a whole estate) but kept
  `to_mermaid` as the public single-chart function; this re-exposes it as
  an opt-in CLI flag — one bare Mermaid flowchart body instead of the
  Markdown report, suitable for piping into mermaid-cli or pasting into a
  live editor. Interactions: `--collapse-threshold` and `--elk` apply
  (`to_mermaid` grew the `elk` keyword rather than the CLI importing the
  private frontmatter constant); `--direction auto` falls back to LR (the
  per-component heuristic has no meaning here); `--include-singletons` is
  moot — the whole graph has no appendices, so standalone jobs always
  render. The DL-35 report stays the default output. Tests 1687 → 1690.
- DL-62 SEM-35 timezone-name resolution: the ujo_timezones ladder +
  `--timezone-map` (2026-08-07). Problem: `timezone: Zurich` (a vendor
  city entry) refused at preflight — the runner resolved names through raw
  zoneinfo only, but the vendor resolves them through the OS *and* the
  instance's ujo_timezones table (TechDocs 12.1, timezone attribute +
  autotimezone command pages, both [V]; SEM-35 carries the condensed
  quotes). Port, in vendor order: (1) zoneinfo, case-insensitively and
  with -/_ folded (the attribute is "not case-sensitive"); (2) the alias
  map given as `--timezone-map` on run/rehearse — the `autotimezone -l`
  listing verbatim (Entry/Type/Zone rows; bare `name zone` pairs also
  accepted), Alias/City chains chased at most FIVE reads (the vendor's own
  bound), cycles refuse; (3) POSIX fixed offsets (`GMT+5`, `"IST-5:30"`)
  west-positive per the TZ-variable syntax, with a preflight WARN spelling
  the sign convention; POSIX strings WITH dst rules refuse — approximating
  vendor DST rules would silently shift ticks. Without a map, a city name
  falls back to a documented deterministic default in the DL-59 spirit:
  the UNIQUE zoneinfo zone whose final path component matches (Zurich →
  Europe/Zurich; the docs' own City rows — Vancouver → Canada/Pacific,
  Denver → US/Mountain — follow this shape), surfaced as a preflight WARN
  naming the assumed zone; ambiguous components (Indianapolis) refuse and
  list candidates. A supplied listing is complete estate truth (`-l`
  lists ALL entries), so the city default is OFF when a map is given —
  a name missing from it would fail on the real instance too. Mechanics:
  `resolve_timezone`/`parse_timezone_map` in runner.py; Scheduler and
  preflight take `tz_aliases`; `--timezone` (base zone) resolves through
  the same ladder; unresolvable stays a preflight ERROR, now naming the
  applicable remedy. The runbook grows a read-only `autotimezone -l`
  capture step for any migrated estate. Tests 1690 → 1704.
- DL-63 nightbank operator-training sandbox (2026-08-09). New top-level
  `examples/nightbank/`: a synthetic bank overnight estate ("Alpenbank
  Global Overnight") for training operators on the REAL engine — TUI,
  sendevent/query, supervisor, scheduler, restart/resume. Boundary
  decision: the engine is untouched; all fakery lives estate-side (a
  `fakework` worker moving marker files + a scripted `incidents.conf`).
  Clock manipulation was considered (a ScaledClock with a fixed-point
  clock-id encoding) and REJECTED for v1 — real clock, real processes;
  compressed play comes from the estate being condition/file-driven with
  launch-stamped anchors (`~{$X}~` placeholders + a per-night properties
  file, the existing preprocessor). libfaketime remains the documented
  external route if wall-time compression is ever needed. Two profiles
  from one topology: `estate/small` (~80 jobs, hand-written) and
  `estate/bank` (~520 jobs, `generate.py`, byte-stable regeneration,
  pinned by test). Corpus hygiene: fully synthetic, repo-only, not
  packaged. Smoke coverage in tests/test_nightbank_example.py (lint
  zero-findings gate, full-night VirtualClock rehearsal to the SOD flip
  with operator actions as scripted events, QUE_WAIT/priority pins).
- DL-64 operator-visibility batch from the first nightbank training night
  (2026-08-09). Four decisions, one origin: a real operator ran the DL-63
  sandbox and got silently confused. (1) SEM-10 STARTJOB refusals leave a
  START_REFUSED trace record naming FORCE_STARTJOB — for the EXPLICIT
  event only; internal condition-edge probes stay silent (they hit the
  same gates constantly, and vendor parity stays: the event is accepted,
  the start is declined — we record the decline, we do not error it).
  (2) The TUI header clock renders UTC, matching the engine's naive-UTC
  time basis (ss9): one time base on screen, never the viewer's local
  wall; the FORCE_STARTJOB key (`f`) joins the visible footer — the
  training night showed `s` visible + `f` hidden is exactly backwards.
  (3) New read-only control verb `spec job` (+ `dsl41 query spec`):
  serves the preserve-rendered post-placeholder JIL block the running
  engine loaded (rendered at load from the parsed AST via
  render_statement; ControlServer takes an optional spec_texts map, so
  embedders without source text serve jil:null rather than refusing).
  The TUI shows it as a job-details popup (`d`/Enter). (4) Pane geometry
  stays keyboard-only: `m` maximize-toggles the log tail, `]`/`[` and
  `}`/`{` nudge the two splits; no mouse splitters (value/effort, and
  textual-serve keeps keyboard parity in the browser). Tests 1710 → 1716
  (SEM-10c refusal records on both bisimulation paths, spec verb wire
  shape, _spec_texts rendering, spec popup pilot, geometry pilot); the
  SEM-32 dead-tick test now expects the tick's START_REFUSED record --
  its "no trace at all" wording was a proxy for the pinned no-arm, which
  stands unchanged.
- DL-65 navigation & observability at estate scale (2026-08-09). Origin:
  bank-profile training (518 rows) made the flat jobs table unnavigable,
  and a deliberate systemctl/systemd API review supplied the orthogonal
  ideas worth stealing. TUI: the jobs table renders the BOX TREE from a
  new per-job box_name/job_type in the ss10 status response (space folds
  the selected box, z all; a folded box row carries its hidden-descendant
  count and a red problem tally -- a fold must never swallow a FAILURE
  silently); `/` incremental name filter (substrings AND'd; Enter keeps,
  Esc clears); `v` view cycle all -> problems -> active; filtered and
  non-all views are deliberately FLAT (a match inside a folded box must
  never be invisible); the event console moved from `/` to `:`. Table
  rebuilds wholesale only when row ORDER changes, else per-cell updates;
  border titles are rich Text (markup injection via user filters).
  Control plane, each a systemctl analog: `deps job` = list-dependencies
  --reverse (upstream from _entity_keys, downstream from the oracle's
  edge-trigger index -- the blast radius before KILLJOB/ON_HOLD);
  `timers` = list-timers (oracle pending timers + Scheduler.upcoming(),
  one due-ordered list); status gains `spec_drift` = the daemon-reload
  hint INVERTED (lazy 15s sha256 re-check of the loaded input files;
  there is no reload -- the TUI subtitle says the running catalog no
  longer matches the disk, cold restart to adopt); CLI predicates
  `is-success`/`is-failed` = is-active (print status, exit 0/1, shell
  glue). The details popup gains needs:/blocks: lines and a log tail
  (the `systemctl status` composite). Deliberately NOT copied:
  daemon-reload (cold-restart doctrine stands), drop-in overrides (the
  properties file is the one templating layer), mouse splitters.
  `analyze` (blame/critical-chain over the WAL) is noted as the next
  standalone unit, not built here. Review round (standing flow, run as a
  parallel five-agent workflow: two test writers + three adversarial
  lenses): 13 breadth tests green first pass; 10 findings, all fixed
  same session -- 4 MAJOR: rebuilds bounced the selection through row 0
  wiping the log tail on every filter keystroke (restore-guard in the
  highlight handler); an empty filtered view left keyed verbs aimed at
  an invisible job (selection cleared); CHANGE_STATUS refused the
  "JOB^INST" pseudo-entity so the cross-instance runbook play could
  never fire (declared-xinst suffixes now pass the gate, SEM-07); the
  folded-box rollup and the problems view disagreed on QUE_WAIT one
  keystroke apart (single _is_problem predicate). MINORs: deps
  classified by key-string sniffing (atom-type walk now; a job legally
  named 'g:x' no longer reads as global x), deps omitted box
  containment (box_name/members served + popup lines -- condition edges
  are not a box's blast radius), fingerprint re-read raced the loader
  (hashes the loaded bytes, inside the exit-2 guard), flat views wore
  stale fold decoration, }/{ resized the wrong node after the layout
  change. Tests 1716 -> 1731.
- DL-66 recovery, artifact, and truthfulness hardening (2026-08-09). An
  independent adversarial review of the DL-63/64 state (8 findings)
  drove one batch; every High/Major addressed, two defers recorded.
  (1) The nightbank SOD flip is IDEMPOTENT under rerun (empty pending/
  beside populated current/ = already-flipped no-op) and destroys last
  (every pre-rmtree step is a rename; a crashed flip leaves
  previous.old/ for the next run's cleanup) — an engine-down window
  between the flip and the SOD_DATE publish reruns the whole JIL chain,
  and rerunning must not rotate the fresh day away. (2) Run roots are
  self-contained artifacts: manifest/ holds the post-placeholder JIL
  (render_preserve, byte-exact) + manifest.json (version, catalog hash,
  input sha256s, original paths, launch options), written before
  baselining, never repainted onto a used root. DEFER: catalog-hash
  relocation-independence (SourceSpan.file is hashed; changing that
  orphans every existing journal's resume gate — own migration unit).
  (3) Secure defaults: run roots 0700 (tightened at resume too),
  journal/spool/std files 0600; the nightbank launcher matches
  (0700 run tree, 0600 properties/profile). (4) Each estate ships its
  OWN incidents.conf (bank targets are per-asset-class; generate.py
  emits it; the launcher copies from the estate dir); bank gains
  OPS_XINST_DEMO_C (519 jobs); the runbook's bank section states the
  real mapping instead of "same incidents". (5) nightbank drop-file
  resolves the ONE live night by socket probe (ambiguity refuses and
  lists; "latest directory" is not "the live night") and refuses paths
  escaping data/; README deletion advice now says stop the night first.
  (6) Runbook truth: OFF_ICE does not re-run the iced job (SEM-20
  reoccurrence documented; retrigger or FORCE); the xinst play works
  since DL-65's CHANGE_STATUS fix. (7) query status --brief (one line
  per job) + explain global atoms carry the effective value (actual;
  null = unset) in CLI and TUI. DEFER: full table/NDJSON output modes
  and trace windowing — the analyze unit's territory. (8) Claims are
  CI-substantiated: incident targets exist in their estate (both
  profiles), RUNBOOK job names exist in a catalog (regex contract),
  README's viz/report/uc acceptance runs in CI; the bank byte-stable
  pin now covers incidents.conf. Tests 1731 -> 1735.
- DL-67 the zoomed log is a real pager; operator verbs unreachable while
  paging (2026-08-09). The report that triggered it: `/` with the log
  maximized opened the DL-65 tree filter — an Input the maximized view
  HIDES — so keystrokes vanished into an invisible widget and the tree
  came back mysteriously narrowed. The hole generalized: every app-level
  key stayed live under maximize, so less/vim muscle memory aimed at the
  VIEW fired estate verbs — `k` (scroll up) sent KILLJOB at the selected
  job, `f` (page forward) sent FORCE_STARTJOB, `q` (leave the pager) quit
  the app. Decision, per the category convention (less/vim/tig/k9s all
  agree `/` in a full-screen log searches the log — find-in-what-fills-
  the-screen, so the key's meaning is stable even though its target
  changes): the log tail WITH FOCUS is a less-style pager, and focus is
  the mode switch. Textual consults the focused widget's bindings before
  the app's, so one mechanism both provides the pager verbs and shadows
  the operator verbs; keys with no pager meaning ring the bell exactly
  as less does; `m`/`o`/`r` and the pane-resize keys pass through
  deliberately; a binding-drift guard test keeps the shadow/allowlist
  partition total so a future app binding cannot silently leak into the
  pager. Paging never mutates the estate. Mechanics: `m` maximizes the
  log PANE (tail + prompt line — the prompt must render inside the
  maximized subtree, which is exactly what the tree filter got wrong);
  search is regex with smartcase, all matches reverse-video, `/`/`?`/
  `n`/`N` in the less directions with wrap; `&` shows only matching
  lines as a VIEW of the buffer (appends join it, empty submit restores,
  a broken regex holds the prompt open with the error on its border);
  follow is pinned-at-bottom — scrolling up pauses ([paused] in the
  title), F/G/End resume; buffer, filter view, and widget cap at 10k
  lines in lockstep (the tail grew unbounded before). ESCAPE_TO_MINIMIZE
  is OFF: textual swallows escape before any binding runs while a widget
  is maximized, which would make escape-in-the-prompt exit the pager;
  escape is owned by explicit bindings instead (prompt: cancel; log:
  leave), and the tree filter/console focus actions are guarded under
  maximize as defense in depth. Found while building: the widget-
  inherited relative scrolls move against scroll_TARGET, which goes
  stale across a viewport resize (the small pane's scroll_end left a
  target past the maximized pane's max, so `k` only shaved the phantom
  overshoot; they also animate, which reads as "not at the end" to the
  follow logic) — pager motion snaps from the real offset; the tail key
  is (stream, path), was path-only, so `o` retitles even when out and
  err resolve to the same file. Deliberately NOT done: live incremental
  log search (a per-keystroke restyle of a 10k-line buffer; less is
  submit-driven and so is the pager — the tree filter stays live, its
  cost model is rows not lines), and shelling out to real less via
  App.suspend (dies under textual-serve — E3 web-posture parity).
  Tests 1735 -> 1742 (pager suite: test_runner_tui.py section 5).
- DL-68 trigger visibility: what started this job, what starts the next
  one (2026-08-09). The gap: a running job's trigger was not attributable
  in the TUI — the engine's queue entries carry provenance (source in
  {scheduler, adapter, control, reconcile}, ss7 input records) but the
  oracle collapsed a scheduler calendar tick and an operator sendevent
  into the identical cause "STARTJOB event", and nothing forward-looking
  said what would fire next or why. Five units. (1) Provenance
  thread-through: Event grows a `source` field the engine stamps at
  enqueue (the WAL input record already persisted it); event-driven start
  causes carry it — "STARTJOB event (scheduler)", "FORCE_STARTJOB event
  (control)" — while internal/synthetic dispatches without a source keep
  the old format; JobRuntime.started_by records the MOST RECENT actual
  start's trace cause verbatim (source-tagged events, condition edges
  "status of X changed to ...", box starts, OFF_HOLD, resources-freed —
  one mechanism, set where every start funnels), and the status verb
  serves it. Replay threads the recorded source back through re-injection,
  so a resumed run re-derives byte-identical causes and started_by; the
  bisim harness injects source=None deliberately — trace identity is
  pinned over inputs INCLUDING source, and oracle-direct scripts carry
  none. Cause-string format change: old pinned trace tests updated where
  a source is now attached. (2) A TUI triggers view on key `t`, backed by
  the existing timers verb, with countdowns to each due instant. (3)
  Filewatch visibility: synthesized "filewatch" rows in the timers verb
  plus a "watching" status. (4) The armed latch (SEM-32/DL-54) rendered
  as flag "A" in the jobs table and listed in the triggers view. (5) The
  details popup gains started-by / next-tick / pending-timers / armed /
  watching lines. Tests 1742 -> 1745 (unit 1: scheduler/control cause
  tags, status verb field, replay determinism).
- DL-69 Q3d opened — does ON_ICE discard a latched tick (2026-08-10).
  DL-54's adversarial round pinned "a pre-existing arm survives
  ON_ICE/OFF_ICE untouched" without a citation. Writing the skip-a-day
  operator drill (nightbank RUNBOOK exercise 13) surfaced the
  consequence: the sendevent set has no latch-discharge verb precisely
  because of this pin — if the vendor instead discards the queued start
  on ICE, ON_ICE *is* that verb, and a stale tick could no longer start
  a job on a post-OFF_ICE condition edge (the current pinned behavior,
  in tension with SEM-20's reoccurrence rule). Registered per the
  open-questions discipline instead of living as a runbook aside:
  `# PENDING: Q3d` at the oracle's OFF_ICE handler, [?] residue note on
  the SEM-32 pin, live discriminator protocol in
  docs/live-instance-runbook.md (Q3c's shape, standalone job, ~3 min).
  The survive-pin stands as the deterministic default; a flip clears
  `armed` in ON_ICE (SCHED_DISARM record) and amends SEM-20/32.
  Doc + comment only — no behavior change, tests unchanged.
- DL-70 viz gains --fixed-scale and a self-contained --html report
  (2026-08-11). Problem: on a bank-scale estate, rendering the DL-35
  report to HTML is unreadable — Mermaid's default useMaxWidth=true lets
  the host fit-to-width each chart independently, so every workflow lands
  at a different scale; only the ELK layout held up. Re-opens DL-35(8)'s
  parked "alternative renderers, revisit here" clause. Decisions:
  (1) --fixed-scale emits per-chart frontmatter `flowchart.useMaxWidth:
  false` (natural size, uniform scale in any frontmatter-honoring
  renderer); _ELK_FRONTMATTER became _frontmatter(elk, fixed_scale) with
  `layout:` deliberately ordered before `flowchart:` so elk-only bytes are
  unchanged — zero pre-existing test assertions edited is the byte-safety
  proof. (2) dsl41 viz --html emits ONE offline page (file://, zero
  network): full report parity via the new viz._report_content that both
  to_markdown (byte-identical, verified by empty small-estate diff) and
  viz_html.to_html format — parity drift between emitters is the
  no-silent-loss failure mode, so neither re-walks the graph. Charts
  render in-browser: sources ship as one JSON script with every "<"
  escaped to \u003c (neutralizes </script and <!-- in a single rule); the
  page drives mermaid.render itself — sequential async loop, progress
  counter, per-chart try/catch so one bad chart cannot kill the page;
  hand-rolled pan/zoom (resizes the SVG layout box, no CSS transform, so
  overflow:auto scrollbars stay coherent; per-chart +/-/1:1 toolbar
  overlaid on a non-scrolling chartbox wrapper, revealed once the chart
  renders — wheel-only zoom was invisible UI). Page defaults layout=elk +
  useMaxWidth=false + maxEdges=10000 + maxTextSize=1e7 via
  mermaid.initialize — the latter two are secure-listed (frontmatter
  CANNOT raise them) and the bank estate exceeds both vendor defaults
  (500 edges / 50k chars). (3) Vendored assets in the wheel (user
  decision): src/dsl41/_vendor/ carries mermaid 11.16.1 dist/mermaid.min.js
  byte-exact (MIT, sha256-pinned in tests, inline-safe: no </script
  substring) and an esbuild IIFE bundle of @mermaid-js/layout-elk 0.2.2
  (MIT) + elkjs 0.9.3 (EPL-2.0) — layout-elk publishes ESM-only, which is
  not single-file-inlinable (103 chunks, dynamic imports);
  scripts/vendor_mermaid.sh pins all versions and stamps an attribution
  banner (elkjs ships no /*! comments of its own). The elk IIFE global is
  a namespace — the page uses elkLayouts.default || elkLayouts. Wheel
  grows 290 KiB -> 1.74 MiB (vendor deflates to 1.45 MiB; 5.1 MiB raw JS
  committed to git, marked linguist-vendored -diff). License posture: first third-party
  code redistributed by this AGPL-3.0-only + commercial project — mere
  aggregation (browser-side assets copied into an output artifact, never
  linked with the Python code); EPL-2.0 §3.2 notice/source obligations
  discharged by THIRD_PARTY_LICENSES (repo root, license-files, wheel
  dist-info) + the page's head comment; flagged for deliberate sign-off
  before the next PyPI release. (4) Flag interactions, DL-61 style
  (absorb, never error): --html implies --elk/--fixed-scale (page
  defaults; passing them is a no-op), --direction/--collapse-threshold/
  --include-singletons shape charts as in the report, --whole-graph
  composes into a single-chart page (legend kept — an HTML page is a
  terminal artifact, unlike the pipeable bare chart), -o recommended in
  help (~5 MB page), stdout still allowed, exit codes unchanged. Layout
  name is `elk` — layout-elk 0.2.2 silently falls back to dagre for the
  README-documented `elk.layered`. Deliberately NOT done: no CDN or
  --js-dir variants (offline is the point), no vendored pan/zoom lib
  (~70 lines hand-rolled), no dark theme, no markdown->HTML conversion
  (deps stay lark/pydantic/typer). Only a browser can verify the ELK
  render, pan/zoom feel, and error isolation — headless node verified
  payload evaluation, global shapes, and loop syntax; mermaid.parse needs
  a DOM (DOMPurify). Tests 1771 -> 1788 (fixed-scale frontmatter x4,
  vendor integrity x2, html emitter/CLI x11; nightbank acceptance
  extended in-place).
- DL-71 viz gains --explore: an interactive cytoscape.js navigation page
  (2026-08-11). Problem: DL-70's --html report renders faithfully but a
  bank-scale whole-graph chart is a static hairball — the operator task
  is "find this job, see what feeds it, hide everything else", which no
  pre-rendered SVG can serve. Feasibility was proven on a throwaway
  prototype against the nightbank estate (523 nodes / 420 edges / 63
  compound boxes; ELK layered layout in 0.7 s headless Chrome; focus
  re-layouts instant). Decisions: (1) `dsl41 viz --explore` emits ONE
  self-contained offline page (file://, zero network — DL-70's posture),
  a third output mode beside the report and --html. It is a navigation
  LENS, not the artifact of record: the Markdown/HTML report keeps the
  appendices and stays the no-silent-loss carrier; explore must still
  surface every edge annotation via a click-details panel (node: kind,
  schedule, command/watched path, owning box; edge: via, lookback,
  class, mapping row, full assumption text — assumptions are never
  truncated, mirroring the report's content policy). (2) Data path: new
  viz_explore.py emits cytoscape elements JSON straight from
  DerivedGraph — no Mermaid text anywhere. Nodes carry id/label/kind/
  schedule/detail + parent from box_tree.parent (boxes = cytoscape
  compound nodes); endpoints outside the catalog (externals "name^INST",
  global names) synthesize EXT nodes (class `global` added when
  via=="global"); edges carry via/lookback/cls/mapping_row/assumption
  with cls as the style class. Pure function _elements(graph) for
  emitter tests; to_explore_html(graph, *, title, direction) wraps it.
  JSON embeds with the DL-70 rule (every "<" -> \u003c). (3) Vendor
  bundle #2, built by extending scripts/vendor_mermaid.sh: esbuild IIFE
  of cytoscape 3.33.1 (MIT) + cytoscape-elk 2.3.0 (MIT) + elkjs 0.9.3
  (EPL-2.0, same pin as the mermaid bundle) + cytoscape-context-menus
  4.1.0 (MIT; its CSS inlined into the template), global `cyBundle`,
  ~1.9 MiB minified. cytoscape-elk drives elk.bundled.js on the MAIN
  thread — no Worker, no fetch, so the offline invariant holds. Same
  vendor invariants, same tests shape: no `</script` substring,
  attribution banner, size floor (esbuild output is not
  byte-reproducible). elkjs is deliberately DUPLICATED across the two
  bundles — they are independent artifacts; a shared-chunk build couples
  their upgrade cadence for ~500 KiB deflated, not worth it. License
  posture unchanged: adds MIT payloads only, EPL-2.0 already discharged
  by THIRD_PARTY_LICENSES (new source URLs appended); re-flag for the
  same deliberate sign-off before the next PyPI release. (4) Page
  behavior (prototype-validated): ELK layered, direction from
  --direction (auto/LR -> RIGHT, TD -> DOWN),
  hierarchyHandling=INCLUDE_CHILDREN, nodeDimensionsIncludeLabels; DL-35
  visual grammar re-expressed in the cytoscape stylesheet (CMD
  round-rect, FW diamond, BOX compound container, EXT dashed grey;
  edges: exact solid, assumed dashed amber, redesign dotted red; labels
  via + [lookback]). Toolbar: substring search over node names (Enter ->
  highlight + fit; hits hidden by a previous focus are un-hidden — a
  search that cannot find a hidden node is a lying search), show-all,
  fit, re-layout-on-focus toggle, visible/total stats line. Node context
  menu: select fan-in / fan-out (direct = incomers/outgoers), fan-in /
  fan-out tree (predecessors/successors), both trees; focus this +
  neighbours; focus = hide non-selected (keeps compound ancestors —
  members without their box do not render) then ELK re-layout of the
  visible subset (toggle off = fit only); hide this node; show all; fit
  (last two also on the core menu). Prototype cleanups owed: edge labels
  hidden below a zoom threshold (they overlap at fit zoom), drop
  deprecated width:'label' styling, leave wheelSensitivity at default.
  (5) Flag interactions, DL-61 style (absorb, never error): --explore
  wins over --html if both are passed (explore IS an html page);
  --whole-graph / --elk / --fixed-scale / --collapse-threshold /
  --include-singletons are no-ops under --explore — the page is always
  the whole graph, always ELK, always natural scale, boxes never
  collapse (navigation replaces collapsing), singletons always present
  (search must find them); --direction maps per (4); -o recommended in
  help (~2 MiB page), stdout allowed, exit codes unchanged.
  (6) Verification posture as DL-70: emitter tests pin elements JSON
  (parent mapping, EXT synthesis, edge classes, escaping), CLI tests pin
  flag absorption + -o, vendor tests pin the invariants, nightbank
  acceptance extends in-place; only a browser verifies render/menu/
  focus feel — the prototype's playwright-over-installed-Chrome smoke
  (throwaway venv, channel="chrome") is the recorded dev technique, not
  a test dependency. Implementation landed as specced; the dev smoke
  re-ran green against the emitted bank-estate page (523/420/63, ELK
  0.7 s, menu/focus/search/details verified, zero console errors, zero
  network requests). An adversarial review pass hardened the unit:
  edge ids prefix away from the shared cytoscape id namespace (a job
  named "e0" would have silently swallowed an edge at init -- no-silent-
  loss), template substitution became single-pass in BOTH html emitters
  (chained .replace() let a marker-shaped job/file name splice the
  vendor bundle into the embedded JSON and kill the page -- latent in
  DL-70 too), menu item ids no longer duplicate toolbar DOM ids, the
  vendor script's `! grep` invariants became real gates (POSIX errexit
  exempts `!` pipelines) and the CSS drift check diffs the whole
  normalized block instead of grepping lines. Tests 1791 -> 1811
  (vendor integrity x1, elements emitter x9, page x6, CLI x3,
  determinism-across-hash-seeds x1; marker-injection regression added
  to test_viz_html.py; nightbank acceptance extended in-place).
  Deliberately NOT done: expand/collapse of compound
  boxes (cytoscape-expand-collapse drags in an undo stack; revisit here
  if bank-scale boxes drown the canvas), minimap (cytoscape-navigator),
  dark theme, view-state persistence, regex search, replacing the
  mermaid report (both stay: report = record, explore = lens), CDN or
  --js-dir variants (offline is the point).
- DL-72 deduplication and vocabulary: one process-identity module, one
  status-letter table, one connected-components implementation, one set of
  public names across the viz emitters (2026-08-12). Problem: four
  near-copies had accumulated, and copies drift. The proof was already in
  the tree — runner_wrapper.durable_write created its temp file 0o600
  ("sol #3: owner-only"), runner_supervisor's copy of the SAME helper
  created it 0o644; the wrapper's copy carried the richer docstrings, the
  supervisor's had been stripped. Decisions: (1) src/dsl41/runner_procid.py
  holds one copy of durable_write / durable_write_json / current_boot_id /
  proc_start_token / start_tokens_match / verify_alive / killpg_quiet /
  utc_now_iso, with the wrapper's docstrings kept and the mode unified on
  the TIGHTER 0o600 (the run_root is 0o700 already, so nothing widens).
  It is STDLIB ONLY like its two callers — the DL-42 extraction boundary
  reads "nothing from dsl41, nothing third-party", and a sibling stdlib-only
  module is inside it, not a breach. The wrapper and the supervisor are run
  BY FILE PATH, so they import it as a plain top-level module (`import
  runner_procid`), which resolves via sys.path[0] — except under
  PYTHONSAFEPATH=1, which strips that entry; both therefore prepend their
  own directory first (verified: without the guard, `PYTHONSAFEPATH=1
  python runner_wrapper.py` dies at import). That guard is conditional and
  self-undoing — prepend only what is missing, remove it again once the
  import is done — because those two files are ALSO imported as ordinary
  package modules: the engine reads __file__ and SPEC_VERSION off them, so
  an unconditional `sys.path.insert(0, ...)` would leave src/dsl41 at the
  front of sys.path in every `dsl41 run` / `supervise` / TUI process
  (twice, ahead of the stdlib and of cwd) and shadow top-level names — ir,
  cli, viz, dsl, oracle, conditions, derive, lint, equiv, autocal, runner,
  placeholders — for the whole process, including a consumer's own module
  of one of those names. Rejected: appending instead of prepending (still
  leaves a library's package directory on the importer's sys.path forever)
  and branching on __package__ to import dsl41.runner_procid in that case
  (breaks the stdlib-only import test and mypy). sys.path now ends exactly
  as CPython handed it over, in both invocation modes; the only residue is
  a second module object under the top-level name in engine processes,
  which is harmless (runner_procid is pure functions, no module state).
  Pinned by test_importing_the_engine_leaves_sys_path_untouched. mypy maps
  the same file as dsl41.runner_procid and cannot also see it under its
  top-level name, so the first cut of this unit carried
  `# type: ignore[import-not-found]` on both by-path imports — which cost
  every helper call in those two files its static types (they were locally
  defined and typed before), in the two files that kill process groups and
  guard against PID reuse. RESIDUE, CLOSED in post-landing review of this
  branch. The two repairs weighed here — a hand-maintained stub (the
  duplication this entry removes) and mypy_path surgery ("source file found
  twice") — are indeed worse than the disease; a third is not. The import
  splits in two: `if TYPE_CHECKING: from dsl41.runner_procid import ...`
  names the module mypy already maps, `else: from runner_procid import ...`
  is what actually runs, and the sys.path guard above is unchanged because
  the runtime half still needs it. Verified: mypy reports both argument
  types of a deliberately wrong `verify_alive("not-an-int", 42)` in
  runner_supervisor.py, and said nothing about it before; the runtime works
  in all three modes that matter (spawned by file path, by file path under
  PYTHONSAFEPATH=1, imported as an ordinary package module). The
  import-graph tests were taught the distinction rather than loosened: they
  read RUNTIME imports (the body of an `if TYPE_CHECKING:` does not count,
  its `else:` does), so a type-time alias of dsl41 passes while a real
  runtime import — at any nesting — still reds them, which was checked by
  adding one and watching it fail. One test per file pins the two halves to
  the same helper list, and one runs mypy over both import shapes — in a
  single invocation, never importing mypy into the pytest process — to pin
  WHICH one buys the coverage. That run is the heaviest step in the file and
  it perturbed test_runner_tui.py's pager-follow pilot test into failing 3
  full-suite runs in 13, so a latent race there was fixed with it: two
  assertions read `is_vertical_scroll_end` on the frame the appended buffer
  landed in, and the tail scroll settles a frame later; both now wait for the
  scroll through _wait_for_ui, as the rest of that file does (8 consecutive
  green full runs after, against 10 for the unchanged tree). The
  engine, an ordinary package module, imports dsl41.runner_procid and lost
  its own third _killpg_quiet. Import-graph tests now cover all three files
  (the allowed non-stdlib name is exactly "runner_procid"), plus one test
  per caller that it still works under PYTHONSAFEPATH=1. (2) The status
  vocabulary was spelled out four times; conditions.STATUS_LETTER is now
  DERIVED from _STATUS_BY_KW (the single-character keys ARE the letters),
  dsl.py imports it (_FOLDABLE_STATUS keeps its meaning: everything but
  NOTRUNNING), viz builds _VIA_LETTER from it plus the two vias that are
  not statuses (exitcode -> e, global -> v; Via stays lowercase, no case
  migration), and derive's _STATUS_TO_VIA dict is gone — the mapping is
  status.lower(), now a one-line typed helper so the Via Literal stays
  checked. (3) derive.components(nodes, edges, *, bind_box_members) is the
  one union-find; the box-membership policy that silently differed between
  viz.split_components (binds) and backend_uc._components (does not) is now
  a named argument, and ordering is the caller's business (viz re-sorts by
  descending size in one line; the UC backend wants the function's own
  order). Its contract is "members in nodes order, groups in first-member
  order".
  That pins an ordering the UC backend previously left to chance: its old
  sort keyed on the union-find REPRESENTATIVE's position, which is an
  artifact of edge order (it differs from first-member order on ~23% of
  random graphs; a test now pins wf_a before wf_b on such a shape). No
  corpus output moved. (4) viz/viz_html/viz_explore are three emitters over
  one content model, so the names they share stopped pretending to be
  private: report_content, ReportContent, ChartSection, edge_label,
  LEGEND_CHART, LEGEND_PROSE, LOCKS_PROSE, substitute. The modules stay
  split — the split is genuine. (5) AGENTS.md was an untracked byte-copy of
  CLAUDE.md (title line apart); it is now a tracked symlink (mode 120000),
  so the working agreement cannot fork per agent. Behaviour is unchanged
  throughout: every corpus file's report / uc / viz / --html / --explore /
  --whole-graph / lint / decompile output is byte-identical before and
  after, sem31_xor.jil still refuses to lower with the same message, and
  importing the package changes no process-global state (the sys.path point
  above — found in review of the first cut of this unit, which did leave the
  entries behind, and fixed before it landed). The one deliberate change of
  an unspecified ordering is the UC backend's workflow order in (3), which
  no corpus output exercises. Tests 1808 -> 1812 (PYTHONSAFEPATH x2,
  component ordering x1, sys.path hygiene x1).
- DL-73 IR-F carries conditions as (cond, span) pairs, IR-G edges carry
  their atom, IR-G stops copying IR-F for the display layer; ir_version
  0.1 -> 0.2 (2026-08-12). Problem: three places let a model's shape
  push work onto its readers. (1) Semantics declared condition /
  box_success / box_failure alongside condition_span / box_success_span /
  box_failure_span, six fields rejoined at runtime by an
  `f"{attr}_span"` getattr that no type checker can see -- and a reader
  that wanted the pair had to know the convention. They are now three
  fields of one CondAttr(cond, span) model; _CONDITION_ATTRS and the
  getattr join are gone and JobIR.iter_conditions is an unrolled walk
  over the three attrs (a plain loop needs a Literal-typed local, which
  costs more than it saves). The provenance note stays on CondAttr: a
  Cond node's CondSpan is a char offset into the parsed attribute text,
  the CondAttr's SourceSpan locates the attribute in the file. (2)
  DerivedEdge recorded via + lookback + the owning attr's span but not
  the atom it was derived from, so the UC backend RE-SCANNED the
  consumer's condition to recover an exit-code or global comparison's
  op/value (_split_global_edge, _exitcode_var_condition) -- a search for
  something derive had in hand. The edge now carries the atom; both
  helpers are deleted, and with them the "no recoverable op/value"
  exclusion branch, which can no longer occur. The atom is a deep COPY,
  and `lookback` now takes its copy the same way: IR-G must not alias
  IR-F's mutable AST nodes (the pin external_boundary already carried).
  `via` stays a field (every consumer matches on it, and a property would
  change model equality), as do lookback and source_atom; the model now
  validates that via and the atom's kind agree (global iff GlobalAtom,
  exitcode iff ExitCodeAtom) because that pairing is exactly what the
  backend narrows on -- enforced where it is established rather than
  assumed downstream. Two behaviour changes ride along, both fixes, and
  neither reachable from the corpus (no output moves). (a) The old scan
  matched by producer NAME, so a condition naming one global -- or one
  producer's exit code -- twice with different comparands answered with
  the FIRST occurrence for every edge; each edge now reads its own atom
  (test_compile_twin_exit_code_edges_carry_their_own_comparison). (b) The
  scan only ever looked at `condition`, so an exit-code atom of
  box_success/box_failure origin (M15) compiled to an edge with
  var_condition=None -- the comparison silently dropped -- and the global
  equivalent was recorded as unrecoverable; both now carry their
  comparison. Every M15 exit-code edge found is excluded downstream as
  workflow-spanning and every box-override global is R-classified before
  the branch, which is why no output moves; the loss was real all the
  same. IR-G's serialized shape is free to move:
  ir-design §1 forbids serializing it as authority, and the tree agrees --
  nothing writes a DerivedGraph anywhere (the only model_dump_json is a
  determinism assertion). (3) DerivedGraph.node_meta was a verbatim copy
  of IR-F (job_type, a trigger digest, the command/watch path) carried
  only so viz could take the graph without the catalog. IR-G is an
  analysis product whose every loss is materialized as an annotation; a
  copy is neither, and it made the analysis layer the place to add every
  future display need. NodeMeta, _node_meta and the field are deleted;
  viz.to_markdown / to_mermaid / viz_html.to_html /
  viz_explore.to_explore_html now take (catalog, graph=None) -- the shape
  lint, the UC backend and the decompiler already had -- and the display
  facts are three small functions in viz (job_kind / job_schedule /
  job_detail) that all three emitters share. _schedule_digest moved with
  them (it is real derivation, but display's); _trigger_signature stays in
  derive, where the cadence analysis uses it, and the digest cites it
  rather than iterating its field list -- the digest formats each field
  differently and in its own order, so driving it off the shared tuple
  would need a formatter table to buy nothing. Consequence, accepted:
  IR_VERSION and CatalogMeta.ir_version go 0.1 -> 0.2, which moves
  catalog_hash, which is the resume gate of every existing run journal
  (cli._write_manifest). In-flight journals become unresumable. The gate
  already fails loudly with the mismatch named, and there is no live
  estate. Verification: every corpus file's viz / --html / --explore /
  --whole-graph / report / uc / decompile / lint output is byte-identical
  before and after EXCEPT the catalog hash printed by report and uc.
  Tests +3 (the via/atom-kind validator, the edge-atom copy pin, the
  per-edge exit-code comparison); the seven node_meta tests moved from
  test_derive to test_viz as display-facts tests.
- DL-74 structure: runner.py split along the seams its own test files
  already used, and the Oracle's capacity subsystem extracted as a
  _CapacityPool (2026-08-12). Pure structure — no behaviour and no output
  moves. Problem: runner.py had grown to 3649 lines holding eleven
  concepts, and the evidence that the seams were real was already in the
  tree — tests/test_runner_scheduler.py, test_runner_adapters.py,
  test_runner_journal.py, test_runner_control.py and
  test_runner_lifecycle.py each test one of them. oracle.py had the same
  shape one level down: a contiguous eleven-method block (_demand_vector,
  _can_admit, _acquire, _release, _enqueue_waiter, _sorted_waiters,
  _wake_waiters, _readmit, _dequeue, _cancel_waiter) with its own
  vocabulary — demand vectors, waiters, QUE_WAIT, admission order — inside
  a 55-method class. Decisions: (1) Five new modules, each holding the
  symbols it owns and NOTHING re-exported: runner_clock.py (the ss9 Clock
  protocol, VirtualClock, RealClock), runner_adapters.py (AdapterContext /
  AdapterResult / Terminated / Failed / JobAdapter, FakeAdapter,
  LocalCommandAdapter, FileWatcherAdapter, job_log_paths, _build_run_spec,
  the ss6a Tier-1 SupervisorClient + SupervisedCommandAdapter, and the ss7
  spool ladder _resolve_spool that the detached adapter and resume share),
  runner_journal.py (Journal, read_journal, replay_inputs),
  runner_scheduler.py (Scheduler, _SchedulePlan, _CalCache,
  _scheduler_calendar AND the whole SEM-35 timezone block — timezone
  exists to turn schedule ticks into UTC instants, so it belongs with the
  scheduler), and runner_preflight.py (PreflightItem, preflight,
  resolve_machine, and_success_skeleton, _resource_preflight,
  _local_identity, and the two probe helpers _preflight_local_day /
  _next_eligible_day, which sat in the timezone block's line range but are
  preflight's, not the scheduler's). runner.py keeps the engine loop, the
  run lifecycle (start_run / resume_run / _reconcile) and the ss10
  ControlServer; Engine and ControlServer stay together deliberately —
  they share the single-writer invariant and read better as one file.
  (2) NO re-export shim in runner.py. Every import site — cli.py,
  runner_tui.py, every tests/*.py, the three test drivers — now names the
  module that owns the symbol. A facade would have kept the single door
  the split exists to remove, and it would have left `from dsl41.runner
  import Scheduler` working, which is how a "split" quietly becomes a
  second name for the same monolith. (3) The import graph is a DAG:
  runner_clock is the bottom; runner_scheduler and runner_journal sit on
  it, runner_preflight on runner_scheduler (it consumes resolve_timezone /
  _city_candidates / _DAY_CODES), runner_adapters on runner_clock, and
  runner.py on all five. The two annotation-only edges — Journal in
  AdapterContext, PreflightItem in Journal.preflight — are TYPE_CHECKING
  imports, so the runtime graph stays as shallow as the module list
  suggests. (4) Two symbols the split brief wanted to keep in runner.py
  had to move, because a DAG cannot hold them there: EngineError (every
  module raises it) went to runner_clock.py, the bottom of the graph, and
  catalog_hash / _dsl41_version went to runner_journal.py, whose
  Journal.create is their caller. Keeping either in runner.py would have
  made runner_adapters/runner_journal/runner_scheduler import runner.py
  and closed a cycle. (5) The 182-line module docstring was split with its
  subjects, verbatim: each new module carries the paragraphs describing
  what it now owns (the adapter contract to runner_adapters, the WAL
  bullet to runner_journal, the naive-UTC time basis to runner_clock, the
  scheduler bullet to runner_scheduler, the preflight bullet to
  runner_preflight), each under its own phase header so no ss-xx / DL-xx
  citation is orphaned. runner.py keeps the engine-loop, kill-wins,
  resume, commit-discipline, control-plane and 11d paragraphs and gains
  one paragraph naming where the rest went. (6) oracle.py's capacity
  subsystem becomes a private _CapacityPool in the SAME file (~130 lines
  does not earn a module). The line is responsibility, not vocabulary: the
  pool owns the sized buckets, what each RUNNING job holds, the waiter
  queue and its admission ORDER (demand_vector, can_admit, acquire,
  holds, release, enqueue, sorted_waiters, dequeue); the Oracle keeps
  every status transition and every event emission, which is why
  _enqueue_waiter, _wake_waiters, _readmit and _cancel_waiter stay on it —
  they set QUE_WAIT, INACTIVE or start a job. The `# PENDING: Qr2` and
  `Qr4` markers travel with the code they qualify (Qr6 stays on _readmit).
  Consequence: docs/runner-design.md ss14's "the house layout is flat:
  runner.py (clock, engine, scheduler, adapters, journal, preflight,
  control server)" is superseded by this entry; the design it describes is
  unchanged, only its file count. Verification: every moved line is
  byte-identical (the split was done by line-range extraction, so `git
  blame -C` and `git log --follow` still resolve), the suite is unchanged
  at 1815 passed / 3 skipped with no test added or removed, and mypy and
  ruff stay clean. Test-side churn is imports only, plus the four
  test-docstring pointers whose "runner.py's own docstring" target moved,
  and test_resources.py's white-box reads of the capacity buckets, which
  now go through `o._pool`.
- DL-75 user surface, citations, and a signal-driven architecture review
  (2026-08-12). Four changes, one theme: a reader — of the CLI, of a
  comment, of the tree — should not have to hold a rule in their head that
  the artifact could have encoded. (1) `dsl41 viz` took three booleans,
  `--whole-graph`, `--html` and `--explore`, for what are exclusive modes.
  Eight combinations described five outcomes — report, bare chart, HTML
  report, the HTML single-chart page `--html --whole-graph` composed
  (DL-70(4)), and the explore page — so a precedence rule had to settle the
  rest (`--explore` beat `--html`, `--html` beat `--whole-graph`), and each
  flag's help carried prose about what it nullified. They are replaced by
  one `--format {report|chart|html|explore}` (VizFormat enum): the
  exclusivity is now structural, typer rejects a fifth value, and the
  precedence rule is deleted because the state it arbitrated cannot be
  expressed. The fifth outcome is DELETED, not renamed — this is the one
  capability this unit removes, so it is recorded rather than left to be
  discovered: an offline page wrapping a single whole-graph Mermaid chart
  is the static hairball DL-71 built `--format explore` to replace, and
  `--format chart` still emits that chart for any renderer. `to_html` loses
  its `whole_graph` parameter and branch with it — an unreachable branch
  standing in for a deleted mode is exactly the residue this unit exists to
  remove — and passing the two flags together gets its own refusal naming
  the removal, since routing that user to `--format chart` or `--format
  html` would hand them something else without saying so. That deletion is
  superseded by DL-76, which restores the mode as `--format html-chart`.
  The three
  booleans otherwise survive as hidden options for one purpose only —
  passing one exits 2 naming its replacement, which beats a bare "no such
  option" for anyone with the old command in a script. (2) The rule for the
  shaping options
  (`--collapse-threshold`, `--direction`, `--include-singletons`, `--elk`,
  `--fixed-scale`) is now: refuse ONLY where the chosen format cannot
  deliver the effect. `--elk` and `--fixed-scale` under `--format html`
  stay silent, because that page already lays its charts out with ELK at
  natural scale — the asked-for effect happens, so there is nothing to
  refuse. Under `--format explore` the same test clears two of the four
  Mermaid-shaping options and stops the other two — the first cut refused
  all four, contradicting the rule it had just stated, and was corrected in
  post-landing review of this branch. The page always lays out with ELK and
  always carries every standalone job (search must find them), so `--elk`
  and `--include-singletons` ask for what happens anyway and are accepted
  silently; `--collapse-threshold` and `--fixed-scale` exit 2, each naming
  what it cannot get and why — the canvas never collapses a box, and
  elkLayout runs with `fit: true`, scaling its layout to the viewport,
  which is the very thing `--fixed-scale` asks an emitter to stop doing.
  `--direction` it honors. Refusing a flag whose effect the
  user is getting anyway teaches the user nothing except to distrust the
  refusals. (3) The sources carry ~1600 citation tokens across eighteen
  namespaces, which is the project's core discipline — but two of those
  shapes pointed at review conversations with no in-repo index (`sol #3`
  at five sites, `review X-n` at fifteen). A citation whose target is a
  conversation is a note to the one person who was in the room, so every
  one is deleted and the reason it stood for inlined (all five `sol #3`
  sites meant "owner-only because of what this artifact holds"); where a
  `review` token restated a dossier entry, the dossier citation replaced
  it. The remaining eighteen namespaces get docs/citation-index.md: one row
  per namespace with the token shape as a machine-read regex, plus the
  three collision notes that a reader actually trips on (`Q\d` vs `Qr\d`,
  `M\d{2}` the mapping row vs a review finding, `R\d` the risk register vs
  `R-classified`). Adding a namespace means adding the row first. (4)
  scripts/arch_check.py enforces that, and three more objective
  regressions, in about a second of stdlib with no LLM in the loop: a
  function body duplicated across two modules (the drift class DL-72
  removed), a NEW private cross-module import in src/ (`from dsl41.x import
  _y` — the 13 the tree already had are pinned in scripts/arch_baseline.json,
  so the check catches additions, not history; tests/ is deliberately out of
  scope, because a white-box test reaching into the module it tests is this
  project's normal style rather than two modules coupling, and gating it
  would red CI on an ordinary new test while the only remedy,
  `--update-baseline`, re-blesses every src/ site added in the same commit),
  a citation token that
  resolves to no index row, and a CatalogIR JSON-schema change without an
  IR_VERSION bump (the schema is hashed and pinned; bumping IR_VERSION is
  what licenses a new hash). Those four block: each names a specific way
  the tree got worse. Size — modules over 1200 lines, functions over 120
  lines or 40 branches — is ADVISORY and ratcheted against the same
  baseline, so only new or worsened entries are reported. Taste is not a
  build failure, and a gate that bills you for the past gets muted. CI runs
  it next to ruff and mypy. (5) The gate also prints when a conceptual
  review is due — on any finding, or on more than 800 lines changed since
  the most recent `arch-review/<date>` tag (the branch point if there is
  none). That is the whole trigger: signal, not calendar. There is
  deliberately NO cadence, because reviewing unchanged code on a schedule
  is waste and trains everyone to skip it. The review itself is
  `.claude/skills/arch-review/SKILL.md`, a lens rather than a procedure:
  run the gate first, then look only for complexity the code ADDED —
  duplicated concepts, parallel models, pass-through layers, abstractions
  with one implementation, flag matrices encoding an enum, vocabulary
  re-encoded per layer, data copied across a layer boundary — rank by
  (cognitive load removed) / (cost to change), anchor every finding to
  file:line, and ALWAYS name what is load-bearing and should be left
  alone, because a review that only lists problems reads as "everything
  here is too complex" and gets ignored. Declined findings become log
  entries too, or the next review re-finds them. Verification: every corpus
  file's report / uc / lint / decompile / viz output over all four formats
  is byte-identical to the pre-unit capture except the one provenance
  comment each emitted page carries, which names the flag that produced it
  (`dsl41 viz --html` -> `dsl41 viz --format html`) — the flag spelling
  moved, nothing else did; tests 1815 -> 1841 (the pre-unit tree collects
  1815 passed + 3 skipped, as DL-74 records; the 1821 first written here was
  wrong, corrected in post-landing review), and the corrections recorded in
  (2) above and in DL-72's residue take it to 1845. Twenty of the new ones
  are tests/test_arch_check.py — each blocking check and the advisory ratchet,
  tripped and not-tripped over tiny synthesised trees, plus two deliberate
  assertions on the real tree (the IR-F schema pin still matches, and the
  citation index still parses into its namespace rows).
- DL-76 the single-chart offline page returns as `--format html-chart`
  (2026-08-12). Supersedes DL-75(1)'s deletion. Problem: DL-75 collapsed
  three viz mode booleans into one exclusive enum, which was right, and in
  the same move deleted the outcome `--html --whole-graph` composed —
  counting the old surface as four modes when it was five. The mode was not
  an accident of the flag matrix: DL-70(4) chose it deliberately and wrote
  down why ("--whole-graph composes into a single-chart page (legend kept —
  an HTML page is a terminal artifact, unlike the pipeable bare chart)").
  A capability with a recorded design rationale should not fall out of a
  surface refactor, and `--format explore`, which the refusal pointed at,
  answers a different question: the operator who wants ONE picture of the
  estate to open, print or attach is not the operator who wants to navigate
  it. Decisions: (1) It comes back as a fifth enum VALUE, `html-chart`, and
  never as a flag combination — the exclusivity DL-75 made structural is
  the part that must survive, and it does: typer still rejects a sixth
  value, there is still no precedence rule, and no boolean is un-hidden.
  (2) `to_html`'s deleted `whole_graph` branch is restored from git rather
  than rewritten, and adapted to DL-73's `(catalog, graph)` signature. It
  does NOT come back as a mode parameter on `to_html`: the page shell both
  emitters share (header, legend chart, chart JSON, vendor payloads) is now
  `_page`, and the single-chart page is its own function `to_html_chart`.
  A bool selecting between two bodies inside the emitter is the same shape
  DL-75 removed from the CLI, and keeping the report body at its own
  indentation left every one of its lines byte-identical. `to_html` shrank
  165 -> 148 lines as a side effect. (3) Shaping-flag verdicts, by DL-75's
  rule (refuse only what the format cannot deliver): this format delivers
  all five, so it refuses NOTHING. `--collapse-threshold` and `--direction`
  are passed to `to_mermaid` and shape the chart; `--elk` and
  `--fixed-scale` are page defaults set by `mermaid.initialize`
  (`layout: "elk"`, `useMaxWidth: false`) exactly as under `--format html`;
  `--include-singletons` asks for standalone jobs that `to_mermaid` renders
  unconditionally — the whole-graph chart has no appendix to drop them to,
  which is the same reason `--format chart` accepts it silently. (4) The
  `--html --whole-graph` refusal stays a special case of
  `_refuse_removed_viz_flags`, because the generic loop would send its user
  to `--format html` AND `--format chart`, neither of which emits this
  page; it now names `--format html-chart` instead of announcing a removal.
  Verification: `viz --format html-chart` over the whole lowerable corpus is
  byte-identical to `viz --html --whole-graph` run from the branch point,
  except the provenance comment DL-75 already moved
  (`dsl41 viz --html` -> `dsl41 viz --format html`, 12 bytes over a 5.1 MB
  page) — that comment is left naming the format family, as it did before,
  rather than editing the shared template and moving the `--format html`
  capture with it. Tests 1845 -> 1854 (five emitter, four CLI; the refusal
  test was renamed, not added). The gate's size ratchet was re-blessed for
  two entries and the reason is here rather than in a silent baseline diff:
  a fifth format costs cli.py 16 lines (1576 -> 1592, already over the
  advisory 1200 before this unit) and takes `viz` to 124 lines, over the
  120 advisory. Splitting `viz` was weighed and declined — 55 of those
  lines are typer option declarations, and the only extraction available
  is an eight-parameter dispatch helper that would have to import the
  emitter types at module level, defeating the lazy imports the CLI keeps
  for startup time. Taste is not a build failure (DL-75(4)); a
  nine-parameter pass-through would be a real one.
- DL-77 the explore page was fatally broken in Safari, and nothing in the
  suite ever ran it (2026-08-12). ONE entry for two commits, deliberately:
  the fix and the browser smoke test are the same decision seen from both
  ends — a fix to a defect no test could see is not trustworthy until a
  test that can fail on it exists, and that test exists for no other
  reason. Every citation this unit left in code, tests, CI, pyproject and
  the vendor docs reads DL-77; keep it that way. Root cause:
  cytoscape-context-menus 4.1.0 builds its menu from CUSTOMIZED BUILT-IN
  elements (`class extends HTMLDivElement` + `customElements.define(…,
  {extends: "div"})`). WebKit has never implemented customized built-ins —
  autonomous custom elements only — so `cy.contextMenus(…)` threw
  "Illegal constructor" at page init, and because that call sat ABOVE
  them, every statement below it never ran: the initial ELK layout (so
  #stats read "laying out…" forever and the picture was cytoscape's
  default placement, not ELK's), the toolbar (#fit, #show-all), the
  re-layout toggle and #search — five dead controls. Zoom, drag and the
  click-details panel kept working (cytoscape's own handlers, registered
  above the throw), which is why the page read as SLOW rather than dead.
  It shipped with DL-71 because the suite asserted on the emitted BYTES
  and not one of its 1854 tests ever executed the page: no byte assertion
  can see a runtime throw. Decisions: (1) Vendor a polyfill rather than
  drop the plugin (the user's call — the right-click focus menu is the
  page's primary control, and a hand-rolled replacement is more code to
  own than a pinned 7.6 KiB payload). @ungap/custom-elements 1.3.0, ISC
  — the first ISC payload, so THIRD_PARTY_LICENSES gains entry 7 and the
  vendor README a row; byte-exact npm `min.js`, sha256-pinned like
  mermaid's (esbuild bundles get size floors instead), its own `/*! */`
  banner kept as attribution, pinned/copied/gated by
  scripts/vendor_mermaid.sh. It loads through its own single-pass
  `__DSL41_CUSTOM_ELEMENTS_JS__` marker AHEAD of the cytoscape bundle,
  because the polyfill must own `customElements` before the plugin
  defines anything through it. It feature-detects, so it is inert where
  the browser is native. (2) The standing rule the fix installs: nothing
  essential may sit below an optional plugin. The layout, the toolbar and
  the search are wired FIRST; the plugin registers LAST, inside
  try/except; and a failed optional feature is NAMED in #stats ("context
  menu unavailable in this browser", carried by every updateStats) rather
  than degrading silently. This is DL-07's no-silent-loss discipline
  applied to the browser surface: silent degradation is exactly what hid
  this defect for a whole unit. Proven by forcing the registration to
  throw — layout, toolbar and search still work and the notice appears.
  (3) Edges route `curve-style: "taxi"` along a taxi-direction derived
  from DIRECTION (horizontal for RIGHT, vertical for DOWN) instead of
  bezier, so the drawing reads as the layered graph ELK computed instead
  of a centre-to-centre spline fan. To be precise about what this does
  and does not do: a cytoscape layout assigns node POSITIONS only, so
  ELK's own edge sections are discarded either way — taxi is cytoscape's
  orthogonal router, not ELK's. What it buys is a picture whose visual
  grammar matches the layering, not fidelity to ELK's routing (the
  ac2d089 commit message overstates this; this entry is the correction).
  Documented
  exception: taxi cannot draw an edge whose endpoints overlap, and a
  member's edge to its own ancestor box is exactly that — it would
  vanish from the picture entirely, which is silent loss, so those edges
  are classified once at load and kept on bezier. Checked at both
  directions and at bank scale (523 nodes / 420 edges, 0 undrawable
  edges after layout). (4) The test gap is closed by
  tests/test_viz_explore_browser.py: playwright drives ONE corpus-emitted
  page in chromium, webkit and firefox over precisely the controls the
  defect killed — the initial ELK layout completes, #fit rescales,
  #show-all restores every element and clears highlights, #search marks
  hits, reports no-match and un-hides a node a focus had hidden, the
  re-layout toggle off pins positions, the details panel opens for a node
  AND for an edge, a real right-click opens the context menu and one of
  its items narrows the graph, and the whole session must throw nothing.
  The engine is a fixture param so a single-engine failure names it, and
  the first layout timeout is remembered so the remaining eight tests
  fail with the same diagnosis instead of waiting it out again (webkit
  red 9:01 -> 1:01). It is a smoke test, not a rendering test: whether
  each control is wired and does its job, never how the picture looks.
  (5) Falsifiability is part of the deliverable — a smoke test that
  cannot fail is worse than none. Replayed against ac2d089^, the last
  broken tree: webkit fails 9/9 with "the initial ELK layout never
  completed — #stats is still 'laying out…'. Uncaught page errors:
  ['Illegal constructor']" while chromium passes 9/9 on the same tree;
  on the fixed tree 27/27 pass in all three engines. (6) Cost: the tests
  are opt-in via `DSL41_BROWSER_TESTS=1` and playwright is dev-only, so
  the package keeps its three runtime dependencies and a plain
  `pytest -q` neither slows down nor starts needing a ~200 MB browser
  install (1854 -> 1858 passed, 3 -> 4 skipped, 51.5 s -> 52.1 s; the
  fourth skip is this file skipping at module level). They run in their
  own CI job `explore-page` on one python — the matrix must not pay for
  the engines three times — beside the matrix job, so the workflow's
  critical path is unchanged (~3 min, ~2 of it the install). The pytest
  dev floor moves 8 -> 8.2 for `importorskip(reason=)`. (7) Two
  corrections to the manual harness, now pinned in code rather than in
  someone's memory: the "re-layout OFF keeps positions" check must
  exclude COMPOUND nodes (a box is its members' bounding box and
  legitimately moves — the harness checked all nodes and was wrong), and
  no `page.evaluate` may return a cytoscape object (playwright serializes
  the whole graph and webkit never finishes, which reads as a hang).
  Verification: WebKit before = layout never completes, five dead
  controls, 1 uncaught page error; WebKit after = 34/34 manual harness
  checks including all nine menu items clicked by mouse, 0 uncaught
  errors; chromium and firefox after = the same. Docs squared with the
  behavior: README's viz section (taxi routing, the polyfill, the named
  degradation), its source and test maps, the vendor README, and the
  nightbank runbook's navigation bullet.

- DL-78 the ss10 control plane becomes a module and a frozen document
  (2026-08-13; opened by an architecture review of the whole tree ahead of
  a prod-readiness / multihost push). Finding: the runner's OUTER protocol
  — what operators, the ss11 TUI, the headless `query`/`sendevent` CLI and
  any future non-local controller speak to a running engine — was the one
  contract in the project with no owning module and no frozen spec, while
  the INNER protocol of the deliberately dumb lifecycle tier has had both
  since DL-42/DL-48 (docs/supervisor-protocol.md). The asymmetry showed up
  three ways: 500 lines of ControlServer lived inside runner.py, whose
  stated job (DL-74) is the single-writer loop and the run lifecycle; the
  wire vocabulary was runner.py module-privates that runner_tui.py
  imported ACROSS the module boundary (`_JOB_EVENT_VERBS`, `_STATUSES` —
  two of the thirteen private cross-module imports pinned in
  scripts/arch_baseline.json); and the two client implementations sat in
  two further modules (an async one in runner_tui.py, a sync one inlined
  in cli.py, each with its own framing and its own error handling).
  Nothing was broken by this — every one of those pieces is tested — but
  a protocol whose definition is spread over four modules cannot be
  reviewed as a protocol, and the next work item that touches it is a
  transport change. Decisions: (1) runner_control.py owns both ends —
  vocabulary, server, async client, sync client. It continues DL-74's
  split, and it takes the seam the tests already named:
  tests/test_runner_control.py predates the module. Nothing is
  re-exported, per DL-74: cli.py, runner_tui.py and the tests each import
  from the owner. (2) docs/control-protocol.md freezes it, with the same
  standing as docs/supervisor-protocol.md — a change to a frozen item
  needs a DL entry. The doc records what the protocol IS, including four
  gaps it deliberately does not fix in this unit: no version handshake
  (adding a required field breaks every deployed client, and the first
  non-local transport needs a handshake anyway — that is where it should
  land), no authN/authZ beyond the socket's 0600 mode and file ownership
  (the ss12 RBAC non-goal, made concrete), unix-domain only (so the
  single-engine guarantee rests on a local bind(), which is exactly the
  guarantee a second host removes), and prose errors rather than stable
  codes. Naming them in the frozen doc is the point: the multihost track
  has to answer each one, and an unwritten gap gets re-discovered instead
  of designed. (3) Two names go public rather than move a private import
  to a new file. `Engine.live_jobs()` replaces the control plane reaching
  into `Engine._live` (a filewatch is ONLY an in-flight adapter task —
  no registry, no status field — so the read model genuinely needs it),
  and `runner_adapters.LINE_LIMIT` drops its underscore because three
  modules need it and a constant imported through a private name is a
  boundary that was never real. Net effect on the DL-75 gate: four of the
  thirteen pinned private cross-module imports are gone and none is
  added. (4) What deliberately did NOT change: the wire format (this is
  an extraction, not a protocol revision — the full suite passes
  unchanged at 1858 passed / 4 skipped); the absence of a controller
  lease at this tier (DL-41a — sendevent is multi-writer by AutoSys
  nature and the single-writer loop serializes it); and the two query
  handlers that read oracle package-privates (`_cond_true`,
  `_referencers`), because explain and deps must serve the ORACLE's
  truth and a second evaluator is exactly the duplication this review
  exists to prevent. runner.py 1352 -> 797 lines, which drops it below the
  advisory module ceiling entirely; runner_tui.py 1757 -> 1652 and cli.py
  1592 -> 1583 ratchet down but stay over it. The baseline is re-stamped so
  the gate measures from the smaller tree.

- DL-79 the supervisor lease is fenced by incumbency, not by a label
  (2026-08-14; found by the DL-78 architecture review while scoping the
  multihost track, closed before any transport work). Defect: `_h_acquire`
  handed a LIVE lease to any claimant whose `controller_id` matched the
  holder's, and the engine's `controller_id` was
  `f"engine:{run_root.resolve()}"` -- stable per run root by design, with
  the comment "one run_root has one logical engine controller (the ss10
  control-socket gate enforces it)". That reasoning is sound and it is
  local: the gate is a `bind()` on a unix socket, so it enforces one
  engine per run root ON ONE MACHINE. The moment a second host can serve
  the same logical run, the label stops discriminating and the failure
  inverts -- a partitioned OLD leader reconnects, presents the same label,
  mints a HIGHER token, and fences out the leader that legitimately took
  over. Nothing was broken today; this is a latent hole that step 2 of the
  multihost track would have walked straight into, and it is much cheaper
  to close first. Decisions: (1) A lease is LIVE when it is unexpired AND
  its holder's connection is still open; a live lease yields only to a
  claimant presenting the CURRENT token. controller_id authorizes nothing
  and is now a label for LIST and for the lease_held refusal. The token
  proves incumbency, not authenticity -- it is a small monotone integer,
  and authentication remains the same-uid peer-cred gate on accept, inside
  which a process can signal the engine directly anyway. (2) The resume
  property is preserved by a DIFFERENT mechanism than before, and this is
  the load-bearing half: a lease whose holder's CONNECTION is gone is
  freely grantable even while unexpired, because the kernel closes an
  AF_UNIX fd only when the holder process is gone (kill -9 included), so
  EOF is proof of death. `_drop_conn` already nulled `lease.conn` for the
  push path, so the discriminator existed and was simply not consulted.
  This is strictly better than the old rule in both directions: the old
  rule ALSO refused an orphaned lease to a differently-labelled claimant,
  so a resume that changed its label would have had to wait out the TTL.
  (3) The engine's controller_id becomes per-incarnation
  (`engine:<run_root>#<uuid8>`). It is no longer load-bearing, but the old
  value ENCODED the assumption multihost breaks, and leaving that
  sentence in the identifier invites the bug back. All three ACQUIRE sites
  in SupervisorClient (acquire, reconnect, the renew loop's re-acquire)
  now carry `token`; the first acquire of a process sends null and takes
  the free/orphaned path. (4) Recorded as a constraint on the multihost
  transport rather than solved now: over a network, EOF stops being proof
  of death, so a relay must not close the supervisor-side connection while
  its controller lives, or the orphan branch must become TTL-gated.
  docs/supervisor-protocol.md ss5 is amended (a frozen item, hence this
  entry). Falsifiability, both new tests against the pre-fix rule:
  `test_live_lease_yields_only_to_the_token_holder` fails (the spoofed
  label takes the lease) and
  `test_dead_holder_frees_the_lease_without_waiting_out_the_ttl` fails
  ("orphaned lease never freed"); both pass after. The old
  `test_lease_held_reacquire_and_fencing_monotonicity` pinned exactly the
  removed behaviour and now presents the token where it used to present
  the label alone.

- DL-80 the supervisor lease carries an incarnation id (2026-08-14; raised
  by the user reviewing DL-79 the same day, and it is a hole DL-79 itself
  made reachable -- recorded that way deliberately). The fencing counter is
  in-memory by design (spec ss5: "supervisor death kills all wrappers by
  lifeline, so the counter cannot regress while any spawned run is alive"),
  so a restarted supervisor mints token 1 again. That was harmless while
  the credential was `controller_id`. DL-79 made the TOKEN the credential,
  which turned counter reuse into a cross-incarnation ABA: supervisor S1
  issues token 1 to controller C1; S1 dies and S2 starts on the same
  socket; C2 acquires from S2 and is issued token 1 again; C1 reconnects,
  replays its stored token 1, matches the new holder's token by
  coincidence, and TAKES THE LEASE from the controller that legitimately
  owns it. Verified by removing only the new gate: the theft ACQUIRE
  returns `{ok: True, token: 2}`. Decisions: (1) The supervisor mints an
  `incarnation` at every start (uuid4 hex) and returns it from PING, LIST
  and ACQUIRE; the pid file carries it too. The fencing credential is the
  PAIR (incarnation, token). (2) The refusal is `wrong_incarnation`, a
  DISTINCT error from `stale_token`, and that distinction is the point:
  they demand opposite client behaviour. wrong_incarnation means the
  supervisor you knew is gone and every wrapper it held died by lifeline
  -- re-acquire AND reconcile from the spool. stale_token means another
  controller legitimately holds the lease -- do NOT re-acquire. A single
  widened token (say a uuid instead of a counter) would close the ABA
  just as well and was rejected for exactly this reason: it collapses two
  conditions whose correct responses differ. (3) The incarnation is
  PUBLIC -- any reader gets it from PING -- and the token is the secret
  half. The pair is not an authentication mechanism (that remains the
  same-uid peer-cred gate on accept); it identifies WHICH world a
  credential belongs to. (4) SupervisorClient injects the incarnation in
  `_request`, next to `"v": 1`, rather than each mutating verb naming it:
  a fencing credential that a newly added verb can forget to carry is not
  a credential. On `wrong_incarnation` the renew loop drops BOTH halves
  before re-acquiring, so it takes the free path instead of replaying a
  credential that can now collide. (5) Mutating verbs REQUIRE the field
  (fail closed) rather than checking it only when present. The tier ships
  as one wheel and the DL-42 extraction has not happened, so there are no
  external adopters to break; `v` stays 1 and this is recorded as the
  reason. Had adopters existed, this would have been a v2. The raw-client
  test harness learns the incarnation from responses exactly as the real
  client does, so the existing protocol tests keep their original intent;
  `test_mutating_verbs_require_a_token` now pins both gates separately.
  1860 -> 1861 passed, 4 skipped.

- DL-81 an explicit start against a live job is no longer silent
  (2026-08-14; DL-64's remaining corner, shipped standalone because it is
  independent of the OCC design it was found by). Two operators racing a
  STARTJOB on the same idle job both get `ok` from the control socket, one
  start happens, and the OTHER used to vanish completely -- no transition,
  no record, nothing in the trace to show a second attempt was ever made.
  DL-64 gave both SEM-10 gates a START_REFUSED record for exactly this
  reason ("an operator's STARTJOB dying without any visible
  acknowledgement proved untrainable") and left this branch returning a
  bare None. The arbitration itself was never wrong -- total order in the
  single-writer loop, then re-evaluation against current state at the
  point of application, so exactly one process runs and the ghost-run gate
  is a second independent belt. Only the visibility was missing.
  `_attempt_start`'s live-job branch now returns a reason naming the
  status that beat the request. Two properties fall out of where the
  branch sits, and both are pinned: it is ABOVE the force branch, so a
  FORCE_STARTJOB against a running job records too (that case matters
  more -- the operator explicitly forced and still got nothing), and only
  the explicit-event path in `_dispatch` consumes the return value, so the
  three internal probe callers (OFF_HOLD sweeps, box-start member launches,
  condition-edge re-evaluation) stay silent exactly as before. This does
  NOT solve operator concurrency: a caller still cannot express "start
  this only if it is still the job I looked at", and it does nothing for
  the CHANGE_STATUS overwrite, which is the dangerous lost-update surface.
  Those need the optimistic-concurrency design and are deliberately not in
  this entry. Verified before the change that no existing trace test
  covered this branch (the whole suite passed with the reason returned),
  so the new record is genuinely new coverage rather than a re-pin.
  1861 -> 1863 passed.

- DL-82 StatusStore owns every JobRuntime field and every global
  (2026-08-14; the first buildable piece of the concurrency model, landed
  ahead of the rest because it is a pure refactor that commits to none of
  the decisions still under review). The optimistic-locking design needs
  one place where "this entity changed" is observable exactly once per
  applied input. Eighteen assignment sites scattered across the
  interpreter is not that place. The first draft proposed proving
  completeness with a before/after property test over fed events plus a
  per-site deletion sweep; the adversarial review killed both with one
  observation, and it is the reason this entry exists: `_run` mutates
  armed, run_number and started_by and then sets status twice, so ONE
  touch inside `_set_status` masks every missed site in that feed, the
  property still passes, and deleting the missed touch does not turn the
  suite red either. A check that observes whole feeds cannot see a missed
  write. Decisions: (1) StatusStore gains runtime()/update()/seed()/
  set_global() and is the sole writer; Oracle._runtime delegates to it.
  Pydantic refuses an undeclared field name, so a typo in update() is loud
  rather than a silently created attribute nothing reads. (2)
  scripts/arch_check.py gains a BLOCKING structural check: a JobRuntime
  field assigned outside StatusStore, or a write reaching through
  store.job / store.globals_ from any module. It is scoped to the module
  that defines JobRuntime plus those container paths -- the supervisor's
  own _Run.run_number must not trip it -- and it stops accidents, not
  sabotage, the same stance as the rest of the gate. Proven falsifiable:
  reintroducing `rt.started_by = cause` blocks with exit 1. (3) The gate
  immediately earned itself. It found a nineteenth site no field-name grep
  could see: SEM-24 definition-time seeding in Oracle.__init__ assigned a
  whole JobRuntime row through `store.job[name] = ...`, which is a
  Subscript, not an Attribute. That site now routes through seed(), a name
  distinct from update() only in intent so its one caller reads as "this
  row starts here". (4) Behavior is unchanged and the bisimulation gate
  over the whole SEM corpus is the proof: 1863 passed before, 1863 after,
  plus 3 new tests for the gate itself = 1866. No wire change, no new
  field, no protocol version bump -- state_rev, the envelope and
  mandatory preconditions all land later and cite the frozen spec.
  oracle.py 1326 -> 1365 lines, re-stamped.

- DL-83 a signal in the spawn window is retryable, not a no-op; and the
  state-owner gate derives what it watches (2026-08-14; two independent
  fixes shipped together as the pre-work for the concurrency model, both
  found by the fourth adversarial pass over the design spec). (1) SIGNAL.
  `_h_spawn` returns as soon as the wrapper is FORKED, and the wrapper
  writes spawn.json a few syscalls later. `_signal_command` answered False
  when spawn.json was absent, which `_h_signal` reported as
  `{ok, noop: true}` -- indistinguishable from "the group is already
  gone". So a KILLJOB decided milliseconds after a start was silently
  dropped: the oracle recorded TERMINATED and the process ran to
  completion, with the late real record then discarded by the ss4 stale
  gate. The window is a few milliseconds, which is exactly the class DL-41a
  exists to close, and the same hazard was already found and fixed for
  SHUTDOWN (DL-48 gave `_orderly_shutdown` a bounded wait for missing
  spawn records) while the per-run path was left exposed. The
  discriminator is information the supervisor already had: a wrapper that
  is ALIVE with no record yet is mid-spawn (retry), one that has EXITED
  without a record left nothing this tier can address (a truthful noop).
  `_signal_command` now returns "sent"/"noop"/"not_ready"; `not_ready` is
  `{ok: false, error: "not_ready"}` so a caller cannot mistake it for
  success, and SupervisedCommandAdapter retries across a 5s spawn window
  -- matching DL-48's own bound -- giving up loudly on stderr rather than
  dropping the kill silently. Under the coming effect-dedup design this
  mattered more: persisting that noop as an applied effect would have made
  the kill unretryable forever. Falsifiable: collapsing the two answers
  back into "noop" fails the new test. The window is milliseconds, so the
  test does not race it -- it RECREATES the state deterministically by
  moving spawn.json aside while the wrapper is demonstrably alive, because
  a test that silently loses its race is the kind of coverage this project
  does not count. (2) The DL-82 gate. Its watched field list was
  hard-coded, so it would have stopped protecting the moment `state_rev`
  was added -- a gate that quietly narrows is worse than none. The set is
  now DERIVED from JobRuntime's own AST (annotated fields, stdlib-only, no
  dsl41 import), and coverage widened past Assign/AugAssign/setattr to
  AnnAssign, `del`, tuple/list destructuring including starred targets,
  and mapping mutators on the owner's containers
  (`store.job.update/setdefault/pop/clear/popitem`). The fixtures that
  declared bare class attributes were unrepresentative -- a Pydantic field
  is annotated -- and now match the model the gate really watches. 1867 ->
  1871 passed.
- DL-84 the concurrency model is frozen: mandatory optimistic locking,
  globally at-most-once effects, and an operator-owned host lifecycle
  (2026-08-14; `docs/concurrency-model.md`, stage S0 of the phase-12
  programme). The trigger was a question about two operators sending START
  to the same stopped job: the wanted answer is one winner and one
  rejection *at the point of application*, on the version the caller read.
  The scope is deliberately larger than that -- OCC covers every entity a
  human can address, globals included (an operator setting a variable
  while believing its value is X would otherwise silently overwrite
  someone's Y), and it is unavoidable rather than opt-in, because an
  optional precondition is one every careless caller omits. Four
  adversarial passes over the design produced real KILLs each time; the
  fourth returned nine freeze-blockers, and the four that changed the
  design rather than merely sharpening it are worth recording. (i)
  At-most-once was HOST-LOCAL. An effect identified only by a durable
  `effect_id` is bound to no host, so after an uncertain SPAWN a takeover
  could route the same id to a second relay, each host's dedup store would
  correctly report "first application", and the same bank job would run
  twice -- the exact failure the model exists to prevent. Effects are now
  bound to `{effect_id, run_id, job, run_number, executor_id}` and retries
  always go to that executor; rerouting demands a new run, a new effect,
  and proof the old executor is dead. (ii) "Deduplicate and replay the
  original result" is unimplementable: persist tombstone -> kill() ->
  crash, and nothing can know whether the signal landed, so the states are
  three (`pending` / `applied(result)` / `indeterminate`) and a retry
  replays a result only when it is KNOWN. (iii) The supersession guard did
  not fire for its own motivating scenario, because KILLJOB does not
  advance `run_number` -- a delayed SPAWN for run N is still "current"
  after run N is TERMINATED. Applicability is now exact desired state,
  with TERM and KILL as distinct stages with distinct ids. (iv) Timers are
  ordered GLOBALLY by `(due, insertion seq)`, so a per-job digest of the
  heap misses the relative order of equal-time timers across jobs, which
  decides resource release and box cascades; and four pieces of
  authoritative state (`_CapacityPool`, its waiter order, `_box_ran`,
  `_run_started_at`) had never been inventoried at all. Storage is frozen
  rather than left as an implementation detail because admission needs two
  atomic multi-record writes: Postgres-class, outbox IN the ledger, one
  transaction. Kafka alone gives the ordered log and leaves election and
  the outbox beside it -- two consistency stories where the model needs
  one. On the host lifecycle: quarantining an unreachable host is safe but
  insufficient, since one dead host would hold its jobs forever, so the
  operator owns an explicit per-host routing state (active / passive /
  quarantined / evicted) that is durable in the ledger so a failover
  cannot undo a drain. Eviction is the only state that lets another host
  run work bound to this one, and it is made PROVABLE rather than asserted
  by an opt-in deadman: a supervisor with no live leaseholder for
  `T_deadman` exits, killing its wrappers by lifeline EOF -- the mechanism
  supervisor-protocol ss5 already depends on, so the tier learns one
  number and no policy (DL-42's fence holds). The deadman is opt-in
  because it costs the crash-resume behaviour DL-79 preserves, and a run
  root without one is never reroutable except by an attributed
  `evict --force`, which is recorded with its authenticated principal and
  is the single path in the document that can produce a double run. It is
  fenced on return: eviction bumps the host `generation`, and a returning
  relay with a stale generation must self-fence before re-registering,
  which cannot un-run a duplicate but stops it continuing and turns a
  silent divergence into a recorded incident. Fourteen obligations
  (CM-01..CM-14, new namespace row in the citation index, tests named
  `test_cmNN_*`) replace the prose assertions of earlier revisions; CM-14
  -- no `(job, run_number)` runs twice over seeded interleavings -- is the
  point of the whole exercise, and it is checked by COUNTING spawns per
  run across an injected-fault interleaving rather than by argument. The
  build order puts the deterministic model harness (H) before the code it
  validates, and corrects two inverted dependencies: S2 persists
  `InputAttempt`, so the envelope and `ApplyResult` types freeze first,
  and both S2's admission and S5's effects depend on storage, election and
  relay contracts, which is why those are S0 text rather than S6
  discoveries.
- DL-85 the model harness lands before the code it validates, and an
  adapter call turns out not to be an effect application (2026-08-14;
  stage H of the phase-12 programme, `tests/model_harness.py` +
  `tests/test_model_harness.py`). The harness holds a spawn log that
  OUTLIVES an engine incarnation, because a resume-driven double run is
  invisible to any per-engine assertion -- each engine is individually
  correct -- and the CM-14 safety property is checked by COUNTING work
  that started, per (job, run_number), across the whole interleaving.
  Two things came out of building it. (1) The observation point was
  wrong on the first pass. Counting `adapter.run()` entries conflates
  "the adapter was invoked" with "a process started", and reconciliation
  invokes the adapter a SECOND time under the same run_number on two
  legitimate paths: an FW re-dispatch (which runner._reconcile calls an
  "idempotent read" -- re-arming a file watch executes nothing) and a
  detached-resume REATTACH (the wrapper never stopped, so the adapter
  awaits its exit push and spawns nothing). A naive counter reports both
  as the double run it exists to catch. The log therefore classifies each
  invocation exec/watch/attach and the checkers count execs -- which is
  the frozen model's own distinction surfacing in the first thing built
  against it, since DL-84 binds effects to `run_id`, the per-spawn uuid4,
  and not to `run_number`. A probe that misclassifies FW proves the
  classification is load-bearing rather than decoration. (2) Four of the
  five obligation tests were VACUOUS on the first pass -- they passed
  with their guard deleted -- and the probe is what said so. The
  corrections are each a small lesson: the crash/resume test's guard is
  reconciliation's own "never a silent re-run" rule and NOT the
  `_dispatched` seeding that looked like the obvious candidate; the kill
  test had to assert WHEN the process ended, because leaving the task to
  run to its natural end and dropping the late completion by the ss4
  stale gate also satisfies "it ended"; the restart test had to overwrite
  a LIVE run, since a job that already finished has no stale task to
  leak; and the duplicate-completion property turned out to be guarded
  twice, so it keeps its end-to-end test explicitly labelled a
  composition -- no single mutation reddens it -- with the stale gate and
  the ghost-run gate each given a sibling test that isolates it. Six
  mutations now turn their tests red: DL-81's live-job refusal,
  reconcile's no-spool-trace FAILURE, the ghost-run gate, the ss4 stale
  gate's run_number branch, _dispatch's terminal cancel, and the
  harness's own FW classification. Partition, reroute and leader failover
  are ABSENT rather than stubbed: a stub that always passes is the exact
  failure mode a safety harness exists to prevent. 1871 -> 1884 passed.
- DL-86 the state owner: frozen rows, maps that cannot escape, and one
  verb per kind of change (2026-08-14; stage S1b of the phase-12
  programme, `src/dsl41/oracle.py` + `tests/test_runtime_state.py`).
  `StatusStore` (DL-82) becomes `RuntimeState`, which is what
  docs/concurrency-model.md ss3 asks for and S1c's `state_rev` needs
  underneath it. Four changes of substance. (1) `JobRuntime` and the new
  `GlobalRuntime` are FROZEN, so a change is a replacement rather than a
  field write nobody watched, and an aliased row can no longer reach past
  every gate; `store.job` / `store.globals_` publish read-only proxies
  over private maps, so escape is now impossible at runtime instead of
  merely detected in the source. (2) The generic `update(**fields)` is
  gone, replaced by `transition`, `start_run`, `set_flags`, `set_armed`,
  `set_global` and `enqueue_timer` -- each tested for what it must change
  AND what it must leave alone, which is the failure the field dict made
  invisible. Two traps surfaced in the rewrite and are pinned by tests:
  `model_copy(update=)` does NOT validate (it will store the string
  "seven" in `run_number`), so rows rebuild through the model's own
  constructor; and rebuilding from a dict DEFAULTS to dropping an
  unrecognised key, which would have silently retired DL-82's typo guard
  -- `extra="forbid"` keeps it. (3) The timer heap moved onto the owner
  WITH its ordering token, and `timers_for(job)` publishes the per-entity
  half, because sorting the union of those views has to reproduce the
  heap exactly and a per-job set digest cannot. (4) The ss3 state
  inventory is settled: `_box_ran` MOVED onto the box row as
  `ran_members`; `_run_started_at` was assigned on every start and read
  NOWHERE, so it was never state and is deleted; `_CapacityPool` and its
  waiter order STAYED, under the two invariants ss3 demands be tested
  rather than asserted -- the waiter set is exactly the QUE_WAIT jobs and
  only a starting or running job holds units, both held after every event
  of a randomised contended schedule. The waiter order needs no token
  because a waiter's rank is fixed at its QUE_WAIT transition, which is
  itself a projected change; a timer does need one because a SECOND
  schedule tick arms a second `must_start` deadline while finding the job
  already armed, changing no row at all. The doc's op list named a
  `cancel_timer`; it is not built, because the oracle discards a
  superseded timer at FIRE time and a fire advances the clock and runs
  the lazy checks on its way past, so an early drop is not
  behaviour-preserving -- ss3 carries the amendment. Behaviour is
  unchanged and the bisimulation gate over the whole SEM corpus is the
  proof: 1884 -> 1902 passed with no test rewritten to fit. Fourteen
  mutations turn their tests red. Accepted cost, on the record rather
  than as drift: oracle.py is 1365 -> 1489 lines and now trips the DL-75
  size advisory. The owner wants its own module, but `JobStatus` and
  `_TERMINAL` must move with it or the import cycles, and that is a
  separate refactor with its own bisimulation run -- the cheap first move
  is extracting `_CapacityPool` (DL-74 already drew that line).
- DL-87 every entity carries a revision, and one input moves it at most
  once (2026-08-14; stage S1c of the phase-12 programme,
  `src/dsl41/oracle.py` + `src/dsl41/runner_control.py` +
  `tests/test_runtime_state.py` + `tests/test_runner_control.py`).
  `state_rev` lands on `JobRuntime` and `GlobalRuntime`, and every input
  runs inside a transaction that increments each entity exactly once and
  only if its SEMANTIC projection moved. This closes CM-02 and CM-03.
  Behaviour is otherwise unchanged; the bisimulation gate is again the
  proof (1902 -> 1916 passed).
  What one input IS, pinned: one `feed()` or one `advance()` (ss4 puts
  standalone time observations on the same path as operator commands), so
  the fired timers, the cascade and the box folds that follow are
  consequences of ONE input and share its revision. A per-write bump would
  make `expect` usable only for an input that changes a single field --
  starting a box moves the box row four times before its members are
  touched. The commit happens in a `finally`: the oracle has no rollback,
  so whatever DID change is durable and a reader holding the old revision
  must be invalidated by it; swallowing the bump on a raised input would
  hand that reader a stale success.
  The touched set is snapshot-on-first-touch inside `RuntimeState` rather
  than a snapshot of everything. That is exact, not an optimisation: the
  projection is a function of the rows and the heap, and both move only
  through the owner's own mutators, so the set cannot be
  under-approximated by construction -- which is the direction ss3 says
  must not be got wrong. The heap half matters twice, and both are tested
  in isolation: arming a timer touches its job (a second schedule tick
  arms a second `must_start` deadline while finding the job already
  armed, changing no field at all), and a timer LEAVING the heap touches
  it too (a stale deadline that fires, emits nothing and writes nothing
  is still a schedule change a client must not hold a precondition
  across).
  The catalog seed is treated as the genesis input. Not ceremony: it is
  what makes `revision(key) == 0` mean "absent" for a global, and
  therefore what makes a conditional create expressible -- a DECLARED
  global lands at 1 like anything else that has been through an input, so
  nothing that exists shares 0 with the undeclared. The ss6 read verbs
  follow from that: `global name` / `globals names` answer NAMED entities
  with `{present, value, state_rev}` and insert nothing, an unset name
  answered rather than omitted, and `status` now carries each job's
  `state_rev`. Absence you cannot name is absence you cannot lock
  against.
  One honesty correction, made because the probe forced it: a test
  claiming that excluding `state_rev` from its own projection prevents a
  self-justifying loop was VACUOUS -- the bump is applied AFTER the
  commit-time comparison, so a projected revision could not yet have
  moved and no loop is reachable. The exclusion stays (ss3 mandates it,
  and `_projection` is the natural semantic digest for S2's ApplyResult,
  where a value that differs for two identical states is a false
  conflict) but the test now pins what is actually true: two rows
  differing only in revision project identically.
  Second probe lesson, on the probe rather than the code: three
  mutations were guarded ONLY by the CM-03 hypothesis property, and one
  of them reported falsifiable on the first run and VACUOUS on the
  second, because catching a narrow mutation depended on the seed. Each
  now has a deterministic sibling test -- `advance()` as an input, a
  timer leaving the heap as the sole effect of an input, and a global's
  revision accumulating across inputs -- and the property runs alongside
  as corroboration, never alone. A probe whose verdict depends on a seed
  will eventually lie in both directions.
  Fourteen mutations turn their tests red, twice in a row.
  `docs/citation-index.md` gains the `S\d[a-c]?` row: the stage names are
  a real namespace (ss10 defines S0-S7) and the DL-75 gate blocked on
  them until it existed, which is the gate working as designed.
- DL-88 the capacity pool gets its own module (2026-08-14;
  `src/dsl41/capacity.py`, extracted from `src/dsl41/oracle.py`). The
  promised follow-up from DL-86, done as its own commit rather than as a
  rider on the stage that caused the growth. `_CapacityPool` becomes
  `CapacityPool` -- public in its own module, since a private
  cross-module import is itself an arch_check block -- and takes its
  three private helpers (`_safe_units`, `_release_policy`,
  `_merge_policy`) with it; they had no other caller. It moves along the
  line DL-74 already drew and needs no new seam: nothing in the pool
  knows about statuses, events or time, which is exactly why it was the
  piece to move first. No behaviour change, no test rewritten, 1916
  passed either side.
  oracle.py is 1637 -> 1472 lines. Still 107 above the DL-75 baseline
  (1365) and the advisory still fires, deliberately: the remaining growth
  IS the state owner, and moving it out is a real architectural decision
  rather than a size trim -- `JobStatus` and `_TERMINAL` must move with
  it (runner.py's allow-listed private import of `_TERMINAL` is a
  baseline entry that would change), `Event` would have to become a
  TYPE_CHECKING-only reference, and `OracleError` would have to pick a
  side. That is what /arch-review is the lens for. Re-baselining now
  would silence the one signal pointing at the question, which is the
  failure mode the ratchet exists to prevent.
- DL-89 admission lands: one order for every input, and the log becomes a
  ledger (2026-08-15; stage S2 of `docs/concurrency-model.md` §10, closing
  CM-04, CM-05 and CM-07). New module `src/dsl41/runner_admission.py`
  holds the record types (`Attempt`, `ApplyResult`), the typed
  `Frontiers`, the `DecisionIndex`, the fingerprint, the stale gate as a
  pure function, and `apply_attempt` — the one function the live engine
  and replay both go through, because two paths that could disagree make
  the log a record of nothing. It is its own module rather than part of
  the WAL because S6 replaces the storage under a stable admission layer;
  that is where the seam goes.
  **The order, and why each step is where it is.** Dedup precedes
  admission, so a retry consumes no index and assigns no leader timestamp
  — admitting first and deduplicating after would let a retry storm walk
  the clock forward. The batch's time observation applies before the
  gate, so a `term_run_time` kill lands before the gate reads the status
  it gates on. And the time half applies even when the attempt is
  REJECTED: a rejected completion still observed the clock, and the kill
  that observation let fire is a decision the estate has already acted
  on. That last one is why §4 makes the batch two records rather than one
  field on one record — a design detail that read as ceremony until the
  first sketch collapsed them and turned DL-44's kill-survives-replay
  property red.
  **One batch is one store transaction.** Split out as its own commit
  first (`Oracle.batch` / `InputBatch`): the shell has to fire timers
  before deciding whether to feed, and doing that as `advance()` then
  `feed()` moved every touched entity TWICE for one admitted input, which
  would make `expect` name a revision no read ever returned. `feed()` and
  `advance()` are now that object with the attempt present or absent.
  `_TERMINAL` became `TERMINAL` in the same commit: admission must know
  which statuses end a run, and the engine's allow-listed private import
  of it was already the standing admission that the shell needs the
  constant, so this removes an allow-list exception rather than adding
  one.
  **The log.** An `input`/`advance` record IS the `InputAttempt` — one
  line, so a crash cannot tear the batch in half — and now names its
  `request_id` and `fingerprint`. A new `result` record carries the
  decision, appended after the attempt, which is what makes replay
  two-pass: pass one indexes the decisions, pass two applies. A durable
  decision is authoritative and is never recomputed; an attempt with no
  result is applied, through the gate, because admission is the commit
  point and a decision is exactly what it is missing. The `result` index
  rides under `index` rather than `seq` because a result shares its
  attempt's number and `seq` is the §10 subscribe cursor — two records
  under one cursor value would leave the second undeliverable to a
  resuming subscriber. `drop` narrows to pre-admission refusals, which
  today means only the E9 missed scheduler ticks: a gated completion is
  now a recorded rejection, and the difference is that absence cannot be
  told apart from a crash.
  **No format gate, and that is a claim, not an omission.** A journal
  written before this build carries no results, so every attempt in it
  applies — exactly what the single-pass reader did — and its attempts
  get the ids they would have been admitted under, named by their
  position in the log. Tested.
  Cost, stated: one extra append (and, in the real domain, one extra
  fsync) per input. Recording a result only for rejections would halve it
  and destroy the design — the absence of a result is how replay
  recognises the crash window, so an absence that also meant "nothing
  happened" would make that window unreadable.
  A ss7 mixed-build detector came free: applying an attempt whose durable
  result disagrees with the revisions this build derives raises rather
  than continuing, since every precondition checked from there on would
  be checked against a number the log never produced.
  1919 -> 1937 passed; ruff, mypy and arch_check clean. Twenty mutations
  turn their tests red, and the probe found two tests that could not
  fail: the fingerprint's "excludes the stamp" half compared two
  identical calls (`at` is not a parameter, so the claim is structural —
  it is now tested where it BITES, in the CM-05 retry stamped five
  minutes after its original), and `ApplyResult`'s reason/decision
  validator had no test at all. Re-ran S1b and S1c after the refactor:
  14/14 each, with three anchors re-pointed at code DL-88 and this entry
  moved.
- DL-90 preconditions become mandatory, and the wire breaks once
  (2026-08-15; stage S3 of `docs/concurrency-model.md` §10, closing
  CM-06). §0's invariant — *no externally requested direct mutation of
  published oracle state is applied without checking the version the
  caller read* — is enforced from here on. There is no opt-out and no
  `"any"`: a mutation that names no revision is refused.
  **Where the mandate lives.** In `parse_envelope`
  (`runner_admission.py`), not in the socket server, because §0 admits no
  exception and a rule written once per transport is a rule that one
  transport will eventually write differently — the relay S5 adds must
  reach the same verdict. `Engine.inject` is deliberately NOT behind it:
  the scheduler, the adapters, reconciliation and every test script are
  in-process and inside the trust boundary, and §0's subject is what
  crosses it. `Engine.submit` is the external door and the only one that
  takes an envelope.
  **Refused and rejected are different facts.** A refusal (steps 1–2 —
  bad framing, absent `expect`, a `baseline_id` from another run, a
  reused `request_id`, a stale `epoch`) consumes no index, moves no
  clock, and leaves NOTHING in the log; it is recorded only on
  `Engine.refusals`, which exists because otherwise a caller that asked
  for no decision would watch a command vanish. A rejection (step 6 — the
  entity moved) took an index and fired its batch's timers, so it is a
  decision and is journaled as one. Collapsing them would make "your
  command did nothing" indistinguishable from "the engine crashed before
  deciding", which is precisely the distinction S2 built the ledger for.
  A reused `request_id` stopped raising through the loop: a client error
  must not take the estate down.
  **`expect` is part of the command.** It is in the fingerprint, so "kill
  the run I saw at 12" and "kill whatever is running now" are two
  commands. Hashing them alike would let a retry of the first be answered
  by the second's decision — the confusion optimistic concurrency exists
  to prevent, arriving through the dedup path instead. `claimed_actor` is
  in it for the same reason, and is named a claim because there is no
  authentication at this tier (control-protocol §7 gap 2): the log
  records what the client said about itself, which is a breadcrumb, never
  attribution.
  **The boundary of the check, found by a test and worth stating.** A
  timer firing inside an input's OWN batch does not invalidate that
  input's precondition. §3 gives one input one increment, applied at
  commit, so everything an input causes shares its revision — a revision
  that moved because of the caller's own input is not one they could have
  read beforehand, and requiring them to name it would make every
  precondition unsatisfiable. The same deadline firing as its own input
  (due strictly earlier) does invalidate it, which is the case an
  operator actually meets. What still stands between an operator and a
  job that ended a moment ago is the semantics, not concurrency control.
  Both halves are pinned; the first sketch asserted the opposite and was
  wrong.
  **Protocol v2** (`docs/control-protocol.md` re-frozen). One break, two
  changes, taken together because taking them apart would break every
  client twice. Every request names `"v": 2` — the handshake §7 recorded
  as a known gap, checked ahead of the subscribe branch so no door is
  left unversioned. A mutation carries the §6 envelope (`verb`,
  `payload`, `request_id`, `expect`, optional `claimed_actor`) instead of
  the flat `event`/`job`/`name` fields, and is answered with its
  DECISION: §4 emits `command_committed` at step 4 and `oracle_applied`
  at step 7, and answering with the first would tell an operator their
  kill was written down, not that it landed. A precondition whose outcome
  the caller cannot see is not a precondition. The answer therefore
  carries `decision`, `index`, `request_id` and the revisions the input
  moved, and drops `at` — a retry's answer is the ORIGINAL decision, and
  echoing the retry's own stamp beside it would make two answers to one
  command differ in a field that is not about the command. Every read
  answer carries `baseline_id`, `epoch` and `applied_index` (§6), since a
  revision is meaningless without the log it came from. A sendevent that
  gets no decision within 5 s answers "I do not know" and says re-read
  before retrying — deliberately shorter than the client timeout, so the
  caller gets a diagnosis rather than a bare socket timeout.
  **`epoch` is required, not defaulted, and is checked in its §4 place**
  — after dedup, not before — even though it is inert on one host. An
  exact old-epoch retry recovers its original result (it was decided by
  the leader that held that epoch); an unseen old-epoch request is
  refused. Implementing the ordering now costs one comparison and pins it
  with a test, rather than leaving S6 to rediscover why it is
  counter-intuitive. Requiring the field is the same argument one level
  up: §6 ships it in v2 so that clients CARRY it, and a field that may be
  omitted is a field nobody sends — S6 would then be the second wire
  break that shipping it early was meant to avoid. It costs nothing to
  send, since every read publishes the current epoch beside the revision.
  **Clients.** `dsl41 sendevent` reads the addressed entity's revision
  and names it; `--expect N` names one by hand. The auto-read narrows the
  race to one round trip and does not close it, and the docstring says
  so: an operator who looked at a status page and then chose to act
  should pass the revision they looked at. The §11 TUI does exactly that
  for free — `expect` comes from the row the table was SHOWING — which is
  the thing a terminal can do that a shell script cannot.
  1938 → 1971 passed; ruff, mypy and arch_check clean. Twenty-five
  mutations turn their tests red, three of them by HANGING rather than
  failing — delete the line that answers a parked caller and nothing ever
  answers — which is a finding about the probe as much as the code: its
  runs are bounded now, and a hang is reported as red.
- DL-91 architecture review (2026-08-15; the `/arch-review` lens, first
  since DL-75 built the gate). `scripts/arch_check.py` reports three size
  advisories and nothing blocking; what follows is the half it cannot
  see. Findings ranked by cognitive load removed over cost to change,
  with the declines recorded so they are not re-found next time.
  **1. The client half of the protocol had no home — FIXED.** DL-78 moved
  the §10 server, its vocabulary and both clients into
  `runner_control.py` so the wire had exactly one definition. S3 (DL-90)
  then gave a mutation a shape a caller must BUILD — read the addressed
  entity, name its revision — and left that rule at the call sites: which
  query answers a `global:` key versus a `job:` one, where the revision
  sits in each answer, and that a refused read means revision 0. Four
  copies (`cli.py`, `runner_tui.py`, two in the tests), and
  `_claimed_actor` was duplicated verbatim in two modules. Now
  `read_for`, `revision_in`, `command` and `claimed_actor` live beside
  the clients, and all four call sites use them. The round trip stays at
  the call site, because `ControlClient` and `roundtrip` are two
  transports for one protocol — that part was always right.
  **2. Two clients of the supervisor protocol, one of them in `cli.py` —
  FIXED.** `runner_adapters.SupervisorClient` is the engine's; `dsl41
  supervise` had its own `_SupervisorConn`, and both knew the same
  framing rule (stamp `"v": 1`, newline-delimited JSON, drop `push`
  notifications) for a socket `docs/supervisor-protocol.md` owns. Moved
  to `runner_adapters.SupervisorConn`, beside its twin, on DL-78's
  argument exactly: two transports for one protocol, not two protocols.
  **3. `oracle.py` holds two concepts — ACCEPTED, sequenced next.** The
  standing advisory (1523 lines) is not a size problem, and the earlier
  note that a split is "a real architectural decision" was right about
  the difficulty and wrong about the seam. The cut is not "extract the
  state owner" — `RuntimeState`'s timer heap holds `Event`s, so `Event`
  goes with it and drags `EventKind`, and what is left over is not a
  fragment but the other half of a pair: **the model and the interpreter
  that moves it.** The import table says the same thing from outside: ten
  modules import from `oracle`, eight of them want `Event` and only six
  want `Oracle`, so `runner_scheduler` currently drags an 854-line
  interpreter in to name a timestamped event. Splitting puts `JobStatus`,
  `TERMINAL`, `EventKind`, `Event`, `TraceEntry`, `JobRuntime`,
  `GlobalRuntime`, the projection constants, `OracleError` and
  `RuntimeState` in one module with no dependency on the interpreter, and
  takes `oracle.py` under the threshold on the way. `InputBatch` stays
  with `Oracle` — it drives the interpreter's clock. No re-export shim:
  a re-export layer is a pass-through layer, so the ~30 import sites are
  updated. Its own commit, before S5 adds more importers.
  *(Done, same day.* `src/dsl41/oracle_state.py`, 429 lines; `oracle.py`
  falls to 1152 and off the advisory list. 28 files' imports updated. The
  module name is `oracle_state` rather than `runtime` because two letters
  from `runner` is not a distance, and rather than `oracle_model` because
  the dominant thing in it is an owner with verbs, not a record.*)
  **4. `Engine.drops` holds two categories — DECLINED, one wording fix.**
  Since DL-89/DL-90 the vocabulary is refused (never admitted) / rejected
  (a decision) / missed (E9's scheduler ticks), and the fields are
  `drops` (rejected + missed) and `refusals` (refused). The seam is in
  the wrong place, but renaming the in-memory list without renaming the
  `drop` WAL record — frozen in runner-design §7, and already narrowed
  once by DL-89 — would create a second vocabulary for one concept, which
  is the disease rather than the cure. What was actually wrong is that
  `_serve_run` prints every drop under a comment claiming they are the
  resume missed-tick sweep, which stopped being true when reconciliation
  rejections started landing there; the label is fixed, the list is not
  split. Revisit if a third category appears.
  **5. No name for the failed-status set — DECLINED.** `("FAILURE",
  "TERMINATED")` is spelled inline at five sites. Three are the oracle's
  own box-fold rules, where the tuple IS the SEM rule; the other two are
  UI policy (`is-failed`, the TUI's problem highlight). A shared constant
  across that boundary would let an edit made for the UI change the
  semantics, which is a worse failure than the repetition. `TERMINAL` is
  shared because every user of it means the same thing.
  **A limitation of the gate, for the record.** The duplicate-body check
  compares normalised ASTs, and `_claimed_actor` escaped it because
  `cli.py` defers its imports into the function body while `runner_tui.py`
  imports at module level — the same function, two different statement
  lists. Deferred imports are a real and deliberate style in `cli.py`
  (startup cost), so this is a class of duplicate the script structurally
  cannot see, which is the argument for the review existing rather than a
  bug to fix in the script.
  **Load-bearing — leave alone.** The citation density and the SEM/UCS
  cross-references: that is the project's core discipline, not clutter.
  The viz trio, which already shares correctly (`viz_html` imports
  `to_mermaid` and formats one `ReportContent`; `viz_explore` imports
  `edge_label`/`job_detail`/`job_kind`/`job_schedule` from `viz` and
  `substitute` from `viz_html`) — three renderers, one grammar. The
  `Clock`, `JobAdapter` and `EdgeEnds` protocols, each with two or more
  implementations and a documented reason. `apply_attempt` as the one
  function the live engine and replay both take: that is not indirection,
  it is the reason the log records anything. `on_ice`/`on_hold`/
  `on_noexec`/`armed` as four independent booleans — they are not an enum
  in disguise, AutoSys lets a job be iced and held at once. And
  `runner_admission` as its own module rather than part of the WAL: S6
  replaces the storage under a stable admission layer, which is the
  argument DL-89 made and it still holds.
  **The ratchet is re-baselined, and that accepts more than the split.**
  `oracle.py` leaves the size map because the finding was acted on;
  `cli.py` (1583 → 1612) and `runner_tui.py` (1652 → 1712) stay in it at
  their new heights, which is an acceptance, not a reset — both were read
  in this review, the duplication in them was the finding above and is
  gone, and what remains is the S3 surface itself (`--expect`, the
  read-then-write, the TUI's precondition path). Two entries move the
  other way and are worth naming because they are S2's doing:
  `run_until_quiescent` 161 → 135 lines and `_reconcile` 124 → 123, from
  pulling the admission order out into `_admit_and_apply`. The gate now
  measures drift from here.
- DL-92 the operator gets the vocabulary S3 built (2026-08-15; stage S4
  of `docs/concurrency-model.md` §10, the CLI/TUI half of the only
  file-disjoint pair). S3 gave the engine four answers and gave its
  clients two: `ok` and not-`ok`. This closes that gap. Nothing here is
  new machinery — it is the machinery becoming reachable.
  **Four outcomes, one classifier.** `applied` / `refused` (nothing
  admitted, no index, nothing in the log) / `rejected` (a decision, at an
  index, against you) / `unknown` (no decision arrived; it may yet
  apply). They call for four different next moves — send it again, fix
  it and send it again, re-READ and decide again, and *look before you
  touch anything* — so a client that cannot tell them apart has to guess
  at the one moment guessing is most expensive. `outcome_of` lives in
  `runner_control.py` beside the other client-half functions (DL-91
  finding 1, applied the same day it was written): what an answer MEANS
  is a reading of the protocol, while what a surface DOES with it is not
  — the CLI turns it into an exit code and the TUI into a sentence, and
  those may legitimately differ.
  **`refused` becomes load-bearing in its absence,** so it had to become
  exhaustive. The classifier reads a missing marker as uncertainty, which
  is only safe if every `ok: false` a mutation can meet carries one —
  including the two doors it shares with the queries (a malformed frame,
  a wrong `v`) and `unknown cmd`. Those three now say `refused: true`.
  The no-decision timeout is the one deliberate exception and the test
  sweeps every other door to prove it is the only one. An `internal
  error` from a handler bug also carries no marker and classifies as
  `unknown`, which is correct: a crashed handler genuinely does not know
  what it did.
  **Exit codes 2/3/4.** A script's entire view of a command is its exit
  status. 2 keeps its established meaning (nothing happened — the
  "never started" family), 3 is rejected and 4 is unknown; 1 is
  untouched and no other verb uses 3 or 4. `--expect`'s help changed
  from "refused" to "rejected" in the same pass: it was describing the
  outcome with the wrong word, which is exactly the confusion this
  entry removes.
  **`--request-id`, and why exit 4 prints it.** The engine has
  recognised an exact retry since S3, and no shell could reach that:
  `command()` mints a fresh uuid4 per invocation, so a retry after a lost
  answer was a NEW command — the double-apply the dedup path exists to
  prevent, arriving through the recovery path. The flag carries an id
  back in; exit 4 prints the id it sent to stderr, because the answer
  that would have carried it is precisely the one that never came. Both
  halves are needed and neither is useful alone.
  **`query global` / `globals`.** On the wire since S1c (DL-87), reachable
  only from the TUI, which left `SET_GLOBAL` as the one mutation a script
  could not compose honestly — its precondition is a revision it had no
  verb to read. `global` names one and `globals` a list, as the server
  has them: asking for one through the plural would make a client that
  wants a single revision unwrap a map to find it. Also documented in
  control-protocol §4, which had been referring to verbs it never
  defined.
  **`status --brief` carries the revision.** The skim is what an operator
  reads immediately before acting, so it is where the number has to be:
  an `--expect` read by the CLI a millisecond earlier names what the CLI
  saw, and one taken off the skim names what the OPERATOR saw. That
  distinction is the whole content of §0's "the version the caller read".
  **Not done, deliberately: no `state_rev` column in the TUI table.** It
  is the one cell that changes on every input to its job, so it would be
  the most frequently redrawn cell in the table — and DL-46's whole
  reason for the (text, style) cell diff is that pushing changed cells at
  estate scale is what froze the UI. The table already USES the revision
  it holds (the `expect` comes from the row the operator was looking at,
  DL-90); the number itself belongs where it is read deliberately, which
  is `--brief` and `query`.
  1971 → 1982 passed; ruff, mypy and arch_check clean. `cli.py` 1612 →
  1683 and `runner_tui.py` 1712 → 1744 keep both files on the advisory
  list; they are the surface this stage is about, and DL-91's re-baseline
  was six commits ago, so the next review inherits them rather than a
  fresh baseline.
- DL-93 how S5 is built, and where host state lives (2026-08-15;
  sequencing decision, taken before the code so the four commits do not
  re-derive it). `docs/concurrency-model.md` §10 names S5 as one stage —
  *relay + host identity, effects, barrier, deadman, host states +
  evict* — and it is the largest in the programme: five CM obligations
  (09, 10, 11, 12, 13) plus the half of CM-06 that S3 left open. It is
  built in four slices, in this order, each landing on its own with its
  own tests.
  **S5a host identity, the routing table, host states, the `host` verb.**
  First because `executor_id` has to name something before an effect can
  bind to it. Closes CM-13 (drain: `passive` routes nothing new and
  finishes what is running) and the refusal half of CM-11 — a host with
  no deadman can never be evicted, which is §8's first precondition and
  is testable the day the state exists.
  **S5b the deadman.** One interval and one exit in the Tier-1
  supervisor, on the mechanism supervisor-protocol §5 already relies on
  (supervisor death kills every wrapper by lifeline EOF), so it adds no
  policy to the tier and does not breach DL-42's counter-fence. Closes
  CM-10 and completes CM-11: the eviction bound is only checkable once
  something produces `last_contact` and `T_deadman`.
  **S5c the effect outbox.** `effect_id` bound to `executor_id`, the
  three states (`pending` → `applied(result)` | `indeterminate`),
  supersession by exact desired state, per-run ordering. Closes CM-06's
  remainder (`outcome_unavailable`) and CM-09's application half. Local
  executor throughout — none of it needs a network, which is why it
  comes before one.
  **S5d the relay and the takeover barrier.** Quarantine, epoch
  re-checking on dispatch, the fenced return of an evicted host. CM-12
  and CM-09's delivery half.
  **The outbox is built ON the spool, not beside it.** The lifecycle
  tier already records durably what a naive outbox would re-invent:
  `spawn.json` says a spawn happened and with what process identity,
  `status.json` says how it ended, and the ABSENCE of status.json is
  already the unobservable case (E7). §5's `indeterminate` IS that
  absence. What the engine lacks and S5c adds is the layer above:
  intent recorded before the attempt, keyed by an id that binds to an
  executor, and a kill that is an effect with an id rather than a
  `task.cancel()` with none. A second durable record of "did this run
  start" would be the parallel-model smell DL-91 exists to catch.
  **Host routing state is admitted input, not a side table.** It gets a
  third `expect` namespace, `host:<id>`, beside `job:` and `global:`,
  and its rows live in the one published-state owner (§3's
  `RuntimeState`). That is not a convenience: durability, replay, the
  §3 one-increment-per-input rule, the §0 precondition check and the
  read verbs are all machinery that already exists and works on
  namespaced keys, and a routing table with its own revision counter
  would be the same concept spelled a second way. §8 requires the state
  to survive a failover, which is exactly what being an input buys.
  **The oracle must never read a host row,** and the split DL-91 made is
  what keeps that honest: `HostRuntime` belongs in `oracle_state.py`,
  which the interpreter does not depend on, and `HOST` is NOT an oracle
  `EventKind` — a job's condition truth cannot depend on where its
  machine routes. The verb is applied by the engine inside the input's
  batch, on the seam §4 already has for an input that carries no oracle
  event (`Attempt.kind is None`, today the standalone time observation).
  A test pins that `oracle.py` never names the row type; the exact
  encoding of the attempt is the slice's business, not this entry's.
- DL-94 the routing table lands: four states, three operator verbs, and a
  drain that holds work instead of losing it (2026-08-15; stage S5a,
  building DL-93's first slice). `docs/concurrency-model.md` §8 becomes
  reachable: `HostRuntime` under §3's owner, a `host:<id>` namespace beside
  `job:` and `global:`, a `host` verb on the control plane, and
  `dsl41 host`. Closes CM-13 and the refusal half of CM-11. §8 carries the
  four findings the build produced, as an amendment; this entry is the
  shape.
  **A drain HOLDS.** `passive` routes no new effect, so `_spawn` dispatches
  nothing — and the job is held, not failed and not rerouted. Rerouting
  without proof the old executor is dead is the double run the whole model
  exists to prevent (§7), and failing it would turn a maintenance window
  into an estate-wide incident. Held-ness is DERIVED — the oracle has the
  job live at a run_number the ghost-run gate never dispatched — so there
  is no second record of intent to fall out of step with the first. S5c's
  outbox is where intent becomes durable, and building a held set now would
  be that concept twice.
  **Which means `activate` re-drives.** §8 calls `passive` reversible and
  that has to mean something: the oracle decided those starts once and will
  not decide them again, so an `activate` that left them RUNNING forever
  would be a worse failure than the one draining avoids. One loop over the
  derived set, on the applied-host-command seam.
  **And a held job survives the restart the drain survives.** The half that
  was nearly lost: the routing row replays on its own, but the work it was
  protecting goes through reconciliation, where a start with no spool trace
  is a crash between feed and spawn and is FAILED rather than silently
  re-run. On a host that routes nothing that inference is simply wrong —
  there was no crash — so the sweep leaves those jobs alone and un-seeds the
  ghost-run gate that `resume_run` had set from the run_number. A drain
  whose state survived a failover while its work was failed would be a drain
  in name only.
  **`held` is published because a held job reads RUNNING.** `_run` walks a
  start through STARTING to RUNNING inside one feed, so a drained estate is
  indistinguishable from a working one by status alone. That is a silent
  hang, and a drain is precisely the operation an operator has to be able to
  watch, so `status` carries a derived `held` per job. Deliberately not on
  the TUI table: DL-92's reasoning about the most-redrawn cell applies
  unchanged, and the drain view is `hosts`.
  **Eviction's gate is written in full and rejects in full.** All three §8
  preconditions, as a pure function of the row: the honest consequence is
  that `evict` in a real estate today always rejects, because nothing
  produces `quarantined` until S5d and nothing keeps `last_contact` fresh
  until S5b. That IS CM-11's refusal half — a host nobody can prove dead may
  not have its work rerouted — and the permitted side of the bound is
  unit-tested over rows the tests build, which is available precisely
  because the gate reads a row rather than probing a host. `T_kill` is two
  wrapper graces at the 10 s default (supervisor-protocol §4 step 5) and
  `T_skew` is 200 ppm of the interval over a 1 s floor: both are added to a
  wait `--force` can always skip, so erring long costs patience and erring
  short costs a double run.
  **The fingerprint gained a key that is OMITTED when absent.** `host`
  hashes under its own key so no host command can collide with a verb whose
  payload happens to look like one — but a null-valued key would have
  changed every fingerprint an earlier build wrote, turning an exact retry
  across a resume into a `RequestCollision`. That is §7's mixed-build hazard
  arriving through the one door with no version gate yet.
  **`dsl41 journal` had to learn the genesis.** It replays a log onto a bare
  `Oracle` to reconstruct the trace, and a routing input needs the table the
  engine seeded — so it calls the same `seed_local_executor`. Reproducing a
  log means reproducing the genesis it was replayed onto, not only the
  catalog.
  Also: `scripts/arch_check.py`'s state-owner gate now derives its watched
  fields from the owner's whole model SET rather than one model — a gate
  that protected only `JobRuntime` would have been narrower the day after it
  was written, which is the DL-83 discipline applied to its own author. And
  `docs/citation-index.md`'s `S\d[a-c]?` widened to `[a-d]`: DL-93 named
  S5d and the gate correctly refused to resolve it.
  1982 -> 2007 passed; ruff, mypy, arch_check blocking checks clean. Two
  advisory notes are inherited (cli.py and runner_tui.py over 1200 lines,
  the latter from DL-92) and one is this stage's: `_reconcile` was already
  over the 120-line advisory at 123 and is now 131. It is not extracted
  here — the sweep that grew is a distinct concern and pulling it out is an
  architecture change, which gets its own commit rather than riding inside
  a slice.
- DL-95 the deadman: one interval, one exit, and the two producers the
  eviction bound was missing (2026-08-15; stage S5b, DL-93's second slice).
  Closes CM-10 and makes CM-11's bound computable from real state. The
  supervisor gains `--deadman-seconds N`: no LIVE leaseholder for N seconds
  and it stops its loop and returns. Its death EOFs every lifeline it owns,
  which is supervisor-protocol §4 step 5's existing kill path, not a new
  one — so the tier still decides nothing about what should run, only that
  nobody is watching it. DL-42's counter-fence holds: no queueing, no
  policy, one number.
  **Both halves of "live" are load-bearing.** §5 defines a live lease as
  unexpired AND its holder's connection still open, and either alone asks
  the wrong question here: an unexpired lease whose connection died is a
  controller that is GONE (the kernel closes an AF_UNIX fd only when the
  holder process is, `kill -9` included), and an expired one whose
  connection is open is a controller that stopped renewing. The clock
  restarts whenever a live leaseholder appears, so a reconnecting engine
  reprieves it — a deadman that fired on a watched supervisor would be an
  outage generator rather than a safety property. The test pins the
  contrast, not only the firing.
  **`deadman_s` is read back, never declared.** It rides `PING` and `LIST`
  (additive; older clients ignore unknown fields) and the engine records
  what the supervisor SAYS it runs. A reattaching engine meets a supervisor
  it did not start, possibly on a different interval or none — and a bound
  derived from this invocation's flag would then describe nothing. A wrong
  number here is not cosmetic: it is the length of the wait standing
  between an operator and a double run. `--deadman` on an already-running
  supervisor therefore warns rather than pretending.
  **`last_contact` left the semantic projection.** An engine renews every
  twenty seconds; admitting that as an input would move every host row's
  revision three times a minute, making an `expect` on a host unholdable
  and the WAL a heartbeat log. It is the class §3 already excludes for
  `watching` — state that moves with relay activity and no committed
  input — so the lease callback stamps it for free. Safe in the one
  direction that matters: a FRESHER contact only ever delays an eviction,
  and a replay re-seeds it at resume time, which is fresher still. A new
  leader that cannot reach a host therefore counts the bound from its own
  takeover rather than from an inherited value: over-waiting, which is the
  safe way to be wrong.
  **`--deadman` needs `--detached`, loudly.** A tethered run has no
  supervisor, so there is nothing a deadman could bound. Accepting the flag
  and ignoring it would be the worst of both.
  Not closed here: `evict` still rejects at §8's first precondition, whose
  producer — the leader's unreachability detector — is S5d's. What S5b
  adds is that the bound behind it is now computed from a real interval and
  a real contact rather than from rows a test built.
  2007 -> 2014 passed; ruff, mypy, arch_check blocking checks clean. The
  chain was exercised end to end against real processes, not only tests:
  `run --detached --deadman 3`, SIGKILL the engine, the job keeps running,
  three seconds later the supervisor logs the firing and exits, and the
  wrapper records `terminated / parent lost` with `signaled: 15`.
  arch_check now reports five advisory notes and a review-due diff; cli.py's
  `run` crossed the 120-line function note, which is one typer option and
  two validations. Extraction is an architecture change and gets its own
  commit — `_running_deadman` came out of `_serve_run` because it names a
  concern, not to buy back lines.
- DL-96 the effect outbox: intent before the attempt, and the orphaned run
  that proves why (2026-08-15; stage S5c, DL-93's third slice). Closes
  CM-09's application half and CM-06's `outcome_unavailable`. §5's four
  deviations are recorded there as an amendment; this entry is the shape and
  the two bugs it found.
  **The leak that makes it worth its weight.** A kill was a `task.cancel()`
  with no id and no record. An engine that decided TERMINATED and died
  before cancelling left a DETACHED run — whose parent is the supervisor, so
  it survives — and reconciliation walked straight past it, because its job
  is already TERMINAL, which reads as "its completion was already replayed".
  Nothing looked again and the process ran on orphaned, forever. With the
  intent in the log the next engine re-drives it. That is not a new licence:
  runner-design §7 already permits exactly one side effect at resume and
  names it "recorded kills" — the sentence was aspirational for the detached
  path and is now literal. The test pins it against its own contrast: the
  same journal minus the one effect record, and the process survives.
  **A second bug, found by trying to use the first fix.** The reattach
  branch of `SupervisedCommandAdapter.run` returned OUTSIDE the
  cancel handler, so a cancellation never reached the kill ladder: KILLJOB
  against a REATTACHED detached run stopped the adapter task and left the
  process running. One await-and-cancel path for both branches now. It also
  makes `engine.detach.stopping` load-bearing on that path for the first
  time — the CLI already set it, so the production stop is unchanged, but a
  test harness that did not was silently relying on the gap.
  **Built ON the spool, not beside it** (DL-93). Nothing here re-records
  what the lifecycle tier already knows. A pending SPAWN with a run
  directory WAS applied — the engine died in the window between launching
  and recording — so it is reconciled from the spool rather than re-driven,
  which is also what stops the next dispatch from `mkdir()`ing a run
  directory that exists. And an undelivered kill met with no live wrapper is
  resolved three ways from `status.json`: signalled means it landed, exited
  means the run finished first and the kill is retired, and NO record at all
  means `indeterminate`. That third branch is where §5's third state earns
  its keep — two states would report a signal that did land as one that did
  not.
  **The outbox subsumes the derived held set.** DL-94 derived held-ness from
  the oracle's status and said S5c is where intent becomes durable; it is,
  so `held_jobs` is now "the pending SPAWNs" and DL-94's special case in
  reconciliation is deleted. A held start survives a restart as a pending
  effect, which the derivation could not do.
  **Routing gates SPAWN only.** §8's column is about NEW work: `passive`
  says running work continues to completion, and a kill is how running work
  ends. Holding kills during a drain would make KILLJOB stop working exactly
  while an operator is most likely to reach for it.
  **The ghost-run gate moved to planning.** It has always meant "a
  CHANGE_STATUS-parity STARTING overwrite launches nothing"; it now decides
  whether an EFFECT exists rather than whether a task is created, which is
  the honest place — the shell never intended to act, so nothing belongs in
  the log.
  `effect_id` is DERIVED (`e<index>:<KIND>:<job>.<run>`), not minted: one
  admitted input decides at most one effect of each kind per job, so replay
  reconstructs the same outbox without a uuid the log would have to be
  trusted for. `load_json` became public in runner_adapters — the spool
  reader is now used from two modules, and arch_check blocked the private
  cross-module import, correctly.
  2014 -> 2033 passed; ruff, mypy, arch_check blocking checks clean.
- DL-97 quarantine gets a producer; the relay does not get built, and why
  (2026-08-15; stage S5d, DL-93's fourth slice — closing the S5 programme
  at the boundary where honest work stops). §7 carries the deferral as an
  amendment with its trigger; this entry is the shape.
  **What the relay would have cost, and why it is not paid now.** Every
  remaining part of §7 rests on one of two things that do not exist.
  The takeover barrier begins at ACQUIRE, and there is no election until
  S6. The relay is a network transport with mutually authenticated
  principals, and §7 does not say — because it could not usefully — how
  those principals are named, issued or rotated. That design wants one real
  deployment to answer it; freezing it now would freeze the least informed
  version, which is DL-42's own argument against premature extraction
  applied to the same seam from the other side. There is also no second
  machine to test one against, and a loopback relay proves the handshake,
  not the thing a relay exists for. Trigger recorded: build it when there
  is a second execution host to route to, which in practice means alongside
  S6, since a leader that can be superseded is what makes a second host's
  fencing mean anything.
  **Quarantine landed, because on ONE host it is worth having on its own
  account.** A supervisor the engine cannot reach used to fail every spawn
  against it, so an outage that had nothing to do with the estate's jobs
  marked real jobs FAILURE. Quarantined, that work is HELD and resumes when
  the host answers. The producer is the point where the supervisor client
  GIVES UP — five consecutive renewal failures, already loud — not any
  single failure: one refused connection is a blip, and a quarantine per
  blip would hold work for no reason.
  **The leader's verbs take the leader's door.** `quarantine` and
  `reinstate` go through `Engine.inject_host` with no envelope and no
  `expect`, and the wire refuses them outright. §0's mandate is on
  externally REQUESTED mutations; a leader reporting what it cannot reach
  is making an observation, and an operator asserting unreachability would
  be asserting something they cannot know. They are still admitted inputs —
  journaled, replayed — because a quarantine that did not survive a restart
  would let the next engine route work at a host that is not answering.
  **Quarantine remembers what it interrupted.** A host drained for
  maintenance that then stops answering must still be drained when it
  answers again. The operator's intent is not the leader's to revoke, and a
  blip that silently ended a maintenance window would be the worst kind of
  automation. One nullable field, written by one transition and read by one.
  **CM-12 lands as a refusal, not as an automated kill.** Reaching an
  evicted host again does not un-evict it: the returning host must
  re-register at its new generation and self-fence first. The self-fencing
  is the RELAY's act. With no relay, the honest thing is the rule and the
  refusal that names it — an engine that shut down someone else's
  supervisor on a generation mismatch, on a single host where eviction is
  meaningless anyway, would be a destructive automation nobody asked for.
  With quarantine produced, CM-11's permitted half is now reachable with
  every input produced rather than built by hand: the deadman from what the
  supervisor reports (S5b), the contact from a lease exchange (S5b), the
  quarantine from the leader losing the host (here).
  2033 -> 2039 passed; ruff, mypy, arch_check blocking checks clean.
- DL-98 architecture review after S5 (2026-08-15; the DL-75 gate asked, at
  ~5100 lines changed since arch-review/2026-08-15). Subject: what S5 ADDED
  that did not need adding. Three findings acted on, three declined with
  reasons so the next review does not re-find them.
  **Acted (1): the engine had grown a second reader of the spool format,
  and it read the wrong field.** `_kill_outcome_from_spool` decided whether
  a kill landed by looking at `status["observed"]["outcome"]` -- which the
  wrapper writes as FORENSICS about how the command group died, not as its
  verdict. The verdict is the top-level `outcome`, and a mapping for it
  already existed next door and is shared by the live adapter path and
  reconciliation so those two can never diverge. Now `isinstance(
  outcome_from_status(status), Terminated)`, which is the same question
  asked once. The mapping went public for it, as `load_json` did in DL-96 --
  arch_check blocked the private cross-module import both times, correctly.
  The test fixture had encoded the same misreading, which is how a wrong
  reading survives review; it now uses real record shapes and says why.
  **Acted (2): one verb vocabulary, spelled three times.** `HostVerb`,
  `HOST_VERBS` and `LEADER_VERBS` were three hand-kept lists that nothing
  made agree. `LEADER_VERBS` is now derived (`get_args(HostVerb) -
  HOST_VERBS`), which also fails SAFE: a verb added to the type and not to
  the operator set is a leader verb, so it is not reachable from the wire
  until someone says it should be.
  **Acted (3): `_reconcile` had absorbed two outbox concerns.** 164 lines
  and 41 branches, doing supervisor listing, candidate collection, spool
  ladder, never-spawned sweep AND two effect resolutions. The two effect
  halves are now `_reconcile_applied_spawns` and `_retire_lost_spawns` --
  named, because each answers a question a reader asks separately.
  **Declined (1): `_dispatched` and the outbox both record what was
  dispatched.** They look like a parallel model and are not: the outbox is
  exact and durable, `_dispatched` is deliberately OVER-approximated at
  resume (seeded from every job's run_number, not from applied effects), so
  a journal written before the outbox existed, or a run whose effect was
  retired, still refuses a re-dispatch. Collapsing them would trade a
  conservative gate for an exact one, and the exact one is wrong for exactly
  the inputs that are hardest to test.
  **Declined (2): `EffectOutcome.state` has four members where ss5 names
  three.** `retired` is ss7's word ("retire superseded"), not a fourth
  outcome of an attempt: it means the effect was never attempted because
  the world moved past it. Renaming it would lose the distinction that
  matters -- `retired` is safe to forget, `indeterminate` is not.
  **Declined (3): cli.py at 1830 lines, `run` at 129.** Real, and not S5's
  to fix: the growth is one typer option per stage, and splitting the CLI is
  its own commit with its own seam argument. Recorded here so the next
  review sees it was seen.
  Load-bearing, leave alone: the four-state host row (ss8's frozen
  vocabulary, not a flag matrix); `superseded_reason` beside `stale_reason`
  (two gates over two domains -- effects and completions -- that happen to
  rhyme); the derived `held_jobs` projection (one line, but it names what
  the control plane publishes); and every ss/DL citation comment, which is
  this project's core discipline and not noise.
  2039 passed; ruff, mypy, arch_check blocking checks clean.
- DL-99 how S6 is built, and where the leader record lives (2026-08-15;
  sequencing decision, taken before the code so the slices do not re-derive
  it, as DL-93 was for S5). `docs/concurrency-model.md` ss10 names S6 as
  *ledger + election*, and ss1's contract table says what a ledger has to
  provide. Two of its five rows landed with S2 without being called out as
  such: decision lookup by `request_id` IS the `DecisionIndex`, and atomic
  multi-record commit IS the one-line attempt record -- a batch no crash can
  tear in half, because it is a single append. S6 is the other three:
  monotone epoch allocation, epoch-conditional append, and a linearizable
  read of the leader record. Three slices, in this order.
  **The hole this closes is live, and it is not the one the epoch closes.**
  `_serve_run` claims the control socket AFTER `resume_run` has replayed the
  log, reconciled the estate, re-driven recorded kills and appended to the
  WAL. Two `dsl41 run --resume` processes on one run root therefore both act,
  in full, before either is refused -- and the refusal itself is a heuristic:
  a 0.2-second connect probe that UNLINKS a socket it cannot reach. A mutex
  taken after the first side effect is not a mutex. That is what ss7 means by
  beginning the barrier at ACQUIRE, and it is the headline of S6a.
  **S6a election: the lock, the epoch, and eligibility.** An exclusive lock
  on the run root, taken before the log is read and before anything is
  written; the epoch allocated under it and appended as a `leader` record;
  ss7's eligibility gate completed. A refusal names the holder.
  **S6b the fence: epoch-conditional append.** ss7's "losing proof stops
  dispatch, not merely renewal" -- every append re-checks that this process
  still provably holds the run root, and an append is what precedes every
  effect, so a fence on appends is a fence on dispatch without a second
  mechanism to keep in step.
  **S6c the takeover barrier.** ACQUIRE -> reconcile -> retire superseded,
  re-drive pending -> dispatch, ordered explicitly rather than by accident of
  call order, and closing the question DL-96 deferred to it: whether a
  pending SPAWN is re-driven or failed.
  **The leader record lives IN the ledger,** for ss1's own reason about the
  outbox: the epoch is allocated BY appending it, so the allocation and the
  log's account of it cannot disagree, and no crash leaves one without the
  other. The lock file is the mutex and nothing else. It carries the holder's
  pid and epoch so a refusal can name who holds it, and that note is
  diagnostics -- never read as the fence, because a note can be stale and a
  held lock cannot.
  **The mutex needs no liveness heuristic,** which is the whole reason to
  prefer it to what is there now. The kernel releases an `flock` when the
  holder dies, `kill -9` included -- exactly what the connect probe
  approximates, and gets wrong in both directions: an engine wedged past
  200ms loses its socket to a second engine, and a socket left by a crash has
  to be unlinked on a guess. Nothing has to decide whether the previous
  holder is alive. The probe stays where it is, demoted to what it always
  was -- cleanup of a stale socket FILE, not an election.
  **What the substrate provides, and where it stops.** ss1 asks for a
  Postgres-class store and says "whatever provides it": a flock'd file with
  fsync provides all three remaining rows for ONE host and none of them
  across hosts. So S6 does not unblock the relay DL-97 deferred, and should
  not be read as doing so -- a lock on a local filesystem is not a
  linearizable leader record for a second machine, and NFS is where that
  sentence stops being pedantic. What S6 gives the relay is the token it
  would fence on, allocated and moving, rather than a constant.
  **The state-machine version is ss7's other half of eligibility.** The
  header already pins `catalog_hash` and resume already gates on it; the
  version is pinned and gated on nothing. `dsl41_version` is the wrong thing
  to gate on -- it moves for a docs typo, and refusing to resume a live
  estate after a patch release would be an outage manufactured by
  bookkeeping. ss7 says "state-machine version", which is a number that moves
  when the oracle's derivation of state from inputs moves, and that is what
  S6a adds. Absent from a pre-S6 header it reads as 1, on the same courtesy
  S2 gave a journal with no `request_id`.
  **What the epoch is worth on one host, stated without inflation.** It is
  not what stands between this estate and a double run -- the lock is. It
  buys two things now: the log becomes self-describing about which
  incarnation admitted which input, which is what makes a failover
  reconstructible after the fact, and ss4's step-2 ordering (dedup, THEN
  reject an unseen stale epoch) stops being a rule pinned by a test against a
  constant and becomes one exercised by a value that moves. The rest of its
  worth arrives with a second leader, which is S7's matrix to prove.
  **Journal-less engines keep `INERT_EPOCH`.** The bisimulation harness runs
  Engines with no run root and no ledger, so there is nothing for them to be
  elected over. Epoch 0 stops meaning "not implemented" and starts meaning
  "no election was held here", which is a distinction worth keeping rather
  than papering over with a fake epoch 1 on an engine that has no log to
  fence.
- DL-100 election: the lock, the epoch, and the act that used to happen
  before both (2026-08-15; stage S6a, DL-99's first slice). ss1 and ss7 carry
  the amendments; this entry is the shape and the hole it closed.
  **The hole was live.** `_serve_run` claimed the control socket AFTER
  `resume_run` had replayed the log, reconciled the estate, re-driven
  recorded kills and appended to the WAL. Two `dsl41 run --resume` processes
  on one run root therefore both acted, in full, before either was refused --
  and the refusal was a 0.2-second connect probe that UNLINKS a socket it
  cannot reach, so an engine wedged past that timeout loses its socket to a
  second engine. Both halves are now behind an `flock` taken before the log
  is read. The probe stays, demoted to what it always was: cleanup of a stale
  socket FILE, not an election.
  **The mutex needs no liveness heuristic,** which is the whole reason to
  prefer it. The kernel releases an `flock` when the holder dies, `kill -9`
  included, so nothing decides whether the previous holder is alive. A test
  spends a real subprocess on that -- acquire, SIGKILL, acquire again --
  because it is the property the probe could not have.
  **Acquire precedes every ACT, not every append,** and the CLI is where
  that distinction earns its keep: `dsl41 run --detached` STARTS a supervisor
  and takes its lease, so leadership is taken in `_serve_run` before the
  supervisor exists and passed down, rather than inside the engine's own
  entry points where it would already be too late. A refused `--detached`
  resume now leaves no supervisor behind.
  **The epoch is allocated by being appended.** A `leader` record, written
  under the lock, so allocation and the log's account of it are one write --
  the same argument ss1 makes for keeping the outbox in the ledger, one level
  up. It also makes a failover reconstructible: every input between two
  `leader` records was admitted by the incarnation the earlier one names.
  **What the epoch is worth on one host, without inflation.** Not what stands
  between the estate and a double run -- the lock is. What it buys today is
  that ss4 step 2 stops being a rule pinned against a constant: `epoch` is in
  the fingerprint, so an EXACT old-epoch retry (the identical envelope,
  resent to a superseded leader) recovers its original result, while an
  UNSEEN old-epoch request is refused. Both are now tested with a value that
  moves, and their old test could not tell them apart. The corollary is
  worth stating: a client that RE-COMPOSES at the new epoch is not retrying;
  it is reusing an id for a different command, and it gets a collision
  refusal. Retries resend bytes.
  **`INERT_EPOCH` changed meaning rather than retiring.** 0 now means "no
  election was held here" -- an Engine with no run root and no log, which is
  what the bisimulation harness runs -- rather than "not implemented yet".
  Forcing a fake epoch 1 on an engine with no ledger to fence would have lost
  a real distinction.
  **`STATE_MACHINE_VERSION` is ss7's second pin, and deliberately not
  `dsl41_version`.** The package version moves for a docs typo, and refusing
  to resume a live estate after a patch release would be an outage
  manufactured by bookkeeping. This one moves when the derivation from inputs
  to state moves, and nothing a replay cannot see; the bump rule is on the
  constant. A header that pins none reads as 1. No mechanical gate detects a
  missed bump -- unlike IR_VERSION, whose shape arch_check can hash -- so it
  is a discipline, recorded here as one rather than dressed up as a guard.
  2039 -> 2051 passed; ruff, mypy clean, arch_check blocking checks clean
  (runner.py 1389 -> 1454 lines, over the advisory note, and DL-98 already
  declined the same finding for cli.py on the same grounds: splitting it is
  its own commit with its own seam argument).
- DL-101 the fence: every append re-proves leadership (2026-08-15; stage
  S6b, DL-99's second slice). ss7 carries the amendment; this entry is the
  shape.
  **One check, at the append, before the write.** ss1's epoch-conditional
  append and ss7's "losing proof stops dispatch, not merely renewal" are the
  same requirement from two directions, and they cost one `stat` against the
  `fsync` the real domain already pays per record. Before the write, so a
  leader that cannot prove it leads admits nothing -- an engine that appended
  and then noticed would have already admitted the input.
  **Fencing appends fences dispatch, with no second mechanism.** S5c put the
  outbox record BEFORE the attempt, so an append this engine may not make is
  an effect it never applies. That is worth stating because the alternative
  -- a fence on appends AND a fence on dispatch -- is two guards that have to
  agree about a window, and this design has no window for them to disagree
  about. The test asserts the spawn did not happen, not only that the record
  did not.
  **No background prober.** An engine with nothing to append dispatches
  nothing, so the only proof that goes unchecked is proof nothing was about
  to rely on. Adding a timer would buy a faster diagnosis of an idle engine's
  irrelevance, at the cost of a mechanism that can itself be wrong.
  **What losing proof means here is not a lapsed lease** -- there is no
  expiry -- but a lock file deleted or replaced under the holder. Not
  hypothetical: delete the name and the next engine creates a fresh inode,
  flocks it happily, and two leaders run. The test does exactly that and
  asserts the usurper SUCCEEDS, because a fence whose danger is not
  demonstrable is a fence nobody can evaluate. The check does not prevent the
  second engine; it stops the first. Same bargain ss8 strikes for its sibling
  fence: it cannot un-run the duplicate, it stops it continuing and turns a
  silent divergence into a recorded incident.
  **Stopping is stopping, not self-fencing.** The engine raises and the
  existing tethered/detached contract decides what becomes of its wrappers.
  Killing them on losing proof would be reaching for the relay's act (DL-97)
  from the one position that cannot know whether the new leader has already
  adopted them.
  Mutation-tested: with the check disabled all three fence tests red.
  2051 -> 2054 passed; ruff, mypy, arch_check blocking checks clean.
- DL-102 the takeover barrier, and the start that gets re-driven rather than
  failed (2026-08-15; stage S6c, DL-99's third slice, closing S6). ss7 and
  runner-design ss7 carry the amendments; this entry is the shape. Closes
  CM-09's local delivery half.
  **The question DL-96 sent here, answered.** runner-design ss7 fails a start
  with no spool trace rather than re-running it, and DL-41a decided that
  deliberately. It was ONE rule because the log held one kind of evidence.
  It now holds two, so the rule splits at the seam the outbox put there: a
  start whose SPAWN is still PENDING is an intent the previous leader
  recorded and never delivered -- nothing anywhere ran -- and is re-driven at
  the run_number the oracle already decided; a start with no pending intent
  is failed exactly as before. The second case is a journal written before
  S5c, or an effect already resolved whose spool has since gone, and it is
  the case DL-41a was reasoning about. Not overturned, split.
  **Why failing all of them was a real cost.** A crash between deciding a
  start and dispatching it failed a job for a reason that had nothing to do
  with the job -- and FAILURE is not inert here, it routes the estate's
  f()-recovery paths and satisfies failure conditions downstream. On a busy
  estate one crash could do that to every start in flight.
  **Re-driving needed no mechanism, which is the tell that the seam is
  right.** Leaving the effect pending IS the re-drive: dispatch drains the
  outbox through the same gates a fresh effect passes, so a drained or
  quarantined host holds it and this sweep does not have to know that. The
  routing check the sweep used to carry is deleted, and `_retire_lost_spawns`
  with it -- its only caller was the branch that no longer exists. Two rules
  that each knew about routing were one too many.
  **"Reconcile every execution host" is load-bearing, not a turn of phrase.**
  What the supervisor LISTs now joins the sweep's candidate set beside what
  the disk shows. The sweep concludes "never spawned" from ABSENCE, and
  absence that only meant "the run directory is gone" would re-drive a start
  the host is still running -- the double run the model exists to prevent.
  Mutation-tested: without it the test's second process is launched.
  **The barrier ends in a dispatch, and did not before.** `_dispatch` runs on
  the way out of `_admit_and_apply`, so the outbox was drained only when the
  NEXT input arrived. A re-driven start would have waited on unrelated
  traffic -- hours on a quiet estate, forever on one whose only remaining
  work was the run that was lost. The same latency was already there for a
  drain's held work and nobody had met it, because the `activate` that
  released it was itself an input. Also mutation-tested.
  2054 -> 2057 passed; ruff, mypy, arch_check blocking checks clean.
- DL-103 S6 closes, and the relay's trigger did not fire (2026-08-15;
  stage close-out). ss10 carries the amendment. Three slices landed --
  DL-100 election, DL-101 the fence, DL-102 the barrier -- and ss1's five
  ledger capabilities are all provided for one host.
  **The relay was not built here, and that is not an omission.** DL-97
  deferred it with the trigger "build it when there is a second execution
  host to route to, which in practice means alongside S6". S6 has landed and
  the relay has not, so the distinction that sentence packed into "in
  practice" needs unpacking: the trigger is a SECOND EXECUTION HOST, and S6
  was the expected occasion for one, not the condition. Nothing in election,
  the fence or the barrier produces a second machine or answers ss7's open
  question of how mutually authenticated principals are named, issued and
  rotated. The barrier S6c built is the local half -- it reconciles every
  host in the routing table, and that table has one row. What S6 does hand
  the relay is what it was missing: an epoch that is allocated, monotone and
  re-checked on every append, so ss7's "a relay rejects any dispatch carrying
  an epoch below the highest it has seen" names a value that moves.
  **What the obligations table says now.** CM-06, CM-09 (local), CM-10,
  CM-11, CM-13 closed; CM-09's remote half and CM-12's self-fencing wait with
  the relay; CM-14 -- no `(job, run_number)` runs twice over seeded
  interleavings -- is S7, and is the one the whole document exists for.
  **What S6 changed that was not on its list.** Three things, each a real
  defect rather than a stage deliverable: two engines on one run root could
  both replay, reconcile, re-drive kills and append before either was
  refused (DL-100); a start the previous leader decided and never dispatched
  was failed rather than delivered, and FAILURE is not inert -- it routes the
  estate's f()-recovery paths (DL-102); and the outbox was drained only on
  the way out of the next admitted input, so held or re-driven work waited on
  unrelated traffic (DL-102). None of the three was visible before the stage
  that named them.
- DL-104 architecture review after S6 (2026-08-15; the DL-75 gate asked, at
  ~2400 lines changed since arch-review/2026-08-15). Subject: what S6 ADDED
  that did not need adding. Three findings acted on, two declined with
  reasons so the next review does not re-find them.
  **Acted (1): an ownership rule with no caller that exercises it.**
  `start_run` and `resume_run` each carried `owned = lock is None` and
  released only what they had acquired themselves -- three states for a
  reader to hold, in two places, guarding a case that does not occur. The
  only caller that passes a lock is `_serve_run`, and every path where the
  release matters ends in an EngineError that the CLI prints before exiting;
  the lock object is never touched again. One rule now: a failed start or
  resume releases the lock, because a caller that got that far and was
  refused is on its way out.
  **Acted (2): three spellings of "take leadership of this run root".**
  `acquire_run_root` existed for the CLI (which must mkdir first), and both
  engine entry points built and acquired a `LeaderLock` inline. One name for
  one act now, `lock = lock or acquire_run_root(run_root)`, which also puts
  the mkdir where the acquire is rather than trusting each caller to have
  done it.
  **Acted (3): `_reconcile` had absorbed a second question.** The ladder asks
  how runs that DID leave a trace ended; the sweep at its foot asks what to
  do about a start that left none. S6c grew the second one, and 160 lines
  with a closure shared between them is where a reader stops being able to
  tell which question a line answers. Now `_resume_untraced_starts`, with the
  injector promoted from a closure to `_inject_completion` -- named, because
  "this is a completion and therefore subject to the ss4 stale gate" is the
  fact that makes it correct.
  **Declined (1): cli.py at 1842 lines, `_serve_run` at 182.** Real, unchanged
  in kind since DL-98 declined it, and S6 added eight lines of it. Splitting
  the CLI is its own commit with its own seam argument.
  **Declined (2): `Journal` holds the lock and releases it on close.** It
  reads like a module knowing something it should not. It is the definition:
  ss1's ledger is the log plus the mutex that says who may append to it, so
  closing one closes both. An engine that dropped the file and kept the lock
  would exclude its own successor, and splitting the two would need a rule
  to keep them in step -- which is the coupling, spelled out longhand.
  Load-bearing, leave alone: `STATE_MACHINE_VERSION` beside `_ASSUMED_VERSION`
  (not a duplicate -- they diverge the moment the first is bumped, and the
  second is what a pre-S6a header MEANS); the fence in `_write` rather than a
  second guard at dispatch (ss5 records intent before the attempt, so there
  is no window for two guards to disagree about); `LeaderLock.check` reading
  inode identity rather than the holder note (a note can be stale, a held
  lock cannot); `candidates` carrying `Path | None` from three witnesses (one
  question, three ways to know the answer); and every ss/DL citation comment.
  2057 passed; ruff, mypy clean; the `_reconcile` size note cleared.
- DL-105 branch coverage over the concurrency tier, and what asking for it
  found (2026-08-15; an audit before S7, at the user's request: "confirm we
  have tests for all concurrent functionality we manufacture"). The answer
  was "mostly, and here is the list" -- which is not an answer anyone should
  accept from a reading, so the reading was replaced with a gate.
  **The gate.** `coverage` joins the dev extra; CI's bare `pytest -q` becomes
  `coverage run -m pytest -q` plus `coverage report` (running the suite twice
  to measure it once is a minute of CI for nothing). BRANCH coverage, not
  line: this tier is mostly decisions, and a guard whose false arm nothing
  takes is exactly the untested wiring the audit was looking for -- line
  coverage calls such a guard covered. Scope is seven modules, named in
  `[tool.coverage.report]` with the argument beside them: the state owner,
  admission, the ledger, the outbox, the routing table, the journal and the
  engine loop. `runner_supervisor.py` and `runner_wrapper.py` are excluded
  because they run as separate PROCESSES -- in-process coverage reads the
  supervisor at 18% however well its 31 subprocess tests drive it, and a
  number that cannot be true is worse than no number.
  **What it found that the reading did not.** `superseded_reason` was proven
  as a function while the wiring that ACTS on it had never once run: no test
  reached the arm of `_apply_effect` that retires a superseded effect. That
  is ss5's own headline and CM-09's "superseded effects retired", and it was
  green because the decision and the delivery were tested at different
  levels. It has a test now, through the case that makes it reachable on one
  host -- a drain parks a SPAWN, the operator kills the job while it waits,
  and the effect is still there when routing returns.
  **Also closed:** the resume path that resolves an undelivered kill from the
  spool (the function was tested, its only production call site was not); an
  applied eviction (every CM-11 test drove the REJECTION or built an evicted
  row by hand, so `apply_host_command`'s evict arm -- the state change and
  the generation bump a returning relay must clear -- had never run); the
  eviction gate's never-been-in-contact arm; quarantine and reinstate
  idempotence; the envelope's three refusals; the WAL's preflight record,
  double-unsubscribe and empty-interior-line corruption; and six arms of the
  ss7 reconciliation ladder. Plus the two S6a error paths this session's own
  code shipped without: a lock file that vanishes or is replaced DURING the
  acquire, and a refusal whose holder note is unreadable.
  **Four guards are unreachable by construction and now say so.** Each keeps
  a `# pragma: no cover` and a comment naming the invariant that makes it
  unreachable and what would have to break for it to fire -- which is worth
  more than the test would have been, because the invariants live in other
  modules: the oracle refusing to start a live job (DL-81) is what stops
  `_apply_spawn` meeting a stale task; `plan_effects` refusing to plan a KILL
  for a job with no live run is what stops `_apply_kill` meeting one; ss7's
  hash gate is what stops the catalog and the log disagreeing; and an FW
  watch spawning no process is what keeps it out of a candidate set built
  from dispatch records and run directories. A pragma that says only "no
  cover" would have hidden exactly these dependencies.
  **Two accessors were dead and are deleted.** `DecisionIndex.__len__` and
  `Outbox.__len__` had no callers. Coverage is a decent dead-code detector
  when the target is 100%: the honest way to cover an unused method is to
  remove it.
  **What the audit found that no gate can.** The model harness (stage H)
  promised in its own docstring that "partition, reroute and leader failover
  arrive with S5/S6". Both stages have landed and it has none of them: it
  models ONE run root across SEQUENTIAL incarnations, and the six
  `test_cm14_*` cases in it are hand-built scenarios, not ss9's matrix over N
  concurrent engines and seeded interleavings. The docstring now says that
  instead of promising it, and CM-14's row says it too -- S7 starts by paying
  a debt two stages deep, and finding that out at the start of S7 is worth
  more than finding it out in the middle.
  Two smaller ones: `test_cm14_a_run_lost_to_an_engine_crash_is_failed_not_rerun`
  still cited the pre-DL-102 rule as its guard (the test was right, its stated
  reason was a rule that had since narrowed), and DL-102's re-drive test did
  not carry the `cm09` name the obligation table credits it under. CM-01 and
  CM-08 have no `test_cmNN_*` and correctly cannot: one is the arch_check
  gate over the model's AST, the other is "the bisimulation suite stays
  green". The table now says where each is enforced rather than leaving a
  reader to grep for tests that were never going to exist.
  2057 -> 2081 passed; 100% branch on all seven modules; ruff, mypy,
  arch_check blocking checks clean.
- DL-106 the engine loop and the run lifecycle stop sharing a file
  (2026-08-15; taken before S7 rather than during it). `runner.py` had grown
  to 1517 lines and the DL-75 gate had been asking about it since S5; the
  size is the prompt, not the argument.
  **The seam was already in the file, and in the docstring.** A comment bar
  reading `run lifecycle (ss7)` sat at line 993, and DL-74's own sentence has
  said "what stays here is the engine loop AND the run lifecycle" ever since
  the first split. Two things joined by "and". The Engine is the single-
  writer loop over a LIVE estate -- admission, dispatch, effects, all of it
  reacting to inputs as they arrive. What moved runs ONCE per incarnation,
  before that loop exists: create or claim a run root, take leadership,
  replay the log, reconcile the estate against the world, hand back an
  Engine. They share exactly one object, and it is the one the moved half
  constructs. 1517 -> 968 + 605.
  **Not `runner_lifecycle`.** DL-42 spent "lifecycle" on the wrapper and
  supervisor tier, and that meaning is load-bearing across
  docs/supervisor-protocol.md and the counter-fence argument. Two meanings of
  one word in one codebase is how a reader learns to check which is meant.
  `runner_startup` says when it runs, which is the thing that distinguishes
  it from the loop.
  **`start_run` is the barrier's degenerate case,** which is why it belongs
  here rather than staying beside the Engine it builds: ss7's sequence is
  acquire, replay, reconcile, re-drive, dispatch, and over an empty log every
  step but the first has nothing to do. Genesis and takeover are one
  procedure with a different amount of history behind them.
  **No re-export facade** (DL-74's rule, and an arch-review smell in its own
  right): the fourteen call sites -- the CLI, the harnesses, eleven test
  modules -- import from the module that owns the name.
  **The gate found the seam's real cost, twice.** Moving the code turned
  three private helpers into cross-module imports: `_resolve_spool` and
  `_fsync_dir` from runner_adapters, `_last_journal_at` from runner_journal.
  arch_check blocked all three, correctly and for the fourth and fifth time
  in this programme (DL-96's `load_json`, DL-98's `outcome_from_status`).
  They are public now, where they live. A split that had smuggled them
  through would have left the DL-74 DAG intact on paper only.
  **The coverage gate had to be told,** and that is worth recording as a
  hazard rather than a footnote: `[tool.coverage.report]` names its modules
  explicitly (DL-105), so a new module is outside the 100% branch requirement
  until someone adds it. A split is exactly the operation that silently moves
  code out of a gate's scope. Added in the same commit; 100% held.
  2081 passed either side of the move -- no test changed except its import
  line, which is the only evidence a pure move can offer.
- DL-107 how S7 is built, and what CM-14 can honestly close on one host
  (2026-08-15; sequencing decision before the code, as DL-93 was for S5 and
  DL-99 for S6). `docs/concurrency-model.md` ss10 names S7 as *failover /
  partition / double-run matrix over nightbank*, and ss9 says what the matrix
  is: N engines, the ledger, relays and execution hosts over the virtual
  clock, with injected partitions, pauses, message loss and duplication,
  every interleaving reproducible from a seed, and the ss0 safety property
  checked by COUNTING spawns per `(job, run_number)`.
  **S7 starts in debt, and the debt is named.** The model harness (stage H)
  exists, its checkers have teeth, and its docstring promised that
  "partition, reroute and leader failover arrive with S5/S6". Both stages
  landed with none of them (DL-105). What it models today is one run root
  across SEQUENTIAL engine incarnations, with hand-built scenarios standing
  in for the matrix. So the first slice is not new ground -- it is the ground
  two stages walked past.
  **What one host can actually suffer, and therefore what gets injected.**
  Engine death at an arbitrary point; a completion delivered twice or not at
  all; an effect whose outcome was never recorded (the crash window S5c gave
  a name to); a supervisor the engine cannot reach, and the quarantine that
  follows; a routing state changing under in-flight work; leadership lost
  mid-run. Every one of those is producible today and every one has a rule
  it is supposed to obey. Partition BETWEEN leaders and reroute to a second
  host are not injectable, because the second host does not exist -- they are
  absent rather than stubbed, on the harness's own standing principle that a
  stub which always passes is the failure mode it exists to prevent.
  **Seeds, not scenarios.** A hand-built case tests the interleaving its
  author thought of. The driver takes a seed, decides at each step whether to
  fire a fault, and reports the seed on failure -- so a violation is
  reproducible by number and the suite explores rather than illustrates.
  Fixed seeds over a range rather than hypothesis: the run root is real
  filesystem state and the loop is asyncio, so a shrinking search would spend
  its time on setup, and "every seed in 0..N" is already the property.
  **Three slices.** S7a: the harness grows seeds, a fault schedule and the
  fault vocabulary S5/S6 added, and CM-14 is checked over every interleaving
  rather than over six chosen ones. S7b: the same sweep over `nightbank`,
  which ss9 names as the proving ground and whose own test file records the
  gap this programme closes. S7c: the real-process tier, where nightbank's
  manual RUNBOOK path becomes automated -- ss9 calls it a separate tier, and
  it is the only slice whose cost is not obvious in advance.
  **What CM-14 closes here, stated before the work rather than after.** The
  single-host half: no `(job, run_number)` runs twice under engine death,
  message loss, duplication, quarantine, drain or lost leadership, over
  seeded interleavings. ss0's sentence also says "host reroute", and that
  half closes when there is a host to reroute TO -- the same boundary DL-97
  drew for the relay and DL-103 confirmed S6 did not move. The obligation
  table will say which half is which, because a CM-14 marked done that only
  covered one host would be the most expensive kind of wrong.
- DL-108 the seeded sweep, and the model that was wrong about the engine
  (2026-08-15; stage S7a, DL-107's first slice). Closes CM-14's single-host
  half. The harness gains `FaultSchedule` -- what goes wrong and when,
  decided from a seed -- and six more faults, all of them things one host can
  actually suffer: leader failover at an arbitrary point, a spawn decided and
  never acted on, duplicated and stale completions, quarantine, and a drain
  under in-flight work.
  **It found a bug on its first afternoon, and the bug was in the model.**
  Twelve of forty-eight seeds reported `alpha run 1 ran twice`, across a
  failover that lost the effect's outcome record. The engine was right: it
  re-drove a SPAWN that was still pending with no trace anywhere, which is
  exactly what DL-102 says to do. The harness's adapter was wrong -- it ran
  work without leaving the run DIRECTORY every real adapter creates, so a
  spawn that had happened looked to the barrier like one that never did.
  **What that dependency actually is, now that it is written down.**
  `runner_adapters` creates `runs/<job>.<run_number>` with
  `mkdir(parents=True)` and no `exist_ok`, before anything runs, commented "a
  collision is a bug: run_numbers never repeat". TWO rules rest on it and
  neither is visible from the engine: DL-96 deviated from ss5's "bind run_id
  before the attempt" BECAUSE that mkdir makes a second spawn of one run fail
  loudly rather than double; and DL-102's re-drive is sound only because
  anything that ran left the directory behind, so "no trace anywhere" really
  does mean "nothing ran". A model that omitted it was a model of a different
  system -- and the omission was invisible until a seed put a failover in the
  one window where it mattered. The harness now creates the directory the
  same way, with the two rules named in the comment.
  **Adding the trace then hid the branch it exposed,** which is the sort of
  thing a sweep quietly does: with every exec leaving a trace, no seed could
  ever produce a pending SPAWN with nothing on disk, so DL-102's re-drive --
  the newest rule in the barrier -- would never have fired. `lost_dispatch`
  is the fault that restores it: the next exec parks before leaving any
  trace, then the engine dies and its outcome record is cut, which from disk
  and from the spawn log is indistinguishable from dying a moment earlier.
  **Two tests keep the sweep honest, and they are not the same test.** One
  asserts the seeds PLAN every fault in the menu -- a pure function of the
  seed, no runs needed. The other asserts every fault actually FIRES, which
  costs a second of suite time and is the one that catches rot: every fault
  returns False when its precondition is absent, so a driver whose faults had
  all quietly become no-ops would still schedule the full menu and still
  report forty-eight green runs of an ordinary happy path. Mutation-tested --
  making one fault silently no-op reds it.
  **What CM-14 closes, and what it does not.** Landed: no `(job,
  run_number)` runs twice under everything one host can suffer, over
  interleavings a seed chooses. Not landed: ss0's "host reroute", which needs
  a host to reroute TO. The obligation table says which half is which,
  because a CM-14 marked done that covered one host would be the most
  expensive kind of wrong.
  2081 -> 2132 passed; 100% branch held; ruff, mypy, arch_check clean.
- DL-109 the matrix moves onto the proving ground (2026-08-15; stage S7b,
  DL-107's second slice). ss9 names `examples/nightbank` as the estate the
  model's properties are properties OF -- "under injected faults, not an
  assertion in prose" -- and quotes that file's own admission as the gap the
  programme closes. S7a held CM-14 over a four-job fixture; this holds it
  over the real 81-job night.
  **What a real estate adds that size does not describe.** Boxes whose
  members cascade, a resource mutex that makes jobs queue, cross-region
  conditions, a scheduler firing start_times, and a human approval in the
  middle. So a double run does not show up as a second spawn in a log of
  four -- it shows up as a second CASCADE, and a resume that mishandled the
  scheduler shows up as a job that runs twice a quarter-hour apart. 16 seeds,
  44-66 execs each, dispatch continuing across as many as seven engine
  incarnations in one night.
  **The harness needed three things it did not have,** each of which is
  about faithfulness rather than convenience. It takes a lowered CATALOG,
  because nightbank is five files and placeholder substitution before it is
  an estate. It takes a SCHEDULER factory, built fresh per incarnation --
  carrying one across a crash would model a scheduler that never noticed the
  crash, where the real one re-anchors at the last journal instant and dedups
  against the ticks the log holds (DL-45). And it takes what an UNSCRIPTED
  job does: the small fixtures want a job that parks forever (a run a resume
  must not duplicate), an estate driven end to end wants a duration and an
  exit code, or its cascades never advance.
  **A baseline, because `execs > 10` is a weak guard.** A driver that stalled
  the night at its first box would still dispatch more than ten jobs and
  still pass every seed. So one fault-free run asserts the night reaches the
  SOD flip -- the same end state the CLI rehearsal test asserts, reached
  through the engine the sweep perturbs. The seeded runs are then a
  perturbation of something known to work rather than of something unknown.
  **The estate mostly SURVIVES,** which is worth recording because it was not
  the expected shape: across sixteen fault-injected nights, 1142 jobs reached
  SUCCESS and two reached FAILURE. Failover mid-night, a spawn decided and
  never acted on, a drained host and a quarantined one do not, on this
  estate, cost the night -- they cost the runs that were in flight, and the
  barrier picks the rest up. That is the behaviour the programme was built
  for, observed rather than argued.
  ss9's proving-ground paragraph carries the amendment: the virtual-clock
  half of "the live-engine path is exercised manually" is now false. The
  real-PROCESS half stands and is S7c.
  2132 -> 2149 passed in +4s of suite time; 100% branch held.
- DL-110 a doc that cites a test is held to it (2026-08-15; the gate that has
  to exist before the worked examples do). `scripts/arch_check.py` gains a
  fifth blocking check: a `test_...` named in backticks in `docs/`,
  `CLAUDE.md` or `README.md` that no test defines fails the build.
  **Why before the examples and not with them.** The examples this unblocks
  are the documents' falsifiable half -- "this is what the code does, and
  here is what holds it to that" -- and a citation that resolves to nothing
  turns the strongest sentence in a frozen spec into its least trustworthy
  one. Worse, it breaks by ordinary means: renaming a test is a refactor
  nobody thinks of as a documentation change, so the rot is silent and
  arrives through a commit that was right about everything else.
  **It starts green, which is the point of doing it now.** Eight literal test
  names are already cited across the docs (CLAUDE.md's precedence pins, the
  supervisor-protocol lease tests, the autosys dossier's worked example) and
  every one resolves today. The gate is a ratchet from a clean tree, not a
  cleanup.
  **Families are not citations, and the regex is the whole of that rule.**
  `test_cmNN_*`, `test_semXX_*` and `test_sem09*` name a convention rather
  than a function, and every family the docs use carries a `*` or an
  uppercase placeholder -- neither of which can appear between `test_` and a
  closing backtick. A first draft had a second filter for XX/NN placeholders;
  it could not fire, and a second filter is a second thing to be wrong about.
  **The pinned-artifact exception applies.** `tests/test_arch_check.py` builds
  tiny trees under tmp_path for every other check, because a gate that
  asserted on the real tree would red every time the real code legitimately
  changed. This check gets a third test that DOES assert on the real tree,
  for the same reason the IR-F schema pin and the citation index do: the tree
  is the artifact being protected. Mutation-tested -- citing an absent test
  from concurrency-model.md blocks with the file, line and name.
- DL-111 the spec gets worked examples, and the examples find five things
  the text was wrong about (2026-08-15; item 1 of the post-S7b plan, at the
  user's request: "enrich it with representative examples, would be both
  more readable and the proof of being real -- once example is possible").
  Four sections gain one: ss4 an operator kill and the four lines it leaves,
  ss5 the delayed spawn that outlives its own run plus the three-state kill,
  ss7 one failover in order, ss8 the eviction bound in seconds. Every example
  cites the tests that hold it, and DL-110's gate fails the build if one of
  those names stops existing.
  **The examples were GROUNDED by agents and WRITTEN by hand,** which is the
  division that matters for a frozen document: four agents traced the code
  and returned real values, real record shapes and real strings; four more
  tried to refute each trace; every correction they found was applied, and
  every value in the four examples was then reproduced here against the
  shipped code before it went in. Voice and argument are not delegable in a
  document whose value is its argument.
  **What the exercise found, which is the answer to "proof of being real".**
  (1) ss4 says the step-4 batch is "`TimeAdvanced(at)` + `InputAttempt`" --
  two records. It is one, and one is STRONGER: a single line cannot be torn
  in half by a crash. ss1's own DL-100 amendment already described it that
  way; two code comments still asserted the two-record reading and are now
  corrected. (2) ss5 enumerates three effect states; there are four, and
  `retired` is the recorded outcome of the supersession rule the same
  section states three paragraphs later. (3) ss5's "tombstones carry
  fingerprints and reject collisions" is not built and was never amended --
  named now rather than quietly dropped, with why it has not bitten
  (`effect_id` is DERIVED, so a collision means an inconsistent log, not a
  confused client) and what would make it real (an id that is minted, which
  is what a relay needs). (4) DL-96's amendment defends dropping the
  pre-attempt `run_id` binding with "the outbox records the process identity
  the spool reports, when it reports it" -- on the live path it never does;
  only the two resume paths populate it. (5) ss8 says `--force` is "recorded
  with the AUTHENTICATED principal". It records `claimed_actor`, whose own
  docstring calls it a claim and points at control-protocol ss7 gap 2. Force's
  whole safety story rests on that word, so the word is now "claimed", and it
  goes back when there is an authenticated principal to stamp.
  **And one latent bug, fixed rather than documented.** `evict_host` left
  `state_before_quarantine` set, while the field documents itself as non-null
  only while the row is `quarantined` -- and a gated eviction can only start
  FROM quarantined, so every gated eviction falsified it. Harmless today
  (`reinstate_host` refuses a row that is not quarantined) and a loaded gun
  for the next transition someone writes. Eviction clears it.
  **A test whose name promised more than it held.**
  `test_cm11_force_skips_the_preconditions_and_is_recorded_with_its_principal`
  pinned the bypass and checked the attribution by hand-calling the store
  with a principal -- never by letting a forced command supply one. So
  `forced_by=actor if cmd.force else None` was exercised only on its `else`
  side. There is now a test that goes through the verb rather than around it.
  **The one-host table is why half of ss8 is unreproducible by CLI,** and the
  section now says so: `register_host` has one caller, which has one caller,
  and no startup path can set `executor_id`. Every rule in ss8 is implemented
  and tested; what is missing is a second row to point them at.
  2149 -> 2154 passed; 100% branch held; ruff, mypy, arch_check clean.
- DL-112 the layers that were only ever tested by an interpreter meet two
  processes (2026-08-16; stage S7c, item 2 of the post-S7b plan). S0-S4 each
  got a real-process tier as they landed -- the wrapper's kill matrix, the
  supervisor's lease and fencing, the deadman, engine SIGKILL tethered and
  detached, 55 tests across two files. S5 and S6 did not. Everything the
  routing table, the election, the fence and the takeover barrier shipped
  with drives one engine in-process under a virtual clock, which is the
  right instrument for interleavings (S7a/S7b) and cannot ask the question
  this tier asks: an `flock` is a mutex only if a second PROCESS is refused
  it, a fence is a fence only if an unlink is noticed, and a crash window is
  a window only if a process can die inside it.
  `tests/test_runner_leadership.py` is that tier: two real `dsl41 run`
  processes racing one run root (`test_cm14_a_second_engine_is_refused_the_run_root_and_the_first_is_untouched`,
  with `test_a_run_root_whose_holder_was_killed_is_taken_by_the_next_engine`
  as its contrast -- SIGKILL, so nothing releases anything and the kernel is
  the whole cleanup step); a live engine whose lock file is deleted and, in
  the other arm, replaced under it
  (`test_cm14_an_engine_that_cannot_prove_it_leads_stops_dispatching`);
  DL-102's re-drive end to end
  (`test_cm09_a_spawn_that_never_reached_the_host_is_redriven_at_resume`);
  and quarantine produced rather than injected
  (`test_cm09_five_failed_renewals_quarantine_the_host_and_new_work_is_held`).
  Six tests, 6.8 s.
  **Every mechanism claim carries a positive control,** because an empty
  `runs/` is also what a build that never starts anything produces. The
  fence test starts a job through the very verb and socket it then breaks,
  one step earlier, and asserts the directory holds exactly that one; the
  re-drive test reads the job's own stdout; the quarantine test waits for a
  real lease exchange to move `last_contact` before killing the process that
  was stamping it.
  **Scoped to seconds on purpose.** The tier must be ordinary CI, so it
  waits out no bound: what waiting out ss8's real `T_kill` would prove is a
  sum, and the sum is pinned under a controlled clock in test_hosts.py. The
  one thing paid in real time is the renewal loop's five consecutive
  failures -- and the ~4 s floor that count implies is now ASSERTED, because
  giving up on the first blip reaches `quarantined` too, faster, and would
  hold an estate's work over one refused connection.
  **The crash window is picked, not raced.** `_admit_and_apply` journals the
  attempt and its effects, and the loop then dispatches them; the window
  between is two statements wide. `tests/runner_redrive_driver.py` replaces
  `_dispatch` with an untrappable `os._exit`, so what the patch chooses is
  WHEN the process dies -- every record before it was written and fsync'd by
  the ordinary path, nothing after it exists, and the log is the one a
  `kill -9` landing there would leave. Racing for it instead would buy
  nothing and flake.
  **What it found, which is why the tier exists.** `dsl41 sendevent` against
  an engine that died mid-request exited **2**, and 2 is REFUSED: "nothing
  was admitted, no index consumed, the log says NOTHING about it -- safe to
  send again unchanged" (`outcome_of`'s own words). The engine fsyncs an
  attempt BEFORE it feeds it, so a connection that dies after the write may
  have died over a command that is already durably admitted. That is
  `unknown`, whose only safe retry is under the same `request_id`, and the
  CLI was telling operators the opposite. `ControlClientError` now carries
  `delivered` -- set at the write in both clients, defaulted to False
  because the safe default claims less -- and both surfaces read it: the CLI
  exits 4 with the retry id, and the TUI console stops printing "not sent"
  over a command that may be running. Both mutating verbs now go through one
  `_mutate`, which is also the duplicate block they already were.
  **Nine mutations, nine caught,** each by the test that claims it: the mutex
  swallowing its refusal, the epoch not moving, `check()` proving nothing on
  either arm, the barrier not re-driving, unreachability not reaching the
  routing table, a quarantined host routing anyway, quarantine on the first
  blip instead of the fifth, a lease exchange that stops stamping the
  routing row, and nothing dispatching at all.
  **The first draft made three claims it did not hold,** all found by an
  adversarial pass and all fixed rather than reworded. (1) The bound was
  re-derived as `deadman + T_kill + skew(deadman)` where ss8 measures skew
  over the whole wait -- agreeing only because `SKEW_FLOOR_S` dominates
  below ~5000 s, so the test pinned a constant while claiming to pin the
  arithmetic. (2) "A `last_contact` stamped by a real lease exchange" was
  false: `on_contact` is wired after `acquire()` and the supervisor died
  before the first RENEW landed, so the value came from the genesis seed and
  deleting the wiring changed nothing. (3) "Five failed renewals" was in the
  test's NAME and in no assertion. Also fixed: the contrast test sent SIGINT,
  whose orderly path calls `release()` -- so the test named for the kernel
  dropping the lock was exercising a manual unlock, and would have passed
  against the lease design `runner_ledger`'s docstring rejects.
  2154 -> 2165 passed; 100% branch held on the eight gated modules; ruff,
  mypy, arch_check clean.
- DL-113 run history: a projection, not a new record kind (2026-08-16).
  The operational question no surface answered: "how long did this job take,
  run after run, and did it change." Every fact was already durable -- the
  `dispatch` record, `spawn.json`, `status.json` -- and what was missing was a
  key, an index and a query, because `run_number` restarts at 0 in every run
  root (§6 of `docs/deployment-runbook.md`) and run roots are not indexed.
  `dsl41 runs <run-root>... [--job NAME] [--since ISO8601]
  [--format table|json|csv]` folds one or more run roots' `journal.jsonl` +
  `manifest/` + spool into one row per job run --
  `src/dsl41/runner_history.py`'s `RunRow`, the pure `fold_run_rows` (CM-37),
  and the thin I/O shell `read_run_root`/`read_run_roots`. Offline only, no
  control-protocol change (`docs/control-protocol.md` stays frozen at v2).
  **Four decisions.** (1) Clock: a row's `started_at`/`ended_at` come from
  the spool WHOLESALE when spawn.json (and, for a complete run, status.json)
  name that exact run; journal otherwise -- never a per-field mix, which
  `clock_source` records. (2) Boxes get rows: since a box never gets a
  `dispatch` record and its fold is emitted, never journaled, a box row is
  folded entirely from the replayed trace (the Nth `*->STARTING` transition
  opens run N, the next terminal transition closes it), always on the
  journal clock. (3) Incomplete runs never fabricate a duration: still
  RUNNING at journal end, or a `source="reconcile"` completion whose
  payload carries no true `ended_at` (E7, and the "wrapper lost; killed at
  resume" TERMINATED case -- both verified against `runner_startup.py` to
  return `None` for it), get `ended_at: None` and their real recorded
  status, never the resume instant dressed up as an end. (4) Segmentation is
  per JOB, not per estate: every row carries both the journal's
  `catalog_hash` and this job's own `job_hash` (sha256 over the job's lowered
  IR with `span` keys stripped -- `catalog_hash`'s own technique one level
  down), and `--format table`'s labelled break fires on `job_hash`, falling
  back to `catalog_hash` and saying so when either row lacks one. Drawing the
  break from the estate hash alone was the first shape and it is wrong:
  that hash is deliberately conservative, so a release touching twelve jobs
  of eight hundred moves it for all eight hundred and the break fires on
  every job in the estate -- checked against two real `examples/nightbank`
  run roots, where it marked all 519. The per-job hash covers the
  POST-placeholder definition, so an estate whose placeholders vary per run
  (nightbank bakes the run root into `profile`/`std_out_file`) degrades back
  to exactly what `catalog_hash` said; both hashes ride on the row so a
  reader can tell the two situations apart. What this is NOT is a definition
  diff: "changed how, and can state carry across it" is a catalog-diff
  classification, and it is not built. (5) A MISSING `manifest/` degrades
  and a WRONG one refuses. Refusing both would have made the tool unable to
  read exactly the run roots it exists for -- `manifest/` is DL-66, every
  root predating it has none, and retention prunes it (6 of 18 local
  nightbank roots). An absent manifest folds from records alone; a manifest
  whose `catalog_hash` disagrees with the header still refuses, because that
  is a wrong fact rather than a missing one. The cost rides per row in
  `fidelity="records_only"` rather than only in a warning line a JSON or CSV
  consumer never sees: no box rows, no `box_name`/`started_by`/`job_hash`,
  bare-default SEM-09 verdicts, and -- the one that misleads rather than
  omits -- a KILLJOB/`term_run_time` close reading as RUNNING, since the
  trace decision 2 needs is exactly what is unavailable. The CLI also warns
  on stderr for that reason.
  **One thing built beyond the literal proposal.** §6a's sketch reads as if
  a box's completion could be read like a leaf job's; reading `oracle.py`
  showed the fold is entirely emitted, never an admitted input, so a box
  row needs a full replay through a fresh Oracle -- the same one
  `dsl41 journal` already does, with the catalog rebuilt from `manifest/`
  rather than supplied on the command line (DL-66's self-contained artifact
  makes this possible with no new CLI argument). The same replay turned out
  to be the only way to close a leaf run that KILLJOB or a `term_run_time`
  auto-TERMINATE ended, too: both are decided by the oracle synchronously
  while processing the KILLJOB/timer input itself, so neither produces the
  adapter-completion `STATUS` record a pure dispatch+STATUS read would
  need -- reading only those two record kinds would have silently
  misreported such a run as still RUNNING.
  **What is deferred.** A row carries neither an estate nor a period id:
  `run_number` still resets at every re-baseline, so the caller names which
  run roots to combine and `catalog_hash` -- not `run_number` -- is what
  tells two runs of the same job apart across one. Rows are computed on
  demand by the CLI and never materialized at any write time, there being no
  period boundary to materialize them at. Both become changes to an existing
  projection if a carried `run_number` ever arrives, not new decisions.
  `tests/test_run_history.py`: the pure-fold property (folding the same
  records twice reproduces the same rows) and the combine (a two-run-root
  series comes back segmented, never merged into one hash), a suite over the
  spool/journal fallback (including a pruned and a partially-pruned spool),
  the E7 and KILLJOB no-duration cases, and a real-subprocess pair through
  `start_run` + `RealClock` + `LocalCommandAdapter` proving `read_run_root`
  end to end against a real journal, manifest and spool -- 29 tests.
  2165 -> 2194 passed; ruff, mypy, arch_check clean (baseline updated:
  `cli.py`'s advisory line count moved with the new command).
- DL-114 the period model is frozen (2026-08-20). `docs/period-model.md` is
  normative for periods, seals, the carry, the lineage fence and the optional
  run root, in the way `docs/concurrency-model.md` is for admission and
  `docs/control-protocol.md` for the wire: each change to a frozen item needs a
  DL entry. It supersedes `docs/ops-model.md` §1–§3 and §8a–§8b as mechanism;
  `docs/ha-deployment.md` and `docs/ops-model.md` are reconciled to it and say
  so. Its §13 obligations (`PR-\d{2}[a-z]?`, tests `test_prNN_*`) are the risk
  control: nothing ships incrementally, so an obligation weak enough to let a
  broken implementation pass is a defect of the same rank as a wrong mechanism.
  DL-115..DL-127 below are the decisions the document contains, stated once
  each so they are citable; the document carries the reasons.
- DL-115 the directory is not the baseline (2026-08-20; period-model §1). Five
  identities: estate (`estate_id`, uuid4 at genesis), estate root (a path, and
  only a path), period (`period_id`, `baseline_id`; one catalog + one runtime
  profile + one state-machine version), segment (one WAL file), seal, execution.
  **I1**: a period is exactly one segment — there is no size roll; to roll,
  seal. **I2**: indices, epochs and run numbers are monotone across the
  estate. Every periodized root carries a permanent `journal.jsonl` sentinel
  (`rec: period_root`) so an old binary refuses both `run` and `run --resume`
  there. One ownership rule for every root and every anchor: absent → create;
  exact same estate and exact same incomplete transaction → resume; anything
  else → refuse. Rolling the estate root is optional archival hygiene: the seal
  and opening are byte-identical whether the next period continues in place or
  opens a fresh root (PR-24).
- DL-116 the lineage fence (2026-08-20; period-model §1.3). Four head states
  `open | closed | claimed | adopting` in an anchor at a path outside any
  archivable root; `claim_successor` is a CAS idempotent on `claim_id =
  sha256(prev_seal_digest, next_period, realpath(target_root))`, never on a
  PID. Local substrate is `LeaderLock`'s pattern on the anchor — lifetime
  flock, inode-under-pathname re-check before every append, dispatch,
  revision-bearing read and FW spool append; local filesystem only (NFS
  refused); every write by the spool liturgy. A stale claim is break-glass
  (`estate reclaim --force`), attributed. When the shared store arrives
  (ha-deployment S8a) it **replaces** the anchor as the sole authority for term
  and head; two leadership truths is the failure mode. The anchor's `periods`
  map is the archive registry; a period's row is inserted when a root first
  owns it, provisional until its segment is durable.
- DL-117 `baseline_id` is the period's, and derived (2026-08-20; period-model
  §3.4, §4). Every transition derives `baseline_id = sha256(estate_id,
  period_id, stage_digest)`; a command composed under C1 is refused after C2
  opens even when the addressed row never moved. `control-protocol.md`'s wire
  shape does not change; the definition does, from the log's identity to the
  period's. A committed seal's exact retry is answered from the seal record
  before the baseline gate, one seal back; older retries are refused as stale
  (liveness, not safety). Client-proposed `next_period` carries only catalog
  and profile identity; the engine derives `period_id = current + 1`,
  `segment_no = period_id`, `baseline_id`, `clock_domain = current`, and
  `first_index = closes_at_index + 1` after the cutoff.
- DL-118 the atomic `decision` record and control-protocol v3 (2026-08-20;
  period-model §2). `result` plus standalone `effect` records become one
  `decision` line carrying the decision, revisions and effects in admission
  order, `index` not `seq` (DL-89 kept). This closes CM-17 on the file
  substrate: the result-fsynced-effect-not-written window is gone. `header` is
  retired for a self-describing `segment` record; `seal` is added; `leader`
  unchanged. The subscribe stream changes shape, which is a wire break:
  **v3**, on DL-90's precedent — v2 is gone, not deprecated. v3 also carries
  the `seal` verb, the `host` cmd's `route` verb and `routes` query with the
  `route:` namespace, the subscription gap marker, and the exact-retry
  expiry (DL-123).
- DL-119 the seal artifact and the canonical form (2026-08-20; period-model
  §3). Three writes in order: sidecar by the liturgy, `seal` record, anchor
  CAS `open → closed`. The seal carries rows, timers with `timer_seq`,
  `consumed`, `enqueue_counter`, `now` + clock domain,
  `scheduler_admitted_through`, `outbox_pending`, `executions` (a
  discriminated union `pending_spawn | bound | fw_watch` — no `terminating`;
  the sealer waits out a KILL ladder), `classification`, `next_period`, and the
  boundary request; it does not carry `last_contact`, `deadman_us`, the
  decision index, `unresolved`, or anything derived. §3.2's canonical form
  governs every artifact this spec defines under one shared
  `artifact_format_version`: sorted keys at every depth, no floats, every
  typed field present, opaque payloads preserved, Unicode scalar values at
  every ingress, pinned escaping, golden vectors. A seal is an authoritative
  checkpoint reproducible from its opening checkpoint and its period's inputs
  — "verified" means re-derived by `audit`, not self-consistent; `verify`
  (attestation binding) is a different verb; attestation N requires N−1.
- DL-120 capacity decomposed (2026-08-20; period-model §5). DL-86's move
  finished: `CapacityReservation {bucket, units > 0, release_policy}` and
  `waiter_seq` live on `JobRuntime`; `consumed` (≥ 0, ghost buckets survive a
  resource's removal) and `enqueue_counter` under `RuntimeState`;
  `CapacityPool` becomes a pure function of (catalog, rows, consumed).
  Reason: `_bucket_used` summed units held by live runs with units permanently
  spent, and a seal recomputing usage from holders would refund every
  depletable (SEM-16 inverted). DL-86's invariant — no pool change without a
  row change — was true and is not the property a seal needs, which is
  reconstructibility from those rows.
- DL-121 `run_id` is minted in the effect; SPAWN idempotency is durable; FW has
  a spool (2026-08-20; period-model §2.3, §3.5, §11a). DL-96's deferral is
  lifted: `plan_effects` mints `run_id` and every SPAWN effect carries it, with
  `{executor_id, generation}` from birth. The supervisor's SPAWN idempotency is
  directory-backed — `mkdir`, `run_id` index, `receipt.json`, spawn,
  `reply.json`, answer — resolved through the index and answered from the
  directory, so it outlives `LIST` presence and a supervisor restart; a
  detached run's directory is created by the supervisor; `run_id` has a
  filename-safe grammar at the wire. The FW adapter writes an append-only
  `watch.jsonl` — a `start` line on dispatch, then one line per poll, under the
  fence — and the seal carries `watch_seq`, not wall time.
- DL-122 `catalog_hash` v2, `source_bundle_hash`, `RuntimeProfile` (2026-08-20;
  period-model §1.1, §2.1). `catalog_hash` v2 is the canonical `CatalogIR` with
  `meta` projected to `{source_files}` — `tool_version` and `parsed_at` leave,
  spans stay — carried with `catalog_hash_version: 2`; today's hash moves on a
  patch release, which is DL-100's "outage manufactured by bookkeeping".
  `catalogs/` is addressed by `source_bundle_hash` over the ordered,
  length-framed post-placeholder bytes. A period's semantics are `(catalog_hash,
  runtime_hash, state_machine_version)`; `runtime_hash` is over a typed
  `RuntimeProfile` (timezone basis, `as_machine`, policy, mode, durations in
  µs, `retry_horizon_us`) — not the route table. Two manifests: the CLI stages
  `staged_manifest.json`; the engine writes `manifest.json` with the derived
  fields at install.
- DL-123 classification, latches, the retry horizon (2026-08-20; period-model
  §9, §10). Three tiers: executing → R; latent intent (`armed`, QUE_WAIT,
  semantic timer) → A except removed → R; not live → carry. `pending_spawn` is
  executing. The graph runs job → dependency over jobs, globals, `name^INST`,
  boxes (both ways, nested), resources, machines, calendars, the timezone
  basis, and runtime-profile fields per kind (`retry_horizon_us` reaches no
  job); IR-G is one input, reversed. E19 closes: a member changed while its
  box is executing is R. **Armed latches cross a release** — the runbook's
  "latches die with the old baseline" becomes false; one held tick under C1 →
  exactly one start after C2. The gate counts the last admitted externally
  requested attempt with a durable decision (rejected and no-op included)
  against the **closing** period's `retry_horizon_us`; naming a horizon
  weakens `control-protocol.md` §3's unbounded exact-retry promise (no wire
  change, this entry).
- DL-124 the seal operation (2026-08-20; period-model §6–§8). `dsl41 seal` in
  live mode (control verb; the engine seals in its single-writer loop and
  exits 3) or offline mode (takes both locks, runs the recovery barrier);
  both stage C2, run three loader phases (`validate_staged` before the barrier,
  `validate_boundary` after, `open_from_seal` at resume, each over a typed
  context), and hand off to `run --resume` or `run --open-from`. The `seal`
  record is the boundary's own decision; an uncommitted request is unseen and
  retries afresh; the seal append is the point of no return — failure there
  fail-stops with an unknown outcome and recovery decides after a confirming
  `fsync`. The barrier places no holds (there is one hold bit; it is the
  operator's); the reversible interval ends before the append and
  `abort_boundary` runs on every exit inside it. The cutoff watermark is
  `scheduler_admitted_through: T`; C1 owns ticks ≤ T. **A transition may not
  change `state_machine_version`**: an SM bump stays a new estate.
- DL-125 adoption (2026-08-20; period-model §11). `dsl41 estate adopt`: take
  both locks, mint `estate_id`, reconstruct legacy state read-only, phase-1
  readiness, then fence (hard-link, sentinel over `journal.jsonl`), then
  authority (`absent → adopting`), then a dispatch-free recovery barrier under
  the adopter's own `leader` term, translate into a conforming segment
  (`result`+`effect` folded into `decision` with `legacy_batch: true`), seal as
  period 1 through the common body, `adopting → closed`. Refuses a live
  wrapper, a live FW, a pending legacy outbox, or an admitted input without
  a durable `result`. Retries under the same `estate_id` with a corrected C2.
- DL-126 `deadman_s` leaves the host semantic projection (2026-08-20;
  concurrency-model §3 amended by period-model §3.3). It joins `last_contact`
  in `_UNPROJECTED_HOST`: it is observed liveness configuration read back from
  the supervisor, registration changes it with no journal record, and a
  projected change there moved `state_rev` past what audit could derive. The
  eviction gate reads the current row value regardless. A host row's deadman
  is null until the host re-registers in a new period, and a null deadman is
  not evictable except by force. An evicted host's return stays the relay's
  (CM-12); this spec records nothing for it.
- DL-127 retention floors (2026-08-20; period-model §11a, §12). Policy is a
  business decision; the floors are not: never prune the sentinel, anchor,
  active claim, the opening and closing sidecars, current and committed-next
  manifests and bundles, an uncommitted candidate's artifacts, the latest
  attestation checkpoint, the WAL and spool of any unattested period (E20
  gates the rest), the spool of any live or carried execution, or any SPAWN
  tombstone whose effect can still be replayed — deleting one authorizes a
  spawn.
- DL-128 canonical form: "control character" is Unicode Cc (2026-08-20;
  period-model §3.2 amended at build of `canon.py`). The frozen text said
  "every other control character `\u00XX`" and the first implementation read
  it as C0 + DEL, leaving U+0080–U+009F raw. The set is fixed as the Unicode
  category: U+0000–U+001F, U+007F, U+0080–U+009F. Reason: the category is the
  only reading with no second interpretation, and the `\u00XX` template names
  exactly that range. The golden vector (PR-08) pins it.
- DL-129 SPAWN idempotency outlives the supervisor, and the watch keeps a log
  (2026-08-20; period-model §11a and §2.2, built as PR-36 / PR-34 / PR-34a).
  The supervisor's dedup was an entry in `self.runs`, so a bounded LIST and a
  delayed duplicate SPAWN together made a second execution. The store is the
  run **directory** now. A detached run's directory is created by the
  supervisor on receipt (the engine keeps it for tethered runs only), and the
  write order is `mkdir`, the `runs/.by_run_id/<run_id>` index, `receipt.json`,
  the wrapper, `reply.json`, the answer — index before receipt, receipt before
  the fork. A replay resolves through the index, never the incoming path, and
  answers from the directory: the original `reply.json`, or the same two facts
  reconstructed from `spawn.json`; `collision` for a second identity on either
  side of the one-to-one map or a changed `spec_fingerprint`; `in_progress`
  while the wrapper lives; `indeterminate` where the crash left no answer.
  `run_id` is checked against the uuid4 grammar at the wire, and `run_dir`
  must be the path the supervisor owns (compared resolved, not spelled: the
  engine and the supervisor are told the run root separately). A receiptless
  directory holding a wrapper record predates the protocol and is never
  reused. `in_progress` is retryable, not a completion -- the detached adapter
  awaits the outcome of the run that is already forked rather than failing a
  live process. The index directory grows one entry per run and the retention
  floor bounds it; the only scan of it is the orphan case, after a crash. The three new files are §3.2-canonical
  and liturgy-written; the wrapper's two are unchanged. LIST keeps live runs
  plus a bounded window of recent completions. The FW adapter gains the
  append-only `watch.jsonl` — a `start` line on dispatch, then one write-ahead
  line per poll, no-change polls included — so `next_poll_at` and the
  stable-poll count are derived from evidence rather than from a task's
  memory. The §11 ladder reads it: a `start` line resolves the pending SPAWN
  it names, and a completing final line is injected as the completion. The
  engine side closes PR-36a: at resume, a supervised run whose bound effect
  has no spool evidence is REPLAYED through the idempotent SPAWN rather than
  failed by guesswork; the wire spec is validated against the whole frozen
  §2 schema (unknown keys refused — the typed float encoding is only
  injective over pinned key types) before anything durable; corruption is
  never read as absence (an unreadable index entry answers indeterminate,
  never first-application); every record consulted for an answer or a
  signal must NAME the run; and the watch fold refuses a line of unknown
  kind or foreign identity. The
  per-poll anchor-fence re-check and the §6 seal barrier are not built here.
- DL-130 a period gets its name, its inputs and its launch options
  (2026-08-20; period-model ss1.1 and ss2.1, built as PR-07a / PR-08a /
  PR-08c / PR-15 / PR-15a / PR-22's first half). Four things land together
  because they are one identity. (1) **`catalog_hash` v2.** v1 hashed the
  whole `CatalogIR` including `CatalogMeta.tool_version`, so 1.2.3 -> 1.2.4
  moved the hash of an unchanged estate and leader eligibility refused to
  resume it -- the outage DL-100 refused to manufacture for the
  state-machine version, arriving by the other door. v2 is sha256 over the
  ss3.2 canonical form with `meta` projected to `{source_files}`:
  `tool_version` and `parsed_at` leave, spans STAY (a relocated or
  reordered estate is still a different estate). This changes a frozen
  identity, so it is versioned rather than swapped: `catalog_hash_version`
  rides on the record and on the manifest, `catalog_hash_v1` stays
  computable, and every gate recomputes under the recipe the log itself
  names -- v1 for a legacy `header`, v2 for a `segment`. Comparing across
  recipes would have refused every journal in existence, in one direction
  or the other. (2) **`source_bundle_hash`**, framed normatively: inputs in
  command-line order, `len(path) || path || len(bytes) || bytes` with
  8-byte big-endian lengths, sha256 over the concatenation. The framing is
  what stops `["ab", "c"]` colliding with `["a", "bc"]`; the order is kept
  rather than sorted away, because `catalog_hash` covers `source_files` and
  the spans, so one address for two orderings would map one directory to
  two catalog hashes. `catalogs/<digest>/` holds the post-placeholder JIL
  byte-exact plus `sources.json`, content-addressed and reused rather than
  rewritten. The directory is named by the digest's hex half and the value
  is spelled `sha256:...`: a colon in an archived path is a remote host to
  `rsync` and `scp`, and an estate root is a thing operators archive. (3)
  **`RuntimeProfile` and `runtime_hash`.** The launch options that change
  interpretation or dispatch, as a typed frozen model rather than an open
  list, so a field added later is hashed by construction; every duration is
  microseconds validated against its own bound, `deadman_us` is the only
  nullable, `as_machine` is sorted and de-duplicated. Identical JIL under
  two timezones has one `catalog_hash` and two sets of ticks, and without
  this nothing would say the period changed. The PR-15 sweep is derived
  from `model_fields` -- the DL-83 discipline -- so a new field fails the
  suite until it has a case. (4) **The `segment` record replaces
  `header`.** A once-per-log header cannot describe a log made of segments;
  a segment is self-describing (estate, period, baseline, both hashes and
  their versions, clock domain, first index) and carries no
  `dsl41_version`, so two openings under two patch releases can be
  byte-identical. Nothing writes a `header`; every reader accepts one.
  `periods/000001/manifest.json` is installed before the log opens and both
  are derived from ONE object, so they cannot disagree at birth; resume
  checks them against each other and names both sides of any disagreement.
  A root that stores no inputs -- an unstaged embedder, a pruned
  archive -- degrades to `records_only` in `dsl41 runs` rather than
  refusing, which is DL-113 decision 5 unchanged. `dsl41 rehearse
  --run-root` stages too: a rehearsal is evidence about production
  behavior, and its `--timezone` is the option most able to make it
  evidence about something else. The legacy `manifest/` is
  no longer written and is still read. Two readings the spec left open are
  recorded rather than hidden: `sources.json` records the sha256 of the
  POST-placeholder bytes (the bytes the directory holds and the address is
  taken over, so the bundle verifies against itself), and
  `retry_horizon_us` defaults to 60s, which is what ss9's own worked
  example gives the closing period -- the gate that reads it is a later
  unit. The adversarial pass added four
  refusals the first cut left implicit: a `catalog_hash_version` this
  binary does not implement is refused BY NAME rather than recomputed
  under v1 and reported as "the estate changed" (which would tell an
  operator to abandon a live estate); an unreadable period manifest is an
  `EngineError` naming the file rather than a decoder error escaping
  `dsl41 runs` as a traceback; bundle completeness is `sources.json` and
  not the directory, with the bundle assembled in a temp directory and
  renamed in, so a crash mid-write cannot poison an address forever and a
  concurrent writer of the same bytes is not an error; and the manifest
  pins the deadman the supervisor REALLY runs, not the one the invocation
  asked for -- the same rule DL-126 applies to the routing table, for the
  same reason. Out of scope by construction: the seal, `wal/`, the sentinel, the
  anchor, `periods/.staging/` and adoption. 2325 -> 2410 passed; ruff,
  mypy and arch_check clean.
- DL-131 the classifier reads a graph nobody had, in two directions
  (2026-08-20; period-model ss10, built as PR-37, PR-37a, PR-38..PR-44).
  `classify.py` answers one question per boundary: C1 is open and holds
  live work, C2 is staged -- may the boundary commit, and what must the
  seal record about what it carried? Three answers, R / A / carry, one per
  job of either catalog, in name order, so phase 2's map is reproducible
  byte for byte.
  (1) **The graph is built for the purpose.** Neither the per-job
  fingerprint nor IR-G computes the blast radius: IR-G has job and global
  nodes only, and no resource, machine, calendar, cycle, external instance,
  timezone or runtime-profile node exists there. So the module builds its
  own -- ten node kinds, each with a stated "changed when" -- and takes
  IR-G for the BOX TOPOLOGY alone. The condition atoms are walked
  DIRECTLY off `JobIR.iter_conditions()` (condition, box_success,
  box_failure) -- never off IR-G's edge list, which diverts a local
  unqualified n() into `mutex_groups` (M07) and keeps no edge for it: a
  reader that "reversed IR-G" would carry a boundary over `b: condition:
  n(a)` while b executes and C2 changes a.
  (2) **The edges run FROM a job TO what it depends on**, the profile
  fields included, so a live CMD's forward closure reaches
  `profile:cmd_grace_us` and a C2 that changes only the kill ladder's grace
  cannot commit over a live run. The reversed spelling reaches no profile
  field from any job and passes every other obligation, which is why
  `test_pr37a_profile_edges_run_from_job_to_field` asserts the CLOSURE and
  not only a verdict.
  (3) **Two directions, both computed.** The R gate is a job's forward
  closure; the boundary-truth diff is a changed node's reverse closure plus
  condition truth under both catalogs at the one carried state. Neither
  substitutes for the other and
  `test_ss10_2_forward_and_reverse_answer_different_questions` shows a case
  each way.
  (4) **Truth has one authority.** The diff evaluates through the
  interpreter itself -- a throwaway `Oracle` seeded with the carried rows
  -- rather than a second evaluator: SEM-05's iced predecessor, SEM-06's
  undefined atom and the lookback ladder are semantics, and a classifier
  that re-derived them would answer a question the engine does not ask.
  Six readings the spec left open are recorded rather than hidden. A box
  containment node's value is the containment RELATION under it, not the
  member SET: moving a leaf from an inner box up to the outer one leaves
  the outer set identical and is exactly the "at any nesting depth" case
  the rule is about. A member depends on two facts about its box -- the
  containment set and the box's own definition -- because the box's
  condition and schedule gate the member. The graph unions C1's and C2's
  edges, which can only add a changed node to a closure, never hide one. A
  resource node is `(amount, res_type)`: the release-policy default IS
  `res_type` (`capacity._release_policy`), so the pair says all three
  things ss10.2 names. The timezone basis stays a node beside the two
  profile fields it is computed from, because ss10.2 lists both and a
  report that named only a field would not say what moved. And the armed
  assumption is keyed on a moved SCHEDULE or CONDITION, ss10.3's own two
  things: an armed job whose command changed is still an A, but telling an
  operator "the C1 trigger survives under C2 gating" when the gating is
  what did not move misnames the risk, so that case takes the general
  sentence. Ghosts and changed-but-not-live are disjoint lists, and a job
  only C2 has is out of the readiness diff -- it has no C1 truth to differ
  from. A carried row is seeded for the truth
  diff even when C2 no longer has the job: the ghost row is RETAINED across
  the boundary, so the opened period holds it and an atom naming it reads
  it -- SEM-06's "undefined is false" is about a job with no row, not about
  a job with no definition, and L001 refuses the condition that would make
  the difference visible. `runner_history._job_fingerprints` moved to
  `period.job_fingerprints` unchanged, because a pure analysis pass may not
  import a private name out of a runner module, and the leaf test is
  identity, which is what `period.py` holds; ss10.2 names it under its old
  home, so ss15 carries the amendment and `ops-model.md` ss8a.4 is
  corrected. `citation-index.md` gains the `C[12]` row: C1 and C2 are the
  vocabulary this programme is written in, and the gate is right that a
  token nobody can resolve is not one. Out of scope by construction: the
  seal operation that calls this (ss6/ss7), the carried-state snapshot the
  seal builds, and PR-26 -- one tick under C1 while held, exactly one start
  after C2 opens -- which is a runtime obligation and needs an engine.
  2442 -> 2509 passed; ruff, mypy and arch_check clean. An adversarial
  review pass found no blocker and one leaking test -- a SEM-05 arm whose
  two cases were both false under the rule and without it -- rebuilt so
  that removing the ice branch from the interpreter reds it.
- DL-132 the period writes itself down: one sidecar, and the two functions
  over it (2026-08-20; period-model ss3 and ss4, built as PR-05b, PR-05c,
  PR-07's opening half, PR-08, PR-08d, PR-10a..PR-14, PR-18a, PR-19a,
  PR-20, PR-21, PR-22, PR-22a, PR-24a and PR-47d). `seal.py` is the
  artifact ss3.1 shapes and the two pure functions over it: CLOSE turns a
  runtime snapshot into a `Seal`, OPEN turns a sidecar into an
  `OpenedRuntime`. Neither touches a clock, a socket, an adapter or a
  disk.
  (1) **The artifact IS the model.** Every section is a frozen
  `extra="forbid"` model and the wire keys are the field names, so a
  section this binary does not know is a refusal rather than a silent
  drop. `digest` is deliberately NOT a field: it is a pure function of
  everything else, stamped on the way out and checked on the way in, and a
  stored copy would be a second authority an artifact could disagree with
  itself about. No section is named `digest`, because only the top-level
  key is stripped (PR-13).
  (2) **A `Seal` object is always a valid seal.** Every ss7 phase-3 step-6
  invariant that reads the artifact ALONE is a model validator rather than
  a check inside one of the functions: with the checks in `open` alone,
  `close` could write a sidecar nothing opens; with them in `close` alone,
  a tampered file would pass. Two checks need a fact the artifact does not
  carry, and both are parameters of `open_from_seal`: the naming record's
  digest and the opening period's committed manifest. A third, ss3.5's
  CMD-or-FW half, needs C2's catalog and therefore belongs to the loader
  that holds one.
  (3) **The two ss3.3 exclusions are typed, not filtered.** `SealedHost`
  has no `last_contact` field at all and its `deadman_us` is typed `None`,
  so carrying either is not expressible; `SealedState` also accepts live
  `HostRuntime` rows and projects them, so a caller cannot forget the
  projection. The exclusion test derives its expectation from
  `HostRuntime.model_fields`, so a field added to the host row later lands
  in the seal or reds. `start_period` (PR-50) landed on `JobRuntime` in
  the same unit's review round: set beside `run_number` at the actual
  start from `RuntimeState._period_id`, whose writes are exactly two
  verbs, split explicitly rather than inferred: `seed_period` (assembly's
  first act -- one-shot, never inside or after a committed input; the
  constructor's replay-identical genesis seeding is discounted by a
  one-shot `finish_genesis`) and `open_period` (a live state's advance by
  exactly one, never inside an input); the seal bounds the stamp to
  [1, period_id].
  (4) **`routes` is modelled and projected.** ss3.3 makes it a row like
  the other three, owned by `RuntimeState` and remapped by the `host`
  cmd's `route` verb -- and neither the storage nor the verb exists.
  Today every effect is born for one local executor, so `implicit_routes`
  projects exactly one row whose role IS that executor's id at revision 0,
  because no verb that could have moved it exists. The row shape is the
  frozen one, so the unit that adds the storage changes the producer and
  not the artifact.
  (5) **Order is applied by the writer and REQUIRED of the reader.**
  `close_runtime` sorts `outbox_pending` and `executions` by
  `(index, effect_id)`; `open_from_seal` refuses a document that is out of
  that order rather than quietly sorting it, because a reader that sorted
  would hide a writer that never did. Timers are the same, on
  `(due, token)`.
  (6) Seven readings the spec left to the implementation are recorded
  rather than hidden. `class` is the wire key for a verdict, exactly as
  ss3.1's block spells it -- a Python keyword, so the field is `verdict`
  under an alias. The seal carries the WHOLE authoritative timer heap, not
  the interpreter's live subset: a fire already discards a stale entry by
  its own rule, and dropping one at the boundary would be the engine
  editing authoritative state on the way out. An aware datetime is
  REFUSED, where `canon` would convert it, because the artifact compares
  instants -- `now`, `scheduler_admitted_through` and T are one value --
  and a mixed pair would raise a `TypeError` out of a validator instead of
  naming the field. A committed seal never carries an R verdict, at both
  ends: ss10.1 says the boundary does not commit while one exists. An
  execution's `run_id` is required and held to ss11a's grammar. A seal's
  `deadman_us` is always null and the field stays present, because ss3.2
  requires a typed field to be present rather than absent. And ss3.2's
  reservation sentence has two halves that land in two places: duplicate
  buckets are REJECTED at validation, and the vector SORTS at projection,
  because the row keeps the order it acquired in.
  (7) The digest is checked twice, with two messages: over the document as
  decoded, before the model is built -- so a mutation that also breaks the
  schema is still reported as a tamper -- and then against the canonical
  form, so a file whose own digest is right but whose bytes are not ss3.2's
  is refused too. `next_period.baseline_id` is re-derived on every read
  from `{estate_id, period_id, stage_digest}`, so "derived, not minted"
  (PR-47d) is checkable by every reader and not only by `audit`. Two rules
  the spec states and a first pass only stated back: the opening's own
  `artifact_format_version` is checked against this binary (ss8, PR-08d) --
  it is a staged field, so a case that moves it must re-derive the baseline
  or it is caught by the wrong rule -- and `run_dir` is refused unless it is
  relative to the estate root (ss3.5), because an absolute one is the
  physical roll's silent failure and the adapter that records a run
  directory today records an absolute path.
  (8) Three small changes outside the module. `RuntimeState` publishes
  `timer_seq`, on `enqueue_counter`'s rule: the seal carries the
  allocator's high-water mark, the heap can be empty while the allocator
  stands at 41, and an opener that restarted from 0 would re-issue tokens
  the carried firing order was written in -- and `scripts/arch_check.py`'s
  ownership gate gains it beside the other allocator, because a gate that
  watched one of the two would be narrower the day after it was written
  (PR-52). `segment_record` takes `opens_from_seal`, so the opening segment
  keeps ONE writer and the unit above copies the two derived fields rather
  than recomputing them; and because that field became writable,
  `check_segment_record` now holds it to ss2.1's shape -- `{period_id,
  digest}`, null on segment 1 and non-null on every later one, since every
  later segment opens a period and a period opens from a seal. "Any dict"
  would have let a segment name its seal by a key nothing reads.
  Out of scope by construction: the cutoff barrier (ss6), the seal
  operation (ss7's two modes, staging, the three-write liturgy, the abort),
  ss8's preconditions, the `seal` record, the anchor and the lineage fence,
  adoption, and every CLI verb. The fold that turns the first `watch_seq`
  lines of a `watch.jsonl` into an `fw_watch` entry stays with the reader of
  that evidence: an artifact module that imported the adapter tier to spell
  the projection would invert the layering to save a rename.
  `OpenedRuntime` holds the SEAL-derived
  half and states the five seeding rules the loader follows; assembling a
  `RuntimeState` from it needs an install-verbatim verb and the `routes`
  row, both of which belong to the unit that seeds an engine -- and ss7 is
  explicit that a pure function may not return an `Engine`.
  2530 -> 2650 collected, 120 new, all passing with 100% branch coverage of
  `seal.py`; ruff, mypy and arch_check clean. An adversarial review pass
  found one blocker and eight should-fixes, all folded in: the opening's
  `artifact_format_version` was unchecked and the test that named the rule
  passed on a hex substring of the baseline refusal; three sweep cases
  tripped a neighbouring rule rather than their own, so every case now
  carries the message fragment only its own rule produces and the two
  `next_period` cases re-derive the baseline they moved; the ghost-run
  gate's `run_number > 0` filter, the shared-manifest field set and the
  `run_dir` rule had no case at all. Each fix was mutation-checked --
  neutralise the rule, and exactly its case reds. One duplicate rule was
  removed rather than tested twice: `from_bytes` no longer re-checks what
  `from_payload` checks. The close-side R gate was REMOVED as a duplicate
  and then PUT BACK by review: the model refuses an R in the projected
  MAP, but the projection is a dict build, and two verdicts for one job
  would let a later carry overwrite an R before the model ever saw it --
  so `close_runtime` refuses on `Classification.refused` and on duplicate
  verdicts, on the classifier's own object.

- DL-133 the period gets a boundary: the anchor, the cutoff, and the three
  writes that end a period (2026-08-20; period-model ss1.1, ss1.2, ss1.3,
  ss6, ss7, ss8, ss9 and ss11 steps 1-4, built as U6b -- PR-01a, PR-01b,
  PR-01c, PR-02, PR-02b, PR-02c, PR-03, PR-04, PR-05, PR-05b, PR-07's
  operational half, PR-25, PR-25a, PR-26, PR-27, PR-28, PR-28b, PR-28d,
  PR-28e, PR-29, PR-30, PR-30a, PR-30b, PR-30c, PR-30d, PR-30e, PR-30f,
  PR-32, PR-33, PR-34's barrier half, PR-45's in-place rows and PR-46;
  PR-27's engine-visible and supervisor clauses -- see (13) -- and PR-31
  not at all, because a bound detached run crossing a boundary live is
  first exercised with the `seal` CLI surface).
  `seal.py` is the artifact; `boundary.py` is the OPERATION -- what must be
  true before a period may close, in what order the bytes hit the disk,
  which single writer performs the cutoff, and who may open next.
  (1) **The layout moved, and `journal.jsonl` stayed.** A periodized root's
  records live in `wal/000001.jsonl`; the file at the old name is the
  one-line `period_root` SENTINEL. Keeping the NAME is the point: an old
  binary refuses `run` because a `journal.jsonl` exists and refuses `run
  --resume` because the first record is not `header`, and there is no
  instant at which the file is absent. Every reader follows the sentinel's
  `see` through ONE function, `period.resolve_wal`, so a caller holding a
  run root, a sentinel or a segment reads the same records and none of them
  gets an opinion about the layout. A legacy root -- `journal.jsonl` IS the
  WAL -- keeps working unchanged, and the first record's kind is what tells
  the two apart.
  (2) **One ownership rule, one implementation.** ss1.1 states the rule
  once -- absent creates, the same estate and the same incomplete
  transaction resumes, anything else refuses -- and `period.own_or_refuse`
  is it. The root and the anchor both call it and each supplies its own
  answer to "is this ours", because only the caller knows which incomplete
  transaction it is resuming. Genesis is now ss1.1's six-step transaction
  in that order: `leader.lock`, the sentinel, `anchor.lock` plus the
  create-only CAS, the bundle and manifest, the segment, the finalize CAS.
  A crash anywhere in it re-runs idempotently and reads `estate_id` BACK
  from the sentinel rather than minting a second (PR-01a).
  (3) **The anchor is `LeaderLock`'s pattern, generalized** (ss15's row):
  same file-replacement fence, same process-lifetime hold, a different name
  and a different noun in the refusal. The run-root spelling is the
  default, so a legacy root's `leader.lock` and its refusals are
  byte-identical to what every earlier build wrote. What is new is
  `Fence` -- BOTH proofs, re-proved together before every append and every
  dispatch. One appender, two things to prove, and a writer typed to a
  single lock could only ever check one of them (PR-03).
  (4) **The barrier lives in the engine and the artifacts live in the
  boundary.** ss6's cutoff is the one act that must observe a state nothing
  else can move, so it runs inside the single-writer loop: freeze
  admission at `_push` -- the one choke point every input passes, gated on
  `envelope is not None` so the engine's own doors stay open -- park every
  FW task at its poll boundary, drain, choose T, admit every tick <= T,
  advance through T, drain again, re-check ss8, and hand a SNAPSHOT to
  `boundary.commit_boundary`. The snapshot is taken without yielding, so
  nothing can move between the last precondition and the bytes the seal
  carries. The seal request is deliberately NOT on that queue: its decision
  is the `seal` record, and excluding it from the drain is therefore
  structural rather than a special case (PR-28e; draft 26 deadlocked on
  exactly that).
  (5) **`abort_boundary` is the caller's, and the fail-stop is not.** Every
  non-commit exit before the `seal` append runs the abort -- it clears the
  flag, reopens admission, restarts scheduler admission and unparks FW, and
  it touches no row, because the barrier held no job. From the append
  onward every failure is a `BoundaryFailStop`: an `fsync` error does not
  prove the line absent, and reopening C1 behind a possibly-durable seal
  line would append records after a seal, which recovery rightly refuses
  (PR-28b, PR-28d).
  (6) **The staged bytes are found where they now are.** ss7's reuse path
  needs `staged_manifest.json` and `candidate.json` after the rename has
  moved them out of `periods/.staging/`, which is exactly why the install
  keeps both beside `manifest.json`. `staged_bytes_for` consults the
  installed directory only when its candidate NAMES the request's digest,
  so a stale install is never read as this request's staging; a retry with
  a different digest quarantines the installed candidate two levels deep,
  under `<old stage digest>/<sha256 of its manifest.json>/`, which cannot
  collide when candidates alternate and is idempotent when the same bytes
  are quarantined twice (PR-30d, PR-30f).
  (7) **Two questions about a live run, one traversal.** The classifier asks
  only "is this job executing", and no ss10 rule tells the three ss3.5
  kinds apart -- so `executing_jobs` answers it from the WAL alone. That
  matters: readiness runs BEFORE the sealer has waited an unbound SPAWN
  out, and a classifier that needed `spawn.json` could not run there at
  all. `executions_at` answers the other question -- what exactly this
  execution IS -- and reads the spool for it. Both are built on one
  liveness rule (`live_spawns`), so the two can never disagree about which
  runs are live.
  (8) **Phase 2's classification is committed, and checked to be.**
  `validate_boundary` re-runs the classifier over the post-barrier state
  and compares its result with the candidate sidecar's `classification`
  field. A seal carrying phase 1's map is refused rather than trusted --
  the barrier's own admissions can create latent intent phase 1 never saw
  (PR-28a).
  (9) **Four changes outside the two modules.** `Oracle.__init__` takes
  `carried` rows and skips the genesis seed for every entity the seal
  brought, so carried revisions install VERBATIM and an operator's `expect`
  against a published revision stays holdable; `RuntimeState.install` is
  the verb that does it, under the owner, legal exactly once and before any
  input. `parse_envelope` gained `addressed=None` for the one command that
  addresses no row -- a parameter rather than a second parser, because ss0
  admits one mandate and not one per verb set, and "no row" therefore means
  `expect` is REFUSED rather than optional. `replay_inputs` seeds its
  frontiers from the opening record's own `first_index`, because I2 makes
  indices monotone across the ESTATE and a segment that opens at 5311
  replays from 5310. And `scheduler_frontier` replaces `last_journal_at`
  for the scheduler's re-anchor: the frontier is the opening watermark,
  the admitted ticks, the drops and the advances, and NOTHING else -- a
  `leader` record at 02:10 was silently consuming a 02:05 tick (PR-25a).
  (10) **Two frozen contracts move, each with its reason.** `deadman_s`
  leaves the host semantic projection, joining `last_contact` in
  `_UNPROJECTED_HOST`: it is observed liveness configuration, startup
  registers it with no journal record, and a projected value therefore
  moves a revision audit cannot derive (period-model ss3.3, PR-24b). And
  `runner-design.md` ss7's ladder now re-drives **a live wrapper under a
  terminal row regardless of the KILL effect's recorded state**, including
  when no KILL effect exists -- `_apply_kill` records `applied` before its
  TERM/grace/KILL ladder runs, so an engine that dies mid-ladder leaves
  exactly that state, and re-driving only PENDING kills read it and walked
  past it. One shipped test flipped with it, from "the process survives" to
  "the orphan is re-driven", and says so in its own docstring (PR-33).
  (11) **The read fence is the LINEAGE's proof, not both.** PR-03 wants a
  `status` after the anchor is replaced refused rather than answered, and
  the shipped rule for the RUN ROOT's proof is CM-14's -- dispatch stops on
  the way into admission's first append. Checking both at the control
  door broke that: the read a client composes its `expect` from was refused
  too, so the mutation that was supposed to stop the engine was never sent
  and a displaced leader ran on, answering nothing. So the door checks the
  anchor alone, the append checks both, and each obligation is held where
  its own spec puts it.
  (12) **The anchor's default path is a project decision, not the spec's.**
  ss1.1 says the anchor is outside every archivable root and names no path.
  `--estate-anchor` overrides it; omitted, it is `<run-root>.anchor`, a
  sibling of the root. Deterministic from the root, so an operator who
  restarts with the same `--run-root` reaches the same lineage; outside the
  directory they archive, so `tar`ing a root never carries the fence away
  with it.
  (13) **ss8's quiescence set at the cutoff.** The cutoff holds the
  engine-visible clauses -- the input queue, every admitted attempt
  decided, no unresolved KILL ladder, no indeterminate effect, every
  applied CMD SPAWN bound or terminal -- and then the SUPERVISOR clauses
  (a peer-review round moved these into U6b: the reachable control-seal
  path is the verb operators get first, and a CLI deferral cannot cover
  it). The CLI hands the engine its `SupervisorClient`, and
  `_supervisor_proof` runs after quiescence and before the horizon gate:
  the supervisor reachable, its LIST from the incarnation whose lease this
  engine holds (a restarted supervisor's empty history is not proof), and
  the LIST reconciled BOTH ways against the executions the seal will carry
  -- a carried bound run missing from it, an identity split on `run_id`,
  or a live run it holds that the seal does not carry each refuse. A dead
  supervisor that owns nothing a seal needs does not block (PR-27a). What
  ss8 still does NOT re-run at the cutoff is resume's whole reconciliation
  sweep; in its place every spool read the seal performs is held to the
  bound `run_id` at the read (`executions_at` refuses a stranger's
  `watch.jsonl` or `spawn.json` by name, DL-118), so the sweep's
  conclusion cannot be silently replaced between reconciliation and T.
  The "no open RuntimeState transaction" clause remains structural: the
  single writer runs the boundary, so no transaction can be open under
  it.
  (14) **A legacy root still resumes.** ss11's matrix says a `header`
  journal is refused by `run --resume` until it is adopted. That refusal
  belongs with `estate adopt`, which is U7's -- refusing first would strand
  every existing run root with no verb to un-strand it. A root with no
  sentinel therefore skips ss11 steps 1-4 and runs the ladder it always
  ran, and `runner-design.md` ss7 records that.
  Out of scope by construction, and unchanged from U6b's fence: the
  physical roll (`run --open-from`), adoption and the `adopting` head's
  WRITER, `audit`/`verify` and the attestation chain, `estate reclaim
  --force`, and the `dsl41 seal` CLI surface -- the live `seal` control
  verb IS built, and the same functions are what U7 wires its verbs to. The
  `adopting` head is READ here so a resume refuses it by name rather than
  meeting an unknown state and guessing.
  2679 -> 2789 collected, 107 new in `tests/test_boundary.py`, one in
  `tests/test_runner_control.py` and one in
  `tests/test_runner_leadership.py`, all passing;
  ruff, mypy and arch_check clean, and branch coverage of the eight gated
  modules unchanged against the tree this unit started from. An adversarial
  review pass found three blockers and fourteen lesser items, all folded
  in: ss9's gate joined attempts on `index` where `Journal.admit` writes
  `seq`, so it was DEAD in production while four synthetic-record tests
  stayed green -- the helper had written both keys; `dsl41 runs` read
  period 1's manifest beside period N's records and refused every estate
  that had crossed a boundary; and ss11's "torn or empty FIRST line"
  row was unimplemented, so a crash mid-write of the successor segment
  refused instead of re-opening. Each fix is mutation-checked: neutralise
  it and exactly its case reds. Phase 2 was also restructured -- the
  classifier now runs BEFORE the sidecar it commits into, so "phase 2's
  output is the committed classification" is true by construction where it
  had been an equality check against a map the same call had produced --
  and the candidate quarantine moved out of the plan and into the install,
  so the one destructive act in the reversible interval happens after
  phase 2 has passed.
  A second, external adversarial round found ten blockers, all folded in
  and each pinned by a test that reds if the fix is removed: the
  unused-root predicate covers the whole estate surface (a sentinelless
  root keeping a WAL, a seal, a committed period or a populated `runs/`
  refuses genesis; what the launcher pre-stages -- `catalogs/`,
  `periods/.staging/` -- is excluded by design); a sealed `bound`
  execution is restored into C2's outbox as an APPLIED binding, not just
  a carried row; recovery fsyncs the closing WAL before the head CAS
  (readable is not durable, and a power cut after the CAS must not lose
  the successor's naming seal); `Journal.seal` fsyncs unconditionally --
  a virtual-domain journal buffers ordinary appends and the CAS follows
  the seal line at once; the run root's directory entry for `seals/` is
  fsynced when the first boundary creates it; EVERY pre-PONR exception
  aborts the boundary, `OSError` included, and `durable_write` unlinks
  its temp on failure and clears a stale one so a retry is never wedged
  by its own O_EXCL; the seal's spool reads are held to the bound
  `run_id` (13); the supervisor clauses moved into the engine (13); the
  recovery comparison covers every duplicated record field -- `source`,
  `request_id`, `claimed_actor`, `force_seal` -- not just the selecting
  ones; and a subscription re-proves the anchor before EVERY response,
  backfill and live, not only at accept.
  A third round (the same reviewer over the fixed tree) found eight more,
  same treatment: the CLI proves the FULL ss1.1 predicate before it
  stages a bundle into a root or starts a supervisor against it (both are
  acts on an estate the process may turn out not to lead; a used root
  without `--resume` gets the same early refusal); a genesis that fails
  after the claim releases both raw-fd locks, so a same-process retry is
  never wedged; the ss8 mode table's "in place, tethered -- full drain"
  is enforced -- a tethered estate with a live bound command refuses the
  seal, since exit code 3 would cancel the run the seal just carried, and
  a DETACHED estate whose engine holds no supervisor client refuses too
  (unprovable, not vacuous); `resume_run` binds the client to the engine
  in core, not only in the CLI; the quiescence-and-proof pair loops until
  both hold at once, draining any completion that lands while the proof
  awaits the supervisor; `Journal.seal` publishes to subscribers only
  AFTER the fsync, so no subscriber is told about a boundary recovery may
  discard; the record schema requires `catalog_hash_version` to be an
  exact non-boolean integer; and the parents of a created run root and
  anchor directory are fsynced, so a power cut cannot retain one lineage
  half and lose the other's directory entry.
  A fourth round found five shadows of the third's fixes, each closed at
  the root: `Journal.create` fsyncs the opening `segment` record
  unconditionally (it NAMES the segment; the head actions rely on the
  file, and a virtual-domain journal buffers everything else), and the
  boundary's crash-retry reuse of an existing segment fsyncs it before
  the CAS; an input stamped AFTER the cutoff T that lands mid-seal
  refuses the boundary (it is C2's; re-choosing T would move the boundary
  under the request that composed it) where an input at or before T is
  drained and re-proved; the seal loop re-settles after every proof, so a
  task that dies while the proof awaits the supervisor surfaces before
  the snapshot; `start_run` no longer pre-creates the root with a plain
  mkdir (the durability helper would then prove nothing); and directory
  creation is `mkdir_durable` -- every created component fsynced plus the
  deepest pre-existing ancestor, unconditional on retry -- used by the
  run root, the anchor directory and `write_bundle` (which may be the
  first write into a fresh root, since the CLI stages before genesis).
  A fifth round found the last shadow: `_settle` deleted a failed live
  adapter task BEFORE re-raising, so the one observation of the failure
  could be consumed by the seal's reversible-abort path -- leaving C1
  running over an applied SPAWN whose task and completion were both gone.
  It now raises first and deletes only on clean completion (what the
  reaping list already did), so the corpse stays observable until the
  raise escapes the loop and the engine dies loudly. A small ledger of
  already-raised corpses keeps `shutdown` from raising the same failure a
  second time to the caller who just observed it -- teardown collects,
  it does not repeat.
- DL-134 the estate gets its verbs: the seal an operator runs, the proof
  that lets a root be archived, and the two ways a root joins a lineage
  (2026-08-20; period-model ss1.3, ss7, ss8, ss11 and ss13's PR-01c,
  PR-02a, PR-02d, PR-02e, PR-02f, PR-47a, PR-47b and PR-48's readiness,
  drain, idempotency and `adopting` rows, built as U7). DL-133 built the
  boundary and left five things out by construction: the `dsl41 seal` CLI,
  the physical roll, adoption, `audit`/`verify`, and `estate reclaim`.
  This unit builds all five and flips DL-133 item 14.
  (1) **The lock decides which seal mode you get, not a flag.** ss7 has
  two entry modes and one body. `dsl41 seal` tries to take `leader.lock`:
  refused means a live engine leads the root, so the CLI stages C2 and
  asks it over the socket; taken means nothing leads it, so this process
  becomes the offline leader for exactly one boundary. Probing
  `control.sock` would have answered a different question -- a socket file
  outlives the process that made it -- and a `--offline` flag would have
  let an operator assert something the estate can prove.
  (2) **The offline mode is `resume_run` plus `submit_seal`, and nothing
  else.** ss7 says offline "runs the same-root recovery barrier in full,
  then performs ss6 steps 1-8 as that offline leader", and that IS resume
  followed by the live seal path: the same `leader` record at epoch+1, the
  same replay, the same reconciliation, the same cutoff in the same
  single-writer loop. A second implementation of the boundary would have
  been a second place for every crash window this model spent twenty-six
  drafts closing. C1 is loaded from the ROOT's own bundle rather than from
  the command line, because the run root outlives the estate files it was
  launched from and an offline seal that needed them would be unusable
  exactly when it matters.
  (3) **`audit` re-derives; `verify` checks a checkpoint.** ss11 says
  "verified means re-derived, not self-consistent", so `rederive_seal`
  rebuilds the whole sidecar from the four inputs ss11 names -- the
  opening seal, the complete ordered WAL, the immutable spool, the C1 and
  C2 manifests -- plus the sentinel, read for the one derivation of
  `boundary_request.source`. It reuses the machinery resume already has
  (`open_from_seal`, `replay_inputs`, `executions_at`, the classifier,
  `close_runtime`), so audit and the engine cannot drift about what a
  period means. The staged half of the opening comes back off C2's
  COMMITTED MANIFEST, because the `seal` record carries only
  `next_period_id` and `next_baseline_id` -- which is exactly why ss11
  names that manifest as an audit input. The three `boundary_request`
  input scalars are checked record-against-sidecar and carried, as ss11
  exempts them; `source` is derived and compared, and an adoption's
  `request_id` is re-derived (PR-47b).
  (4) **Producing and consuming a checkpoint are two rules, and the code
  says so in two functions.** `audit_period` requires the predecessor
  attestation present and VERIFIED; `verify_attestation` accepts one
  ALONE. Draft 8 wrote one rule for both and made a second roll
  impossible: a rolled root holds the seal it opened from and none of that
  period's evidence, so it can verify and must not be asked to audit.
  `dsl41 audit` with no `--period` therefore names only the periods this
  root holds a WAL for, and audit of an imported seal refuses by naming
  `verify` and the registry.
  (5) **`chain_through_period` is derived, not asserted.** It is
  `predecessor + 1`, and it must equal `period_id` -- so a producer that
  skipped a link cannot write a checkpoint that claims to cover it. The
  consumer reads that one field and the induction is what it trusts.
  (6) **The attestation lives beside its seal**, at
  `seals/<period>.audit.json`. ss11 names the filename and no directory;
  this is the choice, and it is the one that makes a physical roll's import
  a pair of files under one name pattern rather than a second index.
  (7) **The physical roll is the in-place opener with two steps in front
  of it.** ss7's order -- new-root `leader.lock`, sentinel durable,
  `anchor.lock` and the claim, the import, the segment, the head -- is
  honoured by giving `open_next_period` a catalog it may resolve LAZILY:
  a callable is called after the claim and before the segment, which is
  where ss7 puts the import and the only place a roll can load C2, since
  until the import there is no bundle to load it from. The import is
  idempotent by content address, and it deliberately does NOT bring C1's
  WAL or bundle: re-deriving C1 in the new root would need C1's whole
  proof set, and importing that on every roll is retention policy rather
  than a boundary mechanism.
  (8) **Adoption stops at the point where the root becomes ordinary.**
  `adopt_legacy_root` runs ss11 steps 1-6 and hands back a period-1 root;
  step 7 is the COMMON seal body, run by an ordinary engine over it. A
  private seal path for adoption would have been a second implementation
  of the one thing this model exists to have exactly one of (PR-48). One
  reordering against ss11's numbering, stated where it happens: the SPLIT
  (step 6) precedes the TRANSLATION (step 5's second half), because the
  synthesized `segment` record IS the manifest's fields and the manifest
  has to exist before the record that copies it. Nothing observes the root
  in between -- the head is `adopting` and every other resume refuses by
  name -- so the reordering changes no state a reader can reach.
  (9) **`adopting` has one exception and it is explicit.** The head exists
  so adoption owns recovery of the root until period 1 seals, and
  `act_on_head` refuses it by name. The adopter's own barrier passes an
  `adopting=True` that also holds the outbox -- one flag, one meaning
  ("this resume IS ss11 step 5"), two consequences that are both step 5's:
  the head is ours to stand on, and the barrier is dispatch-free so a
  reconciled FAILURE's planned SPAWN is durable in the translated WAL and
  in the seal before anything acts on it.
  (10) **The legacy catalog is rebuilt under DL-66's `original_path`, and
  the header's v1 hash is carried opaque.** ss11 says "`catalog_hash`
  recomputed as v2 from the loaded catalog, `catalog_hash_v1` from the
  header", and it says nothing about verifying the latter -- deliberately:
  v1 hashes `meta`, so it moves with the installed package version, and an
  adoption is by definition an old root met by a new binary. Refusing on
  it would refuse every adoption there will ever be. The paths still
  matter, because v2 covers spans, so the rebuild parses under the
  original paths the legacy manifest recorded.
  (11) **The LEGACY period's launch options are operator-attested.**
  DL-66's `manifest/` recorded inputs and a catalog hash, not launch
  options, so `estate adopt` takes them as flags (`--timezone`,
  `--as-machine`, ...) and pins them in period 1's manifest. A wrong
  attestation is not silent: the resume gate compares the wiring against
  the pin and refuses, loudly, before anything is sealed.
  (12) **Two shipped bugs surfaced on the way and are fixed at the root.**
  A period opened from a seal was re-seeded from the CATALOG on every
  resume AFTER the one that opened it -- globals gone, holds gone,
  revisions moved -- because the carry was computed only on the opening
  pass. And the carried outbox was patched in AFTER the segment's records
  were read, so an `effect_result` in the new segment for an effect born
  in the old one hit `Outbox.resolve`'s "outcome for unknown effect" on
  the next resume. Both are one fix: ss7 phase 3 runs at EVERY resume of a
  period that opened from a seal, and `carried_outbox` seeds the replay
  rather than being applied to it. Neither was reachable before this unit
  because nothing resumed a rolled or re-opened root twice.
  (13) **The legacy launch options are the LEGACY MANIFEST's where it has
  them.** DL-66's `manifest/manifest.json` carries an `options` block with
  `timezone`, `as_machine`, `machine_policy` and `detached`, so adoption
  reads those and the flags supply only what it never held -- the deadman,
  the timezone table, the reconciliation windows. The adopter's WIRING is
  then built from the result, so the pin and the machine agree by
  construction instead of by attestation, and the operator is asked to
  remember only what nothing recorded. What is still attested is UNCHECKED
  at adoption and cannot be otherwise -- the barrier is wired from the
  profile it is attesting, so nothing can disagree with it. It is pinned in
  period 1's manifest and every LATER resume is held to it, and that is
  where a wrong one surfaces. Said plainly in the verb's help and in the
  runbook, because a guard that cannot fire is worse than none.
  (14) **Break-glass is recorded twice.** ss1.3 says a reclaim is recorded
  in the anchor and in the next `segment`'s `reclaimed` field. The anchor
  keeps an append-only ledger and the opener COPIES the entry for its own
  period rather than consuming it, so the fork stays visible in the fence
  that permitted it and in the log of the period that was let through.
  (15) **DL-133 item 14 is flipped.** A legacy `header` root now refuses
  `run --resume` and names `dsl41 estate adopt`. The question asked is
  narrow -- the first record is a `header` -- because a `journal.jsonl`
  that opens with a `segment` is a root somebody rewrote over its own
  sentinel, and telling that operator to adopt would send them where the
  problem is not. `runner-design.md` ss7 records the flip.
  Deliberately deferred, each with its reason. The seal's crash matrix
  over the roll and over adoption's seven steps (PR-45's roll rows, PR-48's
  power-loss half) is not built: the seams are in place (`crash_point` on
  both operations) and the matrix is a unit of its own, the size of
  DL-133's. `--trust-unaudited-seal` (PR-47) is not built -- it is
  resume's switch, not an estate verb's. The `dsl41 audit`/`journal`/`runs`
  estate-WIDE walk across roots through the registry (PR-02f's cross-root
  half) is not built; the registry carries what it needs and the readers
  still take one root at a time. And `_resume_untraced_starts`'s
  no-adapter branch is now UNREACHABLE from `resume_run` -- the only route
  to it was a legacy root, which (14) closed -- so its test was retargeted
  to the two refusals that replaced it and the branch is left in place,
  named here, for the architecture review to decide about rather than
  deleted quietly on the way past. `cli.py` grew by roughly 1100 lines and
  is the largest advisory finding `arch_check` reports; the verbs' bodies
  are the natural thing to move out and that is the review's call, not
  this unit's.
  A self-review sweep over the finished tree found twelve more, each
  fixed at the root and pinned by a test where a test can see it. The
  worst was an `estate adopt` re-run over a root whose period 1 was
  ALREADY SEALED: steps 5-6 ran before the recovery check, so a re-run
  under a different flag rewrote the committed manifest and exited 0,
  leaving a period that could never be re-derived and a verb that said it
  had succeeded. The recovery check moved above the split, and the split
  now REFUSES a manifest it disagrees with instead of overwriting it. A
  physical roll interrupted after its claim refused its own head, because
  the roll accepted only `closed` -- ss1.3 makes the claim idempotent on
  `claim_id`, so a `claimed` head naming this root now resumes through the
  claim FILE, which carries the seal the head no longer does, and a
  COMPLETED roll refuses by naming `--resume`. `dsl41 audit` held the
  lineage lock for the whole verb, so auditing period N-1 while period N
  ran was impossible -- a live engine holds that lock for its process
  lifetime -- and the lock now wraps the one write ss1.3 gives it; a
  caller that cannot take it keeps the checkpoint, which is what `verify`
  and `run --open-from` actually read, and is told the registry row is
  outstanding. `_drive_boundary` read the LOOP first, so an engine failure
  during teardown after a committed boundary reported exit 2 -- "it did
  not commit" -- the one lie about the estate it could tell; the future
  decides now and the loop's other exceptions are diagnostics. The
  `result`+`effect` FOLD had no test at all, on the one verb that writes a
  durable period-1 WAL, because `legacy_twin` downgrades the header and
  leaves the body native; the fixture now unfolds it and the fold is
  checked against the retained original record by record. `audit`'s
  producer-side chain check was a tautology once `verify` had run and is
  gone, `verify`'s own two rules got the tests they lacked, and PR-08b's
  attestation golden vector -- cited but not built -- is built. An earlier
  sweep found five, each fixed the same way. ss11's matrix row
  "adoption's `seal` record present, head still `adopting`" was
  UNIMPLEMENTED: the re-run refused its own head and the estate was
  stranded with a committed boundary nobody could close, so `estate adopt`
  now performs that CAS -- fsyncing the WAL first, on the rule every head
  transition here follows -- and reports a finished adoption rather than
  sealing a period that is already closed. The roll left an open `Journal`
  on the new root's WAL, and closing it the ordinary way would have
  released the proofs its own successor was about to append under, so
  `Journal.close` split into `detach` (the descriptor) plus the release
  (the term) and the roll takes the first. The import copied the seal and
  the attestation without reading the COPY back, which is the one thing
  the new root will resume from, audit against and hand to a second roll.
  A `chmod` after `durable_write`'s rename was a window on a WAL that
  carries globals. And the drain check compared attempts against the
  POST-REPLAY decision index, which recovers exactly the missing decisions
  it was supposed to detect -- dead in production while its test stayed
  green, so it now reads the durable decisions off the records.
  2789 -> 2839 collected, 49 new in `tests/test_estate.py` and one in
  `tests/test_boundary.py`, all passing; ruff, mypy and arch_check clean.
  The first EXTERNAL adversarial round found eleven blockers, all folded
  in with pins: audit compares the `seal` record against the sidecar it
  attests and the manifest against the segment BEFORE re-derivation (a
  rewritten record or rewritten segment pins over an untouched partner
  refuses by name); the watch fold takes the seal's own `watch_seq`
  prefix, so a watch that carried across the boundary is audited on C1's
  evidence and not the successor's later polls (the digest comparison is
  what makes the claimed prefix safe to take); the attestation model is
  strict -- no coercions, one byte form (`from_bytes` requires the file's
  bytes to BE the canonical serialization), links and timestamps
  grammar-checked, and the null predecessor is period 1's base case and
  nothing else's; audit is idempotent AND still finishes the `attested`
  CAS a crashed run left behind; adoption proves the anchor is empty or
  its own BEFORE the fence, and an adopted sentinel with an empty anchor
  refuses a retry pointed at the wrong `--estate-anchor` (a second closed
  authority over one root is the fork ss1.3 exists to prevent); the fence
  requires an existing archived name to be the SAME inode as the legacy
  journal and fsyncs unconditionally; the drain check asks a live
  supervisor socket for LIST (a wrapper the spool never recorded) and
  refuses an unanswering socket; the fold refuses a spawn.json naming
  another (job, run_number); recovery of a committed adoption validates
  the sidecar, its record mirrors, its `adopt` source, its estate and the
  successor manifest before the head CAS; and `reclaim` recomputes the
  claim id from the body and requires agreement with the head, the
  estate, the target root and the registry's closing seal before it moves
  anything.
  The second external round found seven shadows, closed at the root: the
  watch fold's cut is `at <= T` with T from the period's OWN replay --
  never a prefix read off the artifact being verified, which a coherent
  re-forge could move -- and lines past the cut are not validated at all,
  so a successor's evidence (an unsupported version a future binary
  wrote included) cannot make a closed period unauditable; the fence
  durably binds the ORIGINAL anchor (`legacy/anchor.json`), which is what
  tells the fence-to-authority crash window (retry proceeds) from a retry
  pointed at the wrong anchor (the fork; refused naming the bound one) --
  and `legacy_sources` reads DL-66's `manifest/` directly so the window
  itself is resumable; the drain LIST must be a well-formed successful
  answer, never an error envelope read as empty; the `attested` CAS is
  bound to the estate, the normalized root and the seal digest, so a
  stranger's unlocked anchor cannot be marked; recovery of a committed
  adoption runs the FULL seal-to-opening validation (`open_from_seal`)
  before the head CAS, not a presence check; and `reclaim` requires exact
  registry agreement, null included.
  The third external round found five, closed at the root: the watch
  fold's cut is POSITIONAL (ss3.5: an instant is not a unique log
  position -- a successor unparked at the boundary can poll at exactly
  T), and the count's authority is the successor segment's
  `opens_from_seal`, an artifact independent of the sidecar under audit
  -- a coherent re-forge of sidecar and record together refuses against
  the successor's pin before any evidence is folded, and a period with no
  successor segment owns its whole log; lines past the prefix are never
  read. The drain LIST requires the frozen protocol version and every
  typed field per row -- a row missing `wrapper_alive` would read as
  false and fence over an unspooled live wrapper. The `attested` CAS and
  `reclaim` both require the registry row committed AND durable
  (`seal_digest` exact, null included; `segment_durable` true). And the
  fence's anchor binding moved INTO the sentinel (`adopted_anchor`,
  written by the same atomic rename) -- a side file was swappable under
  an untouched sentinel, and a forged binding could point a fence-crash
  retry at a second empty anchor.
  A fourth round found the last one: attestation publication is
  CREATE-ONLY (`durable_create`: the temp is linked into place, never
  renamed over an existing name) -- two racing audits would otherwise
  publish two digests for one period, and a roll could import one while
  the closing root keeps the other, a forked chain. The loser verifies
  the winner and finishes the `attested` CAS over it -- after first
  making the winning file and its directory entry DURABLE
  (`make_durable`): the winner links before it fsyncs, and a durable
  `attested` row over a merely-visible checkpoint survives a power cut
  the checkpoint does not. The idempotent early return fsyncs the
  observed checkpoint on the same rule.
- DL-135 what may never be deleted, and the subscriber that stopped
  losing a period (2026-08-20; period-model ss11a, ss12 and ss13's PR-36b,
  PR-36c and PR-49's backfill half, built as U8). DL-134 left the estate
  with verbs and no way to stop it growing, and with one reader that had
  quietly become wrong when the WAL became many files.
  (1) **A floor is computed, not asserted.** ss12 itemizes what may never
  be pruned -- everything reachable from the lineage head -- and
  `retention.py` reads one root and returns that list with a citation on
  every row. The verb then iterates the PRUNABLE set alone, and `_remove`
  proves three things before it unlinks: the verdict is prunable, the
  path is under this run root, and the path holds no retained artifact
  BENEATH it. The third is the one a plausible implementation skips:
  removing a directory removes what is inside it, so a guard that
  compared paths for equality would let `runs/` go while every floored
  tombstone under it was named in the same plan (PR-36c).
  (2) **Three verdicts, because ss12 leaves a middle.** `floored` is the
  model's refusal. `prunable` is a deletion the spec licenses BY NAME:
  PR-36b's tombstone whose period is attested and whose run is terminal,
  and ss12's quarantined candidate that no recovery references. Everything
  between them -- a closed period's WAL, a sidecar the head has moved
  past, an older manifest, an unreferenced bundle, a superseded
  checkpoint -- is `held`: the floor has lifted and PR-Q3/E20 is open, so
  nothing here deletes it. Two verdicts would have forced a choice
  between refusing what PR-36c says may go and deleting what PR-Q3 says
  stays, and both would have been wrong.
  (3) **The one asymmetry is the spec's, and is stated where it happens.**
  ss12 floors "the WAL and spool of any unattested period (E20 gates the
  rest)", which reads as holding an attested period's spool too; PR-36b
  then says of a run directory and its index entry, in as many words,
  "after the period is attested and the run terminal, it may go". The
  itemized obligation wins for the spool. The WAL -- the input E20 is
  actually about -- is held, and carries the `# PENDING: E20` marker so
  that answering the question is a deliberate change here rather than a
  drift.
  (4) **Terminality is read off the SEALS, not off the spool.** A run
  named in seal N's `executions` was live at that boundary, so it ended in
  period N+1; a run no seal names ended inside the period that spawned it.
  Either way ONE period is the last that can reference the run, and its
  attestation is the whole gate -- producing a checkpoint requires the
  predecessor's, so everything below is covered by induction. A rule that
  asked `status.json` instead would have believed a file the estate never
  admitted, and a rule that stopped at the birth period would release a
  carried run's spool while the period whose audit needs it was still
  open. A root that holds no seal for the ending period answers the same
  way as one whose ending period is open, and the refusal says so: a
  rolled root holds no successor seal by design, and "I cannot prove it
  ended" is the same answer as "it has not" for a floor.
  (5) **A directory with no provenance is floored, and so is an index
  entry that will not parse.** ss11a's store is an idempotency store:
  "no index entry" means "first application", so deleting one AUTHORIZES a
  spawn of a job that already ran. Nothing here guesses which period would
  have to be attested to release a run the retained WAL cannot show, and
  an unreadable entry still NAMES a `run_id`.
  (6) **`periods/.staging/` is floored and `periods/.quarantine/` is
  not.** The install refuses when the staged bytes are gone, and its own
  comment names a retention sweep as the thing that could have removed
  them. Being quarantined is what says no recovery reads a candidate, so
  ss12 releases it in the same sentence that floors the installed one.
  (6a) **What pruning a tombstone costs is stated where an operator will
  meet it.** `dsl41 runs` takes a row's start and end from `spawn.json`
  and `status.json`, and `read_spool` already reads a pruned directory as
  ABSENT rather than refusing -- "one corrupt directory should cost one
  row's timings, not the whole report". So the row survives and its
  timings do not, and the runbook says so beside the verb.
  (7) **Policy is flags and nothing else.** `--tombstones`,
  `--quarantine`, `--keep-runs`, `--older-than-days`. A run with no class
  named deletes nothing and says why: a default set would be a retention
  policy, and ss12 makes that the operator's. `--dry-run` with no class
  is a survey and lists every licensed deletion, sizes included -- an
  operator deciding whether to prune is asking how much it is worth. The
  thresholds filter whole RUNS, so a directory is never removed while its
  index entry stays -- ss11a spends its whole protocol keeping that pair
  one-to-one. `--keep-runs` is per JOB, because `run_number` is per job:
  one list ranked by run number compares numbers from different series,
  and `--keep-runs 3` would then delete a quiet job's whole history while
  keeping three of a busy one's. `--older-than-days` reads whichever of a
  run's artifacts survive, so an index entry orphaned by a hand-deleted
  directory is protected by the same threshold that protected the pair.
  (7a) **A deletion the FILESYSTEM refuses is reported, not raised.**
  Deletion is irreversible and partial, so the sweep records the refusal,
  keeps the REST of that run, continues to the next one, and exits 2 with
  a list. Stopping at the first error would leave an operator with a
  traceback, some artifacts already gone and no way to tell which. A
  deletion the PLAN refuses still raises: that is a floor being reached,
  which is a defect rather than a fact about the disk. And a spool that is
  a SYMLINK is floored rather than met at deletion time -- the plan reads
  a name and the removal follows a link, so a spool that is a link is one
  where those two are not the same object.
  (8) **The verb reads the anchor and never locks it.** A live engine
  holds the lineage lock for its process lifetime, so a prune that needed
  it could only run against a stopped estate. Reading is sound because
  attestation is monotone and because everything a running period creates
  belongs to a period that is not attested -- floored for that reason
  alone, without a race to lose.
  (9) **The subscriber's backfill spans segments, and is BOUNDED.** `since`
  has always been an estate-wide index (I2), and the backfill read the
  ACTIVE segment's file -- so from the moment DL-133 made the WAL many
  files, a client resuming with a cursor taken before a boundary was
  answered with the new period's records and no sign that anything came
  before them: silently, the exact gap it was resuming to avoid.
  `read_backfill` walks the retained segments NEWEST FIRST and stops at
  the one holding a record at or below the cursor. Everything older sits
  before the caller's positional cut, so reading it would be work whose
  whole result is discarded -- and the read runs on the single-writer
  loop, where an estate that has crossed a year of boundaries would have
  paid seconds for it. A cursor inside the live period costs one segment,
  and only `since` at the very beginning costs the estate. Nothing else
  moved -- the cut is still positional and the seam still keys on the
  presence of `seq`, so DL-89's exactly-once and at-least-once guarantees
  are word for word the ones that held before.
  (10) **A cursor below what the root retains gets ss11's gap marker, and
  the ANSWER is the gap.** `{"gap": true, "earliest_retained": <index>}`,
  sent before the backfill. The number is the oldest retained segment's
  `first_index` -- the first index it may allocate -- and not the lowest
  `seq` in it: a period that admitted no input still covers its whole
  range. `read_backfill` returns the gap rather than a number for the
  caller to compare, because stopping early IS the proof that there is
  none, and a caller handed a number would re-derive a comparison the
  reader had already made. Index 1 is the first there is, so a cursor at
  or below 0 is never a gap. The reachable case is a physical roll, which
  imports the seal it opens from and none of the closing period's WAL by
  design. The marker is a RESPONSE, so PR-03's fence runs in front of it
  like every other one.
  (10a) **The widened read can refuse, and it refuses ON THE STREAM.** It
  opens files this subscription's own period did not write, so it can meet
  a foreign name under `wal/` or a closed segment whose tail is missing.
  The ack has gone by then, so an exception would hang the client up with
  no answer -- the one thing control-protocol ss2 forbids. And a closed
  segment must END in a `seal`: `read_journal` tolerates a torn FINAL
  line, which is right for the file an appender is still appending to and
  wrong for a closed one, where a tolerated tail is a hole in the MIDDLE
  of the concatenation and the subscriber would be handed a stream that
  skips records without being told.
  (11) **`root_of_wal` is the layout read backwards, and it is named.**
  `period.estate_wal` says which file a root's appender opens; going the
  other way -- from a segment to the root that holds its siblings -- had
  no function, so a caller would have spelled the directory shape inline.
  DL-133 put the layout behind one function on the way down for exactly
  that reason, and this is the same rule on the way up.
  2846 -> 2884 passing, 32 new in `tests/test_retention.py` and six in
  `tests/test_boundary.py`; ruff, mypy and arch_check clean. An external
  adversarial round over the finished tree found three blockers and
  fifteen lesser items, all folded in and each pinned where a test can see
  it. The blockers: `--keep-runs` ranked every run of every job on ONE
  list by run number and broke ties in set iteration order, so a quiet job
  was starved by a busy one and a `--dry-run` could disagree with the run
  that followed it; the widened backfill read the WHOLE estate
  synchronously on the engine's single-writer loop and re-parsed the
  oldest segment a second time to answer the gap question; and `prune` had
  no error containment, so one `OSError` aborted the loop after partial
  deletion with no report. The fifteen: `_subscribe` was unwrapped, so a
  foreign file under `wal/` hung the client up with no answer at all; a
  torn CLOSED segment truncated a stream silently; a symlinked spool
  reached `rmtree` instead of being floored; the age threshold skipped an
  orphaned index entry; an unreadable `stat` read as "ancient" instead of
  failing closed; a `now=` parameter was declared and ignored; the default
  log filename was spelled a second time beside
  `runner_adapters.job_log_paths` (both now call `default_log_paths`); a
  dry run reported zero bytes; a negative cursor produced a spurious gap
  marker; the gap boundary was pinned only from above; the shared-bundle
  case the bundle test's docstring described was one no fixture could
  distinguish; a file named like a spool was refused with the wrong
  reason; the `PR-Q\d` index row claimed a code marker PR-Q5 does not
  have; the exit-4 runbook entry named a reuse collision where the
  refusal is a stale baseline; and ss15's citation-index row did not
  mention the new namespace. Deliberately NOT changed: `estate prune`
  asks for no confirmation, because naming a class is the confirmation --
  a verb that deleted by default and then asked would be the retention
  policy ss12 says is the operator's. Every floor
  and every gate is mutation-checked: neutralise it and exactly its case
  reds -- the verdict gate, the containment gate, the run-root gate, the
  carry lookup, the attestation requirement, the provenance refusal, the
  E20 hold, the segment-spanning read and the gap marker, one at a time.
  Deliberately not built: a retention SCHEDULER or any default policy
  (ss12 makes it a business decision, and the runbook's ss2a is where it
  is stated); pruning anything in the `held` bucket, which waits on
  PR-Q3/E20; and the estate-WIDE prune across roots through the registry,
  which is the same cross-root walk DL-134 deferred for
  `audit`/`journal`/`runs` and is one unit for all four rather than a
  fourth private walk.
  The first external adversarial round found six blockers, all folded
  in with pins: the planner refuses a WAL holding a SECOND SPAWN for one
  (job, run_number) (I2 -- a silently-kept first binding would floor the
  wrong effect); run-directory spellings are canonical (`b.01` never
  shadows `b.1`) and index bodies are joined to the WAL effect's own
  run_id (a body rewritten to a terminal run refuses the plan, ss11a's
  pair both ways); deletion-authorizing seals are BOUND -- sentinel
  estate, period filename, the WAL's own `seal` record, and the
  successor's `opens_from_seal` (an imported seal on a rolled root binds
  through the successor link alone) -- so a valid pair swapped in from
  another estate refuses; the backfill verifies the chain it streams
  (filename vs opening, contiguous retained numbers, each segment opening
  from the seal that closes the one before it, one estate, continuous
  index frontier); a backfill error after the ack re-proves the lineage
  before it is sent (PR-03 per response); and a failed parent fsync
  PROPAGATES, so an undurable removal is reported failed and wedges its
  run rather than reported done.
  The second round found four shadows, closed: an index BODY's own
  run_id must equal its filename (a rewritten body keeping the tuple
  refuses the plan); the backfill binds every opening record to the
  SENTINEL's estate -- the early stop can read one segment, where no
  adjacency check runs; every traversed closing `seal` record passes the
  full ss2.2 schema before the continuity comparison (an off-type
  `closes_at_index` refuses instead of skipping the check); and a
  sidecar whose LOCAL WAL lacks exactly one naming `seal` record refuses
  the plan -- an unbound sidecar authorizes no deletion.
  The third round found five, closed: the plan is bound to the resolved
  root's (st_dev, st_ino) and every removal re-proves it, so a run-root
  symlink retargeted after planning refuses instead of deleting inside
  the replacement estate; a SPAWN whose run_id is null floors its
  tombstone ("provenance incomplete") instead of joining as unbound; an
  index entry without its artifact_format_version is unreadable evidence
  and floored; the local seal record passes the FULL ss2.2 schema before
  the mirror comparison; and a `header` WAL under a periodized root's
  sentinel refuses the backfill -- a legacy stream does not belong to a
  sentineled estate.
  The fourth round found the last two, closed: SPAWN identity evidence
  is exact -- a boolean run_number (which aliases run 1 through an
  isinstance check), an empty job, an out-of-grammar run_id each refuse
  the plan; and removal is DESCRIPTOR-relative -- the root is opened
  once, fstat-proved against the plan's inode, and every step to the
  artifact walks openat with O_NOFOLLOW from that fd, so neither a
  symlink retarget nor a rename-swap of any component after the proof
  can redirect a deletion into another estate (a symlink AT the artifact
  path is unlinked as a link, never followed).
  The fifth round found the last two, closed: `relative_to` is lexical,
  so a crafted `..` component is refused before the descriptor walk; and
  every prunable artifact's own (st_dev, st_ino) is pinned at PLAN time
  and re-proved through the held descriptor at removal -- another
  directory renamed onto a prunable name after planning is recorded as
  `ArtifactChanged` (a disk fact, the sweep continues and the run
  wedges), never deleted under the plan's verdict.
  The sixth round found two more TOCTOU corners, closed: removal
  ISOLATES the named entry first (an atomic same-directory rename to a
  scratch name), verifies the isolated entry's pinned identity, and
  restores-and-refuses on mismatch -- no rename can swap the target
  between the proof and the deletion; and the recursive walk checks
  every entry against the plan's RETAINED identity set, so a floored or
  held artifact moved inside a prunable tree after planning refuses
  mid-walk (the tree's licensed content may already be partially gone
  under the scratch name; the retained artifact itself is untouched and
  the failure is reported).
  The seventh round closed the last two rename corners: the scratch
  name is uuid-fresh per removal and checked free first (a deterministic
  name could be pre-seeded with a retained artifact for the isolation
  rename to destroy; Python exposes no portable RENAME_NOREPLACE, and
  guessing a fresh uuid inside the check-to-rename window is not a
  practical attack); and the mismatch ROLLBACK renames back only when
  the original name is still free -- an entry that appeared meanwhile
  survives, the isolated replacement stays under its scratch name, and
  the refusal reports that location.
  The eighth round closed the final pair: a MISMATCHED isolation is
  never renamed back at all -- without a no-replace primitive any
  restore can clobber a newcomer, so the isolated entry stays under its
  scratch name and the refusal reports the location; and the retained
  identity set is RECURSIVE -- every inode at and beneath a floored or
  held directory is pinned, so one file moved out of a retained bundle
  into a prunable tree refuses mid-walk instead of being swept.
  The ninth round closed the last hole: retained-tree traversal fails
  CLOSED -- an unreadable subdirectory or entry refuses the whole plan
  (silently skipping it would leave its children unpinned for a later
  move-and-sweep); a file deleted mid-walk is exempt, since a gone inode
  cannot be moved anywhere.
  The tenth round removed the last exemption: an entry the walk listed
  and the lstat cannot find refuses the plan too -- a vanish between
  listing and lstat cannot be told apart from a rename into a prunable
  tree, and only refusing covers both. (A retained artifact path absent
  at the TOP level stays exempt: it was never listed as existing and has
  no inode to move.)
  The eleventh round removed the top-level exemption as well: every
  artifact on the plan's list was observed by the scan moments earlier,
  so an absence at pin time is concurrent mutation and refuses -- there
  is no "legitimately absent" retained artifact.
  The twelfth round closed the regress at its root: identity is
  captured when the estate is FIRST observed -- plan_retention takes one
  fail-closed snapshot walk of the whole root before any scan read,
  retained identities are pinned from that snapshot (so an original
  moved into a prunable tree during the scan KEEPS its pinned identity
  and the removal walk refuses it), and a closing bracket re-proves that
  every retained path still holds the observed inode (a byte-identical
  substitution during the scan refuses: "the estate is being mutated
  under the planner"). Retained paths outside the root -- the anchor,
  claims -- and paths that appeared mid-scan pin through the live
  fail-closed traversal, which cannot have been the target of a
  pre-scan swap.
  The thirteenth round moved capture into the enumeration itself: the
  snapshot walks opened descriptors, each file identity is the d_ino the
  `scandir` listing reports (no later pathname lookup to race), and a
  descent re-proves each subdirectory -- opened under the parent's fd
  with O_NOFOLLOW, its fstat must equal the listed identity or the plan
  refuses ("identity changed between listing and descent", which also
  catches a mount placed mid-tree). The snapshot now covers the ANCHOR
  tree too, so outside-root retained artifacts are bracketed exactly
  like in-root ones.
  The fourteenth round completed the symmetry: PRUNABLE identity comes
  from the observation snapshot too, and only while the disk still
  agrees with it -- a foreign directory swapped in during the scan is
  stamped None, and a None identity is an `ArtifactChanged` at removal
  (recorded, the sweep continues), never a deletion of something the
  plan did not observe. Every identity in the plan now traces to the one
  pre-scan enumeration.
  The fifteenth round added the inverse licence: the removal walk
  deletes ONLY inodes the pre-scan enumeration observed somewhere in the
  estate -- an unobserved artifact moved into a prunable tree after
  planning refuses mid-walk and survives. With the retained set, the
  observed set, the per-artifact stamps and the bracket, every deletion
  now traces to the one observation: nothing is removed that was not
  seen, and nothing seen as protected is removed.
  The sixteenth round made the licence PER ARTIFACT: each prunable
  artifact carries the snapshot identities observed at and beneath ITS
  path, and the walk deletes only those -- an observed inode from a
  class the operator did not select, moved inside a selected tree after
  planning, refuses instead of riding along. The estate-wide allow-set
  is gone.
- DL-136 the boundary era becomes operable, and one reader stops losing a
  period (2026-08-20; period-model ss7, ss8, ss9, ss11, ss11a, ss12 and
  ss13's PR-50, built as U9). DL-133, DL-134 and DL-135 built the boundary,
  its verbs and its floors, each pinned by unit tests over two-job
  fixtures. Nothing had ever driven them over an ESTATE, and nothing told
  an operator how to run one.
  (1) **The training estate gets the boundary, as exercises and as
  scenarios.** `examples/nightbank/RUNBOOK.md` grows exercises 15-21 --
  seal the night live and offline, open period 2 in place and see what
  crossed, the morning-after `audit`/`verify`/attested row, adopt a night
  from before the period model, roll to a fresh run root, reclaim a
  crashed roll, prune. `tests/test_nightbank_boundary.py` drives the same
  flows over the small profile's 81 jobs and asserts only what an operator
  sees: an exit code, a line of output, a file, an answer over the control
  socket. Nothing there re-tests a unit; what is new is the estate.
  (2) **One flagship runs real processes, and the rest do not.** Three
  claims cannot be made from inside one interpreter -- the engine exits
  code 3 when its period is sealed, a refused seal leaves it serving, and
  period 2 answers `dsl41 query` with period 1's globals, holds and
  statuses. Those are one test with real subprocesses. The other five run
  in process against the same estate and cost about a second each.
  (3) **The night under test is the night the operator starts.** The
  launcher's setup is now `prepare_night`, and both `nightbank up` and the
  fixtures call it. A second copy of it in a test file would drift from
  the one an operator runs, and a sandbox whose test night is not the real
  night is worth nothing.
  (4) **The retry horizon is where the scenarios and the fixtures part,
  and the rule is the spec's.** ss9's gate counts EXTERNALLY requested
  attempts, which `boundary.externally_requested_attempts` defines as
  `expect is not None`. `dsl41 sendevent` carries one and `Engine.inject`
  does not, so the flagship meets the gate -- it asserts the refusal, then
  commits with `--force-seal` and checks `forced_gate` on the seal -- while
  the in-process scenarios seal unforced and honestly. No test invents a
  short horizon: `retry_horizon_us` is a `RuntimeProfile` field with no CLI
  flag, so a root carrying a small one is a root no operator could make.
  (5) **A defect in a landed unit, found by the scenarios and fixed at the
  root: `dsl41 runs` lost every period it had ever printed.**
  `read_run_root` opened the ACTIVE WAL segment, which was right for as
  long as a run root held one journal and became silently wrong the moment
  DL-133 made the WAL many files. After the first seal the table came back
  EMPTY -- exit 0, no rows, no warning -- on exactly the estates the
  boundary exists for. It is the same defect DL-135 closed for the
  subscriber's backfill, in the other reader, and PR-50 ("run history spans
  a boundary keeping `start_period`") is the obligation it broke.
  `read_run_root` now walks `period.estate_segments` and folds each period
  on its OWN inputs -- its manifest, its bundle, its replay -- and
  concatenates, which is the same fold `read_run_roots` already performs
  per root. A period is a baseline, so DL-113 decision 4's segmentation
  break lands across periods exactly as it already did across roots.
  (5a) **The replay is SEEDED, because a period does not start from
  nothing.** Reading the segments was half of it. `replay_trace` built its
  `Oracle` empty, so a later period derived revisions and run numbers the
  log never recorded and `replay_inputs` refused -- "replay diverged at
  index N" -- on the first admitted input that touched a carried entity.
  That was already true of the ACTIVE segment before this unit, so `dsl41
  runs` refused any real estate the moment its second period touched a job
  from its first; nothing had ever run it there. The fix is the one audit
  already uses: seed from the rows the period opened with, and
  `attest._carried_from_opening` became `attest.carried_from_opening` so
  there is ONE derivation of that fact rather than a second copy in the
  reporting tool. Run NUMBERS then continue too, which needed the fold's
  own half: `_windows_from_entries` counted a job's runs from 1 within a
  segment, so a box that ran in two periods would have been numbered 1
  twice, and a leaf run carried across a boundary could not find its trace
  window by position -- losing `started_by` and decision 2's
  KILLJOB/`term_run_time` close fallback. Windows are now keyed by the run
  NUMBER, from the number the period opened with.
  (5b) **What let it ship was an assertion that could not fail.** PR-50's
  own test asserted `isinstance(rows, list)` over a fixture whose
  `FakeAdapter` writes no `dispatch` record and therefore produces no rows
  at all -- a test that could never have seen the rows disappear. The fix
  is pinned in `tests/test_run_history.py`
  (`test_pr50_run_history_spans_a_boundary`), over real subprocesses, where
  a leaf row exists to lose; mutation-checked (fold the newest segment only
  and exactly that case reds). The boundary-tier test keeps the half it CAN
  hold -- the active period's manifest is the one selected -- and says in
  its own docstring why the other half is not there. The replacement pin
  runs ONE job and ONE box in BOTH periods -- the leaf takes its number
  from the `dispatch` record and could never duplicate, so the box is
  where the carry is proved -- and each half is mutation-checked
  separately: unseed the replay and it refuses, number the windows
  positionally and the box rows collide.
  (5c) **What the fix does not claim.** A run that SPANS a boundary keeps
  its row in the period that dispatched it, with its spool timings, and its
  STATUS stays RUNNING: the terminal input is in the NEXT segment and the
  fold reads one segment at a time. The end time is there and the verdict
  is not. And the cost is one replay per retained period, with `--job` and
  `--since` filtering after the fold, so a long-lived estate pays for its
  whole history to answer about one run. Both are stated in the function's
  docstring where a reader meets them.
  (6) **Deliberately not fixed here: `dsl41 journal`.** ss11 says replay
  across periods "walks segments and switches catalogs at each `segment`
  record", and the verb still replays one segment under one catalog. Unlike
  run history -- whose fold is per period already, because a period is a
  baseline -- a journal replay is one oracle over a record sequence, so
  switching catalogs mid-stream is a change to the replay contract and a
  unit of its own. Named here rather than fixed quietly on the way past.
  (7) **The RUNBOOK is held to the CLI.** A renamed verb is a refactor
  nobody thinks of as a documentation change, so
  `test_every_dsl41_verb_the_runbook_types_exists` requires every
  `dsl41 <verb>` the runbook types to be a command this build has -- the
  same contract `test_runbook_job_names_exist_in_an_estate` already holds
  the job names to.
  (8) **One stale count corrected.** `examples/nightbank/README.md` said
  "twelve operator exercises" over a runbook that had fourteen before this
  unit added seven.
  (9) **Deliberately not built: period-model ss14's B1, B2 and the rest of
  C.** ss14 names four worked scenarios over this estate. What landed here
  is A's carry-and-audit half and C's quiet roll, crashed claim and
  adoption rows. B1 (a boundary committing mid-night, detached, over a live
  command, a KILL ladder in flight, a crossing FW watch, a QUE_WAIT pair
  and two timers due at exactly T) and B2 (the same estate refusing, one
  change at a time) each need a detached night with live closure under
  them, which is a unit the size of DL-133's and not an exercise. C's
  remaining rows -- a fork attempt from a second root, an old binary
  pointed at an adopted root, the anchor deleted under the incumbent, a
  crash in period 1 before any seal, a lost `seal` response on both sides
  of the record -- are pinned unit-side in `tests/test_boundary.py` and
  `tests/test_estate.py` already; what ss14 asks for is the same rows over
  THIS estate, and that is the same unit.
  An adversarial self-review over the finished tree found three blockers
  and five lesser items, all folded in. The blockers: the `dsl41 runs`
  replay was unseeded (5a) -- found by reasoning about the code, then
  reproduced by widening the pin; RUNBOOK exercise 21 typed `--keep-runs
  20` and then claimed a deletion, but `--keep-runs` is per JOB and no
  nightbank job has twenty runs, so the command it printed would have
  deleted nothing; and `read_run_root`'s docstring said a spanning run
  reads as open only "with the spool pruned", where the status is RUNNING
  either way (5c). The lesser five: the runbook-verb contract read only
  the FIRST token, so `estate adopt` passed on the strength of `estate`
  alone; the PR-50 pin's docstring described a `job_hash` comparison the
  body did not make; a `${NEXT[@]}` array was fenced as ```sh` where it
  needs bash or zsh; exercise 16's "run 2" for `AMER_MKT_FX_C` holds only
  after exercise 4's rerun; and one sentence of rhetoric left the
  sandbox README.
  2918 -> 2926 collected, eight new: seven in
  `tests/test_nightbank_boundary.py` (six scenarios and the runbook-verb
  contract) plus PR-50's real pin in `tests/test_run_history.py`; ruff,
  mypy and arch_check clean, and the full suite green.
  The external adversarial round found seven blockers, all folded in:
  history reads the estate through the SAME validated stream as the
  subscriber's backfill (DL-135's chain proofs -- a spliced foreign
  segment refuses instead of reporting a stranger's rows; a missing
  oldest segment stays the legitimate pruned-history gap); the period
  manifest is held to the FULL PR-22 agreement, not one field; an
  opening that names a seal this root cannot prove REFUSES rather than
  replaying from an empty state (a period running only C2-added jobs
  would otherwise return full-fidelity history from an unproved
  opening); the roll gained a READ-ONLY preflight in the CLI, so the
  unattested-roll refusal writes nothing at all -- not even the target
  directory and its lock (each gate re-runs authoritatively under the
  locks; sound early because attestation is monotone); the runbook's
  audit recipe names the SENTINEL as the fifth input with the
  adopted_from <-> catalog_hash_v1 agreement rule; the README routes a
  retained estate through the boundary-era verbs instead of "delete
  freely" (raw deletion only for a night whose anchor goes with it); and
  the anchor-flag exercise is scoped to lineage-fenced operations with a
  real refusal-then-corrected pair.
  The second round found two shadows, closed: the opening PROOF runs
  before ANY degradation branch (`_prove_opening`: a named seal must be
  readable and be the named one, or even `records_only` rows refuse --
  the manifest-missing path had bypassed the check); and `read_journal`
  refuses an opening record anywhere after line 1 (I1: a segment opens a
  FILE, and an embedded `segment` or `header` is a splice the validated
  stream's splitter would have treated as a file boundary).
  The third round found two, closed: the opening proof binds the
  sidecar's whole identity -- period, estate, and a `next_period` that
  IS this opening -- not the digest alone (a valid sidecar from another
  estate with a rewritten link is an identity graft and refuses); and
  the backfill's cursor containment comes from the validated opening's
  own `first_index` (positional, never a record scan a forged `seq: 0`
  could satisfy), with any seq below the segment's first_index refusing
  as a record the period could not have admitted (I2).
  The fourth round found three, closed: the opening proof compares the
  COMPLETE next_period projection onto the record's own fields --
  segment_no, first_index, the three content hashes, clock_domain, and
  `closed_at == the opening's at` -- so two same-stage seals differing
  only in cutoff or index range cannot be swapped; the segment schema
  requires `first_index >= 1` (a forged 0 would stop the backfill's
  positional containment at that segment and hide every older one); and
  the identity-graft pin was reshaped onto the rolled/pruned single-
  segment root where the opening proof is the ONLY guard -- on a
  two-segment root the chain check fires first and the pin proved
  nothing about the proof it named.
  The fifth round closed the projection at its root: the compared set
  is DERIVED -- every `next_period` model field that is also a ss2.1
  segment field (`period.SEGMENT_FIELDS`, now public), so a field added
  to either model is covered by default -- and the coverage is PINNED
  per field: each shared field forged on the opening refuses, with
  earlier gates (the seq-range invariant for first_index, I1's own
  number rule, the sentinel's estate binding) accepted as the coverage
  where they fire first.

- DL-137 the review after the programme: what ten units of adversarial
  hardening left behind (2026-08-21; the DL-75 architecture review over
  the U6b-U9 tree, run by three parallel readers with the arch-review
  lens; 29 size advisories left to the script, this entry is the half a
  script cannot see).
  THREE DEFECTS found and fixed with pins. (1) The resume sweep parsed
  run directories with an inline `rpartition` that accepted `b.01` as
  run 1 -- sorted-first, a directory this estate never wrote could
  answer the ss7 ladder for a real run's fate; retention's canonical
  parser was promoted to `period.split_run_dir` and both readers use it.
  (2) `--machine-policy bogus` was a clean exit-2 refusal on `run` and
  an uncaught ValidationError (exit 1, documented as an estate failure)
  on `seal` and `estate adopt`; the guard now lives in `_next_profile`,
  which all three routes call. (3) `_check_existing_segment` compared a
  hand-written FIVE fields where `check_manifest_against_segment`
  compares ten for the same question -- weaker by accident; the set is
  now derived (`model_fields & SEGMENT_FIELDS`), like every other
  projection after this entry.
  ACTED, the cheap-deletion slice: ONE `OpenedRuntime.carried_rows`
  derivation (the engine, the auditor and run history had three; the
  docstring's "one derivation" promise is now structural); the third
  uuid4 grammar copy in seal.py deleted (runner_effects' predicate was
  already imported; the Tier-1 copy keeps DL-42's licence, and a new pin
  asserts the two `.by_run_id` spellings agree across that boundary);
  `attest._opening_at` deleted for `period.opening_at`; the
  `check_record_names_sidecar` three-symbol forwarding chain collapsed
  to one name; `period._SHARED_FIELDS` derived instead of hand-listed;
  `plan_retention`'s anchor default computed once; the CLI's read-header
  check extracted from two verbatim copies; the `StagedManifest ->
  StagedNextPeriod` projection unified as `boundary.staged_next_from`
  (four spellings: one hand-listed, one derived, two reflection
  rebuilds); `CommittedBoundary.record` is a property (a stored pure
  function of the seal was a field that could go stale); ONE
  `load_bundle_catalog` (staging validation and audit parsed the same
  way in two bodies); `fsync_dir`/`fsync_file` promoted to
  runner_procid (five spellings of one liturgy step; every dsl41-tier
  module now imports them, and the durability primitive no longer lives
  in the execution tier); `estate.fold_legacy` renamed
  `translate_legacy_records` ("fold" named two unrelated acts, against
  the project's own stated rule); the resume-time `_require_adapters`
  guard made unconditional (its condition was a dead fork of the
  docstring's rule since DL-134).
  DEFERRED, each its own future slice: the cli.py split (the verb-body
  moves land first -- `_wire_from_profile` to runner_startup,
  `_drive_boundary`, `_stage_period`, `_closed_periods` to their owning
  modules -- then a five-module split by domain is mechanical; the 19
  spellings of "EngineError means exit 2", the `_live_seal` copy of
  `_mutate`'s DL-92 ladder and the four spellings of the seals-filename
  rule consolidate as part of it); `_serve_run` wiring adapters bare
  while the profile pins the windows (latent divergence -- the values
  coincide today and no `run` flag reaches them yet; wiring through
  `_wire_from_profile` belongs with that move); a typed LIST-row model
  parsed inside SupervisorClient (four decoders, one strict);
  runner_history's lenient second decoder of `decision.effects`
  (whether history should REFUSE like `read_outbox` or degrade is a
  judgement, recorded, not snuck); `run_until_quiescent`'s three
  chained-negation booleans folded into one `_next_work` choice (sits on
  the bisimulation pins; own slice, own test argument); one
  `check_record(record, rec, schema, cite)` for the seal/segment record
  liturgies; a shared `disagreements()` shape for the seven comparison
  dialects; a shared ss3.2 `from_bytes` ingress for Seal and
  Attestation.
  DECLINED, so the next review does not re-find them: collapsing
  `StagedManifest`/`Manifest` into `StagedNextPeriod`/
  `CommittedNextPeriod` (measured: Manifest == CommittedNextPeriod +
  runtime_profile -- but the pair-of-pairs exists because seal.py may
  not import period.py's owner, the who-may-say-it split is the PR-05c
  refusal mechanism, and a deep rework saving one concept fails the
  review's own ratio); `OpenedRuntime` holding its `Seal` instead of
  seven copied fields (closed_at -> opened_at is a semantic re-frame,
  not a copy); renaming `globals_`/`digest_over`/the three `_naive_utc`
  meanings (churn in frozen modules for a grep convenience).
  LOAD-BEARING, named so this review is usable: the Tier-0/Tier-1
  stdlib re-implementations (DL-42's licence, import-boundary tested);
  the five TOCTOU re-checks over one piece of evidence in the resume
  ladder; the Engine's four input doors (ss0's trust boundary made
  structural); the two-transport pairs (SupervisorConn/Client,
  roundtrip/ControlClient); `read_backfill`'s chain proofs beside
  `select_seal` (a concatenation and a selection are different
  questions); the staged/committed model split as a concept; baseline
  and claim ids derived rather than read (the anti-forgery property);
  `Reclaimed` copied into the segment (the one place a copy is the
  requirement); `validate_boundary` re-running the classifier (an
  enforced rule, not a tautology); the `Head` discriminated union; the
  `floored`/`held`/`prunable` transcription of ss12's own list; and
  every citation comment.
  2934 -> 2937 collected (the parser pin, the machine-policy pin and
  the tier-boundary equality pin); ruff, mypy and arch_check clean; the
  review stamped arch-review/2026-08-21.
- DL-138 the legacy read dialects retire, and the contract that governs the
  next retirement (2026-08-21; a pre-production reset over the whole read
  side -- period-model draft 30, the new `docs/protocol-evolution.md`, and the
  code strip that follows them).
  THE RULING. Adoption from a legacy estate is ruled out. No dsl41 estate runs
  in production, so the five read dialects the tree still carried have no
  producer and no estate left to consume: the `header` journal opening;
  `catalog_hash` version 1; the `result` and standalone `effect` records with
  the `legacy_batch: true` fold that read them; the `manifest/manifest.json`
  run-root layout; and the whole `dsl41 estate adopt` path. Every one of them
  was kept for a single reason -- a run root written before the period model
  must keep replaying, resuming and reporting -- and no such root exists.
  THE CLASSIFICATION. This is a PRE-PRODUCTION RESET under the reset clause of
  the evolution contract below. The ordinary retirement gate is the ACTUAL
  ABSENCE of every instance, and here it is met trivially, because nothing
  was ever written anywhere. The consequence is stated rather than hidden: a
  pre-DL-138 on-disk root is unreadable. The clause is usable only while
  nothing runs in production, and this entry states that condition so a later
  reader can check the claim instead of inferring it. First use, and once
  production exists, the last.
  WHAT REPLACES THE READERS AS PROOF. Those readers were also the tree's only
  live demonstration that its versioning mechanism worked at all. Four things
  carry that now: the contract in `docs/protocol-evolution.md`; owner-local
  refusal-by-name tombstones; per-surface dispatcher tests; and this strip,
  recorded in that document as the contract's first executed retirement.
  D1 RETIREMENT IS REFUSAL BY NAME, through OWNER-LOCAL tombstone registries
  -- never a deleted reader, which produces a generic parse error or silence.
  For record kinds, ONE validator at the `read_journal` layer dispatches three
  ways: a current kind proceeds, from a registry pinned at implementation from
  the read sites plus period-model ss2 and runner-design ss7, `host` INCLUDED;
  a retired kind (`header`, `result`, `effect` -- append-only) refuses naming
  the kind and this entry; an unknown kind refuses naming the kind. Today
  `read_journal` IGNORES an unknown `rec`, and the change is deliberate:
  version gating sits on the opening `segment`, so an unrecognised kind inside
  a version-matched segment is corruption, not tolerance. `legacy_batch` is
  pinned three ways at the same validator -- exactly `False` proceeds, `True`
  is retired and refused naming this entry, missing or non-boolean is
  malformed and refused as a DISTINCT error -- so `runner_history` and
  `retention`, which parse decision effects without `read_outbox`, inherit all
  three. The validator reads `rec` and `legacy_batch` only; per-record key
  strictness stays whatever each schema already declares.
  D2 `legacy_batch` STAYS on the `decision` record, required and false. The
  writer pins it; the reads are D1's. No wire break.
  D3 SCHEMA SURGERY IS FULL REMOVAL, no tombstone fields: `Sentinel`'s
  `adopted_from` AND `adopted_anchor`; the `adopting` `Head` variant with its
  whole operative surface (`create_adopting`, the `close_period` adopting
  branch, `_spell`, `act_on_head`, the startup adoption flags);
  `SealRequest.for_adoption` and the boundary-request schema narrowed in the
  same change; the seal `source` literal narrowed to `"request"` with
  `attest._boundary_request`'s derivation simplified; `adopt_request_id`;
  `claim_root(adopted_from=...)`; and the `segment` field `catalog_hash_v1`
  with its schema entry. An on-disk anchor whose head state is `adopting` gets
  a PRE-PARSE refusal naming this entry -- a retired-STATE tombstone in
  `boundary`, not a generic validation error a reader cannot act on.
  D4 ONE three-way catalog-hash-version dispatcher -- current proceeds, 1 is
  retired and named, anything else is unknown and refuses generically --
  shared by EVERY owner path: `check_segment_record`, `Journal.create`'s
  independent gate and `catalog_hash_at`. `catalog_hash_v1()` and the v1
  recipe go, and `catalog_hash_for`'s header arm with them. Three owners asked
  one question in three places, and a strip that fixed only the one with a
  test would have left the other two answering it differently.
  D5 THE ADOPT-FUNNEL REFUSALS STOP NAMING A DELETED VERB. `resume_run`'s
  legacy-header refusal and its `_is_legacy_header` helper are subsumed by
  D1's tombstone. `boundary.claim_root` and `retention.plan_retention` do not
  route through `read_journal`, so each gets an owner-local header-only
  tombstone check: a recognised header root refuses naming this entry, while
  an unknown non-estate root keeps its generic refusal.
  `attest.rederive_seal` refuses "retired dialect" without naming a verb that
  no longer exists.
  D6 THE TRIPLICATED DECIDED-SET JOIN drops its `"result"` arm in all three
  places in one unit: `runner_journal.read_decisions`,
  `boundary.externally_requested_attempts` and `estate.check_drained` -- the
  last of which goes with its module.
  D7 DEAD CODE DELETED AND NAMED, corrected from the first reading.
  `runner_startup`'s legacy default reconciliation windows -- the two
  `settle_seconds`/`grace_seconds` fallbacks commented "a legacy root: the
  shipped defaults" -- have been unreachable since DL-134 made a legacy root
  refuse before it could reach them, and DL-134 did not name them. They go,
  with the obsolete legacy comments beside them. The refusal a few lines ABOVE
  them STAYS and is load-bearing: a `segment` journal with no
  `periods/000001/manifest.json` is a root that LOST its pin, and degrading
  there would skip every profile gate below. Two branches in one function, one
  dead and one carrying the section's whole argument -- naming which is which
  is the point of this paragraph. DL-134's own named dead branch,
  `_resume_untraced_starts`' no-adapter `continue`, goes too.
  D8 THE EVOLUTION CONTRACT, `docs/protocol-evolution.md`, written per
  CONCRETE protocol with SEPARATE compatibility and lifetime columns, because
  a tolerant reader is not a long-lived one and a long-lived artifact is not
  automatically a tolerant one. Rows are split BY TOLERANCE RULE, not by file:
  WAL journal records; the closed estate artifacts of period-model ss3.2 --
  every member it declares closed, `candidate.json` and `staged_manifest.json`
  included, assigned by reference to the rule ss3.2 already states rather than
  restated here; the tolerant estate files -- `sources.json` and the
  FW-written watch records, whose readers take the fields they need while
  their versions still refuse; the tolerant supervisor artifacts
  (receipts, replies and the run-id index); the wrapper-owned spool, which is
  `spawn.json` and `status.json` and NOTHING else -- `watch.jsonl` is
  FW-adapter-written and lives in its own row; the supervisor socket AND the
  control socket, one row each; and `state_machine_version` as a SEMANTICS
  row, frozen across a transition, evolving only by a full drain and a
  new-estate genesis. UNKNOWN FIELDS AND UNSUPPORTED VERSIONS ARE DISTINCT
  CASES ON EVERY ROW: a tolerant row ignores an unknown field and still
  REFUSES an unsupported version, and the two are tested separately. A new
  dialect enters service in four steps -- introduce with dual-read, overlap
  with POSITIVE compatibility tests that pin both versions, switch the writer
  for new instances only, then retire. The retirement gate is the actual
  absence of every instance, floored, held or prunable-but-present alike:
  DL-135 made pruning optional and policy-driven, so a prunable artifact can
  stay readable indefinitely, and "prunable" is a verdict rather than a
  deletion. MIGRATION IS NOT THIS CONTRACT'S MECHANISM: if one is ever needed
  it is its own decision-log entry with lineage and verification proofs -- the
  path this entry retires is the shape such a thing takes -- and an in-place
  rewrite of an immutable digest-bound artifact is never permitted. The reset
  clause is the only bypass, under its own stated condition. Every evolution
  event owes one decision-log entry plus dispatcher tests per affected row.
  D9 RETIRED FORMATS GET OWNER-LOCAL NAMED TOMBSTONES, not generic refusals:
  `catalog_hash` version 1 through D4's dispatcher, and the legacy `manifest/`
  layout in `runner_history` discriminated ON THE FILE --
  `<run-root>/manifest/manifest.json` present where `periods/<id>/manifest.json`
  is absent is the retired layout and refuses naming this entry, while a
  `manifest/` directory WITHOUT that file is unknown residue and refuses
  generically. The two are different states and an operator needs to be told
  which one is on the disk.
  D10 THE ADOPTION-ONLY RUNTIME SURFACE GOES WITH THE PATH. `Engine.hold_outbox`
  is retired: the field on the engine in `runner.py` and the dispatch branch
  that read it -- the one that made the whole dispatch surface a no-op, with
  exactly one caller and a docstring citing the period-model ss11 steps this
  entry deletes. The `estate` CLI group's help text and `estate.py`'s module
  docstring stop promising adoption; the group keeps `reclaim` and `prune`.
  WHAT THE SPECS SAY NOW. `docs/period-model.md` goes to DRAFT 30. ss2's
  retired-record list reads "refused by name since DL-138" and confirms `host`
  as current; ss2.1 loses `catalog_hash_v1`; ss2.3 states the three
  `legacy_batch` cases; ss1.3 loses the `adopting` head state and its two
  transitions; the sentinel loses `adopted_from`; ss3.1's seal source narrows
  to `request`; ss8's adoption mode goes; ss11's seven-step adoption
  transaction is replaced by a short retirement note pointing at the contract,
  and the recovery matrix's legacy-header row now reads "refused (retired
  dialect)". ss13's preamble gains a ROW STATE: active, or retired with the
  entry that retired it and the replacement tests named. PR-48 is RETIRED here
  and KEPT in the table so its citations still resolve; its replacements are
  named in its own cell -- the header/`result`/`effect` and unknown-kind
  dispatch, the `host`-accepted positive, the `legacy_batch` trio driven
  through a history and a retention consumer, catalog-hash version 1 through
  both the journal reader and journal creation, the manifest-layout pair, the
  `claim_root`/`plan_retention` header-root tombstones, the pre-parse
  `adopting` anchor refusal, and `estate adopt` as an unknown command.
  PR-01b, PR-02c, PR-02f and PR-47b stay ACTIVE and keep their numbers,
  losing only their adoption clauses -- PR-47b keeps every request-only audit
  obligation it had. PR-01c is reworded: its refusal names a retired dialect
  instead of a verb. `docs/citation-index.md`'s PR row now says a property is
  tested while the row is active and that a retired row cites its retiring
  entry and its replacement tests. concurrency-model ss7 retires "a legacy
  `header` pins v1"; runner-design ss7 keeps the retired record entries as
  history with their read promises withdrawn; the runbook's Adopt section
  becomes a pointer to the contract and states that a pre-boundary-era root is
  not adoptable.
  WHAT IS KEPT, AND STRENGTHENED. `read_journal`'s opening requirement -- the
  first record is a valid `segment` -- with `check_segment_record`'s header
  exemption deleted so the ss2.1 check is unconditional; the splice refusal;
  `read_backfill`'s chain proofs with their header short-circuits gone; the
  D4 dispatcher replacing AND keeping `Journal.create`'s version gate at the
  same strictness; `legacy_batch` required-false; and the protocol v3
  handshake, the `sha256:` grammar, the canonical forms and the wire
  tolerant-reader rules, untouched and now cited as rows of the matrix.
  THE TAIL. Unit L1 landed the documents above. Unit L2 lands the code half in
  ONE unit -- the readers, the D1 validator, the D4 dispatcher, the D5 and D9
  tombstones, the adoption path, the schema surgery and the tests -- because a
  split leaves a red intermediate state. The pinned test counts move with it
  and are appended here.
  THE CODE HALF LANDED (L2). 2937 -> 2921 collected: 27 legacy tests deleted
  with the `legacy_twin`, `_legacy_root`, `_unfold_decisions` and `_adopt`
  fixtures, 11 added -- the D1 roster in `tests/test_decision_record.py`, the
  D4 trio in `tests/test_period_identity.py` (the journal reader, journal
  creation, and the ordering pin the review added), the unknown-field half of the
  control row beside the version half it was folded into, `estate adopt` as
  an unknown command, and the two direct pins named below -- and about a
  dozen rewritten to keep their native halves. Four things the
  implementation decided that the plan did not.
  FIRST, D4 had a FOURTH owner: `period.check_manifest_self_consistent` asked
  the same version question in its own words, so it routes through the
  dispatcher with the other three -- a gate that told an operator holding a
  retired root something different from the reader that opens it is the
  defect D4 exists to prevent. SECOND, `runner_startup`'s missing-manifest
  refusal loses its `rec == "segment"` fork with the `header` that was the
  other arm: the refusal is unconditional now and the profile block below it
  is no longer nested, which is what the deletion left behind rather than a
  change of rule. THIRD, `examples/nightbank/RUNBOOK.md` had to be swept, and
  it is not documentation for this purpose:
  `test_every_dsl41_verb_the_runbook_types_exists` resolves every `dsl41
  <verb>` the runbook types against this build, so exercise 18 becomes a
  retirement note pointing at the contract. FOURTH, one test was LEAKING and
  is recorded rather than quietly fixed: the segment-rewritten-to-v1 pin in
  `tests/test_period_identity.py` edited `journal.jsonl` -- the SENTINEL --
  instead of the WAL beside it, and its `catalog_hash` match was satisfied by
  a pydantic "extra inputs are not permitted" error, so it never once drove
  the gate it named. It drives the
  WAL now, as `test_the_journal_reader_refuses_the_retired_recipe_by_name`,
  and after the review below it drives the record v1 REALLY wrote rather
  than a v2-shaped one relabelled -- the second-order leak the first fix
  left, and the one that hid the ordering defect recorded at the end.
  One property was pinned ONLY through the adoption path, and it is
  RE-PINNED directly rather than left to be discovered: "presence is not
  agreement -- the full seal-to-opening validation runs before the recovery
  CAS" is now
  `test_an_existing_next_segment_that_disagrees_refuses_before_the_cas` in
  `tests/test_boundary.py`, with
  `test_an_existing_next_segment_that_agrees_is_verified_and_the_cas_runs`
  as the positive case it is measured against. Both build the window at the
  opener's OWN seam -- claim taken, segment durable, head still `claimed` --
  so the retry meets what a crash leaves rather than what anchor surgery
  composed. The refusing case drives `runtime_hash`, one of the five fields
  the hand-written check DL-137 replaced never compared, and asserts the
  head is still `claimed` after the refusal: the field pins the DERIVED set,
  the head pins the ORDER.
  TWO CLAUSES WIDENED IN REVIEW. D6 named three copies of the decided-set
  join; `runner_history._note_executor` had a fourth reader of the
  standalone `effect` record, unreachable and -- once its test went with the
  dialect -- untested, so it goes with the other three. D5 asked
  `attest.rederive_seal` to REWORD its opening refusal; with `OPENING_RECS`
  down to `segment` the branch is unreachable, and the only message it could
  carry calls whatever it found "retired" without naming an entry, which is
  the merge ss6 forbids. It is deleted and the guarantee it duplicated is
  stated where the read happens.
  TWO DEFECTS FOUND IN REVIEW, both in the ORDER the reads run in. FIRST,
  the tombstones ran too late. The current-dialect schema and the later
  records were validated before the opening's version was dispatched, so a
  real version-1 opening -- bare hexdigest `catalog_hash`, `catalog_hash_v1`
  field -- was called malformed by the grammar or the unknown-key check
  instead of reaching its tombstone, and an unsupported opening followed by
  an unknown record kind reported the KIND. The version verdict owns the
  file's fate and is taken first now: `period.check_segment_version` is
  split out of `check_segment_record` and runs at the top of it and at the
  top of `read_journal`, before D1's pass.
  `test_the_openings_version_is_dispatched_before_any_record_is_read` pins
  the order, with the current version as its control. SECOND, D5's
  header-only tombstone was header-only in this entry and not in the code:
  `opens_with_retired_rec` answered for `result` and `effect` too, so
  `claim_root` and `plan_retention` named DL-138 over a file that was never
  a legal journal opening. The helper is `opens_with_rec` now and reports
  the kind raw; each owner compares it to `header` and keeps its own
  wording, and the two owners' tests carry the `result` and `effect`
  residue cases beside the garbage one.

- DL-139 one decoder for a decision's effects: run history refuses what
  `read_outbox` refuses (2026-08-21; the DL-137 deferred slice named
  "whether history should REFUSE or degrade is a judgement, recorded, not
  snuck" -- this is the judgement).
  THE RULING. `runner_history` REFUSES a malformed `decision`, exactly as
  `read_outbox` does. It does not skip the effect and print the row.
  FOUR REASONS. (1) Refuse-don't-degrade on DURABLE EVIDENCE is this
  estate's standing rule; a decision is durable evidence. (2) After DL-138
  the record-level checks -- the kind, `legacy_batch` -- are already
  centralized at `read_journal`'s `check_record` for every consumer, so the
  per-effect shape was the last split read. (3) History is an
  operator-facing TRUTH SURFACE: an effect silently dropped is a row with
  no executor or no bound run_id, which mis-reports where a run went and
  which process was its own -- no silent loss applies to a report, not only
  to a write path. (4) Two readers answering "is this decision valid"
  differently is the copies-of-one-question defect DL-137 and DL-138 spent
  their whole length killing. Forensic inspection of a corrupt log is a
  different tool's job; it is not the default of the history reader.
  THE MECHANISM is one shared decoder, not a second copy of the checks:
  `runner_journal.decision_effects(record)` returns the typed `Effect`s of
  one decision and refuses the list that is not a list, the effect that
  does not validate, a `run_id` outside the ss11a grammar, and the DL-118
  birth identity a native effect always carries. `read_outbox` calls it,
  with its wording unchanged; history's two raw loops -- the executor read
  in `_leaf_rows` and `_bound_run_ids` -- call it and let the refusal
  through. `_note_executor` is deleted: its whole body was the duck-read a
  typed effect makes unnecessary, and DL-138 had already emptied its other
  half. The decoder takes no `where`: the record names itself by `index`,
  so ONE corrupt record gets ONE refusal text whichever reader met it, and
  a test asserts the three readers' messages are equal rather than similar.
  At the I/O shell `read_run_root` wraps that refusal in `RunHistoryError`
  naming the root, like every other refusal in that module -- `dsl41 runs`
  reads several roots in one command and "decision at index 5" does not say
  whose.
  WHAT STAYS LENIENT, ON PURPOSE, so the next review does not re-find it:
  the duck-reads on records that are NOT a `decision` -- the STATUS
  payload's job/run_number (a run-number-less operator CHANGE_STATUS is
  legal, SEM-01), the spool's timestamps, a `dispatch` without a `run_dir`,
  a stranger's spool record reading as absent. Those are MISSING facts,
  which decision 3 and decision 5 of the module require a projection to
  survive; this entry is about CORRUPT ones.
  NOT IN SCOPE, named so it is not mistaken for an oversight:
  `retention._spawn_periods` parses decision effects with its own
  hand-written per-effect check. It already refuses rather than degrades,
  so it is a third SPELLING of one question and not a second ANSWER; it
  converts to the shared decoder when that file is next opened.
