"""
Module: toolset.py

Description:
    The Pydantic Agent's local tools, discovered from the shared agents/tools folder.

    Each <tool>/tool.json manifest (the same shape MCPAgent's server reads) describes a
    command, its fixed arguments and its parameters. This module turns every manifest into a
    pydantic-ai tool:
      - The JSON schema is built from the manifest's params, as the MCPAgent server does.
      - Calling the tool validates the arguments and runs the command as a subprocess, with
        agents/tools as its working directory.
"""
import json
import os
import subprocess
from pathlib import Path

from jsonschema import ValidationError, validate
from pydantic_ai import ModelRetry, Tool, ToolFailed
from pydantic_ai.toolsets import FunctionToolset

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
SCRIPT_TIMEOUT = 30
AGENT_NAME = "Pydantic Agent"  # Lets tools such as mr and pr_gate say which agent ran them


def run_script(*command: str, env: dict[str, str] | None = None, timeout: float = SCRIPT_TIMEOUT) -> str:
    """
    Run a tool command from the tools folder and return its output.
    Args:
        *command: The program and its arguments, e.g. ("bash", "time/time.sh").
        env: Extra environment variables for the command (AGENT_NAME is always set).
        timeout: Seconds to wait (a manifest's "timeout", default SCRIPT_TIMEOUT).
    Returns:
        str: The command's standard output, stripped.
    Raises:
        ToolFailed: If the command exits nonzero or times out; the model sees the message.
    """
    try:
        completed = subprocess.run(
            command, cwd=TOOLS_DIR, env={**os.environ, "AGENT_NAME": AGENT_NAME, **(env or {})},
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise ToolFailed(f"{command[1]} timed out after {timeout:g}s") from None
    output = completed.stdout.strip()
    if completed.returncode != 0:
        # The model sees the failure and can explain it, like MCPAgent's isError results.
        raise ToolFailed(output or completed.stderr.strip() or f"exit code {completed.returncode}")
    return output


def input_schema(manifest: dict) -> dict:
    """
    Build the JSON schema for a manifest's params, the same way the MCPAgent server does.
    Args:
        manifest: A parsed tool.json.
    Returns:
        dict: An object schema; params are required unless they set "required": false.
    """
    params = manifest.get("params", [])
    return {
        "type": "object",
        "properties": {p["name"]: {"type": p.get("type", "string"), "description": p.get("description", "")}
                       for p in params},
        "required": [p["name"] for p in params if p.get("required", True)],
        "additionalProperties": False,
    }


def build_argv(manifest: dict, arguments: dict) -> list[str]:
    """
    Build the command line for one call.
    Args:
        manifest: A parsed tool.json.
        arguments: The model's arguments for this call.
    Returns:
        list[str]: The command, its fixed args, then each given param as `--name value` (flag) or
        a bare value (positional). Omitted optional params are left out.
    """
    argv = [manifest["command"], *manifest.get("args", [])]
    for param in manifest.get("params", []):
        value = arguments.get(param["name"])
        if value is None:
            continue  # Optional and omitted: the script uses its own default
        if param.get("style", "flag") == "positional":
            argv.append(str(value))
        else:
            argv += [f"--{param['name']}", str(value)]
    return argv


def manifest_tool(name: str, manifest: dict) -> Tool:
    """
    Turn one manifest into a pydantic-ai tool.
    Args:
        name: The tool name (its folder name).
        manifest: The parsed tool.json.
    Returns:
        Tool: A tool whose function validates the arguments against the schema (raising
        `ModelRetry` so the model can correct them) and then runs the command.
    """
    schema = input_schema(manifest)

    def call(**arguments) -> str:
        """
        Validate the model's arguments and run the tool's command.
        Args:
            **arguments: The model's arguments; None values count as omitted.
        Returns:
            str: The command's output.
        Raises:
            ModelRetry: If the arguments do not match the schema.
        """
        arguments = {key: value for key, value in arguments.items() if value is not None}  # null = omitted
        try:
            validate(arguments, schema)  # Tool.from_schema leaves validation to us
        except ValidationError as error:
            raise ModelRetry(f"Invalid arguments: {error.message}") from None
        return run_script(*build_argv(manifest, arguments), env=manifest.get("env"),
                          timeout=float(manifest.get("timeout", SCRIPT_TIMEOUT)))

    return Tool.from_schema(call, name=name, description=manifest.get("description"), json_schema=schema)


def load_toolset(tools_dir: Path = TOOLS_DIR) -> FunctionToolset:
    """
    Load one tool per <tool>/tool.json under a tools folder.
    Args:
        tools_dir: The folder to scan (default: agents/tools).
    Returns:
        FunctionToolset: The tools, named after their folders, in alphabetical order.
    """
    toolset = FunctionToolset()
    for manifest_path in sorted(tools_dir.glob("*/tool.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        toolset.add_tool(manifest_tool(manifest_path.parent.name, manifest))
    return toolset


toolset = load_toolset()
