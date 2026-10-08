# PR gate status

This is how an agent sees the pull request gate: which PRs are waiting, why a change is blocked, and where to take its quiz.

The tool reads assessments and checks the service. Quiz generation and grading belong to the separate [gate service](../../gatekeepers/pr/README.md).

## Use it

From the repository root:

~~~bash
bash gatekeepers/pr/pr_gate.sh status
bash gatekeepers/pr/pr_gate.sh status --project=core_dump --pr=8
bash gatekeepers/pr/pr_gate.sh status --action=history --project=core_dump --pr=8
bash gatekeepers/pr/pr_gate.sh status --action=start
~~~

The gated projects are the folders marked `"pr_gated": true` in [context/paths.json](../../context/paths.json).
`--project` takes the folder name or `owner/name` and may be omitted when only one project is gated. Omit `--pr` to show
all relevant requests. History shows previous assessments and their scores.

The agent can start the systemd service, but it cannot stop or restart it through this tool. A person manages those actions with `./install.sh --gate stop` or `restart`.

The result reports whether the gate permits merging. GitHub may still have other requirements. See the [walkthrough](../../gatekeepers/pr/README.md#what-happens-to-a-change) for how the status reaches GitHub.
