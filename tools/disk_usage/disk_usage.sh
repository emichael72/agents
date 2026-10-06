#!/bin/bash

# Usage: ./disk_usage.sh [path]
# Prints the total size of a file or folder (default: the tools folder)

TARGET="${1:-.}"

if [ ! -e "$TARGET" ]; then
  echo "Error: '$TARGET' does not exist"
  exit 1
fi

SIZE=$(du -sh -- "$TARGET" 2>/dev/null | cut -f1)
FILES=$(find "$TARGET" -type f 2>/dev/null | wc -l)
echo "$TARGET: $SIZE in $FILES files"
