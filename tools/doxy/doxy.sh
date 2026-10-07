#!/bin/bash
#
# doxy.sh
# Purpose:
#   Checks that C/C++ sources and headers are documented with Doxygen, using the settings in
#   Doxyfile.check (next to this script). Each path is a file or a folder (searched recursively)
#   inside context/paths.json's folders; one argument may also hold several paths separated by
#   spaces, as the agents pass them. Documentation problems are the result, not a failure: the
#   script exits nonzero only when the check itself cannot run.
# Usage: ./doxy.sh --help
#

set -euo pipefail # Strict mode: exit on errors, unset vars, failed pipes

SCRIPT_VERSION="1.0.0"
SCRIPT_PATH="${BASH_SOURCE[0]:-$0}" # zsh sets $0 to the script outside functions

# ============================================================================
# Globals and configuration
# ============================================================================

MAX_LINES=100                      # Most report lines shown after the summary line
EXTENSIONS="c|h|cc|cpp|hpp|cxx|hh" # File extensions checked, as an extended regex alternation
REPO_ROOT=""                       # Set by init_paths: the repository root
TOOL_DIR=""                        # Set by init_paths: this script's folder
CONFIG=""                          # Set by init_paths: the Doxygen settings, Doxyfile.check
FS_GATE=""                         # Set by init_paths: gatekeepers/fs/fs_gate.py (run as a module)
WORK_DIR=""                        # Set by main: Doxygen's temporary output, removed on exit
INPUT_PATHS=()                     # The paths to check, as given
FILE_LIST=()                       # Every C/C++ file found under them
RESOLVED=()                        # Each given path, resolved to an absolute path
SHOWN=()                           # Each given path as the report shows it (<allowed name>/...)
DOXY_INPUT=""                      # Doxygen's INPUT value: the resolved paths, quoted

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
# @brief Print usage information.
#
print_usage() {
    local script
    script="$(basename "${SCRIPT_PATH}")"

    echo ""
    echo "Doxygen documentation check (v${SCRIPT_VERSION})"
    echo "Usage: ${script} [OPTION]... PATH..."
    echo ""
    echo "Checks that C/C++ sources and headers (.c, .h, .cc, .cpp, .hpp, .cxx, .hh) are"
    echo "documented with Doxygen, using Doxyfile.check. Each PATH is a file or a folder (searched"
    echo "recursively) starting with a folder name from context/paths.json (FS_GATE_PATHS overrides"
    echo "that file); one argument may hold several paths separated by spaces."
    echo ""
    echo "Options"
    echo "    -v, --version               Print the version and exit"
    echo "    -h, --help                  This message"
    echo ""
    echo "Exit status"
    echo "    0 when the check ran, whether or not it found documentation problems;"
    echo "    1 when it could not."
    echo ""
    echo "Example"
    echo "    ./${script} \"core_dump/src/pi.c core_dump/include/pi.h\""
    echo ""
}

#
# @brief Parse CLI arguments: options, then the paths to check.
# @return 0 on success, 1 on an unknown option
#
parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
        -v | --version)
            log OUT "%s %s" "$(basename "${SCRIPT_PATH}")" "${SCRIPT_VERSION}"
            exit 0
            ;;
        -h | --help)
            print_usage
            exit 0
            ;;
        --)
            shift
            break
            ;;
        -*)
            log ERROR "Unknown option: %s (try --help)" "$1"
            return 1
            ;;
        *)
            add_input_paths "$1"
            shift
            ;;
        esac
    done

    while [[ $# -gt 0 ]]; do
        add_input_paths "$1"
        shift
    done
    return 0
}

#
# @brief Split one argument on spaces, tabs and newlines and add each word to INPUT_PATHS.
# @param $1 One or more paths
#
add_input_paths() {
    local word
    while IFS= read -r word; do
        if [[ -n "${word}" ]]; then
            INPUT_PATHS+=("${word}")
        fi
    done < <(printf '%s\n' "$1" | tr -s ' \t' '\n')
    return 0
}

#
# @brief Verify that the commands this script runs are installed.
# @return 0 on success, 1 if any is missing
#
verify_dependencies() {
    local cmd
    local missing=()

    if ! command -v doxygen >/dev/null 2>&1; then
        log ERROR "doxygen is not installed (%s)" \
            "Fedora: sudo dnf install doxygen; Debian/Ubuntu: sudo apt install doxygen"
        return 1
    fi

    for cmd in python3 find grep sed head wc tr realpath mktemp; do
        if ! command -v "${cmd}" >/dev/null 2>&1; then
            missing+=("${cmd}")
        fi
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        log ERROR "Missing required commands: %s" "${missing[*]}"
        return 1
    fi
    return 0
}

#
# @brief Resolve the files this script uses, from its own location.
# @return 0 on success, 1 if one is missing
#
init_paths() {
    REPO_ROOT="$(find_repo_root)" || return 1
    TOOL_DIR="$(cd "$(dirname "${SCRIPT_PATH}")" && pwd)"
    CONFIG="${TOOL_DIR}/Doxyfile.check"
    FS_GATE="${REPO_ROOT}/gatekeepers/fs/fs_gate.py"

    if [[ ! -f "${CONFIG}" ]]; then
        log ERROR "%s not found" "${CONFIG}"
        return 1
    fi
    if [[ ! -f "${FS_GATE}" ]]; then
        log ERROR "%s not found" "${FS_GATE}"
        return 1
    fi
    return 0
}

#
# @brief Check each path in INPUT_PATHS against context/paths.json and collect the files to check.
#
#   Fills FILE_LIST, RESOLVED, SHOWN and DOXY_INPUT.
#
# @return 0 on success, 1 if a path is not allowed, not C/C++, or no files were found
#
resolve_inputs() {
    local item resolved target shown file
    local source_regex="\\.(${EXTENSIONS})\$"

    if [[ ${#INPUT_PATHS[@]} -eq 0 ]]; then
        log ERROR "give at least one C/C++ source or header file, or a folder"
        return 1
    fi

    for item in "${INPUT_PATHS[@]}"; do
        # The gate prints "<absolute path><TAB><path as shown>", or "Error: <reason>"
        if ! resolved="$(PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
            python3 -m gatekeepers.fs.fs_gate "${item}")"; then
            log ERROR "${resolved#Error: }"
            return 1
        fi
        IFS=$'\t' read -r target shown <<<"${resolved}"

        if [[ -d "${target}" ]]; then
            while IFS= read -r file; do
                FILE_LIST+=("${file}")
            done < <(find "${target}" -type f -regextype posix-extended -regex ".*${source_regex}")
        elif [[ "${target}" =~ ${source_regex} ]]; then
            FILE_LIST+=("${target}")
        else
            log ERROR "'%s' is not a C/C++ source or header (%s)" "${item}" \
                ".c, .h, .cc, .cpp, .hpp, .cxx, .hh"
            return 1
        fi
        RESOLVED+=("${target}")
        SHOWN+=("${shown}")
        DOXY_INPUT+=" \"${target}\""
    done

    if [[ ${#FILE_LIST[@]} -eq 0 ]]; then
        log ERROR "no C/C++ sources or headers found in: %s" "${INPUT_PATHS[*]}"
        return 1
    fi
    return 0
}

#
# @brief Run Doxygen with Doxyfile.check and this run's overrides (later lines win).
# @return Doxygen's exit status
#
run_doxygen() {
    local rc=0

    {
        cat "${CONFIG}"
        echo
        echo "INPUT = ${DOXY_INPUT}"
        echo "OUTPUT_DIRECTORY = \"${WORK_DIR}/out\""
        echo "GENERATE_XML = YES" # Doxygen needs one output format; XML goes to WORK_DIR
        echo "WARN_LOGFILE = \"${WORK_DIR}/warnings.log\""
        # shellcheck disable=SC2016 # Doxygen's own placeholders, which must not expand here
        echo 'WARN_FORMAT = "$file:$line: $text"'
    } | doxygen - >"${WORK_DIR}/doxygen.out" 2>&1 || rc=$?
    return "${rc}"
}

#
# @brief Report each file without a @file block in Doxygen's warning log.
#
#   With EXTRACT_ALL = NO, Doxygen silently skips everything in such a file, so it would
#   otherwise pass unchecked.
#
report_missing_file_blocks() {
    local file

    for file in "${FILE_LIST[@]}"; do
        if ! grep -qE '[@\\]file\b' "${file}"; then
            printf '%s:1: error: %s\n' "$(realpath "${file}")" \
                "File has no @file documentation block, so Doxygen does not check its contents." \
                >>"${WORK_DIR}/warnings.log"
        fi
    done
}

#
# @brief Print the result: that everything is documented, or the problems found.
# @param $1 Doxygen's exit status
# @return 0 when the check ran, 1 when Doxygen failed without reporting problems
#
print_report() {
    local doxygen_rc="$1"
    local files="${#FILE_LIST[@]}"
    local problems="" version count lines i=0

    if [[ -f "${WORK_DIR}/warnings.log" ]]; then
        problems="$(sed -e 's|: warning: |: |' "${WORK_DIR}/warnings.log")"
    fi

    # Show paths as the model gave them (<allowed name>/...)
    while [[ ${i} -lt ${#RESOLVED[@]} ]]; do
        problems="${problems//"${RESOLVED[i]}"/"${SHOWN[i]}"}"
        i=$((i + 1))
    done

    count="$(grep -cE '^[^ ].*:[0-9]+: ' <<<"${problems}" || true)"
    version="$(doxygen --version)"

    if [[ ${count} -eq 0 ]]; then
        if [[ ${doxygen_rc} -ne 0 ]]; then
            log ERROR "doxygen failed:\n%s" "$(head -n 20 "${WORK_DIR}/doxygen.out")"
            return 1
        fi
        log OUT "All documented: %s file(s) checked, %s (doxygen %s, Doxyfile.check)." \
            "${files}" "no Doxygen warnings" "${version}"
        return 0
    fi

    log OUT "Documentation problems: %s in %s file(s) checked (doxygen %s, Doxyfile.check):" \
        "${count}" "${files}" "${version}"
    log OUT "$(head -n "${MAX_LINES}" <<<"${problems}")"
    lines="$(wc -l <<<"${problems}")"
    if [[ ${lines} -gt ${MAX_LINES} ]]; then
        log OUT "... %s more lines not shown" "$((lines - MAX_LINES))"
    fi
    return 0
}

#
# @brief Remove the temporary folder.
#
cleanup() {
    if [[ -n "${WORK_DIR}" ]]; then
        rm -rf "${WORK_DIR}"
    fi
}

#
# @brief Parse the arguments, check the environment and the paths, then run the check.
# @return 0 when the check ran (documentation problems included), 1 when it could not
#
main() {
    local doxygen_rc=0

    # Bash's array indexing (from 0) when run by zsh
    if [[ -n "${ZSH_VERSION:-}" ]]; then
        setopt KSH_ARRAYS
    fi

    parse_args "$@" || return 1
    verify_dependencies || return 1
    init_paths || return 1
    resolve_inputs || return 1

    WORK_DIR="$(mktemp -d)"
    trap cleanup EXIT

    run_doxygen || doxygen_rc=$?
    report_missing_file_blocks
    print_report "${doxygen_rc}"
}

main "$@"
