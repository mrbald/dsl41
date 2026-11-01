# Review a candidate

For the reviewer and maintainers arranging acceptance.
Read the [shared contract](../../CLAUDE.md) and the task contracts.
The boss chooses bindings. The operator dispatches the assigned reviewers.

## Your task

- Review the exact candidate named by commit or file hash. Start the first pass
  with fresh context; keep findings context for confirmation rounds.
- Read the diff against the definition of done and the relevant contracts.
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

Run an adversarial review before committing a change in one of these
classes (DL-262, DL-395). The operator supplies the reviewer assignment.
An additional pass uses a reviewer-grade model from the other vendor than the primary reviewer,
bound by the boss and subject to the fallback below.

- Semantic class, at any size: one reviewer from a different vendor than the
  implementer, subject to the fallback below, and an additional pass when the
  boss asks. It covers a change to what a compiled or run estate does: lowering,
  the oracle, the runner, the scheduler, or calendars. It also covers any version
  constant, any rule `docs/` freezes, and instruction policy. The frozen rules are the runner
  contracts listed in the [shared contract](../../CLAUDE.md#read-for-the-task)
  and the rules DL-225 names.
- Any other multi-file change: one reviewer. Examples are a refactor that
  keeps behavior, tests, docs, tooling, and the browser or TUI front ends.
- In addition, slices built apart and integrated together get one additional
  pass over what the integration changed. Each slice keeps its own review.

The operator applies the assigned stand-in when a reviewer is unavailable.
For quota fallback, the vendor's quota must be exhausted and the work unable to
wait for reset. Use a fresh reviewer from the other vendor. Semantic changes
require two independent reviewers when acceptance uses only the implementer's
vendor. Record the unavailability reason and stand-in in the PR or review record.
The stand-in also confirms that reviewer's fixes. If no qualified reviewer is
available, acceptance waits.

Follow [Review rounds](#review-rounds) before review or
rework: per-reviewer caps, owner escalation, the shape note, fresh final review,
and the stated-limit rules (DL-430). Each ruling names the finding's raiser.

Look specifically for: over-claiming or leaking tests, duplicate records,
orphaned tests from bad inserts, clock-domain mixups, and flaky timing
assertions -- these are recurring defect classes in this codebase.

## Review rounds

Track these counts in the change's review record (DL-430):

- A review round is one reviewer's pass over the change or fixes to its findings.
- A rework round is the implementer's pass over every open finding, whoever raised it.
- A fix is found wrong when the raiser or its named stand-in reopens the finding.

Each reviewer has three review rounds. Only the raiser or its named stand-in
confirms a fix. A material finding still open after that reviewer's third round
goes to the owner, who approves further rounds one at a time.

Before the fourth rework round, or when one finding's fix has been found wrong
twice, the boss writes a shape note. It states the rule the change implements,
why cases keep appearing, and the single rule, split or redesign it suggests.
The owner reads it before rework starts. The change leaves the merge train
while it waits.

After more than three rework rounds, the final diff gets a fresh review unless
every reviewer's last round covered the current fixes. Use a new reviewer from
the vendor whose last round is older, with its own three-round cap. Apply the
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
