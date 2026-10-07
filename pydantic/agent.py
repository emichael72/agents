"""
Module: agent.py

Description:
    Launches the pydantic_agent package while preserving the repository's existing
    command: python pydantic/agent.py.
"""

from pydantic_agent import main

if __name__ == "__main__":
    raise SystemExit(main())
