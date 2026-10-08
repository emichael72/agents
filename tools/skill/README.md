# Skills

A skill is a written procedure for a task. Tools perform individual actions; a skill explains which actions to take, in what order.

For example, the pull-request skill tells the model to sync the repository, read the relevant code, edit it, add tests, run the checks, and submit the change.

The agents list skill names and short descriptions at startup. When a task matches, the model uses this read-only tool to get the full procedure. A skill is not another agent or an executable program: the model follows the text using its available tools.

## Read a skill

From the repository root:

~~~bash
python3 tools/skill/skill.py
python3 tools/skill/skill.py --name=pull-request
~~~

The first command lists skills; the second reads one.

## Add a procedure

Create `skills/<name>/SKILL.md` with a short header:

~~~markdown
---
name: pull-request
description: Make a code change and open a pull request.
---

Write the procedure here.
~~~

The folder name identifies the skill. The description says when to use it; the body gives the steps. The agents discover it on their next start. `skills_dir` in `context/agent.json` selects the folder.

Skills guide the model's behavior. Actual access restrictions are enforced by the tools and gatekeepers.
