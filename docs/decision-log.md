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
