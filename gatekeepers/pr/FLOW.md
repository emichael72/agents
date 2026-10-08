# How one PR passes through the gate

There are two separate conversations with the model: the coding agent asks it to make a change, and the gate later asks it to write a quiz about the submitted diff.

The frameworks, skills, and tools help the agent produce the change. The gate then reuses the code-checking tools and manages its own assessment.

## First, the service starts

~~~text
systemd
  └─ pr_gate.sh serve
       └─ Python service
            ├─ Quiz website
            └─ GitHub polling thread
~~~

systemd manages the program's lifetime. The Python program handles GitHub, model requests, the database, and grading. Closing an agent chat does not stop it.

## Then, follow a change

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

The diagram shows the normal code-change path. Cosmetic changes may need no quiz, and demo settings allow an explicit skip after the code checks pass.

## What each step actually does

**The skill gives the agent a procedure.** It describes how to sync, read, edit, test, check, and submit. The agent carries out those steps with tools. The skill is text; it is neither another agent nor a program.

**The PR tool submits the work.** It checks uncommitted changes using the gate's own inspector, then opens a new branch's PR against the default branch. Its manifest allows a wait of up to 100 seconds for the quiz link. It does not wait for the person to finish the quiz.

**The poller finds work.** It asks GitHub for open PRs targeting main, roughly five seconds after each polling round. It handles the configured developer and looks for a stored assessment matching the PR number, PR commit, and base commit. Existing assessments are reused.

**The gate checks the submitted files.** It downloads temporary copies from GitHub. Build and tests run through the agents' shell tool, with only the downloaded tree exposed to its sandbox. Documentation uses the agents' doxy script. Both the PR tool and the service use ChangeInspector.

**The model writes the quiz.** The service sends the diff, PR description, build/test output, and its analysis of code changes. It validates the returned structure and shuffles choices while preserving the answer key. The instructions ask for three questions; the schema allows three to five.

**Python grades the answers.** The browser sends the choices to the service. The service verifies the PR version, compares with the saved key, and records the score. It then publishes the status to GitHub in that submission request. There is no new model call for grading.

## What is stored where?

| Information | Location |
| --- | --- |
| Questions, choices, key, explanations, model used, PR version, and check reports | SQLite quizzes table on minion |
| Each attempt's score, total, pass/fail, and timestamp | SQLite attempts table |
| The code tests that make check runs | Submitted project files, currently core_dump's Makefile |
| Required merge rule and reported commit statuses | GitHub |

The default database is `agents/gatekeepers/pr/data/quiz.sqlite3`. `QUIZ_DATA_DIR` can move it. Attempts store scores rather than the complete selection of answers. Restarting preserves the database.

## How GitHub knows to block merging

GitHub needs a rule requiring **developer-quiz** on main. The service posts a status on the exact PR commit:

~~~text
Name:    developer-quiz
State:   pending, failure, error, or success
Details: the quiz URL
~~~

GitHub trusts this reported state for the configured requirement. It does not read SQLite or grade answers. Installing the systemd service does not configure the GitHub rule.

A missing, pending, failed, or error result cannot satisfy that required status. Success satisfies this gate; other merge conditions may still apply. Administrator bypass rules are a separate setting.

## When something changes

A new PR commit needs a new assessment. A new base commit also does: old submissions are rejected, and the poller prepares the new assessment. GitHub's requirement for an up-to-date branch matters too, because its status is attached to a commit while the local assessment records both versions.

If the service stops, it cannot publish new results. A missing or pending result keeps waiting. A success already posted is not automatically withdrawn just because the service stopped.

The service saves a score before posting it. If GitHub cannot be reached, resubmitting can publish the saved result. Quizzes survive restarts; the poller's temporary generation-retry counters do not.

## Where to look in the merged code

| Responsibility | Code |
| --- | --- |
| Start the service | `pr-gate.service` and `pr_gate.py` |
| Poll PRs and serve pages | `server.py` |
| Check builds and documentation | `changes.py: ChangeInspector` |
| Write the quiz | `generator.py: QuizGenerator` |
| Save quizzes and attempts | `store.py: QuizStore` |
| Grade and publish a result | `gate.py: QuizGate.submit()` and `publish()` |
| Send the GitHub commit status | `github.py: GitHub.publish_status()` |
| Validate question structure | `quiz.py` |

For commands and settings, see the [gate README](README.md).
