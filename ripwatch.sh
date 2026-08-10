#!/usr/bin/env bash
#
# ripwatch.sh — entry point for RipWatch.
# Verifies prerequisites, then hands off to ripwatch.py (stdlib-only Python).
#
# Usage:
#   ./ripwatch.sh --input ~/rips/firefly-s1 --output "/Volumes/media/TV Shows"
#
set -euo pipefail

for bin in ffmpeg ffprobe python3; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "error: '$bin' not found on PATH." >&2
    if [[ "$bin" != "python3" ]]; then
      echo "  install ffmpeg (includes ffprobe) with: brew install ffmpeg" >&2
    fi
    exit 1
  fi
done

exec python3 "$(cd "$(dirname "$0")" && pwd)/ripwatch.py" "$@"