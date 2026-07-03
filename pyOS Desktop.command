#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$DIR/pyOSgui.py" ]; then
    cd "$DIR"
elif [ -f "$DIR/source/pyOS/pyOSgui.py" ]; then
    cd "$DIR/source/pyOS"
else
    echo "Could not locate pyOSgui.py near this launcher." >&2
    exit 1
fi
python3 pyOSgui.py
