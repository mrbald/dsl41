#!/usr/bin/env bash
# Run inside a disposable native target as root, with network disabled (DL-389).
set -euo pipefail
if [ "$#" -ne 2 ]; then
    echo "usage: $0 <qualified-bundle> <new-scratch-directory>" >&2
    exit 2
fi
[ "$(id -u)" = 0 ] || { echo 'run in a disposable target container as root' >&2; exit 2; }
tools="$(cd "$(dirname "$0")" && pwd)"
bundle="$(cd "$1" && pwd)"
python="${PYTHON:-/usr/bin/python3.12}"
dependency_flag=""
if [ "${RPM_NODEPS:-0}" = 1 ]; then dependency_flag="--nodeps"; fi
mkdir "$2"
scratch="$(cd "$2" && pwd)"
PYTHON="$python" bash "$tools/build-rpm-source.sh" "$bundle" "$scratch/source"
version="$("$python" -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$bundle/manifest.json")"
for prefix in /opt/dsl41 /srv/dsl41-rpm-check; do
    [ ! -e "$prefix" ] && [ ! -L "$prefix" ] || { echo "prefix already exists: $prefix" >&2; exit 2; }
    for profile in base ui; do
        top="$scratch/$(basename "$prefix")-$profile"
        mkdir -p "$top"/{BUILD,RPMS,SOURCES,SPECS,SRPMS}
        cp "$scratch/source/"*.tar.gz "$top/SOURCES/"
        for revision in 1 2; do
            rpmbuild -bb "$tools/dsl41-offline.spec" --define "_topdir $top" \
                --define "dsl41_version $version" --define "dsl41_release $revision" \
                --define "dsl41_prefix $prefix" --define "dsl41_profile $profile" \
                --define "dsl41_python $python"
        done
        first="$(find "$top/RPMS" -name "dsl41-offline-$version-1.*.rpm")"
        second="$(find "$top/RPMS" -name "dsl41-offline-$version-2.*.rpm")"
        [ -n "$first" ] && [ -n "$second" ]
        # The harness is isolated; generated state remains after every erase (DL-389).
        mkdir -p "$prefix/bin"
        printf '%s\n' 'operator launcher' > "$prefix/bin/dsl41-launch"
        rpm -Uvh ${dependency_flag:+"$dependency_flag"} "$first"
        candidate="$("$python" -I "$tools/bundle.py" candidate "$bundle" "$prefix" "$profile")"
        ln -s ".venvs/$(basename "$candidate")" "$prefix/.venv"
        before="$("$python" -I -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$candidate/.dsl41/receipt.json")"
        # Package presence is not readiness after a failed %post (DL-389).
        cp -a "$bundle" "$top/failed-post-bundle"
        "$python" -I - "$top/failed-post-bundle/manifest.json" <<'PYCODE'
import json
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
manifest = json.loads(path.read_text())
manifest["lifecycle_fault"] = "deliberately incomplete candidate"
path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
PYCODE
        failed_candidate="$("$python" -I "$tools/bundle.py" candidate "$top/failed-post-bundle" "$prefix" "$profile")"
        mkdir "$failed_candidate"
        printf '%s\n' 'incomplete candidate; inspect before retirement' > "$failed_candidate/incomplete"
        PYTHON="$python" bash "$tools/build-rpm-source.sh" "$top/failed-post-bundle" "$top/failed-source"
        cp "$top/failed-source/"*.tar.gz "$top/SOURCES/"
        rpmbuild -bb "$tools/dsl41-offline.spec" --define "_topdir $top" \
            --define "dsl41_version $version" --define 'dsl41_release 3' \
            --define "dsl41_prefix $prefix" --define "dsl41_profile $profile" \
            --define "dsl41_python $python"
        failed_rpm="$(find "$top/RPMS" -name "dsl41-offline-$version-3.*.rpm")"
        if rpm -Uvh ${dependency_flag:+"$dependency_flag"} "$failed_rpm" > "$top/failed-post.log" 2>&1; then
            :
        fi
        grep -q 'scriptlet failed' "$top/failed-post.log"
        case "$(rpm -q --qf '%{RELEASE}' dsl41-offline)" in 3*) ;; *) echo 'failed-post package is not installed' >&2; exit 1;; esac
        [ ! -e "$failed_candidate/.dsl41/receipt.json" ]
        [ "$(cat "$failed_candidate/incomplete")" = 'incomplete candidate; inspect before retirement' ]
        [ "$(readlink "$prefix/.venv")" = ".venvs/$(basename "$candidate")" ]
        "$prefix/.venv/bin/dsl41" --help >/dev/null
        "$python" -I "$tools/bundle.py" verify "$bundle" "$candidate" "$profile"
        echo "ok: failed RPM %post retained prior active runtime: $prefix $profile"
        for package in "$first" "$second" "$second" "$first"; do
            rpm -Uvh ${dependency_flag:+"$dependency_flag"} --replacepkgs --oldpackage "$package"
            [ "$(readlink "$prefix/.venv")" = ".venvs/$(basename "$candidate")" ]
            [ "$(cat "$prefix/bin/dsl41-launch")" = 'operator launcher' ]
            "$python" -I "$tools/bundle.py" verify "$bundle" "$candidate" "$profile"
            rpm -ql dsl41-offline | while IFS= read -r owned; do
                case "$owned" in "$prefix"/delivery/*) ;; *) echo "unexpected RPM ownership: $owned" >&2; exit 1;; esac
            done
        done
        rpm -e ${dependency_flag:+"$dependency_flag"} dsl41-offline
        [ -L "$prefix/.venv" ] && [ -f "$candidate/.dsl41/assets/dsl41-launch" ]
        after="$("$python" -I -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$candidate/.dsl41/receipt.json")"
        [ "$before" = "$after" ]
        "$python" -I "$tools/bundle.py" verify "$bundle" "$candidate" "$profile"
        echo "ok: RPM fresh, upgrade, reinstall, downgrade, erase: $prefix $profile"
        # Only this harness's link is removed; every runtime remains retained (DL-389).
        rm "$prefix/.venv"
    done
done
