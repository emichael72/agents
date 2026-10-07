"""
Module: config.py

Description:
    Loads the MCPAgent configuration (JSON/JSONC/JSON5): one file, jsons/mcpagent.jsonc by
    default, validated against jsons/schemas/mcpagent.schema. Its "server" section configures
    the MCP server and its "client" section the agent; server_settings and client_settings
    return them.
    Paths named in the configuration are relative to the repository root (REPO_ROOT).
"""

import copy
from pathlib import Path
from typing import Any

import json5
from jsonschema import ValidationError, validate

# The configuration and its schema, shared by the client and the server.
JSONS_DIR = Path(__file__).resolve().parent / "jsons"
SCHEMA_DIR = JSONS_DIR / "schemas"
DEFAULT_CONFIG = JSONS_DIR / "mcpagent.jsonc"
SCHEMA_FILE = SCHEMA_DIR / "mcpagent.schema"

# The repository root: the nearest folder above this file holding pyproject.toml
REPO_ROOT = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").is_file())


def repo_path(value: str | Path) -> Path:
    """
    Resolve a path named in a config.
    Args:
        value: An absolute path, a path starting with ~, or a path relative to the repository root.
    Returns:
        Path: The path, with relative ones joined to REPO_ROOT.
    """
    return REPO_ROOT / Path(value).expanduser()


def load_config(config_file: str | Path) -> dict[str, Any]:
    """
    Read a configuration and validate it against SCHEMA_FILE.
    Args:
        config_file: Path to a JSON/JSONC/JSON5 configuration file.
    Returns:
        dict[str, Any]: The parsed configuration: its "server" and "client" sections.
    Raises:
        RuntimeError: If the config is missing or is not an object, the schema cannot be
            loaded, or schema validation fails.
        ValueError: If the config cannot be parsed.
    """
    path = Path(config_file)
    if not path.is_file():
        raise RuntimeError(f"Configuration file not found: {path}")

    with path.open("r", encoding="utf-8") as config_stream:
        config_data = json5.load(config_stream)

    try:
        with SCHEMA_FILE.open("r", encoding="utf-8") as schema_stream:
            schema = json5.load(schema_stream)
        validate(instance=config_data, schema=schema)
    except ValidationError as error:
        raise RuntimeError(f"Schema validation failed for {path} against {SCHEMA_FILE}: {error.message}") from error
    except Exception as error:
        raise RuntimeError(f"Error loading schema {SCHEMA_FILE}: {error}") from error

    if not isinstance(config_data, dict):
        raise RuntimeError(f"Configuration must be a JSON object: {path}")
    return config_data


def _section(config_data: dict[str, Any], name: str, config_file: str | Path) -> dict[str, Any]:
    """
    One section of a loaded configuration.
    Args:
        config_data: The configuration, from load_config.
        name: "server" or "client".
        config_file: The configuration's path, for the error.
    Returns:
        dict[str, Any]: The section.
    Raises:
        RuntimeError: If the configuration has no such section.
    """
    section = config_data.get(name)
    if not isinstance(section, dict):
        raise RuntimeError(f'Configuration {config_file} has no "{name}" section')
    return section


def server_settings(config_data: dict[str, Any], config_file: str | Path) -> dict[str, Any]:
    """
    The MCP server's settings: the configuration's "server" section.
    Args:
        config_data: The configuration, from load_config.
        config_file: The configuration's path, for errors.
    Returns:
        dict[str, Any]: The section, as MCPService takes it.
    Raises:
        RuntimeError: If the configuration has no "server" section.
    """
    return _section(config_data, "server", config_file)


def client_settings(config_data: dict[str, Any], config_file: str | Path) -> dict[str, Any]:
    """
    The agent's settings: the configuration's "client" section, with each HTTP server entry
    that has no "config" pointed at the "server" section's address and port.
    Args:
        config_data: The configuration, from load_config.
        config_file: The configuration's path, for errors.
    Returns:
        dict[str, Any]: A copy of the section, as MCPClient uses it.
    Raises:
        RuntimeError: If the configuration has no "client" section, or an HTTP entry needs the
            "server" section's port and there is none.
    """
    client = copy.deepcopy(_section(config_data, "client", config_file))
    for entry in client.get("servers", []):
        if entry.get("transport", "").upper() != "HTTP" or "config" in entry:
            continue
        server = _section(config_data, "server", config_file)
        host = server.get("mcp_server_bind_address") or "127.0.0.1"
        host = "127.0.0.1" if host == "0.0.0.0" else host  # Reach a server bound to all interfaces locally
        port = server.get("mcp_server_port")
        if port is None:
            raise RuntimeError(f'Configuration {config_file} has no "mcp_server_port" in its server section')
        # noinspection HttpUrlsUsage
        url = f"http://{host}:{port}/"
        entry["config"] = {"url": url, "sse_url": url + "sse"}
    return client
