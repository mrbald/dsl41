#!/usr/bin/env bash
# Both profiles qualify the same delivery, without activation (DL-389).
set -euo pipefail
if [ "$#" -ne 2 ]; then
    echo "usage: $0 <bundle> <qualification-prefix>" >&2
    exit 2
fi
tools="$(cd "$(dirname "$0")" && pwd)"
for profile in base ui; do
    bash "$tools/prepare.sh" "$1" "$2" "$profile"
    bash "$tools/prepare.sh" "$1" "$2" "$profile"
done
