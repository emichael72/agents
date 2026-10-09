"""
Module: session.py

Description:
    `AgentSession`: the Pydantic Agent in the terminal, the counterpart of MCPAgent
    (`python mcp/agent.py`). It uses the same model profiles, instructions and tools
    (agents/tools) as MCPAgent; the agent loop (call the model, run the requested tools, send the
    results back, repeat until it answers) is pydantic-ai's. The session builds the `Agent`, runs
    one turn at a time while rendering pydantic-ai's events (streamed text, tool calls and
    results) through `Output`, and provides the interactive chat.
"""
import json
from dataclasses import asdict
from pprint import pprint
from typing import Any, Optional

import pydantic_ai
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import ANSI
from pydantic_ai import Agent, AgentRetries, AgentRunResultEvent, UnexpectedModelBehavior, capture_run_messages
from pydantic_ai.messages import (FunctionToolCallEvent, FunctionToolResultEvent, ModelMessage, ModelResponse,
                                  PartDeltaEvent, PartStartEvent, TextPart, TextPartDelta, ThinkingPart,
                                  ThinkingPartDelta, ToolReturnPart)
from pydantic_ai.models import Model
from pydantic_ai.toolsets import FunctionToolset
from rich.console import Console

from pydantic_agent.context import AgentContext
from pydantic_agent.output import Output
from pydantic_agent.profiles import ModelProfiles
from pydantic_agent.guard import RepeatGuard
from pydantic_agent.toolset import LocalTools


class AgentSession:
    """
    One run of the Pydantic Agent in the terminal: one prompt, or an interactive chat.
    """

    EXIT_WORDS = {"exit", "quit", "q"}
    PROMPT = ANSI("\x1b[90mYou > \x1b[0m")

    def __init__(self, trace: bool = True, parallel: Optional[bool] = None, context: Optional[AgentContext] = None,
                 console: Optional[Console] = None) -> None:
        """
        Args:
            trace: Debug mode: print the banner, tool calls and results as gray lines; otherwise a
                spinner shows them.
            parallel: Run the tool calls of one model response concurrently instead of one at a time;
                None uses parallel_tool_calls in pydantic/instructions.json (true when unset).
            context: The shared context files; None uses agents/context.
            console: Where to print; None prints to the terminal.
        """
        pydantic_ai.BANNER_ENABLED = False  # This program owns its output: no first-run banner
        self.trace = trace
        self.context = context or AgentContext()
        self.parallel = self.context.own().get("parallel_tool_calls", True) if parallel is None else parallel

        self.console = console or Console(highlight=False, soft_wrap=True)  # Never re-wrap lines
        self.settings = self.context.agent_settings()
        self.tool_retries = int(self.settings.get("tool_retries", 3))  # Corrections of an invalid call
        self.limits = self.context.usage_limits(self.settings)
        self.output_settings = self.context.output_settings()
        self.tools: Optional[FunctionToolset] = None  # The local tools, once build_agent loads them
        self.model_settings: dict[str, Any] = {}  # The resolved model profile, once run picks it

    def build_agent(self, model: Model) -> Agent:
        """
        Build the agent with the shared instructions and the local tools folder's tools.
        Args:
            model: The model to drive the agent.
        Returns:
            Agent: The pydantic-ai agent.
        """
        tools = LocalTools.load()
        self.tools = tools
        LocalTools.guard = RepeatGuard(int(self.settings.get("max_repeated_calls", 0)))
        return Agent(model, instructions=self.context.system_prompt(self.settings), toolsets=[tools],
                     retries=AgentRetries(tools=self.tool_retries))

    async def run(self, profile: Optional[str] = None, model: Optional[str] = None, base_url: Optional[str] = None,
                  prompt: Optional[str] = None, show_history: bool = False) -> int:
        """
        Build the model and the agent, then answer one prompt or chat until the user exits.
        Args:
            profile: Model profile name; None uses the models file's default.
            model: Overrides the profile's model.
            base_url: Overrides the profile's base URL.
            prompt: A single prompt to answer before exiting; None starts the interactive chat.
            show_history: With a single prompt, print the message history after the response.
        Returns:
            int: The exit code: 0, or 1 when the agent could not start or failed.
        """
        if self.trace:
            self.console.print()  # Blank line before the banner (hidden without debug)
        try:
            settings = ModelProfiles.load().resolve(profile, model=model, base_url=base_url)
            self.model_settings = settings
            agent = self.build_agent(ModelProfiles.build_model(settings))
            tools = f"{len(self.tools.tools) if self.tools else 0} tools"
            execution = "parallel" if self.parallel else "sequential"
            self._output().line(f"{settings['name']} model: {settings['model']} @ {settings['base_url']}, "
                                f"{tools} ({execution})")
            return await self.chat(agent, prompt, show_history)
        except Exception as error:
            self.console.print(f"Error: {error}", style="red", markup=False)
            return 1

    async def ask(self, agent: Agent, prompt: str, history: list[ModelMessage]) -> list[ModelMessage]:
        """
        Run one user turn, printing streamed text and tool activity. A model that keeps making invalid
        tool calls (past tool_retries) ends the turn, not the conversation: its messages so far are
        kept, without the last response, whose calls never got results.
        Args:
            agent: The agent to run.
            prompt: The user's message.
            history: The messages of earlier turns.
        Returns:
            list[ModelMessage]: The updated message history, including this turn.
        """
        output = self._output()
        output.start()
        LocalTools.guard.reset()  # Only this turn's calls count as repeats
        # pydantic-ai reports every call of a model response before their results; hold each call
        # line until its result arrives, so the two print together (as in the other agents).
        pending_calls: dict[str, str] = {}
        # pydantic-ai can run the tool calls from one model response concurrently; parallel_tool_calls
        # in pydantic/instructions.json turns it on, and the instructions there tell the model to put
        # only independent calls in one response.
        failure: Optional[UnexpectedModelBehavior] = None
        kept: list[ModelMessage] = history
        cut_off = False
        try:
            with capture_run_messages() as run_messages, \
                    agent.parallel_tool_call_execution_mode("parallel" if self.parallel else "sequential"):
                async with agent.run_stream_events(prompt, message_history=history, usage_limits=self.limits) as events:
                    async for event in events:
                        if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
                            output.text(event.part.content)
                        elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
                            output.text(event.delta.content_delta)
                        elif isinstance(event, (PartStartEvent, PartDeltaEvent)) and \
                                isinstance(getattr(event, "part", None) or getattr(event, "delta", None),
                                           (ThinkingPart, ThinkingPartDelta)):
                            output.thinking()  # A reasoning model thinks: the spinner shows for how long
                        elif isinstance(event, FunctionToolCallEvent):
                            arguments = json.dumps(event.part.args_as_dict(), separators=(",", ":"))
                            pending_calls[event.tool_call_id] = f"→ {event.part.tool_name}({arguments})"
                            if not self.trace:  # Name the tool on the spinner now, while it runs
                                output.line(pending_calls[event.tool_call_id])
                            else:  # The call prints with its result; meanwhile the spinner names it
                                output.spin(f"Running {event.part.tool_name}…")
                        elif isinstance(event, FunctionToolResultEvent):
                            part = event.part
                            if event.tool_call_id in pending_calls:
                                output.line(pending_calls.pop(event.tool_call_id))
                            if isinstance(part, ToolReturnPart) and part.outcome == "success":
                                output.line(f"← {part.tool_name}: {Output.readable(part.model_response_str())}")
                            else:  # The tool failed, or its arguments were invalid
                                message = part.model_response_str() if isinstance(part, ToolReturnPart) else part.content
                                output.line(f"✗ {part.tool_name}: {Output.readable(str(message))}")
                        elif isinstance(event, AgentRunResultEvent):
                            usage = event.result.usage
                            if usage.input_tokens or usage.output_tokens:
                                output.add_usage(usage.input_tokens, usage.output_tokens, usage.requests)
                            messages = event.result.all_messages()
                            last = next((m for m in reversed(messages) if isinstance(m, ModelResponse)), None)
                            cut_off = last is not None and last.finish_reason == "length"  # The answer stops short
                            kept = messages
        except UnexpectedModelBehavior as error:
            failure = error
            kept = list(run_messages) or history
            while kept and isinstance(kept[-1], ModelResponse):
                kept.pop()
        finally:
            for line in pending_calls.values():  # Calls that never got a result (e.g. the run failed)
                output.line(line)
            output.finish()
        if cut_off:
            self.console.print(ModelProfiles.out_of_tokens(self.model_settings), style="red", markup=False)
        if failure is not None and "token limit" in failure.message:  # The reply hit max_tokens
            self.console.print(ModelProfiles.out_of_tokens(self.model_settings), style="red", markup=False)
        elif failure is not None:  # pydantic-ai's message, without its advice on the retry setting
            reason = failure.message.split(". ")[0].rstrip(".")
            self.console.print(f"The turn ended: {reason}. Its tool results above are kept; ask again to go on "
                               f"(tool_retries in context/agent.json sets how many corrections a tool gets).",
                               style="red", markup=False)
        return kept

    def print_history(self, history: list[ModelMessage]) -> None:
        """
        Print the raw request/response messages pydantic-ai exchanged with the model.
        Args:
            history: The message history to print.
        """
        for i, message in enumerate(history, 1):
            self.console.print(f"\n--- MESSAGE {i}: {type(message).__name__} ---", style="bold", markup=False)
            pprint(asdict(message), width=120, sort_dicts=False)

    async def chat(self, agent: Agent, prompt: Optional[str], show_history: bool = False) -> int:
        """
        Run one prompt, or the interactive chat until the user exits.
        Args:
            agent: The agent to run.
            prompt: A single prompt to run and exit; None starts the interactive chat.
            show_history: With a single prompt, print the message history after the response.
        Returns:
            int: The exit code.
        """
        history: list[ModelMessage] = []
        async with agent:
            if prompt is not None:
                history = await self.ask(agent, prompt, history)
                if show_history:
                    self.print_history(history)
                return 0
            self._output().line("Ask me to use a tool. /history shows messages, /reset clears them, exit quits.")
            on_exit = self.context.instructions("on_exit")
            session: PromptSession[Any] = PromptSession()
            while True:
                try:
                    prompt = (await session.prompt_async(self.PROMPT) or "").strip()
                except (EOFError, KeyboardInterrupt):
                    return 0
                if prompt.lower() in self.EXIT_WORDS:
                    # One last turn to save what is worth remembering (save_on_exit); Ctrl+C skips it
                    if self.settings.get("save_on_exit") and on_exit and AgentContext.worth_saving(history):
                        self._output().line("Before exiting: saving anything worth remembering (Ctrl+C skips).")
                        try:
                            await self.ask(agent, on_exit, history)
                        except Exception as error:
                            self.console.print(f"\nError: {error}", style="red", markup=False)
                    return 0
                if prompt == "/reset":
                    history = []
                    self._output(debug=True).note("History cleared.")
                elif prompt == "/history":
                    self.print_history(history)
                elif prompt:
                    try:
                        history = await self.ask(agent, prompt, history)
                    except Exception as error:
                        self.console.print(f"\nError: {error}", style="red", markup=False)

    def _output(self, debug: Optional[bool] = None) -> Output:
        """
        A new terminal layout for one response or message.
        Args:
            debug: Print gray lines rather than the spinner; None follows the session's trace.
        Returns:
            Output: The layout, with the shared settings.
        """
        return Output(self.console, self.output_settings, debug=self.trace if debug is None else debug)
