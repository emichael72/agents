# Shell

Runs a command line for the agents in one of the allowed folders, inside a sandbox. One tool
instead of many: the commands it offers are listed in [`commands.json`](commands.json), and the
folders it can reach, with their access, in [`../../context/paths.json`](../../context/paths.json).

**Usage Example:**

```bash
python3 shell/shell.py --cwd core_dump --command "grep -rn print_pi src | wc -l"
python3 shell/shell.py --cwd core_dump --command "make && ./core_dump -dp && make clean"
python3 shell/shell.py --cwd core_dump --command "git log --oneline -5"
python3 shell/shell.py --cwd core_dump --command help        # the folders, their access and the commands
```

Inside, the allowed folders appear as `/work/<name>`, and the command starts in `cwd`, or in the
first folder of `context/paths.json` when `cwd` is omitted (for commands such as `whoami` or `bc`
that do not depend on a folder). The agents'
C/C++ style, [`context/clang-format.yaml`](../../context/clang-format.yaml), is mounted at
`/work/.clang-format`, so `clang-format` uses it for every project that has no `.clang-format` of
its own; likewise [`context/clang-tidy.yaml`](../../context/clang-tidy.yaml) at `/work/.clang-tidy`
for `clang-tidy`. Output shows
`/work/<name>` as `<name>`; it stops after 25 seconds and 300 lines.

## The sandbox

Every command line runs in [bubblewrap](https://github.com/containers/bubblewrap) (`bwrap`), so the
limits are enforced by the kernel, whatever the command or its arguments:

| Inside the sandbox | |
| --- | --- |
| `/usr` | read-only (the programs) |
| `/work/<name>` | each allowed folder: read-only, or writable with `w` access; sub-folder overrides apply |
| `/tmp` | private and empty, deleted afterwards (`HOME` is `/tmp/home`) |
| everything else | absent: no `/home`, no `/etc` (except time zone and library cache), no network |

`.git/config` and `.git/hooks` stay read-only even in writable folders, and git runs with hooks and
fsmonitor off, so nothing planted in a repository runs later outside the sandbox. A script the
model writes cannot change the tools or `context/`: they are read-only or absent in the sandbox.

**What the sandbox cannot cover:** files the model changes in a writable folder that *you* later
use outside it, such as a Makefile you then run with `make`, or code you build and run. Review
such changes as you would any contributor's.

## The command check

Before running, the line is checked; the sandbox is the boundary, the check keeps the model to
what is offered:

- Every command (after `|`, `||`, `&&` or `;`) must be in `commands.json`, or be a program inside a
  folder with `x` access (`./core_dump`).
- **A command that is also its own tool is refused**, naming the tool: the dedicated tool wins. For
  example, adding `tools/git/tool.json` would take `git` out of the shell.
- No redirection (`<`, `>`), background jobs (`&`), subshells or groups, `$(...)`, backticks or
  line breaks. `2>&1` and `2>/dev/null` are accepted and dropped: errors already appear in
  the output, interleaved with it. `cd` is followed, and must stay in the allowed folders.
- **`git` only reads:** `status`, `log`, `show`, `diff`, `blame`, `grep`, `ls-files`, `restore` (to
  undo uncommitted edits) and listing branches and tags. Committing, branching, merging and syncing
  belong to the [`pr`](../pr/README.md) tool, which keeps the repository in the state it expects
  (and the sandbox has no network to push or pull anyway).
- Commands marked `"needs": "x"` (`make`, which runs the Makefile) need execute access where they
  run, and may not use `-C` or `-f` to point elsewhere.

The check is stricter than bash, never looser: it may refuse an unusual line, but it never passes
a line in which bash would find a command it did not see. Some allowed commands can still start
others (`find -exec`, `awk`'s `system()`), only ever inside the sandbox.

## Access rights

From `context/paths.json`, for a folder and everything in it:

| Right | Shell | ed |
| --- | --- | --- |
| `r` | the folder is mounted (read-only without `w`) | |
| `w` | the folder is mounted writable | required |
| `x` | `./program` and `make` may run there | |

Requires bubblewrap (`sudo dnf install bubblewrap` on Fedora, `sudo apt install bubblewrap` on
Debian/Ubuntu).
