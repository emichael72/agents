#!/bin/bash

# Usage: ./list_files.sh [path]
# Lists a folder (default: the tools folder), folders first, at most 200 entries

TARGET="${1:-.}"
MAX_ENTRIES=200

if [ ! -d "$TARGET" ]; then
  echo "Error: '$TARGET' is not a folder"
  exit 1
fi

COUNT=$(find "$TARGET" -mindepth 1 -maxdepth 1 | wc -l)
echo "$TARGET ($COUNT entries):"
ls -lAh --group-directories-first --time-style=+%Y-%m-%d "$TARGET" | tail -n +2 | head -n "$MAX_ENTRIES"
if [ "$COUNT" -gt "$MAX_ENTRIES" ]; then
  echo "... and $((COUNT - MAX_ENTRIES)) more"
fi
