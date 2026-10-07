# Agents

Three agents that do the same job, each built a different way. All three talk to the same model,
configured once in [`context/models.json`](context/models.json) (by default an LM Studio server
running `qwen/qwen3-coder-30b`), and run the same tools, which they discover in the shared
[`tools/`](tools/README.md) folder. Each greeting says which agent ran it.

| Agent | Built with | Who runs the agent loop | README |
| --- | --- | --- | --- |
| `mcpagent` | Python, no framework (MCPAgent) | hand-written code | [mcpagent/README.md](mcpagent/README.md) |
| `pydantic` | Python, [pydantic-ai](https://pydantic.dev/docs/ai/) | the framework | [pydantic/README.md](pydantic/README.md) |
| `vercel` | TypeScript, [AI SDK](https://ai-sdk.dev) | the framework | [vercel/README.md](vercel/README.md) |

The commands below are a quick tour. Every option and troubleshooting are in each agent's own
README. Run every command from the repository root; paths are relative to it.

## Setup

`install.sh` prepares everything the three agents need:

- `.venv/`: one Python environment shared by MCPAgent and the Pydantic Agent, with both agents'
  pinned requirements (`mcpagent/requirements.txt`, `pydantic/requirements.txt`). Neither agent is
  installed as a package; both run from the source tree.
- `vercel/node_modules/`: the Vercel Agent's packages, installed exactly as
  `vercel/package-lock.json` records (`npm ci`).

```bash
./install.sh  # -f recreates both, --skip-vercel skips Node, -h help
```

It also installs the pinned development tools in `requirements-dev.txt`: Ruff, the Python linter.
`.venv/bin/ruff check` checks the repository for likely bugs, with the rules in `ruff.toml`.

Requirements: Python 3.10+, and Node.js 22.18+ with npm 10+ for the Vercel Agent (Node 22.18+ runs
the `.ts` files directly, so there is no build step). Neither `.venv/` nor `node_modules/` is
committed.

## Tools

`tools/` holds one folder per tool: a `tool.json` manifest, the script and its `README.md`.
Every agent scans this folder at startup, so a tool added there is available to all three
without code changes. [tools/README.md](tools/README.md) describes the manifest and how to add
a tool. Current tools: `shell` (ls, cat, grep, find, sed, make, gcc, git and more, in a sandbox),
`ed` (edit files), `mr` (open a merge request), `doxy`
(checks Doxygen documentation of C/C++ sources) and `pr_gate` (Pull Request Gate: a merge gate
that quizzes a developer on their pull request; see [gatekeepers/pr/README.md](gatekeepers/pr/README.md)).
Tools that take a path only reach the folders named in
[`context/paths.json`](context/paths.json).

```bash
bash tools/greet/greet.sh --name Alice  # run a tool's script by hand
```

## Gatekeepers

[`gatekeepers/`](gatekeepers/README.md) holds what keeps the agents and their work in bounds:

- **`fs`**, the file-system gate: every tool that takes a path checks it against the folders and
  access rights in `context/paths.json`.
- **`pr`**, the pull request gate: a service that builds, tests and checks the documentation of
  each pull request to `core_dump`, then quizzes its author before GitHub allows the merge. Install
  and run it with `./install.sh --gate install` (also `status`, `logs`, `restart`, `stop`,
  `uninstall`).

## Context

`context/` holds what the agents share besides tools; edit it once and every agent picks up the
change on its next start. None of this is hard-coded in the agents.

- `context/instructions.json`: the instructions (system prompt) given to the model, as a list of
  lines.
- `context/agent.json`: the agent loop: `max_tool_calls`, the most tool calls the model may make while
  answering one prompt; `0` (the setting now) means no limit, so stop a runaway answer with Ctrl+C.
- `context/output.json`: the terminal layout: `width` (120) and `show_time` (true). See
  "Terminal output" below.
- `context/clang-format.yaml`: the C/C++ style (4-space indents, function braces on their own
  line, 120 columns), used by `clang-format` in the shell and by `mr`, which formats the changed
  files before opening a merge request. A project's own `.clang-format` wins.
- `context/paths.json`: the folders the tools may use and their access (`r` read, `w` write,
  `x` execute): `core_dump` is `rwx`, `tools` is `r`. Every tool that takes a path checks it with
  the file-system gate, `gatekeepers/fs/fs_gate.py`, and `shell` mounts exactly these folders in its
  sandbox; see
  [tools/README.md](tools/README.md). Unlike the rest of `context/`, it is read on every tool call,
  so a change applies at once.
- `context/models.json`: the model profiles. `"default"` names the profile used when no flag picks
  one; it ships with `local` (an LM Studio server) and `openai`. The `local` profile expects LM
  Studio at `http://boba:1234/v1`; point it at another host with `LOCAL_LLM_BASE_URL` or by
  editing the profile.

```json
"profiles": {
  "local": {
    "name": "Local model server",
    "base_url": "http://boba:1234/v1",  "base_url_env": "LOCAL_LLM_BASE_URL",
    "model": "qwen/qwen3-coder-30b",     "model_env": "LOCAL_LLM_MODEL",
    "api_key_env": "LOCAL_LLM_API_KEY",  "api_key": "lm-studio",
    "timeout": 300
  },
  "openai": { "name": "OpenAI", "base_url": "https://api.openai.com/v1", "model": "gpt-4.1-mini",
              "model_env": "OPENAI_MODEL", "api_key_env": "OPENAI_API_KEY", "timeout": 60 }
}
```

| Field | Meaning |
| --- | --- |
| `base_url`, `model` | Required. An OpenAI-compatible server and a model id it serves (MCPAgent also needs the server to support `/v1/responses`) |
| `base_url_env`, `model_env` | Optional environment variables that override `base_url` and `model` |
| `api_key_env` | Environment variable holding the key; a profile never reads another profile's key |
| `api_key` | Fallback when `api_key_env` is unset, for servers that need no real key; never put a secret here |
| `timeout` | Request timeout in seconds; 300 leaves time for LM Studio to load a model |
| `model_auto` | Ask the server which model is loaded (LM Studio's `/api/v0/models`) and use it; `model` is the fallback when none is loaded. `--model` and `model_env` still win |
| `name` | Display name in the chat banner and error messages |

All three agents take the same options: `--profile NAME` (or the shortcuts `--local` and
`--openai`) picks a profile, and `--model` / `--base-url` override it for one run. Precedence is
the command line, then the `*_env` variables, then the file. To add a model, add a profile (for
example `"mistral": {...}`) and pass `--profile mistral`.

## Terminal output

All three agents print a turn the same way, so their output can be compared line for line:

```text
Local model server model: qwen/qwen3-coder-30b @ http://boba:1234/v1, 12 tools (sequential)
→ pr_gate({})
← pr_gate: Quiz service: running at http://minion:8000
  PR #1 'Compute pi using the C math library' by emichael72 at 135c35a: quiz waiting, merge blocked: …

The quiz service is running. Pull request #1 is still waiting for its quiz, so its merge is blocked.

Response time: 6.3s · tokens: 4,313 in, 53 out (2 model calls)
```

1. **Only the model's answer is in the terminal's normal color.** Everything else (the banner, the
   chat hints and `You >` prompt, tool calls `→`, results `←`, failures `✗`, and the response time)
   is dark gray (ANSI 90). Errors are red.
2. **Everything fits in `width` columns** (`context/output.json`, 120), or in the terminal if it is
   narrower. Gray lines wrap with a two-space indent; the streamed answer wraps between words as
   it arrives. A word longer than the width, such as a URL, is never broken.
3. **Links are clickable.** On a terminal, a Markdown link `[text](url)` shows as *text* and a web
   address as itself, both as clickable OSC 8 links (VS Code's terminal, iTerm2, GNOME Terminal and
   others), in the answer and in the gray tool lines. A link is never split while the answer
   streams. `"links": false` turns this off; piped output is always plain.
4. **The answer has exactly one blank line before and after it.** A tool call prints together with
   its result, in the order they ran. `--quiet` hides the tool lines.
5. **Each response ends with its time and tokens**: `Response time: N.Ns` from sending the prompt
   to the end of the answer, tools included; then the tokens the model calls used, as the server
   reports them: *in* (sent to the model: instructions, tool list, history, tool results, added
   up over every call) and *out* (generated), and how many model calls the answer took.
   `"show_time": false` and `"show_tokens": false` turn them off; `tokens: not reported` means
   the server sent no counts.

Each agent implements this in an `Output` class (`mcpagent/client/agent.py`, `pydantic/agent.py`,
`vercel/agent.ts`), with tests that check the wrapping and the timing line.

## MCPAgent

An MCP server that exposes shell scripts as tools, plus a terminal client that drives the model.
See [mcpagent/README.md](mcpagent/README.md).

```bash
.venv/bin/python -m mcpagent.server                      # terminal 1: MCP server on 127.0.0.1:6275
.venv/bin/python -m mcpagent.client                      # terminal 2: chat, default model profile
.venv/bin/python -m mcpagent.client --prompt "Greet me"  # one prompt and exit
.venv/bin/python -m unittest discover -s mcpagent/tests  # offline tests
```

## Pydantic

The same agent with pydantic-ai running the loop; tools run in-process, or from the
MCPAgent server with `--mcp`. See [pydantic/README.md](pydantic/README.md).

```bash
.venv/bin/python pydantic/agent.py                                # interactive chat
.venv/bin/python pydantic/agent.py --prompt "Greet me" --history  # one prompt + raw message dump
.venv/bin/python pydantic/agent.py --parallel                     # run a response's tool calls concurrently
.venv/bin/python pydantic/agent.py --mcp                          # tools from the MCPAgent server
.venv/bin/python -m unittest discover -s pydantic/tests           # offline tests
```

## Vercel

The same agent in TypeScript with the AI SDK's `ToolLoopAgent`; Node runs the `.ts` files
directly. See [vercel/README.md](vercel/README.md).

```bash
node vercel/agent.ts                                # interactive chat
node vercel/agent.ts --prompt "Greet me" --history  # one prompt + raw message dump
node vercel/agent.ts --parallel                     # run a response's tool calls concurrently
npm --prefix vercel test                            # offline tests
```

`--mcp` is also available, but it does not yet work against the MCPAgent server (an MCP protocol
version mismatch); the vercel README explains why.

## Shared settings

The model settings live in `context/models.json` (see "Context" above). With the default `local`
profile, these environment variables override it in all three agents:

| Variable | Default |
| --- | --- |
| `LOCAL_LLM_BASE_URL` | `http://boba:1234/v1` |
| `LOCAL_LLM_MODEL` | `qwen/qwen3-coder-30b` |
| `LOCAL_LLM_API_KEY` | `lm-studio` |

To check that the model server is up, request its model list, e.g. `curl -s http://boba:1234/v1/models`
for the default `local` profile.

## License

MIT; see [LICENSE](LICENSE).
