#!/bin/bash

# Usage: ./count_lines.sh <file>
# Counts number of lines in the file

if [ ! -f "$1" ]; then
  echo "Error: file not found"
  exit 1
fi

LINES=$(wc -l < "$1")
echo "File '$1' has $LINES lines."
