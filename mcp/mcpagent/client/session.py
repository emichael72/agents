"""
Module: session.py

Description:
    `AgentSession`: the agent's terminal front end. It reads the client config and the shared
    context, builds and connects the `MCPAgent`, then answers one prompt or chats until the user
    exits, rendering through `Output`.
"""
import asyncio
import json
from pathlib import Path
from typing import Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import ANSI
from rich.console import Console

from mcpagent.client.agent import MCPAgent
from mcpagent.client.client import MCPClient
from mcpagent.client.context import AgentContext
from mcpagent.client.output import Output
from mcpagent.client.profiles import ModelProfiles


class AgentSession:
    """
    One run of the agent in the terminal: one prompt, or an interactive chat.
    """

    EXIT_WORDS = {"exit", "quit", "q"}
    PROMPT = ANSI("\x1b[90mYou > \x1b[0m")

    def __init__(self, config_file: str | Path, profile: Optional[str] = None, model: Optional[str] = None,
                 base_url: Optional[str] = None, context: str = "", trace: bool = True) -> None:
        """
        Args:
            config_file: The MCPAgent config; its client section names the MCP servers and the
                shared context.
            profile: Model profile name; None uses the models file's default.
            model: Overrides the profile's model.
            base_url: Overrides the profile's base URL.
            context: Extra instructions for the assistant.
            trace: Debug mode: print the gray lines (banner, hints, tool calls and results)
                instead of a spinner.
        """
        self.config_file = config_file
        self.profile = profile
        self.model = model
        self.base_url = base_url
        self.context = context
        self.trace = trace
        self.console = Console(highlight=False, soft_wrap=True)  # Never re-wrap lines; Output wraps
        self.output = Output(self.console, debug=trace)  # Replaced by the configured layout in _start
        self.agent: Optional[MCPAgent] = None
        self.shared: Optional[AgentContext] = None
        self.settings: dict = {}  # The agent loop settings (context/agent.json)

    async def run(self, prompt: Optional[str] = None) -> int:
        """
        Answer one prompt, or chat interactively until the user exits.
        Args:
            prompt: A single prompt to answer before exiting; None starts an interactive session.
        Returns:
            int: The exit code: 0, or 1 when the agent could not start or failed.
        """
        if self.trace:
            self.console.print()  # Blank line before the banner (hidden without debug)
        try:
            await self._start()
            if prompt is not None:
                await self._answer(prompt)
                return 0
            return await self._chat()
        except (KeyboardInterrupt, asyncio.CancelledError):
            return 0
        except Exception as error:
            self._error(error)
            return 1
        finally:
            await self.close()

    async def close(self) -> None:
        """Close the agent's MCP connections and its model client, if it was created."""
        if self.agent is not None:
            await self.agent.close()

    async def _start(self) -> None:
        """
        Read the config and the shared context, build the agent and connect it to its MCP servers.
        """
        mcp_client = MCPClient(self.config_file)
        shared = AgentContext(mcp_client.config_data)
        self.shared = shared
        self.output = Output(self.console, shared.output_settings(), debug=self.trace)
        model = ModelProfiles.load(mcp_client.config_data).resolve(self.profile, model=self.model,
                                                                    base_url=self.base_url)
        self.settings = shared.agent_settings()
        agent = MCPAgent(mcp_client, base_url=model["base_url"], model=model["model"],
                         api_key=model["api_key"], provider=model["name"], timeout=model["timeout"],
                         error_hints=model["error_hints"],
                         instructions=shared.system_prompt(self.settings),
                         context=self.context, trace=self.output.line,  # Debug lines, else the spinner
                         max_tool_calls=int(self.settings.get("max_tool_calls", 8)),
                         max_repeated_calls=int(self.settings.get("max_repeated_calls", 0)))
        self.agent = agent
        await agent.connect()
        servers = len({server for server, _, _ in agent.routes.values()})
        tools = f"{len(agent.routes)} tools" + (f" from {servers} servers" if servers > 1 else "")
        self.output.line(f"{agent.provider} model: {agent.model} @ {agent.base_url}, {tools} (sequential)")

    async def _answer(self, prompt: str) -> None:
        """
        Ask one prompt, printing tool activity and the streamed answer.
        Args:
            prompt: The user's message.
        """
        assert self.agent is not None
        self.output.start()
        try:
            answer = await self.agent.ask(prompt, on_text=self.output.text)
            if not self.output.in_text:
                self.output.text(answer)  # Nothing was streamed (e.g. a non-streaming reply)
        finally:
            for input_tokens, output_tokens in getattr(self.agent, "usage", []):
                self.output.add_usage(input_tokens, output_tokens)
            self.output.finish()

    async def _chat(self) -> int:
        """
        The interactive session: prompts, the /history and /reset commands, and exit.
        Returns:
            int: The exit code, 0.
        """
        assert self.agent is not None and self.shared is not None
        self.output.line("Ask me to use a tool. /history shows messages, /reset clears them, exit quits.")
        on_exit = self.shared.instructions("on_exit")
        session = PromptSession()
        while True:
            try:
                prompt = (await session.prompt_async(self.PROMPT)).strip()
            except (EOFError, KeyboardInterrupt):
                return 0
            if prompt.lower() in self.EXIT_WORDS:
                # One last turn to save what is worth remembering (save_on_exit); Ctrl+C skips it
                if self.settings.get("save_on_exit") and on_exit and AgentContext.worth_saving(self.agent.history):
                    self.output.line("Before exiting: saving anything worth remembering (Ctrl+C skips).")
                    try:
                        await self._answer(on_exit)
                    except Exception as error:
                        self._error(error)
                return 0
            if prompt == "/reset":
                self.agent.history = []
                self.output.note("History cleared.")
            elif prompt == "/history":
                self.console.print(json.dumps(self.agent.history, indent=2, ensure_ascii=False), markup=False)
            elif prompt:
                try:
                    await self._answer(prompt)
                except Exception as error:
                    self._error(error)

    def _error(self, error: Exception) -> None:
        """
        Print an error in red.
        Args:
            error: The error.
        """
        self.console.print(f"Error: {error}", style="red", markup=False)
