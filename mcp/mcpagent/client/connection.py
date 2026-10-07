"""
Module: connection.py

Description:
    `MCPClientConnection`: one connection to one MCP server, hiding the transport.
      - HTTP: JSON-RPC requests over aiohttp, with SSE for server notifications.
      - STDIO: newline-delimited JSON over a child process's stdin/stdout.
    Provides the handshake and session, requests (await or callback style), notifications,
    event listening and `ping()` health checks.
"""

import asyncio
import contextlib
import inspect
import json
import logging
import itertools
import time
from typing import Any, AsyncGenerator, Optional, Union

# Third-party
import aiohttp

# Local imports
from mcpagent import (
    ConfigType,
    DebugGuru,
    EventCallbackType,
    HTTPConfigType,
    JSONRPCResponseCoroType,
    JSONRPCResponseTaskType,
    ListenEventsReturnType,
    MCPAgentLogger,
    MCPTransportType,
    ResponseCallbackType,
    STDIOConfigType,
)


class MCPClientConnection:
    """
    Encapsulates a single MCP client to server connection.
    Supports multiple transport types:
      - HTTP (using aiohttp sessions)
      - STDIO (using subprocess pipes)
    """

    def __init__(self, server_id: str, transport: MCPTransportType,
                 config: ConfigType,
                 capabilities: Optional[dict] = None,
                 log_level: Optional[int] = logging.DEBUG,
                 debug_guru: Optional[DebugGuru] = None) -> None:
        """
        Manages a single MCP server connection.
        Args:
            server_id (str): Unique identifier for this connection (e.g., "srv1").
            transport (MCPTransportType): Transport type to use. Supported: HTTP, STDIO.
            config (ConfigType): Transport-specific configuration:
                - HTTPConfigType:
                    url (str): Base URL of the MCP server.
                    headers (Optional[dict[str, str]]): Extra HTTP headers.
                    sse_url (Optional[str]): Override for SSE event endpoint.
                - STDIOConfigType:
                    command (list[str]): Subprocess command and arguments to run.
                    env (Optional[dict[str, str]]): Environment variables for the subprocess.
            capabilities (Optional[dict]): Optional capabilities that declare server-specific requirements.
            log_level (Optional[int]): Logging level for this connection.
            debug_guru (Optional[DebugGuru]): Optional debugging helper instance.
        """
        self._server_id: str = server_id
        self._transport: MCPTransportType = transport
        self._config: ConfigType = config
        self._stop_event: asyncio.Event = asyncio.Event()
        self._last_error = None
        self._capabilities: dict = capabilities or {}
        self._request_ids = itertools.count(1)
        self.protocol_version = None
        self._session_id: Optional[Union[str, int]] = None  # May be sent by a service
        self._debug_guru: Optional[DebugGuru] = debug_guru

        # Configure logger
        self._logger = MCPAgentLogger("Connection")
        if log_level is None:
            self._logger.disabled = True
        else:
            self._logger.setLevel(level=log_level)

        # Transport-specific state
        self._http_session: Optional[aiohttp.ClientSession] = None
        self._last_seen: Optional[float] = None
        self._lock = asyncio.Lock()

        # STDIO state
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None

    async def connect(self) -> None:
        """
        Establish the server connection.
        - For HTTP transport: opens an aiohttp ClientSession.
        - For STDIO transport: spawns the configured subprocess with
          stdin/stdout pipes and prepares for JSON-RPC message exchange.
        """
        if self._transport == MCPTransportType.HTTP:
            self._http_session = aiohttp.ClientSession()

        elif self._transport == MCPTransportType.STDIO:
            assert isinstance(self._config, STDIOConfigType)
            cmd = self._config.command
            if not cmd:
                raise ValueError("STDIO transport requires a 'command' in config")

            # Spawn subprocess with pipes
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._proc = proc
            self._reader = proc.stdout
            self._writer = proc.stdin

            # Start a background task to read stderr for logging
            asyncio.create_task(self._log_stderr())

    async def _log_stderr(self) -> None:
        """
        Continuously read the subprocess's stderr stream and print
        lines for debugging/logging purposes.

        Runs as a background task once a STDIO process is spawned.
        """
        assert self._proc and self._proc.stderr
        async for line in self._proc.stderr:
            self._logger.error(f"{self._server_id} STDERR: {line.decode().rstrip()}")

    async def stdio_write(self, message: dict) -> None:
        """
        Send a JSON message to the connected server.
        For STDIO transport, the message is serialized as a single JSON line
        (newline-delimited). If you need LSP-style `Content-Length` framing,
        this method must be extended.
        Args:
            message (dict): The JSON-serializable message to send.
        Raises:
            RuntimeError: If the writer is not initialized.
        """

        if self._transport == MCPTransportType.STDIO:
            if not self._writer:
                raise RuntimeError("STDIO writer not initialized")
            data = json.dumps(message) + "\n"
            self._writer.write(data.encode())
            await self._writer.drain()
        else:
            raise NotImplementedError("send_message only supports STDIO in this version")

    async def stdio_read(self, timeout: float = 5.0) -> dict:
        """
        Read the next JSON message from the connected server.
        For STDIO transport, this expects newline-delimited JSON messages.
        Args:
            timeout (float): Maximum number of seconds to wait for a message
                             before raising TimeoutError. Defaults to 5.0.
        Returns:
            dict: The decoded JSON message.
        Raises:
            RuntimeError: If the reader is not initialized.
            EOFError: If the process closes its stdout unexpectedly.
            TimeoutError: If no message arrives before the timeout expires.
        """
        if self._transport == MCPTransportType.STDIO:
            if not self._reader:
                raise RuntimeError("STDIO reader not initialized")
            try:
                line = await asyncio.wait_for(self._reader.readline(), timeout=timeout)
            except asyncio.TimeoutError:
                raise TimeoutError(f"No message received within {timeout:.1f}s") from None

            if not line:
                raise EOFError("STDIO process closed")

            return json.loads(line.decode())
        else:
            raise NotImplementedError("read_message only supports STDIO in this version")

    def listen(self,
               callback: Optional[EventCallbackType] = None) -> ListenEventsReturnType:
        """
        Unified API for receiving asynchronous events across transports.
        HTTP transport:
            Connects to the server `/sse` endpoint, consuming messages via Server-Sent Events (SSE). Each `data:`
            line is parsed as JSON and yielded. If the stream breaks, the listener retries after a short delay.
        STDIO transport:
            Reads newline-delimited JSON objects from subprocess stdout. Messages without `"id"` are treated as
            events and yielded. EOF is terminal (no retries).
        Args:
            callback (Optional[Callable[[dict], None]]): Function invoked per event.
                - If provided: runs in background, returns an `asyncio.Task`.
                - If omitted: returns an async generator yielding event dicts.
        Returns:
            ListenEventsReturnType: Async generator of events (await mode) or asyncio.Task (callback mode).
        """
        if self._transport == MCPTransportType.HTTP:
            if not self._http_session:
                raise RuntimeError("Connection not established. Call connect().")
            assert isinstance(self._config, HTTPConfigType)

            url = (self._config.sse_url or self._config.url).rstrip("/") + "/sse"
            session, headers = self._http_session, self._config.headers
            retry_interval = 5.0  # Seconds between reconnect attempts

            async def _http_events() -> AsyncGenerator[dict, None]:
                """
                Reconnect loop for SSE events.
                Works with any MCP-compliant server:
                  - Collects full SSE events (multi-line data: blocks).
                  - Decodes JSON payloads into dicts.
                  - Yields each payload upstream.
                Policy decisions (e.g., how to use sessionId or capabilities)
                are handled by higher-level logic, not here.
                """
                buffer: list[str] = []
                while True:
                    try:
                        async with session.get(url=url, headers=headers) as resp:
                            resp.raise_for_status()
                            async for raw_line in resp.content:
                                line = raw_line.decode(errors="ignore").strip()

                                if not line:
                                    # Blank line -> end of one SSE event
                                    if buffer:
                                        try:
                                            data_lines = [line[5:].strip() for line in buffer if line.startswith("data:")]
                                            payload_str = "\n".join(data_lines)
                                            payload = json.loads(payload_str)
                                            yield payload
                                        except json.JSONDecodeError:
                                            self._logger.debug("Malformed SSE payload: %s", buffer)
                                        buffer = []
                                    continue

                                buffer.append(line)

                    except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError) as http_error:
                        if str(http_error) != self._last_error:
                            self._logger.warning(
                                f"Listener for {self._server_id} stopped: {http_error}, "
                                f"retrying in {int(retry_interval)} seconds"
                            )
                            self._last_error = str(http_error)
                        await asyncio.sleep(retry_interval)
                        continue

                    except Exception as exception:
                        self._logger.error(f"Listener for {self._server_id} crashed: {exception}")
                        break

            events = _http_events


        elif self._transport == MCPTransportType.STDIO:
            assert isinstance(self._config, STDIOConfigType)
            if not self._reader:
                raise RuntimeError("STDIO reader not initialized. Call connect().")
            reader = self._reader

            async def _stdio_events() -> AsyncGenerator[dict, None]:
                """STDIO event loop; EOF means exit."""
                while True:
                    line = await reader.readline()
                    if not line:
                        self._logger.warning(f"STDIO listener for {self._server_id} terminated (EOF)")
                        break
                    try:
                        payload = json.loads(line.decode())
                    except json.JSONDecodeError:
                        continue
                    if "id" not in payload:  # notifications/events
                        yield payload

            events = _stdio_events

        else:
            raise NotImplementedError(f"Unsupported transport: {self._transport}")

        if callback:
            if not callable(callback):
                raise TypeError("'callback' must be callable")

            async def _runner() -> None:
                async for event in events():
                    cb_result = callback(event)
                    if cb_result is not None and inspect.isawaitable(cb_result):
                        with contextlib.suppress(Exception):
                            await cb_result

            return asyncio.create_task(_runner())

        return events()

    def request(self,
                method: str,
                params: dict[str, Any],
                callback: Optional[ResponseCallbackType] = None,
                timeout: float = 5.0, ) -> Union[JSONRPCResponseCoroType, JSONRPCResponseTaskType]:
        """
        Send a JSON-RPC request to the MCP server.
        Supports two modes:
            - Await mode: returns a coroutine yielding the JSON-RPC response.
            - Callback mode: schedules a background Task and returns it.
        Args:
            method (str): JSON-RPC method name.
            params (dict[str, Any]): Parameters for the RPC call.
            callback (Optional[ResponseCallbackType]): Function invoked with (result, error). May be sync or async. If set,
                the request runs in the background and returns an asyncio.Task.
            timeout (float): Timeout in seconds. Defaults to 5.0.
        Returns:
            Union[JSONRPCResponseCoroType, JSONRPCResponseTaskType]: Coroutine in await mode, Task in callback mode.
        """

        async def _do_request() -> dict[str, Any]:

            request_id = next(self._request_ids)
            headers = self._request_headers()

            payload = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }

            # Exception: Jenkins initialize
            if method == "initialize" and self._capabilities.get("initialize_via") == "sse":
                headers["Accept"] = "text/event-stream"

            async with self._lock:
                if self._transport == MCPTransportType.HTTP:
                    if not self._http_session:
                        raise RuntimeError("Connection not established.")
                    assert isinstance(self._config, HTTPConfigType)

                    url = self._config.url
                    if self._debug_guru:
                        if self._debug_guru:
                            debug_info = {"url": url, "header_names": list(headers), "payload": payload}
                            self._debug_guru.show_box(
                                title=f"Request to '{self._server_id}'",
                                debug_data=debug_info
                            )
                    async with self._http_session.post(url, json=payload, headers=headers,
                                                       timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                        try:
                            resp.raise_for_status()
                        except aiohttp.ClientResponseError as e:
                            if self._debug_guru:
                                self._debug_guru.show_box(
                                    title=f"HTTP error from '{self._server_id}'",
                                    debug_data={
                                        "status": resp.status,
                                        "url": str(resp.url),
                                        "message": str(e),
                                        "body": await resp.text()
                                    }
                                )
                            raise

                        # Special case: initialize via SSE
                        if method == "initialize" and self._capabilities.get("initialize_via") == "sse":
                            self._last_seen = time.time()
                            return {
                                "jsonrpc": "2.0",
                                "id": request_id,
                                "status": resp.status,
                                "note": "Initialize sent (SSE mode); listen on /sse for actual result",
                            }

                        if method == "initialize":
                            self._session_id = resp.headers.get("Mcp-Session-Id")

                        # Normal synchronous response
                        data = await resp.json()
                        self._last_seen = time.time()

                        if self._debug_guru:
                            self._debug_guru.show_box(title=f"Response from '{self._server_id}'", debug_data=data)
                        return data

                elif self._transport == MCPTransportType.STDIO:
                    if not self._writer or not self._reader:
                        raise RuntimeError("STDIO connection not established. Call connect().")
                    assert isinstance(self._config, STDIOConfigType)

                    # Send newline-delimited JSON
                    self._writer.write((json.dumps(payload) + "\n").encode())
                    await self._writer.drain()

                    try:
                        line = await asyncio.wait_for(self._reader.readline(), timeout=timeout)
                    except asyncio.TimeoutError:
                        raise TimeoutError(f"Request {request_id} timed out after {timeout:.1f}s") from None

                    if not line:
                        raise EOFError("STDIO server closed the connection")

                    data = json.loads(line.decode())
                    self._last_seen = time.time()
                    return data

                else:
                    raise NotImplementedError(f"Unsupported transport: {self._transport}")

        if callback:
            if not callable(callback):
                raise TypeError("'callback' must be callable")

            async def _runner() -> dict[str, Any]:
                cb_result = None  # always defined
                try:
                    result = await _do_request()
                    cb_result = callback(result, None)
                    return result
                except Exception as e:
                    cb_result = callback(None, e)
                    # Swallow the exception because callback is the reporting channel
                    return {"error": str(e)}
                finally:
                    if inspect.isawaitable(cb_result):
                        with contextlib.suppress(Exception):
                            await cb_result

            return asyncio.create_task(_runner())

        return _do_request()

    def _request_headers(self):
        """
        Build the HTTP headers for a JSON-RPC request.
        Returns:
            dict[str, str]: The configured headers plus Accept, and the session id and negotiated
            protocol version once the server has provided them.
        """
        headers = dict(self._config.headers or {}) if isinstance(self._config, HTTPConfigType) else {}
        headers.setdefault("Accept", "application/json, text/event-stream")
        if self._session_id:
            headers["Mcp-Session-Id"] = str(self._session_id)
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        return headers

    async def notify(self, method: str, params: Optional[dict] = None) -> None:
        """
        Send a JSON-RPC notification (a message with no id, so no response is expected).
        Args:
            method: The notification method, e.g. "notifications/initialized".
            params: Optional parameters.
        Raises:
            RuntimeError: If the HTTP connection is not established.
        """
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        async with self._lock:
            if self._transport == MCPTransportType.HTTP:
                if self._http_session is None:
                    raise RuntimeError("Connection not established")
                assert isinstance(self._config, HTTPConfigType)
                async with self._http_session.post(
                    self._config.url, json=payload, headers=self._request_headers(),
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as response:
                    response.raise_for_status()
            else:
                await self.stdio_write(payload)

    async def close(self, proc_grace_time: float = 2.0) -> None:
        """
        Close the server connection and release resources.
            - HTTP transport: closes the aiohttp session.
            - STDIO transport: terminates the subprocess and closes its pipes.
        Args:
            proc_grace_time: Seconds to wait for a STDIO subprocess to exit before killing it.
        """
        if self._transport == MCPTransportType.HTTP:
            if self._http_session:
                await self._http_session.close()
                self._http_session = None

        elif self._transport == MCPTransportType.STDIO:
            if self._writer:
                try:
                    self._writer.close()
                    await self._writer.wait_closed()

                except (BrokenPipeError, ConnectionResetError, OSError):
                    # Expected pipe closure or reset during shutdown
                    pass
                self._writer = None

            if self._proc:
                self._proc.terminate()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=proc_grace_time)
                except asyncio.TimeoutError:
                    self._proc.kill()
                    await self._proc.wait()
                self._proc = None
            self._reader = None

    @property
    def config(self) -> ConfigType:
        """
        Return the typed configuration object for this connection.

        - If transport == HTTP, returns an HTTPConfigType.
        - If transport == STDIO, returns a STDIOConfigType.
        - Otherwise raises NotImplementedError.

        Callers may use `isinstance` or check `self._transport`
        to safely access transport-specific fields.
        """
        if self._transport == MCPTransportType.HTTP:
            assert isinstance(self._config, HTTPConfigType)
            return self._config
        elif self._transport == MCPTransportType.STDIO:
            assert isinstance(self._config, STDIOConfigType)
            return self._config
        else:
            raise NotImplementedError(f"Unsupported transport: {self._transport}")

    @property
    def transport(self) -> Optional[MCPTransportType]:
        """Return the connection selected transport type"""
        return self._transport

    @property
    def last_seen(self) -> Optional[float]:
        """Return the last-seen timestamp for this connection, if available."""
        return self._last_seen

    @property
    def session_id(self) -> Optional[Union[str, int]]:
        """Get the current session ID (string, int, or None)."""
        return self._session_id

    @session_id.setter
    def session_id(self, value: Optional[Union[str, int]]) -> None:
        """
        Set the session ID.
        Args:
            value: The id the server assigned (string or int), or None to clear it.
        Raises:
            TypeError: If the value is not a string, int or None.
        """
        if value is not None and not isinstance(value, (int, str)):
            raise TypeError(f"session_id must be int, str, or None, got {type(value).__name__}")
        self._session_id = value
