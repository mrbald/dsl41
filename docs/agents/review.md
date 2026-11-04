# Review a candidate

For the reviewer and maintainers arranging acceptance.
Follow the maintainer track under [Documents](../../README.md#documents).
Read the [shared contract](../../CLAUDE.md) and the task contracts.
The boss chooses bindings. The operator dispatches the assigned reviewers.
Read the [approved operating model](operating-model.md) for authority, role limits and owner escalations.

## Your task

- Review the exact candidate named by commit or file hash. Start the first pass
  with fresh context; keep findings context for confirmation rounds.
- Read the diff against the definition of done and the relevant contracts.
  Read every cited decision whole, from its `- DL-` line to the next entry.
  Check its closing limits and rejected options before calling a rule missing.
  Verify load-bearing external citations against their sources.
  Check the implementer's evidence and its provenance. Use permitted targeted probes;
  request any needed probe your tools cannot run. Run the full suite only when the brief asks.
- Keep the candidate read-only. Write findings to the assigned scratch file if permitted;
  otherwise return the full findings for the operator to save.
  Name the failure scenario, file and line, and an acceptance check for each finding.
- Return `accepted`, `rework`, or `blocked`, with a ranked findings table and blocker detail.
  Send the verdict to the operator. A failed invocation or timeout is not a verdict.
- Confirm fixes to your own findings. Do not close another reviewer's finding
  unless you are its named stand-in. Follow the round limits below.

## Required passes

Arrange independent review before committing every change (DL-440, rules 9
and 19). The operator supplies the reviewer assignments. Use the
[coordination table](coordination.md#operating-model) for the primary binding;
obtain any additional reviewer's binding from the boss.
Apply these passes (DL-262, DL-395):

- Semantic class, at any size: the primary reviewer and one additional reviewer
  from the other vendor. Both passes are required, whoever implemented the
  change, subject to the fallback below. It covers a change to what a compiled
  or run estate does: lowering, the oracle, the runner, the scheduler, or
  calendars. It also covers any version constant, any rule `docs/` freezes,
  and instruction policy. The frozen rules are the runner
  contracts listed in the [shared contract](../../CLAUDE.md#read-for-the-task)
  and the rules DL-225 names.
- Every other change, including a single-file change: one primary reviewer.
  Examples are a refactor that keeps behavior, tests, docs, tooling, and the
  browser or TUI front ends.
- In addition, assign the integration reviewer one pass over what integrating
  separately built slices changed. Each slice keeps its own review.

The operator applies the assigned stand-in when a reviewer is unavailable.
For quota fallback, the vendor's quota must be exhausted and the work unable to
wait for reset. Use a fresh reviewer from the other vendor. For semantic work,
keep two independent reviewers when only one vendor is available, including
when it is the implementer's vendor. Record the unavailability reason and
stand-in in the PR or review record.
The stand-in also confirms that reviewer's fixes. If no qualified reviewer is
available, acceptance waits.

Follow [Review rounds](#review-rounds) before review or
rework: per-reviewer caps, owner escalation, the shape note, fresh final review,
and the stated-limit rules (DL-430). Each ruling names the finding's raiser.

Look specifically for: over-claiming or leaking tests, duplicate records,
orphaned tests from bad inserts, clock-domain mixups, and flaky timing
assertions -- these are recurring defect classes in this codebase.

## Review rounds

Track these counts in the change's review record (DL-430, DL-457):

- A review round is one reviewer's pass over the change, fixes to its findings,
  or a repair plan.
- A change is handed in when it is first submitted for review, integration or
  qualification, even with failing gates.
- A rework round is the implementer's pass, after the change is first handed in,
  over every open finding or failed required gate, whoever raised it.
- A fix is found wrong when the raiser or its named stand-in reopens the finding,
  or the same gate fails again on the same test, statement or transition.

Each reviewer has three review rounds. Only the raiser or its named stand-in
confirms a fix. A material finding still open after that reviewer's third round
goes to the owner, who approves further rounds one at a time.

When a required gate fails after hand-in, the change's assigned reviewer checks
the repair plan before the repair pass, unless the exact, non-semantic edit is
already specified by a reviewer, a ruling or a deterministic tool diagnostic;
record that source. A coverage report that lists missing lines does not specify
their repair. The plan states what will change and why that closes the gate
rather than an exclusion, a ruling or a scope question for the owner. A rejected
plan goes to the boss (DL-457).

For a release item, the primary reviewer checks the implementer's plan
before the build. The plan covers only the item the brief quotes and states
what changes where, the tests, and any contract or format change. The plan
review is a review round; a rejected plan goes to the boss (DL-458).

Before the fourth rework round, or when the fix for one finding or failed-gate
item has been found wrong twice, the boss writes a shape note. It states the rule the change implements,
why cases keep appearing, and the single rule, split or redesign it suggests.
The owner reads it before rework starts. The change leaves the merge train
while it waits.

After more than three rework rounds, the final diff gets a fresh review unless
every reviewer's last round covered the current fixes. Use a new reviewer from
the vendor whose last round is older, with its own three-round cap. For this
exemption and the vendor choice, count only reviews of the change's diff, not
plan-only reviews. Apply the
[stand-in rule](#required-passes) if that vendor is out of budget. A material finding
from this review gets one more owner-approved rework round.

From the raiser's second review round, a finding may become a stated limit
only if all these conditions hold:

- It requires a deliberately hostile operator or an unsupported operation,
  such as a process stopped or a supervisor started by hand.
- It cannot corrupt durable state or cause an external effect: a launch,
  a kill, or an emitted artifact.
- The raiser agrees. The change's decision-log entry, or a new entry, records it.

Each ruling names the finding's raiser.
Send semantic disagreements and hard rulings to the boss through the operator;
the boss may consult the advisor (DL-440). Follow
[coordination](coordination.md#bring-an-item-to-the-owner) for owner hand-offs.
