#!/bin/sh
ROOT=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
exec python3 "$ROOT/pixpack.py" "$@"
