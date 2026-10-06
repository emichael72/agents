#!/bin/bash

# Usage: ./df.sh [<allowed name>/<path>]
# Prints the total size of a file or folder (default: tools). The path must be inside
# context/paths.json's folders.

TARGET="${1:-tools}"

# Check the path against context/paths.json (prints "<absolute path><TAB><path as shown>")
TOOLS_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RESOLVED="$(python3 "$TOOLS_DIR/fs_gate/fs_gate.py" "$TARGET")" || { echo "$RESOLVED"; exit 1; }
IFS=$'\t' read -r TARGET SHOWN <<< "$RESOLVED"

SIZE=$(du -sh -- "$TARGET" 2>/dev/null | cut -f1)
FILES=$(find "$TARGET" -type f 2>/dev/null | wc -l)
echo "$SHOWN: $SIZE in $FILES files"
