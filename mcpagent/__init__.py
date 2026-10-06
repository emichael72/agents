"""
Module: __init__.py

Description:
    Package root of mcpagent: an MCP server that exposes scripts as tools, and an MCP client
    with an agent that lets a model use them.

    Defines `__version__` and re-exports the public classes and types, so callers can write
    `from mcpagent import MCPClient, MCPService, ...`. The package's own modules import from
    their own folders instead (client/, server/), so the order below does not matter.
"""
__version__ = "1.0.2"  # Defined before the imports below, which read it

from .client.types import (MCPTransportType, HTTPConfigType, STDIOConfigType, ConfigType,
                                  ResponseCallbackType, EventCallbackType, RequestReturnType,
                                  ListenEventsReturnType, JSONRPCResponseCoroType, JSONRPCResponseTaskType,
                                  DebugGuru)
from .client.connection import MCPClientConnection
from .client.client import MCPClient
from .server.service import MCPService

__all__ = [
    "__version__",

    # Client
    "MCPClient", "MCPClientConnection",

    # Server
    "MCPService",

    # Client types
    "MCPTransportType", "HTTPConfigType", "STDIOConfigType", "ConfigType",
    "ResponseCallbackType", "EventCallbackType", "RequestReturnType", "ListenEventsReturnType",
    "JSONRPCResponseCoroType", "JSONRPCResponseTaskType", "DebugGuru",
]
