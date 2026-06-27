#!/usr/bin/env bash
# Launch the CAS front-end (Linux/macOS). Needs python3 (+ tkinter) and adb on PATH.
# Script lives in scripts/; run from the repo root so `python3 -m cas` finds the package.
cd "$(dirname "$0")/.." && exec python3 -m cas "$@"
