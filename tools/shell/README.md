# Shell

The shell tool reads files, searches code, builds projects, and runs tests inside a bubblewrap sandbox. It can reach
only the folders permitted in `context/paths.json` and has no network access.

## Use it

From the repository root:

~~~bash
python3 tools/shell/shell.py --cwd=core_dump --command="cat -n src/main.c"
python3 tools/shell/shell.py --cwd=core_dump --command="make && make check"
python3 tools/shell/shell.py --cwd=core_dump --command="git diff"
python3 tools/shell/shell.py --command=help
~~~

Help lists the available commands and folders. [commands.json](commands.json) defines the command list; a listed
command that is not installed on the machine is left out of help and refused.

## What is different from a normal shell?

Pipes and `&&`, `||`, and `;` are supported. Redirection, background jobs, command substitution, subshells, and
multiline commands are refused. Use [ed](../ed/README.md) to write files.

Git is mostly for inspection: status, logs, diffs, and similar reads. `git restore` can undo uncommitted edits. Syncing,
creating branches, committing, and opening PRs belong to the [pr tool](../pr/README.md).

Programs inside project folders, Makefiles, and ninja builds need execute permission (`x`). make and ninja run in the
folder holding their build files (`cd build && ninja`), not through `-C` or `-f`. Build variables such as CFLAGS can be
supplied before make, and a time zone before date (`TZ=Asia/Tokyo date`; the zone must be installed). Other
environment assignments are refused.

## What the sandbox sees

The permitted folders appear under `/work/<name>`, writable only where allowed. System programs are read-only. Temporary
files are private to the call, and other home-directory files are absent. Git hooks and repository configuration are
protected.

The shared clang-format and clang-tidy settings supply defaults when the project has none of its own. Calls stop after
25 seconds and limit displayed output to 300 lines.

## Environment

The sandbox starts from a fixed environment. The `environment` entry in [commands.json](commands.json) adds to it:

~~~json
{
  "environment": {
    "path": ["/usr/local/bin"],
    "variables": {"RUN_BY_AGENT": "1", "AGENT_NAME": "${AGENT_NAME}"}
  }
}
~~~

`path` lists folders searched before `/usr/bin`, as the sandbox sees them: under `/usr`, or `/work/<name>/...` in a
folder with `x` access. A folder that does not exist, or lacks that access, is skipped. Commands must still be listed
in `commands` to run.

`variables` are exported to every command, so a script can tell that an agent runs it. `${NAME}` takes NAME from the
shell tool's own environment, where the agents set `AGENT_NAME`. The sandbox's own variables (`PATH`, `HOME`, `GIT_*`,
`LD_*`, and the rest of its fixed set) cannot be replaced.

## Limits

The sandbox contains the command's execution. Files it changes may later be built or run outside it, so review the
submitted changes normally.

Requires bubblewrap (`bwrap`). See [gatekeepers](../../gatekeepers/README.md) for folder permissions.
