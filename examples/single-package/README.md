# Package one engine and its JIL

For the Packager building one site's RPM with an engine runtime, main config,
JIL and systemd unit. Copy [the spec](synthetic-engine.spec),
[the unit](synthetic-engine.service) and [the config](dsl41.toml), then replace
the synthetic names consistently. The rest of the [packager track](../../README.md#documents)
covers reference offline delivery. Host clock, backup and recovery procedures
remain in [deployment](../../docs/deploy.md).

## Choose the files the RPM owns

```text
/opt/synthetic-engine/
    runtime/                       Python environment and installed wheels
    cfg/*.jil                      release-owned JIL
/etc/synthetic-engine/dsl41.toml    site-edited main config
/usr/lib/systemd/system/synthetic-engine.service
/srv/synthetic-engine/run/          engine-owned state; outside the RPM
```

The layout shows package files and the separate durable root; it omits job data.

The spec creates `synthetic-engine:synthetic-engine`. Jobs run as that account.
It creates a missing run root with mode 0700. It never packages that root, changes
existing contents or removes the account on erase. Keep job data outside the
runtime too. Adjust `RequiresMountsFor` for every required filesystem.

The main config is `%config(noreplace)`: local edits survive and a changed
packaged default becomes `.rpmnew`. Edit the installed config before `rpm -U`
when the release needs config changes; `%posttrans` uses that file.
JIL is ordinary payload: the release replaces it. Deliver normal JIL edits in a
new release. Use a separate deployment procedure if JIL
must be locally editable; `.rpmnew` JIL would not be the intended release.
The vendor unit is ordinary payload. Put local overrides in
`/etc/systemd/system/synthetic-engine.service.d/`, outside this RPM.

## Prepare the runtime at its final path

Build separately in a disposable EL8 or EL7 **x86_64** container. Use the exact
reviewed main commit, its wheel and its locked dependencies. The project version
alone does not identify its state format. Record the commit and wheel hashes.
This site package owns a prebuilt runtime; the reference
[offline RPM](../../scripts/offline/dsl41-offline.spec) instead owns delivery
files and retains generated environments (DL-389, DL-439).

1. In the checkout, build and export the inputs. Unset `VIRTUAL_ENV`:

   ```sh
   env -u VIRTUAL_ENV uv build --wheel --out-dir /var/tmp/package-inputs/dist
   env -u VIRTUAL_ENV PYTHON=python3.12 scripts/export-release-inputs.sh \
     /var/tmp/package-inputs/dist/dsl41-*.whl \
     /var/tmp/package-inputs/exports
   ```

   Use a directory containing only the wheel just built. Take the engine version
   from that wheel. Transfer those files and these
   templates into the build container. Install `rpm-build`, `tar`, `gzip` and
   the spec's OS dependencies there. EL8 also needs `python3.12` and
   `python3.12-pip`.

2. Create the runtime at `/opt/synthetic-engine/runtime`, an unused path in
   that container. On EL8:

   ```sh
   python3.12 -m venv /opt/synthetic-engine/runtime
   ```

   For EL7, choose an interpreter using the procedure below.
   Do not manufacture a venv under a staging path and rename it: its scripts
   contain absolute paths (DL-389).

3. In the container's input directory, append the release wheel's hash to the
   base export, then download only matching binary wheels:

   ```sh
   cp exports/requirements-base.txt requirements.txt
   set -- dist/dsl41-*.whl
   [ "$#" -eq 1 ] && [ -f "$1" ] || exit 1
   wheel=$1
   version=$(basename "$wheel" | cut -d- -f2)
   digest=$(sha256sum "$wheel" | cut -d ' ' -f 1)
   printf '\ndsl41==%s --hash=sha256:%s\n' "$version" "$digest" >> requirements.txt
   python=/opt/synthetic-engine/runtime/bin/python3.12
   "$python" -I -m pip download --only-binary=:all: --require-hashes --no-deps \
     -r exports/requirements-base.txt -d wheelhouse
   cp "$wheel" wheelhouse/
   "$python" -I -m pip install --no-index --find-links wheelhouse \
     --only-binary=:all: --require-hashes --no-deps --no-compile -r requirements.txt
   "$python" -I -m pip check
   /opt/synthetic-engine/runtime/bin/dsl41 --version
   /opt/synthetic-engine/runtime/bin/dsl41 --help
   ```

   Retain the wheelhouse, exports and hashes as build inputs. A missing matching
   wheel is a build failure; do not fall back to a compiler or an unpinned version.
   The base profile supplies the engine and CLI. It omits the optional TUI.

4. Assemble `synthetic-engine-1.0.0/` with `runtime/` copied from that final
   path, `cfg/` containing only the release's JIL, and the unit and config
   templates at its top level. Retain dependency and Python license files.
   These commands use the repository's synthetic estate:

   ```sh
   mkdir -p source/synthetic-engine-1.0.0/cfg rpmbuild/{BUILD,RPMS,SOURCES,SPECS,SRPMS}
   cp -a /opt/synthetic-engine/runtime source/synthetic-engine-1.0.0/
   cp synthetic-engine.service dsl41.toml source/synthetic-engine-1.0.0/
   cp receive_process_load.jil source/synthetic-engine-1.0.0/cfg/estate.jil
   tar -czf rpmbuild/SOURCES/synthetic-engine-1.0.0.tar.gz -C source synthetic-engine-1.0.0
   rpmbuild -bb synthetic-engine.spec --define "_topdir $PWD/rpmbuild" \
     --define 'site_release 1'
   ```

   Copy `receive_process_load.jil` unchanged from `tests/fixtures/` in the checkout;
   it lints clean.
   `1.0.0` is the site's package version, independent of the engine version.
   Increment `site_release` for each rebuilt payload. The spec disables RPM's
   automatic ELF dependencies and postprocessing to preserve the prepared tree.
   It disables shared build-id links so packages carrying the same dependency
   wheels do not conflict on those links.
   Keep its explicit OS requirements aligned with the chosen Python distribution.
   Lint the exact packaged inputs as the service account before site rollout.

## Provide Python 3.12 on EL7

On EL7 userspace, the engine ran directly; RPM checks covered `$1` values,
scriptlet order, account creation and run-root creation.
The scriptlets' stop, start, wait and update steps ran only on EL8.
The EL7 systemd service lifecycle and kernel 3.10 were not checked.
Rehearse install, both upgrades and erase on a real EL7 host before a site rollout.

### Use a site-provided interpreter

This route was not tested. The build container must have the same interpreter
build at the same absolute path as the target hosts; runtime scripts carry that
path. Set `site_python` to that executable and create the runtime at its final path:

```sh
"$site_python" -m venv /opt/synthetic-engine/runtime
```

After installing the wheels, check the interpreter's modules:

```sh
/opt/synthetic-engine/runtime/bin/python3.12 -I -c 'import sqlite3, ssl'
```

Before packaging, [start one synthetic engine](../../docs/deploy.md#start-the-estate)
with this runtime, require `query status --json` to report `RUNNING`, then
[drain it](../../docs/deploy.md#drain-the-engine).
Continue source assembly above and build with:

```sh
rpmbuild -bb synthetic-engine.spec --define "_topdir $PWD/rpmbuild" \
  --define 'site_release 1' --define 'custom_python 1'
```

The runtime depends on that interpreter staying installed at that path.
If the interpreter has a site Python RPM, add that package to this RPM's
`Requires`. The `custom_python` flag drops the default OS Python requirements.

### Embed the standalone distribution

For a site without its own Python 3.12, use the stripped GNU/glibc x86_64
[python-build-standalone distribution](https://github.com/astral-sh/python-build-standalone/releases/tag/20261026)
inside this RPM. This adds Python without a compiler, a second venv, or a change
to system Python. Its [upstream runtime floor](https://gregoryszorc.com/docs/python-build-standalone/main/running.html#runtime-requirements)
is glibc 2.17. Use the ordinary `x86_64` build, not a higher CPU-level variant
or the fully static build, which cannot load the Pydantic extension.

1. Obtain this exact archive on the build machine and verify its SHA256:

   ```sh
   archive=cpython-3.12.16+20261026-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz
   curl -fL "https://github.com/astral-sh/python-build-standalone/releases/download/20261026/$archive" -o "$archive"
   printf 'ce4f6d0a6cfdb3bed6e44c2f9affd85943c76a42e25ad3a259002f31d6fb8a20  %s\n' "$archive" | sha256sum -c -
   ```

2. In the EL7 x86_64 build container, unpack it at the unused final path:

   ```sh
   mkdir -p /opt/synthetic-engine/runtime
   tar -xzf "$archive" --strip-components=1 -C /opt/synthetic-engine/runtime
   /opt/synthetic-engine/runtime/bin/python3.12 --version
   ```

3. Continue the wheel installation and source-assembly steps above with that
   interpreter. Preserve the archive's license files. Build with:

   ```sh
   rpmbuild -bb synthetic-engine.spec --define "_topdir $PWD/rpmbuild" \
     --define 'site_release 1' --define 'custom_python 1'
   ```

   That flag removes the OS Python requirements. The tested engine dependency
   extension is a CPython 3.12 `manylinux_2_17_x86_64` Pydantic wheel.
   The explicit `glibc` and `libgcc` requirements cover its observed ELF links.

EL7's systemd 219 needs `StartLimitInterval=300` and `StartLimitBurst=5` in
`[Service]`. The reference unit's `StartLimitIntervalSec` and start-limit
placement in `[Unit]` do not fit 219. Its `systemctl` also lacks `--value`;
the spec reads `ActiveState=...` without it.

The service account's `nologin` shell produces a preflight warning and uses
`/bin/sh` for jobs on both EL7 and EL8.

## Put each action in its scriptlet

The template enables and starts the service on first install. It also starts it
on every upgrade, even if it was stopped beforehand. Use a quiet window, an
available systemd manager and one version of this package at a time. This recipe
is not an offline image-install or parallel-install recipe.

| Scriptlet | First install | Upgrade | Erase |
| --- | --- | --- | --- |
| New `%pre` | `$1=1`: create account | `$1=2`: stop old unit before payload replacement; ensure account | Not run |
| New `%post` | `$1=1`: create missing root, reload units, enable | `$1=2`: create missing root, reload; keep enablement | Not run |
| Old `%preun` | Not run | `$1=1`: do nothing | `$1=0`: stop, then disable |
| Old `%postun` | Not run | `$1=1`: reload units; never restart | `$1=0`: reload after payload removal |
| New `%posttrans` | EL8 `$1=1`, EL7 `$1=0`: start, wait, update once | EL8 `$1=1`, EL7 `$1=0`: start, wait, update once | Not run |

`$1` counts remaining instances for the install/removal scriptlets. Test `>1`
in `%pre` for upgrades, rather than assuming every transaction has exactly two.
RPM 4.14 on EL8 passes 1 to `%posttrans`; RPM 4.11 on EL7 passes 0.
The body performs the same actions regardless of that argument.
For this package, the upgrade order is new `%pre`, new files, new `%post`, old
`%preun`, old obsolete files removed, old `%postun`, new `%posttrans`.
See the [RPM scriptlet order](https://rpm.org/docs/4.20.x/manual/triggers.html).
An older package with a restarting `%postun` must be adapted before using this
recipe: the new spec cannot change the old installed scriptlet.

The unit launches `run --config`, drains in `ExecStop`, and leaves exit 2, 6 or 7
down for diagnosis. Keep the clock waiter and host checks from
[deployment](../../docs/deploy.md#install-the-package-and-unit).
The template omits the optional site status hook and failure unit.

`Type=simple` does not establish engine readiness. `%posttrans` polls
`query status --json` for up to 180 seconds before issuing one `update --wait`.
A no-change update prints `nothing changed: ...` and changes no revision (DL-478).
A changed update adopts the delivered JIL (DL-437, DL-463).
Do not put a bare update in `ExecStartPost`: it can race socket creation.
Readiness does not prevent old-estate scheduling between start and update.
This product supplies no atomic start-and-apply barrier. Use a window with no
eligible work, or arrange deliberate holds with the operator beforehand.

Never remove or reinitialize the run root, delete `engine.db`, change its version,
restore statuses, force-start jobs, or replace runtime files while the engine
runs in a scriptlet. Never hide an update failure with `|| :`, retry an uncertain
request automatically, or treat RPM's exit status alone as service readiness.
A failed scriptlet does not roll back its filesystem or service effects.
RPM treats a failed `%post` or `%posttrans` as a warning and still exits 0.
In every `%posttrans` outcome below, the new package and its JIL are installed;
on an upgrade the old release's JIL is gone.

| Outcome | What remains | Next step |
| --- | --- | --- |
| Unit fails to start | Engine down. | Read `journalctl -u synthetic-engine.service`; for a state-version refusal, follow [recovery](#recover-a-refused-start). |
| Readiness wait times out | Engine may be running or still starting on the stored estate. | Check `query status --json`; once it reports `RUNNING`, run `update --wait` by hand. |
| Update is refused | Engine `RUNNING` on the old estate; new JIL pending. | Ship corrected JIL in a new release. Only in an emergency, correct the installed JIL and run `update --wait`; the next upgrade overwrites that edit. |
| Update outcome is uncertain | Application is unconfirmed. | Look up the printed request ID with `update --wait --request-id ID`; do not submit a fresh request blindly. |

Run socket commands as the service account through
`/opt/synthetic-engine/runtime/bin/dsl41`, with
`--socket /srv/synthetic-engine/run/control.sock`.

## Install and check the result

1. Before upgrade, have the operator stop the service, verify the shutdown
   record and absence of writers, then take a
   [whole-root backup](../../docs/deploy.md#take-a-backup-for-restore).
   Retain the old RPM, exact engine identity, inputs and main config.
   The scriptlet's stop is a second guard; it does not make a backup.
2. Install as root:

   ```sh
   rpm -Uvh synthetic-engine-1.0.0-1.el8.x86_64.rpm
   ```

3. Check scriptlet output for refusal, timeout or uncertain update, even if
   RPM exits zero. Then run:

   ```sh
   systemctl is-enabled synthetic-engine.service
   systemctl status synthetic-engine.service --no-pager
   journalctl -u synthetic-engine.service --no-pager
   runuser -u synthetic-engine -- /opt/synthetic-engine/runtime/bin/dsl41 \
     query status --json --socket /srv/synthetic-engine/run/control.sock
   ```

   Require an active unit, `engine: RUNNING`, the intended `config_rev`, and
   no in-progress update. Compare job statuses, holds and other flags with the
   pre-install record. Confirm the intended definitions through `query spec`
   and a safe synthetic job. Use a `reread` preview to check pending inputs if needed.
   Retained completed jobs keep their status and holds; running and structurally
   changed jobs follow the [update rules](../../docs/deploy.md#reload-a-changed-estate).
4. On erase, run `rpm -e synthetic-engine`. The unit stops and disables;
   runtime, release JIL and vendor unit disappear. State and the service account
   remain. A locally modified main config may remain as `.rpmsave`.

## Recover a refused start

A state-version refusal leaves the root unchanged (DL-424, DL-474).
Read the journal, keep the root, and stop the unit. Reinstall the RPM containing
its original writer with `rpm -Uvh --oldpackage OLD.rpm`. This template's
`%posttrans` starts and checks it too. Clear a reached start limit with
`systemctl reset-failed synthetic-engine.service` before that installation.
Check RUNNING and the saved statuses and holds. Arbitrary downgrades after the
new engine has written state are not supported. Restore the complete stopped
backup with its matching inputs and binary if compatibility requires it.
Drain and snapshot do not convert state. A deliberate fresh root loses active
state; use [the explicit reset procedure](../../docs/deploy.md#upgrade-the-engine).
