#!/bin/bash

# Usage: ./search_text.sh <pattern> [<allowed name>/<path>]
# Finds lines matching a regular expression in a file or folder (recursively; default: tools), at
# most 50 matches. The path must be inside context/paths.json's folders.

PATTERN="$1"
TARGET="${2:-tools}"
MAX_MATCHES=50

if [ -z "$PATTERN" ]; then
  echo "Error: a search pattern is required"
  exit 1
fi
# Check the path against context/paths.json (prints "<absolute path><TAB><path as shown>")
TOOLS_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RESOLVED="$(python3 "$TOOLS_DIR/fs_gate/fs_gate.py" "$TARGET")" || { echo "$RESOLVED"; exit 1; }
IFS=$'\t' read -r TARGET SHOWN <<< "$RESOLVED"

# -I skips binary files; -n adds line numbers; -H always names the file; -E uses extended regular
# expressions. Paths are then shown as the model gave them (<allowed name>/...).
MATCHES=$(grep -rInHE --exclude-dir=.git --exclude-dir=node_modules --exclude-dir=.venv \
  --exclude-dir=__pycache__ -e "$PATTERN" -- "$TARGET" 2>&1)
STATUS=$?
MATCHES="${MATCHES//$TARGET/$SHOWN}"

if [ $STATUS -eq 1 ]; then
  echo "No matches for '$PATTERN' in $SHOWN"
  exit 0
elif [ $STATUS -ne 0 ]; then
  echo "Error: $MATCHES"
  exit 1
fi

TOTAL=$(printf '%s\n' "$MATCHES" | wc -l)
printf '%s\n' "$MATCHES" | head -n "$MAX_MATCHES"
if [ "$TOTAL" -gt "$MAX_MATCHES" ]; then
  echo "... $TOTAL matches in total; narrow the pattern or the path to see the rest"
fi
