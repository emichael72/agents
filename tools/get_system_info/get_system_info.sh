#!/bin/bash

# Usage: ./get_system_info.sh
# Prints information about the machine running the tools

echo "hostname=$(uname -n)"
echo "system=$(uname -s)"
echo "release=$(uname -r)"
echo "machine=$(uname -m)"
