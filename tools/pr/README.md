# Pull requests

The `pr` tool submits the agent's uncommitted work to GitHub. It also brings a working copy up to date and checks
changes before submission.

It used to be called `mr`. “Merge request” and “pull request” refer to the same handoff here; the current tool name is
`pr`.

## The usual sequence

Run from the repository root:

~~~bash
python3 tools/pr/pr.py --action=sync -- core_dump
# Make the change.
python3 tools/pr/pr.py --action=check -- core_dump
python3 tools/pr/pr.py --title="Add a square-root module" --body="Adds the option and tests invalid input." -- core_dump
~~~

Use your actual change and test results in the title and description. Git and `gh` must be installed and authenticated
for syncing and opening a PR.

**sync** updates the default branch by fast-forwarding. It refuses if you have uncommitted work, local commits, or the
wrong branch checked out.

**check** copies the files that would be submitted, then runs the gate's build, test, and documentation checks, with
the folder's build settings from the [gate's settings](../../gatekeepers/pr/settings.json). It does not commit or open
anything. Changed binary files are refused. Fix the reported problems and check again.

**open** is the default action. Start with uncommitted changes on the default branch, normally main, and no local
commits ahead of GitHub. The tool updates the base and formats changed C/C++ files when clang-format is available. In a
folder under the merge gate (`"pr_gated": true` in [context/paths.json](../../context/paths.json)) it checks the change
again and stops before committing if a check fails. Other folders get no gate checks and no quiz wait.

Once ready, it creates a fresh `agent/<title>` branch, commits and pushes the changes, and opens the PR. `--branch`
selects another new branch name. The local checkout then switches back to the default branch.

The tool never merges or force-pushes. All tracked and new files are included unless ignored, so keep unrelated work out
of the checkout.

## Getting the quiz link

When the agent runs the tool, its manifest enables a wait of up to 100 seconds for `developer-quiz`. The result includes
the quiz link when it is ready. A timeout can leave a successfully opened PR still waiting for assessment.

Direct command-line runs do not load the manifest's environment automatically. To get the same wait:

~~~bash
PR_WAIT_CHECK=developer-quiz PR_WAIT_SECONDS=100 python3 tools/pr/pr.py --title="Describe your change" -- core_dump
~~~

Use [pr_gate](../pr_gate/README.md) to check progress afterward. The [gate service](../../gatekeepers/pr/README.md)
handles the quiz; passing its local code checks does not count as passing the developer quiz.
