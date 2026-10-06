# noinspection SpellCheckingInspection
"""
Module: service.py

Description:
    Implements `MCPService`, the MCP server: it exposes command-line tools to MCP clients as
    JSON-RPC 2.0 over HTTP (aiohttp).

    The service provides:
      - Tool discovery from a tools folder (`tools_dir`, one <tool>/tool.json manifest per
        tool) and/or inline tool definitions in the server config.
      - `initialize`, `tools/list`, `tools/call`, `resources/list` and `resources/read`, with
        tool arguments validated against each tool's JSON schema.
      - Command execution as subprocesses, with each tool's working directory and environment.

    Key design points:
      - Single-flight: only one tool runs at a time; a concurrent `tools/call` is rejected
        with a JSON-RPC "Busy" error.
      - Errors are returned as JSON-RPC envelopes, never as HTTP 500.
      - No authentication: bind to localhost (the default) unless that is acceptable.
"""

import asyncio
import contextlib
import json
import os
import re
import shlex
import signal
from datetime import datetime
from json import JSONDecodeError
from pathlib import Path
from typing import Optional, Any, Union
from urllib.parse import urlparse, parse_qsl, unquote

# Third-party
from aiohttp import web
from jsonschema import validate, ValidationError
from colorama import Fore, Style

# Local imports
from .logger import MCPAgentLogger
from .types import MCPServiceConfigType, MCPServiceToolType

# The default config lives next to this module
DEFAULT_CONFIG = Path(__file__).resolve().parent / "server.jsonc"

MAX_BATCH_MCP_COMMANDS = 64
BUSY_CODE = -32004


class MCPService:
    """
    The MCP server: serves the configured tools to MCP clients as JSON-RPC 2.0 over HTTP.
    Tools come from the config's tools folder (`tools_dir`) and/or inline `tools`. Only one
    tool runs at a time; see the module description for the design points.
    """

    def __init__(self, project_data: Optional[dict] = None) -> None:
        """
        Load the tools and server settings from the parsed server config, and set up the routes.
        Args:
            project_data: The parsed server config (server.jsonc): server name and port, bind
                address, allowed browser origins, tools_dir / tools_env and inline tools.
        Raises:
            TypeError: If project_data is not a dict.
        """

        self._single_flight = asyncio.Semaphore(1)  # Single-flight across the whole workspace
        self._current = None  # (tool_name, started_at) for status/telemetry

        self._mcp_config = MCPServiceConfigType()
        self._logger = MCPAgentLogger("Service")
        self._shutdown_event = asyncio.Event()
        self._tools_registry: dict[str, MCPServiceToolType] = {}
        self._patch_vscode_config: bool = False
        self._show_usage_examples: bool = False
        self._shutting_down: bool = False
        self._log_request: bool = False
        self._brutal_termination: bool = False
        self._project_base_path: str = os.getcwd()

        if not isinstance(project_data, dict):
            raise TypeError("project_data must be a dict")
        self._project_data: dict[str, Any] = project_data

        # Override defaults using configuration optional parameters
        self._show_usage_examples = self._project_data.get("show_usage_examples", self._show_usage_examples)
        self._patch_vscode_config = self._project_data.get("patch_vscode_config", self._patch_vscode_config)
        self._tool_prefix: str = (self._project_data.get("tool_prefix") or self._project_data.get("tools_prefix") or "")

        self._mcp_server_name: str = self._project_data.get("project_name", "MCP service")
        self._mcp_server_version: str = self._project_data.get("version", "1.0.0")
        self._tools_data: dict[str, Any] = self._project_data.get("tools", {})

        # Optional shared tools folder: one sub-folder per tool, each with a tool.json manifest
        tools_dir = self._project_data.get("tools_dir")
        if tools_dir:
            discovered = self._discover_tools(tools_dir, self._project_data.get("tools_env", {}))
            self._tools_data = {**discovered, **self._tools_data}
            self._project_data["tools"] = self._tools_data

        # Port and optional host bind address
        self._mcp_server_port: int = self._project_data.get("mcp_server_port", self._mcp_config.port)
        self._mcp_config.port = self._mcp_server_port
        self._mcp_server_bind_address: Optional[str] = self._project_data.get("mcp_server_bind_address")

        @web.middleware
        async def check_origin(request, handler):
            origin = request.headers.get("Origin")
            allowed = self._project_data.get("allowed_origins", [
                "http://localhost:6274", "http://127.0.0.1:6274"
            ])
            if origin and origin not in allowed:
                raise web.HTTPForbidden(text="Origin not allowed")
            protocol = request.headers.get("MCP-Protocol-Version")
            if protocol and protocol not in ("2025-03-26", "2025-06-18"):
                raise web.HTTPBadRequest(text="Unsupported MCP protocol version")
            return await handler(request)

        self._app = web.Application(middlewares=[check_origin])

        # Register all tool routes derived from commands metadata
        self._register_all_commands()

        # Manual endpoints
        self._app.router.add_get("/sse", self._sse_handler)
        self._app.router.add_post("/message", self._rpc_handler)
        self._app.router.add_get("/status", self._status_handler)
        self._app.router.add_get("/help", self._help_handler)

        # HTTP (streamable) at base URL:
        self._app.router.add_post("/", self._rpc_handler)

        # SSE at base URL:
        self._app.router.add_get("/", self._no_stream_handler)

    @staticmethod
    def _log_line(msg: str, level: str = "info", **_ignored) -> None:
        """
        Write a timestamped log line straight to stdout's file descriptor.
        Bypasses Python stream redirection, so the line is visible even while a tool's output
        is being captured.
        Args:
            msg: The message.
            level: Level name shown in the line, e.g. "info" or "debug".
        """
        try:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            line = f"\r{ts} [{level.title():<8}] {msg}\n".encode()
            os.write(1, line)  # bypasses Python stream redirection
        except Exception as e:
            os.write(1, f"MCP Service log error: {e!r} | original message: {msg!r}\n".encode())

    async def _status_handler(self, _request):
        """
        HTTP handler for the status endpoint: basic runtime status, without secrets.
        Args:
            _request: The incoming request (unused).
        Returns:
            web.Response: JSON with host, port, readonly and tool_count.
        """
        return self._json_response({
            "host": self._mcp_config.host,
            "port": self._mcp_config.port,
            "readonly": bool(self._mcp_config.readonly),
            "tool_count": sum(
                1 for k, v in self._tools_data.items() if not v.get("hidden")
            )
        })

    async def _help_handler(self, _request: web.Request) -> web.Response:
        """
        HTTP handler for the help endpoint: the registered tools, as returned by tools/list.
        Args:
            _request: The incoming request (unused).
        Returns:
            web.Response: JSON with the tool list.
        """
        return self._json_response(self._rpc_tools_list())

    async def _no_stream_handler(self, request):
        # Stateless Streamable HTTP: responses are returned directly to POSTs.
        """
        HTTP handler for GET on the MCP endpoint, which this server does not support.
        Stateless Streamable HTTP returns each response directly to its POST, so there is no
        server-to-client stream.
        Args:
            request: The incoming request.
        Raises:
            web.HTTPMethodNotAllowed: Always (405, POST only).
        """
        raise web.HTTPMethodNotAllowed(request.method, ["POST"])

    async def _broadcast(self, obj: dict[str, Any]) -> None:
        """
        Broadcast a JSON-serializable object to all connected SSE clients.
        Args:
            obj (dict[str, Any]): The message payload to send. Must be JSON-serializable.
        Behavior:
            - Encodes the object as compact JSON (no extra whitespace).
            - Frames the data per SSE spec: prefix with "data: " and terminate
              with a double newline.
            - Attempts to send to all active SSE clients; one failing client will
              not disrupt others (exceptions are suppressed).
            - If the client stream supports `.flush()`, it is called after writing.
            - Sending is asynchronous: writes are awaited (or scheduled with
              `asyncio.create_task`) so the server does not block.

        Notes:
            - This is a best-effort broadcast; slow or disconnected clients may
              miss events if they cannot keep up.
            - Assumes `self._sse_clients` is a set of `aiohttp.web.StreamResponse`
              instances that have been prepared by `_sse_handler()`.
        """
        if not hasattr(self, "_sse_clients"):
            return
        frame = (b"data: " + json.dumps(obj, separators=(",", ":")).encode("utf-8") + b"\n\n")
        for ws in list(self._sse_clients):
            with contextlib.suppress(Exception):
                await ws.write(frame)
                if hasattr(ws, "flush"):
                    await ws.flush()

    async def _sse_handler(self, request: web.Request) -> web.StreamResponse:
        """
        Handle a Server-Sent Events (SSE) client connection.
        Behavior:
            - Prepares an SSE-compatible HTTP response with required headers.
            - Adds the connection to `self._sse_clients` for use by `_broadcast()`.
            - Sends an initial `: connected` comment to confirm the stream is active.
            - Periodically sends a `heartbeat` event every 15 seconds until shutdown.
            - Suppresses all exceptions from the write loop to avoid noisy disconnect errors.
            - Removes the connection from the active client set on exit.
        Args:
            request (web.Request): The aiohttp request object.
        Returns:
            web.StreamResponse: The prepared SSE response that will remain open
            until the client disconnects or the server shuts down.
        """
        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Access-Control-Allow-Origin": "*",
            },
        )
        await resp.prepare(request)

        if not hasattr(self, "_sse_clients"):
            self._sse_clients = set()
        self._sse_clients.add(resp)

        with contextlib.suppress(Exception):
            # Send initial connection comment
            await resp.write(b": connected\n\n")

            # Periodic heartbeat
            while not self._shutdown_event.is_set():
                await asyncio.sleep(15)
                await resp.write(b"event: heartbeat\ndata: {}\n\n")

        self._sse_clients.discard(resp)
        return resp

    async def _rpc_handler(self, request: web.Request) -> web.Response:
        """
        JSON-RPC endpoint for MCP over HTTP POST.
        Supports:
          - Single requests and batches per JSON-RPC 2.0.
          - Methods: initialize, ping, help, tools/list, tools/call, resources/list, resources/read.
          - Notifications (no "id"): answered with 202 and no body.
        Error behavior:
          - Always HTTP 200 with a JSON-RPC error envelope (-32700, -32600, -32602, -32603).
          - Never lets exceptions reach aiohttp (no HTTP 500).
        Args:
            request: The incoming POST with a JSON-RPC message or batch.
        Returns:
            web.Response: The JSON-RPC response(s), or 202 for notifications only.
        """

        # Helper builders (avoid repeating structure everywhere)
        def _jr_ok(_jid: Any, _result: Any) -> dict[str, Any]:
            return {"jsonrpc": "2.0", "id": _jid, "result": _result}

        def _jr_err(_jid: Any, _code: int, _message: str, _data: Any = None) -> dict[str, Any]:
            err: dict[str, Any] = {"code": _code, "message": _message}
            if _data is not None:
                err["data"] = _data
            return {"jsonrpc": "2.0", "id": _jid, "error": err}

        with contextlib.suppress(Exception):
            self._log_line(msg="POST /message", level="debug")

        # Method gate first (cheap)
        if request.method != "POST":
            error_body = _jr_err(_jid=None, _code=-32600, _message="method not allowed")
            return web.json_response(error_body)

        # Read body defensively
        raw = await request.read()
        if not raw:
            error_body = _jr_err(_jid=None, _code=-32600, _message="Empty request")
            return web.json_response(error_body)

        try:
            payload: Any = json.loads(raw.decode("utf-8"))
        except Exception as e:
            error_body = _jr_err(_jid=None, _code=-32700, _message="Parse error", _data=str(e))
            return web.json_response(error_body)

        with contextlib.suppress(Exception):
            self._log_line(msg="RPC handler got payload", level="debug")

            if self._log_request:
                pretty = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
                self._log_line(msg=f"Request:\n{pretty}", level="debug")

        async def _handle_one(msg: dict[str, Any]) -> Optional[dict[str, Any]]:
            """
            Handle a single JSON-RPC message. Returns a response dict,
            or None if input was a notification (no 'id').
            """
            if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
                return _jr_err(None, -32600, "invalid request")
            jid = msg.get("id", None)
            is_notification = "id" not in msg
            method = msg.get("method")
            params = msg.get("params", {})

            self._log_line(msg=f"Incoming method: {method}/params:'{params}', id: {jid}", level="debug")

            # Inline helpers return proper envelopes only when id is present
            if is_notification:
                def ok(_: Any) -> None:  # type: ignore[override]
                    return None

                def make_error(_: int, __: str) -> None:  # type: ignore[override]
                    return None
            else:
                def ok(_result: Any) -> dict[str, Any]:
                    return _jr_ok(jid, _result)

                def make_error(_code: int, _message: str) -> dict[str, Any]:
                    return _jr_err(jid, _code, _message)

            if not isinstance(msg, dict) or not isinstance(method, str):
                return make_error(-32600, "invalid request")
            if not isinstance(params, dict):
                return make_error(-32602, "invalid params")

            # -----------------------------------------------------------------
            #
            # Handle common MCP service methods
            #
            # -----------------------------------------------------------------

            try:
                if method == "initialize":
                    client_proto = params.get("protocolVersion", "2025-06-18")
                    info = {
                        "protocolVersion": client_proto if client_proto in ("2025-03-26", "2025-06-18") else "2025-06-18",
                        "serverInfo": {
                            "name": str(self._mcp_server_name),
                            "version": str(self._mcp_server_version),
                        },
                        "capabilities": {
                            "tools": {},
                            "resources": {}
                        },
                    }
                    self._log_line(msg=f"Handled 'initialize'", level="debug")
                    return ok(info)

                # -----------------------------------------------------------------

                elif method == "tools/list":
                    return ok(self._rpc_tools_list())

                # -----------------------------------------------------------------

                elif method == "help":
                    result = await self._help_handler_rpc(params)
                    return ok(result)

                # -----------------------------------------------------------------

                elif method == "resources/list":

                    resources = []
                    for tool_name, tool_info in self._project_data.get("tools", {}).items():
                        resource_path = tool_info.get("resource")
                        if not resource_path:
                            continue
                        abs_path = os.path.join(self._project_base_path, resource_path)
                        uri = f"file://{os.path.abspath(abs_path)}"
                        resources.append({
                            "name": tool_name,
                            "uri": uri,
                            "mimeType": "text/markdown"
                        })
                    return ok({"resources": resources})

                # -----------------------------------------------------------------

                elif method == "resources/read":

                    uri = params.get("uri")
                    if not uri or not uri.startswith("file://"):
                        return make_error(-32602, f"Invalid or missing URI: {uri}")

                    parsed = urlparse(uri)
                    path = unquote(parsed.path)
                    query_params = dict(parse_qsl(parsed.query))
                    allowed_paths = {
                        (Path(self._project_base_path) / t.resource).resolve()
                        for t in self._tools_registry.values() if t.resource
                    }
                    if parsed.netloc or Path(path).resolve() not in allowed_paths:
                        return make_error(-32602, "Resource is not registered")

                    try:
                        with open(path, "r", encoding="utf-8") as f:
                            text = f.read()
                    except Exception as read_error:
                        return make_error(-32000, f"Failed to read resource {uri}: {read_error}")

                    # --- Dynamic substitution: used in templates ---
                    if query_params:
                        # Append a small note at the bottom of the Markdown
                        args_str = ", ".join(f"{k}={v}" for k, v in query_params.items())
                        text += f"\n\n---\n*Template arguments applied:* {args_str}\n"

                    return ok({
                        "contents": [
                            {"uri": uri, "text": text}
                        ]
                    })
                # -----------------------------------------------------------------

                elif method in ("templates/list", "resources/templates/list"):
                    resource_templates = []
                    base = os.path.abspath(os.path.join(self._project_base_path, "resources"))

                    for name, tmpl in self._project_data.get("templates", {}).items():
                        args = []
                        arg_name = ""
                        for arg_name, default in tmpl.get("args", {}).items():
                            args.append({
                                "name": arg_name,
                                "description": f"Argument for {tmpl.get('command')}",
                                "default": default
                            })

                        # Build a simple uriTemplate using the resource path
                        resource_path = tmpl.get("command").replace("tool_", "tool_") + ".md"
                        uri_template = f"file://{base}/{resource_path}?{arg_name}={{{arg_name}}}"

                        resource_templates.append({
                            "name": name,
                            "description": tmpl.get("description", ""),
                            "uriTemplate": uri_template,
                            "arguments": args
                        })

                    return ok({"resourceTemplates": resource_templates})


                # -----------------------------------------------------------------

                elif method == "tools/call":
                    tool_name = params.get("name", "<?>")
                    with contextlib.suppress(Exception):
                        self._log_line(msg=f"Calling tool: {tool_name} with: {params}", level="debug")

                    # Single flight: try to acquire immediately; reject if busy
                    try:
                        await asyncio.wait_for(self._single_flight.acquire(), timeout=0.001)
                    except asyncio.TimeoutError:
                        return make_error(
                            BUSY_CODE,
                            "Busy: another tool is currently running in this workspace")

                    self._current = (tool_name, asyncio.get_running_loop().time())
                    try:
                        result = await self._rpc_tools_call(params)
                        wrapped = {
                            "isError": result.get("status", 0) != 0,
                            "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                        }

                        return ok(wrapped)

                    finally:
                        self._current = None
                        self._single_flight.release()

                # -----------------------------------------------------------------

                elif method == "ping":
                    return ok({})

                return make_error(-32601, f"unknown method: {method}")

            except ValidationError as ex:
                return make_error(-32602, ex.message)
            except KeyError as ke:
                return make_error(-32601, str(ke))
            except Exception as ex:
                with contextlib.suppress(Exception):
                    await self._broadcast({"jsonrpc": "2.0", "method": "tools/error", "params": {"error": str(ex)}})
                    self._log_line(f"_handle_one crash: {ex!r}", level="error")
                return make_error(-32603, "Internal error")

        # Single vs batch
        try:
            if isinstance(payload, list):
                # Empty batch is invalid
                if len(payload) == 0:
                    error_body = _jr_err(_jid=None, _code=-32600, _message="invalid request (empty batch)")
                    return web.json_response(error_body)

                if len(payload) > MAX_BATCH_MCP_COMMANDS:
                    error_body = _jr_err(_jid=None, _code=-32600, _message="batch too large")
                    return web.json_response(error_body)

                replies: list[dict[str, Any]] = []
                for item in payload:
                    resp = await _handle_one(item if isinstance(item, dict) else {})
                    if resp is not None:
                        replies.append(resp)

                if replies:
                    return web.json_response(replies)
                # All were notifications.
                return web.Response(status=202)

            # Single message
            if not isinstance(payload, dict):
                error_body = _jr_err(_jid=None, _code=-32600, _message="Invalid request")
                return web.json_response(error_body)

            reply = await _handle_one(payload)
            if reply is None:
                return web.Response(status=202)
            return web.json_response(reply)

        except Exception as e:
            with contextlib.suppress(Exception):
                self._log_line(f"/message handler crash (outer): {e!r}", level="error")
            error_body = _jr_err(_jid=None, _code=-32603, _message="Internal error")
            return web.json_response(error_body)

    @staticmethod
    async def _help_handler_rpc(_params: dict[str, Any]) -> dict[str, Any]:
        """
        JSON-RPC help method: not implemented yet.
        Args:
            _params: The request params (unused).
        Returns:
            dict[str, Any]: An error stating that help metadata is not available.
        """
        # TODO: Implement
        return {"error": "Help metadata not available"}

    def _add_tool(self, tool: MCPServiceToolType):
        """
        Register a new MCP tool in the server's tool registry.
        Args:
            tool (MCPServiceToolType): The tool instance to register. The `name`
                attribute is used as the registry key.
        Notes:
            - If a tool with the same name already exists, it will be overwritten.
            - Registered tools are discoverable via `tools/list` and callable via
              `tools/call` in the MCP JSON-RPC API.
        """
        self._tools_registry[tool.name] = tool

    @staticmethod
    def _json_response(data: Any, status: int = 200) -> web.Response:
        """
        Create a consistent JSON response with indentation.
        Args:
            data (dict): The data to serialize and return as JSON.
        Returns:
            web.Response: A JSON response with pretty indentation.
        """
        return web.json_response(
            data,
            status=status,
            dumps=lambda x: json.dumps(x, indent=2, ensure_ascii=False) + "\n",
        )

    async def _run_one_cmdline_async(self,
                                     line: Optional[str] = None,
                                     argv: Optional[list[str]] = None,
                                     cwd: Optional[str] = None,
                                     env: Optional[dict[str, str]] = None) -> dict[str, Any]:
        """
        Run a single command asynchronously inside the build shell.
        Args:
            line (str, optional): Command line string (legacy path).
            argv (list[str], optional): Command as argument vector (preferred).
            cwd (str, optional): Working directory to execute in.
            env (dict[str, str], optional): Environment variables to apply.

        """
        logs: list[str] = []  # Executed process output lines
        current_work_dir = str(cwd) if cwd is not None else str(Path.cwd())

        if argv is None:
            if not line:
                raise ValueError("Must provide either argv or line")
            argv = shlex.split(line)

        self._log_line(f"Executing: {argv}, cwd: {current_work_dir}", level="debug")
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=current_work_dir,
                env=env or os.environ.copy(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except Exception as execute_error:
            raise RuntimeError(f"Failed to launch {argv!r}: {execute_error}")

        # Stream logs
        assert proc.stdout is not None
        async for raw_line in proc.stdout:
            decoded: str = raw_line.decode(errors="replace")
            text = decoded.rstrip()
            logs.append(text)

            with contextlib.suppress(Exception):
                await self._broadcast({"event": "log", "data": text})

        status = await proc.wait()

        result: dict[str, Any] = {
            "status": status,
            "logs": logs,
            "summary": f"Executed: {' '.join(argv)} (exit {status})",
        }

        with contextlib.suppress(Exception):
            await self._broadcast({"event": "done", **result})

        return result

    @staticmethod
    def _discover_tools(tools_dir: str, tools_env: dict[str, str]) -> dict[str, dict[str, Any]]:
        """
        Load tool entries from <tools_dir>/<tool>/tool.json manifests.
        Manifest paths are relative to tools_dir, which is also each tool's working directory.
        Args:
            tools_dir (str): Tools folder, relative to the project configuration directory.
            tools_env (dict[str, str]): Environment variables added to every discovered tool.
        Returns:
            dict[str, dict[str, Any]]: Tool entries keyed by tool (folder) name.
        """
        tools: dict[str, dict[str, Any]] = {}
        for manifest in sorted(Path(tools_dir).glob("*/tool.json")):
            entry = json.loads(manifest.read_text(encoding="utf-8"))
            entry["working_dir"] = tools_dir
            entry["env"] = {**tools_env, **entry.get("env", {})}
            if entry.get("resource"):
                entry["resource"] = str(Path(tools_dir) / entry["resource"])
            tools[manifest.parent.name] = entry
        if not tools:
            raise RuntimeError(f"No tool manifests found under '{tools_dir}'")
        return tools

    def _register_all_commands(self) -> None:
        """
        Register all loaded tools both as MCP tools (for SSE JSON-RPC)
        and, optionally, as REST POST endpoints under /tool/<name>.
        """

        if not isinstance(self._tools_data, dict) or not self._tools_data:
            raise TypeError("tools must be a non-empty dict")

        for key, entry in self._tools_data.items():
            tool_name = f"{self._tool_prefix}{key}"
            if not re.fullmatch(r"[a-z0-9_-]+", tool_name):
                raise RuntimeError(f"Invalid MCP tool name: {tool_name}")

            description = entry.get("description") or f"Run '{key}' tool."

            command = entry.get("command")
            if not command:
                raise RuntimeError(f"Missing 'command' in MCP tool entry: {tool_name}")

            working_dir = entry.get("working_dir")
            static_args = entry.get("args", [])
            params = entry.get("params", [])
            env = entry.get("env", {})
            resource = entry.get("resource")
            timeout = entry.get("timeout")  # Seconds; advertised to clients in tools/list

            # MCP-compatible JSON schema from declared params
            input_schema = {
                "type": "object",
                "properties": {
                    p["name"]: {"type": p.get("type", "string"), "description": p.get("description", "")}
                    for p in params
                },
                "required": [p["name"] for p in params if p.get("required", True)],
                "additionalProperties": False,
            }

            # Register as MCP tool
            self._add_tool(MCPServiceToolType(
                name=tool_name,
                description=description,
                input_schema=input_schema,
                command=command,
                working_dir=working_dir,
                args=static_args,
                params=params,
                env=env,
                resource=resource,
                timeout=timeout,
            ))

            # Legacy REST fallback
            def make_handler(_command=command, _args=static_args, _cwd=working_dir, _env=env):
                async def handler(request):
                    payload = {}
                    with contextlib.suppress(Exception):
                        payload = await request.json()

                    # Merge static args + runtime args
                    runtime_args = []
                    with contextlib.suppress(Exception):
                        raw_args = payload.get("args", [])
                        if isinstance(raw_args, str):
                            runtime_args = [raw_args]
                        elif isinstance(raw_args, list):
                            runtime_args = [str(a) for a in raw_args]

                    final_argv = [_command] + _args + runtime_args
                    line = " ".join(final_argv)
                    line = os.path.expandvars(line)

                    try:
                        result = await self._run_one_cmdline_async(
                            line,
                            cwd=_cwd,
                            env={**os.environ, **_env}
                        )
                        return self._json_response({"results": [result]})
                    except Exception as e:
                        return self._json_response({"error": str(e)}, status=500)

                return handler

            self._app.router.add_post(f"/tool/{tool_name}", make_handler())

    async def _rpc_tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Invoke a registered MCP tool as a subprocess by name.
        Args:
            params (dict[str, Any]): JSON-RPC parameters with:
                - "name" (str): The registered tool's name.
                - "arguments" (dict): Arguments to pass to the tool.
                  Must conform to the tool's `input_schema`.
        Returns:
            dict[str, Any]: The tool's result payload, as returned by
            `_run_one_cmdline_async` (JSON-serializable).
        """
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(name, str):
            raise KeyError("tools/call needs a tool name")

        tool = self._tools_registry.get(name)
        if not tool:
            raise KeyError(f"unknown tool: {name}")

        validate(instance=arguments, schema=tool.input_schema)

        # Start with the base command
        argv: list[str] = [tool.command]

        # Add static args
        if tool.args:
            argv.extend(tool.args)

        # Add dynamic params from JSON -> CLI
        for p in tool.params:
            pname = p["name"]
            if pname not in arguments:
                continue

            val = arguments[pname]
            style = p.get("style", "flag")  # default to "flag"

            if style == "positional":
                argv.append(str(val))
            elif style == "flag":
                argv.extend([f"--{pname}", str(val)])
            else:
                raise ValueError(f"Unknown param style '{style}' for {pname}")

        # Merge environment (base + tool-specific overrides)
        env = {**os.environ, **tool.env}

        return await self._run_one_cmdline_async(
            argv=argv,
            cwd=str(Path(self._project_base_path) / (tool.working_dir or ".")),
            env=env, )

    def _rpc_tools_list(self) -> dict[str, Any]:
        """
        List all registered MCP tools.
        Returns:
            dict[str, Any]: A dictionary with key "tools" containing a list of
            tool descriptors, where each descriptor includes:
                - "name" (str): Tool name.
                - "description" (str): Tool description.
                - "inputSchema" (dict): JSON Schema for the tool's input.
                - "_meta" (dict, optional): {"timeout": seconds}, when the tool's manifest sets one.
        """
        tools = [{
            "name": t.name,
            "description": t.description,
            "inputSchema": t.input_schema,
            **({"_meta": {"timeout": t.timeout}} if t.timeout else {}),
        } for t in self._tools_registry.values()]

        return {"tools": tools}

    async def _run_sse(self):
        """
        Internal async loop that configures and starts the SSE server.

        - Uses `aiohttp.web.AppRunner` to attach `self._app` to an HTTP server.
        - Binds to the configured host and port.
        - Stays alive indefinitely, sleeping in 1-hour intervals until stopped.
        - Ensures cleanup of resources on shutdown.
        """
        runner = web.AppRunner(self._app)
        await runner.setup()
        site = web.TCPSite(runner, self._mcp_config.host, self._mcp_config.port)
        await site.start()

        try:
            await self._shutdown_event.wait()  # Block until told to exit
        except asyncio.CancelledError:
            # Handles task.cancel() if loop is being cancelled
            pass
        finally:
            await runner.cleanup()
            # Give aiohttp tasks a chance to settle
            await asyncio.sleep(1)

    @staticmethod
    def _remove_vscode_config(base_path: Optional[Union[Path, str]],
                              host: str,
                              port: int,
                              server_name: str) -> bool:
        """
        Quietly remove a server entry from an existing VS Code MCP config.
        Args:
            base_path (Union[Path, str], optional):
                Workspace base directory containing the .vscode folder.
                If None, use the current directory.
            host (str): Host IP address to match in the config.
            port (int): Host port to match in the config.
            server_name (str): Server key to remove.

        Returns:
            bool: True if the config was updated or nothing needed removal,
                  False if an error occurred.
        """
        if base_path is None:
            base_path = os.getcwd()

        vscode_dir = Path(base_path).expanduser().resolve() / ".vscode"
        config_path = vscode_dir / "mcp.json"

        if not config_path.exists():
            return True  # nothing to remove

        with contextlib.suppress(json.JSONDecodeError, UnicodeDecodeError, OSError):
            with config_path.open("r", encoding="utf-8") as f:
                data = json.load(f)

            url_to_remove = f"http://{host}:{port}"
            servers = data.get("servers", {})

            if server_name in servers:
                if servers[server_name].get("url") == url_to_remove:
                    del servers[server_name]

            # Clean up if servers now empty
            if not servers:
                data.pop("servers", None)

            with contextlib.suppress(Exception):
                config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
                return True

        return False

    @staticmethod
    def _generate_vscode_config(base_path: Optional[Union[Path, str]],
                                host: str,
                                port: int,
                                server_name: str,
                                overwrite_existing: bool = True,
                                create_parents: bool = False,
                                ensure_inputs: bool = True) -> bool:
        """
        Generate/merge a VS Code MCP config without clobbering existing servers.
        Args:
            base_path: Base directory where '.vscode/mcp.json' will be written.
                         If None, uses the current directory.
            host: IP for the SSE server.
            port: Port for the SSE server.
            server_name: Key under "servers" to write/update.
            overwrite_existing: If True, overwrite the existing <server_name> entry
                                if present. If False, only add it if missing.
            create_parents: If True, create <dir>/.vscode if missing.
            ensure_inputs: If True, add a minimal "inputs" section when absent.

        Returns:
            bool: True if the config file was written/updated, False otherwise.
        """
        if base_path is None:
            base_path = os.getcwd()

        base_dir = Path(base_path).expanduser().resolve()

        vscode_dir = base_dir / ".vscode"
        config_path = vscode_dir / "mcp.json"

        if create_parents:
            vscode_dir.mkdir(parents=True, exist_ok=True)

        data: Optional[dict] = None

        if config_path.exists():
            try:
                data = json.loads(config_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {}
            except (JSONDecodeError, UnicodeDecodeError):
                with contextlib.suppress(Exception):
                    config_path.rename(config_path.with_suffix(".json.bak"))
                data = {}

        if data is None:
            data = {}

        servers = data.setdefault("servers", {})
        new_entry = {"type": "http", "url": f"http://{host}:{port}"}

        if server_name not in servers or overwrite_existing or servers[server_name] != new_entry:
            servers[server_name] = new_entry
        else:
            return False  # no change needed

        if ensure_inputs and "inputs" not in data:
            data["inputs"] = [
                {"id": "args", "type": "promptString", "description": "Extra arguments"}]

        with contextlib.suppress(Exception):
            config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            return True

        return False

    # noinspection SpellCheckingInspection
    @staticmethod
    def _greetings(host: str, port: int, server_name: str, show_examples: bool = False,
                   host_bind_address: Optional[str] = None):
        """
        Display greetings and optionally example 'curl' commands that can be copied and run directly
        in a console window to interact with the SSE server.
        Args:
            host (str): Host/IP address of the MCP server.
            port (int): TCP port where the MCP service is listening.
            server_name (str): Name of the MCP service
            show_examples: If True, show example commands.
            host_bind_address (optional str): Bind address to bind to the MCP server.
        """

        base = f"http://{host}:{port}"
        title = f"{Fore.CYAN}MCPAgent HTTP Service Info:{Style.RESET_ALL}"

        # Clear screen and print header
        print("\033[2J\033[3J\033[H", end="")
        print(f"\n{title}\n{Fore.CYAN}{'-' * len('MCPAgent HTTP Service Info:')}{Style.RESET_ALL}")
        info = [
            ("Base", base),
            ("Diagnostic event feed", f"{base}/sse"),
            ("JSON-RPC message bus", f"{base}/message"),
            ("Name", server_name),
        ]
        if isinstance(host_bind_address, str):
            info.append(("Bind address", host_bind_address))
        for label, value in info:
            print(f"{Fore.YELLOW}{f'- {label}:':<25}{Style.RESET_ALL}{Fore.GREEN}{value}{Style.RESET_ALL}")

        if show_examples:
            curl = f"{Fore.CYAN}curl{Style.RESET_ALL}"
            host_colored = f"{Fore.GREEN}{host}{Style.RESET_ALL}"
            port_colored = f"{Fore.MAGENTA}{port}{Style.RESET_ALL}"

            print(f"\n{Fore.CYAN}Example commands you can run in another shell:{Style.RESET_ALL}")

            print(f"\n{Fore.YELLOW}1. Listen for SSE broadcasts:{Style.RESET_ALL}")
            print(f"   {curl} -s -N --noproxy {host_colored} http://{host_colored}:{port_colored}/sse{Style.RESET_ALL}")

            print(f"\n{Fore.YELLOW}2. List available tools:{Style.RESET_ALL}")
            print(f"   {curl} -s --noproxy {host_colored} "
                  "-H \"Content-Type: application/json\" "
                  "-d \"{\\\"jsonrpc\\\":\\\"2.0\\\",\\\"id\\\":1,\\\"method\\\":\\\"tools/list\\\",\\\"params\\\":{}}\" "
                  f"http://{host_colored}:{port_colored}/message | jq{Style.RESET_ALL}")

            print(f"\n{Fore.YELLOW}3. Execute tool 'greet' with argument 'Alice':{Style.RESET_ALL}")
            print(f"   {curl} -s --noproxy {host_colored} "
                  "-H \"Content-Type: application/json\" "
                  "-d \"{\\\"jsonrpc\\\":\\\"2.0\\\",\\\"id\\\":2,\\\"method\\\":\\\"tools/call\\\","
                  "\\\"params\\\":{\\\"name\\\":\\\"greet\\\","
                  "\\\"arguments\\\":{\\\"name\\\":\\\"Alice\\\"}}}\" "
                  f"http://{host_colored}:{port_colored}/message | jq{Style.RESET_ALL}")

        print(
            f"\n{Fore.MAGENTA}Running... Press {Style.BRIGHT}{Fore.RED}Ctrl+C{Style.RESET_ALL}{Fore.MAGENTA} to stop.{Style.RESET_ALL}\n")

    def start(self) -> int:
        """
        Start the MCP server in SSE (Server-Sent Events) mode.
        Attempts to determine the system's primary external IPv4 address
        (non-loopback) by connecting to a known public IP (Google DNS at 8.8.8.8).
        This does not require actual network reachability, no data is sent.
        Runs the asynchronous SSE server loop until interrupted.

        Returns:
            int: 0 if the server started successfully, 1 if an exception occurred.
        """

        def _handle_term_signal():
            self._log_line(msg=f"Interrupted by user, shutting down", level="warning")

            self._shutting_down = True
            self._shutdown_event.set()


            # Remove VSCode config if needed
            if self._patch_vscode_config:
                self._remove_vscode_config(
                    base_path=None,
                    host=self._mcp_config.advertise_ip or "127.0.0.1",
                    port=self._mcp_config.port,
                    server_name=self._mcp_server_name, )
            # Terminate
            if self._brutal_termination:
                os.kill(os.getpid(), signal.SIGKILL)

        try:

            self._mcp_config.host = self._mcp_server_bind_address or "127.0.0.1"
            self._mcp_config.advertise_ip = (
                "127.0.0.1" if self._mcp_config.host == "0.0.0.0" else self._mcp_config.host
            )

            # Create VSCode 'mcp.json' file in the solution workspace
            if self._patch_vscode_config:
                self._generate_vscode_config(base_path=None, host=self._mcp_config.advertise_ip,
                                             port=self._mcp_config.port, server_name=self._mcp_server_name,
                                             overwrite_existing=True, create_parents=True)

            # Show welcome message and usage examples
            self._greetings(host=self._mcp_config.advertise_ip, port=self._mcp_config.port,
                            server_name=self._mcp_server_name, show_examples=self._show_usage_examples,
                            host_bind_address=self._mcp_server_bind_address)

            # Prepare asyncio loop
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

            # Attach signal handlers so Ctrl+C triggers shutdown cleanly
            for sig in (signal.SIGINT, signal.SIGTERM):
                # noinspection PyTypeChecker
                loop.add_signal_handler(sig, _handle_term_signal)

            # Run the SSE server
            if loop.is_running():
                asyncio.create_task(self._run_sse())
            else:
                loop.run_until_complete(self._run_sse())

            return 0

        except KeyboardInterrupt:
            return 0
        except Exception as e:
            if self._shutting_down:
                self._log_line(msg=f"MCP server terminated", level="debug")
                print()
                return 0
            self._log_line(msg=f"MCP Error: {e}", level="error")
            return 1
