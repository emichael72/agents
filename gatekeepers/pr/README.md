# Pull request gate

The gate adds a short understanding check to a code change. It builds the submitted code, runs its tests, checks the documentation, and asks the model to write a quiz about the diff.

You answer in a browser. A perfect score satisfies the quiz requirement; the code checks must also pass. GitHub then permits merging if its other requirements are satisfied. Someone still has to merge the PR.

## The pieces

The **Python service on minion** watches GitHub and serves the quiz website. **systemd** starts that service and restarts it if it crashes.

The **model on the Mac Studio** writes the questions and answer key. The service calls it directly using the shared model profiles; it does not need a coding agent chat to be open.

**SQLite on minion** stores quizzes, answer keys, code-check reports, and scores. **GitHub** stores the PR and enforces the merge rule.

## What happens to a change

1. The agent uses the [pr tool](../../tools/pr/README.md) to check and submit its work.
2. The service discovers the PR by polling GitHub. It reports `developer-quiz = pending`.
3. It downloads that version and repeats the code checks.
4. The model reads the diff and returns questions, choices, correct answers, and explanations.
5. The service saves the assessment and publishes a quiz link.
6. Your browser sends your answers to the service. Python grades them, saves the result, and immediately reports the status to GitHub.

The browser displays the result. It does not release the merge, and the poller does not wait for a database change before reporting a pass.

## The checks use the agents' tools

The gate runs `make` and, when defined, `make check` through the same [shell tool](../../tools/shell/README.md) the agents use. Documentation is checked with the same [doxy script](../../tools/doxy/README.md).

The PR tool's local checks and the service's checks share `ChangeInspector` in `changes.py`. The local check helps catch problems before submission; the service assesses the actual committed version downloaded from GitHub.

Compiler warnings currently fail the check. If the build, tests, or documentation fail, fix the change and push again. Quiz answers cannot override those failures.

## Why GitHub waits

The repository's rule for main must require the **developer-quiz** status. The service uses its `gh` login to post pending, failure, error, or success on the PR commit. GitHub checks that result before allowing a merge.

The service installer does not create that rule. The PR comment and quiz link help people find the assessment; the commit status is what enforces the requirement.

## Stored quizzes and retries

The default database is `gatekeepers/pr/data/quiz.sqlite3` in the agents checkout. It survives restarts. The History page shows the saved assessments and attempts.

The model is asked for three questions, although validation accepts three to five. All answers must be correct. Failed attempts can be retried; a passed version stays passed.

An assessment records both the PR commit and the base commit. If either changes, the service needs a fresh assessment and refuses submissions to the old quiz. Cosmetic-only changes can pass without a quiz when the other checks pass.

## Run and manage it

From the repository root on minion:

~~~bash
./install.sh --gate install
./install.sh --gate status
./install.sh --gate logs
./install.sh --gate restart
~~~

Installation checks for GitHub access and required programs. It also tries to keep the user service running after logout.

Open the [quiz homepage](http://minion:8000) or [History](http://minion:8000/history). The demo sign-in is shown on the login page.

Settings are in [settings.json](settings.json). Environment values can override them; restart after changing service settings. The checked-in `QUIZ_ALLOW_SKIP=true` enables a Skip quiz button. Set it to false to require a perfect score for code changes.

This is a single-user demo. Questions can be wrong, and the shared sign-in does not establish which individual answered. Preview an assessment before using it in a presentation.

For the detailed handoff and the relevant code, read the [flow walkthrough](FLOW.md).
