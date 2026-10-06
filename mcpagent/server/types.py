"""
Module: types.py

Description:
    The server's types:
      - `MCPServiceConfigType`: where the server listens (host, port) and whether it is read-only.
      - `MCPServiceToolType`: one registered tool: its schema, command, arguments and environment.
"""
from dataclasses import dataclass
from typing import Any, Optional

DEFAULT_PORT = 6274  # Used when the server config does not set mcp_server_port


@dataclass
class MCPServiceConfigType:
    """ Configuration for the MCP server connection. """
    host: Optional[str] = None
    advertise_ip: Optional[str] = None
    port: int = DEFAULT_PORT
    readonly: bool = False


class MCPServiceToolType:
    """
    Represents a callable MCP (Model Context Protocol) tool.
    Attributes:
        name (str): Unique tool name as exposed to MCP clients.
        description (str): Short human-readable description of the tool's purpose.
        input_schema (dict[str, Any]): JSON Schema describing the tool's expected
            input parameters (used for validation and discovery in MCP clients).
        command (str): Path to the binary or script to execute.
        working_dir (Optional[str]): Directory where the command should be executed.
        args (list[str]): Static arguments always passed to the command.
        params (list[dict[str, Any]]): Dynamic parameters schema that can be mapped
            to runtime arguments (name, type, description).
        env (dict[str, str]): Optional environment variables to set when running.
        resource (Optional[str]): Path to a documentation resource for this tool.
    """

    def __init__(
            self,
            name: str,
            description: str,
            input_schema: dict[str, Any],
            command: str,
            working_dir: Optional[str] = None,
            args: Optional[list[str]] = None,
            params: Optional[list[dict[str, Any]]] = None,
            env: Optional[dict[str, str]] = None,
            resource: Optional[str] = None,
    ):
        """Store the tool definition; the arguments are described in the class docstring."""
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.command = command
        self.working_dir = working_dir
        self.args = args or []
        self.params = params or []
        self.env = env or {}
        self.resource = resource
