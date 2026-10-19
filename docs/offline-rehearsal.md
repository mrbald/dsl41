# Offline deployment rehearsal

The rehearsal uses the exact release wheel and the shared Nightbank service
scenario bodies (DL-389, DL-390).
It constructs the delivery in a connected target container.
It manufactures runtimes and exercises services in a second container started
with `--network none`.
The second container has systemd as PID 1.
The host architecture is recorded; local ARM64 results do not qualify x86-64.

Run from a trusted source checkout with Podman available:

```sh
bash examples/nightbank/deploy/offline-local.sh ubuntu /tmp/offline-ubuntu
bash examples/nightbank/deploy/offline-local.sh el8 /tmp/offline-el8
```

Both destinations must be new directories.
Only tracked/indexed source paths enter the container checkout.
Stage new public implementation files explicitly before a development rehearsal.
Untracked scratch files are never copied.
The default images are pinned OCI indexes for Ubuntu 24.04 and Rocky Linux 8.
EL8 installs Python 3.12 and pip from its configured OS repositories.
The recipe refuses if Python lacks `venv` or `ensurepip`.
It never substitutes another Python or distribution.
The resolved image, OS packages, OS release, Python, systemd, kernel and
network state are retained in the evidence directory.
OS repositories are resolved during the connected image build.
Their package versions are recorded rather than assumed from the base tag.

`DSL41_RELEASE_WHEEL=/absolute/release.whl` selects an already built wheel.
Without it, the connected target builds one wheel from the copied checkout.
The builder downloads the closure of both profiles from the source lock.
The wheel hash and bundle manifest identify the tested delivery.
Preparation and service probes use that wheel only.

The following overrides are explicit site inputs:

| Variable | Input |
| --- | --- |
| `DSL41_OCI_IMAGE` | Replacement base OCI reference; prefer an index digest |
| `DSL41_APT_MIRROR` | Ubuntu archive mirror URL |
| `DSL41_RPM_MIRROR` | Rocky mirror root containing version, repository and architecture paths |
| `DSL41_PYTHON_INDEX` | Connected Python package index URL; lock hashes still apply |
| `DSL41_RELEASE_WHEEL` | Exact release wheel path |
| `DSL41_REHEARSAL_IMAGE` | Existing provisioned image; its full inspection is recorded |
| `DSL41_REUSE_DELIVERY` | Existing delivery directory for a diagnostic repeat on the same target tuple |
| `DSL41_REHEARSAL_SCOPE=maintenance` | Run all package/fault cells and the focused installed maintenance probes; this is not full acceptance |
| `DRILL_KEEP=1` | Keep the disposable container after diagnostics |

An image or delivery override is part of the evidence.
A diagnostic repeat does not qualify a changed source wheel.
Use a fresh exact release wheel for final acceptance.

The offline target runs these checks:

| Scope | Observable check |
| --- | --- |
| Capability | Dedicated service cgroup, manager-stop refusal, completion exit 8, working Python 3.12 |
| Ubuntu portable installation | Prepare and repeat base/UI at `/opt/dsl41` and an alternate prefix |
| EL8 reference RPM | Fresh, upgrade, reinstall, downgrade, failed `%post` and erase at default/alternate prefixes with base/UI; actual RPM dependency checks |
| Delivery faults | Missing/changed wheel and changed template refuse before candidate creation |
| Preparation faults | Concurrent preparation converges, synthetic partial candidate is retained and refused, unavailable pin leaves no receipt, bounded manufacture ENOSPC retains an incomplete runtime while the prior active receipt verifies |
| Both Nightbank sizes | Genesis, same-root resume, live-work refusal, guarded manager stop/restart, engine-only runtime flip preserving the supervisor and command |
| Exclusion and residuals | Concurrent installed startup preserves WAL; a frozen engine with expired lease still holds exclusion; unknown cgroup residue causes safe refusal/force uncertainty and retains exclusion until explicit fixture containment |
| Deliberate termination | `--force=terminate`, supervisor exit 8, no writers, engine reconciliation without another attempt |
| Definition boundaries | Add, change, remove and rollback a job; removed job is a ghost; exact request replay causes no duplicate file effect |
| Recovery | Catalog mismatch refuses; offline seal, audit, backup, same-path restore and operator holds survive |

The synthetic partial candidate is a precreated fixture.
It does not prove an interrupted process.
The packaging source suite separately kills a real preparation parent and checks
lock inheritance while its installer child remains alive.
The distinct manufacture and runtime tmpfs mounts bound the two ENOSPC probes.

The scenario writes a synthetic file three times through three declared
job definitions.
It checks the exact contents and the retained run number.
It also compares a long command's process and attempt marker before and after
an engine restart and runtime activation.
A green socket query alone is insufficient for these checks.
Fixture signals use pidfds after verifying the process start token.
The frozen-engine case expires the 60-second supervisor lease while the engine
keeps its file lock; it then resumes the same process.
The residual fixture is explicitly contained through the incident procedure.

The existing exhaustive service drill remains available through
`drill-local.sh` and the `exhaustive` workflow lane.
It includes crash-loop recovery, engine and supervisor SIGKILL, access-map
changes, historical state-machine upgrade and rollback, and retirement.
Its historical runtime is downloaded during connected setup.
An old supervisor has no maintenance verb.
Only that named historical path uses the explicit guarded incident-stop
procedure; there is no maintenance fallback.
The reboot step stages stop and target-start ordering in a container.
It does not reboot the host.

The `service-drill` workflow dispatch accepts `all`, `offline`, or `exhaustive`.
The offline matrix runs Ubuntu and EL8 on native x86-64 workers.
The release workflow sends the already built candidate wheel to the same
matrix and blocks publication on it.
It archives qualified target bundles and the EL8 default-prefix/base revision-1
RPM beside the release checksums.
The failed-scriptlet fixture RPM is excluded.
Publication still requires the existing tag and environment approval.

Evidence survives a failed run: capability output, full RPM or portable logs,
fault logs, service journals, unit state, container inspection and durable
synthetic run state.
Container results do not qualify host boot, mounts, kernel upgrades,
SELinux policy, live AutoSys instances, or an untested release pair.
The broad installed CLI/TUI/browser qualification is recorded separately in
[cli-tui-qualification.md](cli-tui-qualification.md).
