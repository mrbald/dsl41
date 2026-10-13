# Statechart engine — the frozen contract

Status: frozen (DL-324). Each change to a frozen item needs a decision-log entry.

This document fixes the executor semantics that every declared runner state machine runs under.
DL-289 made each machine declare its transition table. DL-324 makes the declaration the only
spec, and this document says how the executor reads it. Cross-machine invariants and vendor
requirements stay in the other normative documents (§1).

`docs/state-machines.md` is generated from the declarations. It lists the rows. This document
says how a row is chosen and run.

No machine is converted by DL-324. The executor lands with the pilot (§16).

## 1. Principle

Every runner state machine is a declared, executable statechart. The declaration picks the
transition. Its guard and its action are named callables beside the row. No other code decides
a move.

A converted machine's old decision code and its `take` checking are deleted.

Docs are generated from the declaration. Semantics prose cites row anchors (§14).

Cross-machine invariants and vendor requirements stay in the normative documents (§14). "One
revision per committed input" is a transaction guarantee, not a row.

This reverses one rule of DL-289 for a converted machine: `take` never raised in production, and
concurrency-model §4 says so, and the executor now raises three hard errors (§3
`AmbiguousTransition`, §5 `ReentrantDispatch`, §10 `UnhandledEvent`). Each is a bug that would
corrupt state, not a check to note. DL-292's violation channel (`transition_violation`, its trace
line, `--on-transition-violation`) stays for the machines not yet converted. The entry that
converts the last one retires it.

## 2. Elements

The executor's model is UML 2.5, with every open choice pinned in this document. Where UML 2.5
leaves a choice open, or reads differently, the rule here stands. Its elements are:

- simple and composite states;
- orthogonal regions;
- initial and final pseudostates;
- choice pseudostates, with ordered guarded branches and a mandatory else;
- completion transitions out of transient states;
- internal transitions;
- entry and exit actions;
- deferrable triggers;
- time events.

## 3. Selection

For one event in one instance, the executor computes the whole firing set before any action
runs.

In each region, the walk starts at the innermost active state and goes outward. At each state,
in order: an enabled row for the event is taken as that region's candidate and the walk stops;
a declared deferral of the event (§6) ends the walk with no candidate; otherwise the walk goes
on to the parent. So an inner row beats an outer deferral, and an inner deferral beats an outer
row.

Within one state, the rows for one event are mutually exclusive by contract. The executor evaluates
every guard. More than one enabled row raises `AmbiguousTransition`, in production too. Rows are
therefore unordered. Ordered branches with an else exist only in a choice pseudostate.

A row never targets a state in a sibling region; `well_formed` refuses it. A row whose source is a
state shared by several regions (their common ancestor) is one candidate, however many walks reach
it. When two candidates' exit sets intersect, the one with the deeper source wins. When neither is
deeper, the executor raises `AmbiguousTransition`: a machine that needs both declares one row on
the shared ancestor instead. Conflicts are resolved from the deepest candidate outward: a
candidate beaten by a deeper one is dropped before pairs of equal depth are judged. A candidate
whose source state another surviving candidate exits is dropped, internal rows included;
`well_formed` warns where that can happen. `well_formed` warns where two regions declare rows for
one event of which one leaves the shared ancestor. A row of a state that several regions share
(their common ancestor) is not a candidate when any region below it ended its walk in a deferral
of the event: the inner deferral wins over the outer row, as within one region. A sibling
region's own row still consumes the event.

The firing set is final before any action runs. It executes in region declaration order.

The outcome for the whole instance is decided from all its regions together. If any region
produced a candidate, including an internal `ignored` row, the event is consumed and the firing
set runs. An `ignored` row is a candidate only when no other region produced a candidate that is
not `ignored`; otherwise it yields and writes no trace line. A refusal row beside another region's
candidate raises `AmbiguousTransition`; `well_formed` warns where a refusal row and another
region's row share an event. Otherwise, if any region's walk ended in a deferral, the event is
deferred. Otherwise the event is unhandled (§10). A region whose walk found neither a row nor a
deferral takes no part; it needs no rows for another region's events.

## 4. Execution of one row

An internal transition runs its action only. It has no exit and no entry.

An external transition runs:

1. the exit actions, inner to outer;
2. the action;
3. the target's choice, if the row ends in one, resolved inside this same microstep: its
   branch's guard picks the final target and its action runs (the instance never rests in a
   pseudostate);
4. the entry actions of the final target, outer to inner;
5. the final target's completion transition, queued as §5 step 3 says.

Entering a region's final state completes the region. A composite whose regions are all final
raises its completion event. That completion is a chained item of the microstep that entered the
last final state, queued as §5 step 3 says. A completion item whose source state is no longer
active, or active again by a later entry, when it pops is discarded without error: a completion
belongs to its state. A chained event is an ordinary item and resolves by §3 and §10 like any
event.

Completion events and chained items precede every pool event.

## 5. The step and the worklist

Terms. An item is an external event, a chained event, a released event, or a continuation. A root
item is an external or a released event. A microstep is the handling of one item: its selection and
execution (§3, §4). A macrostep is one run to completion: a root item and every item it chains,
handled as microsteps until the worklist is empty. A transaction is a tier's unit of durability: in
the oracle, one input's batch; it may hold several macrosteps. The pool is the tier's queue of root
items.

A macrostep is iterative on one executor worklist and never recursive.

1. Pop an item: an external event, a released event, a chained event, or a continuation.
   Guards run now.
2. Select and execute, as in §3 and §4.
3. Items an action reports (§8) go to the front of the worklist, in reported order
   (depth-first). The target's completion transition is itself a chained item. It is placed
   behind the items this microstep reported, so a continuation runs only after the whole
   cascade it caused. Every chained item carries the ancestry of the microstep that produced
   it; a completion item too. When the worklist is empty, the deferred release runs (§6);
   then the next root item from the pool.
4. A dispatch while a macrostep is in flight raises `ReentrantDispatch`, in production too.
   Tests assert that the chain works and that the re-entrant call raises.
5. Guards are pure. Actions change machine state and report intents. Each tier's durable
   protocol carries the intents out:
   - the oracle's effects go through the outbox, after DL-320's dry apply on `Oracle.fork()`;
   - the supervisor's go through its index, receipt, spawn and reply protocol;
   - the engine's go through the WAL decision.

   The executor itself does no I/O.

A tier may read how an event would be selected without running it. `Executor.resolve` runs the
selection of §3 for one instance and one event against the instance's present configuration:
guards are evaluated, no action runs, nothing changes, and no hit is recorded. It answers neither
the release of deferred events nor the pool ahead of the event, so it refuses an instance with a
deferred event, and a full deferred queue is outside it. Inside a macrostep it raises
`ReentrantDispatch` (DL-335).

A tier may copy an idle executor. `Executor.fork` returns a new executor that holds every
instance as it stands: its configuration, its deferred queue, its generations and its entry
serials, and the history rings. The two share each instance's record until a dispatch on either
side writes it. A write stores a new record on its own side, so neither side sees the other's
moves. A fork costs one reference copy per instance, a copy of each history ring, and one new
record per instance the tier gives new data; the state owner gives every host instance new data,
bound to the fork's rows, so the rest of its fork's cost is one record per host. Data not given a
new value, and every event payload, stay shared by design, so a tier treats them as immutable. A
fork inside a macrostep raises
`ReentrantDispatch`. A fork with a root item in the pool raises a `StatechartError`, because
that item would run on one side only (DL-339).

A tier whose rows code outside a chart still moves may bring an idle instance back to its row.
`Executor.realign` sets an existing instance's configuration and data as `restore` sets a new
one's: no action runs, its states have no entry yet, and its generations stay. An instance with a
deferred event or an item in the pool raises, since no move of its own would handle it, and a
realign inside a macrostep raises `ReentrantDispatch`. The machine's history ring gets a line for
it, so an error's report shows the jump (DL-341). The method is transitional: the entry that
converts the oracle's last input family removes it.

The executor's errors, among them `AmbiguousTransition` (§3), `ReentrantDispatch` (step 4) and
`UnhandledEvent` (§10), subclass one base, `StatechartError`. Each is an assertion failure, not
a fault to contain. DL-320's classification never catches it: `trial_apply` and
`classify_fault` re-raise it as they re-raise `TransitionError`, and nothing is appended. No tier
may swallow it. The tier stops before any further durable write of its own; a record already
durable, a seal record among them, stays.

The error is structured. It carries the machine, the instance key, the active configuration, the
item and a summary of its payload, the item's ancestry, the candidates found, the deferred queue,
and a ring of the last microsteps of the machine that raised. The executor keeps one ring per
machine, of a declared size; the default is 64. `str()` of the error renders all of it, one fact
per line. The engine and the supervisor print it on stderr, each line under the prefix
`dsl41: statechart error`, and exit 7. The units list 7 in `RestartPreventExitStatus`.

## 6. Deferred events

A state or superstate declares `defer={events}`.

Whether the event is consumed or deferred is decided by the walk in §3: the innermost state
that has an enabled row or a declared deferral decides. An ancestor's `ignored` row does not
override a substate's deferral.

An event that no region consumes and that some active state defers joins the instance's deferred
queue. The queue is FIFO and bounded. Its overflow policy is declared: `stop`, a hard error, or
`drop`, which discards the new event and keeps the queue. Under `drop` the deferring state declares
an internal `overflowed` row that records the dropped event; it runs inside the same microstep, not
as a chained item.

Release is judged when a macrostep ends with the worklist empty. The deferred queue is re-offered
in order, each released item as its own root item and macrostep, ahead of the pool. A released
item that the new configuration defers again keeps its place at the head of the queue, and
release stops there until a later macrostep.

Stale-identity events are refused by a guard row. They are never deferred.

## 7. Clocks and time events

Each machine declares its clock. The clock is logical for the engine tier and the oracle, and
monotonic for the supervisor's lease and process.

A time event is a declared trigger. How the owning tier produces it is the tier's: an armed
timer, or a deadline it samples on its clock. The executor reads no clock.

A time event that the tier may re-arm declares a generation. The executor discards a fired event
whose generation is stale before selection. A time event without a generation is handled only
by rows; a stale one takes its declared row.

Ties between a time event and another item at the same instant are pinned by the machine's
declaration, in the entry that converts it.

## 8. Chaining and continuations

An action never calls the executor. It returns the events it raises, to any instance of the same
executor, and the continuation it requests. An event for another tier is an intent (§5 step 5),
carried by that tier's protocol. The executor queues them (§5 step 3).

A former call-then-continue site becomes a transient state whose completion transition is the
continuation. Its guard reads the state after the chained items ran, because the completion is
queued behind them.

A machine that bounds re-triggering declares the bound as a row guarded on an item's ancestry
depth. Items of one macrostep that are not ancestors of each other are never counted against each
other; only a chain is.

How the oracle's cascade phases and its per-job start bound (DL-304), and whether DL-306's
recursion-limit fitting is still needed, map onto these rules is ruled by the entry that converts
the oracle (§16).

## 9. Wait states

A wait state awaits a reply or a time event. It may or may not defer.

It declares at least one transition on that reply or time event, or it carries
`stays: <reason>`. `well_formed` enforces this.

## 10. Completeness and well-formedness

Completeness is syntactic. For every state and every event in the machine's alphabet, the
configuration resolves to one of:

- a row;
- an internal `ignored` row, which writes a trace line where the tier keeps one;
- a deferral;
- a declared refusal.

Absence is a well-formedness error.

Completeness is judged per instance, over all its regions together (§3). At run time, an event that
no region consumes and no active state defers raises `UnhandledEvent`, in production too: the
guards of a state's rows for one event partition the cases by contract. Like every
`StatechartError`, it stops the tier (§5). This reverses DL-293's
rule of one ignore row per state it occurs in: an ignored event is declared once, on a
superstate.

Ignored events are declared once, on a superstate.

The generated test is in §13.

## 11. Instances and boundaries

A declaration is a type. Instances are per job, per lease, per subscription, and so on; a box is a
variant of the job machine (§16).

One executor per tier object (an `Oracle`, a `Supervisor`, an `Engine`) owns one worklist for all
its instances. A process with an `Engine` has one executor for the engine tier's machines: the
wiring builds it before the engine and hands it to the supervisor client first, then to the engine
(DL-330). The oracle's state owner (`RuntimeState`) has its own executor, for the machines whose
state it owns (DL-333). A fork has its own executor; a fork of the owner copies the source's
(`Executor.fork`, §5) and binds each host instance's data to the fork's rows (DL-339). A durable
file that no process owns has an executor per move: the lineage anchor's methods each build one from
the file they read under the anchor lock, post the move's events, read and settle the outcomes
before the one anchor write (a claim writes its claim file first, then the head), and drop it
(DL-336). `ReentrantDispatch` is per executor; a call from one tier into another tier's executor is
not re-entrance.

The oracle becomes one composite `job` machine per job, with named regions for its status, its
flags and its holding. Its regions, its box variant and its write path through `RuntimeState`
are ruled by the entry that converts it (§16).

DL-337 declares that machine in `src/dsl41/job_chart.py`: one chart `job` with the regions
`status`, `flags` and `holding`, the box as rows guarded by the job type, and the three tables'
id spaces kept as `job.1NN`, `job.2NN` and `job.3NN`. It is checked against the three tables,
row by row and through replays of the old oracle's walks.

The second oracle drives it (DL-341). Each job row has one `job` instance on the state owner's
executor under the row's key; the owner restores them from the rows where it restores the host
instances, and a fork copies them. While code outside the chart still moves rows, the owner
realigns each instance an input touched with its row at the input's commit, and the second oracle
does so for a job before it dispatches to it, so during the conversion the chart decides each
input from the row's projection. A converted input is dispatched to the job's instance, and the
fired row's writes are made by the old oracle's methods. The status written is the one the chart
reached; the old-versus-new harness judges a wrong status row. The check covers what the old code
still decides: the holding, the job type behind a row that takes a job's or a box's path, and the
flags, which the converted inputs move (a flag input, a waiter's kill, a forced start's clears, a
start's arm and its consumption, a leaving waiter's arm) and the other converted writes leave
alone; the flag moves of code the chart does not drive yet are realigned before the dispatch, not
checked. A move that leaves STARTING or RUNNING has its flags compared when its status is written
and its holding when its own settlement has run; any other move has both compared when its status
is written, before the box rules can start the job again. A difference is a statechart error. The
chart is registered when the last input family converts.

Every attempt to start a job is one `start` on its instance (DL-342): a wake, a box start and a
retry post it, and STARTJOB, FORCE_STARTJOB and the window timer's replay relay it, through
`job.177` to `job.179`, FORCE_STARTJOB after its clears. Its payload computes each oracle fact on
the guards' first read, in the order today's rules test them, so an attempt computes only the facts
the old oracle computes. The per-job start bound is a refusal row, `job.100`, on a fact: the job's
starts in progress. An attempt that reaches the admission settles a run whose settlement the box
rules hold back first, and the admission is read after it (DL-340); a read of the attempt says
whether it gets that far. The chart decides; the old methods write the verdict. The first status
write of the attempt is compared with the chart's configuration, and the RUNNING that follows an
admission is the chart's `started`, dispatched and compared at that write; an attempt that moves no
status is compared after its own writes. Until the cascade slice removes the recursion, the chart
oracle's cascade is fitted by its own measured frames per nested start (`fit_recursion_limit` takes
the oracle class). An input on which both oracles raise alike, a RecursionError of either kind or
the same error, agrees in the harness on its attempt half, whose writes are not compared: a control
input that raises is refused and commits nothing, and an engine-made one is quarantined with its
rejection logged and its time half committed. So the harness applies that time half alone on both
oracles, compares it and goes on; the engine stops only when the time half itself raises, and where
it raises on both the harness stops there too.

KILLJOB and the TIMER input are dispatched to the job's instance too (DL-343), and the input
passes its own relay to the attempt, so no cause text is read. A live job's kill (`job.114`) and a
term_run_time on the running run it was armed for (`job.115`) terminate through the old method,
whose terminator cascade for a box stays old code until the cascade slice. A waiter's kill drops
its arm (`job.217`, `job.226`) and the flag move terminates it (`job.111`); a kill on any other
status is ignored with a trace line, and a stale term_run_time writes nothing (`job.184`). Each
move is compared as a STATUS move is. The deadline alarms move no state and stay code. A window
timer reads its own facts before any gate, as the old oracle does: whether the postponed start
still belongs to its box run, and whether a scan continuation is superseded. They are real facts
of its relayed `start`: a stale one is refused with the old text, a live continuation resumes the
scan, and any other is the attempt. Timers stay armed by code; the chart declares moves, not timer
writes.

The flag inputs are dispatched to the job's instance too (DL-344). The fired rows decide each
dimension the old code read from the row or the catalog. Whether the flag was already set is the
row's source state, which the follow before the dispatch makes the row's; a wrong flag target is
caught by the compare after the flag's write. Three dimensions a guard decides are checked against
the row or the catalog before any write, each a statechart error: whether ON_ICE meets a waiter,
whether ON_NOEXEC settles a job or a box, and whether it settles at all (a job settles out of the
queue or from FAILURE or TERMINATED; a box that is not INACTIVE settles; whether an INACTIVE box
settles is a fact of the payload, left to the harness). An input the job ignores
takes the status region's ignored row (`job.148` to `job.158`) and writes the old EVENT_IGNORED
line. A flag row is written by the old flag move, with its marker and line, and the row is
compared with the chart's configuration after it. A status that the `flag moved` fires is
compared as a STATUS move is: an iced waiter leaves the queue (`job.112`), and ON_NOEXEC settles a
waiter, a FAILED job or a box (`job.113`, `job.121`, `job.136`, `job.169`, `job.170`). ON_NOEXEC's
payload reads a box's contained jobs only where the old rule reads them. The moves the chart does
not declare stay old code, in the old order: an ice's box rules and wakes, the mark of a member
taken off ice in its running box, the waiters' wake on a hold, the start retries of OFF_HOLD and
ON_NOEXEC (each one `start`), and a box's members on ON_NOEXEC and OFF_NOEXEC.

RELEASE_RESOURCE and each waiter's readmission are dispatched to the job's instance too
(DL-345). Held units go back with the old line and the queue wakes (`job.307`); a job that
holds nothing (`job.315`), or a live run whose units go back when it ends (`job.316`), takes the
old no-effect line. Whether the job holds units it can give back is the row's, checked before any
write. The queue's scan and its order stay code: the chart has one instance per job, and the
waiters' order (rank, then the queue's order) is the store's. Each waiter the scan reaches gets
one `readmit`, whose payload computes each fact where the old scan reads it. An admission
(`job.108`) is written by the old admission and run, its status writes compared as a start's; a
member of a box that stopped running is cancelled (`job.109`); a waiter that fails the queued
recheck drops its arm (`job.216`, or `job.230` with none, the row's choice, checked before any
write) and its `flag moved` leaves the queue (`job.110`), the window or day disposition after it
staying old code; any other waiter stays queued (`job.181`). A cancel and a leave are compared as
a STATUS move is, and a move compared inside the cascade of another leaves the other waiting for
its own settlement.

The box rules are dispatched to the instance each one moves (DL-346), where the old cascade
runs the rule, so the cascade keeps the old recursion's order and depth. A member's status write
reaches its box. A member that ends FAILURE or TERMINATED meets the box's terminator (`job.130`).
A RUNNING box's completion door reads its overrides (`member moves`), then the default fold
(`completion moment`). An idle box re-derives its status (`box recompute`). The walk up to the
boxes above reads their overrides through `member moves`. A box's start, run and end reach its
members: the reset (`box start`), auto_hold (`box running`), the end of the run's arms (`box run
ends`), the job_terminators (`box ends FAILURE or TERMINATED`), the batch INACTIVE cascade (`box
set INACTIVE`), and OFF_NOEXEC's clear (`OFF_NOEXEC on its box`). The period's first input gives a
removed job's held units back (`job.308`). Each payload computes its facts where the old rule reads
them. A box rule's target is the chart's choice: the status written is the one the chart reached,
and a fact that derives another target is a statechart error before any write. A box's move is
compared as a STATUS move is, a batch row once the batch has written and settled it, and a flag row
after its write; a row that moves nothing must leave the instance's configuration as it was.

The chart decides each box rule's row; the oracle decides when a rule is posted, and to which
instances. `box running`, `box run ends` and `box ends FAILURE or TERMINATED` reach the box's
direct members; `box start`, `box set INACTIVE` and `OFF_NOEXEC on its box` reach every job it
contains. Five code gates choose an audience the chart does not see: `box start` skips a live
subbox's subtree (`_contained(skip_live=True)`, which also skips a live or queued job); `member
moves` is sent by the completion door on the moves it sees, not on a non-terminal move (a
terminal, RUNNING or resolving move only); `box recompute` is sent by its callers' gates, run
numbers and the cascaded-subbox case, LIVE and TERMINATED excluded (a member's terminal move, or
the direct target of an operator's INACTIVE, never a row the cascade took INACTIVE); the walk up
skips an ancestor whose run number moved; and the parent recompute after an operator's INACTIVE
needs the parent's run number unchanged. These audiences are the sender's: the worklist slice
declares them in the action of the row that sends the event (§8).

The chart does not declare a box's ON_NOEXEC on the jobs it contains (their noexec on and their
hold off), so that stays old code, as do the box row's sets of members and the window decisions at
a box start, which move no status. What keeps those two flags in step today is the follow before
each dispatch and the realign at commit. The commit's realign also covers the refresh of an
instance's data when its units change, and the holding of a member whose settlement waits behind
its box rules, which the chart settles inside the member's own dispatch.

## 12. Durability of deferred queues

A deferred input is admitted and applied. The apply's effect is the enqueue. There is no new
admission outcome: applied and rejected stay, and the WAL format stays.

A deferred queue is durable state of its tier, or the tier's protocol provably recovers its
loss. Release runs inside the transaction of the input that moves the configuration, each
released item as its own macrostep.

The admission decision is final at the gate: `expect` and the preconditions run there, as today,
and release does not re-check them. That narrows concurrency-model §4 step 6 for a released control
input; the entry that first makes a machine defer a control input amends concurrency-model §4 and
control-protocol.md's meaning of `applied` for that input, and says so. A released event that no
longer fits takes its declared row.

The representation of a deferred queue inside the oracle (its place in the store's inventory and
projection, concurrency-model §3; the seal format), its dry apply, and the handling of a fault at
release under DL-292 and DL-320 are ruled by the entry that first makes an oracle machine defer.
That entry narrows concurrency-model §4, DL-292 and DL-320 and says so. Until then no oracle
machine defers. A fork of the state owner carries its deferred queues into DL-320's dry apply, so
an event the trial releases is released on the twin only (DL-339).

`STATE_MACHINE_VERSION` and the artifact formats are reviewed on every slice that changes a
derivation (the VERSIONS paragraph of DL-320).

## 13. Coverage and the completeness test

Hits are counted for:

- rows;
- choice branches;
- guard outcomes where false is reachable;
- defer and release paths.

All of them are held at 100% in the gate. A row may carry a mark, `spec-only` (a contract rule
with no code yet) or `unreachable`. The gate lists a marked row and never counts it, and a hit on
it or on its guard's outcomes fails the gate, as for a marked table transition (DL-296, DL-333).

The completeness test is generated per machine from the declaration plus a declared finite
abstraction. The abstraction has three parts:

- payload partitions: an exit code is success or not, and a status is one of the injectable
  set;
- configuration witnesses, which are builders, as in DL-293;
- justified exclusions, listed in the declaration.

For each witness configuration, each event and each partition, the test asserts that the executor's
outcome equals the declaration's resolution of that cell (§3, §6, §10): a row id, `ignored`,
deferred, discarded (a stale generation, §7), or a refusal code. It is a conformance test of the
executor against the declaration, and it proves that no cell falls through. It is not an oracle of
what a row should do: a bare `OracleError` never passes, and the SEM trace tests and the
bisimulation stay independent evidence of behaviour. Coverage proves "as specified".

## 14. Generated docs and anchors

`docs/state-machines.md` stays generated.

Rows get stable anchors. The semantics dossier and the runner contracts cite anchors for
transition mechanics. They keep their own text for vendor requirements and cross-machine
invariants.

Guard and action docstrings are the row's prose. A row with no guard or action carries its
prose in its `note`, which the generated table shows in the action column.

## 15. The executor

The executor is built here, not adopted. It is standard library only and one module.
`state_machine.py` grows into it: `src/dsl41/state_machine.py` holds the table core and the
executor. The module imports nothing from its package, so the supervisor tier loads it by path,
with its own directory on `sys.path`, as it loads `canon.py`. It is deterministic and has no
clock. It does no I/O in production; under the test variables it records hits as `take` does
(DL-289).

The prior-art check covered python-statemachine, transitions, sismic, miros and pysm. In those
libraries it found no deferral with override and no standard-library fit. Vocabulary and semantic
tests are borrowed from sismic and python-statemachine v3's SCXML conformance suite as references
only.

## 16. Migration order and acceptance

Pilot: `supervisor_lease` (token guards, monotonic clock, time events) and `subscription`.

Subscription's declared contract is the one its own document states
([docs/blocks/subscription.md](blocks/subscription.md)): a bounded buffer in both phases, not a
deferral. It runs on the engine's executor, one instance per feed from its join to its end, and
a statechart error that a control handler meets stops the engine as one from the loop does.

`seal_boundary` comes third. `seal_boundary` defers nothing: an input after the cutoff refuses
the boundary (`period-model.md` §6, DL-327).

`supervisor_client` (DL-330) runs on the process's one executor, which the wiring builds before
the engine and hands to both; the client posts nothing once closed, and a statechart error that
its reader, renewal or LIST task meets stops the engine as one from the loop does.

`effect` (DL-331) runs on the engine's executor, one instance per open effect from its record to
its outcome; the outbox refuses a second outcome before any post, and a statechart error out of a
record or an outcome stops the engine as one from the loop does, and in replay names the WAL
record.

`host` (DL-333) is the first oracle-tier machine. It runs on the state owner's executor, one
instance per host row; the row's state is the instance's configuration, written from its leaf. The
gate reads the chart on a scratch executor rebuilt from the rows and keeps only the checks that
read the input's own facts. The chart is dispatched on the store's executor only for an applied
decision; the gate's scratch read is separate and moves nothing. A durable `applied` the chart
refuses is a statechart error that names the WAL input.

`admission` (DL-334) runs on the engine's executor, one instance per request id from its durable
attempt to its durable decision; the decided state is final and forgotten, and the stored decision
answers an exact retry, or refuses a reused id, before any post. Replay rebuilds the index in WAL
order on the resuming engine's executor. A statechart error at an attempt, a decision or the seal's
readiness lookup stops the engine as one from the loop does, and in replay names the WAL record.

`runtime_assembly` (DL-335) is the state owner's own life. Its one instance lives on the store's
executor from construction, and the configuration is the assembly phase. An install and a test's
witness rebuild the executor from the phase and the rows; a fork copies it (DL-339). A verb the
phase does not allow is a refusal row with a code, read before any work of the verb; the verb raises
the class and text it always raised. A cell with no row is a statechart error that stops the tier at
the verb that meets it; the constructor meets the genesis cells.

`anchor_head` and `period_row` (DL-336) run on an executor per move, built from `anchor.json` read
under the anchor lock; the head's tag and each row's flags are the instances' configurations, and
no file or no row means no instance. A refusal row is the anchor method's `EngineError`, with its
text, before any write; the checks that read the method's own facts and the idempotent repeats
come before the post. A gap is a statechart error, also before any write: at the seal's close CAS
it stops the engine with the `seal` record durable and the head open, and at resume, at genesis,
in the roll and in `estate reclaim` it reaches the CLI's stop, exit 7.

Acceptance per machine:

- the old decision code is deleted;
- `well_formed` and §10 pass;
- the generated completeness test exists;
- the coverage gate passes;
- existing tests pass unchanged;
- protocol and output are unchanged, or the entry says what changes and why;
- for oracle-tier machines also: fork isolation, replay, seal and reopen, and per-input
  revision equality against the old implementation.

After the pilot, `seal_boundary` (DL-327), `supervisor_process` (DL-328), `supervisor_client`
(DL-330), `effect` (DL-331), `host` (DL-333), `admission` (DL-334), `runtime_assembly` (DL-335),
and `anchor_head` and `period_row` (DL-336); the rest is the oracle.

The oracle comes last. A second oracle is built beside the old one under a new old-vs-new
harness. The same inputs go to both. The harness compares the trace, the emitted events, the
store rows, the revisions per input and the seal digests, on the corpus and on generated
estates. A divergence is a bug or a fidelity question. The harness is `tests/oracle_lockstep.py`,
proven old-vs-old before the second oracle exists (DL-338). Then the old oracle is deleted. Before
the second oracle moves anything, the composite `job` chart is declared and checked against the
old tables (DL-337). The second oracle, `ChartOracle` in `src/dsl41/oracle_chart.py`, subclasses
the old one and overrides one input family's handlers per entry, STATUS first (DL-341), then the
start attempt (DL-342), then KILLJOB and the TIMER input (DL-343), then the flag inputs
(DL-344), then RELEASE_RESOURCE and the readmission (DL-345), then the box rules (DL-346);
the families not yet converted, and the cascade's order, run the old code in its order.

Each slice is a semantic-class change. Each has its own decision-log entry.

## 17. Names

- "Deferred event" is the UML term, used for the executor.
- A transient state is left by a completion transition.
- A wait state awaits a reply or a time event (§9). Deferral is optional.
- SEM-33's `run_window` deferral is a "window-postponed start" in new prose. Its identifiers,
  its cause strings and the `RUN_WINDOW_DEFER` trace line stay, because they are the trace
  contract.
