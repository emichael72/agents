"""
Module: types.py

Description:
    The client's types:
      - Transport and connection config types (`MCPTransportType`, `HTTPConfigType`,
        `STDIOConfigType`, `ConfigType`).
      - Callback, coroutine and task type aliases for the client's request and event APIs.
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
