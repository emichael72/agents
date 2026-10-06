# MR

Opens a merge request (a GitHub pull request) with the uncommitted changes of a repository in an
allowed folder with write access ([`../../context/paths.json`](../../context/paths.json)), such as
`core_dump`. It is how an agent hands its work to a person: the change waits on GitHub for review,
and in `core_dump` the [`pr_gate`](../../gatekeepers/pr/README.md) quiz checks that the reviewer understands
it before it can merge.

**Before starting work**, `action: sync` brings the repository's default branch up to date with
GitHub (fast-forward only): the agents' shell has no network, so without it they would work on
stale code. It refuses when there are uncommitted changes or local commits, so it never loses work.
The pull request gate's service also keeps `core_dump` current on its own (`QUIZ_LOCAL_CLONE`), so
`sync` mostly confirms it.

**Usage Example:**

```bash
python3 mr/mr.py core_dump --action sync     # update main from GitHub first
python3 mr/mr.py core_dump --title "Add an e module" --body "Prints Euler's number; make builds cleanly."
```

What it does, every time:

1. Checks the repository: it is on its default branch (usually `main`), has no commits of its own,
   and has changes to submit.
2. Brings the default branch up to date with GitHub (fast-forward only).
3. Formats the changed and new C/C++ files with `clang-format`, using the repository's own
   `.clang-format` if it has one, else the agents' template,
   [`context/clang-format.yaml`](../../context/clang-format.yaml). Only those files are touched,
   and the result lists the ones that changed.
4. Creates a new branch (`agent/<title>`, or the `branch` given), commits all changes (respecting
   `.gitignore`), and pushes the branch.
5. Opens the pull request into the default branch, noting which agent opened it, and switches back
   to the default branch; the changes now live on the branch.
6. Waits for the merge gate's check on the new commit (`MR_WAIT_CHECK` in `tool.json`, set to
   `developer-quiz`, for up to `MR_WAIT_SECONDS`, 100) and reports it. With
   [`pr_gate`](../../gatekeepers/pr/README.md) that is the quiz link the reviewer must pass, or the
   documentation problems that fail the check. `tool.json`'s `timeout` (150 s) lets the agents wait
   that long. Set `MR_WAIT_CHECK` to `""` to return right after opening the request.

It never pushes to the default branch, never force-pushes, never reuses an existing branch and
never merges. Git hooks are off while it commits.

This is the one tool that reaches GitHub, with the `git` and `gh` credentials of the user running
the agent, so the pull request is opened in that user's name.
