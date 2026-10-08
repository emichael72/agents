"""
Module: __init__.py

Description:
    Package root of mcpagent: an agent that lets a model use the shared tools through MCP. It
    starts its own MCP server (`service`, which runs the tools) as a child process, talks to it
    with its MCP client (`client`, `connection`), and stops it when it exits.

    It holds `__version__` and the locations the package's modules share: the repository root,
    and the configuration and its schema. The classes live in their modules (agent, client,
    connection, context, guard, output, profiles, service, session, types); import them from
    there, e.g. `from mcpagent.session import AgentSession`.
"""
from pathlib import Path

__version__ = "1.1.0"

# The repository root: the nearest folder above holding pyproject.toml
REPO_ROOT = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").is_file())
JSONS_DIR = Path(__file__).resolve().parent / "jsons"  # The configuration and its schema
SCHEMA_DIR = JSONS_DIR / "schemas"
DEFAULT_CONFIG = JSONS_DIR / "mcpagent.json"  # The agent's, and the server's it starts
SCHEMA_FILE = SCHEMA_DIR / "mcpagent.schema.json"
