# Pydantic agent

This version uses Pydantic AI to manage the conversation with the model and the repeated tool calls. The tools and model settings are the same as the other agents; the framework handles the loop.

## Run it

From the repository root, after installing:

~~~bash
.venv/bin/python pydantic/agent.py
.venv/bin/python pydantic/agent.py -d --prompt "What time is it in Tokyo?"
~~~

`-d` shows tool calls and results. Add `--history` to a one-prompt run to print the exchanged messages. In a chat, `/history` shows them, `/reset` clears them, and `exit` ends the session.

Model options are `--profile NAME`, `--local`, `--openai`, `--model`, and `--base-url`. They use the profiles in [context/models.json](../context/models.json).

## Parallel calls

[instructions.json](instructions.json) currently sets `parallel_tool_calls` to `true`. Independent calls from one model response can run together. A build that depends on an edit must wait for a later response.

Set it to `false` for sequential execution, update the accompanying instructions to agree, and restart the agent. There is no `--parallel` flag in the current launcher.

The agent adds its own instructions.json to the shared context/instructions.json. Its additions explain the parallel behavior to the model. It also loads the shared skills list and memory index. Tools run locally.

## Where the code is

`agent.py` launches the installed `pydantic_agent` package. Most of the work is in:

- `session.py`: builds the Agent and displays its events.
- `toolset.py`: turns shared tool manifests into Pydantic AI tools.
- `context.py` and `profiles.py`: instructions, settings, and model selection.

`.venv/bin/python -m pydantic_agent` also launches it. The package name is deliberately different from the Pydantic dependency.

Offline tests use a stand-in model with real tools:

~~~bash
.venv/bin/python -m unittest discover -s pydantic/tests
~~~

See the [main README](../README.md) for installation and the [tools guide](../tools/README.md) for what the agent can do.
