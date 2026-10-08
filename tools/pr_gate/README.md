# PR Gate (status)

The agents' view of the pull request gate: for each open pull request in the gated repository,
whether the gate's service is running, and whether the request is blocked (build, tests,
documentation, or a quiz waiting, with its link) or may merge.

With `action` set to `history`, it lists past assessments instead: when, the pull request and its
title, the revision, the outcome and the attempts, as the gate's History page shows them.

With `action` set to `start`, it starts the gate's own service (the `pr-gate` systemd user unit)
and nothing else, then reports its state. The agents cannot stop or restart the gate: a gatekeeper
the gated agent can switch off is no gatekeeper. That is left to the user, with
`./install.sh --gate stop` or `restart`.

The gate itself, its service, settings and documentation live in
[`gatekeepers/pr`](../../gatekeepers/pr/README.md); this folder only holds the tool's manifest,
which runs `gatekeepers/pr/pr_gate.sh status`.

**Usage Example:**

```bash
bash ../gatekeepers/pr/pr_gate.sh status          # every open pull request
bash ../gatekeepers/pr/pr_gate.sh status --pr 8   # one
bash ../gatekeepers/pr/pr_gate.sh status --action history          # past assessments
bash ../gatekeepers/pr/pr_gate.sh status --action history --pr 8   # one pull request's
bash ../gatekeepers/pr/pr_gate.sh status --action start
```
