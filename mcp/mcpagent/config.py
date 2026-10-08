"""
Module: config.py

Description:
    `MCPAgentConfig`: the MCPAgent configuration (JSON), one file, jsons/mcpagent.json by default,
    validated against jsons/schemas/mcpagent.schema.json. Its "server" section configures the MCP
    server and its "client" section the agent.
    Paths named in the configuration are relative to the repository root (REPO_ROOT).
"""

import copy
import json
import sys
from pathlib import Path
from typing import Any

from jsonschema import ValidationError, validate

from mcpagent import DEFAULT_CONFIG, REPO_ROOT, SCHEMA_FILE


class MCPAgentConfig:
    """
    An MCPAgent configuration: its "server" and "client" sections.
    """

    def __init__(self, data: dict[str, Any], path: str | Path = "<config>") -> None:
        """
        Wrap configuration data that is already parsed; `load` reads and validates a file.
        Args:
            data: The configuration: its "server" and "client" sections.
            path: Where it came from, for error messages.
        """
        self.data = data
        self.path = Path(path)

    @classmethod
    def load(cls, config_file: str | Path = DEFAULT_CONFIG) -> "MCPAgentConfig":
        """
        Read a configuration and validate it against SCHEMA_FILE.
        Args:
            config_file: Path to a JSON configuration file.
        Returns:
            MCPAgentConfig: The configuration.
        Raises:
            RuntimeError: If the config is missing or is not an object, the schema cannot be
                loaded, or schema validation fails.
            ValueError: If the config is not valid JSON.
        """
        path = Path(config_file)
        if not path.is_file():
            raise RuntimeError(f"Configuration file not found: {path}")

        with path.open("r", encoding="utf-8") as config_stream:
            config_data = json.load(config_stream)

        try:
            with SCHEMA_FILE.open("r", encoding="utf-8") as schema_stream:
                schema = json.load(schema_stream)
            validate(instance=config_data, schema=schema)
        except ValidationError as error:
            raise RuntimeError(f"Schema validation failed for {path} against {SCHEMA_FILE}: {error.message}") from error
        except Exception as error:
            raise RuntimeError(f"Error loading schema {SCHEMA_FILE}: {error}") from error

        if not isinstance(config_data, dict):
            raise RuntimeError(f"Configuration must be a JSON object: {path}")
        return cls(config_data, path)

    @staticmethod
    def repo_path(value: str | Path) -> Path:
        """
        Resolve a path named in a config.
        Args:
            value: An absolute path, a path starting with ~, or a path relative to the repository root.
        Returns:
            Path: The path, with relative ones joined to REPO_ROOT.
        """
        return REPO_ROOT / Path(value).expanduser()

    @property
    def server(self) -> dict[str, Any]:
        """
        The MCP server's settings: the "server" section, as MCPService takes it.
        Raises:
            RuntimeError: If the configuration has no "server" section.
        """
        return self._section("server")

    @property
    def client(self) -> dict[str, Any]:
        """
        The agent's settings: a copy of the "client" section, with each STDIO server entry that has
        no "config" set to start this package's own server, with this config, as a child process.
        Raises:
            RuntimeError: If the configuration has no "client" section, or a STDIO entry without
                "config" has no "server" section to start.
        """
        client = copy.deepcopy(self._section("client"))
        for entry in client.get("servers", []):
            if entry.get("transport", "").upper() != "STDIO" or "config" in entry:
                continue
            self._section("server")  # The server it starts reads this section
            entry["config"] = {"command": [sys.executable, "-m", "mcpagent.service", str(self.path.resolve())]}
        return client

    def _section(self, name: str) -> dict[str, Any]:
        """
        One section of the configuration.
        Args:
            name: "server" or "client".
        Returns:
            dict[str, Any]: The section.
        Raises:
            RuntimeError: If the configuration has no such section.
        """
        section = self.data.get(name)
        if not isinstance(section, dict):
            raise RuntimeError(f'Configuration {self.path} has no "{name}" section')
        return section
