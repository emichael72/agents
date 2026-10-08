# Vercel agent

This is the TypeScript version, using Vercel's AI SDK. `ToolLoopAgent` handles the model-and-tool loop; the surrounding
code loads the shared settings, adapts the tools, and displays the conversation.

## Run it

From the repository root, after installing:

~~~bash
node vercel/agent.ts
node vercel/agent.ts -d --prompt "What time is it in Tokyo?"
~~~

Node.js 22.18 or later runs the TypeScript directly. If the installer downloaded Node into `.node/`, use
`.node/bin/node` instead, or put `.node/bin` first in your PATH.

`-d` shows tool calls and results. `--history` prints the exchanged messages after a one-prompt run. In a chat, use
`/history`, `/reset`, and `exit`.

The model options match the Python agents: `--profile NAME`, `--local`, `--openai`, `--model`, and `--base-url`.

## Parallel calls

[instructions.json](instructions.json) currently enables `parallel_tool_calls`. Tool scripts run as concurrent child
processes, so independent calls can overlap even though Node's JavaScript runs on an event loop.

Set the option to `false` to queue calls one at a time, and keep the accompanying instructions consistent. The launcher
loads local tools. Its own instructions.json augments the shared context/instructions.json, including guidance to the
model about independent and dependent calls.

## Where the code is

`agent.ts` is the launcher. Inside `vercelagent/`:

- `agent.ts` builds the model and agent and handles the chat.
- `tools.ts` loads the tool manifests, validates arguments with Zod, and runs the scripts.
- `index.ts` exposes the names used by the launcher and tests.

The model settings, tools, skills, and memory are shared with the Python agents.

## Development checks

~~~bash
npm --prefix vercel test
npm --prefix vercel run typecheck
npm --prefix vercel run lint
~~~

Use `.node/bin/npm` if needed. The tests replace the model with a scripted stand-in.

See the [main README](../README.md) for installation and the [shared tools](../tools/README.md).
