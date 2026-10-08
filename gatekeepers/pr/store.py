"""
Module: store.py

Description:
    `QuizStore`: the gate's SQLite database (quizzes, with their answer keys, and attempts) and
    the secret key that signs the web service's cookies and form tokens. Both live in the data
    folder (QUIZ_DATA_DIR), created on first use.
"""

import os
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional


class QuizStore:
    """
    The quizzes and attempts, in data/quiz.sqlite3.
    """

    def __init__(self, data_dir: Path) -> None:
        """
        Args:
            data_dir: The data folder (QUIZ_DATA_DIR).
        """
        self.data_dir = data_dir

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """
        Open the quiz database; the block runs as one transaction.
        Yields:
            sqlite3.Connection: A connection whose rows read as sqlite3.Row.
        """
        self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        db = sqlite3.connect(self.data_dir / "quiz.sqlite3", timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def init(self) -> str:
        """
        Create the database tables and the secret key, if missing.
        Returns:
            str: The key that signs the sign-in cookie and the form tokens (data/secret_key).
        """
        with self.connect() as db:
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
            # Columns added after the first version; older databases get them with these defaults.
            # repo is "" for a quiz from when the gate served one repository (see adopt).
            for column in ("pr_title TEXT NOT NULL DEFAULT ''", "skipped INTEGER NOT NULL DEFAULT 0",
                           "build_ok INTEGER NOT NULL DEFAULT 1", "build_report TEXT NOT NULL DEFAULT ''",
                           "docs_ok INTEGER NOT NULL DEFAULT 1", "docs_report TEXT NOT NULL DEFAULT ''",
                           "cosmetic INTEGER NOT NULL DEFAULT 0", "repo TEXT NOT NULL DEFAULT ''"):
                try:
                    # noinspection SqlNoDataSourceInspection
                    db.execute("ALTER TABLE quizzes ADD COLUMN " + column)
                except sqlite3.OperationalError:
                    pass  # Already there
        secret_file = self.data_dir / "secret_key"
        if not secret_file.exists():
            try:
                fd = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass  # Another process created it first
            else:
                with os.fdopen(fd, "w") as f:
                    f.write(secrets.token_urlsafe(32))
        return secret_file.read_text().strip()

    def adopt(self, repo: str) -> int:
        """
        Give the quizzes from when the gate served one repository (repo "") to that repository.
        Args:
            repo: owner/name.
        Returns:
            int: How many quizzes it took.
        """
        with self.connect() as db:
            # noinspection SqlNoDataSourceInspection
            return db.execute("UPDATE quizzes SET repo=? WHERE repo=''", (repo,)).rowcount

    def add(self, row: dict[str, Any]) -> dict[str, Any]:
        """
        Store a new quiz.
        Args:
            row: id, repo, pr, sha, base_sha, developer, content, source, build_ok, build_report,
                docs_ok, docs_report, cosmetic and pr_title.
        Returns:
            dict: The stored row, as `get` reads it.
        """
        columns = ("id", "repo", "pr", "sha", "base_sha", "developer", "content", "source", "build_ok",
                   "build_report", "docs_ok", "docs_report", "cosmetic", "pr_title")
        with self.connect() as db:
            # noinspection SqlNoDataSourceInspection
            db.execute(f"INSERT INTO quizzes({','.join(columns)}) VALUES({','.join('?' * len(columns))})",
                       tuple(row[c] for c in columns))
        return self.get(row["id"])

    def get(self, qid: str) -> dict[str, Any]:
        """
        Read one quiz row.
        Args:
            qid: The quiz id.
        Returns:
            dict: The row, including its answer key.
        Raises:
            KeyError: If there is no such quiz.
        """
        with self.connect() as db:
            # noinspection SqlNoDataSourceInspection
            row = db.execute("SELECT * FROM quizzes WHERE id=?", (qid,)).fetchone()
        if row is None:
            raise KeyError(qid)
        return dict(row)

    def find(self, repo: str, number: int, sha: str, base_sha: str, developer: str) -> Optional[dict[str, Any]]:
        """
        Find the latest quiz for one revision of a PR.
        Args:
            repo: The repository, owner/name.
            number: The PR number.
            sha: The head SHA.
            base_sha: The base SHA.
            developer: The PR's author.
        Returns:
            Optional[dict]: The quiz row, or None if that revision has no quiz yet.
        """
        with self.connect() as db:
            # noinspection SqlNoDataSourceInspection
            row = db.execute(
                "SELECT * FROM quizzes WHERE repo=? AND pr=? AND sha=? AND base_sha=? AND developer=? "
                "ORDER BY created DESC LIMIT 1", (repo, number, sha, base_sha, developer)).fetchone()
        return dict(row) if row else None

    def quizzes(self) -> list[dict[str, Any]]:
        """
        List every quiz, newest first, without content or answer keys.
        Returns:
            list[dict]: id, repo, pr, sha, passed, build_ok, docs_ok, cosmetic, published and created per quiz.
        """
        with self.connect() as db:
            # noinspection SqlNoDataSourceInspection
            return [dict(r) for r in db.execute(
                "SELECT id,repo,pr,sha,passed,skipped,build_ok,docs_ok,cosmetic,published,created FROM quizzes "
                "ORDER BY created DESC")]

    def assessments(self, repo: Optional[str] = None, pr: Optional[int] = None) -> list[dict[str, Any]]:
        """
        Every quiz, newest first, with its attempts summarized.
        Args:
            repo: Only this repository (owner/name); None for all.
            pr: Only this pull request; None for all.
        Returns:
            list[dict]: Each quiz's row (without content or answer keys), plus attempts (count),
                best and total (best score) and last_attempt (time).
        """
        with self.connect() as db:
            # noinspection SqlNoDataSourceInspection
            return [dict(r) for r in db.execute(
                "SELECT q.id, q.repo, q.pr, q.pr_title, q.sha, q.source, q.passed, q.skipped, q.build_ok, q.docs_ok, "
                "q.cosmetic, q.created, q.rowid AS position, COUNT(a.id) AS attempts, MAX(a.score) AS best, "
                "MAX(a.total) AS total, MAX(a.created) AS last_attempt FROM quizzes q "
                "LEFT JOIN attempts a ON a.quiz_id = q.id "
                "WHERE (? IS NULL OR q.repo = ?) AND (? IS NULL OR q.pr = ?) "
                "GROUP BY q.id ORDER BY q.created DESC, position DESC", (repo, repo, pr, pr))]

    def set_title(self, qid: str, title: str) -> None:
        """Save the title of a quiz's pull request."""
        with self.connect() as db:
            # noinspection SqlNoDataSourceInspection
            db.execute("UPDATE quizzes SET pr_title=? WHERE id=?", (title, qid))

    def set_published(self, qid: str, state: str) -> None:
        """Record the status last posted for a quiz's revision."""
        with self.connect() as db:
            # noinspection SqlNoDataSourceInspection
            db.execute("UPDATE quizzes SET published=? WHERE id=?", (state, qid))

    def mark_skipped(self, qid: str) -> None:
        """Mark a quiz skipped and passed; a revision already passed by quiz stays passed, not skipped."""
        with self.connect() as db:
            # noinspection SqlNoDataSourceInspection
            db.execute("UPDATE quizzes SET skipped=1, passed=1 WHERE id=? AND passed=0", (qid,))

    def add_attempt(self, qid: str, developer: str, score: int, total: int, passed: bool) -> None:
        """
        Record an attempt; a quiz that was passed once stays passed.
        Args:
            qid: The quiz id.
            developer: Who answered.
            score: Correct answers.
            total: Questions.
            passed: Whether every answer was correct.
        """
        with self.connect() as db:
            # noinspection SqlNoDataSourceInspection
            db.execute("INSERT INTO attempts(quiz_id,developer,score,total,passed) VALUES(?,?,?,?,?)",
                       (qid, developer, score, total, passed))
            # noinspection SqlNoDataSourceInspection
            db.execute("UPDATE quizzes SET passed=MAX(passed,?) WHERE id=?", (int(passed), qid))
