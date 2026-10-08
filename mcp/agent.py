"""
Module: agent.py

Description:
    Launches MCPAgent from the repository root: python mcp/agent.py. The agent itself is the
    mcpagent package next to this file; it starts its own MCP server.
"""

from mcpagent.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
