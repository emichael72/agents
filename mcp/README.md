# MCPAgent (`agents/mcp`)

MCPAgent: a terminal agent that lets a model use the shared tools over
[MCP](https://modelcontextprotocol.io/). It starts its own MCP server, which runs the tools, as a
child process when it starts, talks to it over stdin and stdout, and stops it when it exits; there
is nothing to start first.

This is the hand-written member of the three agents: its agent loop (`MCPAgent.ask()`) is plain
code, where [pydantic](../pydantic) and [vercel](../vercel) use a framework. All three use the same
model profiles and the same tools, from the shared [tools folder](../tools). The Python package is
named `mcpagent`, inside the `mcp/` project folder.

## Layout

```
agent.py                              launcher (python mcp/agent.py from the repository root)
instructions.json                     the agent's name and its own instructions
mcpagent/__init__.py                  package root: __version__, REPO_ROOT and the config and schema paths
mcpagent/__main__.py                  entry point (python -m mcpagent): the command-line options
mcpagent/session.py                   AgentSession: the terminal front end, one prompt or a chat
mcpagent/agent.py                     MCPAgent: OpenAI Responses tool calling over MCP tools
mcpagent/guard.py                     RepeatGuard: stops a call repeated too often in one turn
mcpagent/profiles.py                  ModelProfiles: the shared model profiles and their overrides
mcpagent/context.py                   AgentContext: shared instructions, identity, skills, memory, settings
mcpagent/output.py                    Output: the terminal layout shared by the three agents
mcpagent/client.py                    MCPClient: the MCP client, for one or more servers
mcpagent/connection.py                one server's transport (STDIO or HTTP), handshake and session
mcpagent/service.py                   MCPService: the MCP server the agent starts (python -m mcpagent.service)
mcpagent/config.py                    MCPAgentConfig: loads and validates the config; its sections
mcpagent/types.py                     the package's types
mcpagent/logger.py, errors.py         the logger, and what the entry point prints on an error
mcpagent/jsons/mcpagent.json          the config: the agent's settings, and the tools its MCP server serves
mcpagent/jsons/schemas/mcpagent.schema.json  JSON schema for mcpagent.json
tests/                                agent-loop and server tests (model responses are mocked)
requirements.txt                      pinned dependencies (installed by the repository's install.sh)
```

## Setup

The repository's [`install.sh`](../install.sh) sets up the shared `.venv/` with
`requirements.txt`, and installs the `mcpagent` package into it in editable mode (from the
repository's `pyproject.toml`), so it runs from this checkout: `python mcp/agent.py` from the
repository root, or `.venv/bin/python -m mcpagent` from any folder. With the `.venv` as the
interpreter, PyCharm resolves the `mcpagent` imports.

Requirements: Python 3.10+, Bash and standard Unix tools; Node.js only for MCP Inspector.

## Run

From the repository root:

```bash
.venv/bin/python mcp/agent.py                                      # chat, default model profile
.venv/bin/python mcp/agent.py --prompt "Time now"                  # one prompt and exit
.venv/bin/python mcp/agent.py --model mistralai/mistral-small-3.2  # another model on the same server
.venv/bin/python -m unittest discover -s mcp/tests                 # offline tests
```

The agent prints the same layout as the other two (see "Terminal output" in the
[main README](../README.md)): a spinner while the model thinks or a tool runs, then the answer,
wrapped to 120 columns with a blank line before and after it, and the response time. `-d` (`--debug`)
shows a dark gray banner (model and tool count) and a dark gray line for each tool call (`→ tool(args)`),
result (`← tool: output`) and failure (`✗ tool: message`) instead of the spinner. In the chat,
`/history` shows the messages exchanged with the model, `/reset` clears them and `exit` quits; the
tool calls per turn are limited by `max_tool_calls` in `../context/agent.json`.

| Option                               | Purpose                                                                                |
|--------------------------------------|----------------------------------------------------------------------------------------|
| `--profile NAME`                     | Model profile from `../context/models.json`; default: the profile named by `"default"` |
| `--local`, `--openai`                | Shortcuts for `--profile local` and `--profile openai`                                 |
| `--model`, `--base-url`              | Override the profile's model or server for this run                                    |
| `--prompt "..."`                     | Run one prompt and exit                                                                |
| `-d`, `--debug`                      | Show the banner, tool calls and results instead of a spinner                           |
| `--config path/to/mcpagent.json`     | Use another config                                                                     |
| `--context path/to/instructions.txt` | Add instructions for the assistant                                                     |

Try: "What time is it in Tokyo?", "Count the lines in tools/time/README.md", "What OS is this
machine running?", "Remember that I prefer short answers", "Count the lines in missing-file and
explain what happened". The tools are listed in [../tools/README.md](../tools/README.md).

## The MCP server

The agent starts its server when it starts, as a child process (`python -m mcpagent.service`, with
the same config), and talks to it over its stdin and stdout: one JSON-RPC message per line. When the
agent exits, it closes the server's stdin and the server exits by itself (it is stopped if a tool is
still running). The server runs in its own session, so Ctrl+C reaches only the agent, which then
stops it. Started from a terminal, the server refuses: it serves only the agent that started it.

The server writes a short log to stderr: one line when it starts and one per tool it runs (the
command, its exit status and how long it took), and its errors. The agent shows these lines only
when `"server_output"` in the config is `true` and `-d` is on, as dark gray lines
among its own:

```text
server started, 9 tools from tools/
Local model server model: qwen/qwen3-coder-30b @ http://boba:1234/v1, 9 tools (sequential)
→ time({"timezone":"UTC"})
server ran time: bash time/time.sh --timezone=UTC (exit 0, 0.0s)
← time: 2026-10-08 03:56:00 UTC (UTC+00:00), Thursday
```

`"server_output"` is `false` by default. If the server stops unexpectedly, the error names its exit
status and its last log lines.

## How it maps to the other two

| Concern                    | mcpagent                                                                                                                             | pydantic                                                                                  | vercel                                                                                     |
|----------------------------|--------------------------------------------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------|
| Agent loop                 | `MCPAgent.ask()`, hand-written                                                                                                       | `Agent.run_stream_events()`                                                               | `ToolLoopAgent.stream()`                                                                   |
| Instructions               | `../context/instructions.json` (`instructions_file` in `mcpagent.json`), then `../mcp/instructions.json` (`agent_instructions_file`) | `../context/instructions.json`, then `instructions.json` → `AgentContext.system_prompt()` | `../context/instructions.json`, then `instructions.json` → `buildAgent()`                  |
| Model provider             | raw `aiohttp`, `/v1/responses`                                                                                                       | `OpenAIChatModel`                                                                         | `@ai-sdk/openai-compatible`                                                                |
| Tools                      | `../tools/*/tool.json`, loaded by the server it starts (`tools_dir`)                                                                 | `../tools/*/tool.json` → `Tool.from_schema`                                               | `../tools/*/tool.json` → `z.fromJSONSchema`                                                |
| Argument validation        | `jsonschema.validate`                                                                                                                | `jsonschema.validate`                                                                     | zod, from the same JSON schema                                                             |
| Tool failure               | `isError` result                                                                                                                     | `ToolFailed`                                                                              | thrown `Error` → `tool-error`                                                              |
| Running a script           | the server, `asyncio.create_subprocess_exec`                                                                                         | `subprocess.run` in a worker thread                                                       | async `execFile`, no threads                                                               |
| Conversation history       | list of Responses items                                                                                                              | `result.all_messages()`                                                                   | `response.messages`                                                                        |
| Loop cap                   | `max_tool_calls=8`                                                                                                                   | `UsageLimits(tool_calls_limit=8)`                                                         | `stopWhen: isStepCount(9)`                                                                 |
| Tool calls of one response | one at a time: the server is single-flight                                                                                           | at the same time: `parallel_tool_calls` in `instructions.json`                            | at the same time: `parallel_tool_calls` in `instructions.json` (`oneAtATime()` when false) |

The server runs one tool at a time: a `tools/call` that arrives while another is running is
rejected with `Busy: another tool is currently running in this workspace`. So MCPAgent asks the model
for one call per response, and its own instructions, [`instructions.json`](instructions.json), tell the
model so. That file also gives the agent its name (`mcp`), which the shared identity line uses.

## Configuring the model

The model is not set in code. `"models_file"` in `mcpagent/jsons/mcpagent.json` names the model profiles shared by
all three agents, [`../context/models.json`](../context/models.json); the fields, the options (`--profile`, `--local`,
`--openai`, `--model`, `--base-url`) and their precedence are described
under "Context" in [../README.md](../README.md). For this agent the profile's server must support
the `/v1/responses` endpoint.

The agent and the server it starts share one config, `mcpagent/jsons/mcpagent.json` (plain JSON,
one set of settings; its `"description"` lines explain the fields): the agent reads the context
files, `"servers"` and `"server_output"`, and the server the tools to serve (`"tools_dir"`,
`"tools_env"`, inline `"tools"`). Every config, including one given with `--config`, is validated
against `mcpagent/jsons/schemas/mcpagent.schema.json`; an invalid config or schema stops loading.
Paths in the config are relative to the repository root.

The model's instructions are not in the code either: `"instructions_file"` in the config names
the JSON file whose `"instructions"` lines are sent with every request, by default the shared
[`../context/instructions.json`](../context/instructions.json). `--context FILE` appends extra
instructions for one run.

With the `openai` profile, set the key without echoing it or saving it in shell history:

```bash
read -rsp "OpenAI API key: " OPENAI_API_KEY && export OPENAI_API_KEY  # Bash
read -rs "OPENAI_API_KEY?OpenAI API key: " && export OPENAI_API_KEY   # zsh
.venv/bin/python mcp/agent.py --openai
```

Prompts, tool schemas and tool outputs are then sent to OpenAI and billed to the API project that owns the key.
Requests use `store: false`; history stays in memory. `.env` files are ignored by git but not
loaded automatically.

## Configuring the MCP servers

`"servers"` in the config lists the MCP servers whose tools the model gets; tools from all
enabled servers are combined. Each entry has:

| Field         | Meaning                                                                                                   |
|---------------|-----------------------------------------------------------------------------------------------------------|
| `server_id`   | Unique id, shown in tool descriptions as `<server_id>/<tool>`                                             |
| `description` | What the server provides                                                                                  |
| `transport`   | `STDIO` (a child process; used here) or `HTTP`                                                            |
| `config`      | For STDIO: `command` (and optionally `env`); omit it for the agent's own server. For HTTP: `url`          |
| `enabled`     | Optional; `false` skips the entry                                                                         |

So another MCP server can be added beside the agent's own: a command to start it (STDIO), or the
address of one already running (HTTP).

## Inspect tools visually

[MCP Inspector](https://modelcontextprotocol.io/docs/tools/inspector) starts the server the same way
the agent does:

```bash
npx @modelcontextprotocol/inspector .venv/bin/python -m mcpagent.service
```

It connects over STDIO; list the tools, run one, and browse Resources for the tool documentation.
This also checks the server without a model.

For VS Code, merge this entry into the workspace's `.vscode/mcp.json`:

```json
{
  "servers": {
	"mcpagent": {
	  "type": "stdio",
	  "command": "${workspaceFolder}/.venv/bin/python",
	  "args": ["-m", "mcpagent.service"]
	}
  }
}
```

## Exposing other scripts

To add a tool for all three agents, add a folder to `../tools` (see
[../tools/README.md](../tools/README.md)); the agent's server finds it on the agent's next start.

To serve a different set of scripts, copy `mcpagent/jsons/mcpagent.json`, point its `tools_dir`
at another folder of `<tool>/tool.json` manifests (relative to the
repository root, or absolute) and run the agent with it:

```bash
.venv/bin/python mcp/agent.py --config /absolute/path/to/mcpagent.json
```

`tools_env` adds environment variables to every discovered tool. A config can also define tools
inline under `"tools": {"<name>": {...}}`, with the same fields as a `tool.json`; inline tools run
from the repository root unless they set `working_dir`.

## Limits

- The server negotiates MCP `2025-03-26` and `2025-06-18` only.
- Tools run with the user's permissions; the `shell` tool sandboxes its commands, and every tool
  that takes a path checks it against `../context/paths.json`. Output limits and cancellation of a
  running tool need work.

## Troubleshooting

| Symptom                                 | Check                                                                                           |
|-----------------------------------------|-------------------------------------------------------------------------------------------------|
| `The MCP server 'tools' stopped ...`    | The server could not start or crashed; the message ends with its last log lines.                |
| `Unknown model profile`                 | Check the name against `"profiles"` in `../context/models.json`.                                |
| `Set <VARIABLE> in the environment`     | The profile's `api_key_env` is unset; export it.                                                |
| `Could not reach Local model server`    | The model server is stopped or its host is unreachable; check with `curl -s <base_url>/models`. |
| HTTP 400/404 from the local server      | Check the model id against `/v1/models`, and use a model with tool calling.                     |
| OpenAI HTTP 401 / 403 / 404 / 429       | Key, project permissions, model availability, or quota and rate limits.                         |
| A tool reports an error                 | Read the output; check arguments, working directory and required programs.                      |

Requests are not retried automatically, because a tool may already have run.
