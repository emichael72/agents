# Pydantic Agent (`agents/pydantic`)

A terminal agent built with [pydantic-ai](https://pydantic.dev/docs/ai/), the counterpart of
MCPAgent's client. It uses the same model profiles
([`../context/models.json`](../context/models.json)), the same instructions and the same tools, from
the shared [tools folder](../tools). The difference is **who runs the agent loop**: MCPAgent's
`MCPAgent.ask()` is hand-written, while here pydantic-ai does it.

## Layout

```
agent.py        builds the model + Agent, and a thin terminal UI that prints the run's events
                instructions come from the shared ../context/instructions.json
toolset.py      loads ../tools/*/tool.json and turns each manifest into a pydantic-ai tool
tests/          offline tests: a scripted FunctionModel replaces LM Studio, tools run for real
```

## Setup

The repository's [`install.sh`](../install.sh) installs `requirements.txt` into the `.venv/` that
this agent shares with MCPAgent.

## Run

```bash
.venv/bin/python pydantic/agent.py                                # interactive chat, local Python tools
.venv/bin/python pydantic/agent.py --prompt "Greet me" --history  # one prompt + raw message dump
.venv/bin/python pydantic/agent.py --mcp                          # same tools, served by MCPAgent's server over MCP
.venv/bin/python -m unittest discover -s pydantic/tests           # offline tests
```

Run these from the repository root. For `--mcp`, start the MCPAgent server first:
`.venv/bin/python -m mcpagent.server`.

In the chat, `/history` prints the messages exchanged with the model, `/reset` clears them,
and `exit` quits. Tool calls are shown as `→ tool(args)` and results as `← tool: output`;
`--quiet` hides them.

| Option / variable | Purpose |
| --- | --- |
| `--profile NAME`, `--local`, `--openai` | Model profile from `../context/models.json`; default: its `"default"` (`local`) |
| `--model`, `--base-url` | Override the profile's model or server for this run |
| `--mcp [URL]` | Use an MCP server's tools; default URL `http://127.0.0.1:6275/` |
| `--parallel` | Run the tool calls from one model response concurrently |

## How it maps to the other two

| Concern | mcpagent | pydantic | vercel |
| --- | --- | --- | --- |
| Agent loop | `MCPAgent.ask()`, hand-written | `Agent.run_stream_events()` | `ToolLoopAgent.stream()` |
| Instructions | `../context/instructions.json`, named by `instructions_file` in `client.jsonc` | `../context/instructions.json` → `load_instructions()` | `../context/instructions.json` → `loadInstructions()` |
| Model provider | raw `httpx`, `/v1/responses` | `OpenAIChatModel` | `@ai-sdk/openai-compatible` |
| Tools | `../tools/*/tool.json`, loaded by the server (`tools_dir`) | `../tools/*/tool.json` → `Tool.from_schema` | `../tools/*/tool.json` → `z.fromJSONSchema` |
| Argument validation | `jsonschema.validate` | `jsonschema.validate` | zod, from the same JSON schema |
| Tool failure | `isError` result | `ToolFailed` | thrown `Error` → `tool-error` |
| Running a script | the server, `asyncio.create_subprocess_exec` | `subprocess.run` in a worker thread | async `execFile`, no threads |
| MCP client | its own | `MCPToolset` | `@ai-sdk/mcp` `createMCPClient` |
| Conversation history | list of Responses items | `result.all_messages()` | `response.messages` |
| Loop cap | `max_tool_calls=8` | `UsageLimits(tool_calls_limit=8)` | `stopWhen: isStepCount(9)` |
| One tool at a time | always | `parallel_tool_call_execution_mode` | `oneAtATime()` wrapper in `tools.ts` |

## Parallel tool calls

The model only *asks* for tools; the agent code decides how to run them. When one model response
contains several tool calls, they run one at a time by default
(`parallel_tool_call_execution_mode("sequential")`). Asking for parallelism in the prompt does not
change this; `--parallel` does.

Locally, `--parallel` is safe because the tools' scripts share no state. With `--mcp --parallel`,
the MCPAgent server rejects overlapping calls with `Busy: another tool is currently running in
this workspace`. Depending on timing, the model either reports the tool as unavailable or retries
it, and the turn fails once pydantic-ai's retry limit is reached.
