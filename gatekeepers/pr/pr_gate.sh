#!/bin/bash
#
# pr_gate.sh
# Purpose:
#   Runs pr_gate.py, the pull request gate's command line, with the repository's shared Python
#   environment (.venv). Options before the command belong to this script; the command and
#   everything after it are passed to pr_gate.py unchanged.
# Usage: ./pr_gate.sh --help
#

set -euo pipefail # Strict mode: exit on errors, unset vars, failed pipes

SCRIPT_VERSION="1.0.0"
SCRIPT_PATH="${BASH_SOURCE[0]:-$0}" # zsh sets $0 to the script outside functions

# ============================================================================
# Globals and configuration (command-line flags override these)
# ============================================================================

REPO_ROOT=""    # Set by init_paths: the repository root
GATE_DIR=""     # Set by init_paths: this script's folder
PYTHON=""       # Set by init_paths: the shared .venv's interpreter
SHOW_HELP=false # -h/--help: this script's usage, then pr_gate.py's
GATE_ARGS=()    # The command and its arguments, for pr_gate.py

#
# @brief Print a message: output to stdout, errors to stderr.
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
# @brief Print the repository root: the nearest folder above this script holding pyproject.toml.
# @return 0 on success, 1 if there is no such folder
#
find_repo_root() {
    local dir
    dir="$(cd "$(dirname "${SCRIPT_PATH}")" && pwd)"
    while [[ ! -f "${dir}/pyproject.toml" ]]; do
        if [[ "${dir}" == "/" ]]; then
            log ERROR "no pyproject.toml above %s (not in the agents repository?)" "${SCRIPT_PATH}"
            return 1
        fi
        dir="$(dirname "${dir}")"
    done
    printf '%s\n' "${dir}"
}

#
# @brief Print usage information for this wrapper; pr_gate.py's own help follows it.
#
print_usage() {
    local script
    script="$(basename "${SCRIPT_PATH}")"

    echo ""
    echo "Pull request gate (v${SCRIPT_VERSION})"
    echo "Usage: ${script} [OPTION]... <command> [ARG]..."
    echo ""
    echo "Runs pr_gate.py with the repository's .venv. The command and its arguments go to"
    echo "pr_gate.py unchanged; '${script} <command> --help' shows a command's options."
    echo ""
    echo "Options (before the command)"
    echo "    -v, --version               Print the version and exit"
    echo "    -h, --help                  This message, then pr_gate.py's commands"
    echo "        --                      End of this script's options"
    echo ""
    echo "Example"
    echo "    ./${script} status --pr 1"
    echo ""
}

#
# @brief Parse this script's options; the first other argument starts pr_gate.py's arguments.
# @return 0 on success
#
parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
        -v | --version)
            log OUT "%s %s" "$(basename "${SCRIPT_PATH}")" "${SCRIPT_VERSION}"
            exit 0
            ;;
        -h | --help)
            SHOW_HELP=true
            shift
            ;;
        --)
            shift
            break
            ;;
        *)
            break
            ;;
        esac
    done

    GATE_ARGS=("$@")
    return 0
}

#
# @brief Locate pr_gate.py and the shared .venv's Python, from this script's location.
# @return 0 on success, 1 if the .venv is missing
#
init_paths() {
    REPO_ROOT="$(find_repo_root)" || return 1
    GATE_DIR="$(cd "$(dirname "${SCRIPT_PATH}")" && pwd)"
    PYTHON="${REPO_ROOT}/.venv/bin/python"

    if [[ ! -x "${PYTHON}" ]]; then
        log ERROR "%s not found; run ./install.sh from the repository root" "${PYTHON}"
        return 1
    fi
    return 0
}

#
# @brief Parse the options, find the .venv, then replace this shell with pr_gate.py.
# @return 1 if pr_gate.py cannot be started; otherwise pr_gate.py's own exit status
#
main() {
    parse_args "$@" || return 1

    if [[ "${SHOW_HELP}" == true ]]; then
        print_usage
        init_paths || return 1
        exec "${PYTHON}" "${GATE_DIR}/pr_gate.py" --help
    fi

    init_paths || return 1
    exec "${PYTHON}" "${GATE_DIR}/pr_gate.py" "${GATE_ARGS[@]}"
}

main "$@"
