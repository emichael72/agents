# Merge Request Quiz

A merge gate that checks the developer understands the code they are about to merge. When a pull
request is opened (or a commit is pushed to it), a model reads the diff and writes a short
multiple-choice quiz about it. The PR cannot merge until its author answers every question
correctly.

It is a tool and a resident service:

- **The tool** (`mr_quiz`, what the agents call) reports the gate: whether the service is running,
  and for each open PR whether its quiz is waiting (with the link) or passed.
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

1. The target repository ([emichael72/mr_quiz](https://github.com/emichael72/mr_quiz)) protects
   `main`: merging requires a passing `developer-quiz` status check and an up-to-date branch,
   and the rule also applies to administrators. Until the status says success, GitHub blocks the
   merge button.
2. Every 30 seconds the service lists the open PRs that target `main`. For each new revision
   (head and base commit) of a PR opened by the configured developer, it posts "Preparing the
   developer quiz", fetches the diff and asks the model for three questions.
3. The quiz is validated (one correct answer among four distinct options), its choices shuffled,
   and its answer key stored in `data/quiz.sqlite3`. The `developer-quiz` status turns into
   "Complete the developer quiz", and its **Details** link opens the quiz.
4. The developer answers in the browser; the server grades. A perfect score posts success and the
   PR can be merged (the service never merges by itself). A miss posts failure; retries are
   unlimited.
5. A new commit, or a change on `main`, needs a new quiz: the old one can no longer be submitted,
   and the poller generates the next one.

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
password `pass`; the sign-in page shows them. Change them with `QUIZ_WEB_USER` and
`QUIZ_WEB_PASSWORD`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `QUIZ_REPO` | `emichael72/mr_quiz` | The repository to gate |
| `QUIZ_DEVELOPER` | `emichael72` | The only PR author assessed |
| `QUIZ_WEB_USER`, `QUIZ_WEB_PASSWORD` | `user`, `pass` | The web sign-in |
| `QUIZ_BASE_URL` | `http://<hostname>:8000` | Where the PR's Details link points |
| `QUIZ_MODEL_PROFILE` | the models file's default | Model profile |
| `QUIZ_DATA_DIR` | `mr_quiz/data` | Database and the key that signs sign-in cookies (gitignored) |

GitHub access goes through the `gh` CLI and its login; the service needs only outbound access.

## Limits

This is a single-user demo, not tamper-proof enforcement:

- Plain HTTP with a demo sign-in printed on the sign-in page: anyone who can reach the port can
  take a quiz. Use it on a trusted network only.
- The `gh` login that posts the status could post success directly; a real deployment would use
  a dedicated GitHub App as the only allowed status source, HTTPS and per-user sign-in.
- Model-generated questions can be wrong; look at a quiz before presenting it.
- Diffs over 60,000 characters are rejected. The diff is never executed.

## Tests

```bash
.venv/bin/python -m unittest discover -s tools/mr_quiz/tests
```

The tests use temporary storage with mocked GitHub and model calls; they never mark a real PR.
