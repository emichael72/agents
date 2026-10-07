# Pull Request Gate

A merge gate that checks the developer understands the code they are about to merge. When a pull
request is opened (or a commit is pushed to it), a model reads the diff and writes a short
multiple-choice quiz about it. The PR cannot merge until its author answers every question
correctly, until it builds and its tests pass (`make`, then `make check`), and until its changed
C/C++ files are correctly documented (checked with the [`doxy`](../../tools/doxy/README.md) tool). A change that only
touches comments, formatting or
documentation needs no quiz, only correct documentation.

It is a tool and a resident service:

- **The tool** (`pr_gate`, what the agents call) reports the gate: whether the service is running,
  and for each open PR whether it has documentation problems, its quiz is waiting (with the
  link), or it may merge.
- **The service** (`pr-gate`, a systemd user unit on minion) polls GitHub, generates the quizzes,
  serves them at `http://minion:8000` and posts the result to the PR.

**Usage Example:**

```bash
bash gatekeepers/pr/pr_gate.sh status                     # the agents' tool: open PRs and their gate state
bash gatekeepers/pr/pr_gate.sh status --pr 1              # the same, for one PR only
bash gatekeepers/pr/pr_gate.sh create 1 --profile openai  # assess a PR's current revision now (or re-post its status)
bash gatekeepers/pr/pr_gate.sh list                       # every stored assessment, as JSON
bash gatekeepers/pr/pr_gate.sh serve                      # the web service and GitHub poller (what the systemd unit runs)
```

## How the gate works

1. The target repository, [emichael72/core_dump](https://github.com/emichael72/core_dump) (a small C sample project),
   protects
   `main`: merging requires a passing `developer-quiz` status check and an up-to-date branch,
   and the rule also applies to administrators. Until the status says success, GitHub blocks the
   merge button.
2. Every few seconds (`QUIZ_POLL_SECONDS`, 5) the service lists the open PRs that target `main`. For each new revision
   (head and base commit) of a PR opened by the configured developer, it posts "Checking
   documentation and preparing the developer quiz" and inspects the change (`changes.py`):
    - **Build and tests:** it runs `make` (`QUIZ_BUILD_COMMAND`) on the PR's files, then
      `make check` (`QUIZ_TEST_TARGET`) if the Makefile has a `check` target, inside the
      [`shell`](../../tools/shell/README.md) tool's sandbox: the downloaded tree is the only folder it sees,
      with no network. A failure, or a compiler warning (`QUIZ_FAIL_ON_WARNINGS`), fails the check;
      the quiz page shows the warnings, then the end of the output.
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
   | The build or the tests fail | failure, whatever the quiz |
   | Documentation problems in a changed file | failure, whatever the quiz |
   | Cosmetic change, builds, documentation OK | success, no quiz |
   | Code change, quiz passed | success |
   | Code change, quiz skipped (`QUIZ_ALLOW_SKIP`) | success, "Quiz skipped (proof-of-concept mode)" |
   | Code change, last attempt failed | failure (retries are unlimited) |
   | Code change, not yet answered | pending, "Complete the developer quiz" |

   The check's **Details** link opens the revision's page: the documentation result (with each
   problem as `file:line: message`), and the quiz when one is needed. The quiz is validated (one
   correct answer among four distinct options), its choices shuffled, and its answer key stored in
   `data/quiz.sqlite3`; grading happens on the server. A pass lets the PR merge (the service never
   merges by itself).
5. **The pull request gets one comment** from the gate, kept up to date: the build and tests, the
   documentation, and the quiz link (or why there is none yet). Each new revision and each quiz
   result edits the same comment, found by a hidden `<!-- pr_gate -->` marker, rather than adding
   another. `QUIZ_PR_COMMENT` turns it off; a comment that cannot be posted never blocks the check.
6. **The History page** (`/history`, linked in the header) lists every assessment, newest first:
   when, the pull request (number and title), the revision, the outcome (passed, skipped,
   cosmetic, waiting, build or documentation failure), the attempts with the best score, and the
   model that wrote the quiz. `?pr=N` (click a number) shows one pull request's revisions.
7. A new commit, or a change on `main`, needs a new assessment: the old one can no longer be
   submitted, and the poller makes the next one. Fixing documentation problems therefore means
   pushing the fix.

If generation fails three times for a revision, the status becomes an error; push again or run
`create` by hand.

## Model

The quiz is written by one of the agents' model profiles in
[`context/models.json`](../../context/models.json): `local` (LM Studio on boba, the default) or
`openai`. The service uses the file's default unless started with `--profile openai` or
`QUIZ_MODEL_PROFILE=openai`. The systemd unit does not see your shell's environment, so for
OpenAI put `OPENAI_API_KEY=...` in `~/.config/pr-gate.env` (read by the unit).

The model's instructions (system prompt) live in
[`context/instructions.json`](context/instructions.json), as a list of lines like the agents'
`context/instructions.json`. The file is read for every quiz, so an edit applies to the next one
without restarting the service. The reply must still match the JSON shape it describes, which
`quiz.py` validates.

## The service

The repository's installer manages it, from the repository root:

```bash
./install.sh --gate install     # write the pr-gate systemd user unit, enable and start it
./install.sh --gate status      # the service, and the open pull requests' gate state
./install.sh --gate logs        # its last 50 log lines (journalctl --user -u pr-gate -f to follow)
./install.sh --gate restart     # after changing settings.json
./install.sh --gate stop        # or start, uninstall
```

`install` writes the unit with the repository's real location, keeps it running after logout (lingering) and warns about
anything the gate needs that is missing: `gh` (logged in), `bwrap`,
`doxygen`, `clang-format`.

Open `http://minion:8000` (port 8000 is open in minion's firewall) and sign in with user `user`,
password `pass`; the sign-in page shows them.

## Settings

The settings live in [`settings.json`](settings.json). `settings.py` reads them for both the service
and the agents' status tool ([`tools/pr_gate`](../../tools/pr_gate/README.md)), so they share one
configuration. Edit it, then restart the service (`./install.sh --gate restart` from the repository
root). A variable set in the environment (for example in `~/.config/pr-gate.env` for the service)
overrides the file.

| Variable                             | In `settings.json`               | Meaning                                                                                                                                                                                                                                                                                                          |
|--------------------------------------|----------------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `QUIZ_REPO`                          | `emichael72/core_dump`           | The repository to gate                                                                                                                                                                                                                                                                                           |
| `QUIZ_DEVELOPER`                     | `emichael72`                     | The only PR author assessed                                                                                                                                                                                                                                                                                      |
| `QUIZ_BASE_URL`                      | `http://minion:8000`             | Where the PR's Details link points                                                                                                                                                                                                                                                                               |
| `QUIZ_BUILD_COMMAND`                 | `make`                           | How to build a PR's revision; empty skips the build check                                                                                                                                                                                                                                                        |
| `QUIZ_TEST_TARGET`                   | `check`                          | The make target that runs the tests, when the Makefile has it                                                                                                                                                                                                                                                    |
| `QUIZ_FAIL_ON_WARNINGS`              | `true`                           | A compiler warning (`file:line: warning:`) fails the build check, like an error                                                                                                                                                                                                                                  |
| `QUIZ_ALLOW_SKIP`                    | `true`                           | Proof-of-concept mode: a **Skip quiz** button next to the answers unlocks the merge without answering. It appears only once the build, tests and documentation pass, and the check, the PR comment and the database record it as skipped. Set `false` to require the quiz                                        |
| `QUIZ_PR_COMMENT`                    | `true`                           | Post and keep up to date one comment on the pull request with the results and the quiz link                                                                                                                                                                                                                      |
| `QUIZ_LOCAL_CLONE`                   | `~/projects/core_dump`           | A local clone the service keeps current: every `QUIZ_SYNC_SECONDS` it fast-forwards the default branch from GitHub, so agents (whose shell has no network) start from the latest code. Only when the clone is on its default branch with no changes and no local commits; otherwise it waits. Empty turns it off |
| `QUIZ_SYNC_SECONDS`                  | `30`                             | How often to sync the local clone                                                                                                                                                                                                                                                                                |
| `QUIZ_POLL_SECONDS`                  | `5`                              | Seconds between GitHub polls. Each poll that finds nothing new costs one of the 5,000 GitHub API requests per hour your `gh` login allows: 720 an hour at 5 seconds                                                                                                                                              |
| `QUIZ_MODEL_PROFILE`                 | empty: the models file's default | Model profile (`local` or `openai`)                                                                                                                                                                                                                                                                              |
| `QUIZ_WEB_USER`, `QUIZ_WEB_PASSWORD` | `user`, `pass`                   | The web sign-in                                                                                                                                                                                                                                                                                                  |

One setting stays outside the manifest: `QUIZ_DATA_DIR` (default `pr_gate/data`, gitignored), the
database and the key that signs sign-in cookies.

GitHub access goes through the `gh` CLI and its login; the service needs only outbound access.

## Limits

This is a single-user demo, not tamper-proof enforcement:

- Plain HTTP with a demo sign-in printed on the sign-in page: anyone who can reach the port can
  take a quiz. Use it on a trusted network only.
- The `gh` login that posts the status could post success directly; a real deployment would use
  a dedicated GitHub App as the only allowed status source, HTTPS and per-user sign-in.
- Model-generated questions can be wrong; look at a quiz before presenting it.
- The cosmetic comparison understands C/C++ only. A PR that also touches any other code file (a Makefile, a script)
  always gets a quiz.
- The documentation check needs Doxygen on the server; if it cannot run, the check fails.
- Diffs over 60,000 characters are rejected. The diff is never executed.

## Tests

```bash
.venv/bin/python -m unittest discover -s gatekeepers/pr/tests
```

The tests use temporary storage with mocked GitHub and model calls; they never mark a real PR.
