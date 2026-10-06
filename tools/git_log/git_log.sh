#!/bin/bash

# Usage: ./git_log.sh [--count N] [--path <folder>]
# Shows the most recent commits of the git repository that contains the folder

COUNT=10
TARGET="."

while [ $# -gt 0 ]; do
  case "$1" in
    --count)
      COUNT="$2"
      shift 2
      ;;
    --path)
      TARGET="$2"
      shift 2
      ;;
    *)
      echo "Unknown option: $1"
      exit 1
      ;;
  esac
done

if ! [[ "$COUNT" =~ ^[0-9]+$ ]] || [ "$COUNT" -lt 1 ] || [ "$COUNT" -gt 50 ]; then
  echo "Error: count must be between 1 and 50"
  exit 1
fi
if ! git -C "$TARGET" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "Error: '$TARGET' is not inside a git repository"
  exit 1
fi

git -C "$TARGET" log -n "$COUNT" --date=short --format='%h %ad %s'
