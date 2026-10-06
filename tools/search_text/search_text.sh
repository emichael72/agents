#!/bin/bash

# Usage: ./search_text.sh <pattern> [path]
# Finds lines matching a regular expression in a file or folder (recursively), at most 50 matches

PATTERN="$1"
TARGET="${2:-.}"
MAX_MATCHES=50

if [ -z "$PATTERN" ]; then
  echo "Error: a search pattern is required"
  exit 1
fi
if [ ! -e "$TARGET" ]; then
  echo "Error: '$TARGET' does not exist"
  exit 1
fi

# -I skips binary files; -n adds line numbers; -E uses extended regular expressions
MATCHES=$(grep -rInE --exclude-dir=.git --exclude-dir=node_modules --exclude-dir=.venv \
  --exclude-dir=__pycache__ -e "$PATTERN" -- "$TARGET" 2>&1)
STATUS=$?

if [ $STATUS -eq 1 ]; then
  echo "No matches for '$PATTERN' in $TARGET"
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
