# Git

Runs a read-only git command in a repository inside the allowed folders
([`../allowed_paths.json`](../allowed_paths.json)), such as `core_dump`. git runs in the given
folder, so paths in the arguments are relative to it. Output is cut to 200 lines.

**Usage Example:**

```bash
python3 git/git.py core_dump log --args "-5 --oneline"
python3 git/git.py core_dump diff --args "HEAD~1 -- src/main.c"
python3 git/git.py core_dump/src blame --args "-L 70,80 main.c"
```

- **Subcommands:** `status`, `log`, `show`, `diff`, `blame`, `ls-files`, `shortlog`, `grep`,
  `describe`, `rev-parse`, and `branch` / `tag` for listing only (`-a`, `-r`, `-v`, `--list`, ...).
  Nothing is committed, checked out, reset or pushed.
- **Refused arguments:** those that could read outside the repository or run another program:
  `--no-index`, `-C`, `-c`, `--config-env`, `--git-dir`, `--work-tree`, `--output`, `--ext-diff`,
  `--textconv`, `-O` / `--open-files-in-pager`, `--upload-pack`, `--receive-pack`, `--paginate`.
- git runs without a pager, editor or credential prompts.

It replaces the former `git_log` tool: `log --args "-10 --date=short --format='%h %ad %s'"` gives
the same list.
