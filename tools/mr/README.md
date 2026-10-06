# MR

Opens a merge request (a GitHub pull request) with the uncommitted changes of a repository in an
allowed folder with write access ([`../../context/paths.json`](../../context/paths.json)), such as
`core_dump`. It is how an agent hands its work to a person: the change waits on GitHub for review,
and in `core_dump` the [`mr_gate`](../mr_gate/README.md) quiz checks that the reviewer understands
it before it can merge.

**Usage Example:**

```bash
python3 mr/mr.py core_dump --title "Add an e module" --body "Prints Euler's number; make builds cleanly."
```

What it does, every time:

1. Checks the repository: it is on its default branch (usually `main`), has no commits of its own,
   and has changes to submit.
2. Brings the default branch up to date with GitHub (fast-forward only).
3. Creates a new branch (`agent/<title>`, or the `branch` given), commits all changes (respecting
   `.gitignore`), and pushes the branch.
4. Opens the pull request into the default branch, noting which agent opened it, and switches back
   to the default branch; the changes now live on the branch.

It never pushes to the default branch, never force-pushes, never reuses an existing branch and
never merges. Git hooks are off while it commits.

This is the one tool that reaches GitHub, with the `git` and `gh` credentials of the user running
the agent, so the pull request is opened in that user's name.
