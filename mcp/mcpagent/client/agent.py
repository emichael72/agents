"""
Module: agent.py

Description:
    The MCP Agent itself: the agent loop in which a model uses the tools of the configured MCP
    servers. The model is called through the OpenAI Responses API (function calling), which
    OpenAI and compatible servers such as LM Studio implement.

    `MCPAgent` discovers the MCP tools, then for each user turn (`ask`) calls the model, runs the
    tools it requests through MCP, sends the results back, and repeats until the model answers in
    text. History is kept in memory between turns. `Reply` is one model server reply.
    The terminal front end is `AgentSession` (session.py).
"""
import asyncio
import itertools
import json
from typing import Any, Callable, Optional

import aiohttp
from jsonschema import ValidationError, validate

from mcpagent.client.client import MCPClient
from mcpagent.client.guard import RepeatGuard
from mcpagent.client.output import Output


class Reply:
    """A model server's reply to one request: its HTTP status and JSON body."""

    def __init__(self, status_code: int, body: Optional[dict]):
        """
        Args:
            status_code: The HTTP status.
            body: The parsed JSON body; None if it was not JSON.
        """
        self.status_code = status_code
        self.body = body

    @property
    def is_error(self) -> bool:
        """Whether the status is an HTTP error (400 or above)."""
        return self.status_code >= 400

    def json(self) -> dict:
        """
        The JSON body.
        Returns:
            dict: The body.
        Raises:
            ValueError: If the body was not JSON.
        """
        if self.body is None:
            raise ValueError("The reply was not JSON")
        return self.body


class MCPAgent:
    """
    An agent whose tools are those of the configured MCP servers.
    `ask()` runs the agent loop for one user turn; the conversation history carries over
    between turns until it is reset.
    Each MCP tool is offered to the model under an alias (mcp_tool_<n>) that maps back to its
    server and name, so tools from different servers cannot collide.
    """

    def __init__(self, mcp_client: MCPClient, *, base_url: str, model: str, api_key: str,
                 provider: str = "OpenAI", timeout: float = 60.0, error_hints: Optional[str] = None,
                 instructions: str = "",
                 trace: Optional[Callable[[str], None]] = None,
                 max_tool_calls: int = 8, max_repeated_calls: int = 0, context: str = ""):
        """
        Set up the agent; call `connect()` before `ask()`.
        Args:
            mcp_client: Client for the MCP servers whose tools the model may use.
            base_url: OpenAI-compatible API base URL (from the model profile).
            model: Model id on that server.
            api_key: API key for that server only.
            provider: Display name used in messages, e.g. "OpenAI" or "Local model server".
            timeout: Request timeout in seconds.
            error_hints: "openai" for OpenAI's error advice (key, quota, billing); None for the
                generic advice that points at the server and the model.
            instructions: The model's instructions (see `AgentContext.system_prompt`).
            trace: Called with a line for each tool call ("→ tool(args)"), result ("← tool: output")
                and failure ("✗ tool: message").
            max_tool_calls: Maximum tool calls in one user turn; 0 means no limit.
            max_repeated_calls: The most times in a row one turn may make the same call (same tool,
                same arguments); the next is not run and the model is told to answer. 0 means no limit.
            context: Extra instructions appended to `instructions`.
        """
        base_url = base_url.rstrip("/")
        self.local = error_hints != "openai"
        self.base_url = base_url
        self.provider = provider
        self.mcp = mcp_client
        self.model = model
        self.trace = trace or (lambda _: None)
        self.max_tool_calls = max_tool_calls
        self.guard = RepeatGuard(max_repeated_calls)
        self.instructions = "\n".join(part for part in (instructions, context) if part)
        self.history = []
        self.tools = []
        self.routes: dict[str, tuple[str, str, dict[str, Any]]] = {}
        self.usage: list[tuple[int, int]] = []  # Tokens per model call of the last turn
        self.timeouts = {}  # Seconds to wait per tool alias, from the server's tools/list _meta
        # A dedicated session keeps the API key separate from MCP HTTP headers; it is opened on
        # first use, inside the running event loop.
        self._api_key = api_key
        self._timeout = timeout
        self._api: Optional[aiohttp.ClientSession] = None

    async def connect(self):
        """
        Connect to every enabled MCP server and collect its tools (following tools/list paging).
        Raises:
            RuntimeError: If a server cannot list its tools or no tools are found.
        """
        await self.mcp.connect(connect_all=True)
        for server in self.mcp.config_data.get("servers", []):
            if not server.get("enabled", True):
                continue
            server_id = server["server_id"]
            cursor = None
            seen_cursors = set()
            while True:
                response = await self.mcp.call(
                    "tools/list", {"cursor": cursor} if cursor else {}, server_id=server_id
                )
                if "error" in response:
                    raise RuntimeError(f"Cannot discover tools on {server_id}")
                for tool in response["result"]["tools"]:
                    # An index prevents collisions between servers and invalid API names.
                    alias = f"mcp_tool_{len(self.routes)}"
                    self.routes[alias] = (server_id, tool["name"], tool["inputSchema"])
                    self.timeouts[alias] = float((tool.get("_meta") or {}).get("timeout", 30)) + 5
                    self.tools.append({
                        "type": "function", "name": alias,
                        "description": f'{server_id}/{tool["name"]}: {tool.get("description", "")}',
                        "parameters": tool["inputSchema"],
                        # Preserve optional MCP fields; validate locally before execution.
                        "strict": False,
                    })
                cursor = response["result"].get("nextCursor")
                if not cursor:
                    break
                if cursor in seen_cursors:
                    raise RuntimeError(f"Repeated tools/list cursor on {server_id}")
                seen_cursors.add(cursor)
        if not self.tools:
            raise RuntimeError("No MCP tools discovered. Check the client configuration.")

    async def _execute(self, call: dict[str, Any]) -> dict[str, Any]:
        """
        Run one tool call requested by the model.
        The call is checked against the discovered tools and validated against the tool's schema
        before anything runs; a failed request is not retried, since the tool may already have run.
        Args:
            call: The model's function_call item (name, arguments as JSON, call_id).
        Returns:
            dict: The MCP tool result; problems are returned as {"isError": true, ...} so the model
            can explain them.
        """
        display_name = self.routes.get(call["name"], (None, call["name"], None))[1]
        try:
            shown_arguments = json.dumps(json.loads(call["arguments"]), separators=(",", ":"))
        except (TypeError, ValueError):
            shown_arguments = str(call.get("arguments"))
        self.trace(f"→ {display_name}({shown_arguments})")
        try:
            if call["name"] not in self.routes:
                raise ValueError("The requested tool is not in the discovered tool list")
            server, name, schema = self.routes[call["name"]]
            arguments = json.loads(call["arguments"])
            validate(arguments, schema)
            refusal = self.guard.refuse(name, arguments)
            if refusal:
                raise ValueError(refusal)
        except (ValueError, KeyError, ValidationError) as error:
            message = error.message if isinstance(error, ValidationError) else str(error)
            self.trace(f"✗ {display_name}: {message}")
            return {"isError": True, "content": [{"type": "text", "text": message}]}
        try:
            response = await self.mcp.call(
                "tools/call", {"name": name, "arguments": arguments},
                server_id=server, timeout=self.timeouts.get(call["name"], 35.0),
            )
        except Exception:
            # Retrying automatically could execute the same action twice.
            raise RuntimeError(f"Lost response from {server}/{name}; execution may have occurred. No retry was made.") from None
        result: dict[str, Any] = response.get("result") or {
            "isError": True, "content": [{"type": "text", "text": json.dumps(response.get("error"))}]
        }
        text = Output.readable("\n".join(item.get("text", "") for item in result.get("content", [])
                                   if item.get("type") == "text"))
        self.trace(f"{'✗' if result.get('isError') else '←'} {name}: {text}")
        return result

    def _session(self) -> aiohttp.ClientSession:
        """The HTTP session for the model server, opened on first use."""
        session = self._api
        if session is None or session.closed:
            # Connect and read timeouts, not a total one: a streamed answer may take a while
            timeout = aiohttp.ClientTimeout(total=None, sock_connect=self._timeout, sock_read=self._timeout)
            session = aiohttp.ClientSession(headers={"Authorization": f"Bearer {self._api_key}"}, timeout=timeout)
            self._api = session
        return session

    @staticmethod
    async def _body(response: aiohttp.ClientResponse) -> Optional[dict]:
        """Read a response's JSON body, or None if it is not JSON."""
        try:
            return await response.json(content_type=None)
        except (ValueError, aiohttp.ContentTypeError):
            return None

    async def _request_response(self, payload: dict[str, Any],
                                on_text: Optional[Callable[[str], None]] = None) -> Reply:
        """
        Send one Responses request, streaming text deltas when a callback is given.
        Args:
            payload: The Responses API request body.
            on_text: Called with each text delta; None makes a single non-streaming request.
        Returns:
            Reply: The final reply; when streaming, the response in the completed event.
        """
        url = self.base_url + "/responses"
        if on_text is None:
            async with self._session().post(url, json=payload) as response:
                return Reply(response.status, await self._body(response))
        async with self._session().post(url, json={**payload, "stream": True}) as response:
            if response.status >= 400:
                return Reply(response.status, await self._body(response))
            data_lines = []
            async for raw in response.content:  # One line at a time, however the bytes arrive
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                elif not line and data_lines:
                    event = json.loads("\n".join(data_lines))
                    data_lines = []
                    kind = event.get("type")
                    if kind in {"response.output_text.delta", "response.refusal.delta"}:
                        on_text(event.get("delta", ""))
                    elif kind in {"response.completed", "response.incomplete", "response.failed"}:
                        return Reply(200, event["response"])
                    elif kind == "error":
                        raise RuntimeError(f"{self.provider} stream failed. Earlier tool calls may have completed; no retry was made.")
            raise RuntimeError(f"{self.provider} stream ended before completion. Earlier tool calls may have completed; no retry was made.")

    async def ask(self, prompt: str, on_text: Optional[Callable[[str], None]] = None) -> str:
        """
        Run one user turn: call the model, run the tools it requests, repeat until it answers.
        On success the turn is added to the history; on any failure the history is cleared, so a
        later turn never replays unanswered tool calls.
        Args:
            prompt: The user's message.
            on_text: Called with each streamed text delta, if given.
        Returns:
            str: The model's final text answer.
        Raises:
            RuntimeError: On API errors, an incomplete response, or too many tool calls.
        """
        conversation = self.history + [{"role": "user", "content": prompt}]
        calls_used = 0
        self.guard.reset()
        self.usage = []  # (input tokens, output tokens) per model call of this turn, when reported
        try:
            for _ in (itertools.count() if not self.max_tool_calls else range(self.max_tool_calls + 1)):
                response = await self._request_response({
                    "model": self.model, "instructions": self.instructions,
                    "input": conversation, "tools": self.tools,
                    "parallel_tool_calls": False, "store": False,
                    "include": ["reasoning.encrypted_content"],
                }, on_text=on_text)
                if response.is_error:
                    if self.local:
                        raise RuntimeError(f"{self.provider} HTTP {response.status_code} from {self.base_url}. "
                                           f"Check that model '{self.model}' is available on the server.")
                    hints = {
                        401: "Check OPENAI_API_KEY.",
                        403: "Check project and model permissions.",
                        404: "Check OPENAI_MODEL or --model for a model available to your project.",
                        429: "Check API quota, billing, and rate limits.",
                    }
                    # Inspect only known error codes; raw provider errors can echo credentials.
                    try:
                        code = response.json().get("error", {}).get("code")
                    except (ValueError, AttributeError):
                        code = None
                    if code == "insufficient_quota":
                        raise RuntimeError("OpenAI HTTP 429 (insufficient_quota). Add API credit or check the API project's billing/spend limit.")
                    if code == "rate_limit_exceeded":
                        raise RuntimeError("OpenAI HTTP 429 (rate_limit_exceeded). Wait before retrying or check the project's rate limits.")
                    raise RuntimeError(f"OpenAI HTTP {response.status_code}. " + hints.get(response.status_code, "Request failed; check the model and configuration."))
                data = response.json()
                usage = data.get("usage") or {}
                if usage:
                    self.usage.append((int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))))
                if data.get("status") != "completed":
                    raise RuntimeError(f"{self.provider} response did not complete ({data.get('status', 'unknown')}).")
                output = data.get("output", [])
                conversation.extend(output)
                calls = [item for item in output if item.get("type") == "function_call"]
                if not calls:
                    text = "\n".join(
                        part.get("text", part.get("refusal", ""))
                        for item in output if item.get("type") == "message"
                        for part in item.get("content", [])
                        if part.get("type") in ("output_text", "refusal")
                    )
                    if not text:
                        raise RuntimeError(f"{self.provider} returned no text or tool calls.")
                    self.history = conversation
                    return text
                if self.max_tool_calls and calls_used + len(calls) > self.max_tool_calls:
                    raise RuntimeError("Tool-call limit reached. Ask for fewer actions in one turn.")
                for call in calls:
                    result = await self._execute(call)
                    calls_used += 1
                    conversation.append({
                        "type": "function_call_output", "call_id": call["call_id"],
                        "output": json.dumps(result, ensure_ascii=False),
                    })
            raise RuntimeError("Tool-call limit reached.")
        except (aiohttp.ClientError, asyncio.TimeoutError):
            self.history = []
            raise RuntimeError(f"Could not reach {self.provider} at {self.base_url}. Check your network/proxy. Earlier tool calls may have completed; they were not retried.") from None
        except Exception:
            # Never replay orphaned function calls after a failed turn.
            self.history = []
            raise

    async def close(self):
        """Close the MCP connections and the HTTP client."""
        try:
            await self.mcp.close(close_all=True)
        finally:
            if self._api is not None:
                await self._api.close()
