#!/usr/bin/env bash
# Preparation never changes the active runtime or deployment inputs (DL-389).
set -euo pipefail
if [ "$#" -lt 1 ] || [ "$#" -gt 3 ]; then
    echo "usage: PYTHON=python3.12 $0 <bundle> [prefix=/opt/dsl41] [base|ui]" >&2
    exit 2
fi
tools="$(cd "$(dirname "$0")" && pwd)"
python="${PYTHON:-python3.12}"
exec "$python" -I "$tools/bundle.py" lock "$python" "$tools/prepare-locked.sh" \
    "$1" "${2:-/opt/dsl41}" "${3:-base}"
