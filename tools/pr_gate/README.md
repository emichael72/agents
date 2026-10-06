# PR Gate (status)

The agents' view of the pull request gate: for each open pull request in the gated repository,
whether the gate's service is running, and whether the request is blocked (build, tests,
documentation, or a quiz waiting, with its link) or may merge. Read-only.

The gate itself, its service, settings and documentation live in
[`gatekeepers/pr`](../../gatekeepers/pr/README.md); this folder only holds the tool's manifest,
which runs `gatekeepers/pr/pr_gate.sh status`.

**Usage Example:**

```bash
bash ../gatekeepers/pr/pr_gate.sh status          # every open pull request
bash ../gatekeepers/pr/pr_gate.sh status --pr 8   # one
```
