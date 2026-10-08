"""
Module: service.py

Description:
    Implements `MCPService`, the MCP server: it exposes command-line tools to an MCP client as
    JSON-RPC 2.0 over stdin and stdout. The agent (python mcp/agent.py) starts it as a child process
    (`python -m mcpagent.service`), uses it for the session, and closes its stdin when done; the
    server then exits. It does not run on its own: started from a terminal, `main` refuses.

    The service provides:
      - Tool discovery from a tools folder (`tools_dir`, one <tool>/tool.json manifest per
        tool) and/or inline tool definitions in the server config.
      - `initialize`, `tools/list`, `tools/call`, `resources/list` and `resources/read`, with
        tool arguments validated against each tool's JSON schema.
      - Command execution as subprocesses, with each tool's working directory and environment.

    Key design points:
      - One JSON-RPC message (or batch) per line on stdin, one response per line on stdout; a
        notification gets no response. stdout carries only protocol messages.
      - Single-flight: only one tool runs at a time; a concurrent `tools/call` is rejected
        with a JSON-RPC "Busy" error.
      - Its log is a few short lines on stderr ("started, 9 tools from tools/", "ran time: bash
        time/time.sh --timezone=UTC (exit 0, 0.0s)", errors), which the client shows as "server ..."
        among its own gray debug lines when asked to (server_output in the client config).
"""

import argparse
import asyncio
import contextlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Union
from urllib.parse import urlparse, unquote

# Third-party
from jsonschema import validate, ValidationError

# Local imports
from mcpagent import DEFAULT_CONFIG, REPO_ROOT, __version__
from mcpagent.config import MCPAgentConfig
from mcpagent.types import MCPServiceToolType, RPCError


MAX_BATCH_MCP_COMMANDS = 64
BUSY_CODE = -32004


class MCPService:
    """
    The MCP server: serves the configured tools to its client as JSON-RPC 2.0 over stdin/stdout.
    Tools come from the config's tools folder (`tools_dir`) and/or inline `tools`. Only one
    tool runs at a time; see the module description for the design points.
    """

    def __init__(self, project_data: Optional[dict] = None, log: Optional[Callable[[str], None]] = None) -> None:
        """
        Load the tools and server settings from the parsed server config.
        Args:
            project_data: The server settings (mcpagent.json's "server" section): server name and
                version, tools_dir / tools_env and inline tools.
            log: Called with each log line; None writes them to stderr.
        Raises:
            TypeError: If project_data is not a dict.
        """

        self._single_flight = asyncio.Semaphore(1)  # Single-flight across the whole workspace
        # JSON-RPC methods and their handlers: each takes the params and returns the result
        self._rpc_methods: dict[str, Callable[[dict[str, Any]], Awaitable[Any]]] = {
            "initialize": self._on_initialize,
            "ping": self._on_ping,
            "help": self._on_help,
            "tools/list": self._on_tools_list,
            "tools/call": self._on_tools_call,
            "resources/list": self._on_resources_list,
            "resources/read": self._on_resources_read,
        }

        self._log = log or self._stderr_line
        self._tools_registry: dict[str, MCPServiceToolType] = {}
        self._project_base_path: str = os.getcwd()

        if not isinstance(project_data, dict):
            raise TypeError("project_data must be a dict")
        self._project_data: dict[str, Any] = project_data

        self._mcp_server_name: str = self._project_data.get("project_name", "MCP service")
        self._mcp_server_version: str = self._project_data.get("version", "1.0.0")
        self._tools_data: dict[str, Any] = self._project_data.get("tools", {})

        # Optional shared tools folder: one sub-folder per tool, each with a tool.json manifest
        self._tools_dir: Optional[str] = self._project_data.get("tools_dir")
        if self._tools_dir:
            discovered = self._discover_tools(self._tools_dir, self._project_data.get("tools_env", {}))
            self._tools_data = {**discovered, **self._tools_data}
            self._project_data["tools"] = self._tools_data

        self._register_all_commands()

    @staticmethod
    def _stderr_line(text: str) -> None:
        """
        Write one log line to stderr, where the client reads it (stdout carries only the protocol).
        Args:
            text: The line.
        """
        with contextlib.suppress(Exception):
            sys.stderr.write(text.replace("\n", " ") + "\n")
            sys.stderr.flush()

    async def handle_line(self, line: Union[str, bytes]) -> Optional[str]:
        """
        Answer one line from the client: a JSON-RPC message, or a batch of them.
        Errors are answered as JSON-RPC error envelopes (-32700, -32600, -32603); nothing is raised.
        Args:
            line: The line, without framing.
        Returns:
            Optional[str]: The response line (without its newline), or None when there is nothing to
                answer: an empty line, or notifications only.
        """
        raw = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else line
        if not raw.strip():
            return None
        try:
            payload: Any = json.loads(raw)
        except ValueError as error:
            return json.dumps(self._rpc_error(None, -32700, "Parse error", str(error)))
        try:
            if isinstance(payload, list):
                if not payload:
                    return json.dumps(self._rpc_error(None, -32600, "invalid request (empty batch)"))
                if len(payload) > MAX_BATCH_MCP_COMMANDS:
                    return json.dumps(self._rpc_error(None, -32600, "batch too large"))
                replies = [reply for item in payload
                           if (reply := await self._handle_message(item if isinstance(item, dict) else {})) is not None]
                return json.dumps(replies) if replies else None
            reply = await self._handle_message(payload if isinstance(payload, dict) else {})
            return None if reply is None else json.dumps(reply)
        except Exception as error:
            self._log(f"error: {error!r}")
            return json.dumps(self._rpc_error(None, -32603, "Internal error"))

    async def run_stdio(self, reader: Optional[asyncio.StreamReader] = None,
                        writer: Optional[Callable[[str], None]] = None) -> int:
        """
        Serve the client: read lines from stdin and answer on stdout, until stdin closes.
        Args:
            reader: Where the lines come from; None reads stdin.
            writer: Called with each response line; None writes it to stdout.
        Returns:
            int: 0, once the client has closed stdin.
        """
        if reader is None:
            loop = asyncio.get_running_loop()
            reader = asyncio.StreamReader(limit=2 ** 24)  # A tool call's arguments may be a whole file
            await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
        if writer is None:
            def writer(text: str) -> None:
                sys.stdout.write(text + "\n")
                sys.stdout.flush()

        self._log(f"started, {len(self._tools_registry)} tools" + (f" from {self._tools_dir}/" if self._tools_dir else ""))
        while line := await reader.readline():
            response = await self.handle_line(line)
            if response is not None:
                writer(response)
        return 0

    async def _handle_message(self, msg: dict[str, Any]) -> Optional[dict[str, Any]]:
        """
        Handle one JSON-RPC message: check it, run its method from the dispatch table, and wrap
        the result or the error in a response envelope.
        Args:
            msg: The message.
        Returns:
            Optional[dict[str, Any]]: The response, or None for a notification (no "id"), whose
                method still runs.
        """
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            return self._rpc_error(None, -32600, "invalid request")
        jid = msg.get("id", None)
        is_notification = "id" not in msg
        method = msg["method"]
        params = msg.get("params", {})

        try:
            if not isinstance(params, dict):
                raise RPCError(-32602, "invalid params")
            handler = self._rpc_methods.get(method)
            if handler is None:
                raise RPCError(-32601, f"unknown method: {method}")
            result = await handler(params)
            return None if is_notification else self._rpc_result(jid, result)
        except RPCError as error:
            return None if is_notification else self._rpc_error(jid, error.code, error.message)
        except ValidationError as ex:
            return None if is_notification else self._rpc_error(jid, -32602, ex.message)
        except KeyError as ke:
            return None if is_notification else self._rpc_error(jid, -32601, str(ke))
        except Exception as ex:
            self._log(f"error: {method}: {ex!r}")
            return None if is_notification else self._rpc_error(jid, -32603, "Internal error")

    @staticmethod
    def _rpc_result(jid: Any, result: Any) -> dict[str, Any]:
        """
        A JSON-RPC success envelope.
        Args:
            jid: The request id.
            result: The method's result.
        Returns:
            dict[str, Any]: The response.
        """
        return {"jsonrpc": "2.0", "id": jid, "result": result}

    @staticmethod
    def _rpc_error(jid: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
        """
        A JSON-RPC error envelope.
        Args:
            jid: The request id; None when it is not known.
            code: The JSON-RPC error code.
            message: The error message.
            data: Optional detail.
        Returns:
            dict[str, Any]: The response.
        """
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return {"jsonrpc": "2.0", "id": jid, "error": error}

    async def _on_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        initialize: the protocol version (the client's, when supported), server info and capabilities.
        Args:
            params: The request params ("protocolVersion").
        Returns:
            dict[str, Any]: The result.
        """
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
        return info

    @staticmethod
    async def _on_ping(_params: dict[str, Any]) -> dict[str, Any]:
        """
        ping: an empty result.
        Args:
            _params: The request params (unused).
        Returns:
            dict[str, Any]: {}.
        """
        return {}

    async def _on_help(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        help: see `_help_handler_rpc`.
        Args:
            params: The request params.
        Returns:
            dict[str, Any]: The result.
        """
        return await self._help_handler_rpc(params)

    async def _on_tools_list(self, _params: dict[str, Any]) -> dict[str, Any]:
        """
        tools/list: every registered tool, see `_rpc_tools_list`.
        Args:
            _params: The request params (unused).
        Returns:
            dict[str, Any]: The result.
        """
        return self._rpc_tools_list()

    async def _on_tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        tools/call: run one tool, one at a time in the whole workspace.
        Args:
            params: The request params ("name", "arguments").
        Returns:
            dict[str, Any]: The tool's result as MCP content, with isError set when it failed.
        Raises:
            RPCError: BUSY_CODE when another tool is running.
        """
        # Single flight: try to acquire immediately; reject if busy
        try:
            await asyncio.wait_for(self._single_flight.acquire(), timeout=0.001)
        except asyncio.TimeoutError:
            self._log(f"refused {params.get('name', '?')}: another tool is running")
            raise RPCError(BUSY_CODE, "Busy: another tool is currently running in this workspace") from None

        try:
            result = await self._rpc_tools_call(params)
            return {
                "isError": result.get("status", 0) != 0,
                "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
            }
        finally:
            self._single_flight.release()

    async def _on_resources_list(self, _params: dict[str, Any]) -> dict[str, Any]:
        """
        resources/list: each tool's documentation (its README) as a Markdown resource.
        Args:
            _params: The request params (unused).
        Returns:
            dict[str, Any]: The result: "resources".
        """
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
        return {"resources": resources}

    async def _on_resources_read(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        resources/read: one registered tool's documentation.
        Args:
            params: The request params ("uri", a file:// URI from resources/list).
        Returns:
            dict[str, Any]: The result: "contents".
        Raises:
            RPCError: -32602 for a missing, invalid or unregistered URI; -32000 if it cannot be read.
        """
        uri = params.get("uri")
        if not isinstance(uri, str) or not uri.startswith("file://"):
            raise RPCError(-32602, f"Invalid or missing URI: {uri}")

        parsed = urlparse(uri)
        path = unquote(parsed.path)
        allowed_paths: set[Path] = set()
        for tool in self._tools_registry.values():
            resource = tool.resource
            if resource:
                allowed_paths.add((Path(self._project_base_path) / resource).resolve())
        if parsed.netloc or Path(path).resolve() not in allowed_paths:
            raise RPCError(-32602, "Resource is not registered")

        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except Exception as read_error:
            raise RPCError(-32000, f"Failed to read resource {uri}: {read_error}") from None

        return {
            "contents": [
                {"uri": uri, "text": text}
            ]
        }

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

    async def _run_one_cmdline_async(self, argv: list[str], cwd: Optional[str] = None,
                                     env: Optional[dict[str, str]] = None) -> dict[str, Any]:
        """
        Run one tool command and collect its output.
        Args:
            argv: The command as an argument vector.
            cwd: Working directory to execute in.
            env: Environment variables to apply.
        Returns:
            dict[str, Any]: "status" (the exit code), "logs" (the output lines, stdout and stderr
                together) and "summary".
        Raises:
            RuntimeError: If the command cannot be started.
        """
        logs: list[str] = []  # Executed process output lines
        current_work_dir = str(cwd) if cwd is not None else str(Path.cwd())

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=current_work_dir,
                env=env or os.environ.copy(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except Exception as execute_error:
            raise RuntimeError(f"Failed to launch {argv!r}: {execute_error}") from execute_error

        # Stream logs
        assert proc.stdout is not None
        async for raw_line in proc.stdout:
            decoded: str = raw_line.decode(errors="replace")
            text = decoded.rstrip()
            logs.append(text)

        status = await proc.wait()

        result: dict[str, Any] = {
            "status": status,
            "logs": logs,
            "summary": f"Executed: {' '.join(argv)} (exit {status})",
        }

        return result

    @staticmethod
    def _discover_tools(tools_dir: str, tools_env: dict[str, str]) -> dict[str, dict[str, Any]]:
        """
        Load tool entries from <tools_dir>/<tool>/tool.json manifests.
        Manifest paths are relative to tools_dir, which is also each tool's working directory.
        Args:
            tools_dir (str): Tools folder, relative to the repository root (the working directory).
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
        Register all loaded tools as MCP tools, served by tools/list and tools/call.
        """

        if not isinstance(self._tools_data, dict) or not self._tools_data:
            raise TypeError("tools must be a non-empty dict")

        for key, entry in self._tools_data.items():
            tool_name = key
            if not re.fullmatch(r"[a-z0-9_-]+", tool_name):
                raise RuntimeError(f"Invalid MCP tool name: {tool_name}")

            description = entry.get("description") or f"Run '{key}' tool."
            if isinstance(description, list):
                description = " ".join(description)

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

        # Add dynamic params from JSON -> CLI: each flag as one --name=value argument, then "--" and
        # the positional values (tools/README.md, "Parameters")
        positional: list[str] = []
        for p in tool.params:
            pname = p["name"]
            if pname not in arguments:
                continue

            val = arguments[pname]
            style = p.get("style", "flag")  # default to "flag"

            if style == "positional":
                positional.append(str(val))
            elif style == "flag":
                argv.append(f"--{pname}={val}")
            else:
                raise ValueError(f"Unknown param style '{style}' for {pname}")
        if positional:
            argv += ["--", *positional]

        # Merge environment (base + tool-specific overrides)
        env = {**os.environ, **tool.env}

        started = time.monotonic()
        result = await self._run_one_cmdline_async(
            argv=argv,
            cwd=str(Path(self._project_base_path) / (tool.working_dir or ".")),
            env=env, )
        shown = " ".join(argv)
        shown = shown if len(shown) <= 100 else shown[:99] + "…"  # Arguments may hold a whole file
        self._log(f"ran {name}: {shown} (exit {result['status']}, {time.monotonic() - started:.1f}s)")
        return result

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

    @classmethod
    def serve(cls, config_path: Optional[Union[str, Path]] = None) -> int:
        """
        Run the server for the client that started it: the "server" section of an MCPAgent config,
        with its paths (tools_dir) relative to the repository root, over stdin and stdout.
        Args:
            config_path: The config file; None uses DEFAULT_CONFIG. Environment variables and ~ are
                expanded.
        Returns:
            int: 0 once the client closes stdin.
        Raises:
            RuntimeError: If the config is missing or invalid, or has no "server" section.
        """
        path = Path(os.path.expanduser(os.path.expandvars(str(config_path or DEFAULT_CONFIG)))).resolve()
        if not path.is_file():
            raise RuntimeError(f"Project file not found: {path}")
        old_cwd = Path.cwd()
        try:
            os.chdir(REPO_ROOT)  # The service reads tools_dir relative to its working directory
            service = cls(project_data=MCPAgentConfig.load(path).server)
            return asyncio.run(service.run_stdio())
        finally:
            os.chdir(old_cwd)


def build_arg_parser() -> argparse.ArgumentParser:
    """
    Build the argument parser for the server.
    Returns:
        argparse.ArgumentParser: The parser.
    """
    parser = argparse.ArgumentParser(
        prog="python -m mcpagent.service",
        description="The MCP server over the tools its config names, started by the agent's client "
                    "(python mcp/agent.py) and reached over stdin and stdout.")
    parser.add_argument("config", nargs="?", type=Path, metavar="CONFIG",
                        help="MCPAgent config file (default: mcp/mcpagent/jsons/mcpagent.json).")
    parser.add_argument("-v", "--version", action="store_true", help="Show the package version and exit.")
    return parser


def main() -> int:
    """
    The server's entry point.
    Returns:
        int: Exit code (0 = success, nonzero = failure).
    """
    args = build_arg_parser().parse_args()
    if args.version:
        print(f"mcpagent {__version__}")
        return 0
    if sys.stdin.isatty():
        print("The MCP server is started by the agent (python mcp/agent.py), which talks to it over stdin "
              "and stdout; it does not run on its own.", file=sys.stderr)
        return 2
    try:
        config_file = None
        if args.config is not None:
            config_file = args.config.expanduser().resolve()
            if not config_file.is_file():
                raise FileNotFoundError(f"Server config file not found: {config_file}")
        return MCPService.serve(config_file)
    except KeyboardInterrupt:
        return 0
    except Exception as error:  # Reported on stderr, which the client shows when it cannot start the server
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
