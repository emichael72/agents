"""
Module: __init__.py

Description:
    The gatekeepers: what keeps the agents and their work in bounds. fs is the file-system gate
    (context/paths.json) that every path-taking tool checks; pr is the pull request gate.

    REPO_ROOT is the repository root, for every gatekeeper. fs_gate resolves it, because Bash
    tools run fs_gate.py on its own, outside the package.
"""

from gatekeepers.fs.fs_gate import REPO_ROOT

__all__ = ["REPO_ROOT"]
