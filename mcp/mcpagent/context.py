"""
Module: context.py

Description:
    `AgentContext`: what the three agents share besides tools, read from the files the client
    config names (all relative to the repository): the model's instructions and identity lines
    (context/instructions.json), this agent's name and own instructions (mcp/instructions.json),
    the agent loop settings (context/agent.json), the terminal
    layout (context/output.json), and the memory index those settings point at.
"""
import json
import os
import socket
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from mcpagent.config import MCPAgentConfig


class AgentContext:
    """
    The shared context the client config names.
    """

    def __init__(self, client_config: dict[str, Any]) -> None:
        """
        Args:
            client_config: The client config: "instructions_file", "agent_instructions_file",
                "agent_file", "output_file".
        """
        self.config = client_config

    def instructions(self, key: str = "instructions") -> str:
        """
        Read lines from the instructions file the client config names.
        Args:
            key: Which lines to read: "instructions", "identity", "whereabouts", "on_exit" (the prompt sent
                before exit) or "out_of_tokens" (sent to ask again after a reply ran out of tokens).
        Returns:
            str: The lines joined with newlines, or "" if none are configured.
        """
        data = self._read("instructions_file")
        return "\n".join(data.get(key, [])) if data is not None else ""

    def own(self) -> dict[str, Any]:
        """
        Read this agent's own file, which the client config names: its "name", and its own
        "instructions" lines, added to the shared ones.
        Returns:
            dict[str, Any]: The file's contents, or {} if none is configured.
        """
        return self._read("agent_instructions_file") or {}

    def identity(self, name: Optional[str]) -> str:
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
        Read the agent loop settings from the file the client config names.
        Returns:
            dict[str, Any]: "max_tool_calls", "memory_index", "save_on_exit", "skills_dir", "out_of_tokens_retries"
                and "thinking_dir", or {}
                (the defaults) if none is configured.
        """
        return self._read("agent_file") or {}

    def output_settings(self) -> dict[str, Any]:
        """
        Read the terminal layout settings from the file the client config names.
        Returns:
            dict[str, Any]: "width", "show_time" and the rest, or {} (the defaults) if none is configured.
        """
        return self._read("output_file") or {}

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
        index: Optional[str] = settings.get("memory_index")  # Relative to the repository
        skills: Optional[str] = settings.get("skills_dir")
        own = self.own()
        own_lines = "\n".join(own.get("instructions", []))
        return (self.identity(own.get("name")) + self.instructions() + self.whereabouts()
                + ("\n\n" + own_lines if own_lines else "")
                + self.skills_text(MCPAgentConfig.repo_path(skills) if skills else None)
                + self.memory_text(MCPAgentConfig.repo_path(index) if index else None))

    # Keep the skills listing consistent across the independent agent implementations.
    # noinspection DuplicatedCode
    @staticmethod
    def skills_text(folder: Optional[Path]) -> str:
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

    # Keep the memory instructions consistent across the independent agent implementations.
    # noinspection DuplicatedCode
    @staticmethod
    def memory_text(index: Optional[Path]) -> str:
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
    def worth_saving(history: list[dict[str, Any]]) -> bool:
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

    def _read(self, key: str) -> Optional[dict[str, Any]]:
        """
        Read the JSON file a client config key names.
        Args:
            key: The config key, e.g. "agent_file".
        Returns:
            Optional[dict[str, Any]]: The file's contents, or None when the key is not set.
        """
        name: Optional[str] = self.config.get(key)
        if not name:
            return None
        return json.loads(MCPAgentConfig.repo_path(name).read_text(encoding="utf-8"))
