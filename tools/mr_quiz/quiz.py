"""
Module: quiz.py

Description:
    The developer quiz: a model reads a pull request's diff and writes a short multiple-choice
    quiz about it; the PR may merge only after its author passes that quiz.

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
"""

import json
import os
import re
import secrets
import socket
import sqlite3
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

# Third-party
import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

TOOL_DIR = Path(__file__).resolve().parent
MODELS_FILE = TOOL_DIR.parent.parent / "context" / "models.json"
INSTRUCTIONS_FILE = TOOL_DIR / "context" / "instructions.json"

# Overridable from the environment, so the service and the tool share one configuration
DATA = Path(os.environ.get("QUIZ_DATA_DIR", TOOL_DIR / "data"))
REPO = os.environ.get("QUIZ_REPO", "emichael72/mr_quiz")
DEVELOPER = os.environ.get("QUIZ_DEVELOPER", "emichael72")
BASE_URL = os.environ.get("QUIZ_BASE_URL", f"http://{socket.gethostname()}:8000").rstrip("/")
PROFILE = os.environ.get("QUIZ_MODEL_PROFILE") or None  # None uses the models file's default

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
    """A quiz as the model writes it: a title and 3 to 5 questions."""
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(min_length=3, max_length=200)
    questions: list[Question] = Field(min_length=3, max_length=5)


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
    Create the database tables and the access password, if missing.
    Returns:
        str: The HTTP Basic password for the web pages (data/access_password).
    """
    with connect() as db:
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
    secret_file = DATA / "access_password"
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


def publish(row: dict[str, Any], state: str) -> None:
    """
    Post a quiz's status to its revision and record what was posted.
    Args:
        row: The quiz row.
        state: "pending", "success" or "failure".
    """
    description = {"pending": "Complete the developer quiz",
                   "failure": "Quiz not passed; retry the assessment",
                   "success": "Developer passed the revision-specific quiz"}[state]
    publish_status(row["sha"], state, description, f"{BASE_URL}/q/{row['id']}")
    with connect() as db:
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
        row = db.execute(
            "SELECT * FROM quizzes WHERE pr=? AND sha=? AND base_sha=? AND developer=? "
            "ORDER BY created DESC LIMIT 1", (number, sha, base_sha, DEVELOPER)).fetchone()
    return dict(row) if row else None


def list_quizzes() -> list[dict[str, Any]]:
    """
    List every quiz, newest first, without content or answer keys.
    Returns:
        list[dict]: id, pr, sha, passed, published and created per quiz.
    """
    with connect() as db:
        return [dict(r) for r in db.execute(
            "SELECT id,pr,sha,passed,published,created FROM quizzes ORDER BY created DESC")]


def load_instructions(path: Path = INSTRUCTIONS_FILE) -> str:
    """
    Read the quiz writer's instructions file.
    Args:
        path: The JSON file (default: mr_quiz/context/instructions.json).
    Returns:
        str: Its "instructions" lines, joined with newlines.
    """
    return "\n".join(json.loads(path.read_text(encoding="utf-8"))["instructions"])


def resolve_model(profile: Optional[str] = None, path: Path = MODELS_FILE) -> dict[str, Any]:
    """
    Pick a model profile from the agents' shared models file, the same way the agents do.
    The environment variables a profile names (model_env, base_url_env) override its values; the
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
    return {
        "name": settings.get("name", name),
        "base_url": os.environ.get(settings.get("base_url_env", "")) or settings["base_url"],
        "model": os.environ.get(settings.get("model_env", "")) or settings["model"],
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


def generate(diff: str, profile: Optional[str] = None) -> tuple[Quiz, str]:
    """
    Ask the model for a quiz about a diff. Uses the OpenAI-compatible chat completions API,
    which both LM Studio and OpenAI serve. An invalid reply is retried once.
    Args:
        diff: The PR's unified diff.
        profile: The model profile; None uses QUIZ_MODEL_PROFILE, then the models file's default.
    Returns:
        tuple[Quiz, str]: The quiz, and "<profile name> / <model>" for the record.
    Raises:
        ValueError: If the model does not return a valid quiz.
        httpx.HTTPError: If the model server cannot be reached or rejects the request.
    """
    settings = resolve_model(profile or PROFILE)
    instructions = load_instructions()  # Read per quiz, so edits apply without a restart
    error = None
    for _ in range(GENERATION_ATTEMPTS):
        response = httpx.post(
            settings["base_url"].rstrip("/") + "/chat/completions",
            headers={"Authorization": "Bearer " + settings["api_key"]},
            json={"model": settings["model"], "temperature": 0.2, "max_tokens": 2500,
                  "messages": [{"role": "system", "content": instructions},
                               {"role": "user", "content": "Code diff:\n" + diff}]},
            timeout=settings["timeout"])
        response.raise_for_status()
        choice = response.json()["choices"][0]
        if choice.get("finish_reason") != "stop":
            error = "the model did not finish its response"
            continue
        try:
            return parse_quiz(choice["message"]["content"] or ""), f"{settings['name']} / {settings['model']}"
        except ValidationError as exc:
            error = f"the reply was not a valid quiz ({exc.error_count()} errors)"
    raise ValueError(f"No quiz was created: {error}.")


def create_quiz(number: int, profile: Optional[str] = None, fixed: Optional[str] = None) -> dict[str, Any]:
    """
    Make sure the PR's current revision has a quiz, and post its pending status.
    An existing quiz for the same revision is reused (re-posting its status), so a pass is
    never thrown away.
    Args:
        number: The PR number.
        profile: The model profile; None uses QUIZ_MODEL_PROFILE, then the models file's default.
        fixed: A JSON quiz file to use instead of the model (for demos and tests).
    Returns:
        dict: The quiz row.
    Raises:
        ValueError: If the PR may not be assessed, its diff is empty or too large, it moved
            during generation, or the model did not return a valid quiz.
        RuntimeError: If a GitHub request fails.
    """
    init()
    before = pr_info(number)
    check_pr(before)
    head, base = before["head"]["sha"], before["base"]["sha"]
    existing = find_quiz(number, head, base)
    if existing:
        publish(existing, "success" if existing["passed"] else "pending")
        return existing

    diff = gh("pr", "diff", str(number), "--repo", REPO)
    if not diff.strip() or len(diff) > MAX_DIFF_CHARS:
        raise ValueError(f"Diff must be nonempty and at most {MAX_DIFF_CHARS:,} characters.")
    publish_status(head, "pending", "Preparing the developer quiz", BASE_URL + "/")
    if fixed:
        quiz, source = Quiz.model_validate_json(Path(fixed).read_text()), "fixed fixture"
    else:
        quiz, source = generate(diff, profile)

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
        db.execute(
            "INSERT INTO quizzes(id,pr,sha,base_sha,developer,content,source) VALUES(?,?,?,?,?,?,?)",
            (qid, number, head, base, DEVELOPER, quiz.model_dump_json(), source))
    row = get_quiz(qid)
    publish(row, "pending")
    return row


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
        ValueError: If the answers are malformed or the PR moved past the quiz's revision.
        RuntimeError: If a GitHub request fails (the attempt is already saved; resubmit).
    """
    row = get_quiz(qid)
    check_pr(pr_info(row["pr"]), row["sha"], row["base_sha"])
    quiz = Quiz.model_validate_json(row["content"])
    if len(answers) != len(quiz.questions) or any(type(a) is not int or a not in range(4) for a in answers):
        raise ValueError("Answer every question with one of its four choices.")
    score = sum(a == q.correct for a, q in zip(answers, quiz.questions))
    passed = score == len(quiz.questions)

    # Persist before publishing, so a GitHub failure can be retried
    with connect() as db:
        db.execute("INSERT INTO attempts(quiz_id,developer,score,total,passed) VALUES(?,?,?,?,?)",
                   (qid, row["developer"], score, len(quiz.questions), passed))
        db.execute("UPDATE quizzes SET passed=MAX(passed,?) WHERE id=?", (int(passed), qid))
    row = get_quiz(qid)

    # Recheck after grading, before writing success to the exact tested SHA
    check_pr(pr_info(row["pr"]), row["sha"], row["base_sha"])
    publish(row, "success" if row["passed"] else "failure")
    return {"score": score, "total": len(quiz.questions), "passed": bool(row["passed"]),
            "questions": quiz.questions if row["passed"] else []}
