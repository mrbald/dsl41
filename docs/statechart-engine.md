# Statechart engine — the frozen contract

Status: frozen (DL-324). Each change to a frozen item needs a decision-log entry.

This document fixes the executor semantics that every declared runner state machine runs under.
Each machine declares its transitions, and the declaration is the only spec (DL-289, DL-324).
This document says how the executor reads it. Cross-machine invariants and vendor requirements
stay in the other normative documents (§1).

`docs/state-machines.md` is generated from the declarations. It lists the rows. This document
says how a row is chosen and run.

Every machine in `machines.MACHINES` is a statechart on this executor (§16).

## 1. Principle

Every runner state machine is a declared, executable statechart. The declaration picks the
transition. Its guard and its action are named callables beside the row. No other code decides
a move.

No machine keeps decision code or a transition table beside its declaration (DL-352, DL-353,
DL-356).

Docs are generated from the declaration. Semantics prose cites row anchors (§14).

Cross-machine invariants and vendor requirements stay in the normative documents (§14). "One
revision per committed input" is a transaction guarantee, not a row.

The executor raises hard errors, in production too, among them `AmbiguousTransition` (§3),
`ReentrantDispatch` (§5) and `UnhandledEvent` (§10). Each is a bug that would corrupt state, not
a check to note. No machine reports a broken transition: DL-292's violation channel is retired
(DL-353), and `dsl41 run --on-transition-violation` is accepted and inert until the next major
release.

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
`chart_warnings` warns where that can happen. It also warns where two regions declare rows for
one event of which one leaves the shared ancestor. A row of a state that several regions share
(their common ancestor) is not a candidate when any region below it ended its walk in a deferral
of the event: the inner deferral wins over the outer row, as within one region. A sibling
region's own row still consumes the event.

The firing set is final before any action runs. It executes in region declaration order.

The outcome for the whole instance is decided from all its regions together. If any region
produced a candidate, including an internal `ignored` row, the event is consumed and the firing
set runs. An `ignored` row is a candidate only when no other region produced a candidate that is
not `ignored`; otherwise it yields and writes no trace line. A refusal row beside another region's
candidate raises `AmbiguousTransition`; `chart_warnings` warns where a refusal row and another
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

A completion item carries the payload of the microstep that completed its state: for a simple
state, the event that entered it, so its data is the data of the event that put it there; for a
composite, the event that put its last region at rest. The completion's guards and actions read
that payload. The item and its context (`Context.completing`) name the completing state.

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
   it; a completion item too, with the payload of that microstep's event (§4). When the
   worklist is empty, the deferred release runs (§6); then the next root item from the pool.
4. A dispatch while a macrostep is in flight raises `ReentrantDispatch`, in production too.
   Tests assert that the chain works and that the re-entrant call raises.
5. Guards are pure. Actions change machine state and report intents. Each tier's durable
   protocol carries the intents out:
   - the oracle's effects go through the outbox, after DL-320's dry apply on `Oracle.fork()`;
   - the supervisor's go through its index, receipt, spawn and reply protocol;
   - the engine's go through the WAL decision.

   The executor itself does no I/O. A tier whose later items read what an earlier item's intents
   write passes `run` a hook. The executor calls it with each outcome as the microstep ends, before
   the next item pops, under the macrostep: it may read, and a post, dispatch, resolve or fork from
   it raises `ReentrantDispatch`; it reports no item. The oracle's hook writes the store the fired
   rows' moves, since the guards of the items a row chains read the store; the engine and the
   supervisor carry their intents out after the run.

A tier may read how an event would be selected without running it. `Executor.resolve` runs the
selection of §3 for one instance and one event against the instance's present configuration:
guards are evaluated, no action runs, nothing changes, and no hit is recorded. It answers neither
the release of deferred events nor the pool ahead of the event, so it refuses an instance with a
deferred event, and a full deferred queue is outside it. Inside a macrostep it raises
`ReentrantDispatch` (DL-335).

A tier may copy an idle executor. `Executor.fork` returns a new executor that holds every instance
as it stands: its configuration, its deferred queue, its generations and its entry serials, and the
history rings. The two share each instance's record until a dispatch on either side writes it. A
write stores a new record on its own side, so neither side sees the other's moves. A fork costs one
reference copy per instance, a copy of each history ring, and one new record per instance the tier
gives new data; the state owner gives every host instance new data, bound to the fork's rows, so the
rest of its fork's cost is one record per host. Data not given a new value, and every event payload,
stay shared by design, so a tier treats them as immutable. A fork inside a macrostep raises
`ReentrantDispatch`. A fork with a root item in the pool raises a `StatechartError`, because that
item would run on one side only (DL-339).

The executor's errors, among them `AmbiguousTransition` (§3), `ReentrantDispatch` (step 4) and
`UnhandledEvent` (§10), subclass one base, `StatechartError`. Each is an assertion failure, not a
fault to contain. DL-320's classification never catches it: `trial_apply` and `classify_fault`
re-raise it, and nothing is appended. No tier may swallow it. The tier stops before any further
durable write of its own; a record already durable, a seal record among them, stays.

The error is structured. It carries the machine, the instance key, the active configuration, the
item and a summary of its payload, the item's ancestry, the candidates found, the deferred queue,
and a ring of the last microsteps of the machine that raised. The executor keeps one ring per
machine. Its size is the executor's `history` argument; the default is 64. `str()` of the error
renders all of it, one fact per line. The engine and the supervisor print it on stderr, each line
under the prefix `dsl41: statechart error`, and exit 7. The units list 7 in
`RestartPreventExitStatus`.

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
declaration, in the entry that converted it (§16).

## 8. Chaining and continuations

An action never calls the executor. It returns the events it raises, to any instance of the same
executor, and the continuation it requests. An event for another tier is an intent (§5 step 5),
carried by that tier's protocol. The executor queues them (§5 step 3).

A step that must continue after the items it raises have run is a transient state whose completion
transition is the continuation. Its guard reads the state after the chained items ran, because the
completion is queued behind them.

A machine that bounds re-triggering declares the bound as a row guarded on an item's ancestry depth,
or on the count of one step in it, which a guard reads in constant time (`Context.count`). Items of
one macrostep that are not ancestors of each other are never counted against each other; only a
chain is.

The oracle's cascade maps onto these rules as follows (DL-350). Each input is a root item on its
job's instance. Each step that runs after an earlier step's whole cascade is a chained self-event,
a moment, whose payload carries the step the oracle runs when it pops: `box rules`, `settle`,
`wake`, `walk up`, `holders may leave`, `scan`, `load taken`, `box ends step`, `notify step`,
`window pass`, `after inject`, `after leave` and `retry step`. Each is an internal row of the
lifecycle that moves nothing. A loop whose gate reads state that an earlier step's cascade can
change is a moment that sends its next item and then itself again; the gate is re-read at each pop.
A row whose own write must follow its chained items targets a transient state. An admitted start
enters `ADMITTED` from its `start admitted`, and a readmission from its own row. The state's entry
action sends the units, the run's start and a box's resets; its completion moves the job to
STARTING after their cascades and sends the rest of the start (DL-364). The per-job start bound
(DL-304) is `job.100`, guarded on the count of the job's own `start admitted` items in the item's
ancestry. The completion descends from `start admitted`, so every later step of the start counts
it. A readmission enters `ADMITTED` without `start admitted`, so the bound does not count it. The
cascade takes no Python frame per nested start, so it fits no recursion
limit to the job count: only a fact's own recursion, such as a deep condition, needs the stack, and
each construction raises the process's limit to a fixed 3,000, so the deepest condition the
lowering accepts evaluates. A cascade that does not end takes items instead. The oracle's hook
counts a root dispatch's items, and the item that reaches the cap for the estate's job count, four
times its square, at least 100,000 and at most 400,000, raises `CascadeDepthError`, an
`OracleError`; the input is refused or quarantined as any input whose apply raises (DL-351). The
ceiling keeps a cascade that does not end within half the engine's lease lapse at every measured
size. `classify._TruthOracle` builds only the oracle's reads (`OracleReads`), to read conditions.

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
`StatechartError`, it stops the tier (§5).

An ignored event is declared once, on a superstate. DL-293's rule of one ignore row per state
it occurs in does not apply.

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

The oracle runs one composite `job` machine per job, with named regions for its status, its flags
and its holding, its box variant as rows and its writes through `RuntimeState` (§16).

DL-337 declares that machine in `src/dsl41/job_chart.py`: one chart `job` with the regions
`status`, `flags` and `holding`, the box as rows guarded by the job type, and ids whose hundred
names the region: `job.1NN` and `job.4NN` status, `job.2NN` flags, `job.3NN` holding. It is
registered in `machines.MACHINES`, and is the one declaration of a job's moves (DL-353).

The oracle, `Oracle` in `src/dsl41/oracle.py`, drives it (DL-341). Each job row has one `job`
instance on the state owner's executor under the row's key; the owner restores them from the rows
where it restores the host instances, and a fork copies them. Every input that moves a job row is a
root item on the job's instance, and its whole cascade runs on the executor's worklist (§8, DL-350).
The chart decides every move. A row's action takes the items it sends beyond its own move from the
tier's handle on its payload (`Units.consequences`), as a guard takes its facts: the chart declares
which rows send and when, and the oracle computes each send list and its audience as the row runs.
The chart has no send list of its own: such a row that meets a payload with no handle is a
statechart error (DL-356). The oracle's hook (§5 step 5) writes what each microstep's fired rows
reached, with the store's verbs, in the order the rows fired (`oracle_writers`). The status written
is the one the chart reached; the golden digests judge a wrong row (§16). A fired row the hook has
no write for is a statechart error. Two checks are statechart errors: the job type behind a row that
takes a job's or a box's path, checked before the write; and, at each macrostep's end and at the
input's commit, every job the input reached has its row project to its instance's configuration. The
store's verb decides a settlement's holding move from the units; a settlement row that holds another
state than the verb wrote fails the second check. No code outside the chart moves a job row, so no
instance is brought back to its row.

Every attempt to start a job is one `start` on its instance (DL-342): a wake, a box start and a
retry send it, and STARTJOB, FORCE_STARTJOB and the window timer's replay relay it, through
`job.177` to `job.179`, FORCE_STARTJOB after its clears. Its payload computes each oracle fact on
the guards' first read, in the order `start_verdict` tests them, so an attempt computes only the
facts its verdict reads. The per-job start bound is the internal row `job.100`, guarded on the
job's own `start admitted` items in the attempt's ancestry (§8); it refuses the start with a
START_REFUSED line. An attempt whose run's settlement is owed,
because the box rules held it back, takes the internal row `job.401` past the bound (DL-340): its
action sends the ended run's `leave STARTING or RUNNING`, then the same attempt again, which reads
the admission the settlement left (DL-349). An attempt whose window or leave already decided it
carries that decision, and is not decided again. Each admitting row (`job.101` to `job.104`,
`job.124`) sends `start admitted`; the admission's and the start on held units' rows (`job.102`,
`job.104`, `job.124`) are internal. Each row of `start admitted` is guarded on one verdict its
payload carries. The rows for an admission (`job.402`) and for a start on held units (`job.426`)
enter the transient state `ADMITTED`. Its entry action sends the units the start reserves or
takes over, the run's start, which consumes the arm, and a box's resets. Its completion carries
the payload of `start admitted` (§4) and is guarded on its kind: `job.427` moves the job to
STARTING and sends the move's box rules, settlement and wakes, a box's reset notices, then
`started`, the RUNNING move, guarded on the run it carries, a freed excess's scan and the load
mark. The bypass (`job.101`) and the queue (`job.103`) are status moves of their own; the rows of
`start admitted` for them (`job.424`, `job.425`) send the bypass's start and move and the queue's
move. Those moves are the bound's descendants. A readmission (`job.108`) enters `ADMITTED` itself,
without `start admitted`: it is not a start the bound counts. Its completion (`job.428`) carries
the readmission's payload and sends the same move and `started`. An input that raises commits
none of its attempt half's writes: a control input that raises is refused and commits nothing, and
an engine-made one is quarantined with its rejection logged and its time half committed; the engine
stops only when the time half itself raises. The golden digests replay such an input the same way:
put back, then its time half applied alone (`tests/oracle_goldens.py`). No input of the goldens'
sample raises (DL-348).

KILLJOB and the TIMER input are dispatched to the job's instance too (DL-343), and the input passes
its own relay to the attempt, so no cause text is read. A live job's kill (`job.114`) and a
term_run_time on the running run it was armed for (`job.115`) terminate the job; a box's terminator
cascade is the chart's `box ends step`. A waiter's kill drops its arm (`job.217`, `job.226`) and
the flag move terminates it (`job.111`); a kill on any other status is ignored with a trace line,
and a stale term_run_time writes nothing (`job.184`). The deadline alarms move no state and stay
code. A window timer reads its own facts before any gate: whether the postponed start still belongs
to its box run, and whether a scan continuation is superseded. They are real facts of its relayed
`start`: a stale one is refused with its refusal line, a live continuation resumes the scan, and
any other is the attempt. Timers stay armed by code; the chart declares moves, not timer writes.

The flag inputs are dispatched to the job's instance too (DL-344). The fired rows decide each
dimension a rule reads from the row or the catalog. Whether the flag was already set is the row's
source state. An input the job ignores takes one of the status region's internal rows `job.148`
to `job.158` and writes the EVENT_IGNORED line with the rule's reason (`_oob_ignored`); a row the
chart ignores where no rule gives a reason is a statechart error. A flag row is written by the flag
move, with its marker and line. A status that the `flag moved` fires is a status move: an iced
waiter leaves the queue (`job.112`), and ON_NOEXEC settles a waiter, a FAILED job or a box
(`job.113`, `job.121`, `job.136`, `job.169`, `job.170`). ON_NOEXEC's payload reads a box's
contained jobs only where the rule reads them. The flag rows' actions send or report what follows,
in order: an ice's box rules and wakes (`holders may leave`, `wake`), the mark of a member taken
off ice in its running box, the waiters' scan on a hold, and the start retries of OFF_HOLD and
ON_NOEXEC (`retry step`, each one `start`). A box's ON_NOEXEC and OFF_NOEXEC on the jobs it
contains are box rules (below).

RELEASE_RESOURCE and each waiter's readmission are dispatched to the job's instance too (DL-345).
Held units go back with their line and the queue is scanned (`job.307`); a job that holds nothing
(`job.315`), or a live run whose units go back when it ends (`job.316`), takes the no-effect line.
Whether the job holds units it can give back is the row's. The queue's scan is the `scan` moment.
Its pass bookkeeping and its order stay oracle code: the chart has one instance per job, and the
waiters' order (rank, then the queue's order) is the store's. A pass reads its waiters when it
starts and each waiter's status at its turn; it ends at the first `readmit` whose row reports that
it admitted or cancelled its waiter, which starts the next pass. A scan asked inside a pass sends
nothing; an admit-only one asks the pass for a full pass next. The scan an admission owes the input
(DL-255) is a root item after the input's macrosteps, marked by `load taken` behind a freed
excess's scan. Each waiter the scan reaches gets one `readmit`, whose payload computes each fact
where the scan's rule reads it. An admission (`job.108`) sends the start's extent; a member of a
box that stopped running is cancelled (`job.109`); a waiter that fails the queued recheck drops its
arm (`job.216`, or `job.230` with none) and its `flag moved` leaves the queue (`job.110`), with the
window or day disposition after it in `after leave`; any other waiter stays queued (`job.181`).

The box rules are dispatched to the instance each one moves (DL-346). A status move's row sends, in
order, its `box rules` (the moment that reaches the job's box and the boxes above), its `settle`
and its `wake` (the referencers' starts, then a scan where the move freed units or lifts a block).
A member that ends FAILURE or TERMINATED meets the box's terminator (`job.130`). A RUNNING box's
completion door reads its overrides (`member moves`), then the default fold (`completion moment`),
sent on every move of a member of a RUNNING box and after the overrides only when none fired. An
idle box re-derives its status (`box recompute`). The walk up to the boxes above (`walk up`) reads
their overrides through `member moves`, and an ice that takes a member out of its box's run is a
completion moment there (`holders may leave`). A box's start, run and end reach its members: the
reset (`box start`), auto_hold (`box running`), the end of the run's arms (`box run ends`), the
job_terminators (`box ends FAILURE or TERMINATED`, one member per `box ends step`), the batch
INACTIVE cascade (`box set INACTIVE`, then `notify step` per row), ON_NOEXEC's flag and hold
(`ON_NOEXEC on its box`, DL-347), and OFF_NOEXEC's clear (`OFF_NOEXEC on its box`). The window
decisions at a box start and the completion of a box run that left no member in the run are the
`window pass`: a box started inside another's pass registers with it, and the outermost pass
decides them all. The parent's recompute after an operator's INACTIVE is `after inject`. The
period's first input gives a removed job's held units back (`job.308`). Each payload computes its
facts where the rule reads them. A box rule's target is the chart's choice.

The chart decides each box rule's row; the oracle decides when a rule is sent, and to which
instances. `box running`, `box run ends` and `box ends FAILURE or TERMINATED` reach the box's direct
members; `box start`, `box set INACTIVE`, `ON_NOEXEC on its box` and `OFF_NOEXEC on its box` reach
every job it contains. Six gates choose an audience the chart does not see: `box start` skips
a live subbox's subtree (`_contained(skip_live=True)`, which also skips a live or queued job);
`member moves` is sent by the completion door on the moves it sees, not on a non-terminal move (a
terminal, RUNNING or resolving move only); `box recompute` is sent by its callers' gates, run
numbers and the cascaded-subbox case, LIVE and TERMINATED excluded (a member's terminal move, or the
direct target of an operator's INACTIVE, never a row the cascade took INACTIVE); the walk up skips
an ancestor whose run number moved; the parent recompute after an operator's INACTIVE needs the
parent's run number unchanged; and `box ends FAILURE or TERMINATED` goes only to the members of the
run that ended, while the box still stands at that end. Its sender reads that run, a kill before
the status write and a fold or an override with the FAILURE or TERMINATED it reads after the end's
wakes, and `box ends step` compares the box's run number and its status before each member's send:
once the wakes, or an earlier member's kill, started the box again, or an ancestor's restart reset
the box to INACTIVE without moving its run (SEM-10), no further member gets the event, not even one
carried RUNNING from the run that ended (DL-348, DL-360). A fold or an override may read a
later run's own end, whose cascade already ran; the members it reaches again ignore the event. These audiences are the sender's: each
gate is read in the action of the row that sends the event, or in its iterating moment (§8), never
as a fact of the recipient's row.

A box's ON_NOEXEC reaches each job it contains as `ON_NOEXEC on its box`: the flag on, from either
value, then the hold it supersedes off, with their lines. Whether the hold goes is the row's
choice. The jobs it releases retry their start (DL-347).

An instance's data is its catalog entry's, and changes only when a catalog is installed. What
leaving a run does with its units is a fact of `leave STARTING or RUNNING`'s payload: the action
that sends the settlement builds it with the handle on the job's units that its own payload carries,
and the holding rows read the policy when the settlement pops, from the units the row holds then
(DL-349). A move outside the batch INACTIVE path settles at its own `settle`, after its box rules.
The job is still live when that settlement pops if the move entered STARTING or RUNNING, or if the
job started again inside its move's cascade (DL-340); nothing settles then, and a rank the move left
still drops (`job.418`).

## 12. Durability of deferred queues

A deferred input is admitted and applied. The apply's effect is the enqueue. Deferral adds no
admission outcome and no WAL format change: the outcomes are applied and rejected.

A deferred queue is durable state of its tier, or the tier's protocol provably recovers its
loss. Release runs inside the transaction of the input that moves the configuration, each
released item as its own macrostep.

The admission decision is final at the gate: `expect` and the preconditions run there, and
release does not re-check them. That narrows concurrency-model §4 step 6 for a released control
input; the entry that first makes a machine defer a control input amends concurrency-model §4 and
control-protocol.md's meaning of `applied` for that input, and says so. A released event that no
longer fits takes its declared row.

The representation of a deferred queue inside the oracle (its place in the store's inventory and
projection, concurrency-model §3; the seal format), its dry apply, and the handling of a fault at
release under DL-292 and DL-320 are ruled by the entry that first makes an oracle machine defer.
That entry narrows concurrency-model §4, DL-292 and DL-320 and says so. DL-292's violation channel
is retired (DL-353). Until that entry, no oracle machine defers. A fork of the state owner carries
its deferred queues into DL-320's dry apply, so an event the trial releases is released on the twin
only (DL-339).

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
it or on its guard's true outcome fails the gate (DL-296, DL-333). Its guard's false outcome is
the cell another row takes: neither required nor forbidden (DL-353).

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
`src/dsl41/state_machine.py` holds the declarations, the executor and its hit recording
(DL-356). The module imports nothing from its package, so the supervisor tier loads it by
path, with its own directory on `sys.path`, as it loads `canon.py`. It is deterministic and has
no clock. It does no I/O in production; under the test variable `DSL41_TRANSITION_HITS` it
records the coverage items it hits (§13).

The prior-art check covered python-statemachine, transitions, sismic, miros and pysm. In those
libraries it found no deferral with override and no standard-library fit. Vocabulary and semantic
tests are borrowed from sismic and python-statemachine v3's SCXML conformance suite as references
only.

## 16. Machines and acceptance

Each machine has its decision-log entry: `supervisor_lease` (DL-326), `seal_boundary` (DL-327),
`supervisor_process` (DL-328), `subscription` (DL-329), `supervisor_client` (DL-330), `effect`
(DL-331), `host` (DL-333), `admission` (DL-334), `runtime_assembly` (DL-335), `anchor_head` and
`period_row` (DL-336), and the oracle's `job` (DL-337).

`supervisor_lease` has token guards, a monotonic clock and time events.

Subscription's declared contract is the one its own document states
([docs/blocks/subscription.md](blocks/subscription.md)): a bounded buffer in both phases, not a
deferral. It runs on the engine's executor, one instance per feed from its join to its end, and
a statechart error that a control handler meets stops the engine as one from the loop does.

`seal_boundary` defers nothing: an input after the cutoff refuses the boundary (`period-model.md`
§6, DL-327).

`supervisor_client` (DL-330) runs on the process's one executor, which the wiring builds before
the engine and hands to both; the client posts nothing once closed, and a statechart error that
its reader, renewal or LIST task meets stops the engine as one from the loop does.

`effect` (DL-331) runs on the engine's executor, one instance per open effect from its record to
its outcome; the outbox refuses a second outcome before any post, and a statechart error out of a
record or an outcome stops the engine as one from the loop does, and in replay names the WAL
record.

`host` (DL-333) is an oracle-tier machine. It runs on the state owner's executor, one
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
the class and text it raised before its conversion, an `OracleError` or a `ValueError` (DL-335). A
cell with no row is a statechart error that stops the tier at the verb that meets it; the
constructor meets the genesis cells.

`anchor_head` and `period_row` (DL-336) run on an executor per move, built from `anchor.json` read
under the anchor lock; the head's tag and each row's flags are the instances' configurations, and
no file or no row means no instance. A refusal row is the anchor method's `EngineError`, with its
text, before any write; the checks that read the method's own facts and the idempotent repeats
come before the post. A gap is a statechart error, also before any write: at the seal's close CAS
it stops the engine with the `seal` record durable and the head open, and at resume, at genesis,
in the roll and in `estate reclaim` it reaches the CLI's stop, exit 7.

A machine is accepted when:

- the old decision code is deleted;
- `well_formed` and §10 pass;
- the generated completeness test exists;
- the coverage gate passes;
- existing tests pass unchanged;
- protocol and output are unchanged, or the entry says what changes and why;
- for oracle-tier machines also: fork isolation, replay, seal and reopen, and per-input
  revision equality against the old implementation.

Each slice is a semantic-class change. Each has its own decision-log entry.

The oracle's composite `job` chart (DL-337) drives the job rows. Its families each have an entry:
STATUS (DL-341), the start attempt (DL-342), KILLJOB and the TIMER input (DL-343), the flag inputs
(DL-344), RELEASE_RESOURCE and the readmission (DL-345), the box rules (DL-346), a box's ON_NOEXEC
on the jobs it contains (DL-347), and the cascade itself, on the executor's worklist (DL-350).
`Oracle` in `src/dsl41/oracle.py` is the one oracle, built in layers, one module each:
`oracle_reads.py` (the store, the trace and the reads), `oracle_schedule.py` (the clock's reads and
the timers), `oracle_cascade.py` (the cascade's moments), `oracle_writers.py` (each fired row's
writes) and `oracle.py` (the inputs, the hook and the checks), with the payloads in
`oracle_facts.py` and `oracle_intents.py`. The engine, every replay, preflight and equiv build it
(DL-351). The golden digests (`tests/oracle_goldens.py`, over the inputs in
`tests/oracle_inputs.py`) hold the answers of the decision code the chart replaced, over the sample
of the old-versus-new harness (DL-338). The code and the harness are deleted (DL-352). The oracle
reproduces the digests; a golden that changes is a DL-235 decision. The job has no transition table,
the store checks no write against one, and the chart is registered (DL-353). Each row that carries
the `unreachable` mark (§13) states its reason, and `docs/state-machines.md` lists them all. Among
them are job.312, since the admission the oracle builds carries no previous end, and job.314, since
a live status never stands on held units (DL-353).

## 17. Names

- "Deferred event" is the UML term, used for the executor.
- A transient state is left by its completion transition, whose payload is the completing
  microstep's (§4, §8).
- A wait state awaits a reply or a time event (§9). Deferral is optional.
- SEM-33's `run_window` deferral is a "window-postponed start" in new prose. Its identifiers,
  its cause strings and the `RUN_WINDOW_DEFER` trace line stay, because they are the trace
  contract.
