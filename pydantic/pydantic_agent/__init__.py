"""
Module: __init__.py

Description:
    The Pydantic Agent's package root. It is named pydantic_agent to keep it separate from the
    installed pydantic dependency.

    It holds the locations the package's modules share: the repository root and the shared
    context files. The classes live in their modules (context, output, profiles, session,
    toolset); import them from there, e.g. `from pydantic_agent.session import AgentSession`.
"""
import json
from pathlib import Path

# The repository root: the nearest folder above holding pyproject.toml
REPO_ROOT = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").is_file())
CONTEXT_DIR = REPO_ROOT / "context"  # Shared by all three agents
INSTRUCTIONS_FILE = CONTEXT_DIR / "instructions.json"
OWN_FILE = REPO_ROOT / "pydantic" / "instructions.json"  # This agent's name, settings and own instructions
AGENT_FILE = CONTEXT_DIR / "agent.json"  # Agent loop settings
OUTPUT_FILE = CONTEXT_DIR / "output.json"  # Terminal layout
MODELS_FILE = CONTEXT_DIR / "models.json"  # Model profiles
TOOLS_DIR = REPO_ROOT / "tools"  # One <tool>/tool.json per tool

# Lets tools such as pr and pr_gate say which agent ran them ("display_name" in OWN_FILE)
AGENT_NAME = json.loads(OWN_FILE.read_text(encoding="utf-8"))["display_name"]
