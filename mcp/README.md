# MCPAgent

This is the hand-written version of the agent. Its Python code manages the model conversation, runs requested tools, sends their results back, and repeats until there is an answer.

The tools live in a separate MCP server process. MCPAgent starts that process itself and talks to it through standard input and output. Exiting the agent stops the server too.

## Run it

After running the installer at the repository root:

~~~bash
.venv/bin/python mcp/agent.py
.venv/bin/python mcp/agent.py -d --prompt "What time is it in Tokyo?"
~~~

`-d` shows the model banner, tool calls, and results. In a chat, use `/history`, `/reset`, or `exit`.

The installed package is `mcpagent`, so `.venv/bin/python -m mcpagent` is another way to launch it.

## Change the model or instructions

Model profiles are shared in [context/models.json](https://github.com/emichael72/agents/blob/a2fe18a204843563134bb1ed0d7aaf63d558691e/context/models.json). Use `--profile NAME`, `--openai`, `--model`, or `--base-url` to override them. The model server must support `/v1/responses`.

The shared prompt is followed by this agent's [instructions.json](https://github.com/emichael72/agents/blob/a2fe18a204843563134bb1ed0d7aaf63d558691e/mcp/instructions.json). `--context FILE` adds instructions for one run.

[mcpagent/jsons/mcpagent.json](https://github.com/emichael72/agents/blob/a2fe18a204843563134bb1ed0d7aaf63d558691e/mcp/mcpagent/jsons/mcpagent.json) controls the MCP connections, tool discovery, and server output. Use `--config FILE` for another configuration. Paths in it are relative to the repository root.

## What to expect

Tools run one at a time. The server rejects overlapping calls, and the agent asks the model to work sequentially. This is a choice in this implementation, not a restriction of MCP.

The client can also connect to other configured STDIO or HTTP MCP servers. Its own tool server uses STDIO.

If the child server stops, the agent reports its exit status and recent log lines. Model requests are not automatically retried because a tool may already have completed.

## Where the code is

- `mcpagent/agent.py`: model-and-tool loop.
- `mcpagent/session.py`: chat and terminal output.
- `mcpagent/client.py` and `connection.py`: MCP connections.
- `mcpagent/service.py`: the tool server.

To inspect the tools without a model:

~~~bash
npx @modelcontextprotocol/inspector .venv/bin/python -m mcpagent.service
~~~

Offline tests:

~~~bash
.venv/bin/python -m unittest discover -s mcp/tests
~~~

See the [shared tools](../tools/README.md) and [main README](../README.md) for setup and permissions.
