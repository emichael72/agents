# Tools

The tools every agent can use. Each sub-folder is one tool: a `tool.json` manifest, the script it
runs, and a `README.md` documenting it. The agents scan this folder when they start, so a tool
added here is available to all of them without code changes:

- **mcpagent** — the MCP server reads `tools_dir` from `mcpagent/server/server.jsonc` and serves every manifest
  over MCP, with each tool's `README.md` as an MCP resource.
- **pydantic** — `pydantic/toolset.py` turns each manifest into a pydantic-ai tool.
- **vercel** — `vercel/tools.ts` turns each manifest into an AI SDK tool.

| Tool | Runs | Parameters | Result |
| --- | --- | --- | --- |
| `greet` | Bash | `name` (optional) | A greeting for `name`, or for the shell user (`$USER`) |
| `rand` | Bash | `max` (optional, default 100) | A random number from 1 to `max` |
| `time` | Bash / `date` | `timezone` (optional, IANA name) | The current date and time |
| `sysinfo` | Bash / `uname` | none | Hostname, OS, kernel release and CPU architecture |
| `calc` | Python | `expression` | The value of an arithmetic expression (parsed safely, no `eval`) |
| `ls` | Python | `path` (optional) | A folder's entries with sizes and dates (up to 200); no path lists the allowed folders |
| `cat` | Python | `path`, `start`, `count` (optional) | A text file's lines, numbered, up to 200 per call |
| `ed` | Python | `path`, `action`, `old`, `new`, `all`, `start`, `end`, `line` (by action) | Edits a file: replace exact text, replace or delete lines, insert, or write a whole file; shows the changed lines |
| `wc` | Bash / `wc` | `file` | The file's line count |
| `search_text` | Bash / `grep` | `pattern`, `path` (optional) | Matching lines as `file:line:text` (up to 50) |
| `df` | Bash / `du` | `path` (optional) | A file or folder's total size and file count |
| `git` | Python / `git` | `path`, `command`, `args` (optional) | A read-only git command's output (log, status, diff, show, blame, ...) |
| `make` | Python / `make` | `path`, `target` (optional) | Whether the build succeeded, and its output |
| `gcc` | Python / `gcc` | `sources`, `output`, `flags` (optional) | Warnings and errors (a check), or a built program |
| `doxy` | Bash / `doxygen` | `paths` (files or folders, space-separated) | Doxygen documentation problems as `file:line: message`, or "All documented" |
| `mr_gate` | Python (shared `.venv`) | `pr` (optional) | Open PRs in the quiz-gated repository and their quiz state; see [mr_gate/README.md](mr_gate/README.md) |

Every path a tool takes (`ls`, `cat`, `ed`, `wc`, `search_text`, `df`, `git`, `make`,
`gcc`, `doxy`) must be inside the folders named in [`context/paths.json`](../context/paths.json);
see "Allowed paths" below.

## The manifest

`greet/tool.json`:

```json
{
  "description": "Prints a greeting for the given name. Omit name to greet the current user (for example, when asked to greet me).",
  "command": "bash",
  "args": ["greet/greet.sh"],
  "params": [
    {"name": "name", "type": "string", "description": "Name of the user to greet; omit it to greet the current user", "style": "flag", "required": false}
  ],
  "resource": "greet/README.md"
}
```

| Field | Meaning |
| --- | --- |
| *(folder name)* | The tool's name, as the model sees it. Use `[a-z0-9_-]`. |
| `description` | What the model reads to decide when to use the tool. |
| `command`, `args` | The program and its fixed arguments, e.g. `bash greet/greet.sh`. |
| `params` | Arguments the model supplies. Each has `name`, `type` (`string`, `integer`, `number`, `boolean`), `description`, `style` and optionally `required`. |
| `style` | `flag` passes `--<name> <value>`; `positional` passes the bare value, in `params` order. Default `flag`. |
| `required` | Defaults to `true`. An optional parameter the model omits is not passed at all, so the script's own default applies. |
| `env` | Optional environment variables for the command. |
| `resource` | Optional documentation file, served by MCPAgent as an MCP resource. |

**Paths:** every path in a manifest (`args`, `resource`) is relative to this `tools/` folder, and
this folder is also the working directory when a tool runs.

**Allowed paths:** a path the *model* passes must lie inside one of the folders named in
[`context/paths.json`](../context/paths.json), and starts with that folder's name. Folders are
absolute, start with `~`, or are relative to the agents repository:

```json
"paths": {
  "core_dump": "~/projects/core_dump",
  "tools": "tools"
}
```

So `wc` takes `core_dump/README.md` or `tools/greet/README.md`, and nothing outside
those folders (`..` and symbolic links are resolved before the check). Every tool that takes a
path (`ls`, `cat`, `ed`, `wc`, `search_text`, `df`, `git`, `make`, `gcc`,
`doxy`) checks it with the file-system gate, [`fs_gate/fs_gate.py`](fs_gate/fs_gate.py): Python
tools import it, and Bash tools run `python3 fs_gate/fs_gate.py <path> [--dir|--file]`, which prints
the absolute path and the path as shown, or an error. `fs_gate/` has no `tool.json`, so it is not
offered as a tool. To allow another folder, add a name to `paths.json`; it is read on every call.

Each agent validates the model's arguments against the schema built from `params` before running
the command (`jsonschema` in mcpagent and pydantic, zod in vercel). The agent also sets `AGENT_NAME`
(`MCP Agent`, `Pydantic Agent` or `Vercel Agent`) in the tool's environment. A nonzero exit is
reported to the model as a tool failure, with the script's output as the message.

## Adding a tool

1. Create a folder named after the tool, e.g. `tools/df/`.
2. Add the script, e.g. `df/df.sh`. Print the result to stdout, and exit nonzero
   with an explanation on failure.
3. Add `df/tool.json`, with paths relative to `tools/`:

   ```json
   {
     "description": "Shows how much disk space a folder uses.",
     "command": "bash",
     "args": ["df/df.sh"],
     "params": [
       {"name": "path", "type": "string", "description": "Folder to measure, relative to the tools folder", "style": "positional"}
     ],
     "resource": "df/README.md"
   }
   ```

4. Add `df/README.md` describing it.
5. Restart the agents (and the MCPAgent server). Each agent lists the tools it loaded; ask one to use
   the new tool.

Tools run with the permissions of the user running the agent, and are not sandboxed.
