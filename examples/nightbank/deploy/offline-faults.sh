#!/usr/bin/env bash
# Faults use the delivered wheels and retain every failed candidate (DL-389).
set -euo pipefail
[ "$#" = 1 ] || { echo 'usage: offline-faults.sh BUNDLE' >&2; exit 2; }
bundle=$(cd "$1" && pwd)
python=${PYTHON:-python3.12}
work=$(mktemp -d /tmp/dsl41-offline-faults.XXXXXX)
tools=$bundle/tools
candidate() { "$python" -I "$tools/bundle.py" candidate "$1" "$2" base; }
prepare() { PYTHON=$python bash "$tools/prepare.sh" "$1" "$2"; }
refuses() {
    local code=0
    "$@" >"$work/refusal.log" 2>&1 || code=$?
    cat "$work/refusal.log"
    [ "$code" != 0 ] || { echo 'fault unexpectedly succeeded' >&2; exit 1; }
}
for fault in missing-wheel changed-wheel changed-template; do
    cp -a "$bundle" "$work/$fault"
    wheel=$(find "$work/$fault/wheels" -name 'dsl41-*.whl')
    case $fault in
        missing-wheel) rm "$wheel";;
        changed-wheel) printf 'corrupt\n' >>"$wheel";;
        changed-template) printf 'corrupt\n' >>"$work/$fault/assets/dsl41-launch";;
    esac
    refuses prepare "$work/$fault" "$work/$fault-prefix"
    test ! -d "$work/$fault-prefix/.venvs" ||
        [ -z "$(ls -A "$work/$fault-prefix/.venvs")" ]
    echo "ok: refused $fault before candidate creation"
done
prefix=$work/concurrent
prepare "$bundle" "$prefix" >"$work/first.log" 2>&1 & first=$!
prepare "$bundle" "$prefix" >"$work/second.log" 2>&1 & second=$!
status=0
wait "$first" || status=1
wait "$second" || status=1
[ "$status" = 0 ]
[ "$(tail -n 1 "$work/first.log")" = "$(tail -n 1 "$work/second.log")" ]
[ "$(find "$prefix/.venvs" -mindepth 1 -maxdepth 1 -type d | wc -l)" = 1 ]
runtime=$(candidate "$bundle" "$prefix")
ln -s ".venvs/${runtime##*/}" "$prefix/.venv"
before=$(sha256sum "$runtime/.dsl41/receipt.json")
"$prefix/.venv/bin/dsl41" --help >/dev/null
# An incomplete final path is evidence, never an instruction to repair it (DL-389).
partial=$(candidate "$bundle" "$work/partial")
mkdir -p "$partial"
printf 'preserve interrupted preparation\n' >"$partial/evidence"
refuses prepare "$bundle" "$work/partial"
[ "$(cat "$partial/evidence")" = 'preserve interrupted preparation' ]
test ! -e "$partial/.dsl41/receipt.json"
[ "$(sha256sum "$runtime/.dsl41/receipt.json")" = "$before" ]
"$prefix/.venv/bin/dsl41" --help >/dev/null
# A valid delivery with an unavailable pin must fail without a network fallback (DL-389).
cp -a "$bundle" "$work/missing-pin"
printf 'absent-offline-fixture==99 --hash=sha256:%064d\n' 0 >"$work/missing-pin/requirements-base.txt"
wheel=$(find "$work/missing-pin/wheels" -name 'dsl41-*.whl')
"$python" -I "$tools/bundle.py" manifest "$work/missing-pin" "$wheel"
refuses prepare "$work/missing-pin" "$work/missing-pin-prefix"
test ! -e "$(candidate "$work/missing-pin" "$work/missing-pin-prefix")/.dsl41/receipt.json"
[ "$(sha256sum "$runtime/.dsl41/receipt.json")" = "$before" ]
echo "ok: concurrent repeat, partial candidate, unavailable pin; retained evidence $work"

# A separate bounded mount exhausts manufacture storage, without touching the active runtime (DL-389).
read -r storage_type storage_bytes <<<"$(findmnt -n -b -o FSTYPE,SIZE --mountpoint /srv/dsl41-install-storage)"
[ "$storage_type" = tmpfs ] && [ "$storage_bytes" = 16777216 ] ||
    { echo 'a distinct 16 MiB manufacture tmpfs is required' >&2; exit 2; }
storage=/srv/dsl41-install-storage/prefix
refuses prepare "$bundle" "$storage"
grep -E 'No space left on device|ENOSPC' "$work/refusal.log" >/dev/null
failed=$(candidate "$bundle" "$storage")
test -d "$failed"
test ! -e "$failed/.dsl41/receipt.json"
test ! -e "$storage/.venv"
refuses prepare "$bundle" "$storage"
test -d "$failed"
test ! -e "$failed/.dsl41/receipt.json"
[ "$(sha256sum "$runtime/.dsl41/receipt.json")" = "$before" ]
"$python" -I "$tools/bundle.py" verify "$bundle" "$runtime" base
"$prefix/.venv/bin/dsl41" --help >/dev/null
echo "ok: actual manufacture ENOSPC retained partial; repeat refused; prior active runtime unchanged"
