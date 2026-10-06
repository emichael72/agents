"""
Module: types.py

Description:
    The client's types:
      - Transport and connection config types (`MCPTransportType`, `HTTPConfigType`,
        `STDIOConfigType`, `ConfigType`).
      - Callback, coroutine and task type aliases for the client's request and event APIs.
      - `DebugGuru`, which pretty-prints JSON-RPC traffic for debugging.
"""
import asyncio
import json
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional, Any, TypeAlias, Coroutine, Union, AsyncGenerator, Awaitable

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.pretty import Pretty
from rich.text import Text

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


class DebugGuru:
    """
    Utility class for rendering debug information with Rich.
    Provides methods for displaying structured data in styled panels,
    automatically truncating long strings, and managing console output.
    """

    def __init__(self, console: Optional[Console] = None) -> None:
        """
        Initialize a DebugGuru instance.
        Args:
            console (Optional[Console], optional):
                A Rich Console instance to render into. If None, a new Console
                is created with `force_terminal=True`.
        """
        self._console = console or Console(force_terminal=True)

    def show_box(self,
                 title: str,
                 debug_data: object,
                 adjust_content: bool = True,
                 show_border: bool = False,
                 paint_background: bool = False) -> None:
        """
        Render debug information in a styled Rich panel.

        Args:
            title (str): Title text for the panel header.
            debug_data (object): Data to render (e.g., string, dict, list).
            adjust_content (bool, optional): Truncate long strings to fit terminal width. Defaults to True.
            show_border (bool, optional): Show a border around the panel. Defaults to False.
            paint_background (bool, optional): Apply a grey background behind the content. Defaults to False.
        """
        console_width = self._console.width
        max_str_len = max(20, console_width - 10)

        def _maybe_json(text: str) -> Any:
            """Try to parse a string as JSON; return original if not valid."""
            with suppress(Exception):
                return json.loads(text)
            return text

        def _process_content(_data: Any, _max_len: int) -> Any:
            """Recursively truncate and decode JSON strings inside the data."""
            if isinstance(_data, str):
                parsed = _maybe_json(_data.strip())
                if parsed is not _data:
                    return _process_content(parsed, _max_len)  # process decoded JSON
                return _data if len(_data) <= _max_len else _data[:_max_len] + "…"
            elif isinstance(_data, dict):
                return {k: _process_content(v, _max_len) for k, v in _data.items()}
            elif isinstance(_data, list):
                return [_process_content(v, _max_len) for v in _data]
            elif isinstance(_data, tuple):
                return tuple(_process_content(v, _max_len) for v in _data)
            return _data

        try:

            safe_data = (
                _process_content(debug_data, max_str_len)
                if adjust_content
                else debug_data
            )

            # Build renderable
            style = "dim on grey15" if paint_background else "dim"

            if safe_data in ("", None, {}, []):
                content = Text("<empty>", style=style)
            else:
                content = Pretty(safe_data, expand_all=True)

            if show_border:
                renderable = Panel(
                    content,
                    title=f"[white]DEBUG: {title}[/white]",
                    border_style="bright_black",
                    box=box.DOUBLE,
                    title_align="left",
                    padding=(0, 1),
                    expand=True,
                    style=style)
            else:
                # Wrap in Panel
                renderable = (
                    Panel(content, box=box.MINIMAL, padding=0, style=style)
                    if paint_background
                    else content)

            # Print
            self._console.print(renderable, width=console_width)
            self._console.print()

        except Exception as render_error:
            self._console.print(f"DebugGuru Error: {render_error}")
