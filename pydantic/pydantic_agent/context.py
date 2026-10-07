"""
Module: context.py

Description:
    `AgentContext`: what the three agents share besides tools, read from agents/context: the
    model's instructions and identity lines (instructions.json), the agent loop settings
    (agent.json), the terminal layout (output.json), and the memory index those settings point at.
"""
import json
from pathlib import Path
from typing import Any

from pydantic_ai import UsageLimits
from pydantic_ai.messages import ModelMessage, ToolCallPart, UserPromptPart

from pydantic_agent import AGENT_FILE, INSTRUCTIONS_FILE, OUTPUT_FILE, REPO_ROOT


class AgentContext:
    """
    The shared context files.
    """

    NAME_KEY = "pydantic"  # This agent's key in context/agent.json's "names"

    def __init__(self, instructions_file: Path = INSTRUCTIONS_FILE, agent_file: Path = AGENT_FILE,
                 output_file: Path = OUTPUT_FILE) -> None:
        """
        Args:
            instructions_file: The instructions (default: agents/context/instructions.json).
            agent_file: The agent loop settings (default: agents/context/agent.json).
            output_file: The terminal layout (default: agents/context/output.json).
        """
        self.instructions_file = instructions_file
        self.agent_file = agent_file
        self.output_file = output_file

    def instructions(self, key: str = "instructions") -> str:
        """
        Read lines from the shared instructions file.
        Args:
            key: Which lines to read: "instructions", "identity", or "on_exit" (the prompt sent
                before exit).
        Returns:
            str: Those lines, joined with newlines ("" if the file has none).
        """
        return "\n".join(json.loads(self.instructions_file.read_text(encoding="utf-8")).get(key, []))

    def identity(self, name: str | None) -> str:
        """
        The identity lines that open the instructions, naming the agent.
        Args:
            name: The agent's name (context/agent.json's names); None when not configured.
        Returns:
            str: The lines with {name} filled in, and a blank line after them; "" without a name.
        """
        text = self.instructions("identity")
        return text.replace("{name}", name) + "\n\n" if name and text else ""

    def agent_settings(self) -> dict[str, Any]:
        """
        Read the shared agent loop settings.
        Returns:
            dict[str, Any]: "max_tool_calls", "memory_index", "save_on_exit" and "names".
        """
        return json.loads(self.agent_file.read_text(encoding="utf-8"))

    def output_settings(self) -> dict[str, Any]:
        """
        Read the shared terminal layout settings.
        Returns:
            dict[str, Any]: "width" (wrap column), "show_time" and the rest.
        """
        return json.loads(self.output_file.read_text(encoding="utf-8"))

    def system_prompt(self, settings: dict[str, Any]) -> str:
        """
        The model's full instructions: this agent's identity, the shared instructions, and the
        topics in its memory.
        Args:
            settings: The agent loop settings, from `agent_settings`.
        Returns:
            str: The instructions.
        """
        index = settings.get("memory_index")  # Relative to the repository
        return (self.identity(settings.get("names", {}).get(self.NAME_KEY)) + self.instructions()
                + self.memory_text(REPO_ROOT / index if index else None))

    @staticmethod
    def usage_limits(settings: dict[str, Any]) -> UsageLimits:
        """
        The run's limits. Tool calls per prompt are shared with the other agents (max_tool_calls;
        0 means no limit). pydantic-ai also caps model requests (50 by default): allow one per tool
        call, plus retries and the final answer, or none at all when tool calls are unlimited.
        Args:
            settings: The agent loop settings, from `agent_settings`.
        Returns:
            UsageLimits: The limits for one prompt.
        """
        max_tool_calls = int(settings.get("max_tool_calls", 8))
        if not max_tool_calls:
            return UsageLimits(tool_calls_limit=None, request_limit=None)
        return UsageLimits(tool_calls_limit=max_tool_calls, request_limit=2 * max_tool_calls + 1)

    # Keep shared agent behavior consistent in each project's own implementation.
    # noinspection DuplicatedCode
    @staticmethod
    def memory_text(index: Path | None) -> str:
        """
        The agents' memory, to append to their instructions: the topics in the memory index (kept
        by the memory tool), so the model knows what it remembers without having to look.
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

    @staticmethod
    def worth_saving(history: list[ModelMessage]) -> bool:
        """
        Whether a session may hold something to remember: a tool call, or more than one exchange.
        Args:
            history: The session's pydantic-ai messages.
        Returns:
            bool: True when the model should be asked to save before exit.
        """
        parts = [part for message in history for part in message.parts]
        calls = sum(isinstance(part, ToolCallPart) for part in parts)
        prompts = sum(isinstance(part, UserPromptPart) for part in parts)
        return calls > 0 or prompts > 1
