"""
Module: types.py

Description:
    The server's types:
      - `MCPServiceConfigType`: where the server listens (host, port) and whether it is read-only.
      - `MCPServiceToolType`: one registered tool: its schema, command, arguments and environment.
      - `RPCError`: a JSON-RPC error a method handler raises: its code and message.
"""
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class MCPServiceConfigType:
    """ Configuration for the MCP server connection. """
    host: Optional[str] = None
    advertise_ip: Optional[str] = None
    port: Optional[int] = None  # The server settings' mcp_server_port
    readonly: bool = False


class RPCError(Exception):
    """
    A JSON-RPC error that a method handler raises, answered with this code and message.
    """

    def __init__(self, code: int, message: str) -> None:
        """
        Args:
            code: The JSON-RPC error code, e.g. -32602 for invalid params.
            message: The error message.
        """
        super().__init__(message)
        self.code = code
        self.message = message


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
        timeout (Optional[float]): Seconds a client should wait for the tool (from the manifest); None for the client's default.
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
            timeout: Optional[float] = None,
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
        self.timeout = timeout
