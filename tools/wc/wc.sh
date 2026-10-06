#!/bin/bash

# Usage: ./wc.sh <allowed name>/<file>
# Counts number of lines in the file. The file must be inside context/paths.json's folders.

TARGET="$1"
if [ -z "$TARGET" ]; then
  echo "Error: give a file, e.g. core_dump/README.md"
  exit 1
fi

# Check the path against context/paths.json (prints "<absolute path><TAB><path as shown>")
TOOLS_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RESOLVED="$(python3 "$TOOLS_DIR/fs_gate/fs_gate.py" "$TARGET" --file)" || { echo "$RESOLVED"; exit 1; }
IFS=$'\t' read -r TARGET SHOWN <<< "$RESOLVED"

LINES=$(wc -l < "$TARGET")
echo "File '$SHOWN' has $LINES lines."
