# Shared tools

All three agents use the scripts in this folder. Each tool has a `tool.json` that describes its purpose, arguments,
command, and documentation.

MCPAgent's child server exposes them over MCP. Pydantic and Vercel turn the same definitions into framework tools and
run the scripts locally.

## Pick a tool

| Tool                         | Use it for                                      |
|------------------------------|-------------------------------------------------|
| [shell](shell/README.md)     | Read files, search, build, and run tests        |
| [ed](ed/README.md)           | Edit or create text files                       |
| [pr](pr/README.md)           | Sync a repository, check changes, and open a PR |
| [pr_gate](pr_gate/README.md) | See gate status, quiz links, and history        |
| [doxy](doxy/README.md)       | Check Doxygen documentation                     |
| [memory](memory/README.md)   | Keep notes between chats                        |
| [skill](skill/README.md)     | Read a procedure for the current task           |
| [time](time/README.md)       | Get the date and time                           |
| [sysinfo](sysinfo/README.md) | Inspect the machine running the tools           |

All command examples in these pages run from the **repository root**.

## Paths and permissions

Use the folder names in [context/paths.json](../context/paths.json). For example, `core_dump/src/main.c` refers to a
file inside the configured core_dump checkout.

`r` allows reading, `w` allows writing, and `x` allows running programs and Makefiles. Subfolders can have different
permissions. Tools resolve paths before checking them, including parent references and symbolic links.

The [file-system gate](../gatekeepers/README.md) checks access. The shell adds a bubblewrap sandbox with no network.
Other tools run as the current user and apply their own checks; there is no single sandbox around every tool.

## Add a tool

Create a folder under `tools/` containing a script, a README, and a manifest. For example:

~~~json
{
  "description": "Prints the system uptime.",
  "command": "uptime",
  "params": [],
  "resource": "uptime/README.md"
}
~~~

The folder name becomes the tool name. `command` and optional `args` say what to run. `params` defines the arguments;
optional `env` and `timeout` configure execution.

Script and resource paths are relative to `tools/`, which is also the working directory when agents run a tool. Return
useful output on stdout and a nonzero exit status for a failure. A path-taking tool must use the file-system gate.

Restart the agent to discover the new tool.

## How arguments reach a script

The agents pass flags as `--name=value` and put positional values after `--`. This keeps values beginning with a dash
from being mistaken for another option:

~~~bash
python3 tools/ed/ed.py --old=-Wall --new='-Wall -Wextra' -- core_dump/Makefile
~~~

The model's arguments are checked against the manifest schema before execution. The Python scripts use a shared parser
in `tools/common/cli.py`; `--help` shows each script's options.

The PR gate also runs these tools: shell handles its builds/tests and doxy checks its documentation. The gate and pr
tool share the same inspection code.

Skills describe how to use tools in order. Read the [skill guide](skill/README.md) to add a procedure.
