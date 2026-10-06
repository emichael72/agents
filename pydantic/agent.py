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
from dataclasses import asdict
from pathlib import Path
from pprint import pprint

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")  # Skip pydantic-ai's observability banner

import httpx2
from prompt_toolkit import PromptSession
from pydantic_ai import Agent, AgentRunResultEvent, UsageLimits
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.messages import (FunctionToolCallEvent, FunctionToolResultEvent, PartDeltaEvent,
                                  PartStartEvent, TextPart, TextPartDelta, ToolReturnPart)
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from rich.console import Console

from toolset import toolset as local_toolset

MCP_URL = "http://127.0.0.1:6275/"  # MCPAgent's server (python -m mcpagent.server)

# Instructions (system prompt) and model profiles, shared by all three agents
CONTEXT_DIR = Path(__file__).resolve().parent.parent / "context"
INSTRUCTIONS_FILE = CONTEXT_DIR / "instructions.json"
MODELS_FILE = CONTEXT_DIR / "models.json"

# Same cap as MCPAgent's max_tool_calls.
LIMITS = UsageLimits(tool_calls_limit=8)

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
    return Agent(model, instructions=load_instructions(), toolsets=[toolset])


def load_models(path: Path = MODELS_FILE) -> dict:
    """
    Read the shared model profiles file.
    Args:
        path: The JSON file (default: agents/context/models.json).
    Returns:
        dict: "default" (a profile name) and "profiles" (by name).
    """
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_model(models: dict, profile: str | None = None,
                  model: str | None = None, base_url: str | None = None) -> dict:
    """
    Pick a model profile and apply overrides, the same way as MCPAgent.
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
    return {
        "name": settings.get("name", name),
        "base_url": base_url or os.environ.get(settings.get("base_url_env", "")) or settings["base_url"],
        "model": model or os.environ.get(settings.get("model_env", "")) or settings["model"],
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
            self.out.print(self.pending + body, end="", markup=False, soft_wrap=True)
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
                        return event.result.all_messages()
    finally:
        for line in pending_calls.values():  # Calls that never got a result (e.g. the run failed)
            output.line(line)
        output.end()
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
        console.print("Ask me to use a tool. /history shows messages, /reset clears them, exit quits.")
        session = PromptSession()
        while True:
            try:
                prompt = (await session.prompt_async("You > ") or "").strip()
            except (EOFError, KeyboardInterrupt):
                return 0
            if prompt.lower() in {"exit", "quit", "q"}:
                return 0
            if prompt == "/reset":
                history = []
                console.print("History cleared.")
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
        tools = f"MCP server {args.mcp}" if args.mcp else ", ".join(local_toolset.tools)
        execution = "parallel" if args.parallel else "sequential"
        console.print(f"{settings['name']} model: {settings['model']} @ {settings['base_url']}\n"
                      f"Tools: {tools} ({execution})", markup=False)
        return asyncio.run(chat(agent, args.prompt, trace=not args.quiet,
                                show_history=args.history, parallel=args.parallel))
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        console.print(f"Error: {error}", style="red", markup=False)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
