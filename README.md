# Agents

This project builds the same coding agent three ways. Each version uses the same model, tools, and shared instructions,
so the comparison is mostly about who manages the work between model calls.

| Agent                             | Language   | Agent loop                                 |
|-----------------------------------|------------|--------------------------------------------|
| [MCPAgent](mcp/README.md)         | Python     | Hand-written, with its own MCP tool server |
| [Pydantic AI](pydantic/README.md) | Python     | Managed by Pydantic AI                     |
| [Vercel AI SDK](vercel/README.md) | TypeScript | Managed by ToolLoopAgent                   |

Pydantic AI and Vercel AI SDK supply the tool-calling loop, model integrations, and streaming events; MCPAgent
implements that work in Python.

I focused on Pydantic because Python is more familiar to me. The TypeScript version follows a similar design and is
readable alongside it.

In the demo, the agents and [core_dump](https://github.com/emichael72/core_dump) run on **minion**. **qwen-coder-next**
runs through LM Studio on an **M3 Mac Studio**. The agent extends the C program, checks its work, and opens a pull
request. A separate service quizzes the developer before the change can merge.

## Start an agent

The installer supports Red Hat family Linux distributions with `dnf`; it has been tested on Rocky Linux 9.4. It needs
Python 3.10 or later. The coding tools also need bubblewrap, a C build environment, Doxygen, and the relevant
Git/formatting tools.

From the repository root:

~~~bash
./install.sh
~~~

This creates a shared Python environment, installs the repository's Python packages in editable mode, and prepares the
TypeScript dependencies. If Node is missing or too old, the installer can download a local copy into `.node/` on
supported Linux systems. Use `--skip-vercel` for Python only.

Then choose one:

~~~bash
.venv/bin/python mcp/agent.py
.venv/bin/python pydantic/agent.py
node vercel/agent.ts
~~~

MCPAgent starts and stops its own tool server. No second terminal is needed. If the installer supplied Node, use
`.node/bin/node` in the last command.

Add `-d` to see the tool calls and results. On a terminal the answer's Markdown is rendered as it streams (headings, emphasis, lists, tables, highlighted code), and with `-d` code a tool shows from a file is highlighted, dimmed; `--plain` prints raw text instead (`render` in `context/output.json`). Type `exit` to finish; `/history` shows the conversation and `/reset` clears
it.

## Models and settings

[context/models.json](context/models.json) holds the profiles shared by all three agents. The local profile points to LM
Studio at `http://boba:1234/v1` and can discover the loaded model. It names no model of its own, so it never makes LM Studio load one: with nothing loaded, the agent says so and stops.

Use `--profile NAME`, `--model`, or `--base-url` to change a run. The `--openai` shortcut uses the OpenAI profile and
needs `OPENAI_API_KEY` in the environment. MCPAgent needs a provider with a Responses endpoint; Pydantic and Vercel use
chat completions.

The rest of `context/` holds shared instructions, allowed folders, formatting settings, and turn limits. Each agent adds
its own instructions.json to the global instructions: shared guidance first, then the details of that agent. MCPAgent
tells the model to call tools one at a time; Pydantic and Vercel explain that independent calls may run together.

## Tools, skills, and memory

The [tools](tools/README.md) read and edit files, build code, check documentation, and submit changes. A tool's
`tool.json` tells the model what it does and defines its arguments. All three agents load the same definitions.

[Skills](tools/skill/README.md) describe a procedure, such as how to make a change and open a PR. The agents see a short
list at startup and read the full procedure when needed.

[Memory](tools/memory/README.md) keeps short notes between chats in `.memory/`. The notes are local and shared by the
three agents.

## Tool execution

MCPAgent runs tools one at a time. Pydantic and Vercel currently run the calls from one response concurrently;
`parallel_tool_calls` in their own `instructions.json` controls this. Their instructions tell the model to group
independent calls and wait before doing dependent work.

Turn and repeat limits live in `context/agent.json`. The current settings are 150 for max_tool_calls and 3 for identical
consecutive calls; Vercel uses the former to set its step limit.

## The pull request gate

The [pr tool](tools/pr/README.md) synchronizes the working copy, checks a proposed change, and opens a PR. It was
previously called `mr`.

The [gate service](gatekeepers/pr/README.md) checks the submitted code and asks the model for a quiz about the diff. The
gate reuses the agents' `shell` tool for builds/tests and their `doxy` script for documentation. You answer in a browser;
the Python service grades the answers, stores the result, and reports the `developer-quiz` status to GitHub. GitHub
enforces the required-status rule, and a person merges the PR.

The demo settings allow skipping the quiz. Turn that off to require a perfect score for code changes. See
the [flow walkthrough](gatekeepers/pr/README.md#what-happens-to-a-change) for the full handoff.

MIT licensed.
