# PR

Opens a pull request (PR) on GitHub with the uncommitted changes of a repository in an allowed
folder with write access ([`../../context/paths.json`](../../context/paths.json)), such as
`core_dump`. Users may call it a merge request (MR); the tool's description tells the model that
this is the same thing, so "open an MR" uses this tool. It is how an agent hands its work to a
person: the change waits on GitHub for review, and in `core_dump` the
[`pr_gate`](../../gatekeepers/pr/README.md) quiz checks that the reviewer understands it before it
can merge.

**Before starting work**, `action: sync` brings the repository's default branch up to date with
GitHub (fast-forward only). The agents' shell has no network access. Syncing first keeps the agents
from working on stale code. The sync action refuses when there are uncommitted changes or local
commits, so it never loses work.
The pull request gate's service also keeps `core_dump` current on its own (`QUIZ_LOCAL_CLONE`), so
`sync` mostly confirms it.

**Before opening**, `action: check` runs the merge gate's own checks on the uncommitted changes and
reports what fails, without committing anything. It copies the files a commit would hold (tracked
and new, not ignored, so no old build output), then, with the gate's settings
(`QUIZ_BUILD_COMMAND`, `QUIZ_TEST_TARGET`, `QUIZ_FAIL_ON_WARNINGS` in
[`gatekeepers/pr/settings.json`](../../gatekeepers/pr/settings.json)), runs `make` and `make check`
in the [`shell`](../shell/README.md) tool's sandbox (a compiler warning fails it) and
[`doxy`](../doxy/README.md) on the changed C/C++ files. It is the same code the gate runs
(`ChangeInspector` in [`gatekeepers/pr/changes.py`](../../gatekeepers/pr/changes.py)), so a change
that passes here passes the gate's build and documentation checks, and the model checks with one
call instead of several. A passing build shows only its summary line; a failing one shows its
warnings and the end of its output.

**Usage Example:**

```bash
python3 pr/pr.py --action=sync -- core_dump     # update main from GitHub first
python3 pr/pr.py --action=check -- core_dump    # the gate's checks on the changes
python3 pr/pr.py --title="Add an e module" --body="Prints Euler's number; make builds cleanly." -- core_dump
```

What it does, every time:

1. Checks the repository: it is on its default branch (usually `main`), has no commits of its own,
   and has changes to submit.
2. Brings the default branch up to date with GitHub (fast-forward only).
3. Formats the changed and new C/C++ files with `clang-format`, using the repository's own
   `.clang-format` if it has one, else the agents' template,
   [`context/clang-format.yaml`](../../context/clang-format.yaml). Only those files are touched,
   and the result lists the ones that changed.
4. Runs the gate's checks, as `action: check` does. If one fails, it stops and reports why: nothing
   is committed, pushed or opened, so a pull request never starts out failing the gate.
5. Creates a new branch (`agent/<title>`, or the `branch` given), commits all changes (respecting
   `.gitignore`), and pushes the branch.
6. Opens the pull request into the default branch, noting which agent opened it, and switches back
   to the default branch; the changes now live on the branch.
7. Waits for the merge gate's check on the new commit (`PR_WAIT_CHECK` in `tool.json`, set to
   `developer-quiz`, for up to `PR_WAIT_SECONDS`, 100) and reports it. With
   [`pr_gate`](../../gatekeepers/pr/README.md) that is the quiz link the reviewer must pass, or the
   documentation problems that fail the check. `tool.json`'s `timeout` (360 s) covers the checks and
   that wait. Set `PR_WAIT_CHECK` to `""` to return right after opening the pull request.

It never pushes to the default branch, never force-pushes, never reuses an existing branch and
never merges. Git hooks are off while it commits.

This is the one tool that reaches GitHub, with the `git` and `gh` credentials of the user running
the agent, so the pull request is opened in that user's name.
