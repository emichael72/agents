#!/bin/bash

# Usage: ./current_time.sh [--timezone <Area/City>]
# Prints the current date and time, in the given IANA time zone or the machine's own

TIMEZONE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --timezone)
      TIMEZONE="$2"
      shift 2
      ;;
    *)
      echo "Unknown option: $1"
      exit 1
      ;;
  esac
done

if [ -n "$TIMEZONE" ]; then
  if [ ! -f "/usr/share/zoneinfo/$TIMEZONE" ]; then
    echo "Error: unknown time zone '$TIMEZONE' (use an IANA name such as Europe/London)"
    exit 1
  fi
  export TZ="$TIMEZONE"
fi

date '+%Y-%m-%d %H:%M:%S %Z (UTC%:z), %A'
