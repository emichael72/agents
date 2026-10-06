# Merge Request Quiz

A merge gate that checks the developer understands the code they are about to merge. When a pull
request is opened (or a commit is pushed to it), a model reads the diff and writes a short
multiple-choice quiz about it. The PR cannot merge until its author answers every question
correctly, and until its changed C/C++ files are correctly documented (checked with the
[`doxy`](../doxy/README.md) tool). A change that only touches comments, formatting or
documentation needs no quiz, only correct documentation.

It is a tool and a resident service:

- **The tool** (`mr_quiz`, what the agents call) reports the gate: whether the service is running,
  and for each open PR whether it has documentation problems, its quiz is waiting (with the
  link), or it may merge.
- **The service** (`mr-quiz`, a systemd user unit on minion) polls GitHub, generates the quizzes,
  serves them at `http://minion:8000` and posts the result to the PR.

**Usage Example:**

```bash
bash mr_quiz/mr_quiz.sh status          # the agents' tool: open PRs and their quiz state
bash mr_quiz/mr_quiz.sh status --pr 1
bash mr_quiz/mr_quiz.sh create 1 --profile openai  # create (or re-post) a PR's quiz right now
bash mr_quiz/mr_quiz.sh list            # every stored quiz, as JSON
bash mr_quiz/mr_quiz.sh serve           # what the systemd unit runs
```

## How the gate works

1. The target repository, [emichael72/core_dump](https://github.com/emichael72/core_dump) (a small C sample project), protects
   `main`: merging requires a passing `developer-quiz` status check and an up-to-date branch,
   and the rule also applies to administrators. Until the status says success, GitHub blocks the
   merge button.
2. Every few seconds (`QUIZ_POLL_SECONDS`, 5) the service lists the open PRs that target `main`. For each new revision
   (head and base commit) of a PR opened by the configured developer, it posts "Checking
   documentation and preparing the developer quiz" and inspects the change (`changes.py`):
   - **Documentation:** it downloads the PR's files and runs `doxy` on the whole tree,
     keeping the problems in the files the PR changed. The whole tree is checked so that a
     function documented in an unchanged header still counts as documented.
   - **Code or cosmetic:** it compares each changed C/C++ file before and after with comments and
     formatting removed (string literals and preprocessor line ends still count). Documentation
     files (`.md`, `.txt`, ...) never change code; any other file, such as a Makefile, always does.
3. The model gets the diff and the server's analysis. For a cosmetic change it answers
   `"cosmetic": true` with no questions; otherwise it writes three questions. The server accepts
   "cosmetic" only if its own comparison agrees, so a model cannot wave a code change through.
4. The status is decided in this order:

   | Situation | `developer-quiz` |
   | --- | --- |
   | Documentation problems in a changed file | failure, whatever the quiz |
   | Cosmetic change, documentation OK | success, no quiz |
   | Code change, quiz passed | success |
   | Code change, last attempt failed | failure (retries are unlimited) |
   | Code change, not yet answered | pending, "Complete the developer quiz" |

   The check's **Details** link opens the revision's page: the documentation result (with each
   problem as `file:line: message`), and the quiz when one is needed. The quiz is validated (one
   correct answer among four distinct options), its choices shuffled, and its answer key stored in
   `data/quiz.sqlite3`; grading happens on the server. A pass lets the PR merge (the service never
   merges by itself).
5. A new commit, or a change on `main`, needs a new assessment: the old one can no longer be
   submitted, and the poller makes the next one. Fixing documentation problems therefore means
   pushing the fix.

If generation fails three times for a revision, the status becomes an error; push again or run
`create` by hand.

## Model

The quiz is written by one of the agents' model profiles in
[`context/models.json`](../../context/models.json): `local` (LM Studio on boba, the default) or
`openai`. The service uses the file's default unless started with `--profile openai` or
`QUIZ_MODEL_PROFILE=openai`. The systemd unit does not see your shell's environment, so for
OpenAI put `OPENAI_API_KEY=...` in `~/.config/mr-quiz.env` (read by the unit).

The model's instructions (system prompt) live in
[`context/instructions.json`](context/instructions.json), as a list of lines like the agents'
`context/instructions.json`. The file is read for every quiz, so an edit applies to the next one
without restarting the service. The reply must still match the JSON shape it describes, which
`quiz.py` validates.

## The service

```bash
ln -sf ~/projects/agents/tools/mr_quiz/mr-quiz.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now mr-quiz
journalctl --user -u mr-quiz -f       # watch the poller
```

Open `http://minion:8000` (port 8000 is open in minion's firewall) and sign in with user `user`,
password `pass`; the sign-in page shows them.

## Settings

The settings live in the `"env"` block of [`tool.json`](tool.json), the tool's manifest. The agents
pass that block to the tool, and `quiz.py` reads it too, so the tool and the service share one
configuration. Edit it, then restart the service (`systemctl --user restart mr-quiz`). A variable
set in the environment (for example in `~/.config/mr-quiz.env` for the service) overrides the file.

| Variable | In `tool.json` | Meaning |
| --- | --- | --- |
| `QUIZ_REPO` | `emichael72/core_dump` | The repository to gate |
| `QUIZ_DEVELOPER` | `emichael72` | The only PR author assessed |
| `QUIZ_BASE_URL` | `http://minion:8000` | Where the PR's Details link points |
| `QUIZ_POLL_SECONDS` | `5` | Seconds between GitHub polls. Each poll that finds nothing new costs one of the 5,000 GitHub API requests per hour your `gh` login allows: 720 an hour at 5 seconds |
| `QUIZ_MODEL_PROFILE` | empty: the models file's default | Model profile (`local` or `openai`) |
| `QUIZ_WEB_USER`, `QUIZ_WEB_PASSWORD` | `user`, `pass` | The web sign-in |

One setting stays outside the manifest: `QUIZ_DATA_DIR` (default `mr_quiz/data`, gitignored), the
database and the key that signs sign-in cookies.

GitHub access goes through the `gh` CLI and its login; the service needs only outbound access.

## Limits

This is a single-user demo, not tamper-proof enforcement:

- Plain HTTP with a demo sign-in printed on the sign-in page: anyone who can reach the port can
  take a quiz. Use it on a trusted network only.
- The `gh` login that posts the status could post success directly; a real deployment would use
  a dedicated GitHub App as the only allowed status source, HTTPS and per-user sign-in.
- Model-generated questions can be wrong; look at a quiz before presenting it.
- The cosmetic comparison understands C/C++ only. A PR that also touches any other code file
  (a Makefile, a script) always gets a quiz.
- The documentation check needs Doxygen on the server; if it cannot run, the check fails.
- Diffs over 60,000 characters are rejected. The diff is never executed.

## Tests

```bash
.venv/bin/python -m unittest discover -s tools/mr_quiz/tests
```

The tests use temporary storage with mocked GitHub and model calls; they never mark a real PR.
