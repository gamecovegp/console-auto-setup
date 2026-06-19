#!/usr/bin/env bash
# Launch the CAS front-end (Linux/macOS). Needs python3 (+ tkinter) and adb on PATH.
cd "$(dirname "$0")" && exec python3 -m cas "$@"
