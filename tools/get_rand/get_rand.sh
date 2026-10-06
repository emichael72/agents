#!/bin/bash

# Usage: ./get_rand.sh [max]
# Generates a random number up to the given max (default 100)

MAX=${1:-100}

if ! [[ "$MAX" =~ ^[0-9]+$ ]] || [ "$MAX" -le 0 ]; then
  echo "Error: max must be a positive integer"
  exit 1
fi

# RANDOM gives values 0–32767, so mod works fine
RANDOM_NUMBER=$(( (RANDOM % (10#$MAX)) + 1 ))
echo "Random number (1-$MAX): $RANDOM_NUMBER"
