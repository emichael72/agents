# Pull request gate

The gate adds a short understanding check to a code change. It builds the submitted code, runs its tests, checks the
documentation, and asks the model to write a quiz about the diff.

You answer in a browser. A perfect score satisfies the quiz requirement; the code checks must also pass. GitHub then
permits merging if its other requirements are satisfied. Someone still has to merge the PR.

## The pieces

The **Python service on minion** watches GitHub and serves the quiz website. **systemd** starts that service and
restarts it if it crashes.

The **model on the Mac Studio** writes the questions and answer key. The service calls it directly using the shared
model profiles; it does not need a coding agent chat to be open.

**SQLite on minion** stores quizzes, answer keys, code-check reports, and scores. **GitHub** stores the PR and enforces
the merge rule.

~~~text
systemd
  └─ pr_gate.sh serve
       └─ Python service
            ├─ Quiz website
            └─ GitHub polling thread
~~~

systemd manages the program's lifetime. Python handles GitHub, model requests, the database, and grading. Closing an
agent chat does not stop the service.

## What happens to a change

There are two separate conversations with the model: the coding agent asks it to make a change, and the gate later asks
it to write a quiz about the submitted diff. The frameworks, skills, and tools help the agent produce the change; the
gate reuses the code-checking tools and manages its own assessment.

~~~text
Agent follows the pull-request skill
        |
        v
Read and edit code; add tests
        |
        v
pr check → shared shell + doxy checks
        |
        v
pr open → format/check again → commit → push → open PR
        |
        v
Gate notices the new PR version
        |
        +── GitHub status: pending
        |
        v
Download committed files → shared shell + doxy checks
        |
        v
Send diff and context to the model
        |
        v
Save questions and answer key in SQLite
        |
        +── Put quiz link on the PR and in its status
        |
        v
Developer answers in browser
        |
        v
Python grades answers against the stored key
        |
        v
Save score in SQLite → publish result to GitHub
        |
        +── Failure: retry quiz, or fix code and push
        |
        └── Success: required gate satisfied; human may merge
~~~

The diagram shows the normal code-change path. Cosmetic changes may need no quiz, and demo settings allow an explicit
skip after the code checks pass.

The **pull-request skill** gives the agent a procedure for syncing, reading, editing, testing, checking, and submitting.
The agent carries out those steps with tools. The skill is text; it is neither another agent nor a program.

The **[pr tool](../../tools/pr/README.md)** checks uncommitted changes using the gate's inspector, then opens a new
branch's PR against the default branch. Its manifest allows a wait of up to 100 seconds for the quiz link. It does not
wait for the person to finish the quiz.

The **poller** asks GitHub for open PRs targeting main, roughly five seconds after each polling round. It handles the
configured developer and looks for a stored assessment matching the PR number, PR commit, and base commit. Existing
assessments are reused.

The **model** receives the diff, PR description, build/test output, and the service's analysis of code changes. The
service validates the returned questions, choices, correct answers, and explanations, then shuffles choices while
preserving the answer key.

`QUIZ_MODEL_ATTEMPTS` limits the number of model replies per quiz, including the initial reply. The default is 2,
allowing one retry for an invalid or unfinished reply, or one that incorrectly calls a code change cosmetic.

When you submit answers, **Python** verifies the PR version, compares your choices with the saved key, records the
score, and publishes the status to GitHub in that submission request. Grading needs no new model call.

The browser displays the result. It does not release the merge, and the poller does not wait for a database change
before reporting a pass.

## The checks use the agents' tools

The gate runs `make` and, when defined, `make check` through the same [shell tool](../../tools/shell/README.md) the
agents use. Documentation is checked with the same [doxy script](../../tools/doxy/README.md).

The PR tool's local checks and the service's checks share `ChangeInspector` in `changes.py`. The local check helps catch
problems before submission; the service assesses temporary copies of the actual committed version downloaded from
GitHub. Only the downloaded tree is exposed to the shell tool's sandbox.

Compiler warnings currently fail the check. If the build, tests, or documentation fail, fix the change and push again.
Quiz answers cannot override those failures.

## Why GitHub waits

The repository's rule for main must require the **developer-quiz** status. The service uses its `gh` login to post a
status on the exact PR commit:

~~~text
Name:    developer-quiz
State:   pending, failure, error, or success
Details: the quiz URL
~~~

GitHub checks this reported state before allowing a merge. It does not read SQLite or grade answers. A missing, pending,
failed, or error result cannot satisfy the required status. Success satisfies this gate; other merge conditions may
still apply. Administrator bypass rules are a separate setting.

The service installer does not create that rule. The PR comment and quiz link help people find the assessment; the
commit status is what enforces the requirement.

## Stored quizzes and retries

| Information                                                                      | Location                                                |
|----------------------------------------------------------------------------------|---------------------------------------------------------|
| Questions, choices, key, explanations, model used, PR version, and check reports | SQLite `quizzes` table on minion                        |
| Each attempt's score, total, pass/fail, and timestamp                            | SQLite `attempts` table                                 |
| The code tests that `make check` runs                                            | Submitted project files, currently core_dump's Makefile |
| Required merge rule and reported commit statuses                                 | GitHub                                                  |

The default database is `gatekeepers/pr/data/quiz.sqlite3` in the repository checkout. `QUIZ_DATA_DIR` can move it. It
survives restarts. Attempts store scores rather than the complete selection of answers. The History page shows the saved
assessments and attempts.

The model is asked for three questions, although validation accepts three to five. All answers must be correct. Failed
attempts can be retried; a passed version stays passed.

An assessment records both the PR commit and the base commit. If either changes, the service needs a fresh assessment
and refuses submissions to the old quiz; the poller prepares the new assessment. GitHub's requirement for an up-to-date
branch matters too, because its status is attached to a commit while the local assessment records both versions.
Cosmetic-only changes can pass without a quiz when the other checks pass.

If the service stops, it cannot publish new results. A missing or pending result keeps waiting. A success already posted
is not automatically withdrawn just because the service stopped.

The service saves a score before posting it. If GitHub cannot be reached, resubmitting can publish the saved result.
Quizzes survive restarts; the poller's temporary generation-retry counters do not.

## Run and manage it

From the repository root on minion:

~~~bash
./install.sh --gate install
./install.sh --gate status
./install.sh --gate logs
./install.sh --gate restart
~~~

Installation checks for GitHub access and required programs. It also tries to keep the user service running after
logout.

Open the [quiz homepage](http://minion:8000) or [History](http://minion:8000/history). The demo sign-in is shown on the
login page.

Settings are in [settings.json](settings.json). Environment values can override them; restart after changing service
settings. The checked-in `QUIZ_ALLOW_SKIP=true` enables a Skip quiz button. Use `QUIZ_ALLOW_SKIP=false` to require a
perfect score for code changes.

This is a single-user demo. Questions can be wrong, and the shared sign-in does not establish which individual answered.
Preview an assessment before using it in a presentation.

## Where to look in the code

| Responsibility                 | Code                                                            |
|--------------------------------|-----------------------------------------------------------------|
| Start the service              | [pr-gate.service](pr-gate.service) and [pr_gate.py](pr_gate.py) |
| Poll PRs and serve pages       | [server.py](server.py)                                          |
| Check builds and documentation | [changes.py](changes.py): `ChangeInspector`                     |
| Write the quiz                 | [generator.py](generator.py): `QuizGenerator`                   |
| Save quizzes and attempts      | [store.py](store.py): `QuizStore`                               |
| Grade and publish a result     | [gate.py](gate.py): `QuizGate.submit()` and `publish()`         |
| Send the GitHub commit status  | [github.py](github.py): `GitHub.publish_status()`               |
| Validate question structure    | [quiz.py](quiz.py)                                              |
