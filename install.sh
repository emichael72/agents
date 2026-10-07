#!/usr/bin/env bash
#
# install.sh
# Purpose:
#   Installs everything needed to run the three agents:
#     - A shared Python virtual environment (.venv) for MCPAgent and the Pydantic Agent, with
#       every requirements*.txt in the repository (the agents' and the pr_gate tool's pinned
#       requirements, and the development tools), and the repository's own packages (mcpagent,
#       pydantic_agent, gatekeepers) installed editable from pyproject.toml.
#     - vercel/node_modules for the Vercel Agent, installed exactly as package-lock.json
#       records (npm ci). When the system's Node.js is missing or older than 22.18, a pinned
#       Node.js release is fetched from nodejs.org into .node/ (checksum-verified, no root needed).
#   It runs only on Red Hat family systems that use dnf (RHEL, Fedora, Rocky, AlmaLinux, CentOS
#   Stream and the like). Each step is checked, and the installer stops at the first failure.
#
#   With --gate ACTION it manages the pull request gate's service instead (gatekeepers/pr, the
#   pr-gate systemd user unit): install, uninstall, start, stop, restart, status or logs.
#
#   Exit status: 0 on success, 1 on a missing requirement, an unsupported version or a failed step.
# Usage: ./install.sh --help
#

set -euo pipefail # Strict mode: exit on errors, unset vars, failed pipes

SCRIPT_VERSION="1.0.0"
SCRIPT_PATH="${BASH_SOURCE[0]:-$0}" # zsh sets $0 to the script outside functions

# ============================================================================
# Globals and configuration (command-line flags override these)
# ============================================================================

# Supported systems
OS_RELEASE_FILE="/etc/os-release"     # Its ID or ID_LIKE must name a supported system
OS_SUPPORTED_IDS=(rhel fedora centos) # Red Hat family systems, which use dnf

# The shared Python environment of MCPAgent and the Pydantic Agent
PYTHON_VENV_PATH=".venv"                           # Created by create_python_venv
PYTHON_REQUIRED_MIN_VER="3.10"                     # Oldest Python the agents run on
PYTHON_BIN=""                                      # The venv's interpreter, chosen by find_python
PYTHON_REQUEST="${PYTHON:-}"                       # -p/--python or PYTHON; empty: find_python picks
PYTHON_REQUIREMENTS_FILES=()                       # Set by find_requirements_files
PYTHON_REQUIREMENTS_SKIP=(.venv node_modules .git) # Folders not searched for requirements*.txt

# Checked by verify_python_agents
PYTHON_MODULES_TO_RUN=(mcpagent.server mcpagent.client) # Run with --version
PYTHON_VERIFY_MODULES=( # Imported
    mcpagent pydantic_agent gatekeepers.fs.fs_gate gatekeepers.pr.changes
    pydantic_ai jsonschema httpx httpx2 aiohttp prompt_toolkit rich ruff
)

# The Vercel Agent's Node.js
NODE_PROJECT_PATH="vercel"                              # Holds package-lock.json
NODE_REQUIRED_MIN_VER="22.18"                           # Runs .ts files: no build step
NPM_REQUIRED_MIN_VER="10.0"                             # For a system Node.js
NODE_VERIFY_PACKAGES=(ai @ai-sdk/openai-compatible zod) # Must import after npm ci

# Node.js fetched by install_local_node when the system's is missing or too old
NODE_LOCAL_VERSION="22.23.3"             # The release fetched
NODE_LOCAL_PATH=".node"                  # Where it is unpacked
NODE_DIST_URL="https://nodejs.org/dist"  # Releases and their SHASUMS256.txt
NODE_LOCAL_MIN_GLIBC="2.28"              # The official Linux builds need it
NODE_FETCH_NEEDS=(curl tar xz sha256sum) # Commands that fetch and check it
NODE_PLATFORM=""                         # Set by check_node: linux-x64 or linux-arm64
NODE_FETCH=false                         # Set by check_node: whether to fetch

# The pull request gate's service (gatekeepers/pr)
GATE_PATH="gatekeepers/pr"                 # Holds the unit template and pr_gate.sh
GATE_UNIT="pr-gate"                        # The systemd user unit
GATE_PORT=8000                             # Where the gate serves its pages
GATE_NEEDS=(gh bwrap doxygen clang-format) # Commands the gate's checks use

# Command-line flags
FORCE=false       # -f/--force: recreate the .venv, node_modules and a fetched .node
SKIP_VERCEL=false # --skip-vercel: no Node.js
GATE_ACTION=""    # --gate: manage the gate's service instead of installing
QUIET=false       # -q/--quiet: only errors and warnings

#
# @brief Print a message: the single path for this script's own output.
# @param level INFO | LABEL | OK | FAIL | WARN | ERROR
#   INFO is a line on stdout; LABEL is text on stdout without a newline, a status line that OK
#   (green) or FAIL (red "ERROR") then ends. INFO, LABEL, OK and FAIL are silent with -q.
#   WARN ("Warning: ...") and ERROR are lines on stderr, always shown.
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
    WARN) printf '%s\n' "Warning: ${message}" >&2 ;;
    ERROR) printf '%s\n' "${message}" >&2 ;;
    *)
        if [[ "${QUIET}" == true ]]; then
            return 0
        fi
        case "${level}" in
        LABEL) printf '%s' "${message}" ;;
        OK) printf '\033[32m%s\033[0m\n' "OK" ;;
        FAIL) printf '\033[31m%s\033[0m\n\n' "ERROR" ;;
        *) printf '%s\n' "${message}" ;;
        esac
        ;;
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
    echo "Agents installer (v${SCRIPT_VERSION})"
    echo "Usage: ./${script} [OPTION]..."
    echo ""
    echo "Options"
    echo "    -f, --force                 Recreate the shared .venv, vercel/node_modules and a"
    echo "                                fetched .node"
    echo "    -p, --python <cmd>          Create the .venv with this Python (default:"
    echo "                                python3 if >= ${PYTHON_REQUIRED_MIN_VER}, else the newest"
    echo "                                python3.N in PATH); the PYTHON variable sets it too"
    echo "        --skip-vercel           Skip the Vercel Agent (no Node.js needed)"
    echo "        --gate <action>         Manage the pull request gate's service (gatekeepers/pr)"
    echo "                                instead of installing: install, uninstall, start, stop,"
    echo "                                restart, status or logs"
    echo "    -q, --quiet                 Show only errors and warnings"
    echo "    -v, --version               Print the version and exit"
    echo "    -h, --help                  This message"
    echo ""
    echo "Example"
    echo "    ./${script} --skip-vercel"
    echo ""
}

#
# @brief Parse CLI arguments and populate the global options.
# @return 0 on success, 1 on an unknown option or a missing value
#
parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
        -f | --force)
            FORCE=true
            shift
            ;;
        -p | --python)
            if [[ -z "${2:-}" ]]; then
                log ERROR "%s requires a Python interpreter (try --help)" "$1"
                return 1
            fi
            PYTHON_REQUEST="$2"
            shift 2
            ;;
        --skip-vercel)
            SKIP_VERCEL=true
            shift
            ;;
        --gate)
            if [[ -z "${2:-}" ]]; then
                log ERROR "%s requires an action (%s)" "$1" \
                    "install, uninstall, start, stop, restart, status or logs"
                return 1
            fi
            GATE_ACTION="$2"
            shift 2
            ;;
        -q | --quiet)
            QUIET=true
            shift
            ;;
        -v | --version)
            log INFO "%s %s" "$(basename "${SCRIPT_PATH}")" "${SCRIPT_VERSION}"
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
# @brief Print a task label followed by dot padding, leaving space for a result.
# @param $1  Task label (e.g., "Checking Python >= 3.10").
# @param $2  [optional] Result to end the line with: "OK" or "ERROR".
# @param $3  [optional] Column the dots run to; defaults to 60.
#
print_status_label() {
    local label="$1"
    local result="${2:-}"
    local total_width="${3:-60}"
    local dots=$((total_width - ${#label}))
    local dot_str

    if [[ ${dots} -lt 1 ]]; then
        dots=1
    fi
    dot_str="$(printf "%${dots}s" "")"
    log LABEL "%s %s " "${label}" "${dot_str// /.}"
    if [[ -n "${result}" ]]; then
        print_status_label_results "${result}"
    fi
    return 0
}

#
# @brief End the line print_status_label started with its result.
# @param $1  "OK" (green), or anything else for a red "ERROR".
#
print_status_label_results() {
    if [[ "$1" == "OK" ]]; then
        log OK
    else
        log FAIL
    fi
}

#
# @brief Run a command quietly; on failure, report ERROR and show the end of its output.
# @param $1  What failed, for the error message (e.g., "pip install").
# @param ... The command and its arguments.
# @return 0 if the command succeeds, 1 otherwise
#
run_logged() {
    local what="$1"
    shift
    local log_file
    log_file="$(mktemp)"

    if "$@" >"${log_file}" 2>&1; then
        rm -f "${log_file}"
        return 0
    fi

    print_status_label_results "ERROR"
    log ERROR "%s failed; last lines of its output:" "${what}"
    log ERROR "$(tail -n 15 "${log_file}")"
    rm -f "${log_file}"
    return 1
}

#
# @brief Run one installer step, naming it on failure.
# @param ... The step's function and its arguments.
# @return The step's status
#
run_step() {
    if "$@"; then
        return 0
    fi
    log ERROR "Step %s failed" "$1"
    log ERROR ""
    return 1
}

#
# @brief Check whether one version is at least another.
# @param $1  Version to test (e.g., "3.12").
# @param $2  Minimum version (e.g., "3.10").
# @return 0 if $1 >= $2, 1 otherwise
#
version_at_least() {
    [[ "$(printf '%s\n' "$2" "$1" | sort -V | head -n1)" == "$2" ]]
}

#
# @brief Print a Python interpreter's major.minor version.
# @param $1  Interpreter command or path.
# @return 0 if the interpreter ran, nonzero otherwise
#
python_version() {
    "$1" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null
}

#
# @brief Verify that the system is a Red Hat family distribution that uses dnf.
# @return 0 if the system is supported, 1 otherwise
#
check_os() {
    local line id
    local name="" ids=""
    local os_release_re='^([A-Z_]+)="?([^"]*)"?$' # KEY=value or KEY="value"

    print_status_label "Checking for a Red Hat family system (dnf)"

    if [[ ! -r "${OS_RELEASE_FILE}" ]]; then
        print_status_label_results "ERROR"
        log ERROR "Cannot read %s; this installer supports Red Hat family systems only." \
            "${OS_RELEASE_FILE}"
        return 1
    fi

    while IFS= read -r line; do
        if [[ ! "${line}" =~ ${os_release_re} ]]; then
            continue
        fi
        case "${BASH_REMATCH[1]}" in
        ID | ID_LIKE) ids="${ids} ${BASH_REMATCH[2]}" ;;
        PRETTY_NAME) name="${BASH_REMATCH[2]}" ;;
        esac
    done <"${OS_RELEASE_FILE}"

    # ID_LIKE may name several systems, separated by spaces
    while IFS= read -r id; do
        if [[ -z "${id}" || " ${OS_SUPPORTED_IDS[*]} " != *" ${id} "* ]]; then
            continue
        fi
        if ! command -v dnf >/dev/null 2>&1; then
            print_status_label_results "ERROR"
            log ERROR "%s is a Red Hat family system, but dnf is not installed." "${name:-This}"
            return 1
        fi
        print_status_label_results "OK"
        return 0
    done < <(printf '%s\n' "${ids}" | tr -s ' ' '\n')

    print_status_label_results "ERROR"
    log ERROR "%s is not supported; this installer needs a Red Hat family system that uses dnf." \
        "${name:-This system}"
    return 1
}

#
# @brief Choose the Python interpreter for the venv and store it in PYTHON_BIN.
#
#   With a requested interpreter, only that one is considered. Otherwise python3 is used when it
#   is new enough, and if not, the newest python3.N found in PATH that is.
#
# @param $1  Minimum required version (e.g., "3.10").
# @param $2  [optional] Requested interpreter command or path (--python).
# @return 0 if a suitable interpreter was found, 1 otherwise
#
find_python() {
    local required="$1"
    local requested="${2:-}"
    local candidates=()
    local candidate dir name version
    local python_re='^python3\.[0-9]+$'

    if [[ -n "${requested}" ]]; then
        candidates=("${requested}")
    else
        candidates=(python3)
        # Every python3.N in PATH, newest first
        while IFS= read -r name; do
            candidates+=("${name}")
        done < <(
            while IFS= read -r dir; do
                for candidate in "${dir}"/python3.*; do
                    name="${candidate##*/}"
                    if [[ -x "${candidate}" && "${name}" =~ ${python_re} ]]; then
                        printf '%s\n' "${name}"
                    fi
                done
            done < <(printf '%s\n' "${PATH}" | tr ':' '\n') | sort -urV
        )
    fi

    for candidate in "${candidates[@]}"; do
        if ! command -v "${candidate}" >/dev/null 2>&1; then
            continue
        fi
        if ! version="$(python_version "${candidate}")"; then
            continue
        fi
        if version_at_least "${version}" "${required}"; then
            PYTHON_BIN="$(command -v "${candidate}")"
            print_status_label "Checking Python >= ${required} (${PYTHON_BIN}, ${version})" "OK"
            return 0
        fi
    done

    print_status_label "Checking Python >= ${required}" "ERROR"
    if [[ -n "${requested}" ]]; then
        log ERROR "'%s' is not a Python >= %s interpreter." "${requested}" "${required}"
    else
        log ERROR "No Python >= %s found in PATH (python3 is %s). Install one, or pass --python." \
            "${required}" "$(python_version python3 || printf 'missing')"
    fi
    return 1
}

#
# @brief Verify that a command is installed and meets a minimum version.
# @param $1  Command name (e.g., "node").
# @param $2  Display name (e.g., "Node.js").
# @param $3  Minimum required version (e.g., "22.18").
# @return 0 if the command is available and new enough, 1 otherwise
#
check_tool_version() {
    local command_name="$1"
    local display_name="$2"
    local required="$3"
    local version

    print_status_label "Checking ${display_name} >= ${required}"

    if ! command -v "${command_name}" >/dev/null 2>&1; then
        print_status_label_results "ERROR"
        log ERROR "'%s' not found in PATH." "${command_name}"
        return 1
    fi

    version="$("${command_name}" --version 2>/dev/null || true)"
    version="${version#v}" # node prints "v22.18.0"
    if ! version_at_least "${version}" "${required}"; then
        print_status_label_results "ERROR"
        log ERROR "%s >= %s required, found %s" "${display_name}" "${required}" "${version:-none}"
        return 1
    fi

    print_status_label_results "OK"
    return 0
}

#
# @brief Find every requirements*.txt in the repository and store them in PYTHON_REQUIREMENTS_FILES.
#
#   The search skips the folders in PYTHON_REQUIREMENTS_SKIP; the files are sorted by path.
#
# @return 0 if at least one was found, 1 otherwise
#
find_requirements_files() {
    local file skip
    local prune=()
    local pattern="requirements*.txt"

    for skip in "${PYTHON_REQUIREMENTS_SKIP[@]}"; do
        prune+=(-name "${skip}" -o)
    done
    PYTHON_REQUIREMENTS_FILES=()
    while IFS= read -r file; do
        PYTHON_REQUIREMENTS_FILES+=("${file#./}")
    done < <(find . \( "${prune[@]}" -false \) -prune -o -type f -name "${pattern}" -print | sort)

    if [[ ${#PYTHON_REQUIREMENTS_FILES[@]} -eq 0 ]]; then
        log ERROR "No requirements*.txt found in %s." "$(pwd)"
        return 1
    fi
    return 0
}

#
# @brief Create (if missing) the shared Python virtual environment and upgrade its pip.
#
#   The venv is created with PYTHON_BIN. An existing venv is kept only if its Python is new enough.
#
# @param $1  Full path to the virtual environment directory.
# @return 0 on success, 1 on failure
#
create_python_venv() {
    local venv_full_path="$1"
    local version

    print_status_label "Setting up virtual environment at '$(basename "${venv_full_path}")'"
    if [[ ! -d "${venv_full_path}" ]]; then
        run_logged "${PYTHON_BIN} -m venv" "${PYTHON_BIN}" -m venv "${venv_full_path}" || return 1
    fi
    if [[ ! -x "${venv_full_path}/bin/python" ]]; then
        print_status_label_results "ERROR"
        log ERROR "Virtual environment at %s is broken (missing bin/python). Rerun with --force." \
            "${venv_full_path}"
        return 1
    fi
    version="$(python_version "${venv_full_path}/bin/python" || true)"
    if ! version_at_least "${version}" "${PYTHON_REQUIRED_MIN_VER}"; then
        print_status_label_results "ERROR"
        log ERROR "Virtual environment at %s uses Python %s; Python >= %s is required. %s" \
            "${venv_full_path}" "${version:-unknown}" "${PYTHON_REQUIRED_MIN_VER}" \
            "Rerun with --force."
        return 1
    fi
    print_status_label_results "OK"

    print_status_label "Upgrading pip"
    run_logged "pip upgrade" "${venv_full_path}/bin/python" -m pip install --upgrade pip || return 1
    print_status_label_results "OK"
    return 0
}

#
# @brief Install every requirements file, then the repository's own packages (editable).
# @param $1  Full path to the virtual environment directory.
# @return 0 on success, 1 on failure
#
install_python_packages() {
    local venv_full_path="$1"
    local pip=("${venv_full_path}/bin/python" -m pip install)
    local requirements_file
    local site_packages pth_file

    find_requirements_files || return 1
    for requirements_file in "${PYTHON_REQUIREMENTS_FILES[@]}"; do
        print_status_label "Installing '${requirements_file}'"
        run_logged "pip install -r ${requirements_file}" \
            "${pip[@]}" -r "${requirements_file}" || return 1
        print_status_label_results "OK"
    done

    # Editable, so the packages run from this checkout and find its shared folders
    print_status_label "Installing the repository's packages (editable)"
    run_logged "pip install -e ." "${pip[@]}" --no-deps -e . || return 1
    # Also list the source folders in a .pth file, for IDEs that cannot follow the editable
    # install's import hook (PyCharm)
    if ! site_packages="$("${venv_full_path}/bin/python" -c \
        'import sysconfig; print(sysconfig.get_path("purelib"))')"; then
        print_status_label_results "ERROR"
        log ERROR "Cannot find the site-packages folder of %s." "${venv_full_path}"
        return 1
    fi
    pth_file="${site_packages}/agents_sources.pth"
    if ! printf '%s\n' "$(pwd)" "$(pwd)/mcp" "$(pwd)/pydantic" >"${pth_file}"; then
        print_status_label_results "ERROR"
        log ERROR "Cannot write source paths to %s." "${pth_file}"
        return 1
    fi
    print_status_label_results "OK"

    print_status_label "Checking installed packages are consistent"
    run_logged "pip check" "${venv_full_path}/bin/python" -m pip check || return 1
    print_status_label_results "OK"
    return 0
}

#
# @brief Verify the Python agents: modules import, and MCPAgent and the Pydantic Agent start.
# @param $1  Full path to the virtual environment directory.
# @return 0 if verification passes, 1 otherwise
#
verify_python_agents() {
    local py_bin="$1/bin/python"
    local module

    print_status_label "Verifying MCPAgent and the Pydantic Agent"

    for module in "${PYTHON_VERIFY_MODULES[@]}"; do
        if ! "${py_bin}" -c "import ${module}" >/dev/null 2>&1; then
            print_status_label_results "ERROR"
            log ERROR "Module '%s' is not importable with %s." "${module}" "${py_bin}"
            return 1
        fi
    done

    for module in "${PYTHON_MODULES_TO_RUN[@]}"; do
        if ! "${py_bin}" -m "${module}" --version >/dev/null 2>&1; then
            print_status_label_results "ERROR"
            log ERROR "'python -m %s --version' failed with %s." "${module}" "${py_bin}"
            return 1
        fi
    done

    if ! "${py_bin}" pydantic/agent.py --help >/dev/null 2>&1; then
        print_status_label_results "ERROR"
        log ERROR "'pydantic/agent.py --help' failed with %s." "${py_bin}"
        return 1
    fi

    print_status_label_results "OK"
    return 0
}

#
# @brief Print the official Node.js build's platform name for this machine.
# @return 0 for linux-x64 or linux-arm64, 1 for any other system
#
node_platform() {
    case "$(uname -s)-$(uname -m)" in
    Linux-x86_64) printf '%s\n' "linux-x64" ;;
    Linux-aarch64 | Linux-arm64) printf '%s\n' "linux-arm64" ;;
    *) return 1 ;;
    esac
}

#
# @brief Print the C library's version (glibc), or nothing when it is not glibc.
#
glibc_version() {
    local glibc_re='([0-9]+\.[0-9]+)$'
    local line
    line="$(ldd --version 2>/dev/null | head -n1 || true)"
    if [[ "${line}" != *GLIBC* && "${line}" != *"GNU libc"* ]]; then
        return 0 # Not glibc (musl, for instance): print nothing
    fi
    if [[ "${line}" =~ ${glibc_re} ]]; then
        printf '%s\n' "${BASH_REMATCH[1]}"
    fi
}

#
# @brief Choose the Node.js for the Vercel Agent and set NODE_FETCH.
#
#   The system's node is used when it is new enough (its npm is then checked too). Otherwise
#   install_local_node will fetch NODE_LOCAL_VERSION, so this checks that it can: a supported
#   platform, glibc new enough for the official build, and the commands that fetch it.
#
# @return 0 if a usable Node.js is installed or can be fetched, 1 otherwise
#
check_node() {
    local version="" glibc cmd label
    local missing=()

    if command -v node >/dev/null 2>&1; then
        version="$(node --version 2>/dev/null || true)"
        version="${version#v}"
        if version_at_least "${version}" "${NODE_REQUIRED_MIN_VER}"; then
            print_status_label "Checking Node.js >= ${NODE_REQUIRED_MIN_VER} (${version})" "OK"
            NODE_FETCH=false
            check_tool_version npm "npm" "${NPM_REQUIRED_MIN_VER}"
            return
        fi
    fi

    label="Checking Node.js >= ${NODE_REQUIRED_MIN_VER} (${version:-none}"
    print_status_label "${label}; fetch ${NODE_LOCAL_VERSION})"
    if ! NODE_PLATFORM="$(node_platform)"; then
        print_status_label_results "ERROR"
        log ERROR "No official Node.js build for %s; install Node.js >= %s, or use --skip-vercel." \
            "$(uname -sm)" "${NODE_REQUIRED_MIN_VER}"
        return 1
    fi
    glibc="$(glibc_version)"
    if ! version_at_least "${glibc}" "${NODE_LOCAL_MIN_GLIBC}"; then
        print_status_label_results "ERROR"
        log ERROR "The official Node.js build needs glibc >= %s (found %s); %s" \
            "${NODE_LOCAL_MIN_GLIBC}" "${glibc:-none}" \
            "install Node.js >= ${NODE_REQUIRED_MIN_VER}."
        return 1
    fi
    for cmd in "${NODE_FETCH_NEEDS[@]}"; do
        if ! command -v "${cmd}" >/dev/null 2>&1; then
            missing+=("${cmd}")
        fi
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        print_status_label_results "ERROR"
        log ERROR "Fetching Node.js needs: %s" "${missing[*]}"
        return 1
    fi
    print_status_label_results "OK"
    NODE_FETCH=true
    return 0
}

#
# @brief Check a downloaded file against its line in a SHASUMS256.txt.
# @param $1  Folder holding the file and SHASUMS256.txt.
# @param $2  The file's name.
# @return 0 if the checksum is listed and matches, 1 otherwise
#
verify_node_checksum() {
    local sums
    if ! sums="$(grep " $2\$" "$1/SHASUMS256.txt")"; then
        printf '%s is not listed in SHASUMS256.txt\n' "$2"
        return 1
    fi
    (cd "$1" && sha256sum -c - <<<"${sums}")
}

#
# @brief Put NODE_LOCAL_VERSION in NODE_LOCAL_PATH (unless it is there) and first in PATH.
#
#   The release comes from NODE_DIST_URL and is checked against the release's SHASUMS256.txt
#   before it replaces NODE_LOCAL_PATH.
#
# @return 0 on success, 1 on failure
#
install_local_node() {
    local name="node-v${NODE_LOCAL_VERSION}-${NODE_PLATFORM}"
    local tarball="${name}.tar.xz"
    local url="${NODE_DIST_URL}/v${NODE_LOCAL_VERSION}"
    local node="${NODE_LOCAL_PATH}/bin/node"
    local work installed=""

    if [[ -x "${node}" ]]; then
        installed="$("${node}" --version 2>/dev/null || true)"
    fi
    if [[ "${installed}" == "v${NODE_LOCAL_VERSION}" ]]; then
        print_status_label "Using Node.js ${NODE_LOCAL_VERSION} in '${NODE_LOCAL_PATH}'" "OK"
    else
        print_status_label "Fetching Node.js ${NODE_LOCAL_VERSION} into '${NODE_LOCAL_PATH}'"
        work="$(mktemp -d)"
        if ! run_logged "download ${tarball}" \
            curl -fsSL -o "${work}/${tarball}" "${url}/${tarball}" ||
            ! run_logged "download SHASUMS256.txt" \
                curl -fsSL -o "${work}/SHASUMS256.txt" "${url}/SHASUMS256.txt" ||
            ! run_logged "checksum of ${tarball}" verify_node_checksum "${work}" "${tarball}" ||
            ! run_logged "unpack ${tarball}" tar -xJf "${work}/${tarball}" -C "${work}"; then
            rm -rf "${work}"
            return 1
        fi
        rm -rf "${NODE_LOCAL_PATH}"
        if ! mv "${work}/${name}" "${NODE_LOCAL_PATH}"; then
            print_status_label_results "ERROR"
            log ERROR "Cannot move Node.js into %s." "${NODE_LOCAL_PATH}"
            rm -rf "${work}"
            return 1
        fi
        rm -rf "${work}"
        print_status_label_results "OK"
    fi

    # npm and the checks below run this node (npm's own script starts with env node)
    PATH="$(pwd)/${NODE_LOCAL_PATH}/bin:${PATH}"
    export PATH
    return 0
}

#
# @brief Install the Vercel Agent's packages exactly as its package-lock.json records.
# @return 0 on success, 1 on failure
#
install_node_modules() {
    print_status_label "Installing '${NODE_PROJECT_PATH}/package-lock.json'"
    if [[ ! -f "${NODE_PROJECT_PATH}/package-lock.json" ]]; then
        print_status_label_results "ERROR"
        log ERROR "Lock file '%s/package-lock.json' not found." "${NODE_PROJECT_PATH}"
        return 1
    fi
    run_logged "npm ci" npm ci --prefix "${NODE_PROJECT_PATH}" --no-audit --no-fund || return 1
    print_status_label_results "OK"
    return 0
}

#
# @brief Verify the Vercel Agent: its packages resolve and agent.ts starts.
# @return 0 if verification passes, 1 otherwise
#
verify_vercel_agent() {
    local package
    local import_package

    print_status_label "Verifying the Vercel Agent"

    for package in "${NODE_VERIFY_PACKAGES[@]}"; do
        import_package="await import('${package}')"
        if ! (cd "${NODE_PROJECT_PATH}" && node --input-type=module -e "${import_package}") \
            >/dev/null 2>&1; then
            print_status_label_results "ERROR"
            log ERROR "Package '%s' cannot be imported." "${package}"
            return 1
        fi
    done

    if ! node "${NODE_PROJECT_PATH}/agent.ts" --help >/dev/null 2>&1; then
        print_status_label_results "ERROR"
        log ERROR "'node %s/agent.ts --help' failed." "${NODE_PROJECT_PATH}"
        return 1
    fi

    print_status_label_results "OK"
    return 0
}

#
# @brief Manage the pull request gate's systemd user service.
# @param $1  The action: install, uninstall, start, stop, restart, status or logs.
# @return 0 on success, nonzero on failure
#
gate_service() {
    local action="$1"
    local unit_dir="${HOME}/.config/systemd/user"
    local unit_file="${unit_dir}/${GATE_UNIT}.service"
    local user="${USER:-$(id -un)}"
    local unit_template="${GATE_PATH}/${GATE_UNIT}.service"
    local cmd verb

    case "${action}" in
    install)
        if [[ ! -x "${PYTHON_VENV_PATH}/bin/python" ]]; then
            log ERROR "The shared .venv is missing; run %s without --gate first." \
                "$(basename "${SCRIPT_PATH}")"
            return 1
        fi
        for cmd in "${GATE_NEEDS[@]}"; do
            if ! command -v "${cmd}" >/dev/null 2>&1; then
                log WARN "%s is not installed; the gate needs it." "${cmd}"
            fi
        done
        if ! gh auth status >/dev/null 2>&1; then
            log WARN "gh is not logged in (gh auth login); the gate cannot reach GitHub."
        fi
        print_status_label "Installing the ${GATE_UNIT} service"
        run_logged "mkdir ${unit_dir}" mkdir -p "${unit_dir}" || return 1
        # The unit names ~/projects/agents; write it with this repository's real location
        if ! sed "s#%h/projects/agents#$(pwd)#g" "${unit_template}" >"${unit_file}"; then
            print_status_label_results "ERROR"
            log ERROR "Cannot write %s." "${unit_file}"
            return 1
        fi
        run_logged "systemctl daemon-reload" systemctl --user daemon-reload || return 1
        run_logged "systemctl enable" systemctl --user enable --now "${GATE_UNIT}" || return 1
        print_status_label_results "OK"
        # Keep it running after logout and start it at boot
        if ! loginctl enable-linger "${user}" >/dev/null 2>&1; then
            log WARN "could not enable lingering for %s." "${user}"
        fi
        log INFO "The gate serves http://%s:%s (open the port in the firewall to reach it %s)." \
            "$(hostname)" "${GATE_PORT}" "from other machines"
        ;;
    uninstall)
        print_status_label "Removing the ${GATE_UNIT} service"
        # Already disabled or never installed is fine
        systemctl --user disable --now "${GATE_UNIT}" >/dev/null 2>&1 || true
        rm -f "${unit_file}"
        run_logged "systemctl daemon-reload" systemctl --user daemon-reload || return 1
        print_status_label_results "OK"
        ;;
    start | stop | restart)
        case "${action}" in
        start) verb="Starting" ;;
        stop) verb="Stopping" ;;
        restart) verb="Restarting" ;;
        esac
        print_status_label "${verb} the ${GATE_UNIT} service"
        run_logged "systemctl ${action}" systemctl --user "${action}" "${GATE_UNIT}" || return 1
        print_status_label_results "OK"
        ;;
    status)
        # systemctl exits nonzero for a stopped or missing unit; the gate's own report follows
        systemctl --user --no-pager status "${GATE_UNIT}" | head -5 || true
        bash "${GATE_PATH}/pr_gate.sh" status
        ;;
    logs)
        journalctl --user -u "${GATE_UNIT}" -n 50 --no-pager -o cat
        ;;
    *)
        log ERROR "Unknown gate action: %s (%s)" "${action}" \
            "install, uninstall, start, stop, restart, status, logs"
        return 1
        ;;
    esac
}

#
# @brief Check the system and toolchains, then install and verify both environments.
# @return 0 on success, 1 at the first failed step
#
install_all() {
    local full_venv_path
    full_venv_path="$(pwd)/${PYTHON_VENV_PATH}"

    log INFO ""
    log INFO "Starting the agents installer..."
    log INFO ""

    # Check the system and every toolchain first, so nothing is changed when one is missing
    run_step check_os || return 1
    run_step find_python "${PYTHON_REQUIRED_MIN_VER}" "${PYTHON_REQUEST}" || return 1
    if [[ "${SKIP_VERCEL}" == false ]]; then
        run_step check_node || return 1
    fi

    if [[ "${FORCE}" == true ]]; then
        print_status_label "Removing existing environments"
        rm -rf "${full_venv_path}" "${NODE_PROJECT_PATH}/node_modules" "${NODE_LOCAL_PATH}"
        print_status_label_results "OK"
    fi

    # MCPAgent and the Pydantic Agent: one shared Python environment
    run_step create_python_venv "${full_venv_path}" || return 1
    run_step install_python_packages "${full_venv_path}" || return 1
    run_step verify_python_agents "${full_venv_path}" || return 1

    # The Vercel Agent: its own node_modules
    if [[ "${SKIP_VERCEL}" == false ]]; then
        if [[ "${NODE_FETCH}" == true ]]; then
            run_step install_local_node || return 1
        fi
        run_step install_node_modules || return 1
        run_step verify_vercel_agent || return 1
    fi

    log INFO ""
    log INFO "Usage (from the repository root):"
    log INFO "  .venv/bin/python mcp/server.py      # MCPAgent: the MCP server"
    log INFO "  .venv/bin/python mcp/client.py      # MCPAgent: the agent"
    log INFO "  .venv/bin/python pydantic/agent.py  # the Pydantic Agent"
    if [[ "${NODE_FETCH}" == true ]]; then
        log INFO "  .node/bin/node vercel/agent.ts      # the Vercel Agent (Node.js %s in .node/)" \
            "${NODE_LOCAL_VERSION}"
    else
        log INFO "  node vercel/agent.ts                # the Vercel Agent"
    fi
    log INFO "  ./install.sh --gate install         # the pull request gate's service"
    log INFO ""
    return 0
}

#
# @brief Parse the arguments, move to the repository root, then install or manage the gate.
# @return 0 on success, nonzero on failure
#
main() {
    # Bash behavior when run by zsh: arrays from 0, BASH_REMATCH, unmatched globs left as they are
    if [[ -n "${ZSH_VERSION:-}" ]]; then
        setopt KSH_ARRAYS BASH_REMATCH NO_NOMATCH
    fi

    parse_args "$@" || return 1

    # A relative --python path is relative to where the installer was started. Symlinks are
    # kept: a venv's or a shim's python is what was asked for, not the file it points to.
    if [[ "${PYTHON_REQUEST}" == */* ]]; then
        PYTHON_REQUEST="$(realpath -ms "${PYTHON_REQUEST}")"
    fi

    # Paths are relative to this script, so it can be run from any directory
    cd "$(dirname "$(realpath "${SCRIPT_PATH}")")" || return 1

    if [[ -n "${GATE_ACTION}" ]]; then
        gate_service "${GATE_ACTION}"
        return
    fi
    install_all
}

main "$@"
