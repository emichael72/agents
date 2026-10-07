"""
Module: __init__.py

Description:
    The gatekeepers: what keeps the agents and their work in bounds. fs is the file-system gate
    (context/paths.json) that every path-taking tool checks; pr is the pull request gate.

    It holds the locations the gatekeepers share: the repository root and the folders and files
    under it. The classes live in their modules; import them from there, e.g.
    `from gatekeepers.fs.fs_gate import FsGate`.
"""
from pathlib import Path

__version__ = "1.0.0"

# The repository root: the nearest folder above holding pyproject.toml
REPO_ROOT = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").is_file())
TOOLS_DIR = REPO_ROOT / "tools"  # One <tool>/tool.json per tool
GATEKEEPERS_DIR = REPO_ROOT / "gatekeepers"
CONTEXT_DIR = REPO_ROOT / "context"  # Shared by the agents
PATHS_FILE = CONTEXT_DIR / "paths.json"  # The allowed folders and their access
