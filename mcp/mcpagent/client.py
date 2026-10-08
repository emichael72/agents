"""
Module: client.py

Description:
    Implements the MCP client, which manages connections to one or more MCP servers over different
    transport types (HTTP or STDIO).

    The class provides:
      - Registration and lifecycle management of server connections.
      - A unified JSON-RPC API (`request`) that supports both await and
        callback styles, abstracting away transport differences.
      - Event streaming (`listen`) for server-initiated notifications,
        mapped to SSE for HTTP transport and newline-delimited JSON for
        STDIO transport.
      - Case-insensitive server identifiers for consistent lookups.

    Internally, each server connection is represented by an
    `MCPClientConnection` instance (connection.py), which holds the
    transport-specific state, so this module needs neither:
      - HTTP: an aiohttp session.
      - STDIO: an asyncio subprocess with stdin/stdout pipes (the agent's own
        server is one).

    The design goal is to present a consistent high-level interface to callers
    while cleanly separating transport-specific concerns.
"""
import asyncio
import contextlib
import logging
import time
from pathlib import Path
from typing import Any, Callable, Optional, Union

# Local imports
from mcpagent import __version__  # Sent to servers in the initialize handshake
from mcpagent.connection import MCPClientConnection
from mcpagent.types import (
    ConfigType,
    EventCallbackType,
    HTTPConfigType,
    ListenEventsReturnType,
    MCPTransportType,
    RequestReturnType,
    ResponseCallbackType,
    STDIOConfigType,
)
from mcpagent.logger import MCPAgentLogger
from mcpagent.config import MCPAgentConfig


class MCPClient:
    """
    MCP client for managing connections to multiple servers.
    """

    def __init__(self, config_file: Union[Path, str]):
        """
        Load configuration and dynamically add servers.
        Args:
            config_file: Path to the configuration file (Path or str).
        """
        self._servers: dict[str, Any] = {}  # Registry of known server connections
        self._ping_stop = asyncio.Event()  # Used to signal the background ping thread to stop.
        self._health_check_task: Optional[asyncio.Task] = None
        self._logger = MCPAgentLogger("Client")
        self._log_level: Optional[int] = None

        # Load configuration and optionally validate using a schema
        self._config_data: dict[str, Any] = self._load_config(config_file=config_file)

        # Logger configuration
        level_str = str(self._config_data.get("log_level", "WARNING")).upper()
        level = getattr(logging, level_str, None)
        if isinstance(level, int):
            self._log_level = level
            self._logger.setLevel(level)
        else:
            # Invalid level name: fall back and warn
            self._logger.setLevel(logging.WARNING)
            self._logger.warning(
                f"Invalid log_level '{level_str}' in config. Falling back to WARNING."
            )

        # Load service from configuration
        self._load_servers_from_config()

    @staticmethod
    def _load_config(config_file: Union[Path, str]) -> dict[str, Any]:
        """
        Load an MCPAgent configuration file (JSON).
        Args:
            config_file: Path to the configuration file (Path or str).
        Returns:
            dict[str, Any]: Its settings, with the command that starts the agent's own server filled in.
        Raises:
            RuntimeError: If the file is missing or fails schema validation.
        """
        return MCPAgentConfig.load(config_file).settings

    def _load_servers_from_config(self) -> None:
        """
        Initialize all servers defined in the loaded configuration file.
        Iterates over the "servers" list in self._config_data and registers
        each server using add_server(). Invalid entries are skipped with a warning.
        """
        servers: list[dict[str, Any]] = self._config_data.get("servers", [])

        if not servers:
            self._logger.warning("No servers defined in configuration.")
            return

        for i, srv in enumerate(servers, start=1):
            try:
                server_id: str = srv["server_id"]

                # Skip servers which are explicitly marked as disabled
                enabled: bool = srv.get("enabled", True)
                if not enabled:
                    self._logger.warning(f"Server '{server_id}' disabled (skipped)")
                    continue

                transport_str: str = srv["transport"].upper()
                health_check_interval: Optional[float] = srv.get("health_check_interval")
                capabilities: Optional[dict] = srv.get("capabilities")

                # Normalize transport type into your enum
                transport_enum = MCPTransportType(transport_str)
                raw_cfg: dict[str, Any] = srv.get("config", {})

                if transport_enum is MCPTransportType.HTTP:
                    config = HTTPConfigType(
                        url=raw_cfg["url"],
                        headers=raw_cfg.get("headers"),
                        sse_url=raw_cfg.get("sse_url"))
                elif transport_enum is MCPTransportType.STDIO:
                    config = STDIOConfigType(
                        command=raw_cfg["command"],
                        env=raw_cfg.get("env"))
                else:
                    raise ValueError(f"Unsupported transport: {transport_str}")

                self.add_server(
                    server_id=server_id,
                    transport=transport_enum,
                    config=config,
                    capabilities=capabilities,
                    health_check_interval=health_check_interval)

                self._logger.info(
                    f"Server '{server_id}' ({transport_str}), "
                    f"health_check_interval={health_check_interval} added."
                )

            except Exception as e:
                self._logger.error(
                    f"Server entry #{i} (id={srv.get('server_id')}) could not be added due to error: {e}"
                )

    async def _health_check(self) -> None:
        """
        Periodically ping all servers that have `health_check_interval` configured.
        Updates each server's `connected` flag and `last_ping` timestamp.
        Runs until `_ping_stop` is set, with a small pause between iterations.
        """
        while not self._ping_stop.is_set():
            now = time.time()
            for server_id, server_data in self._servers.items():
                conn = self._get_connection(server_id=server_id)
                if not isinstance(conn, MCPClientConnection):
                    raise RuntimeError(f"Invalid connection object for server {server_id}")

                interval = server_data.get("health_check_interval")
                last_ping = server_data.get("last_ping", 0)

                if interval is None or now - last_ping < interval:
                    continue

                prev_state = server_data.get("state", "unknown")
                recover_count = server_data.get("recover_count", 0)

                try:
                    resp = await conn.request(method="ping", params={}, timeout=3.0)
                    ok = isinstance(resp, dict) and "result" in resp and "error" not in resp
                except Exception as e:
                    ok = False
                    if prev_state == "up":  # Transitioned from up → down
                        self._logger.error("Server '%s' ping error: %s → marking disconnected", server_id, e)

                if ok:
                    if prev_state in ("down", "unknown"):
                        if recover_count >= 1:
                            self._logger.info("Server '%s' is back online", server_id)
                            server_data["state"] = "up"
                            recover_count = 0
                        else:
                            recover_count += 1
                    else:
                        pass  # Already up, no log
                else:
                    if prev_state in ("up", "unknown"):
                        self._logger.error("Server '%s' is offline", server_id)
                    server_data["state"] = "down"
                    recover_count = 0

                server_data["recover_count"] = recover_count
                server_data["last_ping"] = now

            await asyncio.sleep(0.5)

    def _get_connection(self, server_id: str) -> Optional[MCPClientConnection]:
        """
        Retrieve the MCPClientConnection object for a given server ID.
        Args:
            server_id: The ID of the server whose connection is requested.
        Returns:
            The MCPClientConnection instance if found, else None.
        """
        server = self._servers[server_id.lower()]
        conn = server.get("conn") if isinstance(server, dict) else None
        if not isinstance(conn, MCPClientConnection):
            return None

        return conn

    def add_server(
            self,
            server_id: str,
            transport: MCPTransportType,
            config: ConfigType,
            capabilities: Optional[dict] = None,
            health_check_interval: Optional[float] = None,
    ) -> None:
        """
        Register a new MCP server connection.
        Server identifiers are case-insensitive and normalized to lowercase (e.g., "Local_Service"
        and "local_service" refer to the same server).

        Args:
            server_id (str): Unique identifier for the server (e.g., "local_service").
            transport (MCPTransportType): Transport type (HTTP or STDIO).
            config (ConfigType): Transport-specific configuration:
                - HTTPConfigType: requires `url`, optional `headers`, `sse_url`.
                - STDIOConfigType: requires `command`, optional `env`.
            capabilities (Optional[dict]): Optional capabilities that declare server-specific requirements.
            health_check_interval (Optional[float]): Interval in seconds for automatic health checks. If None, disabled.
        """
        normalized_id = server_id.lower()
        if normalized_id in self._servers:
            raise ValueError(f"Server '{server_id}' already exists.")

        # Create new connection and delegate our log level
        conn: MCPClientConnection = MCPClientConnection(server_id=server_id, transport=transport,
                                                        config=config, log_level=self._log_level,
                                                        capabilities=capabilities)
        self._servers[normalized_id] = {
            "conn": conn,
            "health_check_interval": health_check_interval,
            "last_ping": 0.0,
            "recover_count": 0,
            "state": "unknown"  # "unknown" | "up" | "down"
        }

    def on_server_output(self, callback: Optional[Callable[[str], None]]) -> None:
        """
        Pass each line the STDIO servers write to stderr (their log) to a callback.
        Args:
            callback: Called with each line; None drops them (the default).
        """
        for sid in self._servers:
            conn = self._get_connection(server_id=sid)
            if isinstance(conn, MCPClientConnection):
                conn.on_stderr = callback

    async def connect(self,
                      server_id: Optional[str] = None,
                      connect_all: bool = False,
                      send_init: bool = True) -> Union[MCPClientConnection, list[MCPClientConnection]]:
        """
        Establish connections to MCP servers.
        Args:
            server_id: ID of a specific server to connect. Ignored if `connect_all=True`.
            connect_all: If True, connect to all registered servers.
            send_init: If True (default), send an MCP "initialize" request
                immediately after connecting.

        Returns:
            - MCPClientConnection if a single server_id was connected.
            - List[MCPClientConnection] if connect_all=True.
        """
        if connect_all and server_id:
            raise ValueError("Specify either server_id or connect_all=True, not both.")

        if not connect_all and not server_id:
            raise ValueError("Must specify either server_id or connect_all=True.")

        async def _connect_one(_sid: str) -> MCPClientConnection:
            conn = self._get_connection(server_id=_sid)
            if not isinstance(conn, MCPClientConnection):
                raise RuntimeError(f"Invalid connection object for server {_sid}")

            # Start the listener in the background note  Connection not established ?
            # _listener_task = await self.listen(server_id=, _sid)

            await conn.connect()

            if send_init:
                try:
                    response = await conn.request("initialize", {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "mcpagent", "version": __version__},
                    })
                    if "error" in response or "result" not in response:
                        raise RuntimeError(f"MCP initialization failed for {_sid}")
                    conn.protocol_version = response["result"]["protocolVersion"]
                    await conn.notify("notifications/initialized")
                except Exception:
                    await conn.close()
                    raise

            return conn

        # Connect all or single server
        if connect_all:
            conns = []
            for sid in self._servers.keys():
                conns.append(await _connect_one(sid))
            result: Union[MCPClientConnection, list[MCPClientConnection]] = conns
        else:
            assert server_id is not None  # Guaranteed by the argument checks above
            result = await _connect_one(server_id)

        # Start background health check after initialization
        if self._health_check_task is None:
            self._health_check_task = asyncio.create_task(self._health_check())

        return result

    async def close(self,
                    server_id: Optional[str] = None,
                    close_all: bool = False) -> None:
        """
        Close connections to MCP servers.
        Args:
            server_id: ID of a specific server to close. Ignored if `close_all=True`.
            close_all: If True, close all registered servers.
        """
        if close_all and server_id:
            raise ValueError("Specify either 'server_id' or 'close_all=True', not both.")
        if not close_all and not server_id:
            raise ValueError("Must specify either 'server_id' or 'close_all=True'.")

        if close_all:

            self._logger.warning("Closing all registered servers.")

            # Stop background health checks
            self._ping_stop.set()

            if self._health_check_task:
                self._health_check_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._health_check_task
                self._health_check_task = None

            for sid in self._servers:
                conn = self._get_connection(server_id=sid)
                if not isinstance(conn, MCPClientConnection):
                    raise RuntimeError(f"Invalid connection object for server {sid}")
                await conn.close()

        else:
            assert server_id is not None  # Guaranteed by the argument checks above
            conn = self._get_connection(server_id)
            if conn is None:
                raise RuntimeError(f"Server '{server_id}' has no open connection")
            await conn.close()

    def request(self,
                method: str,
                params: dict[str, Any],
                server_id: Optional[str] = None,
                callback: Optional[ResponseCallbackType] = None,
                timeout: float = 5.0) -> Union[RequestReturnType, dict[str, RequestReturnType]]:
        """
        Send a JSON-RPC request to one or more registered servers.
        Hybrid API:
            - Await mode → returns coroutine(s) that must be awaited to get responses.
            - Callback mode → schedules background Task(s) that deliver responses
              (or errors) to the callback.
        Args:
            method (str):
                JSON-RPC method name to invoke.
            params (dict[str, Any]):
                Parameters for the RPC call.
            server_id (Optional[str]):
                If provided, target only this server. Otherwise, broadcast to all
                connected servers.
            timeout (float):
                Timeout in seconds (default 5.0).
            callback (Optional[ResponseCallbackType]):
                Optional function invoked with (result, error).
                  - If provided → background tasks are scheduled.
                  - If omitted → you must await the coroutine(s).
        Returns:
            - Single-server: coroutine or Task (depending on callback)
            - Multiple servers: dict[server_id, coroutine|Task]
        Raises:
            ConnectionError:
                If the target server is known to be disconnected.
            RuntimeError:
                If a server entry does not contain an MCPClientConnection.
        """

        # Single-server mode
        if server_id:
            conn = self._get_connection(server_id=server_id)
            if not isinstance(conn, MCPClientConnection):
                raise RuntimeError(f"Invalid connection object for server {server_id}")

            server_data = self._servers.get(server_id.lower(), {})
            if not server_data.get("connected", True):
                if callback:
                    callback(None, ConnectionError(f"Server '{server_id}' not connected"))
                    loop = asyncio.get_running_loop()

                    async def _disconnected_server_result() -> dict[str, Any]:
                        return {"error": f"Server '{server_id}' not connected"}

                    return loop.create_task(_disconnected_server_result())
                raise ConnectionError(f"Server '{server_id}' not connected")
            return conn.request(method=method, params=params, timeout=timeout, callback=callback)

        # Broadcast to all servers
        results: dict[str, RequestReturnType] = {}
        for sid, server_data in self._servers.items():
            conn = self._get_connection(server_id=sid)
            if not isinstance(conn, MCPClientConnection):
                raise RuntimeError(f"Invalid connection object for server {sid}")

            if not server_data.get("connected", True):
                if callback:
                    callback(None, ConnectionError(f"Server '{sid}' not connected"))
                    loop = asyncio.get_running_loop()

                    # Bind this server's id now: the task runs after the loop has moved on
                    async def _disconnected_broadcast_result(_server_id: str = sid) -> dict[str, Any]:
                        return {"error": f"Server '{_server_id}' not connected"}

                    results[sid] = loop.create_task(_disconnected_broadcast_result())
                else:
                    raise ConnectionError(f"Server '{sid}' not connected")
                continue

            results[sid] = conn.request(method=method, params=params, timeout=timeout, callback=callback)
        return results

    async def call(self,
                   method: str,
                   params: dict[str, Any],
                   server_id: str,
                   timeout: float = 5.0) -> dict[str, Any]:
        """
        Send a JSON-RPC request to one server and wait for its response.
        Args:
            method (str): JSON-RPC method name to invoke.
            params (dict[str, Any]): Parameters for the RPC call.
            server_id (str): The server to send the request to.
            timeout (float): Timeout in seconds (default 5.0).
        Returns:
            dict[str, Any]: The JSON-RPC response.
        """
        response = self.request(method, params, server_id=server_id, timeout=timeout)
        assert not isinstance(response, dict)  # A single server without a callback yields a coroutine
        return await response

    def listen(self,
               server_id: str,
               callback: Optional[EventCallbackType] = None) -> ListenEventsReturnType:
        """
        Listen for asynchronous events from a specific MCP server.
        Unified event stream regardless of transport:
          - For HTTP transport, events are consumed from the server's `/sse` endpoint
            using the Server-Sent Events (SSE) protocol.
          - For STDIO transport, events are read continuously from the subprocess's
            stdout; any JSON object without an `"id"` field is treated as an event.

        Hybrid API:
            - Await mode → returns an async generator yielding events (`async for`).
            - Callback mode → schedules a background Task that invokes the callback
              for each event.
        Args:
            server_id (str):
                Identifier of the server to listen to.
            callback (Optional[EventCallbackType]):
                Optional handler for events.
                  - If provided → the listener runs in the background and returns
                    an `asyncio.Task`.
                  - If omitted → the method returns an async generator.
        Returns:
            ListenEventsReturnType:
                - Await mode → AsyncGenerator[dict, None]
                - Callback mode → asyncio.Task[None]
        """

        conn = self._get_connection(server_id=server_id)
        if not isinstance(conn, MCPClientConnection):
            raise TypeError(f"Invalid MCPClientConnection for server {server_id}")

        return conn.listen(callback=callback)

    @property
    def config_data(self) -> dict[str, Any]:
        """Return the processed configuration data as a dictionary"""
        return self._config_data

    @property
    def log_level(self) -> Optional[int]:
        """Return the modules selected logging level"""
        return self._log_level
