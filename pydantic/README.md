# Pydantic Agent (`agents/pydantic`)

A terminal agent built with [pydantic-ai](https://pydantic.dev/docs/ai/), the counterpart of
[MCPAgent](../mcp). It uses the same model profiles
([`../context/models.json`](../context/models.json)), the same instructions and the same tools, from
the shared [tools folder](../tools). The difference is **who runs the agent loop**: MCPAgent's
`MCPAgent.ask()` is hand-written, while here pydantic-ai does it.

## Layout

```
agent.py                    launcher that preserves the existing command
pydantic_agent/__init__.py  package root: REPO_ROOT and the shared context files' paths; its
                            name avoids the installed pydantic package's
pydantic_agent/__main__.py  command line (python -m pydantic_agent): argparse, then AgentSession
pydantic_agent/session.py   AgentSession: builds the Agent, runs turns, renders events, the chat
pydantic_agent/context.py   AgentContext: shared instructions, identity, memory, settings, limits
pydantic_agent/profiles.py  ModelProfiles: the shared model profiles and the pydantic-ai model
pydantic_agent/output.py    Output: the terminal layout shared by the three agents
pydantic_agent/toolset.py   LocalTools: loads ../tools/*/tool.json as pydantic-ai tools
tests/                      offline tests: scripted FunctionModel, tools run for real
```

## Setup

The repository's [`install.sh`](../install.sh) installs `requirements.txt` into the `.venv/` that
this agent shares with MCPAgent.

## Run

```bash
.venv/bin/python pydantic/agent.py                                # interactive chat
.venv/bin/python pydantic/agent.py --prompt "Time now" --history  # one prompt + raw message dump
.venv/bin/python -m unittest discover -s pydantic/tests           # offline tests
```

Run these from the repository root.

The importable package is `pydantic_agent`, inside this folder; `install.sh` installs it into the
`.venv` in editable mode, so `.venv/bin/python -m pydantic_agent` also works from any folder, and
PyCharm resolves its imports with the `.venv` as the interpreter. Keep this folder free of
`__init__.py`: a package named `pydantic` would shadow the installed dependency.

In the chat, `/history` prints the messages exchanged with the model, `/reset` clears them,
and `exit` quits. A spinner shows what the agent is doing; with `-d` (`--debug`), tool calls are
shown as `→ tool(args)` and results as `← tool: output` instead.

| Option / variable | Purpose |
| --- | --- |
| `--profile NAME`, `--local`, `--openai` | Model profile from `../context/models.json`; default: its `"default"` (`local`) |
| `--model`, `--base-url` | Override the profile's model or server for this run |

## How it maps to the other two

| Concern | mcpagent | pydantic | vercel |
| --- | --- | --- | --- |
| Agent loop | `MCPAgent.ask()`, hand-written | `Agent.run_stream_events()` | `ToolLoopAgent.stream()` |
| Instructions | `../context/instructions.json` (`instructions_file` in `mcpagent.json`), then `../mcp/instructions.json` (`agent_instructions_file`) | `../context/instructions.json`, then `instructions.json` → `AgentContext.system_prompt()` | `../context/instructions.json`, then `instructions.json` → `buildAgent()` |
| Model provider | raw `httpx`, `/v1/responses` | `OpenAIChatModel` | `@ai-sdk/openai-compatible` |
| Tools | `../tools/*/tool.json`, loaded by the server (`tools_dir`) | `../tools/*/tool.json` → `Tool.from_schema` | `../tools/*/tool.json` → `z.fromJSONSchema` |
| Argument validation | `jsonschema.validate` | `jsonschema.validate` | zod, from the same JSON schema |
| Tool failure | `isError` result | `ToolFailed` | thrown `Error` → `tool-error` |
| Running a script | the server, `asyncio.create_subprocess_exec` | `subprocess.run` in a worker thread | async `execFile`, no threads |
| Conversation history | list of Responses items | `result.all_messages()` | `response.messages` |
| Loop cap | `max_tool_calls=8` | `UsageLimits(tool_calls_limit=8)` | `stopWhen: isStepCount(9)` |
| Tool calls of one response | one at a time: the server is single-flight | at the same time: `parallel_tool_calls` in `instructions.json` | at the same time: `parallel_tool_calls` in `instructions.json` (`oneAtATime()` when false) |

## Parallel tool calls

The model only *asks* for tools; the agent code decides how to run them. When one model response
contains several tool calls, they run at the same time
(`parallel_tool_call_execution_mode("parallel")`), as `parallel_tool_calls` in
[`instructions.json`](instructions.json) sets; `false` runs them one at a time. The same file adds
lines to the shared instructions telling the model so: several calls go in one response only when
none depends on another, so a build never starts before the edit it needs. It also gives the agent
its name (`dantic`), which the shared identity line uses.
