# Deployment qualification

The acceptance target is offline manufacture and safe operation of the
installed release. A green source-tree suite does not qualify a deployment.
Keep the exact wheel, bundle manifest, RPM, commands, exit codes and logs
with each rehearsal result. The tests use synthetic Nightbank `small` and
`bank`; no client inputs belong in these artifacts.

## Operator-error matrix

Every refusal requires reading its cause before another attempt. UNKNOWN
requires evidence of the first attempt's outcome. It never authorizes a
fresh mutation id, signal fallback or unattended retry.

| Case | Prevention or detectable refusal | Recovery and observable postcondition |
| --- | --- | --- |
| Wrong Python, architecture or incompatible wheel | Bundle target check and binary-only pip admission | Obtain the matching kit. No completion receipt or activation. |
| Missing or corrupt wheel, requirements or template | Verify the manifest and hashes before manufacture | Restore the exact delivery inputs. Retained candidates are unchanged. |
| Pip failure, full disk or interruption | Receipt follows installed-code verification | Keep the incomplete directory for diagnosis. It refuses reuse; remove only the proved inactive failed candidate under the preparation lock. |
| Two preparations at one prefix | Prefix lock serializes them | Identical preparation verifies one candidate; conflicting bytes refuse. |
| Reinstall or downgrade | Candidate identity binds content and profile | Verify the requested candidate; active link and active site files do not change. |
| RPM install or upgrade scriptlet failure | Package presence is not readiness | Inspect `%post` status and candidate receipt. Prior runtime still executes. |
| RPM erasure | Package owns delivery files only | Active link, manufactured venv and staged kit remain usable. |
| Installation into the active venv | Prepare only final versioned backing paths | Refuse conflicts. Never relocate or modify a retained candidate. |
| Activation before compatibility review | Explicit activation separate from preparation | Apply runbook §7's applicable row; retain rollback runtime. |
| Package replaces site configuration | Versioned templates stay inside the candidate | Review and install each active file explicitly, then reload units. |
| Missing mount or root sentinel | Launcher refuses an ambiguous root | Restore the mount or diagnose the missing evidence. No accidental genesis. |
| Supervisor-only bootstrap failure | Cold provision a new root and empty leader lock | Maintenance can exclude engine startup without manufacturing lock evidence. |
| Missing, replaced or unreadable leader lock | Maintenance refuses, even with force | Preserve the root; use the documented incident procedure. No replacement lock while processes may run. |
| Disconnected engine still holds leadership | Server takes existing leader flock | Refuse without signals. Stop the engine and confirm ownership before maintenance. |
| Concurrent engine startup | Maintenance retains the lock through teardown | Exactly one owner acquires. The losing start or maintenance operation refuses. |
| Active or just-spawned wrapper | Safe maintenance checks unreaped wrappers | Finish or deliberately terminate work; no lifeline closes on refusal. |
| Completed leader leaves descendants | Full verified service-cgroup observation | Refuse safe maintenance. Unknown descendants are never blanket-signalled. |
| Missing process observation or shared/delegated cgroup | Capability refusal | Fix containment or use incident recovery; absence of evidence is not idle. |
| Explicit termination does not clear all work | Force signals validated groups, then rechecks containment | UNKNOWN retains supervisor, lifelines and exclusion. Diagnose before emergency stop. |
| Old supervisor lacks maintenance verb | Unknown-command refusal | Coordinate the supervisor upgrade; never fall back to SHUTDOWN. |
| Manual supervisor stop or restart | `RefuseManualStop=yes`, checked at supervisor start (DL-399) | Manager refuses. Routine engine restart remains available and jobs survive. A unit without it fails to start. |
| Lost maintenance answer or partial write | CLI exits 4 after possible delivery | Inspect supervisor PID, unit and log. No automatic retry or signal escalation. |
| Successful maintenance restarts itself | Exit 8 plus reloaded unit exclusions | Supervisor stays inactive; engine is already stopped. |
| Reboot ends a maintenance window | Disable both units before stopping | Hold remains through reboot. Release only after the intended recovery. |
| Unresponsive supervisor | No safe observation or answer | Explicit incident procedure acknowledges termination; it is not successful maintenance. |
| Definition add/change/remove before a boundary | Resume/catalog gates and staged seal | Read refused input; roll forward or restore definitions using the documented rollback row. |
| Interrupted seal or physical open | Durable lineage and sentinel checks | Retry the identical operation as documented; no fabricated genesis or second external effect. |
| Backup omits anchor or live writers | Quiescence and full lineage inventory | Restore at recorded paths and audit before opening; compare durable records. |
| Runtime rollback cannot read retained estate | State-machine and protocol compatibility gates | Audit with retained original runtime; use a new estate when required. |
| Premature runtime or spool retirement | Retention floors and active-process inventory | Keep runtimes required for active processes, rollback or retained-estate audit. |
| Access map invalid, permissions changed or actor revoked | Existing access admission and reload refusals | Correct ownership/policy; prove no unauthorized decision or effect. |
| TUI selection or period changes during confirmation | Frozen confirmation and exact recovery envelope | Original pins apply or refuse; cancellation sends nothing. |
| TUI loses reply or reconnects | Retained complete recovery command beside UNKNOWN | Read state first; deliberate exact recovery only, never automatic resend. |
| Storage write or fsync fails | Existing durable-before-effect gates | Preserve failed-run evidence; compare journal, spool and external-effect counts. |

## Evidence lanes

| Lane | Required evidence | Scope limit |
| --- | --- | --- |
| Unit and subprocess gates | Full project gates; installed executable help, refusals, permissions, output and exit status; injected partial writes/lost answers | In-process stubs do not prove service-manager behavior. |
| Ubuntu portable recipe | Both profiles, final-path venv, networking disabled during manufacture, alternate prefix, repeated and concurrent preparation | Record native architecture explicitly. |
| EL8 reference RPM | Native x86_64, both profiles, default and alternate prefix; fresh install, upgrade, reinstall, downgrade, erase and failed `%post` | Emulation and ARM64 runs do not satisfy native x86_64 acceptance. |
| Supervisor service | Capability gate, active/idle/force cases, retained leadership, residual descendants, manager refusal and exit-8 non-restart | Container cgroups qualify only the observed container configuration. |
| Terminal UI | Pilot plus real terminal focus, keyboard, resize, filter, selection, box actions, confirmations, cancel, reconnect, periods, logs and detach | Loading a page does not prove interaction behavior. |
| Browser UI | Chromium, WebKit and Firefox; bounded headless interactions and retained diagnostics | Skips, unavailable engines and timeouts are failures or explicit unqualified cells. |
| Lifecycle | Both Nightbank sizes, process survival and external-effect counts, definition/upgrade rollback, seal/open recovery, backup/restore, access/storage faults | Two installs of one build prove mechanics, not compatibility of two releases. |
| Real host | Boot order, kernel/process behavior, mount loss and recovery, local filesystem durability, SELinux policy and permissions | Containers do not qualify these host properties. |

## Rehearsal prerequisites

Use a disposable Podman systemd container, with resource bounds and retained
diagnostics. Run `examples/nightbank/deploy/capability-check.sh` first as
root. Native EL8 qualification also passes `--native-x86_64` and records
the host architecture outside the container; container `uname` alone cannot
exclude emulation. A failed capability gate ends the run before installing
or launching an estate.

Resolve OCI images, OS packages, system Python and release inputs before
disconnecting manufacture. Record each resolved image digest and package
version. A mirror substitution must supply the same pinned bytes or become
a separately identified input set. Use the documented builder's package
index controls, Podman's registry mapping and the OS package-manager mirror
configuration; do not edit wheel requirements or suppress hash checks.

The production host requires Linux, local durable storage, a dedicated
systemd supervisor unit, readable cgroup/process evidence, system Python
3.12 with venv and ensurepip, and the locked bundle's binary wheel ABI.
Older systemd releases also need `busctl` to read loaded exit-status policies
when `systemctl show` cannot print them. Unavailable evidence refuses maintenance.
The rehearsal additionally needs Bash, jq, procps, util-linux, sudo and
RPM build/install tools for EL8. Ubuntu needs `python3.12-venv`; EL8 needs
an approved Python 3.12 package source and its ensurepip/venv support.
Neither pip nor the RPM may fetch Python or OS dependencies in `%post`.

Client-dashboard implementation remains deferred. The terminal/browser
qualification here concerns the existing Textual application.
