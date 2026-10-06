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
import json
import os
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import httpx
from jsonschema import ValidationError, validate
from prompt_toolkit import PromptSession
from rich.console import Console

from .client import MCPClient

OPENAI_HOST = "api.openai.com"  # Only used to decide whether OpenAI-specific error hints apply


def load_instructions(config_data: dict, config_file) -> str:
    """
    Read the model's instructions from the file the client config names.
    Args:
        config_data: The parsed client config.
        config_file: The config's path; "instructions_file" is relative to its folder.
    Returns:
        str: The file's "instructions" lines joined with newlines, or "" if none is configured.
    """
    instructions_file = config_data.get("instructions_file")
    if not instructions_file:
        return ""
    path = Path(config_file).resolve().parent / instructions_file
    return "\n".join(json.loads(path.read_text(encoding="utf-8"))["instructions"])


def load_models(config_data: dict, config_file) -> dict:
    """
    Read the model profiles from the file the client config names.
    Args:
        config_data: The parsed client config.
        config_file: The config's path; "models_file" is relative to its folder.
    Returns:
        dict: The file's contents: "default" (a profile name) and "profiles" (by name).
    Raises:
        ValueError: If the config does not name a models file.
    """
    models_file = config_data.get("models_file")
    if not models_file:
        raise ValueError('The client config has no "models_file"; point it at ../context/models.json.')
    path = Path(config_file).resolve().parent / models_file
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_model(models: dict, profile: Optional[str] = None,
                  model: Optional[str] = None, base_url: Optional[str] = None) -> dict:
    """
    Pick a model profile and apply overrides.
    Precedence: explicit arguments (command line), then the environment variables the profile
    names (model_env, base_url_env), then the profile's own values. The API key is read only
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
    return {
        "name": settings.get("name", name),
        "base_url": base_url or os.environ.get(settings.get("base_url_env", "")) or settings["base_url"],
        "model": model or os.environ.get(settings.get("model_env", "")) or settings["model"],
        "api_key": api_key,
        "timeout": float(settings.get("timeout", 60)),
    }


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
                 api_transport=None, max_tool_calls: int = 8, context: str = ""):
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
            api_transport: Optional httpx transport, used by tests to stand in for the model server.
            max_tool_calls: Maximum tool calls in one user turn.
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
        self.routes = {}
        # A dedicated client keeps the API key separate from MCP HTTP headers.
        self.api = httpx.AsyncClient(
            base_url=base_url + "/",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout, transport=api_transport,
        )

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

    async def _execute(self, call):
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
                server_id=server, timeout=30.0,
            )
        except Exception:
            # Retrying automatically could execute the same action twice.
            raise RuntimeError(f"Lost response from {server}/{name}; execution may have occurred. No retry was made.") from None
        result = response.get("result") or {
            "isError": True, "content": [{"type": "text", "text": json.dumps(response.get("error"))}]
        }
        text = readable("\n".join(item.get("text", "") for item in result.get("content", [])
                                   if item.get("type") == "text"))
        self.trace(f"{'✗' if result.get('isError') else '←'} {name}: {text}")
        return result

    async def _request_response(self, payload, on_text=None):
        """
        Send one Responses request, streaming text deltas when a callback is given.
        Args:
            payload: The Responses API request body.
            on_text: Called with each text delta; None makes a single non-streaming request.
        Returns:
            httpx.Response: The final response; when streaming, a response built from the
            completed event.
        """
        if on_text is None:
            return await self.api.post("responses", json=payload)
        async with self.api.stream("POST", "responses", json={**payload, "stream": True}) as response:
            if response.is_error:
                await response.aread()
                return response
            data_lines = []
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                elif not line and data_lines:
                    event = json.loads("\n".join(data_lines))
                    data_lines = []
                    kind = event.get("type")
                    if kind in {"response.output_text.delta", "response.refusal.delta"}:
                        on_text(event.get("delta", ""))
                    elif kind in {"response.completed", "response.incomplete", "response.failed"}:
                        return httpx.Response(200, json=event["response"])
                    elif kind == "error":
                        raise RuntimeError(f"{self.provider} stream failed. Earlier tool calls may have completed; no retry was made.")
            raise RuntimeError(f"{self.provider} stream ended before completion. Earlier tool calls may have completed; no retry was made.")

    async def ask(self, prompt: str, on_text=None) -> str:
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
        try:
            for _ in range(self.max_tool_calls + 1):
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
                if calls_used + len(calls) > self.max_tool_calls:
                    raise RuntimeError("Tool-call limit reached. Ask for fewer actions in one turn.")
                for call in calls:
                    result = await self._execute(call)
                    calls_used += 1
                    conversation.append({
                        "type": "function_call_output", "call_id": call["call_id"],
                        "output": json.dumps(result, ensure_ascii=False),
                    })
            raise RuntimeError("Tool-call limit reached.")
        except httpx.RequestError:
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
            await self.api.aclose()


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


class Output:
    """
    The terminal layout shared by the three agents: tool calls and results as dimmed lines while
    they happen, and the model's text with exactly one blank line before and after it.
    """

    def __init__(self, out: Console):
        """
        Args:
            out: The console to print to.
        """
        self.out = out
        self.in_text = False  # A text block is open
        self.pending = ""  # Trailing newlines held back until more text follows

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
            self.out.print()  # Blank line before the text
            self.in_text = True
        body = chunk.rstrip("\n")
        if body:
            self.out.print(self.pending + body, end="", markup=False)
            self.pending = chunk[len(body):]
        else:
            self.pending += chunk

    def line(self, text: str, style: str = "dim") -> None:
        """
        Print a whole line (a tool call or result), closing any open text block first.
        Args:
            text: The line.
            style: Rich style for the line.
        """
        self.end()
        self.out.print(text, style=style, markup=False)

    def end(self) -> None:
        """Close the open text block, if any: end its line and add the blank line after it."""
        if self.in_text:
            self.out.print("\n")
        self.in_text = False
        self.pending = ""


async def run_agent(config_file, profile=None, model=None, base_url=None, prompt=None, context="",
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
        trace: Print tool calls and results.
    Returns:
        int: The exit code.
    """
    console = Console(highlight=False, soft_wrap=True)  # Never re-wrap lines
    output = Output(console)
    agent: Optional[MCPAgent] = None
    console.print()  # Blank line before anything the agent prints

    async def display_answer(prompt):
        """
        Ask one prompt, printing tool activity and the streamed answer.
        Args:
            prompt: The user's message.
        """
        assert agent is not None  # Set before the first prompt
        try:
            answer = await agent.ask(prompt, on_text=output.text)
            if not output.in_text:
                output.text(answer)  # Nothing was streamed (e.g. a non-streaming reply)
        finally:
            output.end()

    try:
        mcp_client = MCPClient(config_file)
        models = load_models(mcp_client.config_data, config_file)
        settings = resolve_model(models, profile=profile, model=model, base_url=base_url)
        agent = MCPAgent(mcp_client, base_url=settings["base_url"], model=settings["model"],
                         api_key=settings["api_key"], provider=settings["name"], timeout=settings["timeout"],
                         instructions=load_instructions(mcp_client.config_data, config_file),
                         context=context, trace=output.line if trace else None)
        await agent.connect()
        routes = agent.routes.values()
        several_servers = len({server for server, _, _ in routes}) > 1
        tools = ", ".join(f"{server}/{name}" if several_servers else name for server, name, _ in routes)
        console.print(f"{agent.provider} model: {agent.model} @ {agent.base_url}", markup=False)
        console.print(f"Tools: {tools} (sequential)", markup=False)
        if prompt is not None:
            await display_answer(prompt)
            return 0
        console.print("Ask me to use a tool. /history shows messages, /reset clears them, exit quits.")
        session = PromptSession()
        while True:
            try:
                prompt = (await session.prompt_async("You > ")).strip()
            except (EOFError, KeyboardInterrupt):
                return 0
            if prompt.lower() in {"exit", "quit", "q"}:
                return 0
            if prompt == "/reset":
                agent.history = []
                console.print("History cleared.")
            elif prompt == "/history":
                console.print(json.dumps(agent.history, indent=2, ensure_ascii=False), markup=False)
            elif prompt:
                try:
                    await display_answer(prompt)
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
