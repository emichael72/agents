#!/usr/bin/env bash
# =============================================================================
# install.sh
#
# Description:
#   Installs everything needed to run the three agents:
#     - A shared Python virtual environment (.venv) for MCPAgent and the Pydantic Agent,
#       with both agents' pinned requirements and the pr_gate tool's. Both agents run from the source tree.
#     - vercel/node_modules for the Vercel Agent, installed exactly as package-lock.json
#       records (npm ci).
#   Each step is checked, and the installer stops at the first failure.
#
#   With --gate ACTION it manages the pull request gate's service instead (gatekeepers/pr, the
#   pr-gate systemd user unit): install, uninstall, start, stop, restart, status or logs.
#
# Exit Codes:
#   0 - Success.
#   1 - Failure (missing requirements, unsupported versions, or install error).
#
# =============================================================================

PYTHON_VENV_PATH=".venv"
PYTHON_REQUIRED_MIN_VER="3.10"
PYTHON_REQUIREMENTS_FILES="mcpagent/requirements.txt pydantic/requirements.txt gatekeepers/pr/requirements.txt requirements-dev.txt"
PYTHON_MODULES_TO_RUN="mcpagent.server mcpagent.client" # Checked with python -m <module> --version
PYTHON_VERIFY_MODULES="mcpagent pydantic_ai jsonschema httpx httpx2 aiohttp json5 prompt_toolkit rich ruff"

# Node 22.18+ runs .ts files directly (type stripping), so the Vercel Agent needs no build step
NODE_PROJECT_PATH="vercel"
NODE_REQUIRED_MIN_VER="22.18"
NPM_REQUIRED_MIN_VER="10.0"
NODE_VERIFY_PACKAGES="ai @ai-sdk/openai-compatible @ai-sdk/mcp zod"

# The pull request gate's service (gatekeepers/pr), a systemd user unit
GATE_PATH="gatekeepers/pr"
GATE_UNIT="pr-gate"
GATE_PORT=8000
GATE_NEEDS="gh bwrap doxygen clang-format" # Commands the gate's checks use

# Globally control installer verbosity
QUIET_MODE=0

#
# @brief Manage the pull request gate's systemd user service.
# @param $1  The action: install, uninstall, start, stop, restart, status or logs.
# @return 0 on success, nonzero on failure.
#

gate_service() {

    local action="$1"
    local unit_dir="$HOME/.config/systemd/user"
    local unit_file="$unit_dir/$GATE_UNIT.service"
    local command

    case "$action" in
    install)
        if [[ ! -x "$PYTHON_VENV_PATH/bin/python" ]]; then
            printf "The shared .venv is missing; run %s without --gate first.\n" "$0" >&2
            return 1
        fi
        for command in $GATE_NEEDS; do
            command -v "$command" >/dev/null 2>&1 || printf "Warning: %s is not installed; the gate needs it.\n" "$command" >&2
        done
        gh auth status >/dev/null 2>&1 || printf "Warning: gh is not logged in (gh auth login); the gate cannot reach GitHub.\n" >&2
        print_status_label "Installing the $GATE_UNIT service"
        mkdir -p "$unit_dir" || return 1
        # The unit names ~/projects/agents; write it with this repository's real location
        sed "s#%h/projects/agents#$(pwd)#g" "$GATE_PATH/$GATE_UNIT.service" >"$unit_file" || return 1
        run_logged "systemctl enable" systemctl --user daemon-reload || return 1
        run_logged "systemctl enable" systemctl --user enable --now "$GATE_UNIT" || return 1
        print_status_label_results "OK"
        # Keep it running after logout and start it at boot
        loginctl enable-linger "$USER" >/dev/null 2>&1 || printf "Warning: could not enable lingering for %s.\n" "$USER" >&2
        printf "The gate serves http://%s:%s (open the port in the firewall to reach it from other machines).\n" "$(hostname)" "$GATE_PORT"
        ;;
    uninstall)
        print_status_label "Removing the $GATE_UNIT service"
        systemctl --user disable --now "$GATE_UNIT" >/dev/null 2>&1
        rm -f "$unit_file"
        systemctl --user daemon-reload
        print_status_label_results "OK"
        ;;
    start | stop | restart)
        print_status_label "${action^} the $GATE_UNIT service"
        run_logged "systemctl $action" systemctl --user "$action" "$GATE_UNIT" || return 1
        print_status_label_results "OK"
        ;;
    status)
        systemctl --user --no-pager status "$GATE_UNIT" | head -5
        bash "$GATE_PATH/pr_gate.sh" status
        ;;
    logs)
        journalctl --user -u "$GATE_UNIT" -n 50 --no-pager -o cat
        ;;
    *)
        printf "Unknown gate action: %s (install, uninstall, start, stop, restart, status, logs)\n" "$action" >&2
        return 1
        ;;
    esac
}

#
# @brief Append the final result (OK/ERROR) to the line printed by print_status_label.
# @param $1  Result string ("OK" or "ERROR").
#

print_status_label_results() {

    local result="$1"

    ((QUIET_MODE)) && return # Skip if quiet

    if [[ "$result" == "OK" ]]; then
        printf "\033[32m%s\033[0m\n" "OK" # green
    else
        printf "\033[31m%s\033[0m\n\n" "ERROR" # red
    fi
}

#
# @brief Print a task label followed by dot padding, leaving space for an optional result string.
# @param $1  Task label string (e.g., "Checking Python >= 3.10").
# @param $2  [optional] Result string (e.g., "OK", "ERROR"). Defaults to empty.
# @param $3  [optional] Total line width for alignment. Defaults to 60.
#

print_status_label() {

    local label="$1"
    local results_str="${2:-}"   # Default empty
    local total_width="${3:-60}" # Default 60

    ((QUIET_MODE)) && return # Skip if quiet

    local dots=$((total_width - ${#label}))
    ((dots < 1)) && dots=1

    local dot_str
    dot_str=$(printf "%${dots}s" "")
    dot_str=${dot_str// /"."}

    printf "%s %s " "$label" "$dot_str"
    [[ -n "$results_str" ]] && print_status_label_results "$results_str"
}

#
# @brief Run a command quietly; on failure, report ERROR and show the end of its output.
# @param $1  What failed, for the error message (e.g., "pip install").
# @param $@  The command and its arguments.
# @return
#   0 if the command succeeds, nonzero otherwise.
#

run_logged() {

    local what="$1"
    shift
    local log_file
    log_file=$(mktemp)

    if "$@" >"$log_file" 2>&1; then
        rm -f "$log_file"
        return 0
    fi

    print_status_label_results "ERROR"
    printf "%s failed; last lines of its output:\n" "$what" >&2
    tail -n 15 "$log_file" >&2
    rm -f "$log_file"
    return 1
}

#
# @brief Verify that a command is installed and meets a minimum version.
# @param $1  Command name (e.g., "python3", "node").
# @param $2  Display name (e.g., "Python").
# @param $3  Minimum required version (e.g., "3.10").
# @return
#   0 if the command is available and its version >= required, nonzero otherwise.
#

check_tool_version() {

    local command_name="$1"
    local display_name="$2"
    local required="$3"
    print_status_label "Checking $display_name >= $required"

    if ! command -v "$command_name" &>/dev/null; then
        print_status_label_results "ERROR"
        printf "'%s' not found in PATH.\n" "$command_name" >&2
        return 1
    fi

    local version
    if [[ "$command_name" == "python3" ]]; then
        version=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
    else
        version=$("$command_name" --version 2>/dev/null)
        version=${version#v} # node prints "v22.18.0"
    fi

    if [ "$(printf '%s\n' "$required" "$version" | sort -V | head -n1)" != "$required" ]; then
        print_status_label_results "ERROR"
        printf "%s >= %s required, found %s\n" "$display_name" "$required" "$version" >&2
        return 1
    fi

    print_status_label_results "OK"
}

#
# @brief Create (if missing) the shared Python virtual environment and upgrade its pip.
# @param $1  Full path to the virtual environment directory.
# @return
#   0 on success, nonzero on failure.
#

create_python_venv() {

    local venv_full_path="$1"

    print_status_label "Setting up virtual environment at '$(basename "$venv_full_path")'"
    if [[ ! -d "$venv_full_path" ]]; then
        run_logged "python3 -m venv" python3 -m venv "$venv_full_path" || return 1
    fi
    if [[ ! -x "$venv_full_path/bin/python" ]]; then
        print_status_label_results "ERROR"
        printf "Virtual environment at %s is broken (missing bin/python). Rerun with --force.\n" "$venv_full_path" >&2
        return 1
    fi
    print_status_label_results "OK"

    print_status_label "Upgrading pip"
    run_logged "pip upgrade" "$venv_full_path/bin/python" -m pip install --upgrade pip || return 1
    print_status_label_results "OK"
}

#
# @brief Install the agents' requirements into the venv.
# @param $1  Full path to the virtual environment directory.
# @return
#   0 on success, nonzero on failure.
#

install_python_packages() {

    local venv_full_path="$1"
    local pip=("$venv_full_path/bin/python" -m pip install)
    local requirements_file

    for requirements_file in $PYTHON_REQUIREMENTS_FILES; do
        print_status_label "Installing '$requirements_file'"
        if [[ ! -f "$requirements_file" ]]; then
            print_status_label_results "ERROR"
            printf "Requirements file '%s' not found.\n" "$requirements_file" >&2
            return 1
        fi
        run_logged "pip install -r $requirements_file" "${pip[@]}" -r "$requirements_file" || return 1
        print_status_label_results "OK"
    done

    print_status_label "Checking installed packages are consistent"
    run_logged "pip check" "$venv_full_path/bin/python" -m pip check || return 1
    print_status_label_results "OK"
}

#
# @brief Verify the Python agents: modules import, and MCPAgent and the Pydantic Agent start.
# @param $1  Full path to the virtual environment directory.
# @return
#   0 if verification passes, nonzero otherwise.
#

verify_python_agents() {

    local venv_full_path="$1"
    local py_bin="$venv_full_path/bin/python"
    local module

    print_status_label "Verifying MCPAgent and the Pydantic Agent"

    for module in $PYTHON_VERIFY_MODULES; do
        if ! "$py_bin" -c "import $module" >/dev/null 2>&1; then
            print_status_label_results "ERROR"
            printf "Module '%s' is not importable with %s.\n" "$module" "$py_bin" >&2
            return 1
        fi
    done

    for module in $PYTHON_MODULES_TO_RUN; do
        if ! "$py_bin" -m "$module" --version >/dev/null 2>&1; then
            print_status_label_results "ERROR"
            printf "'python -m %s --version' failed with %s.\n" "$module" "$py_bin" >&2
            return 1
        fi
    done

    if ! "$py_bin" pydantic/agent.py --help >/dev/null 2>&1; then
        print_status_label_results "ERROR"
        printf "'pydantic/agent.py --help' failed with %s.\n" "$py_bin" >&2
        return 1
    fi

    print_status_label_results "OK"
}

#
# @brief Install the Vercel Agent's packages exactly as its package-lock.json records.
# @return
#   0 on success, nonzero on failure.
#

install_node_modules() {

    print_status_label "Installing '$NODE_PROJECT_PATH/package-lock.json'"
    if [[ ! -f "$NODE_PROJECT_PATH/package-lock.json" ]]; then
        print_status_label_results "ERROR"
        printf "Lock file '%s/package-lock.json' not found.\n" "$NODE_PROJECT_PATH" >&2
        return 1
    fi
    run_logged "npm ci" npm ci --prefix "$NODE_PROJECT_PATH" --no-audit --no-fund || return 1
    print_status_label_results "OK"
}

#
# @brief Verify the Vercel Agent: its packages resolve and agent.ts starts.
# @return
#   0 if verification passes, nonzero otherwise.
#

verify_vercel_agent() {

    local package

    print_status_label "Verifying the Vercel Agent"

    for package in $NODE_VERIFY_PACKAGES; do
        if ! (cd "$NODE_PROJECT_PATH" && node --input-type=module -e "await import('$package')") >/dev/null 2>&1; then
            print_status_label_results "ERROR"
            printf "Package '%s' cannot be imported.\n" "$package" >&2
            return 1
        fi
    done

    if ! node "$NODE_PROJECT_PATH/agent.ts" --help >/dev/null 2>&1; then
        print_status_label_results "ERROR"
        printf "'node %s/agent.ts --help' failed.\n" "$NODE_PROJECT_PATH" >&2
        return 1
    fi

    print_status_label_results "OK"
}

#
# @brief Entry point for the installer.
# @param
#	Arguments
# @return
#   0 on success, nonzero on failure.
#

main() {

    local force=0
    local skip_vercel=0
    local gate_action=""
    local full_venv_path

    _show_help() {
        cat <<EOF

Agents installer
Usage: $0 [OPTIONS]

Options:
  -f, --force         Recreate the shared .venv and vercel/node_modules from scratch.
      --skip-vercel   Skip the Vercel Agent (no Node.js needed).
  -q, --quiet         Suppress status reporting; only show errors.
      --gate ACTION   Manage the pull request gate's service (gatekeepers/pr) instead of installing:
                      install, uninstall, start, stop, restart, status or logs.
  -h, --help          Show this help message and exit.

EOF
    }

    # Parse args
    while [[ $# -gt 0 ]]; do
        case "$1" in
        -f | --force)
            force=1
            shift
            ;;
        --skip-vercel)
            skip_vercel=1
            shift
            ;;
        --gate)
            gate_action="${2:-}"
            [[ -z "$gate_action" ]] && {
                _show_help >&2
                exit 1
            }
            shift 2
            ;;
        -q | --quiet)
            QUIET_MODE=1
            shift
            ;;
        -h | --help)
            _show_help
            exit 0
            ;;
        *)
            printf "Unknown option: %s\n\n" "$1" >&2
            _show_help >&2
            exit 1
            ;;
        esac
    done

    _run_step() {
        # Helper to execute a step and report its status
        "$@" || {
            local step_name="$1"
            printf "Step \033[1;31m%s\033[0m failed\n\n" "$step_name" >&2
            return 1
        }
    }

    # Paths are relative to this script, so it can be run from any directory
    cd "$(dirname "$(realpath "${BASH_SOURCE[0]}")")" || return 1
    full_venv_path="$(pwd)/$PYTHON_VENV_PATH"

    if [[ -n "$gate_action" ]]; then
        gate_service "$gate_action"
        return
    fi

    ((!QUIET_MODE)) && printf "\nStarting the agents installer...\n\n"

    # Check every toolchain first, so nothing is changed when a requirement is missing
    _run_step check_tool_version python3 "Python" "$PYTHON_REQUIRED_MIN_VER" || return 1
    if ((!skip_vercel)); then
        _run_step check_tool_version node "Node.js" "$NODE_REQUIRED_MIN_VER" || return 1
        _run_step check_tool_version npm "npm" "$NPM_REQUIRED_MIN_VER" || return 1
    fi

    if ((force)); then
        print_status_label "Removing existing environments"
        rm -rf "$full_venv_path" "$NODE_PROJECT_PATH/node_modules" >/dev/null 2>&1 || true
        print_status_label_results "OK"
    fi

    # MCPAgent and the Pydantic Agent: one shared Python environment
    _run_step create_python_venv "$full_venv_path" || return 1
    _run_step install_python_packages "$full_venv_path" || return 1
    _run_step verify_python_agents "$full_venv_path" || return 1

    # The Vercel Agent: its own node_modules
    if ((!skip_vercel)); then
        _run_step install_node_modules || return 1
        _run_step verify_vercel_agent || return 1
    fi

    ((QUIET_MODE)) && return 0 # Skip usage if quiet
    cat <<EOF

Usage (from the repository root):
  .venv/bin/python -m mcpagent.server          # MCPAgent: the MCP server
  .venv/bin/python -m mcpagent.client          # MCPAgent: the agent
  .venv/bin/python pydantic/agent.py           # the Pydantic Agent
  node vercel/agent.ts                         # the Vercel Agent
  ./install.sh --gate install                  # the pull request gate's service (gatekeepers/pr)

EOF
}

main "$@"
