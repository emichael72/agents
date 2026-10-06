#!/bin/bash

# Usage: ./git_log.sh [--count N] [--path <allowed name>/<folder>]
# Shows the most recent commits of the git repository that contains the folder (default: tools,
# the agents repository). The folder must be inside tools/allowed_paths.json's folders.

COUNT=10
TARGET="tools"

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
# Check the path against tools/allowed_paths.json (prints "<absolute path><TAB><path as shown>")
TOOLS_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RESOLVED="$(python3 "$TOOLS_DIR/allowed_paths.py" "$TARGET" --dir)" || { echo "$RESOLVED"; exit 1; }
IFS=$'\t' read -r TARGET SHOWN <<< "$RESOLVED"
if ! git -C "$TARGET" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "Error: '$SHOWN' is not inside a git repository"
  exit 1
fi

git -C "$TARGET" log -n "$COUNT" --date=short --format='%h %ad %s'
