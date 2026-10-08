# Skill

Reads the agents' skills: step-by-step procedures for kinds of tasks, such as changing code and
opening a pull request. Each skill is a folder in [`skills/`](../../skills) (`skills_dir` in
[`context/agent.json`](../../context/agent.json)) holding a `SKILL.md`:

```markdown
---
name: pull-request
description: Change code in a repository (a feature, a fix, a new module or option) and open a pull request (merge request, MR) for it.
---

# Change code and open a pull request

1. Sync: ...
```

The header between the `---` lines gives the skill's `name` (its folder) and a one-line
`description` that says when to use it. Every agent lists each skill's name and description at the
end of its instructions when it starts, so the model knows which skills exist without loading them;
when a task matches one, it reads the skill with this tool and follows its steps. Only that one
line per skill is in every prompt; the procedure is read when needed.

The tool only reads. Skill names are plain (letters, digits, `-` and `_`), so a name cannot reach
outside the skills folder. To add a skill, add `skills/<name>/SKILL.md`; the agents list it on
their next start.

**Usage Example:**

```bash
python3 skill/skill.py                       # the skills, one line each
python3 skill/skill.py --name=pull-request   # one skill's procedure
```
