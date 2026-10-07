"""
Module: agent.py

Description:
    The MCP Agent itself: the agent loop in which a model uses the tools of the configured MCP
    servers. The model is called through the OpenAI Responses API (function calling), which
    OpenAI and compatible servers such as LM Studio implement.

    The module provides:
      - `load_instructions`: reads the model's instructions from the shared file that the
        client config's "instructions_file" names (agents/context/instructions.json).
      - `load_models` and `resolve_model`: read the shared model profiles that the client
        config's "models_file" names (agents/context/models.json), pick one, and apply
        command-line and environment overrides.
      - `MCPAgent`: discovers the MCP tools, then for each user turn (`ask`) calls the model,
        runs the tools it requests through MCP, sends the results back, and repeats until the
        model answers in text. History is kept in memory between turns.
      - `run_agent`: the terminal front end, for one prompt or an interactive session.
"""
import asyncio
import itertools
import json
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import aiohttp
from jsonschema import ValidationError, validate
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import ANSI
from rich.console import Console
from rich.status import Status
from rich.style import Style
from rich.text import Text

from mcpagent.config import repo_path
from .client import MCPClient

OPENAI_HOST = "api.openai.com"  # Only used to decide whether OpenAI-specific error hints apply


def load_instructions(config_data: dict[str, Any], key: str = "instructions") -> str:
    """
    Read the model's instructions from the file the client config names.
    Args:
        config_data: The parsed client config; "instructions_file" is relative to the repository.
        key: Which lines to read: "instructions", or "on_exit" (the prompt sent before exit).
    Returns:
        str: The lines joined with newlines, or "" if none are configured.
    """
    instructions_file: Optional[str] = config_data.get("instructions_file")
    if not instructions_file:
        return ""
    path = repo_path(instructions_file)
    return "\n".join(json.loads(path.read_text(encoding="utf-8")).get(key, []))


def load_models(config_data: dict[str, Any]) -> dict:
    """
    Read the model profiles from the file the client config names.
    Args:
        config_data: The parsed client config; "models_file" is relative to the repository.
    Returns:
        dict: The file's contents: "default" (a profile name) and "profiles" (by name).
    Raises:
        ValueError: If the config does not name a models file.
    """
    models_file: Optional[str] = config_data.get("models_file")
    if not models_file:
        raise ValueError('The client config has no "models_file"; point it at context/models.json.')
    path = repo_path(models_file)
    return json.loads(path.read_text(encoding="utf-8"))


# Match model discovery across the independent agent implementations.
# noinspection DuplicatedCode
def loaded_model(base_url: str, api_key: str = "") -> Optional[str]:
    """
    Ask an LM Studio server which model is loaded (its /api/v0/models lists each model's state).
    Args:
        base_url: The server's OpenAI-compatible base URL, e.g. http://boba:1234/v1.
        api_key: Sent as a bearer token, for servers that check it.
    Returns:
        Optional[str]: The first loaded language model's id, or None when none is loaded or the
            server cannot tell (not LM Studio, unreachable).
    """
    root = base_url.rstrip("/").removesuffix("/v1")
    request = urllib.request.Request(root + "/api/v0/models", headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            models = json.loads(response.read()).get("data", [])
    except (OSError, ValueError):
        return None
    return next((m["id"] for m in models if m.get("state") == "loaded" and m.get("type") in ("llm", "vlm")), None)


# Keep profile precedence consistent across the independent agent implementations.
# noinspection DuplicatedCode
def resolve_model(models: dict, profile: Optional[str] = None,
                  model: Optional[str] = None, base_url: Optional[str] = None) -> dict:
    """
    Pick a model profile and apply overrides.
    Precedence: explicit arguments (command line), then the environment variables the profile
    names (model_env, base_url_env), then, with "model_auto", the model loaded on the server
    (LM Studio), then the profile's own values. The API key is read only
    from the profile's api_key_env (or its api_key fallback), so a key is never sent to a
    server it was not configured for.
    Args:
        models: The model profiles, as returned by `load_models`.
        profile: Profile name; None uses the profile named by "default".
        model: Overrides the profile's model.
        base_url: Overrides the profile's base URL.
    Returns:
        dict: name, base_url, model, api_key and timeout, as `MCPAgent` expects.
    Raises:
        ValueError: If the profile does not exist, lacks base_url or model, or its API key is not set.
    """
    profiles = models.get("profiles") or {}
    name = profile or models.get("default")
    settings = profiles.get(name)
    if not isinstance(settings, dict):
        available = ", ".join(profiles) or "none"
        raise ValueError(f"Unknown model profile '{name}' (available: {available}). Check the models file.")
    missing = [key for key in ("base_url", "model") if not settings.get(key)]
    if missing:
        raise ValueError(f"Model profile '{name}' is missing {', '.join(missing)}.")
    api_key = (os.environ.get(settings.get("api_key_env", ""), "") or settings.get("api_key", "")).strip()
    if not api_key:
        raise ValueError(f"Set {settings.get('api_key_env', 'an API key')} in the environment for the '{name}' profile.")
    base_url = base_url or os.environ.get(settings.get("base_url_env", "")) or settings["base_url"]
    model = model or os.environ.get(settings.get("model_env", "")) \
        or (settings.get("model_auto") and loaded_model(base_url, api_key)) or settings["model"]
    return {
        "name": settings.get("name", name),
        "base_url": base_url,
        "model": model,
        "api_key": api_key,
        "timeout": float(settings.get("timeout", 60)),
    }


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
                 provider: str = "OpenAI", timeout: float = 60.0, instructions: str = "",
                 trace: Optional[Callable[[str], None]] = None,
                 max_tool_calls: int = 8, context: str = ""):
        """
        Set up the agent; call `connect()` before `ask()`.
        Args:
            mcp_client: Client for the MCP servers whose tools the model may use.
            base_url: OpenAI-compatible API base URL (from the model profile).
            model: Model id on that server.
            api_key: API key for that server only.
            provider: Display name used in messages, e.g. "OpenAI" or "Local model server".
            timeout: Request timeout in seconds.
            instructions: The model's instructions (see `load_instructions`).
            trace: Called with a line for each tool call ("→ tool(args)"), result ("← tool: output")
                and failure ("✗ tool: message").
            max_tool_calls: Maximum tool calls in one user turn; 0 means no limit.
            context: Extra instructions appended to `instructions`.
        """
        base_url = base_url.rstrip("/")
        self.local = urlparse(base_url).hostname != OPENAI_HOST
        self.base_url = base_url
        self.provider = provider
        self.mcp = mcp_client
        self.model = model
        self.trace = trace or (lambda _: None)
        self.max_tool_calls = max_tool_calls
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
        text = readable("\n".join(item.get("text", "") for item in result.get("content", [])
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


# Tool output is rendered consistently across the independent agent implementations.
# noinspection DuplicatedCode
def readable(output: str) -> str:
    """
    Make a tool's output readable for the terminal.
    Args:
        output: The output as sent to the model: plain text, or JSON such as the server's
            {"status", "logs", "summary"} result or an {"error": ...} failure.
    Returns:
        str: The "logs" lines or the error message when the output is such JSON, else the text.
    """
    try:
        data = json.loads(output)
    except (TypeError, ValueError):
        return output
    if isinstance(data, dict) and "logs" in data:
        return "\n".join(str(line) for line in data["logs"])
    if isinstance(data, dict) and "error" in data:
        return readable(str(data["error"]))
    return output


def load_output_settings(config_data: dict[str, Any]) -> dict:
    """
    Read the terminal layout settings from the file the client config names.
    Args:
        config_data: The parsed client config; "output_file" is relative to the repository.
    Returns:
        dict: "width" (wrap column) and "show_time", or {} (the defaults) if none is configured.
    """
    output_file: Optional[str] = config_data.get("output_file")
    if not output_file:
        return {}
    path = repo_path(output_file)
    return json.loads(path.read_text(encoding="utf-8"))


# Keep the memory instructions consistent across the independent agent implementations.
# noinspection DuplicatedCode
def memory_text(index: Optional[Path]) -> str:
    """
    The agents' memory, to append to their instructions: the topics in the memory index (kept by
    the memory tool), so the model knows what it remembers without having to look.
    Args:
        index: The index file (context/agent.json's memory_index); None when not configured.
    Returns:
        str: A paragraph listing the topics, or saying the memory is empty; "" without an index.
    """
    if index is None:
        return ""
    lines = [line.replace("**", "") for line in (index.read_text(encoding="utf-8").splitlines() if index.is_file() else [])
             if line.startswith("- ")]
    if not lines:
        return "\n\nYour memory is empty: save lasting facts and the user's preferences with the memory tool."
    return ("\n\nYour memory (topics saved in earlier runs; read one with the memory tool before relying on it, "
            "and save new facts with it):\n" + "\n".join(lines))


def identity_text(name: Optional[str], config_data: dict[str, Any]) -> str:
    """
    The identity lines that open the instructions, naming the agent.
    Args:
        name: The agent's name (context/agent.json's names); None when not configured.
        config_data: The parsed client config.
    Returns:
        str: The lines with {name} filled in, and a blank line after them; "" without a name.
    """
    text = load_instructions(config_data, "identity")
    return text.replace("{name}", name) + "\n\n" if name and text else ""


def worth_saving(history: list) -> bool:
    """
    Whether a session may hold something to remember: a tool call, or more than one exchange.
    Args:
        history: The session's Responses API items.
    Returns:
        bool: True when the model should be asked to save before exit.
    """
    calls = sum(item.get("type") == "function_call" for item in history)
    prompts = sum(item.get("role") == "user" for item in history)
    return calls > 0 or prompts > 1


def load_agent_settings(config_data: dict[str, Any]) -> dict:
    """
    Read the agent loop settings from the file the client config names.
    Args:
        config_data: The parsed client config; "agent_file" is relative to the repository.
    Returns:
        dict: "max_tool_calls", or {} (the defaults) if none is configured.
    """
    agent_file: Optional[str] = config_data.get("agent_file")
    if not agent_file:
        return {}
    path = repo_path(agent_file)
    return json.loads(path.read_text(encoding="utf-8"))


# A Markdown link, [text](url), or a bare web address: shown as a clickable OSC 8 link, in LINK_COLOR
# The terminal layout is intentionally consistent across the independent agent implementations.
# noinspection DuplicatedCode
LINK_COLOR = "bright_cyan"
LINK = re.compile(r"\[([^]\n]+)]\((https?://[^\s)]+)\)|(https?://[^\s<>()\[\]\"'`]+)")
OPEN_LINK = re.compile(r"\[[^]\n]*$|]\([^)\s]*$")  # A Markdown link that is not finished yet


# noinspection DuplicatedCode
def link_segments(text: str) -> list[tuple[str, Optional[str]]]:
    """
    Split text into plain parts and links, the same way in all three agents.
    Args:
        text: The text.
    Returns:
        list[tuple[str, Optional[str]]]: (shown text, URL or None) pairs. A Markdown link shows only
            its text; a bare address shows itself, without trailing punctuation.
    """
    segments, position = [], 0
    for match in LINK.finditer(text):
        shown, url = (match.group(1), match.group(2)) if match.group(1) else (match.group(3), match.group(3))
        trailing = ""
        if not match.group(1):
            stripped = url.rstrip(".,;:!?")
            trailing, url, shown = url[len(stripped):], stripped, stripped
        segments += [(text[position:match.start()], None), (shown, url), (trailing, None)]
        position = match.end()
    segments.append((text[position:], None))
    return [(part, url) for part, url in segments if part]


def wrap(text: str, width: int, indent: str = "  ") -> list[str]:
    """
    Word-wrap text to a width, the same way in all three agents. Each line wraps on its own;
    continuation lines start with `indent`. A word longer than the width (e.g. a URL) is kept whole.
    Args:
        text: The text; may contain newlines.
        width: The column to wrap at.
        indent: Prefix for continuation lines.
    Returns:
        list[str]: The wrapped lines.
    """
    lines = []
    for raw in text.split("\n"):
        words = raw.split(" ")
        line = words[0]
        for word in words[1:]:
            if line.strip() and len(line) + 1 + len(word) > width:
                lines.append(line)
                line = indent + word
            else:
                line += " " + word
        lines.append(line)
    return lines


# noinspection DuplicatedCode
class Output:
    """
    The terminal layout shared by the three agents (README.md, "Terminal output"):
      - By default, a spinner runs while the model thinks or a tool runs ("Running shell…"), and
        only the answer and the timing line are printed.
      - With debug, everything except the model's answer (banner, hints, tool calls and results,
        timing) is a dark gray line, and there is no spinner.
      - The model's streamed answer is word-wrapped as it arrives, with exactly one blank line
        before and after it.
      - All output fits in the configured width (context/output.json), or the terminal's if narrower.
      - Each response ends with how long it took.
    """

    def __init__(self, out: Console, settings: Optional[dict] = None, debug: bool = True):
        """
        Args:
            out: The console to print to.
            settings: The layout settings (context/output.json); None uses the defaults.
        """
        settings = settings or {}
        self.out = out
        self.width = int(settings.get("width", 120))
        if out.is_terminal:
            self.width = min(self.width, out.width)
        self.show_time = bool(settings.get("show_time", True))
        self.show_tokens = bool(settings.get("show_tokens", False))
        self.links = bool(settings.get("links", False))  # OSC 8 links (rich adds them only on a terminal)
        self.usage: Optional[dict[str, int]] = None  # Tokens and model calls of this response, when the server reports them
        self.in_text = False  # A text block is open
        self.pending = ""  # Trailing newlines held back until more text follows
        self.column = 0  # Where the streamed answer's current line ends
        self.word = ""  # The streamed word being collected
        self.spaces = ""  # The spaces before it
        self.started = time.monotonic()
        self.debug = debug  # Print the gray lines; without it, a spinner shows the activity instead
        self.spinner: Optional[Status] = None
        self.blank_owed = False  # The last text ended without its blank line after it

    def start(self) -> None:
        """Start timing a response, and the spinner when not in debug mode (on a terminal only)."""
        self.started = time.monotonic()
        self.usage = None
        self.spin("Thinking…")

    def spin(self, label: str) -> None:
        """
        Show a label on the spinner, starting it if needed; only when not in debug mode, on a terminal.
        Args:
            label: What the agent is doing, e.g. "Thinking…".
        """
        if self.debug or not self.out.is_terminal:
            return
        if self.spinner is None:
            spinner = self.out.status(Text(label, style="bright_black"), spinner="dots",
                                      spinner_style="bright_black")
            self.spinner = spinner
            spinner.start()
        else:
            self.spinner.update(Text(label, style="bright_black"))

    def stop_spinner(self) -> None:
        """Stop the spinner, if it runs; it leaves nothing on the screen."""
        if self.spinner is not None:
            self.spinner.stop()
            self.spinner = None

    def add_usage(self, input_tokens: int, output_tokens: int, requests: int = 1) -> None:
        """
        Count the tokens of model calls made for this response.
        Args:
            input_tokens: Tokens sent to the model (prompt, history, tool results).
            output_tokens: Tokens the model generated.
            requests: How many model calls these tokens cover.
        """
        usage = self.usage or {"input": 0, "output": 0, "requests": 0}
        self.usage = usage
        usage["input"] += input_tokens
        usage["output"] += output_tokens
        usage["requests"] += requests

    def text(self, chunk: str) -> None:
        """
        Print a chunk of streamed model text.
        Args:
            chunk: The text delta.
        """
        if not self.in_text:
            chunk = chunk.lstrip("\n")
            if not chunk:
                return
            self.stop_spinner()
            self.out.print()  # Blank line before the text
            self.blank_owed = False
            self.in_text = True
        body = chunk.rstrip("\n")
        if body:
            self.out.print(self._render(self._wrap_stream(self.pending + body)), end="", soft_wrap=True)
            self.pending = chunk[len(body):]
        else:
            self.pending += chunk

    def line(self, text: str) -> None:
        """
        Print a whole dark gray line (a tool call or result, the banner) in debug mode; otherwise
        only show the activity on the spinner: "Running <tool>…" for a call, "Thinking…" after it.
        Args:
            text: The line; may contain newlines.
        """
        if not self.debug:
            if text.startswith("→ "):
                self.end(blank=False)  # A call after some answer text: end its line, and spin again
                self.spin(f"Running {text[2:].split('(')[0]}…")
            elif self.spinner is not None and text.startswith(("← ", "✗ ")):
                self.spin("Thinking…")
            return
        self.note(text)

    def note(self, text: str) -> None:
        """
        Print a whole dark gray line, also when not in debug mode (the timing line, replies to
        commands), wrapped to the width, closing any open text block first.
        Args:
            text: The line; may contain newlines.
        """
        self.stop_spinner()
        self.end()
        if self.blank_owed:  # Text ended without its blank line (a tool call followed it)
            self.out.print()
            self.blank_owed = False
        for line in wrap(text, self.width):
            self.out.print(self._render(line), style="bright_black", soft_wrap=True)

    def end(self, blank: bool = True) -> None:
        """
        Close the open text block, if any: end its line and add the blank line after it.
        Args:
            blank: Add the blank line now; False leaves it to what follows (more text adds its own,
                and a gray line adds one first), so text around a hidden tool call has only one.
        """
        if self.in_text:
            self.out.print(self._render(self._take_word()), end="", soft_wrap=True)
            self.out.print("\n" if blank else "")
            self.blank_owed = not blank
        self.in_text = False
        self.pending = ""
        self.column = 0
        self.word = self.spaces = ""

    def finish(self) -> None:
        """
        Close the response: the line with how long it took since `start` and the tokens it used goes
        right under the answer, and a blank line after it sets the response off from the next prompt.
        """
        self.stop_spinner()
        self.end(blank=False)
        self.blank_owed = False  # The timing line belongs to the answer: no blank line between them
        parts = [f"Response time: {time.monotonic() - self.started:.1f}s"] if self.show_time else []
        if self.show_tokens:
            if self.usage:
                calls = self.usage["requests"]
                parts.append(f"tokens: {self.usage['input']:,} in, {self.usage['output']:,} out")
                parts.append(f"{calls} model call{'s' if calls != 1 else ''}")
            else:
                parts.append("tokens: not reported")
        if parts:
            self.note(" · ".join(parts))
        self.out.print()

    def _render(self, text: str) -> Text:
        """
        Turn text into a rich Text, with its links as clickable OSC 8 links when links are on, in
        LINK_COLOR: the one vivid color, in the gray lines too, so a link such as the quiz's stands out.
        Args:
            text: Plain text, possibly with Markdown links or web addresses.
        Returns:
            Text: The text to print; rich writes the link codes only on a terminal.
        """
        if not self.links:
            return Text(text)
        rendered = Text()
        for part, url in link_segments(text):
            rendered.append(part, style=Style(link=url, color=LINK_COLOR) if url else None)
        return rendered

    def _wrap_stream(self, text: str) -> str:
        """
        Word-wrap streamed text: words are held until they end, so they can move to the next line.
        Args:
            text: The text to add.
        Returns:
            str: What can be printed now.
        """
        printed = []
        for char in text:
            if char == "\n":
                printed.append(self._take_word() + "\n")
                self.column = 0
                self.spaces = ""
            elif char == " " and self.links and OPEN_LINK.search(self.word) and len(self.word) < 300:
                self.word += char  # Inside [text](url): keep the link together
            elif char == " ":
                printed.append(self._take_word())
                self.spaces += " "
            else:
                self.word += char
        return "".join(printed)

    def _take_word(self) -> str:
        """
        Place the collected word on the current line, or on the next one if it does not fit.
        Returns:
            str: The word with what goes before it (its spaces, or a line break).
        """
        if not self.word:
            return ""
        length = sum(len(part) for part, _ in link_segments(self.word)) if self.links else len(self.word)
        if self.column and self.column + len(self.spaces) + length > self.width:
            placed = "\n" + self.word
            self.column = length
        else:
            placed = self.spaces + self.word
            self.column += len(self.spaces) + length
        self.word = self.spaces = ""
        return placed


async def run_agent(config_file: str | Path, profile=None, model=None, base_url=None, prompt=None, context="",
                    trace=True) -> int:
    """
    Run the agent in the terminal: one prompt, or interactively until the user exits.
    Args:
        config_file: The client config (MCP servers and model profiles).
        profile: Model profile name; None uses the config's default.
        model: Overrides the profile's model.
        base_url: Overrides the profile's base URL.
        prompt: A single prompt to answer and exit; None starts an interactive session.
        context: Extra instructions for the assistant.
        trace: Debug mode: print the gray lines (banner, hints, tool calls and results) instead of a spinner.
    Returns:
        int: The exit code.
    """
    console = Console(highlight=False, soft_wrap=True)  # Never re-wrap lines; Output wraps
    output = Output(console, debug=trace)  # Replaced by the configured layout once the config is read
    agent: Optional[MCPAgent] = None
    if trace:
        console.print()  # Blank line before the banner (hidden without debug, so the answer's own blank line is enough)

    async def display_answer(current_agent: MCPAgent, user_prompt: str):
        """
        Ask one prompt, printing tool activity and the streamed answer.
        Args:
            current_agent: The connected agent.
            user_prompt: The user's message.
        """
        output.start()
        try:
            answer = await current_agent.ask(user_prompt, on_text=output.text)
            if not output.in_text:
                output.text(answer)  # Nothing was streamed (e.g. a non-streaming reply)
        finally:
            for input_tokens, output_tokens in getattr(current_agent, "usage", []):
                output.add_usage(input_tokens, output_tokens)
            output.finish()

    try:
        mcp_client = MCPClient(config_file)
        output = Output(console, load_output_settings(mcp_client.config_data), debug=trace)
        models = load_models(mcp_client.config_data)
        settings = resolve_model(models, profile=profile, model=model, base_url=base_url)
        agent_settings = load_agent_settings(mcp_client.config_data)
        index: Optional[str] = agent_settings.get("memory_index")  # Relative to the repository
        memory = memory_text(repo_path(index) if index else None)
        active_agent = MCPAgent(mcp_client, base_url=settings["base_url"], model=settings["model"],
                         api_key=settings["api_key"], provider=settings["name"], timeout=settings["timeout"],
                         instructions=identity_text(agent_settings.get("names", {}).get("mcpagent"),
                                                    mcp_client.config_data)
                         + load_instructions(mcp_client.config_data) + memory,
                         context=context, trace=output.line,  # Printed in debug mode, else on the spinner
                         max_tool_calls=int(agent_settings.get("max_tool_calls", 8)))
        agent = active_agent  # Retain it for cleanup even if connecting fails.
        await active_agent.connect()
        servers = len({server for server, _, _ in active_agent.routes.values()})
        tools = f"{len(active_agent.routes)} tools" + (f" from {servers} servers" if servers > 1 else "")
        output.line(f"{active_agent.provider} model: {active_agent.model} @ {active_agent.base_url}, {tools} (sequential)")
        if prompt is not None:
            await display_answer(active_agent, prompt)
            return 0
        output.line("Ask me to use a tool. /history shows messages, /reset clears them, exit quits.")
        on_exit = load_instructions(mcp_client.config_data, "on_exit")
        session = PromptSession()
        while True:
            try:
                prompt = (await session.prompt_async(ANSI("\x1b[90mYou > \x1b[0m"))).strip()
            except (EOFError, KeyboardInterrupt):
                return 0
            if prompt.lower() in {"exit", "quit", "q"}:
                # One last turn to save what is worth remembering (save_on_exit); Ctrl+C skips it
                if agent_settings.get("save_on_exit") and on_exit and worth_saving(active_agent.history):
                    output.line("Before exiting: saving anything worth remembering (Ctrl+C skips).")
                    try:
                        await display_answer(active_agent, on_exit)
                    except Exception as error:
                        console.print(f"Error: {error}", style="red", markup=False)
                return 0
            if prompt == "/reset":
                active_agent.history = []
                output.note("History cleared.")
            elif prompt == "/history":
                console.print(json.dumps(active_agent.history, indent=2, ensure_ascii=False), markup=False)
            elif prompt:
                try:
                    await display_answer(active_agent, prompt)
                except Exception as error:
                    console.print(f"Error: {error}", style="red", markup=False)
    except (KeyboardInterrupt, asyncio.CancelledError):
        return 0
    except Exception as error:
        console.print(f"Error: {error}", style="red", markup=False)
        return 1
    finally:
        if agent is not None:
            await agent.close()
