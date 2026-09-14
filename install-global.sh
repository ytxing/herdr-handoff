#!/bin/sh
set -eu
BASE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
BIN=${HANDOFF_BIN_DIR:-"$HOME/.local/bin"}
mkdir -p "$BIN"
ln -sf "$BASE/handoff" "$BIN/handoff"
ln -sf "$BASE/handoff.py" "$BIN/handoff.py"
printf 'installed %s/handoff -> %s/handoff\n' "$BASE" "$BIN"
