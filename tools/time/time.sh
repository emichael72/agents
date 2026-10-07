#!/bin/bash
#
# time.sh
# Purpose:
#   Prints the current date and time, in a given IANA time zone or in the machine's own.
# Usage: ./time.sh --help
#

set -euo pipefail # Strict mode: exit on errors, unset vars, failed pipes

SCRIPT_VERSION="1.0.0"
SCRIPT_PATH="${BASH_SOURCE[0]:-$0}" # zsh sets $0 to the script outside functions

# ============================================================================
# Globals and configuration (command-line flags override these)
# ============================================================================

ZONEINFO_DIR="/usr/share/zoneinfo"               # Where the IANA time zones are installed
DATE_FORMAT="+%Y-%m-%d %H:%M:%S %Z (UTC%:z), %A" # 2026-10-07 15:07:16 UTC (UTC+00:00), Wednesday
TIMEZONE=""                                      # --timezone; empty = the machine's local time

#
# @brief Print a message: the tool's result to stdout, errors to stderr.
# @param level OUT | ERROR
# @param ...   printf-style format and arguments, or a single literal message
#
log() {
    local level="$1"
    shift
    local message
    if [[ $# -gt 1 ]]; then
        # shellcheck disable=SC2059 # The first argument is the format
        message="$(printf -- "$@")"
    else
        message="${1:-}"
    fi

    case "${level}" in
    ERROR) printf '%s\n' "Error: ${message}" >&2 ;;
    *) printf '%s\n' "${message}" ;;
    esac
    return 0
}

#
# @brief Print usage information.
#
print_usage() {
    local script
    script="$(basename "${SCRIPT_PATH}")"

    echo ""
    echo "Current date and time (v${SCRIPT_VERSION})"
    echo "Usage: ${script} [OPTION]..."
    echo ""
    echo "Options"
    echo "    -t, --timezone <zone>       IANA time zone, e.g. Europe/London or Asia/Tokyo"
    echo "                                (also --timezone=<zone>, as the agents pass it)"
    echo "                                (default: the machine's local time)"
    echo "    -v, --version               Print the version and exit"
    echo "    -h, --help                  This message"
    echo ""
    echo "Example"
    echo "    ./${script} --timezone Asia/Tokyo"
    echo ""
}

#
# @brief Parse CLI arguments and populate the global options.
# @return 0 on success, 1 on an unknown option or a missing value
#
parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
        -t | --timezone)
            if [[ $# -lt 2 ]]; then
                log ERROR "%s requires a time zone (try --help)" "$1"
                return 1
            fi
            TIMEZONE="$2"
            shift 2
            ;;
        --timezone=*)
            TIMEZONE="${1#*=}"
            shift
            ;;
        --)
            shift
            ;;
        -v | --version)
            log OUT "%s %s" "$(basename "${SCRIPT_PATH}")" "${SCRIPT_VERSION}"
            exit 0
            ;;
        -h | --help)
            print_usage
            exit 0
            ;;
        *)
            log ERROR "Unknown option: %s (try --help)" "$1"
            return 1
            ;;
        esac
    done
    return 0
}

#
# @brief Select the requested time zone for date, when one was given.
# @return 0 on success, 1 if the time zone is not installed
#
apply_timezone() {
    if [[ -z "${TIMEZONE}" ]]; then
        return 0
    fi
    if [[ ! -f "${ZONEINFO_DIR}/${TIMEZONE}" ]]; then
        log ERROR "unknown time zone '%s' (use an IANA name such as Europe/London)" "${TIMEZONE}"
        return 1
    fi
    export TZ="${TIMEZONE}"
    return 0
}

#
# @brief Parse the arguments, select the time zone and print the date and time.
# @return 0 on success, 1 on a bad argument or time zone
#
main() {
    parse_args "$@" || return 1
    apply_timezone || return 1
    log OUT "$(date "${DATE_FORMAT}")"
}

main "$@"
