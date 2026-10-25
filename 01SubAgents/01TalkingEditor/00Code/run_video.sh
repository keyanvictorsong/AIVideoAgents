#!/bin/sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -x "$SCRIPT_DIR/../../../.venv/bin/python" ]; then
  EDITOR_PYTHON="$SCRIPT_DIR/../../../.venv/bin/python"
elif [ -x "$SCRIPT_DIR/.venv/bin/python" ]; then
  EDITOR_PYTHON="$SCRIPT_DIR/.venv/bin/python"
else
  printf '%s\n' 'TalkingEditor environment is missing. Install from the package root or follow 00Code/README.md.' >&2
  exit 1
fi
exec "$EDITOR_PYTHON" -m talking_editor.delivery "$@"
