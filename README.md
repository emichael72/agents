# Agents

Three agents that do the same job, each built a different way. All three talk to the same model,
configured once in [`context/models.json`](context/models.json) (by default an LM Studio server
running `qwen/qwen3-coder-30b`), and run the same tools, which they discover in the shared
[`tools/`](tools/README.md) folder, and remember between runs in the same [memory](#memory).

| Agent      | Built with                                           | Who runs the agent loop | README                                   |
|------------|------------------------------------------------------|-------------------------|------------------------------------------|
| `mcp`      | Python, no framework (MCPAgent)                      | hand-written code       | [mcp/README.md](mcp/README.md)           |
| `pydantic` | Python, [pydantic-ai](https://pydantic.dev/docs/ai/) | the framework           | [pydantic/README.md](pydantic/README.md) |
| `vercel`   | TypeScript, [AI SDK](https://ai-sdk.dev)             | the framework           | [vercel/README.md](vercel/README.md)     |

The commands below are a quick tour. Every option and troubleshooting are in each agent's own
README. Run every command from the repository root; paths are relative to it.

## Setup

`install.sh` prepares everything the three agents need:

- `.venv/`: one Python environment shared by MCPAgent and the Pydantic Agent, with every
  `requirements*.txt` in the repository (the agents', the pull request gate's and the development
  tools' pinned versions), and the repository's own packages (`mcpagent`, `pydantic_agent`,
  `gatekeepers`) installed editable from `pyproject.toml`, so they import from anywhere and run
  from this checkout.
- `vercel/node_modules/`: the Vercel Agent's packages, installed exactly as
  `vercel/package-lock.json` records (`npm ci`).
- `.node/`, only when the system's Node.js is missing or older than 22.18: a pinned Node.js 22
  release from nodejs.org, checked against its published SHA-256 and unpacked here, so no root
  access or system upgrade is needed (any x86_64 or arm64 Linux with glibc 2.28+).

```bash
./install.sh  # -f recreates both, --skip-vercel skips Node, -h help
```

It also installs the pinned development tools in `requirements-dev.txt`: Ruff, the Python linter.
`.venv/bin/ruff check` checks the repository for likely bugs, with the rules in `ruff.toml`.
The Vercel Agent's TypeScript gets the same kind of check from Oxlint, pinned in
`vercel/package.json` and installed with the agent's packages: `npm --prefix vercel run lint`
(rules in `vercel/.oxlintrc.json`).

None of `.venv/`, `node_modules/` and `.node/` is committed.

### Supported systems

`install.sh` runs on Red Hat family Linux distributions that use `dnf`. It reads
`/etc/os-release` and continues only when `ID` or `ID_LIKE` names `rhel`, `fedora` or `centos` and
`dnf` is installed; on any other system it stops before changing anything.

| System                                                       | `install.sh`                       |
|--------------------------------------------------------------|------------------------------------|
| Rocky Linux 9                                                | Tested (Rocky Linux 9.4)           |
| RHEL 8 and 9, AlmaLinux, CentOS Stream, Oracle Linux, Fedora | Accepted: same family, with `dnf`  |
| Amazon Linux 2023                                            | Accepted: `ID_LIKE` is `fedora`    |
| RHEL 7, CentOS 7, Amazon Linux 2                             | Stops: `yum` instead of `dnf`      |
| Debian, Ubuntu and other Linux distributions; macOS          | Stops: not a Red Hat family system |

What the system needs:

- Python 3.10 or newer: `python3` when it is new enough, else the newest `python3.N` in `PATH`,
  or the one `--python` names. RHEL 8 and 9 ship an older `python3`; install a newer `python3.N`
  package with `dnf`.
- For the Vercel Agent, Node.js 22.18+ with npm 10+, or else `curl`, `tar`, `xz` and
  `sha256sum`, with which `install.sh` fetches Node.js into `.node/` (x86_64 or arm64, glibc
  2.28+). Node 22.18+ runs the `.ts` files directly, so there is no build step. `--skip-vercel`
  needs no Node.js at all.
- For the `shell` tool, bubblewrap (`bwrap`), which sandboxes its commands; the 0.4.1 release
  that RHEL 9 ships works.
- For the tools that need them: `git` and the GitHub CLI `gh` (the `pr` tool and the pull request
  gate), `doxygen` (the `doxy` tool), and `clang-format` and `clang-tidy` (formatting and checks in
  the `shell` and `pr` tools). `./install.sh --gate install` warns about any of these that are
  missing.

## Choosing a model

By default, every agent uses the `local` profile in [`context/models.json`](context/models.json),
an LM Studio server. All three agents take the same options to use another model; run them from
the repository root, and start the MCP server (`.venv/bin/python mcp/server.py`) before MCPAgent.

**OpenAI.** The `openai` profile ships ready to use (`gpt-4.1-mini`). Set the key without echoing
it or saving it in shell history, then pass `--openai`:

```bash
read -rsp "OpenAI API key: " OPENAI_API_KEY && export OPENAI_API_KEY  # Bash
read -rs "OPENAI_API_KEY?OpenAI API key: " && export OPENAI_API_KEY   # zsh

.venv/bin/python mcp/client.py --openai       # MCPAgent
.venv/bin/python pydantic/agent.py --openai   # the Pydantic Agent
node vercel/agent.ts --openai                 # the Vercel Agent
```

Prompts, tool schemas and tool outputs then go to OpenAI and are billed to the key's project.

**Another model for one run.** `--model` picks a different model on the profile's server, and
`--base-url` a different OpenAI-compatible server:

```bash
.venv/bin/python pydantic/agent.py --openai --model gpt-4.1          # another OpenAI model
.venv/bin/python pydantic/agent.py --model qwen/qwen3-coder-30b      # another model in LM Studio
.venv/bin/python pydantic/agent.py --base-url http://otherhost:1234/v1
```

The environment variables a profile names do the same for every run: `OPENAI_MODEL` for the
`openai` profile, and `LOCAL_LLM_MODEL` and `LOCAL_LLM_BASE_URL` for `local` (see "Shared
settings"). The command line wins over the variables, and the variables over the file.

**A different default.** Set `"default"` in `context/models.json` to the profile every agent
should use without a flag, e.g. `"default": "openai"`.

**Another provider.** Add a profile for any OpenAI-compatible server and select it with
`--profile`; the fields are described under "Context" below:

```json
"mistral": {
  "name": "Mistral",
  "base_url": "https://api.mistral.ai/v1",
  "model": "mistral-large-latest",
  "api_key_env": "MISTRAL_API_KEY",
  "timeout": 60
}
```

```bash
.venv/bin/python pydantic/agent.py --profile mistral
```

MCPAgent also needs the server to support the `/v1/responses` endpoint, which OpenAI and LM
Studio do; the Pydantic and Vercel Agents use chat completions. With `-d`, each agent prints the
profile, model and server it is using when it starts.

## Tools

`tools/` holds one folder per tool: a `tool.json` manifest, the script and its `README.md`.
Every agent scans this folder at startup, so a tool added there is available to all three
without code changes. [tools/README.md](tools/README.md) describes the manifest and how to add
a tool. Current tools: `shell` (ls, cat, grep, find, sed, make, gcc, git and more, in a sandbox),
`ed` (edit files), `pr` (open a pull request, also called a merge request or MR), `memory` (notes
kept between runs), `time`, `sysinfo`, `doxy` (checks Doxygen documentation of C/C++ sources) and
`pr_gate` (Pull Request Gate: a merge gate that quizzes a developer on their pull request; see
[gatekeepers/pr/README.md](gatekeepers/pr/README.md)).
Tools that take a path only reach the folders named in
[`context/paths.json`](context/paths.json).

```bash
bash tools/time/time.sh --timezone UTC  # run a tool's script by hand
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
  `memory_index` names the memory index every agent loads (see "Memory" below), `save_on_exit`
  turns on the save turn before exit, and `names` gives each agent its name (`mcp`, `dantic`,
  `vercel`), which it answers with when asked.
- `context/instructions.json` also holds `identity`, the line naming the agent at the top of its
  instructions, and `on_exit`, the prompt of the save turn before exit.
- `context/output.json`: the terminal layout: `width` (120) and `show_time` (true). See
  "Terminal output" below.
- `context/clang-format.yaml`: the C/C++ style (4-space indents, function braces on their own
  line, 120 columns), used by `clang-format` in the shell and by `pr`, which formats the changed
  files before opening a pull request. A project's own `.clang-format` wins.
- `context/clang-tidy.yaml`: the C/C++ checks for `clang-tidy` in the shell (likely bugs and
  unsafe patterns, not style); Fedora's clang-tidy enables none on its own. A project's own
  `.clang-tidy` wins.
- `context/paths.json`: the folders the tools may use and their access (`r` read, `w` write,
  `x` execute): `core_dump` is `rwx`, `tools` is `r`, `memory` (`.memory/`) is `rw`. Every tool that takes a path checks
  it with
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
	"base_url": "http://boba:1234/v1",
	"base_url_env": "LOCAL_LLM_BASE_URL",
	"model": "qwen/qwen3-coder-30b",
	"model_env": "LOCAL_LLM_MODEL",
	"api_key_env": "LOCAL_LLM_API_KEY",
	"api_key": "lm-studio",
	"timeout": 300
  },
  "openai": {
	"name": "OpenAI",
	"base_url": "https://api.openai.com/v1",
	"model": "gpt-4.1-mini",
	"model_env": "OPENAI_MODEL",
	"api_key_env": "OPENAI_API_KEY",
	"error_hints": "openai",
	"timeout": 60
  }
}
```

| Field                       | Meaning                                                                                                                                                          |
|-----------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `base_url`, `model`         | Required. An OpenAI-compatible server and a model id it serves (MCPAgent also needs the server to support `/v1/responses`)                                       |
| `base_url_env`, `model_env` | Optional environment variables that override `base_url` and `model`                                                                                              |
| `api_key_env`               | Environment variable holding the key; a profile never reads another profile's key                                                                                |
| `api_key`                   | Fallback when `api_key_env` is unset, for servers that need no real key; never put a secret here                                                                 |
| `timeout`                   | Request timeout in seconds; 300 leaves time for LM Studio to load a model                                                                                        |
| `model_auto`                | Ask the server which model is loaded (LM Studio's `/api/v0/models`) and use it; `model` is the fallback when none is loaded. `--model` and `model_env` still win |
| `name`                      | Display name in the chat banner and error messages                                                                                                               |
| `error_hints`               | `"openai"`: on a failed request, MCPAgent gives OpenAI's advice (key, quota, billing); otherwise it points at the server and the model                           |

All three agents take the same options: `--profile NAME` (or the shortcuts `--local` and
`--openai`) picks a profile, and `--model` / `--base-url` override it for one run. Precedence is
the command line, then the `*_env` variables, then the file. To add a model, add a profile (for
example `"mistral": {...}`) and pass `--profile mistral`.

## Memory

The agents keep notes between runs in `.memory/`: what they learned about a project, decisions
made, and the user's preferences. Any agent can read what another saved.

- The folder is named `memory` in `context/paths.json`, with read and write access but no
  execute, so nothing stored there can run.
- The [`memory`](tools/memory/README.md) tool saves, reads and forgets the notes, one topic per
  file (for example `core_dump.md`, `preferences.md`), each kept short and up to date.
- `.memory/index.md` lists the topics, one line each. Every agent loads it into its instructions
  at start-up (`memory_index` in `context/agent.json`), then reads only the notes it needs.
- Never secrets: no keys, passwords or tokens.

Before an interactive chat ends with `exit`, the agent gets one more turn to save anything lasting
it has not saved yet (`save_on_exit` in `context/agent.json`, with the `on_exit` prompt in
`context/instructions.json`). It is skipped after a single exchange with no tool call, by Ctrl+C,
and in `--prompt` runs; when there is nothing to save, the model says so and calls no tool.

The notes are local to this machine: git ignores `.memory/`. Delete a note, or the whole folder,
to make the agents forget.

## Terminal output

All three agents print a turn the same way, so their output can be compared line for line. By
default, a dark gray spinner runs while the model thinks (`Thinking…`) or a tool runs (`Running
shell…`), and only the answer and the response time are printed:

```text
You > What time is it in Tokyo?

It's 17:03 JST (UTC+9) in Tokyo, Wednesday.
Response time: 3.0s · tokens: 3,557 in, 48 out · 2 model calls

You >
```

`-d` (`--debug`) shows everything instead of the spinner, the banner and every tool call and result:

```text
Local model server model: qwen/qwen3-coder-30b @ http://boba:1234/v1, 12 tools (sequential)
→ pr_gate({})
← pr_gate: Quiz service: running at http://minion:8000
  PR #1 'Compute pi using the C math library' by emichael72 at 135c35a: quiz waiting, merge blocked: …

The quiz service is running. Pull request #1 is still waiting for its quiz, so its merge is blocked.
Response time: 6.3s · tokens: 4,313 in, 53 out · 2 model calls
```

1. **Only the model's answer is in the terminal's normal color.** Everything else (the spinner, and
   with `-d` the banner, the chat hints, tool calls `→`, results `←` and failures `✗`; always the
   `You >` prompt and the response time) is dark gray (ANSI 90). Errors are red. The spinner shows
   only on a terminal; piped output gets no spinner.
2. **Everything fits in `width` columns** (`context/output.json`, 120), or in the terminal if it is
   narrower. Gray lines wrap with a two-space indent; the streamed answer wraps between words as
   it arrives. A word longer than the width, such as a URL, is never broken.
3. **Links are clickable.** On a terminal, a Markdown link `[text](url)` shows as *text* and a web
   address as itself, both as clickable OSC 8 links (VS Code's terminal, iTerm2, GNOME Terminal and
   others), in the answer and in the gray tool lines. Links are bright cyan, the one vivid color, so
   a link such as the pull request's quiz stands out. A link is never split while the answer
   streams. `"links": false` turns this off; piped output is always plain.
4. **The answer has one blank line before it, and the response time right under it**, then one
   blank line before the next prompt. Text the model writes between tool calls is set off by one
   blank line. A tool call prints together with its result, in the order they ran (with `-d`).
5. **Each response ends with its time and tokens**: `Response time: N.Ns` from sending the prompt
   to the end of the answer, tools included; then the tokens the model calls used, as the server
   reports them: *in* (sent to the model: instructions, tool list, history, tool results, added
   up over every call) and *out* (generated), and how many model calls the answer took.
   `"show_time": false` and `"show_tokens": false` turn them off; `tokens: not reported` means
   the server sent no counts.

Each agent implements this in an `Output` class (`mcp/mcpagent/client/output.py`, `pydantic/pydantic_agent/output.py`,
`vercel/vercelagent/agent.ts`), with tests that check the wrapping and the timing line.

## MCPAgent

An MCP server that exposes shell scripts as tools, plus a terminal client that drives the model.
See [mcp/README.md](mcp/README.md).

```bash
.venv/bin/python mcp/server.py                          # terminal 1: MCP server on 127.0.0.1:6275
.venv/bin/python mcp/client.py                          # terminal 2: chat, default model profile
.venv/bin/python mcp/client.py --prompt "Time now"      # one prompt and exit
.venv/bin/python -m unittest discover -s mcp/tests      # offline tests
```

## Pydantic

The same agent with pydantic-ai running the loop; the tools run in-process. See
[pydantic/README.md](pydantic/README.md).

```bash
.venv/bin/python pydantic/agent.py                                # interactive chat
.venv/bin/python pydantic/agent.py --prompt "Time now" --history  # one prompt + raw message dump
.venv/bin/python pydantic/agent.py --parallel                     # run a response's tool calls concurrently
.venv/bin/python -m unittest discover -s pydantic/tests           # offline tests
```

## Vercel

The same agent in TypeScript with the AI SDK's `ToolLoopAgent`; Node runs the `.ts` files
directly. See [vercel/README.md](vercel/README.md).

```bash
node vercel/agent.ts                                # interactive chat
node vercel/agent.ts --prompt "Time now" --history  # one prompt + raw message dump
node vercel/agent.ts --parallel                     # run a response's tool calls concurrently
npm --prefix vercel test                            # offline tests
npm --prefix vercel run lint                        # Oxlint: likely bugs
```

If `install.sh` fetched Node.js into `.node/`, use it for these commands: `.node/bin/node
vercel/agent.ts`, or put it first in `PATH` (`export PATH="$PWD/.node/bin:$PATH"`), which npm also
needs.

## Tests

Each agent has offline tests: a scripted stand-in model, with the real tools. All three replay
the same turn, [`tests/scenario.json`](tests/scenario.json): the model asks for a few tool calls
in one response (a shell command, one that must fail, the time), and each must end as the file
says. Change a call there and all three agents are tested on it.

```bash
.venv/bin/python -m unittest discover -s mcp/tests
.venv/bin/python -m unittest discover -s pydantic/tests
npm --prefix vercel test
```

## Shared settings

The model settings live in `context/models.json` (see "Context" above). With the default `local`
profile, these environment variables override it in all three agents:

| Variable             | Default                |
|----------------------|------------------------|
| `LOCAL_LLM_BASE_URL` | `http://boba:1234/v1`  |
| `LOCAL_LLM_MODEL`    | `qwen/qwen3-coder-30b` |
| `LOCAL_LLM_API_KEY`  | `lm-studio`            |

To check that the model server is up, request its model list, e.g. `curl -s http://boba:1234/v1/models`
for the default `local` profile.

## License

MIT; see [LICENSE](LICENSE).
