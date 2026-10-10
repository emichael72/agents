"""
Module: context.py

Description:
    `AgentContext`: what the three agents share besides tools, read from agents/context: the
    model's instructions and identity lines (instructions.json), this agent's name, settings and
    own instructions (pydantic/instructions.json), the agent loop settings (agent.json), the terminal layout (output.json), and the memory index those settings point at.
"""
import json
import os
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic_ai import UsageLimits
from pydantic_ai.messages import ModelMessage, ToolCallPart, UserPromptPart

from pydantic_agent import AGENT_FILE, INSTRUCTIONS_FILE, OUTPUT_FILE, OWN_FILE, REPO_ROOT


class AgentContext:
    """
    The shared context files.
    """

    def __init__(self, instructions_file: Path = INSTRUCTIONS_FILE, agent_file: Path = AGENT_FILE,
                 output_file: Path = OUTPUT_FILE, own_file: Path = OWN_FILE) -> None:
        """
        Args:
            instructions_file: The instructions (default: agents/context/instructions.json).
            own_file: This agent's name, settings and own instructions (default:
                agents/pydantic/instructions.json).
            agent_file: The agent loop settings (default: agents/context/agent.json).
            output_file: The terminal layout (default: agents/context/output.json).
        """
        self.instructions_file = instructions_file
        self.agent_file = agent_file
        self.output_file = output_file
        self.own_file = own_file

    def instructions(self, key: str = "instructions") -> str:
        """
        Read lines from the shared instructions file.
        Args:
            key: Which lines to read: "instructions", "identity", "whereabouts", "on_exit" (the prompt sent
                before exit) or "out_of_tokens" (sent to ask again after a reply ran out of tokens).
        Returns:
            str: Those lines, joined with newlines ("" if the file has none).
        """
        return "\n".join(json.loads(self.instructions_file.read_text(encoding="utf-8")).get(key, []))

    def own(self) -> dict[str, Any]:
        """
        Read this agent's own file: its "name", "parallel_tool_calls", and its own "instructions"
        lines, added to the shared ones.
        Returns:
            dict[str, Any]: The file's contents.
        """
        return json.loads(self.own_file.read_text(encoding="utf-8"))

    def identity(self, name: str | None) -> str:
        """
        The identity lines that open the instructions, naming the agent.
        Args:
            name: The agent's name (its own file's name); None when not configured.
        Returns:
            str: The lines with {name} filled in, and a blank line after them; "" without a name.
        """
        text = self.instructions("identity")
        return text.replace("{name}", name) + "\n\n" if name and text else ""

    def agent_settings(self) -> dict[str, Any]:
        """
        Read the shared agent loop settings.
        Returns:
            dict[str, Any]: "max_tool_calls", "memory_index", "save_on_exit", "skills_dir", "out_of_tokens_retries"
                and "thinking_dir".
        """
        return json.loads(self.agent_file.read_text(encoding="utf-8"))

    def output_settings(self) -> dict[str, Any]:
        """
        Read the shared terminal layout settings.
        Returns:
            dict[str, Any]: "width" (wrap column), "show_time" and the rest.
        """
        return json.loads(self.output_file.read_text(encoding="utf-8"))

    def whereabouts(self) -> str:
        """
        Where the model and the tools run, from the whereabouts template: the model and its server's
        machine come from the environment the agent shares with its tools (AGENT_MODEL, AGENT_MODEL_SERVER).
        Returns:
            str: The paragraph, starting with a blank line; "" before a model is chosen, or without a template.
        """
        template, model = self.instructions("whereabouts"), os.environ.get("AGENT_MODEL")
        if not template or not model:
            return ""
        host = urlparse(os.environ.get("AGENT_MODEL_SERVER", "")).hostname or "unknown"
        tools_host = socket.gethostname().split(".")[0]
        model_host = tools_host if host in ("localhost", "127.0.0.1") else host
        return "\n\n" + template.format(model=model, model_host=model_host, tools_host=tools_host)

    def system_prompt(self, settings: dict[str, Any]) -> str:
        """
        The model's full instructions: this agent's identity, the shared instructions, where the model and
        the tools run, its own instructions, its skills, and the topics in its memory.
        Args:
            settings: The agent loop settings, from `agent_settings`.
        Returns:
            str: The instructions.
        """
        index = settings.get("memory_index")  # Relative to the repository
        skills = settings.get("skills_dir")
        own = self.own()
        own_lines = "\n".join(own.get("instructions", []))
        return (self.identity(own.get("name")) + self.instructions() + self.whereabouts()
                + ("\n\n" + own_lines if own_lines else "")
                + self.skills_text(REPO_ROOT / skills if skills else None)
                + self.memory_text(REPO_ROOT / index if index else None))

    # Keep the skills listing consistent across the independent agent implementations.
    # noinspection DuplicatedCode
    @staticmethod
    def skills_text(folder: Path | None) -> str:
        """
        The agents' skills, to append to their instructions: each skill's name and description, from
        the header of its <name>/SKILL.md, so the model knows when to read one with the skill tool.
        Args:
            folder: The skills folder (context/agent.json's skills_dir); None when not configured.
        Returns:
            str: A paragraph listing the skills; "" without a folder or skills.
        """
        if folder is None or not folder.is_dir():
            return ""
        lines = []
        for file in sorted(folder.glob("*/SKILL.md")):
            header = file.read_text(encoding="utf-8").split("\n---", 1)[0]  # The header ends at its second ---
            description = next((line.partition(":")[2].strip() for line in header.splitlines()
                                if line.startswith("description:")), "")
            lines.append(f"- {file.parent.name}: {description}")
        if not lines:
            return ""
        return ("\n\nYour skills (before a task that matches one, read it with the skill tool and follow it):\n"
                + "\n".join(lines))

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
