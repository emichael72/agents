"""
Module: quiz.py

Description:
    The developer quiz: a model reads a pull request's diff and writes a short multiple-choice
    quiz about it; the PR may merge only after its author passes that quiz. The changed C/C++ files
    must also be correctly documented (the doxy tool), and a change that only touches
    comments, formatting or documentation needs no quiz.

    The module provides:
      - Quiz generation through the model profiles shared by the agents (context/models.json).
      - Storage of quizzes and attempts in SQLite, including the answer keys.
      - The GitHub side, through the `gh` CLI: reading PRs and their diffs, and posting the
        `developer-quiz` commit status that branch protection on the target repository requires.

    Key design points:
      - A quiz belongs to one revision: the PR's head SHA and its base SHA. New commits or a moved
        base need a new quiz, and the old one can no longer be submitted.
      - Answer keys stay in the server's database; they are never rendered or sent to GitHub.
      - The diff is untrusted data: it goes to the model as text and is never executed.
      - Single-user demo: only PRs opened by the configured developer are assessed.
      - The gate passes when the documentation is correct and either the change is cosmetic or the
        quiz was passed. A change counts as cosmetic only when the model says so and the server's
        own comparison of the code (changes.py) agrees.
"""

import json
import logging
import os
import re
import secrets
import sqlite3
import subprocess
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

# Third-party
import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

# Local imports
from gatekeepers import REPO_ROOT
from gatekeepers.pr.changes import ChangeInspector

GATE_DIR = Path(__file__).resolve().parent
MODELS_FILE = REPO_ROOT / "context" / "models.json"
INSTRUCTIONS_FILE = GATE_DIR / "context" / "instructions.json"

SETTINGS_FILE = GATE_DIR / "settings.json"


def setting(name: str, required: bool = True) -> str:
    """
    Read one setting: the environment first, then settings.json, which the service and the agents'
    status tool share: it is the place to change them.
    Args:
        name: The setting, e.g. "QUIZ_REPO".
        required: Fail if neither place sets it.
    Returns:
        str: The value; "" for an optional setting that is not set.
    Raises:
        ValueError: If a required setting is missing.
    """
    defaults = json.loads(SETTINGS_FILE.read_text(encoding="utf-8")).get("settings", {})
    value = os.environ.get(name) or defaults.get(name, "")
    if required and not value:
        raise ValueError(f"Set {name} in {SETTINGS_FILE} (or in the environment).")
    return value


DATA = Path(os.environ.get("QUIZ_DATA_DIR") or GATE_DIR / "data")
REPO = setting("QUIZ_REPO")
DEVELOPER = setting("QUIZ_DEVELOPER")
BASE_URL = setting("QUIZ_BASE_URL").rstrip("/")
POLL_SECONDS = float(setting("QUIZ_POLL_SECONDS"))  # Each idle poll costs one GitHub API request
PROFILE = setting("QUIZ_MODEL_PROFILE", required=False) or None  # None uses the models file's default
WEB_USER = setting("QUIZ_WEB_USER")  # Demo sign-in, shown on the sign-in page
WEB_PASSWORD = setting("QUIZ_WEB_PASSWORD")
BUILD_COMMAND = setting("QUIZ_BUILD_COMMAND", required=False)  # "" skips the build check
TEST_TARGET = setting("QUIZ_TEST_TARGET", required=False)
FAIL_ON_WARNINGS = setting("QUIZ_FAIL_ON_WARNINGS", required=False).lower() in ("true", "1", "yes")
LOCAL_CLONE = setting("QUIZ_LOCAL_CLONE", required=False)  # Kept current with GitHub; "" for none
SYNC_SECONDS = float(setting("QUIZ_SYNC_SECONDS", required=False) or 30)
PR_COMMENT = setting("QUIZ_PR_COMMENT", required=False).lower() in ("true", "1", "yes")
ALLOW_SKIP = setting("QUIZ_ALLOW_SKIP", required=False).lower() in ("true", "1", "yes")  # Proof-of-concept mode
COMMENT_MARKER = "<!-- pr_gate -->"  # Finds the gate's own comment on a pull request

CONTEXT = "developer-quiz"  # The status check name branch protection requires
MAX_DIFF_CHARS = 60_000
GENERATION_ATTEMPTS = 2


class Question(BaseModel):
    """One multiple-choice question; `correct` indexes `options`."""
    model_config = ConfigDict(extra="forbid", strict=True)
    question: str = Field(min_length=10, max_length=1000)
    options: list[str] = Field(min_length=4, max_length=4)
    correct: int = Field(ge=0, le=3)
    explanation: str = Field(min_length=5, max_length=2000)

    @model_validator(mode="after")
    def unique_options(self) -> "Question":
        """
        Reject empty, oversized or duplicate options.
        Returns:
            Question: The validated question.
        Raises:
            ValueError: If an option is empty or too long, or two options match.
        """
        if any(not x.strip() or len(x) > 1000 for x in self.options):
            raise ValueError("Options must be nonempty and at most 1000 characters")
        if len(set(x.strip().casefold() for x in self.options)) != 4:
            raise ValueError("Options must be distinct")
        return self


class Quiz(BaseModel):
    """
    A quiz as the model writes it: a title and 3 to 5 questions, or, for a change that only touches
    comments, formatting or documentation, `cosmetic` set and no questions.
    """
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(min_length=3, max_length=200)
    cosmetic: bool = False
    questions: list[Question] = Field(max_length=5)

    @model_validator(mode="after")
    def questions_match_kind(self) -> "Quiz":
        """
        Require 3 to 5 questions for a code change, and none for a cosmetic one.
        Returns:
            Quiz: The validated quiz.
        Raises:
            ValueError: If the number of questions does not match `cosmetic`.
        """
        if self.cosmetic and self.questions:
            raise ValueError("A cosmetic change has no questions")
        if not self.cosmetic and len(self.questions) < 3:
            raise ValueError("A code change needs at least 3 questions")
        return self


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """
    Open the quiz database; the block runs as one transaction.
    Yields:
        sqlite3.Connection: A connection whose rows read as sqlite3.Row.
    """
    DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = sqlite3.connect(DATA / "quiz.sqlite3", timeout=30)
    db.row_factory = sqlite3.Row
    try:
        with db:
            yield db
    finally:
        db.close()


def init() -> str:
    """
    Create the database tables and the secret key, if missing.
    Returns:
        str: The key that signs the sign-in cookie and the form tokens (data/secret_key).
    """
    with connect() as db:
        # The SQLite database is created at runtime, without a fixed IDE data source.
        # noinspection SqlNoDataSourceInspection
        db.executescript("""
        CREATE TABLE IF NOT EXISTS quizzes (
          id TEXT PRIMARY KEY, pr INTEGER NOT NULL, sha TEXT NOT NULL,
          base_sha TEXT NOT NULL, developer TEXT NOT NULL, content TEXT NOT NULL,
          source TEXT NOT NULL, passed INTEGER NOT NULL DEFAULT 0,
          published TEXT NOT NULL DEFAULT '', created TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS attempts (
          id INTEGER PRIMARY KEY, quiz_id TEXT NOT NULL,
          developer TEXT NOT NULL, score INTEGER NOT NULL, total INTEGER NOT NULL,
          passed INTEGER NOT NULL, created TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)
        # Columns added after the first version; older databases get them with these defaults
        for column in ("pr_title TEXT NOT NULL DEFAULT ''", "skipped INTEGER NOT NULL DEFAULT 0",
                       "build_ok INTEGER NOT NULL DEFAULT 1", "build_report TEXT NOT NULL DEFAULT ''",
                       "docs_ok INTEGER NOT NULL DEFAULT 1", "docs_report TEXT NOT NULL DEFAULT ''",
                       "cosmetic INTEGER NOT NULL DEFAULT 0"):
            try:
                # noinspection SqlNoDataSourceInspection
                db.execute("ALTER TABLE quizzes ADD COLUMN " + column)
            except sqlite3.OperationalError:
                pass  # Already there
    secret_file = DATA / "secret_key"
    if not secret_file.exists():
        try:
            fd = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass  # Another process created it first
        else:
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_urlsafe(32))
    return secret_file.read_text().strip()


def gh(*args: str, payload: Optional[dict] = None) -> str:
    """
    Run the GitHub CLI, which supplies the credentials.
    Args:
        *args: The `gh` arguments, e.g. ("api", "repos/owner/name/pulls/1").
        payload: JSON written to the command's stdin (for `--input -`).
    Returns:
        str: The command's standard output.
    Raises:
        RuntimeError: If the command fails or times out.
    """
    try:
        result = subprocess.run(
            ["gh", *args], input=json.dumps(payload) if payload is not None else None,
            capture_output=True, text=True, timeout=45)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("GitHub request timed out") from exc
    if result.returncode:
        raise RuntimeError("GitHub request failed: " + result.stderr.strip()[:400])
    return result.stdout


def pr_info(number: int) -> dict[str, Any]:
    """
    Read one pull request.
    Args:
        number: The PR number.
    Returns:
        dict: GitHub's pull request object.
    """
    return json.loads(gh("api", f"repos/{REPO}/pulls/{number}"))


def open_prs() -> list[dict[str, Any]]:
    """
    List the open pull requests that target main.
    Returns:
        list[dict]: GitHub's pull request objects.
    """
    return json.loads(gh("api", f"repos/{REPO}/pulls?state=open&base=main&per_page=100"))


def check_pr(info: dict[str, Any], sha: Optional[str] = None, base_sha: Optional[str] = None) -> None:
    """
    Check that a PR may be assessed and, optionally, that it is still at a given revision.
    Args:
        info: The PR, as returned by `pr_info`.
        sha: The head SHA the PR must still have.
        base_sha: The base SHA the PR must still have.
    Raises:
        ValueError: If the PR is closed, targets another branch, belongs to another developer,
            or has moved past the given revision.
    """
    if info["state"] != "open" or info["base"]["ref"] != "main":
        raise ValueError("The PR must be open and target main.")
    if info["user"]["login"] != DEVELOPER:
        raise ValueError("This single-user demo only assesses the configured developer's PRs.")
    if sha and info["head"]["sha"] != sha:
        raise ValueError("The PR has new commits. Generate a new quiz for its latest revision.")
    if base_sha and info["base"]["sha"] != base_sha:
        raise ValueError("The base branch changed. Update the PR and generate a new quiz.")


def publish_status(sha: str, state: str, description: str, target_url: str) -> None:
    """
    Post the `developer-quiz` commit status.
    Args:
        sha: The commit to mark.
        state: "pending", "success", "failure" or "error".
        description: The short text GitHub shows next to the check.
        target_url: Where the check's "Details" link points.
    """
    gh("api", f"repos/{REPO}/statuses/{sha}", "--method", "POST", "--input", "-", payload={
        "state": state, "context": CONTEXT, "description": description[:140],
        "target_url": target_url})


def gate_state(row: dict[str, Any], failed_attempt: bool = False) -> tuple[str, str]:
    """
    Decide a revision's `developer-quiz` status. A failed build or tests, or documentation
    problems, fail it whatever the quiz; otherwise a cosmetic change or a passed quiz succeeds.
    Args:
        row: The quiz row.
        failed_attempt: The developer just failed the quiz.
    Returns:
        tuple[str, str]: The state ("pending", "success" or "failure") and its description.
    """
    if not row["build_ok"]:
        return "failure", "Build or tests failed, or compiler warnings; see Details"
    if not row["docs_ok"]:
        return "failure", "Documentation problems in the changed files; see Details"
    if row["cosmetic"]:
        return "success", "Cosmetic change: no quiz needed; builds, documentation OK"
    if row["passed"] and row.get("skipped"):
        return "success", "Quiz skipped (proof-of-concept mode)"
    if row["passed"]:
        return "success", "Developer passed the revision-specific quiz"
    if failed_attempt:
        return "failure", "Quiz not passed; retry the assessment"
    return "pending", "Complete the developer quiz"


def comment_body(row: dict[str, Any], failed_attempt: bool = False) -> str:
    """
    The gate's pull request comment for a revision: build and tests, documentation, and the quiz.
    Args:
        row: The quiz row.
        failed_attempt: The developer just failed the quiz.
    Returns:
        str: Markdown, starting with COMMENT_MARKER.
    """
    url = f"{BASE_URL}/q/{row['id']}"

    def first_line(report: str) -> str:
        return (report or "").strip().splitlines()[0].removeprefix("$ ").replace("|", "\\|")[:150] if report else ""

    build = ("✅ " if row["build_ok"] else "❌ ") + (first_line(row["build_report"]) or "not checked")
    docs = ("✅ " if row["docs_ok"] else "❌ ") + (first_line(row["docs_report"]) if row["docs_ok"]
                                                 else "documentation problems; see the gate's page")
    if not row["build_ok"] or not row["docs_ok"]:
        quiz = f"🔒 Opens once the build, tests and documentation pass ([details]({url}))"
    elif row["cosmetic"]:
        quiz = "✅ Not needed: comments, formatting or documentation only"
    elif row["passed"] and row.get("skipped"):
        quiz = f"⏭ [Skipped]({url}) (proof-of-concept mode)"
    elif row["passed"]:
        quiz = f"✅ [Passed]({url})"
    elif failed_attempt:
        quiz = f"❌ Not passed yet: [try the quiz again]({url})"
    else:
        quiz = f"⏳ [Take the quiz]({url}) to unlock the merge"
    return (f"{COMMENT_MARKER}\n**Pull Request Gate** · revision `{row['sha'][:7]}`\n\n"
            f"| Check | Result |\n| --- | --- |\n| Build and tests | {build} |\n"
            f"| Documentation | {docs} |\n| Quiz | {quiz} |\n")


def publish_comment(row: dict[str, Any], failed_attempt: bool = False) -> None:
    """
    Post the gate's comment on the pull request, or update it: one comment per pull request, kept
    current with the latest revision. A failure is logged, never raised: the status check is what
    gates the merge.
    Args:
        row: The quiz row.
        failed_attempt: The developer just failed the quiz.
    """
    if not PR_COMMENT:
        return
    body = comment_body(row, failed_attempt)
    try:
        comments = json.loads(gh("api", f"repos/{REPO}/issues/{row['pr']}/comments?per_page=100"))
        mine = [c for c in comments if (c.get("body") or "").startswith(COMMENT_MARKER)]
        if mine and mine[-1]["body"] == body:
            return
        if mine:
            gh("api", f"repos/{REPO}/issues/comments/{mine[-1]['id']}", "--method", "PATCH", "--input", "-",
               payload={"body": body})
        else:
            gh("api", f"repos/{REPO}/issues/{row['pr']}/comments", "--method", "POST", "--input", "-",
               payload={"body": body})
    except (RuntimeError, ValueError, KeyError, TypeError) as exc:
        logging.getLogger("pr_gate").warning("PR #%s: could not post the gate's comment: %s", row["pr"], exc)


def publish(row: dict[str, Any], failed_attempt: bool = False) -> None:
    """
    Post a revision's status (see `gate_state`) and comment, and record what was posted.
    Args:
        row: The quiz row.
        failed_attempt: The developer just failed the quiz.
    """
    state, description = gate_state(row, failed_attempt)
    publish_comment(row, failed_attempt)
    publish_status(row["sha"], state, description, f"{BASE_URL}/q/{row['id']}")
    with connect() as db:
        # noinspection SqlNoDataSourceInspection
        db.execute("UPDATE quizzes SET published=? WHERE id=?", (state, row["id"]))


def get_quiz(qid: str) -> dict[str, Any]:
    """
    Read one quiz row.
    Args:
        qid: The quiz id.
    Returns:
        dict: The row, including its answer key.
    Raises:
        KeyError: If there is no such quiz.
    """
    with connect() as db:
        # noinspection SqlNoDataSourceInspection
        row = db.execute("SELECT * FROM quizzes WHERE id=?", (qid,)).fetchone()
    if row is None:
        raise KeyError(qid)
    return dict(row)


def find_quiz(number: int, sha: str, base_sha: str) -> Optional[dict[str, Any]]:
    """
    Find the latest quiz for one revision of a PR.
    Args:
        number: The PR number.
        sha: The head SHA.
        base_sha: The base SHA.
    Returns:
        Optional[dict]: The quiz row, or None if that revision has no quiz yet.
    """
    with connect() as db:
        # noinspection SqlNoDataSourceInspection
        row = db.execute(
            "SELECT * FROM quizzes WHERE pr=? AND sha=? AND base_sha=? AND developer=? "
            "ORDER BY created DESC LIMIT 1", (number, sha, base_sha, DEVELOPER)).fetchone()
    return dict(row) if row else None


def list_quizzes() -> list[dict[str, Any]]:
    """
    List every quiz, newest first, without content or answer keys.
    Returns:
        list[dict]: id, pr, sha, passed, build_ok, docs_ok, cosmetic, published and created per quiz.
    """
    with connect() as db:
        # noinspection SqlNoDataSourceInspection
        return [dict(r) for r in db.execute(
            "SELECT id,pr,sha,passed,skipped,build_ok,docs_ok,cosmetic,published,created FROM quizzes "
            "ORDER BY created DESC")]


NOT_FOUND = "(not found in this repository)"  # The title of an assessment whose pull request is gone


def history(pr: Optional[int] = None) -> list[dict[str, Any]]:
    """
    Every assessment, newest first, with its attempts and outcome, for the history page. Titles
    missing from assessments made before they were stored are fetched from GitHub once and saved,
    but only when the pull request contains the assessed commit: a pull request number can belong
    to a different request (a repository that was recreated), which is marked NOT_FOUND instead.
    Args:
        pr: Only this pull request; None for all.
    Returns:
        list[dict]: Each assessment's row (without content or answer keys), plus attempts (count),
            best and total (best score), last_attempt (time) and state / outcome (see gate_state).
    """
    with connect() as db:
        # noinspection SqlNoDataSourceInspection
        rows = [dict(r) for r in db.execute(
            "SELECT q.id, q.pr, q.pr_title, q.sha, q.source, q.passed, q.skipped, q.build_ok, q.docs_ok, "
            "q.cosmetic, q.created, q.rowid AS position, COUNT(a.id) AS attempts, MAX(a.score) AS best, MAX(a.total) AS total, "
            "MAX(a.created) AS last_attempt FROM quizzes q LEFT JOIN attempts a ON a.quiz_id = q.id "
            "WHERE ? IS NULL OR q.pr = ? GROUP BY q.id ORDER BY q.created DESC, position DESC", (pr, pr))]
    for number in sorted({row["pr"] for row in rows if not row["pr_title"]}):
        try:
            title = pr_info(number).get("title", "")
            commits = {c["sha"] for c in json.loads(gh("api", f"repos/{REPO}/pulls/{number}/commits?per_page=100"))}
        except (RuntimeError, ValueError, KeyError, TypeError):
            continue  # GitHub unreachable: show the number only, and try again next time
        with connect() as db:
            for row in rows:
                if row["pr"] == number and not row["pr_title"]:
                    row["pr_title"] = title if row["sha"] in commits else NOT_FOUND
                    # noinspection SqlNoDataSourceInspection
                    db.execute("UPDATE quizzes SET pr_title=? WHERE id=?", (row["pr_title"], row["id"]))
    for row in rows:
        row["state"], row["outcome"] = gate_state(row)
    return rows


def load_instructions(path: Path = INSTRUCTIONS_FILE) -> str:
    """
    Read the quiz writer's instructions file.
    Args:
        path: The JSON file (default: pr_gate/context/instructions.json).
    Returns:
        str: Its "instructions" lines, joined with newlines.
    """
    return "\n".join(json.loads(path.read_text(encoding="utf-8"))["instructions"])


# Keep the gate independent of the agents' dependencies while matching their model discovery.
# noinspection DuplicatedCode
def loaded_model(base_url: str, api_key: str = "") -> Optional[str]:
    """
    Ask an LM Studio server which model is loaded (its /api/v0/models lists each model's state).
    Args:
        base_url: The server's OpenAI-compatible base URL, e.g. http://boba:1234/v1.
        api_key: Sent as a bearer token, for servers that check it.
    Returns:
        Optional[str]: The first loaded language model's id, or None when none is loaded or the
            server cannot tell (not LM Studio, unreachable).
    """
    root = base_url.rstrip("/").removesuffix("/v1")
    request = urllib.request.Request(root + "/api/v0/models", headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            models = json.loads(response.read()).get("data", [])
    except (OSError, ValueError):
        return None
    return next((m["id"] for m in models if m.get("state") == "loaded" and m.get("type") in ("llm", "vlm")), None)


# Match the agents' profile selection without importing their implementations.
# noinspection DuplicatedCode
def resolve_model(profile: Optional[str] = None, path: Path = MODELS_FILE) -> dict[str, Any]:
    """
    Pick a model profile from the agents' shared models file, the same way the agents do.
    The environment variables a profile names (model_env, base_url_env) override its values, and
    with "model_auto" the model loaded on the server (LM Studio) replaces its model; the
    API key is read only from its api_key_env (or its api_key fallback), so a key is never sent
    to a server it was not configured for.
    Args:
        profile: Profile name ("local" or "openai"); None uses the file's default.
        path: The models file (default: agents/context/models.json).
    Returns:
        dict: name, base_url, model, api_key and timeout.
    Raises:
        ValueError: If the profile does not exist, lacks base_url or model, or its API key is not set.
    """
    models = json.loads(path.read_text(encoding="utf-8"))
    profiles = models.get("profiles") or {}
    name = profile or models.get("default")
    settings = profiles.get(name)
    if not isinstance(settings, dict):
        available = ", ".join(profiles) or "none"
        raise ValueError(f"Unknown model profile '{name}' (available: {available}). Check the models file.")
    missing = [key for key in ("base_url", "model") if not settings.get(key)]
    if missing:
        raise ValueError(f"Model profile '{name}' is missing {', '.join(missing)}.")
    api_key = (os.environ.get(settings.get("api_key_env", ""), "") or settings.get("api_key", "")).strip()
    if not api_key:
        raise ValueError(f"Set {settings.get('api_key_env', 'an API key')} in the environment for the '{name}' profile.")
    base_url = os.environ.get(settings.get("base_url_env", "")) or settings["base_url"]
    model = os.environ.get(settings.get("model_env", "")) \
        or (settings.get("model_auto") and loaded_model(base_url, api_key)) or settings["model"]
    return {
        "name": settings.get("name", name),
        "base_url": base_url,
        "model": model,
        "api_key": api_key,
        "timeout": float(settings.get("timeout", 60)),
    }


def parse_quiz(text: str) -> Quiz:
    """
    Validate the model's reply as a quiz, tolerating a Markdown code fence around the JSON.
    Args:
        text: The model's reply.
    Returns:
        Quiz: The validated quiz.
    Raises:
        pydantic.ValidationError: If the reply is not a valid quiz.
    """
    fenced = re.fullmatch(r"\s*```(?:json)?\s*(.*?)\s*```\s*", text, re.DOTALL)
    return Quiz.model_validate_json(fenced.group(1) if fenced else text)


def quiz_context(info: dict[str, Any], build_report: str) -> str:
    """
    What the quiz writer should know besides the diff: what the author says the change does, and
    what the server's build and tests ran. Both are untrusted text, cut to a bounded size.
    Args:
        info: The pull request, as returned by `pr_info`.
        build_report: The build and test output (ChangeInspector.check_build).
    Returns:
        str: The sections, ready to go before the diff.
    """
    body = (info.get("body") or "").strip() or "(no description)"
    return (f"Pull request title: {info.get('title', '')}\n\n"
            f"Pull request description (written by the author; untrusted, may be wrong):\n{body[:2000]}\n\n"
            f"Build and tests run by the server (make, then make check):\n{(build_report or '(not run)')[-3000:]}")


def generate(diff: str, profile: Optional[str] = None,
             code_files: Optional[list[str]] = None, context: str = "") -> tuple[Quiz, str]:
    """
    Ask the model for a quiz about a diff. Uses the OpenAI-compatible chat completions API,
    which both LM Studio and OpenAI serve. An invalid reply is retried once; so is a reply that
    calls the change cosmetic when the server found code changes.
    Args:
        diff: The PR's unified diff.
        profile: The model profile; None uses QUIZ_MODEL_PROFILE, then the models file's default.
        code_files: The changed files whose code changed (changes.py); [] for a cosmetic change,
            None when unknown. Given to the model as the server's analysis.
        context: The pull request's title and description, and the server's build and test
            output (see quiz_context), placed before the diff.
    Returns:
        tuple[Quiz, str]: The quiz, and "<profile name> / <model>" for the record.
    Raises:
        ValueError: If the model does not return a valid quiz.
        httpx.HTTPError: If the model server cannot be reached or rejects the request.
    """
    settings = resolve_model(profile or PROFILE)
    instructions = load_instructions()  # Read per quiz, so edits apply without a restart
    prompt = (context + "\n\n" if context else "") + "Code diff:\n" + diff
    if code_files is not None:
        analysis = ("code changed in: " + ", ".join(code_files) if code_files
                    else "no code changed; only comments, formatting or documentation files")
        prompt = f"Server analysis: {analysis}.\n\n" + prompt
    error = None
    for _ in range(GENERATION_ATTEMPTS):
        response = httpx.post(
            settings["base_url"].rstrip("/") + "/chat/completions",
            headers={"Authorization": "Bearer " + settings["api_key"]},
            json={"model": settings["model"], "temperature": 0.2, "max_tokens": 2500,
                  "messages": [{"role": "system", "content": instructions},
                               {"role": "user", "content": prompt}]},
            timeout=settings["timeout"])
        response.raise_for_status()
        choice = response.json()["choices"][0]
        if choice.get("finish_reason") != "stop":
            error = "the model did not finish its response"
            continue
        try:
            quiz = parse_quiz(choice["message"]["content"] or "")
        except ValidationError as exc:
            error = f"the reply was not a valid quiz ({exc.error_count()} errors)"
            continue
        if quiz.cosmetic and code_files:
            error = "the model called a code change cosmetic"
            continue
        return quiz, f"{settings['name']} / {settings['model']}"
    raise ValueError(f"No quiz was created: {error}.")


def create_quiz(number: int, profile: Optional[str] = None, fixed: Optional[str] = None) -> dict[str, Any]:
    """
    Make sure the PR's current revision is assessed, and post its status: check the changed files'
    documentation and whether the change is cosmetic, then have the model write the quiz.
    An existing assessment of the same revision is reused (re-posting its status), so a pass is
    never thrown away.
    Args:
        number: The PR number.
        profile: The model profile; None uses QUIZ_MODEL_PROFILE, then the models file's default.
        fixed: A JSON quiz file to use instead of the model (for demos and tests).
    Returns:
        dict: The quiz row.
    Raises:
        ValueError: If the PR may not be assessed, its diff is empty or too large, it moved
            during generation, the model did not return a valid quiz, or a fixed quiz calls a
            code change cosmetic.
        RuntimeError: If a GitHub request fails.
    """
    init()
    before = pr_info(number)
    check_pr(before)
    head, base = before["head"]["sha"], before["base"]["sha"]
    existing = find_quiz(number, head, base)
    if existing:
        publish(existing)
        return existing

    diff = gh("pr", "diff", str(number), "--repo", REPO)
    if not diff.strip() or len(diff) > MAX_DIFF_CHARS:
        raise ValueError(f"Diff must be nonempty and at most {MAX_DIFF_CHARS:,} characters.")
    publish_status(head, "pending", "Checking documentation and preparing the developer quiz", BASE_URL + "/")
    inspection = ChangeInspector(gh, REPO, BUILD_COMMAND, TEST_TARGET, FAIL_ON_WARNINGS).inspect(number, head, base)
    if fixed:
        quiz, source = Quiz.model_validate_json(Path(fixed).read_text()), "fixed fixture"
        if quiz.cosmetic and inspection["code_files"]:
            raise ValueError("The fixed quiz calls the change cosmetic, but its code changed.")
    else:
        quiz, source = generate(diff, profile, inspection["code_files"],
                                quiz_context(before, inspection.get("build_report", "")))

    # Generation takes a while: make sure the quiz still matches the PR
    after = pr_info(number)
    check_pr(after, head, base)

    # Shuffle choices so the model's preferred answer position is not a hint
    for question in quiz.questions:
        pairs = list(enumerate(question.options))
        secrets.SystemRandom().shuffle(pairs)
        question.correct = next(i for i, (old, _) in enumerate(pairs) if old == question.correct)
        question.options = [text for _, text in pairs]

    qid = secrets.token_urlsafe(16)
    with connect() as db:
        # noinspection SqlNoDataSourceInspection
        db.execute(
            "INSERT INTO quizzes(id,pr,sha,base_sha,developer,content,source,build_ok,build_report,"
            "docs_ok,docs_report,cosmetic,pr_title) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (qid, number, head, base, DEVELOPER, quiz.model_dump_json(), source,
             int(inspection.get("build_ok", True)), inspection.get("build_report", ""),
             int(inspection["docs_ok"]), inspection["docs_report"], int(quiz.cosmetic), after.get("title", "")))
    row = get_quiz(qid)
    publish(row)
    return row


def takes_quiz(row: dict[str, Any]) -> None:
    """
    Check that a revision is ready for its quiz.
    Args:
        row: The quiz row.
    Raises:
        ValueError: If the build, tests or documentation failed, or the change is cosmetic.
    """
    if not row["build_ok"]:
        raise ValueError("Fix the build or the tests and push; this revision cannot pass.")
    if not row["docs_ok"]:
        raise ValueError("Fix the documentation problems and push; this revision cannot pass.")
    if row["cosmetic"]:
        raise ValueError("This is a cosmetic change; it needs no quiz.")


def skip(qid: str) -> dict[str, Any]:
    """
    Skip a revision's quiz (proof-of-concept mode, QUIZ_ALLOW_SKIP): the check succeeds as
    "skipped", recorded as such. The build, tests and documentation must still pass.
    Args:
        qid: The quiz id.
    Returns:
        dict: skipped and passed (True), and the questions with explanations.
    Raises:
        KeyError: If there is no such quiz.
        ValueError: If skipping is off, the revision is not ready for its quiz, or the PR moved.
        RuntimeError: If a GitHub request fails (the skip is already saved; retry).
    """
    if not ALLOW_SKIP:
        raise ValueError("Skipping the quiz is turned off (QUIZ_ALLOW_SKIP).")
    row = get_quiz(qid)
    takes_quiz(row)
    check_pr(pr_info(row["pr"]), row["sha"], row["base_sha"])
    with connect() as db:  # A revision already passed by quiz stays passed, not skipped
        # noinspection SqlNoDataSourceInspection
        db.execute("UPDATE quizzes SET skipped=1, passed=1 WHERE id=? AND passed=0", (qid,))
    row = get_quiz(qid)
    publish(row)
    quiz = Quiz.model_validate_json(row["content"])
    return {"skipped": bool(row["skipped"]), "passed": True, "score": 0, "total": len(quiz.questions),
            "questions": quiz.questions}


def submit(qid: str, answers: list[int]) -> dict[str, Any]:
    """
    Grade an attempt and post the result. Only a perfect score passes; a revision that passed
    once stays passed.
    Args:
        qid: The quiz id.
        answers: One option index (0..3) per question.
    Returns:
        dict: score, total, passed, and the questions with explanations once passed.
    Raises:
        KeyError: If there is no such quiz.
        ValueError: If the revision takes no quiz (documentation problems or a cosmetic change), the
            answers are malformed, or the PR moved past the quiz's revision.
        RuntimeError: If a GitHub request fails (the attempt is already saved; resubmit).
    """
    row = get_quiz(qid)
    takes_quiz(row)
    check_pr(pr_info(row["pr"]), row["sha"], row["base_sha"])
    quiz = Quiz.model_validate_json(row["content"])
    if len(answers) != len(quiz.questions) or any(type(a) is not int or a not in range(4) for a in answers):
        raise ValueError("Answer every question with one of its four choices.")
    score = sum(a == q.correct for a, q in zip(answers, quiz.questions, strict=True))
    passed = score == len(quiz.questions)

    # Persist before publishing, so a GitHub failure can be retried
    with connect() as db:
        # noinspection SqlNoDataSourceInspection
        db.execute("INSERT INTO attempts(quiz_id,developer,score,total,passed) VALUES(?,?,?,?,?)",
                   (qid, row["developer"], score, len(quiz.questions), passed))
        # noinspection SqlNoDataSourceInspection
        db.execute("UPDATE quizzes SET passed=MAX(passed,?) WHERE id=?", (int(passed), qid))
    row = get_quiz(qid)

    # Recheck after grading, before writing success to the exact tested SHA
    check_pr(pr_info(row["pr"]), row["sha"], row["base_sha"])
    publish(row, failed_attempt=not row["passed"])
    return {"score": score, "total": len(quiz.questions), "passed": bool(row["passed"]),
            "questions": quiz.questions if row["passed"] else []}
