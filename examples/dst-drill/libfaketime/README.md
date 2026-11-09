# Reproduce the libfaketime initialization repair

For maintainers reproducing a dependency initialization deadlock on Rocky Linux 8/aarch64.
Return to the [DST drill](../README.md) or [maintainer track](../../../README.md#documents).
This optional repair keeps the drill's Debian bookworm dependency recipe unchanged.
The image's whole-directory copy includes this package as unused source content.
The source-distribution build has no custom exclusion for this directory. This package is
intended to ship there as unused source content; exact release-artifact contents remain unbuilt
here. The drill does not select or load the patched library.
No scheduler-rate, MT-library, x86, EL8-kernel, CI or deployment qualification follows.

The patch lets suppressed internal `__fxstat64` calls use the resolved real function before
initialization. Ordinary stat calls still use the upstream handler. The regression requires
both the old initialization-futex cut and successful fake-stat/shared-memory behavior.
It rejects a library that bypasses all stat faking.

## Prepare an isolated target

Use an ordinary user in an exclusive Linux PID/shared-memory namespace. The regression requires
Python 3.12+, GCC, make, target glibc headers exposing `_STAT_VER`, Perl with `Time::HiRes`,
`rpm`, `/proc` visibility, GNU `timeout` and `strace`. Fetching needs curl, tar, patch and sha256sum.
The isolated container needs an init process to reap orphaned build/test children.
The required runtime RPM is `glibc-2.28-251.el8_10.48.aarch64`; another build is refused.
An inability to inspect the negative futex cut is `NOT ESTABLISHED`, never a substitute pass.

Obtain an exclusive target allocation before execution. Record its actual PID/IPC/mount
namespace and tmpfs provenance, and whether shared memory is fresh, retained or unknown.
Keep the observer unpreloaded from process creation. No other process may preload either
library in the allocated namespace while these checks run.

The script stops on a failed prerequisite, assertion or inventory. Keep its printed
`repair_root` and all outputs after failure. Do not disable shared memory/stat faking, change
assertions, substitute libraries, delete shared objects or consume PIDs to evade a collision.
The intentional negative control may leave residue that refuses the later upstream inventory.
This script cannot recreate its target namespace or continue past that refusal. Any separately
allocated namespace work must preserve prior evidence, recheck persistent inputs and respect
the original remaining deadline. It is outside this script; do not invent a restart loop.

## Save and run the complete script

1. Save the complete Bash script below outside the checkout, for example as
   `/tmp/dsl41-libfaketime-reproduce.sh`. Bash reads commands from that saved file, so its
   `exec 0</dev/null` preserves the script while keeping fd0 unchanged through inventory
   and `make test`. Do not paste the script into an interactive shell.

```bash
set -euo pipefail
repair_package=${1:?supply the absolute package directory}
repair_provenance=${2:?supply fresh, retained or unknown provenance}
repair_provenance_note=${3:?supply the actual namespace provenance record}
repair_exclusive_note=${4:?supply the actual exclusive allocation record}
[[ $repair_package =~ ^/[A-Za-z0-9_./-]+$ ]]
case "$repair_provenance" in fresh|retained|unknown) ;; *) exit 64 ;; esac
test -n "${repair_provenance_note//[[:space:]]/}"
test -n "${repair_exclusive_note//[[:space:]]/}"
test "${repair_provenance_note#REPLACE}" = "$repair_provenance_note"
test "${repair_exclusive_note#REPLACE}" = "$repair_exclusive_note"
repair_root=$(mktemp -d /tmp/dsl41-libfaketime.XXXXXXXX)
printf 'repair_root=%s\n' "$repair_root"
repair_commit=3ccdd344aadf1e317156fa5fb7c881f2c4404778
repair_name="libfaketime-$repair_commit"
curl --fail --location --output "$repair_root/source.tar.gz" \
  "https://github.com/wolfcw/libfaketime/archive/$repair_commit.tar.gz"
printf '%s  %s\n' \
  897e3c2b4e92f36770d1be2c57a80964523fb5e8d6cb6c8344cfde8d99efd434 \
  "$repair_root/source.tar.gz" | sha256sum --check --strict -
printf '%s  %s\n' \
  f76f91d49bcdffffae90fe73fc3ca259e2e07decbce08ef68fb441fbb6515c0f \
  "$repair_package/init-stat.patch" | sha256sum --check --strict -
mkdir "$repair_root/base"
tar -xzf "$repair_root/source.tar.gz" -C "$repair_root/base"
repair_base="$repair_root/base/$repair_name"
cp -a "$repair_base" "$repair_root/patched"
repair_patched="$repair_root/patched"
patch --directory="$repair_patched" --forward --batch --fuzz=0 -p1 \
  < "$repair_package/init-stat.patch"
test "$(id -u)" != 0
test "$(uname -m)" = aarch64
test "$(rpm -q glibc)" = glibc-2.28-251.el8_10.48.aarch64
repair_prefix=/home/runner/clock-calibration/prefix
for repair_config in /etc/faketimerc "$HOME/.faketimerc" "$repair_prefix/etc/faketimerc"; do
  test ! -e "$repair_config"
  test ! -L "$repair_config"
done
repair_make_deadline=$(python3.12 -c 'import time; print(time.monotonic() + 600)')
repair_make_budget() {
  python3.12 -c 'import sys,time; n=int(float(sys.argv[1])-time.monotonic()-5); assert n>0, "make deadline exhausted"; print(n)' "$repair_make_deadline"
}
repair_remaining=$(repair_make_budget)
env -i PATH=/usr/bin:/bin HOME="$HOME" LANG=C TZ=UTC LC_ALL=C \
  timeout --signal=TERM --kill-after=5s "${repair_remaining}s" \
  make -C "$repair_base" all CC=gcc PREFIX="$repair_prefix" \
  > "$repair_root/base-build.log" 2>&1
repair_remaining=$(repair_make_budget)
env -i PATH=/usr/bin:/bin HOME="$HOME" LANG=C TZ=UTC LC_ALL=C \
  timeout --signal=TERM --kill-after=5s "${repair_remaining}s" \
  make -C "$repair_patched" all CC=gcc PREFIX="$repair_prefix" \
  > "$repair_root/patched-build.log" 2>&1
repair_original="$repair_base/src/libfaketime.so.1"
repair_candidate="$repair_patched/src/libfaketime.so.1"
printf '%s  %s\n' \
  67a42d3cdcf743e78d1af2abd64b694cb6ae5407be2efb3f3a69ab33ecce4e58 \
  "$repair_original" | sha256sum --check --strict -
repair_candidate_sha=$(sha256sum "$repair_candidate")
repair_candidate_sha=${repair_candidate_sha%% *}
env -i PATH=/usr/bin:/bin TZ=UTC LC_ALL=C python3.12 "$repair_package/observe.py" \
  --original "$repair_original" --candidate "$repair_candidate" \
  --candidate-sha256 "$repair_candidate_sha" \
  --work "$repair_root/probe-work" --output "$repair_root/probe-output" \
  --provenance "$repair_provenance" --provenance-note "$repair_provenance_note" \
  --exclusive-note "$repair_exclusive_note" > "$repair_root/observer.log" 2>&1
exec 0</dev/null
env -i PATH=/usr/bin:/bin TZ=UTC LC_ALL=C python3.12 "$repair_package/upstream_gate.py" inventory \
  --baseline-report "$repair_root/probe-output/report.json" \
  --output "$repair_root/gate-inventory.json" --provenance "$repair_provenance" \
  --provenance-note "$repair_provenance_note"
mkdir "$repair_root/gate-trace"
repair_remaining=$(repair_make_budget)
repair_make_status=0
env -i PATH=/usr/bin:/bin HOME="$HOME" LANG=C TZ=UTC LC_ALL=C \
  FAKETIME_TESTLIB="$repair_candidate" \
  timeout --signal=TERM --kill-after=5s "${repair_remaining}s" \
  strace -ff -s 4096 -e trace=process -o "$repair_root/gate-trace/child" \
  make -C "$repair_patched" test CC=gcc PREFIX="$repair_prefix" \
  > "$repair_root/gate-combined.log" 2>&1 || repair_make_status=$?
env -i PATH=/usr/bin:/bin TZ=UTC LC_ALL=C python3.12 "$repair_package/upstream_gate.py" check \
  --combined-log "$repair_root/gate-combined.log" --make-status "$repair_make_status" \
  --inventory-receipt "$repair_root/gate-inventory.json" \
  --regression-report "$repair_root/probe-output/report.json" \
  --trace-directory "$repair_root/gate-trace" > "$repair_root/gate-check.json"
```

2. From the repository root, run that saved file with Bash in an unpreloaded shell. Supply the absolute package path
   and actual allocation records. Replace the two example notes; the script refuses their
   `REPLACE` prefix. Choose provenance from observed facts, including `unknown` when applicable.

   ```sh
   bash /tmp/dsl41-libfaketime-reproduce.sh \
     "$(pwd -P)/examples/dst-drill/libfaketime" unknown \
     'REPLACE with the actual namespace and tmpfs provenance record' \
     'REPLACE with the actual exclusive preload allocation record'
   ```

   The first workspace action prints `repair_root`; all downloaded sources, build logs,
   observation output and upstream evidence stay there. It is outside the checkout.
   A failure stops the script without removing that evidence.

## Interpret the result

The archive is v0.9.12 at the full pinned commit. Preserve both source trees, their upstream
headers, `COPYING`, test bytes and default flags. The literal
`/home/runner/clock-calibration/prefix` is a synthetic neutral build prefix compiled into the
reference identity; nothing is installed there. Another compiler/build environment can produce
different bytes even on this ABI. Stop on a hash mismatch; never edit the original hash to match.
The selected library is the non-MT `src/libfaketime.so.1`; building the MT variant does not qualify it.
Retain compiler/package versions, logs and both library hashes. No vendor tree or binary belongs in git.

Both builds and `make test` share one 600-real-second monotonic deadline. GNU timeout signals
each owned process group and reserves five seconds for forced cleanup. The observer has its own
120-second aggregate cap with three seconds reserved for owned process cleanup. Expiration stops
execution; neither a namespace boundary nor a failed command resets a deadline.

The observer records names before preload and verifies actual peer PID/start tokens and own-PID
ENOENT. The original must show the writable-mapping futex offset `0x20540` for ten real seconds.
The repaired library must preserve fake timestamps and other metadata, real monotonic progress,
two reader threads, and a living parent/child/grandchild shared timeline.
[`EXPECTED.json`](EXPECTED.json) states the assertion boundary. The observer never deletes shared
objects. Successful process cleanup does not prove no residue. Exclusivity must continue through
the upstream inventory and tests; that inventory is a point-in-time check.

The upstream checker requires four functional suites, nine ordered source sections, eight normal
timetest exits and the date-only exit. It classifies merged diagnostics, progress, dates and target
SIGUSR1 lines. Both upstream timer-overrun assertions stay unchanged. The upstream tests include
stat-disabling sections; the independent probes never use them. Do not use `strace -qq`, which
removes exit receipts. Unexpected diagnostics, missing tools, tracing restrictions, malformed
output or residue stop the run, even if `make` exits zero.

Changes beneath this directory match the DST drill CI trigger. Its qualification and open
[BUG-45](../../../TODO.md#low) remain separate; these instructions authorize no CI dispatch.

## Provenance and licenses

`init-stat.patch` modifies public [wolfcw/libfaketime](https://github.com/wolfcw/libfaketime)
source at the pinned commit. Its upstream header states GNU GPL version 2.
[`COPYING`](COPYING) is the verbatim license text from that archive. Preserve those upstream
notices when applying the patch. The upstream source and its modifications retain those
terms; this repository's commercial offering does not relicense that dependency.

`probe.c`, `observe.py`, `upstream_gate.py` and `EXPECTED.json` are independent regression
material under the repository's [licensing terms](../../../LICENSING.md). They are separate
from the upstream patch/license. No binary distribution is proposed here.
