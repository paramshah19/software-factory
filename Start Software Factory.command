#!/bin/bash
# Double-click to start the Software Factory (macOS), or run: bash "Start Software Factory.command" (Linux).
# It opens the dashboard as an app window. Options after the file name: --browser  --phone  --awake  --no-open
cd "$(dirname "$0")" || exit 1
if ! command -v uv >/dev/null 2>&1; then
  echo 'The Software Factory needs "uv". Install it from https://docs.astral.sh/uv/getting-started/installation/ and start this again.'
  read -r -p "Press Enter to close. " _
  exit 1
fi
if ! uv run --no-project python scripts/sf.py start "$@"; then
  echo
  read -r -p "Something went wrong (see above). Press Enter to close. " _
fi
