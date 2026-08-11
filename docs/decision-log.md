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
