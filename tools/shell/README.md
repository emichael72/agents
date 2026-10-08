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

Help lists the available commands and folders. [commands.json](commands.json) defines the command list.

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

The sandbox contains the command's execution. Files it changes may later be built or run outside it, so review the
submitted changes normally.

Requires bubblewrap (`bwrap`). See [gatekeepers](../../gatekeepers/README.md) for folder permissions.
