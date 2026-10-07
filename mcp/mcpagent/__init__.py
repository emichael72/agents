"""
Module: __init__.py

Description:
    Package root of mcpagent: an MCP server that exposes scripts as tools, and an MCP client
    with an agent that lets a model use them.

    It holds `__version__` and the locations the package's modules share: the repository root,
    and the configuration and its schema. The classes live in their modules; import them from
    there, e.g. `from mcpagent.client.client import MCPClient` or
    `from mcpagent.server.service import MCPService`.
"""
from pathlib import Path

__version__ = "1.0.2"

# The repository root: the nearest folder above holding pyproject.toml
REPO_ROOT = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").is_file())
JSONS_DIR = Path(__file__).resolve().parent / "jsons"  # The configuration and its schema
SCHEMA_DIR = JSONS_DIR / "schemas"
DEFAULT_CONFIG = JSONS_DIR / "mcpagent.json"  # Shared by the client and the server
SCHEMA_FILE = SCHEMA_DIR / "mcpagent.schema.json"
