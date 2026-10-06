#!/bin/bash

# Usage: ./doxy.sh <allowed name>/<path> [...]
# Checks that C/C++ sources and headers are documented with Doxygen, using the settings in
# Doxyfile.check (next to this script). Each path is a file or a folder (searched recursively)
# inside tools/allowed_paths.json's folders; one argument may also hold several paths separated by
# spaces. Documentation problems are the result, not a failure: the script exits nonzero only when
# the check itself cannot run.

TOOL_DIR="$(cd "$(dirname "$0")" && pwd)"
TOOLS_DIR="$(dirname "$TOOL_DIR")"
CONFIG="$TOOL_DIR/Doxyfile.check"
MAX_LINES=100
EXTENSIONS="c|h|cc|cpp|hpp|cxx|hh"

if ! command -v doxygen >/dev/null 2>&1; then
  echo "Error: doxygen is not installed (Fedora: sudo dnf install doxygen; Debian/Ubuntu: sudo apt install doxygen)"
  exit 1
fi
if [ ! -f "$CONFIG" ]; then
  echo "Error: $CONFIG not found"
  exit 1
fi

# Several paths may arrive in one argument (the agents pass a single string)
PATHS=()
for arg in "$@"; do
  read -ra words <<< "$arg"
  PATHS+=("${words[@]}")
done
if [ ${#PATHS[@]} -eq 0 ]; then
  echo "Error: give at least one C/C++ source or header file, or a folder"
  exit 1
fi

INPUT=""
FILE_LIST=()
ABSOLUTE=()  # Each path as resolved, and as shown in the report
SHOWN=()
for path in "${PATHS[@]}"; do
  # Check the path against tools/allowed_paths.json (prints "<absolute path><TAB><path as shown>")
  resolved="$(python3 "$TOOLS_DIR/allowed_paths.py" "$path")" || { echo "$resolved"; exit 1; }
  IFS=$'\t' read -r target shown <<< "$resolved"
  if [ -d "$target" ]; then
    mapfile -t -O "${#FILE_LIST[@]}" FILE_LIST < <(find "$target" -type f -regextype posix-extended -regex ".*\.($EXTENSIONS)$")
  elif ! [[ "$target" =~ \.($EXTENSIONS)$ ]]; then
    echo "Error: '$path' is not a C/C++ source or header (.c, .h, .cc, .cpp, .hpp, .cxx, .hh)"
    exit 1
  else
    FILE_LIST+=("$target")
  fi
  ABSOLUTE+=("$target")
  SHOWN+=("$shown")
  INPUT+=" \"$target\""
done
FILES=${#FILE_LIST[@]}
if [ "$FILES" -eq 0 ]; then
  echo "Error: no C/C++ sources or headers found in: ${PATHS[*]}"
  exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# The settings from Doxyfile.check, then the overrides (later lines win)
{
  cat "$CONFIG"
  echo
  echo "INPUT = $INPUT"
  echo "OUTPUT_DIRECTORY = \"$WORK/out\""
  echo "GENERATE_XML = YES"  # Doxygen needs one output format; XML goes to the temporary folder
  echo "WARN_LOGFILE = \"$WORK/warnings.log\""
  echo 'WARN_FORMAT = "$file:$line: $text"'
} | doxygen - >"$WORK/doxygen.out" 2>&1
STATUS=$?

# With EXTRACT_ALL = NO, Doxygen silently skips everything in a file without a @file block, so
# report those files here, in Doxygen's format
for file in "${FILE_LIST[@]}"; do
  if ! grep -qE '[@\\]file\b' "$file"; then
    echo "$(realpath "$file"):1: error: File has no @file documentation block, so Doxygen does not check its contents." >>"$WORK/warnings.log"
  fi
done

# Show paths as the model gave them (<allowed name>/...)
PROBLEMS="$(sed -e 's|: warning: |: |' "$WORK/warnings.log" 2>/dev/null)"
for i in "${!ABSOLUTE[@]}"; do
  PROBLEMS="${PROBLEMS//${ABSOLUTE[$i]}/${SHOWN[$i]}}"
done
COUNT=$(grep -cE '^[^ ].*:[0-9]+: ' <<< "$PROBLEMS")
VERSION="$(doxygen --version)"

if [ "$COUNT" -eq 0 ]; then
  if [ "$STATUS" -ne 0 ]; then
    echo "Error: doxygen failed:"
    head -n 20 "$WORK/doxygen.out"
    exit 1
  fi
  echo "All documented: $FILES file(s) checked, no Doxygen warnings (doxygen $VERSION, Doxyfile.check)."
  exit 0
fi

echo "Documentation problems: $COUNT in $FILES file(s) checked (doxygen $VERSION, Doxyfile.check):"
head -n "$MAX_LINES" <<< "$PROBLEMS"
LINES=$(wc -l <<< "$PROBLEMS")
if [ "$LINES" -gt "$MAX_LINES" ]; then
  echo "... $((LINES - MAX_LINES)) more lines not shown"
fi
