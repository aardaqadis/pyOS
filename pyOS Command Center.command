#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$DIR/pyOScli.py" ]; then
    cd "$DIR"
elif [ -f "$DIR/source/pyOS/pyOScli.py" ]; then
    cd "$DIR/source/pyOS"
else
    echo "Could not locate pyOScli.py near this launcher." >&2
    exit 1
fi
python3 pyOScli.py
