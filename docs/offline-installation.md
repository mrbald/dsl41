# Offline installation

This recipe prepares a release without changing the running estate (DL-389).
Use the [deployment runbook](deployment-runbook.md) for holds, shutdown,
activation, rollback, and retirement.
The shell recipe uses system Python 3.12, `venv`, and pip.
The target needs Python 3.12 with working `venv` and `ensurepip` modules.
It needs no uv, compiler, package index, or network access.
Managed maintenance needs readable systemd policy and cgroup evidence.
On EL8's systemd 239, `busctl` supplies the loaded exit-status policies that
`systemctl show` prints as `[unprintable]`.

The default prefix is `/opt/dsl41`.
A different absolute prefix is supported.
The layout is:

```text
<prefix>/.venv -> .venvs/<runtime-id>       active runtime; operator-managed
<prefix>/.venvs/<runtime-id>/               prepared runtime; retained
  .dsl41/receipt.json                      completion and installed-file hashes
  .dsl41/manifest.json                     delivery identity and target provenance
  .dsl41/assets/                           versioned deployment templates
<prefix>/delivery/<version>-<rpm-release>/  RPM-owned delivery files
<prefix>/.offline.lock                     preparation lock; retained
```

`runtime-id` contains the release version, profile, and delivery manifest hash.
The profile defaults to `base`.
Choose `ui` only when the host needs the TUI or browser service.
The delivery contains both profiles.
Every virtual environment is created at its final path.
Do not move one after creation.
Its entry-point scripts contain that path.
See [Python's venv documentation](https://docs.python.org/3.12/library/venv.html).

## Prepare release inputs upstream

Use a trusted checkout of the release tag.
Obtain its exact wheel, `requirements-base.txt`, `requirements-ui.txt`,
and `release-inputs.json` from the same release.
Verify all four files against the release's trusted `SHA256SUMS` before use.
Keep the two requirements files and the input manifest together in an exports
directory. Release CI takes these inputs from one candidate artifact.
The producer needs Python 3.11 or later as `python3`, or set `PYTHON` to that
interpreter's path.

The upstream producer uses uv to check the lock and export both profiles
(DL-397). For a development rehearsal, build and export before entering the
target environment:

```sh
uv build --wheel --out-dir /tmp/dsl41-release-dist
bash scripts/export-release-inputs.sh \
    /tmp/dsl41-release-dist/dsl41-1.12.0-py3-none-any.whl /tmp/dsl41-release-inputs
```

The exports directory must be new.
The producer uses `uv export --locked` and preserves markers and hashes.
It omits the command header so local build paths do not enter the exports.
Its input manifest binds the wheel filename and bytes, `pyproject.toml`,
`uv.lock`, and each named profile export by SHA-256.
The input manifest detects mismatched files; it does not authenticate a release.
The trusted release checksums or CI artifact supply that trust.

## Build and qualify on a connected target

Run the native builder from the same trusted release-tag checkout.
It needs system Python 3.12 and pip; it does not need uv.
The rehearsal containers check that uv is absent.
Pass the supplied release wheel and its exports directory.
It verifies their input manifest against `pyproject.toml` and `uv.lock` before
creating output or downloading dependencies. Missing or changed inputs refuse.
The builder has no wheel-build or dependency-export fallback.

```sh
PYTHON=/usr/bin/python3.12
export PYTHON
bash scripts/offline/build-wheelhouse.sh \
    /path/to/dsl41-1.12.0-py3-none-any.whl /path/to/dsl41-release-inputs \
    /tmp/dsl41-offline-bundle
bash scripts/offline/qualify.sh \
    /tmp/dsl41-offline-bundle /tmp/dsl41-offline-qualification
```

The builder retains the original exports and input manifest under
`release-inputs/` in the bundle. It copies each profile's requirements and
appends the exact release-wheel pin for installation.
The target's pip evaluates the exported markers and downloads only pinned
binary wheels with their lock hashes.
Set `DSL41_PYTHON_INDEX` to an approved mirror's simple-index URL when needed.
The lock's hashes still govern downloaded bytes.
Record the mirror origin with the rehearsal inputs; the manifest's wheel
source URL remains the lock's provenance.
The builder copies the supplied release wheel and deployment templates.
`manifest.json` records every delivered file's SHA-256 and mode.
It records each dependency wheel's URL and lock hash.
It also records Python version, implementation, platform, machine, libc,
and operating-system release.
The copied lock, project metadata and release inputs are covered by the file
hashes. `uv.lock` is provenance data; reading it does not require uv.
The target must match this recorded Python/OS/architecture tuple.
Cross-platform download and broad manylinux compatibility are not claimed.
Build and qualify a separate delivery for each target tuple.

Qualification prepares and repeats both profiles.
The installed compiler imports from the candidate, serves CLI help, compiles
a synthetic estate to UC, and emits an explore page with its bundled assets.
UI qualification also imports both UI packages from the candidate.
This is package qualification.
It does not replace the runbook's Linux service and recovery drills.

Archive the qualified bundle and keep its hash in the site's trusted delivery
record before transfer.
Verify that external hash before extracting or executing any delivered tool.
The manifest checks corruption within a trusted delivery.
It is not a signature or an independent source of authenticity.

## Prepare on the offline host

The delivered tools run directly from the extracted bundle:

```sh
bundle=/path/to/verified/dsl41-offline-bundle
prefix=/opt/dsl41
PYTHON=/usr/bin/python3.12 bash "$bundle/tools/prepare.sh" "$bundle" "$prefix"
# Optional UI runtime:
PYTHON=/usr/bin/python3.12 bash "$bundle/tools/prepare.sh" "$bundle" "$prefix" ui
```

The last stdout line names the completed candidate.
The active `.venv` link and installed launcher, access map, and services are
not changed.
Templates are staged under the candidate's `.dsl41/assets/` directory.
Review and configure them through the runbook before activation.

Preparation holds one kernel lock per canonical prefix.
It installs only local wheels with `--no-index`, `--only-binary=:all:`,
`--require-hashes`, `--no-deps`, and `--no-cache-dir`.
The recipe clears pip/Python environment overrides and disables pip config.
It admits only hashed `name==version` requirements with optional markers.
Direct URLs, nested requirements, and pip options are refused.
`--no-index` alone would still permit direct URL requirements.
See [pip's install options](https://pip.pypa.io/en/stable/cli/pip_install/).

The receipt is written only after `pip check` and installed-code smoke tests.
Repeating preparation verifies the delivery identity, the system Python
executable, every installed non-cache file and symlink, and every retained
asset against that receipt.
Generated Python bytecode must match its source.
The repeat also runs `pip check` and the installed-code smoke tests.
It never repairs an existing candidate.

A failure before completion leaves an incomplete directory.
A repeat refuses it and prints recovery instructions.
Do not retry by deleting a directory used by a process, rollback, or retained run.
Keep it for inspection.
Use a new qualified delivery, or retire that exact unused candidate through
the runbook's separate guarded procedure.
Preparation and RPM erase never retire candidates.

## Retire one inactive candidate

Retirement is separate from preparation and RPM erasure.
Keep every runtime still needed by a retained estate's audit, rollback,
or a process started before activation.
First record the exact candidate and why none of those references remain.
Take the deployment runbook's reboot hold and stop every estate using it
through guarded maintenance. Stop attached UI and diagnostic clients too.
Inspect the affected services' cgroups and the host's process inventory.
An unreadable or ambiguous process scope refuses retirement.
Arrange exclusive operator control so another user or service cannot launch
this candidate during the operation. The preparation lock alone does not
prevent arbitrary executable invocation.

The following guard checks the named path and active link under the same
prefix lock as preparation. Run it only after those process and retention
preconditions are established. It also handles a never-completed candidate.

```sh
set -eu
prefix=/opt/dsl41
candidate='/opt/dsl41/.venvs/<runtime-id>'
test -f "$prefix/.offline.lock" && test ! -L "$prefix/.offline.lock" || exit 2
(
    flock -n 9 || exit 2
    test -d "$prefix/.venvs" && test ! -L "$prefix/.venvs" || exit 2
    test -d "$candidate" && test ! -L "$candidate" || exit 2
    canonical=$(realpath -e "$candidate") || exit 2
    versions=$(realpath -e "$prefix/.venvs") || exit 2
    test "$(dirname "$canonical")" = "$versions" || exit 2
    if test -e "$prefix/.venv" || test -L "$prefix/.venv"; then
        test -L "$prefix/.venv" || exit 2
        active=$(realpath -e "$prefix/.venv") || exit 2
        test "$active" != "$canonical" || exit 2
    fi
    rm -rf -- "$canonical"
) 9<>"$prefix/.offline.lock"
test ! -e "$candidate"
```

Keep the delivery kit and its trusted hash if future audit or reproduction
may require it. Release the reboot hold only for estates allowed to start.

## Reference RPM

The reference spec is [dsl41-offline.spec](../scripts/offline/dsl41-offline.spec).
Build on the qualified native target with RPM build tools installed:

```sh
bash scripts/offline/build-rpm-source.sh /tmp/dsl41-offline-bundle /tmp/dsl41-rpm-source
mkdir -p /tmp/dsl41-rpmbuild/{BUILD,RPMS,SOURCES,SPECS,SRPMS}
cp /tmp/dsl41-rpm-source/*.tar.gz /tmp/dsl41-rpmbuild/SOURCES/
rpmbuild -bb scripts/offline/dsl41-offline.spec \
    --define '_topdir /tmp/dsl41-rpmbuild' \
    --define 'dsl41_version 1.12.0'
```

Set `--define 'dsl41_prefix /srv/dsl41'` for a different prefix.
Set `--define 'dsl41_profile ui'` for a UI host.
`dsl41_release` defaults to `1`.
`dsl41_python` defaults to `/usr/bin/python3.12`.
The target package manager must supply Python 3.12 and pip.
The reference dependency names are `python3.12` and `python3.12-pip`.
The target's Python must include `venv` and `ensurepip`.
Confirm those dependencies in the actual target distribution.

`%post` invokes the same preparation recipe.
It never links `.venv`, restarts a service, or overwrites active templates.
A preparation failure makes the scriptlet fail and retains the candidate.
RPM may already have installed its delivery payload at that point.
Inspect the failure before selecting another delivery or retiring unused state.

RPM owns only the versioned delivery subtree.
It does not own generated environments, `.venv`, the lock, or active templates.
Upgrade, reinstall, downgrade, and erase leave those paths in place.
There are no erase scriptlets.
Generated runtime retirement remains a separate operator decision.
This follows RPM's [file ownership and scriptlet rules](https://rpm.org/docs/4.20.x/manual/spec.html).

The lifecycle harness is [rpm-lifecycle.sh](../scripts/offline/rpm-lifecycle.sh).
Run it as root in a disposable native target with networking disabled.
It builds two RPM revisions and tests fresh installation, upgrade, reinstall,
downgrade, and erase for base/UI at the default and a custom prefix.
It checks retained receipts, runtime files, active links, and staged templates.
It also forces a failed `%post` while the prior active runtime stays usable.
Package presence is checked separately from the missing readiness receipt.
It checks that RPM owns only delivery files.
The harness checks declared RPM dependencies by default.
Set `RPM_NODEPS=1` only for an Ubuntu mechanics run with prerequisites already
provided outside RPM.
That mode does not validate the target distribution's dependency resolution.
Ubuntu ARM64 evidence cannot qualify EL8 or x86-64.
