# MCPAgent (`agents/mcp`)

MCPAgent: an [MCP](https://modelcontextprotocol.io/) server that exposes existing shell commands and
Python scripts as tools, plus a terminal client that lets a model use them over MCP.

This is the hand-written member of the three agents: the client's agent loop (`MCPAgent.ask()`) is
plain code, where [pydantic](../pydantic) and [vercel](../vercel) use a framework. All three use the
same model profiles and the same tools, from the shared [tools folder](../tools). The Python package
is named `mcpagent`, inside the `mcp/` project folder.

## Layout

```
client.py                            client launcher (python mcp/client.py from the repository root)
server.py                            server launcher (python mcp/server.py from the repository root)
mcpagent/__init__.py                  the package's public names and __version__
mcpagent/config.py                    MCPAgentConfig: loads and validates the config; its sections
mcpagent/jsons/mcpagent.json          the config: "server" (port, tools_dir) and "client" (MCP servers,
                                      shared context/*.json)
mcpagent/jsons/schemas/mcpagent.schema.json  JSON schema for mcpagent.json
mcpagent/common/logger.py             the client's and the server's logger
mcpagent/common/errors.py             ExceptionReport: what the entry points print on an error
mcpagent/client/__main__.py           client module entry point (python -m mcpagent.client)
mcpagent/client/session.py            AgentSession: the terminal front end, one prompt or a chat
mcpagent/client/agent.py              MCPAgent: OpenAI Responses tool calling over MCP tools
mcpagent/client/profiles.py           ModelProfiles: the shared model profiles and their overrides
mcpagent/client/context.py            AgentContext: shared instructions, identity, memory, settings
mcpagent/client/output.py             Output: the terminal layout shared by the three agents
mcpagent/client/client.py             MCP client for multiple servers
mcpagent/client/connection.py         transport, handshake and session handling
mcpagent/client/types.py              client types
mcpagent/server/__main__.py           server module entry point (python -m mcpagent.server)
mcpagent/server/service.py            MCP HTTP server: tool discovery, validation and commands
mcpagent/server/types.py              server types
tests/                               server and agent-loop tests (model responses are mocked)
requirements.txt                     pinned dependencies (installed by the repository's install.sh)
```

## Setup

The repository's [`install.sh`](../install.sh) sets up the shared `.venv/` with
`requirements.txt`, and installs the `mcpagent` package into it in editable mode (from the
repository's `pyproject.toml`), so it runs from this checkout: `python mcp/server.py` and
`python mcp/client.py` from the repository root, or `.venv/bin/python -m mcpagent.server` and
`.venv/bin/python -m mcpagent.client` from any folder. With the `.venv` as the interpreter,
PyCharm resolves the `mcpagent` imports.

Requirements: Python 3.10+, Bash and standard Unix tools; Node.js only for MCP Inspector.

## Run

The client needs the server, so use two terminals, both at the repository root:

```bash
.venv/bin/python mcp/server.py                                      # terminal 1: wait for "Running..."
.venv/bin/python mcp/client.py                                      # terminal 2: chat, default model profile
.venv/bin/python mcp/client.py --prompt "Time now"                  # one prompt and exit
.venv/bin/python mcp/client.py --model mistralai/mistral-small-3.2  # another model on the same server
.venv/bin/python -m unittest discover -s mcp/tests                  # offline tests
```

The server listens on **http://127.0.0.1:6275/**. Stop it with **Ctrl+C**.

The client prints the same layout as the other two agents (see "Terminal output" in the
[main README](../README.md)): a spinner while the model thinks or a tool runs, then the answer,
wrapped to 120 columns with a blank line before and after it, and the response time. `-d` (`--debug`)
shows a dark gray banner (model and tool count) and a dark gray line for each tool call (`→ tool(args)`), result
(`← tool: output`) and failure (`✗ tool: message`) instead of the spinner. In the chat, `/history` shows the messages
exchanged with the model, `/reset` clears them and
`exit` quits; the tool calls per turn are limited by `max_tool_calls` in `../context/agent.json` (0: no limit).

| Option                               | Purpose                                                                                |
|--------------------------------------|----------------------------------------------------------------------------------------|
| `--profile NAME`                     | Model profile from `../context/models.json`; default: the profile named by `"default"` |
| `--local`, `--openai`                | Shortcuts for `--profile local` and `--profile openai`                                 |
| `--model`, `--base-url`              | Override the profile's model or server for this run                                    |
| `--prompt "..."`                     | Run one prompt and exit                                                                |
| `-d`, `--debug`                      | Show the banner, tool calls and results instead of a spinner                           |
| `--config path/to/mcpagent.json`     | Use another config (its client section)                                                |
| `--context path/to/instructions.txt` | Add instructions for the assistant                                                     |

Try: "What time is it in Tokyo?", "Count the lines in tools/time/README.md", "What OS is this
machine running?", "Remember that I prefer short answers", "Count the lines in missing-file and
explain what happened". The tools are listed in [../tools/README.md](../tools/README.md).

## How it maps to the other two

| Concern              | mcpagent                                                                       | pydantic                                               | vercel                                                |
|----------------------|--------------------------------------------------------------------------------|--------------------------------------------------------|-------------------------------------------------------|
| Agent loop           | `MCPAgent.ask()`, hand-written                                                 | `Agent.run_stream_events()`                            | `ToolLoopAgent.stream()`                              |
| Instructions         | `../context/instructions.json`, named by `instructions_file` in `mcpagent.json`  | `../context/instructions.json` → `AgentContext.instructions()` | `../context/instructions.json` → `loadInstructions()` |
| Model provider       | raw `aiohttp`, `/v1/responses`                                                 | `OpenAIChatModel`                                      | `@ai-sdk/openai-compatible`                           |
| Tools                | `../tools/*/tool.json`, loaded by the server (`tools_dir`)                     | `../tools/*/tool.json` → `Tool.from_schema`            | `../tools/*/tool.json` → `z.fromJSONSchema`           |
| Argument validation  | `jsonschema.validate`                                                          | `jsonschema.validate`                                  | zod, from the same JSON schema                        |
| Tool failure         | `isError` result                                                               | `ToolFailed`                                           | thrown `Error` → `tool-error`                         |
| Running a script     | the server, `asyncio.create_subprocess_exec`                                   | `subprocess.run` in a worker thread                    | async `execFile`, no threads                          |
| MCP client           | its own                                                                        | `MCPToolset`                                           | `@ai-sdk/mcp` `createMCPClient`                       |
| Conversation history | list of Responses items                                                        | `result.all_messages()`                                | `response.messages`                                   |
| Loop cap             | `max_tool_calls=8`                                                             | `UsageLimits(tool_calls_limit=8)`                      | `stopWhen: isStepCount(9)`                            |
| One tool at a time   | always                                                                         | `parallel_tool_call_execution_mode`                    | `oneAtATime()` wrapper in `vercelagent/tools.ts`      |

The server also runs one tool at a time: a `tools/call` that arrives while another is running is
rejected with `Busy: another tool is currently running in this workspace`.

## Configuring the model

The model is not set in code. `"models_file"` in `mcpagent/jsons/mcpagent.json` names the model profiles shared by
all three agents, [`../context/models.json`](../context/models.json); the fields, the options (`--profile`, `--local`,
`--openai`, `--model`, `--base-url`) and their precedence are described
under "Context" in [../README.md](../README.md). For this agent the profile's server must support
the `/v1/responses` endpoint.

The server and the client share one config, `mcpagent/jsons/mcpagent.json` (plain JSON; its
`"description"` lines explain the fields): `mcp/server.py` reads its `"server"` section and `mcp/client.py` its `"client"` section,
and each stops with an error if its section is missing. Every config, including one given with
`--config`, is validated against `mcpagent/jsons/schemas/mcpagent.schema.json`; an invalid config or
schema stops loading. Paths in the config are relative to the repository root.

The model's instructions are not in the code either: `"instructions_file"` in the client section names
the JSON file whose `"instructions"` lines are sent with every request, by default the shared
[`../context/instructions.json`](../context/instructions.json). `--context FILE` appends extra
instructions for one run.

With the `openai` profile, set the key without echoing it or saving it in shell history:

```bash
read -rsp "OpenAI API key: " OPENAI_API_KEY && export OPENAI_API_KEY  # Bash
read -rs "OPENAI_API_KEY?OpenAI API key: " && export OPENAI_API_KEY   # zsh
.venv/bin/python mcp/client.py --openai
```

Prompts, tool schemas and tool outputs are then sent to OpenAI and billed to the API project that owns the key.
Requests use `store: false`; history stays in memory. `.env` files are ignored by git but not
loaded automatically.

## Configuring the MCP servers

`"servers"` in the client section lists the MCP servers whose tools the model gets; tools from all
enabled servers are combined. Each entry has:

| Field         | Meaning                                                       |
|---------------|---------------------------------------------------------------|
| `server_id`   | Unique id, shown in tool descriptions as `<server_id>/<tool>` |
| `description` | What the server provides                                      |
| `transport`   | `HTTP` (used here) or `STDIO`                                 |
| `config`      | For HTTP: `url` (and optionally `sse_url`); omit it to reach the server section's address and port |
| `enabled`     | Optional; `false` skips the entry                             |

## Inspect tools visually

With the server running, launch [MCP Inspector](https://modelcontextprotocol.io/docs/tools/inspector):

```bash
npx @modelcontextprotocol/inspector
```

Choose **Streamable HTTP**, enter **http://127.0.0.1:6275/** and connect. List the tools, run
one, and browse Resources for the tool documentation. This also checks the server without a model.

For VS Code, merge this entry into the workspace's `.vscode/mcp.json`:

```json
{
  "servers": {
	"mcpagent": {
	  "type": "http",
	  "url": "http://127.0.0.1:6275/"
	}
  }
}
```

## Exposing other scripts

To add a tool for all three agents, add a folder to `../tools` (see
[../tools/README.md](../tools/README.md)) and restart the server.

To serve a different set of scripts, copy `mcpagent/jsons/mcpagent.json`, point the server
section's `tools_dir` at another folder of `<tool>/tool.json` manifests (relative to the
repository root, or absolute) and run:

```bash
.venv/bin/python mcp/server.py /absolute/path/to/mcpagent.json
```

`tools_env` adds environment variables to every discovered tool. A config can also define tools
inline under `"tools": {"<name>": {...}}`, with the same fields as a `tool.json`; inline tools run
from the repository root unless they set `working_dir`. The client's local server entry has no
address of its own, so a new server port needs no second edit.

## Transport and deployment limits

- The root endpoint answers POST with JSON, notifications with an empty 202, and GET with 405.
  It negotiates MCP `2025-03-26` and `2025-06-18` only, and rejects other values in the
  `MCP-Protocol-Version` header. This is why the vercel agent's `--mcp` cannot connect yet.
- `/sse` is a custom diagnostic feed, not standard MCP SSE. STDIO support needs more work.
- The server binds to localhost. Browser origins are limited to the default Inspector origins;
  other browser clients need `allowed_origins` in the server configuration.
- Tools run automatically with the server user's permissions and are not sandboxed (for example,
  `wc` accepts any file path). Authentication, output limits and cancellation need
  work before any remote deployment.

## Troubleshooting

| Symptom                                 | Check                                                                                           |
|-----------------------------------------|-------------------------------------------------------------------------------------------------|
| `Cannot connect to host 127.0.0.1:6275` | Start the server in another terminal first.                                                     |
| Address already in use                  | A server is already running: reuse it, or stop it with Ctrl+C.                                  |
| `Unknown model profile`                 | Check the name against `"profiles"` in `../context/models.json`.                                |
| `Set <VARIABLE> in the environment`     | The profile's `api_key_env` is unset; export it.                                                |
| `Could not reach Local model server`    | The model server is stopped or its host is unreachable; check with `curl -s <base_url>/models`. |
| HTTP 400/404 from the local server      | Check the model id against `/v1/models`, and use a model with tool calling.                     |
| OpenAI HTTP 401 / 403 / 404 / 429       | Key, project permissions, model availability, or quota and rate limits.                         |
| A tool reports an error                 | Read the output; check arguments, working directory and required programs.                      |

Requests are not retried automatically, because a tool may already have run.
