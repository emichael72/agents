# Gatekeepers

Two parts of the project decide what an agent may access and what a developer may merge.

**The file-system gate** checks paths against `context/paths.json`. Each named folder has read, write, and execute permissions. Tools ask this gate before touching a path; the shell also mounts the permitted folders inside its sandbox.

**The [pull request gate](pr/README.md)** checks submitted code and quizzes the developer about the change. It reports a result to GitHub, which enforces the required `developer-quiz` status. It gates the folders marked `"pr_gated": true` in `context/paths.json`.

The agent sees the PR gate through the [pr_gate tool](../tools/pr_gate/README.md). It can report status, show history, and start the service. Stopping or restarting it is left to the person managing the demo.

The editing tool refuses changes to the gatekeepers, tools, and shared context folders. The shell sees these only if allowed, and they are read-only or absent.

For the submission sequence, see the [PR tool](../tools/pr/README.md). For the browser, database, and GitHub handoff, see the [flow walkthrough](pr/README.md#what-happens-to-a-change).
