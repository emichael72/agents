"""
Module: __init__.py

Description:
    The Pydantic Agent's package root. It is named pydantic_agent to keep it separate from the
    installed pydantic dependency.

    It holds the locations the package's modules share: the repository root and the shared
    context files. The classes live in their modules (context, output, profiles, session,
    toolset); import them from there, e.g. `from pydantic_agent.session import AgentSession`.
"""
from pathlib import Path

# The repository root: the nearest folder above holding pyproject.toml
REPO_ROOT = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").is_file())
CONTEXT_DIR = REPO_ROOT / "context"  # Shared by all three agents
INSTRUCTIONS_FILE = CONTEXT_DIR / "instructions.json"
AGENT_FILE = CONTEXT_DIR / "agent.json"  # Agent loop settings
OUTPUT_FILE = CONTEXT_DIR / "output.json"  # Terminal layout
MODELS_FILE = CONTEXT_DIR / "models.json"  # Model profiles
TOOLS_DIR = REPO_ROOT / "tools"  # One <tool>/tool.json per tool
