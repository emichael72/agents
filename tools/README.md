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
| `greet_user` | Bash | `name` (optional) | A greeting for `name`, or for the shell user (`$USER`) |
| `get_rand` | Bash | `max` (optional, default 100) | A random number from 1 to `max` |
| `count_lines` | Bash / `wc` | `file` | The file's line count |
| `echo_message` | Python | `message`, `repeat` (optional) | The message in uppercase, repeated |
| `get_system_info` | Bash / `uname` | none | Hostname, OS, kernel release and CPU architecture |
| `current_time` | Bash / `date` | `timezone` (optional, IANA name) | The current date and time |
| `calculate` | Python | `expression` | The value of an arithmetic expression (parsed safely, no `eval`) |
| `list_files` | Bash / `ls` | `path` (optional) | A folder's entries with sizes and dates (up to 200) |
| `search_text` | Bash / `grep` | `pattern`, `path` (optional) | Matching lines as `file:line:text` (up to 50) |
| `disk_usage` | Bash / `du` | `path` (optional) | A file or folder's total size and file count |
| `git_log` | Bash / `git` | `count` (optional, 1-50), `path` (optional) | Recent commits: hash, date, subject |
| `doxy_check` | Bash / `doxygen` | `paths` (files or folders, space-separated) | Doxygen documentation problems as `file:line: message`, or "All documented" |
| `mr_quiz` | Python (shared `.venv`) | `pr` (optional) | Open PRs in the quiz-gated repository and their quiz state; see [mr_quiz/README.md](mr_quiz/README.md) |

## The manifest

`greet_user/tool.json`:

```json
{
  "description": "Prints a greeting for the given name. Omit name to greet the current user (for example, when asked to greet me).",
  "command": "bash",
  "args": ["greet_user/greet_user.sh"],
  "params": [
    {"name": "name", "type": "string", "description": "Name of the user to greet; omit it to greet the current user", "style": "flag", "required": false}
  ],
  "resource": "greet_user/README.md"
}
```

| Field | Meaning |
| --- | --- |
| *(folder name)* | The tool's name, as the model sees it. Use `[a-z0-9_-]`. |
| `description` | What the model reads to decide when to use the tool. |
| `command`, `args` | The program and its fixed arguments, e.g. `bash greet_user/greet_user.sh`. |
| `params` | Arguments the model supplies. Each has `name`, `type` (`string`, `integer`, `number`, `boolean`), `description`, `style` and optionally `required`. |
| `style` | `flag` passes `--<name> <value>`; `positional` passes the bare value, in `params` order. Default `flag`. |
| `required` | Defaults to `true`. An optional parameter the model omits is not passed at all, so the script's own default applies. |
| `env` | Optional environment variables for the command. |
| `resource` | Optional documentation file, served by MCPAgent as an MCP resource. |

**Paths:** every path in a manifest is relative to this `tools/` folder, and this folder is also
the working directory when a tool runs. So `count_lines` takes paths such as
`greet_user/README.md`.

Each agent validates the model's arguments against the schema built from `params` before running
the command (`jsonschema` in mcpagent and pydantic, zod in vercel). The agent also sets `AGENT_NAME`
(`MCP Agent`, `Pydantic Agent` or `Vercel Agent`) in the tool's environment. A nonzero exit is
reported to the model as a tool failure, with the script's output as the message.

## Adding a tool

1. Create a folder named after the tool, e.g. `tools/disk_usage/`.
2. Add the script, e.g. `disk_usage/disk_usage.sh`. Print the result to stdout, and exit nonzero
   with an explanation on failure.
3. Add `disk_usage/tool.json`, with paths relative to `tools/`:

   ```json
   {
     "description": "Shows how much disk space a folder uses.",
     "command": "bash",
     "args": ["disk_usage/disk_usage.sh"],
     "params": [
       {"name": "path", "type": "string", "description": "Folder to measure, relative to the tools folder", "style": "positional"}
     ],
     "resource": "disk_usage/README.md"
   }
   ```

4. Add `disk_usage/README.md` describing it.
5. Restart the agents (and the MCPAgent server). Each agent lists the tools it loaded; ask one to use
   the new tool.

Tools run with the permissions of the user running the agent, and are not sandboxed.
