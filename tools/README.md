# Tools

The tools every agent can use. Each sub-folder is one tool: a `tool.json` manifest, the script it
runs, and a `README.md` documenting it. The agents scan this folder when they start, so a tool
added here is available to all of them without code changes:

- **mcp** — the MCP server reads `tools_dir` from `mcp/mcpagent/jsons/mcpagent.json` and serves every manifest
  over MCP, with each tool's `README.md` as an MCP resource.
- **pydantic** — `pydantic/pydantic_agent/toolset.py` turns each manifest into a pydantic-ai tool.
- **vercel** — `vercel/vercelagent/tools.ts` turns each manifest into an AI SDK tool.

| Tool | Runs | Parameters | Result |
| --- | --- | --- | --- |
| `time` | Bash / `date` | `timezone` (optional, IANA name) | The current date and time |
| `sysinfo` | Python | `section` (optional) | The system, CPU utilization and load, memory, disks, network, GPU, busiest processes and software versions (Python, gcc, ...) |
| `shell` | Python / `bwrap` | `cwd`, `command` | Runs a command line (ls, cat, grep, find, sed, make, gcc, git, ... joined with `\|`, `&&`, `;`) in an allowed folder, inside a sandbox; `help` lists the commands |
| `ed` | Python | `path`, `action`, `old`, `new`, `all`, `start`, `end`, `line` (by action) | Edits a file: replace exact text, replace or delete lines, insert, or write a whole file; shows the changed lines. `hex` (`offset`, `length`) only reads: part of any file as a hex dump |
| `pr` | Python / `git`, `gh` | `path`, `action` (open, sync, check), `title`, `body`, `branch` (optional) | Opens a pull request (also called a merge request, MR): formats the changed C/C++ files, runs the gate's checks (refusing a change that fails them), puts the changes on a new branch, pushes it and proposes it for the default branch; returns the gate's quiz link. `check` runs only the gate's checks |
| `skill` | Python | `name` (optional) | A skill's step-by-step procedure from `skills/`, or the list of skills; their names and descriptions are in every agent's instructions |
| `memory` | Python | `action` (save, read, forget), `topic`, `text`, `summary`, `replace` | The agents' notes between runs, in `.memory/`; the index is loaded into every agent's instructions |
| `doxy` | Bash / `doxygen` | `paths` (files or folders, space-separated) | Doxygen documentation problems as `file:line: message`, or "All documented" |
| `pr_gate` | Python (shared `.venv`) | `pr` (optional) | Open PRs in the gated repository and their gate state; the gate itself is [gatekeepers/pr](../gatekeepers/pr/README.md) |

Every path a tool takes (`shell`, `ed`, `doxy`, `pr`) must be inside the folders named in
[`context/paths.json`](../context/paths.json), with the access the tool needs; see "Allowed paths"
below. Most file and build work goes through `shell`; see [shell/README.md](shell/README.md).

## The manifest

`time/tool.json`:

```json
{
  "description": "Returns the current date and time, optionally in a given time zone.",
  "command": "bash",
  "args": ["time/time.sh"],
  "params": [
    {"name": "timezone", "type": "string", "description": "IANA time zone such as Europe/London or Asia/Tokyo; omit it for the machine's local time", "style": "flag", "required": false}
  ],
  "resource": "time/README.md"
}
```

| Field | Meaning |
| --- | --- |
| *(folder name)* | The tool's name, as the model sees it. Use `[a-z0-9_-]`. |
| `description` | What the model reads to decide when to use the tool. |
| `command`, `args` | The program and its fixed arguments, e.g. `bash time/time.sh`. |
| `params` | Arguments the model supplies. Each has `name`, `type` (`string`, `integer`, `number`, `boolean`), `description`, `style` and optionally `required`. |
| `style` | `flag` passes `--<name>=<value>`; `positional` passes the bare value after `--` (see "Parameters"). Default `flag`. |
| `required` | Defaults to `true`. An optional parameter the model omits is not passed at all, so the script's own default applies. |
| `env` | Optional environment variables for the command. |
| `timeout` | Optional seconds the agents wait for the tool (default 30), for tools that wait on something, such as `pr`. |
| `resource` | Optional documentation file, served by MCPAgent as an MCP resource. |

## Parameters

Every agent calls a tool the same way: its `command` and `args`, then each flag parameter the model
gave as one argument, `--<name>=<value>`, then `--` and the positional parameters' values, in
`params` order:

```bash
python3 ed/ed.py --old=-Wall --new='-Wall -Wextra' -- core_dump/Makefile
```

With the value in the same argument as its option, a value may start with `-` or span lines and
still reach the tool unchanged, and nothing after `--` is taken for an option. An optional
parameter the model omits is not passed. A boolean arrives as `true` or `True`.

The tools read this with their usual parsers, which also accept `--<name> <value>` by hand:

- **Python** (`ed`, `memory`, `pr`, `shell`, `sysinfo`): argparse, through
  [`common/cli.py`](common/cli.py)'s `ToolArgumentParser`, which prints a command-line mistake as
  `Error: <reason>` with exit status 1, like any other failure, and adds `-v`/`--version`. Each
  tool is one class with a `main()` that prints the result and returns the exit status. They run
  under the system's `python3`, which may be outside the `.venv` and as old as 3.9 (RHEL 9), so
  they use only the standard library, the gatekeepers and `common/`.
- **Bash** (`doxy`, `time`): a `parse_args()` loop that accepts `--<name>=<value>` and `--`, with
  `-v`/`--version` and `-h`/`--help`. `pr_gate` runs `gatekeepers/pr/pr_gate.sh`, which passes its
  parameters on to the gate's argparse command line.

**Paths:** every path in a manifest (`args`, `resource`) is relative to this `tools/` folder, and
this folder is also the working directory when a tool runs.

**Allowed paths:** a path the *model* passes must lie inside one of the folders named in
[`context/paths.json`](../context/paths.json), and starts with that folder's name. Each folder has
access rights, inherited by everything inside it; `subpaths` override them for a sub-folder:

```json
"paths": {
  "core_dump": {"path": "~/projects/core_dump", "access": "rwx"},
  "tools": {"path": "tools", "access": "r"}
}
```

| Right | Means |
| --- | --- |
| `r` read | `shell` mounts the folder (read-only without `w`); `doxy` may read it |
| `w` write | `shell` mounts it writable; `ed` may change files there |
| `x` execute | `shell` may run programs found there (`./core_dump`) and `make` |

A path given as a plain string is read-only. Folders are absolute, start with `~`, or are relative
to the agents repository. So `ed` takes `core_dump/src/main.c` but refuses `tools/...` (read-only),
and nothing outside those folders is reachable (`..` and symbolic links are resolved before the
check). The checks live in the file-system gate, [`gatekeepers/fs/fs_gate.py`](../gatekeepers/fs/fs_gate.py):
Python tools load its `FsGate` and ask it, and Bash tools run it with the repository root on
`PYTHONPATH`: `python3 -m gatekeepers.fs.fs_gate <path> [--dir|--file] [--need r|w|x]`, which
prints the absolute path and the path as shown, or an error. `gatekeepers/fs/` has
no `tool.json`, so it is not offered as a tool. `paths.json` is read on every call.

Each agent validates the model's arguments against the schema built from `params` before running
the command (`jsonschema` in mcpagent and pydantic, zod in vercel). The agent also sets `AGENT_NAME`
(`MCP Agent`, `Pydantic Agent` or `Vercel Agent`) in the tool's environment. A nonzero exit is
reported to the model as a tool failure, with the script's output as the message.

## Adding a tool

The top-level `description` may be a string or a list of strings. Each agent joins a list with
spaces before sending the description to the model.

1. Create a folder named after the tool, e.g. `tools/uptime/`.
2. Add the script, e.g. `uptime/uptime.sh`. Print the result to stdout, and exit nonzero
   with an explanation on failure.
3. Add `uptime/tool.json`, with paths relative to `tools/`:

   ```json
   {
     "description": "Shows how long the machine has been running.",
     "command": "bash",
     "args": ["uptime/uptime.sh"],
     "params": [
       {"name": "pretty", "type": "boolean", "description": "Say it in words, e.g. up 3 hours", "style": "flag", "required": false}
     ],
     "resource": "uptime/README.md"
   }
   ```

4. Add `uptime/README.md` describing it.
5. Restart the agents (and the MCPAgent server). Each agent lists the tools it loaded; ask one to use
   the new tool.

A new tool reads its parameters as "Parameters" describes; a Python tool builds its parser with
`ToolArgumentParser`.

A tool that takes a path must check it with the file-system gate (`gatekeepers/fs/fs_gate.py`, see
"Allowed paths"), with the access it needs. A tool named like a command in
[`shell/commands.json`](shell/commands.json) takes that command over: the shell then refuses it
and points the model to the tool.

Tools run with the permissions of the user running the agent, and are not sandboxed.
