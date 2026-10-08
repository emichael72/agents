# Vercel Agent (`agents/vercel`)

A terminal agent built with Vercel's [AI SDK](https://ai-sdk.dev) (`ai` v7), the counterpart of the
[pydantic agent](../pydantic) and [MCPAgent](../mcp). It uses the same model profiles
([`../context/models.json`](../context/models.json)), the same instructions and the same tools, from
the shared [tools folder](../tools). The agent loop is run by the SDK's `ToolLoopAgent`; this code
only defines the tools and renders the stream in the terminal.

## Layout

```
agent.ts                launcher that keeps the command: node vercel/agent.ts
vercelagent/index.ts    public names: what the launcher and the tests import
vercelagent/agent.ts    model, ToolLoopAgent and a thin terminal UI over its fullStream; main();
                        shared ../context instructions
vercelagent/tools.ts    loads ../tools/*/tool.json as AI SDK tools (zod-validated); REPO_ROOT
tests/                  offline tests: the SDK's MockLanguageModelV4 replaces LM Studio, tools run for real
```

The package finds the repository's shared folders (`context/`, `tools/`) from `REPO_ROOT`, the
nearest folder above it holding `pyproject.toml`, as the Python agents do.

Node 22.18+ runs the TypeScript files directly (type stripping), so there is no build step.

## Setup

The repository's [`install.sh`](../install.sh) checks Node.js (>= 22.18) and npm, then runs
`npm ci` here to install exactly what `package-lock.json` records into `node_modules/`. When the
system's Node.js is missing or older, it fetches a pinned Node.js 22 release from nodejs.org
into the repository's `.node/` (checked against the release's SHA-256) and uses that; run the
commands below with `.node/bin/node`, or with `.node/bin` first in `PATH`.

## Run

From the repository root:

```bash
node vercel/agent.ts                                # interactive chat, local tools
node vercel/agent.ts --prompt "Time now" --history  # one prompt + raw message dump
npm --prefix vercel test                            # offline tests
npm --prefix vercel run typecheck                   # tsc --noEmit
npm --prefix vercel run lint                        # Oxlint: likely bugs, not style (.oxlintrc.json)
```

In the chat, `/history` prints the messages exchanged with the model, `/reset` clears them,
and `exit` quits. A spinner ([ora](https://github.com/sindresorhus/ora)) shows what the agent is
doing; with `-d` (`--debug`), tool calls are shown as `→ tool(args)`, results as `← tool: output`
and failures as `✗ tool: error` instead.

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

The AI SDK starts each tool as soon as its call arrives in the stream, so the tool calls of one
response run at the same time. Node is single-threaded, but `execFile` is asynchronous, so the
scripts run as concurrent child processes while the event loop waits. `parallel_tool_calls` in
[`instructions.json`](instructions.json) turns this off: `vercelagent/tools.ts` then chains `execute`
calls through a promise queue (`oneAtATime()`). The same file adds lines to the shared instructions
telling the model that its calls run at the same time, so several go in one response only when
none depends on another, and gives the agent its name (`vercel`).
