# PR gate status

This is how an agent sees the pull request gate: which PRs are waiting, why a change is blocked, and where to take its quiz.

The tool reads assessments and checks the service. Quiz generation and grading belong to the separate [gate service](../../gatekeepers/pr/README.md).

## Use it

From the repository root:

~~~bash
bash gatekeepers/pr/pr_gate.sh status
bash gatekeepers/pr/pr_gate.sh status --pr=8
bash gatekeepers/pr/pr_gate.sh status --action=history --pr=8
bash gatekeepers/pr/pr_gate.sh status --action=start
~~~

Omit `--pr` to show all relevant requests. History shows previous assessments and their scores.

The agent can start the systemd service, but it cannot stop or restart it through this tool. A person manages those actions with `./install.sh --gate stop` or `restart`.

The result reports whether the gate permits merging. GitHub may still have other requirements. See the [walkthrough](../../gatekeepers/pr/README.md#what-happens-to-a-change) for how the status reaches GitHub.
