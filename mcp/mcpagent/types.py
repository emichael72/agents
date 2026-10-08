"""
Module: types.py

Description:
    The package's types:
      - Transport and connection config types (`MCPTransportType`, `HTTPConfigType`,
        `STDIOConfigType`, `ConfigType`).
      - Callback, coroutine and task type aliases for the client's request and event APIs.
      - The server's: `RPCError`, a JSON-RPC error a method handler raises, and
        `MCPServiceToolType`, one registered tool.
"""
import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional, Any, TypeAlias, Coroutine, Union, AsyncGenerator, Awaitable

class MCPTransportType(str, Enum):
    """
    Supported transport types for MCP server connections.
    """

    HTTP = "HTTP"  # Plain http:// or https:// depending on URL
    STDIO = "STDIO"  # Local stdin/stdout pipes


@dataclass
class HTTPConfigType:
    """
    Connection settings for an MCP server reached over HTTP.
    Attributes:
        url: The JSON-RPC endpoint, e.g. "http://127.0.0.1:6275/".
        headers: Extra HTTP headers to send with every request.
        sse_url: Optional server-sent events URL for notifications.
    """
    url: str
    headers: Optional[dict[str, str]] = None
    sse_url: Optional[str] = None


@dataclass
class STDIOConfigType:
    """
    Connection settings for an MCP server started as a subprocess and reached over stdin/stdout.
    Attributes:
        command: The program and its arguments.
        env: Extra environment variables for the subprocess.
    """
    command: list[str]
    env: Optional[dict[str, str]] = None


ConfigType = Union[HTTPConfigType, STDIOConfigType]

# ------------------------------------------------------------------------------
# Callback types
# ------------------------------------------------------------------------------

# Callback used for JSON-RPC requests; it may be a plain function or a coroutine function.
# Args:
#   result: The JSON-RPC response dict if the request succeeded, else None.
#   error:  The Exception raised if the request failed, else None.
ResponseCallbackType: TypeAlias = Callable[[Optional[dict[str, Any]], Optional[Exception]], Optional[Awaitable[None]]]

# Callback used for server-sent events (SSE); it may be a plain function or a coroutine function.
# Args:
#   event: A JSON-decoded dictionary representing the SSE payload.
EventCallbackType: TypeAlias = Callable[[dict[str, Any]], Optional[Awaitable[None]]]

# ------------------------------------------------------------------------------
# Request/response coroutine & task types
# ------------------------------------------------------------------------------

# Coroutine that, when awaited, yields a JSON-RPC response (dict).
# Returned by send_request() in await mode.
JSONRPCResponseCoroType: TypeAlias = Coroutine[Any, Any, dict[str, Any]]

# Task scheduled in the event loop that will eventually yield a JSON-RPC response.
# Returned by send_request() in callback mode.
JSONRPCResponseTaskType: TypeAlias = asyncio.Task[dict[str, Any]]

# Mapping from server_id to either a JSON-RPC coroutine (await mode) or Task (callback mode).
# Returned by MCPClient.request() in broadcast mode.
MultiServerRequestsType: TypeAlias = dict[str, Union[JSONRPCResponseCoroType, JSONRPCResponseTaskType]]

# Unified return type for MCPClient.request():
#   - Single-server await mode → JSONRPCResponseCoroType
#   - Single-server callback mode → JSONRPCResponseTaskType
#   - Multi-server mode → MultiServerRequestsType
RequestReturnType: TypeAlias = Union[JSONRPCResponseCoroType, JSONRPCResponseTaskType, MultiServerRequestsType]

# Hybrid type for events listening:
#   - Await mode → AsyncGenerator yielding dict events.
#   - Callback mode → Task running in the background.
ListenEventsReturnType: TypeAlias = Union[AsyncGenerator[dict, None], asyncio.Task[None]]


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
