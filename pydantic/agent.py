"""
Module: agent.py

Description:
    The Pydantic Agent: a terminal agent built with pydantic-ai, the counterpart of MCPAgent's
    client (`python -m mcpagent.client`).

    It uses the same model profiles (agents/context/models.json), instructions
    (agents/context/instructions.json) and tools (agents/tools) as MCPAgent. The agent loop
    (call the model, run the requested tools, send the results back, repeat until it answers) is
    done by pydantic-ai; this module only:
      - Builds the model and the `Agent`, with local tools or an MCP server's tools (--mcp).
      - Runs one turn at a time and renders pydantic-ai's events (streamed text, tool calls
        and results) in the terminal.
      - Provides the interactive chat and the command line.
"""
import argparse
import asyncio
import json
import os
import re
import time
import urllib.request
from dataclasses import asdict
from pathlib import Path
from typing import Optional
from pprint import pprint

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")  # Skip pydantic-ai's observability banner

import httpx2
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import ANSI
from pydantic_ai import Agent, AgentRunResultEvent, UsageLimits
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.messages import (FunctionToolCallEvent, FunctionToolResultEvent, PartDeltaEvent,
                                  PartStartEvent, TextPart, TextPartDelta, ToolReturnPart)
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from rich.console import Console
from rich.style import Style
from rich.text import Text

from toolset import toolset as local_toolset

MCP_URL = "http://127.0.0.1:6275/"  # MCPAgent's server (python -m mcpagent.server)

# Instructions (system prompt) and model profiles, shared by all three agents
CONTEXT_DIR = Path(__file__).resolve().parent.parent / "context"
INSTRUCTIONS_FILE = CONTEXT_DIR / "instructions.json"
MODELS_FILE = CONTEXT_DIR / "models.json"
OUTPUT_FILE = CONTEXT_DIR / "output.json"  # Terminal layout
AGENT_FILE = CONTEXT_DIR / "agent.json"  # Agent loop settings

# Tool calls per prompt, shared with the other agents (context/agent.json); 0 means no limit.
# pydantic-ai also caps model requests (50 by default): allow one per tool call, plus retries and
# the final answer, or none at all when tool calls are unlimited.
MAX_TOOL_CALLS = int(json.loads(AGENT_FILE.read_text(encoding="utf-8")).get("max_tool_calls", 8))
LIMITS = (UsageLimits(tool_calls_limit=MAX_TOOL_CALLS, request_limit=2 * MAX_TOOL_CALLS + 1) if MAX_TOOL_CALLS
          else UsageLimits(tool_calls_limit=None, request_limit=None))

console = Console(highlight=False, soft_wrap=True)  # Never re-wrap lines


def load_instructions(path: Path = INSTRUCTIONS_FILE) -> str:
    """
    Read the shared instructions file.
    Args:
        path: The JSON file (default: agents/context/instructions.json).
    Returns:
        str: Its "instructions" lines, joined with newlines.
    """
    return "\n".join(json.loads(path.read_text(encoding="utf-8"))["instructions"])


def memory_text(index: Path | None) -> str:
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


def build_agent(model: Model, mcp_url: str | None = None) -> Agent:
    """
    Build the agent with the shared instructions and a tool source.
    Args:
        model: The model to drive the agent.
        mcp_url: An MCP server URL to take tools from; None uses the local tools folder.
    Returns:
        Agent: The pydantic-ai agent.
    """
    toolset = MCPToolset(mcp_url, tool_error_behavior="failed") if mcp_url else local_toolset
    index = json.loads(AGENT_FILE.read_text(encoding="utf-8")).get("memory_index")
    instructions = load_instructions() + memory_text(CONTEXT_DIR.parent / index if index else None)
    return Agent(model, instructions=instructions, toolsets=[toolset])


def load_models(path: Path = MODELS_FILE) -> dict:
    """
    Read the shared model profiles file.
    Args:
        path: The JSON file (default: agents/context/models.json).
    Returns:
        dict: "default" (a profile name) and "profiles" (by name).
    """
    return json.loads(path.read_text(encoding="utf-8"))


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


def resolve_model(models: dict, profile: str | None = None,
                  model: str | None = None, base_url: str | None = None) -> dict:
    """
    Pick a model profile and apply overrides, the same way as MCPAgent.
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
        dict: name, base_url, model, api_key and timeout.
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


def build_model(settings: dict) -> Model:
    """
    Create a model on an OpenAI-compatible server (LM Studio, OpenAI, ...).
    Args:
        settings: A resolved profile, as returned by `resolve_model`.
    Returns:
        Model: A pydantic-ai chat-completions model.
    """
    http_client = httpx2.AsyncClient(timeout=settings["timeout"])
    provider = OpenAIProvider(base_url=settings["base_url"], api_key=settings["api_key"], http_client=http_client)
    return OpenAIChatModel(settings["model"], provider=provider)


def readable(output: str) -> str:
    """
    Make a tool's output readable for the terminal.
    Args:
        output: The output as sent to the model: plain text, or JSON such as MCPAgent's
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


def load_output_settings(path: Path = OUTPUT_FILE) -> dict:
    """
    Read the shared terminal layout settings.
    Args:
        path: The JSON file (default: agents/context/output.json).
    Returns:
        dict: "width" (wrap column) and "show_time" (print each response's duration).
    """
    return json.loads(path.read_text(encoding="utf-8"))


# A Markdown link, [text](url), or a bare web address: shown as a clickable OSC 8 link
LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)|(https?://[^\s<>()\[\]\"'`]+)")
OPEN_LINK = re.compile(r"\[[^\]\n]*$|\]\([^)\s]*$")  # A Markdown link that is not finished yet


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
        line = None
        for word in raw.split(" "):
            if line is None:
                line = word
            elif line.strip() and len(line) + 1 + len(word) > width:
                lines.append(line)
                line = indent + word
            else:
                line += " " + word
        lines.append(line)
    return lines


class Output:
    """
    The terminal layout shared by the three agents (README.md, "Terminal output"):
      - Everything except the model's answer (banner, hints, tool calls and results, timing) is a
        dark gray line.
      - The model's streamed answer is word-wrapped as it arrives, with exactly one blank line
        before and after it.
      - All output fits in the configured width (context/output.json), or the terminal's if narrower.
      - Each response ends with how long it took.
    """

    def __init__(self, out: Console, settings: dict | None = None):
        """
        Args:
            out: The console to print to.
            settings: The layout settings; None reads context/output.json.
        """
        settings = settings or load_output_settings()
        self.out = out
        self.width = int(settings.get("width", 120))
        if out.is_terminal:
            self.width = min(self.width, out.width)
        self.show_time = bool(settings.get("show_time", True))
        self.show_tokens = bool(settings.get("show_tokens", False))
        self.links = bool(settings.get("links", False))  # OSC 8 links (rich adds them only on a terminal)
        self.usage: dict | None = None  # Tokens and model calls of this response, when the server reports them
        self.in_text = False  # A text block is open
        self.pending = ""  # Trailing newlines held back until more text follows
        self.column = 0  # Where the streamed answer's current line ends
        self.word = ""  # The streamed word being collected
        self.spaces = ""  # The spaces before it
        self.started = time.monotonic()

    def start(self) -> None:
        """Start timing a response."""
        self.started = time.monotonic()
        self.usage = None

    def add_usage(self, input_tokens: int, output_tokens: int, requests: int = 1) -> None:
        """
        Count the tokens of model calls made for this response.
        Args:
            input_tokens: Tokens sent to the model (prompt, history, tool results).
            output_tokens: Tokens the model generated.
            requests: How many model calls these tokens cover.
        """
        self.usage = self.usage or {"input": 0, "output": 0, "requests": 0}
        self.usage["input"] += input_tokens
        self.usage["output"] += output_tokens
        self.usage["requests"] += requests

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
            self.out.print(self._render(self._wrap_stream(self.pending + body)), end="", soft_wrap=True)
            self.pending = chunk[len(body):]
        else:
            self.pending += chunk

    def line(self, text: str) -> None:
        """
        Print a whole dark gray line (a tool call or result, the banner), wrapped to the width,
        closing any open text block first.
        Args:
            text: The line; may contain newlines.
        """
        self.end()
        for line in wrap(text, self.width):
            self.out.print(self._render(line), style="bright_black", soft_wrap=True)

    def end(self) -> None:
        """Close the open text block, if any: end its line and add the blank line after it."""
        if self.in_text:
            self.out.print(self._render(self._take_word()), end="", soft_wrap=True)
            self.out.print("\n")
        self.in_text = False
        self.pending = ""
        self.column = 0
        self.word = self.spaces = ""

    def finish(self) -> None:
        """Close the response, and print how long it took since `start` and the tokens it used."""
        self.end()
        parts = [f"Response time: {time.monotonic() - self.started:.1f}s"] if self.show_time else []
        if self.show_tokens:
            if self.usage:
                calls = self.usage["requests"]
                parts.append(f"tokens: {self.usage['input']:,} in, {self.usage['output']:,} out "
                             f"({calls} model call{'s' if calls != 1 else ''})")
            else:
                parts.append("tokens: not reported")
        if parts:
            self.line(" · ".join(parts))

    def _render(self, text: str) -> Text:
        """
        Turn text into a rich Text, with its links as clickable OSC 8 links when links are on.
        Args:
            text: Plain text, possibly with Markdown links or web addresses.
        Returns:
            Text: The text to print; rich writes the link codes only on a terminal.
        """
        if not self.links:
            return Text(text)
        rendered = Text()
        for part, url in link_segments(text):
            rendered.append(part, style=Style(link=url) if url else None)
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


async def ask(agent: Agent, prompt: str, history: list, trace: bool = True, parallel: bool = False) -> list:
    """
    Run one user turn, printing streamed text and tool activity.
    Args:
        agent: The agent to run.
        prompt: The user's message.
        history: The messages of earlier turns.
        trace: Print tool calls and results.
        parallel: Run the tool calls of one model response concurrently instead of one at a time.
    Returns:
        list: The updated message history, including this turn.
    """
    output = Output(console)
    output.start()
    # pydantic-ai reports every call of a model response before their results; hold each call
    # line until its result arrives, so the two print together (as in the other agents).
    pending_calls: dict[str, str] = {}
    # pydantic-ai can run the tool calls from one model response concurrently. The default here is
    # one at a time: MCPAgent's server rejects overlapping calls ("Busy"), and build scripts
    # sharing a workspace shouldn't overlap. The prompt can't change this; only the code can.
    try:
        with agent.parallel_tool_call_execution_mode("parallel" if parallel else "sequential"):
            async with agent.run_stream_events(prompt, message_history=history, usage_limits=LIMITS) as events:
                async for event in events:
                    if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
                        output.text(event.part.content)
                    elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
                        output.text(event.delta.content_delta)
                    elif trace and isinstance(event, FunctionToolCallEvent):
                        arguments = json.dumps(event.part.args_as_dict(), separators=(",", ":"))
                        pending_calls[event.tool_call_id] = f"→ {event.part.tool_name}({arguments})"
                    elif trace and isinstance(event, FunctionToolResultEvent):
                        part = event.part
                        if event.tool_call_id in pending_calls:
                            output.line(pending_calls.pop(event.tool_call_id))
                        if isinstance(part, ToolReturnPart) and part.outcome == "success":
                            output.line(f"← {part.tool_name}: {readable(part.model_response_str())}")
                        else:  # The tool failed, or its arguments were invalid
                            message = part.model_response_str() if isinstance(part, ToolReturnPart) else part.content
                            output.line(f"✗ {part.tool_name}: {readable(str(message))}")
                    elif isinstance(event, AgentRunResultEvent):
                        usage = event.result.usage
                        if usage.input_tokens or usage.output_tokens:
                            output.add_usage(usage.input_tokens, usage.output_tokens, usage.requests)
                        return event.result.all_messages()
    finally:
        for line in pending_calls.values():  # Calls that never got a result (e.g. the run failed)
            output.line(line)
        output.finish()
    return history


def print_history(history: list) -> None:
    """
    Print the raw request/response messages pydantic-ai exchanged with the model.
    Args:
        history: The message history to print.
    """
    for i, message in enumerate(history, 1):
        console.print(f"\n--- MESSAGE {i}: {type(message).__name__} ---", style="bold", markup=False)
        pprint(asdict(message), width=120, sort_dicts=False)


async def chat(agent: Agent, prompt: str | None, trace: bool, show_history: bool, parallel: bool) -> int:
    """
    Run one prompt, or the interactive chat until the user exits.
    Args:
        agent: The agent to run.
        prompt: A single prompt to run and exit; None starts the interactive chat.
        trace: Print tool calls and results.
        show_history: With a single prompt, print the message history afterwards.
        parallel: Run the tool calls of one model response concurrently.
    Returns:
        int: The exit code.
    """
    history = []
    async with agent:  # Opens the MCP connection once for the whole session, when one is used
        if prompt is not None:
            history = await ask(agent, prompt, history, trace, parallel)
            if show_history:
                print_history(history)
            return 0
        Output(console).line("Ask me to use a tool. /history shows messages, /reset clears them, exit quits.")
        session = PromptSession()
        while True:
            try:
                prompt = (await session.prompt_async(ANSI("\x1b[90mYou > \x1b[0m")) or "").strip()
            except (EOFError, KeyboardInterrupt):
                return 0
            if prompt.lower() in {"exit", "quit", "q"}:
                return 0
            if prompt == "/reset":
                history = []
                Output(console).line("History cleared.")
            elif prompt == "/history":
                print_history(history)
            elif prompt:
                try:
                    history = await ask(agent, prompt, history, trace, parallel)
                except Exception as error:
                    console.print(f"\nError: {error}", style="red", markup=False)


def main() -> int:
    """
    Command-line entry point: parse the options, build the agent and run the chat.
    Returns:
        int: The process exit code.
    """
    parser = argparse.ArgumentParser(description="pydantic-ai agent that uses the shared tools.")
    parser.add_argument("--profile", help="Model profile from context/models.json (default: its \"default\").")
    parser.add_argument("--local", action="store_true", help="Shortcut for --profile local.")
    parser.add_argument("--openai", action="store_true", help="Shortcut for --profile openai.")
    parser.add_argument("--model", help="Override the profile's model for this run.")
    parser.add_argument("--base-url", help="Override the profile's OpenAI-compatible base URL for this run.")
    parser.add_argument("--mcp", nargs="?", const=MCP_URL, metavar="URL",
                        help=f"Use tools from an MCP server instead of the local tools folder (default URL: {MCP_URL}).")
    parser.add_argument("--prompt", help="Run one prompt and exit.")
    parser.add_argument("--history", action="store_true", help="With --prompt, print the message history.")
    parser.add_argument("--quiet", action="store_true", help="Hide tool calls and results.")
    parser.add_argument("--parallel", action="store_true",
                        help="Run the tool calls from one model response concurrently (MCPAgent's server rejects this).")
    args = parser.parse_args()
    if sum(map(bool, (args.profile, args.local, args.openai))) > 1:
        parser.error("use only one of --profile, --local and --openai")
    console.print()  # Blank line before anything the agent prints

    try:
        profile = args.profile or ("local" if args.local else "openai" if args.openai else None)
        settings = resolve_model(load_models(), profile, model=args.model, base_url=args.base_url)
        agent = build_agent(build_model(settings), args.mcp)
        tools = f"tools from MCP server {args.mcp}" if args.mcp else f"{len(local_toolset.tools)} tools"
        execution = "parallel" if args.parallel else "sequential"
        Output(console).line(f"{settings['name']} model: {settings['model']} @ {settings['base_url']}, "
                             f"{tools} ({execution})")
        return asyncio.run(chat(agent, args.prompt, trace=not args.quiet,
                                show_history=args.history, parallel=args.parallel))
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        console.print(f"Error: {error}", style="red", markup=False)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
