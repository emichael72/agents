"""
Module: __init__.py

Description:
    Package root of mcpagent: an MCP server that exposes scripts as tools, and an MCP client
    with an agent that lets a model use them.

    Defines `__version__` and re-exports the public classes and types, so callers can write
    `from mcpagent import MCPClient, MCPService, ...`. Internal client modules import from
    this namespace, so types, the logger and DebugGuru must be exported before the connection
    and client classes are imported.
"""
__version__ = "1.0.2"  # Defined before the imports below, which read it

from mcpagent.client.types import (
    ConfigType,
    EventCallbackType,
    HTTPConfigType,
    JSONRPCResponseCoroType,
    JSONRPCResponseTaskType,
    ListenEventsReturnType,
    MCPTransportType,
    RequestReturnType,
    ResponseCallbackType,
    STDIOConfigType,
)
from mcpagent.common.logger import MCPAgentLogger
from mcpagent.client.debug import DebugGuru
from mcpagent.client.connection import MCPClientConnection
from mcpagent.client.client import MCPClient
from mcpagent.server.service import MCPService

__all__ = [
    "__version__",

    # Client
    "MCPClient", "MCPClientConnection", "MCPAgentLogger",

    # Server
    "MCPService",

    # Client types
    "MCPTransportType", "HTTPConfigType", "STDIOConfigType", "ConfigType",
    "ResponseCallbackType", "EventCallbackType", "RequestReturnType", "ListenEventsReturnType",
    "JSONRPCResponseCoroType", "JSONRPCResponseTaskType", "DebugGuru",
]
