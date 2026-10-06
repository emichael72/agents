#!/bin/bash

# Usage: ./greet_user.sh [--name <name>]
# Prints a friendly greeting; without --name, greets the current shell user

NAME="${USER:-$(id -un)}"

while [ $# -gt 0 ]; do
  case "$1" in
    --name)
      NAME="$2"
      shift 2
      ;;
    *)
      echo "Unknown option: $1"
      exit 1
      ;;
  esac
done

# Each agent sets AGENT_NAME when it runs a tool
if [ -n "$AGENT_NAME" ]; then
  echo "Hello, $NAME! Greetings from the $AGENT_NAME."
else
  echo "Hello, $NAME!"
fi
