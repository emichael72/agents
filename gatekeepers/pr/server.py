"""
Module: server.py

Description:
    The quiz web service: the developer opens a quiz from the PR's `developer-quiz` check,
    answers it in the browser and the server grades it. A background poller watches GitHub and
    creates a quiz for every new revision of an open PR, so pushing a commit is all it takes.

    The service provides:
      - `GateApp`, the web application: `/` lists the quizzes and what the poller is doing,
        `/history` every assessment, `/q/<id>` shows and grades a quiz, and `/health` answers
        liveness checks (no authentication).
      - `Poller`, which lists the open PRs every few seconds and calls `QuizGate.create` for
        each revision that has no quiz yet.

    Key design points:
      - Grading happens on the server; the browser only receives questions and choices.
      - A demo sign-in page (user / pass by default, shown on the page) that sets a signed
        cookie, and a per-quiz form token. Plain HTTP: meant for a trusted network only.
      - Polling instead of webhooks, so the host needs only outbound access to GitHub.
      - One generation and one submission at a time.
"""

import hashlib
import hmac
import logging
import secrets
import threading
import time
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional
from urllib.parse import quote

# Third-party
import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

# Local imports
from gatekeepers.pr import TEMPLATES_DIR
from gatekeepers.pr.clone import LocalClone
from gatekeepers.pr.gate import QuizGate
from gatekeepers.pr.quiz import Quiz

logger = logging.getLogger("pr_gate")


class Poller:
    """
    Watches the repository's open PRs and creates a quiz for each new revision.
    A revision whose quiz cannot be generated is retried on the next polls, up to
    MAX_FAILURES times; pushing a new commit starts over.
    """

    MAX_FAILURES = 3  # Generation attempts per revision before the poller gives up on it

    def __init__(self, gate: QuizGate, interval: float = 30, profile: Optional[str] = None) -> None:
        """
        Args:
            gate: The gate that assesses each revision.
            interval: Seconds between polls.
            profile: The model profile for generation; None uses the default.
        """
        self.gate = gate
        self._interval = interval
        self._profile = profile
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._failures: dict[tuple[int, str, str], int] = {}
        self.working: Optional[int] = None  # The PR being quizzed right now, for the home page
        self.last_poll: Optional[str] = None
        self.last_error: Optional[str] = None
        self.last_sync: Optional[str] = None  # The local clone's last sync, for the home page
        self._next_sync = 0.0

    def start(self) -> None:
        """Start polling in a background thread."""
        thread = threading.Thread(target=self._run, name="pr-gate-poller", daemon=True)
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        """Ask the poller to stop; a generation in progress finishes first."""
        self._stop.set()

    def _run(self) -> None:
        """Poll until stopped, logging errors rather than ending the thread."""
        while not self._stop.is_set():
            try:
                self.poll_once()
                self.last_error = None
            except Exception as exc:  # Keep polling whatever goes wrong
                self.last_error = str(exc)
                logger.warning("Poll failed: %s", exc)
            self.sync_clone()
            self._stop.wait(self._interval)

    def sync_clone(self) -> None:
        """
        Every SYNC_SECONDS, fast-forward the local clone (QUIZ_LOCAL_CLONE) to GitHub when it is
        safe (see LocalClone.sync), so agents start from current code. Logged when it changes.
        """
        settings = self.gate.settings
        if not settings.local_clone or time.monotonic() < self._next_sync:
            return
        self._next_sync = time.monotonic() + settings.sync_seconds
        try:
            outcome, message = LocalClone(settings.local_clone).sync()
        except Exception as exc:  # Never stop polling for this
            outcome, message = "skipped", str(exc)
        if outcome == "updated" or (outcome == "skipped" and message != self.last_sync):
            logger.info("Local clone: %s", message)
        self.last_sync = f"{message} ({time.strftime('%H:%M:%S')})"

    def poll_once(self) -> list[int]:
        """
        Create the missing quizzes for the open PRs.
        Returns:
            list[int]: The PRs a quiz was created for.
        """
        created = []
        for pr in self.gate.github.open_prs():
            if pr["user"]["login"] != self.gate.settings.developer:
                continue
            number, head, base = pr["number"], pr["head"]["sha"], pr["base"]["sha"]
            key = (number, head, base)
            if self.gate.find_quiz(*key) or self._failures.get(key, 0) >= self.MAX_FAILURES:
                continue
            logger.info("PR #%s at %s has no quiz; generating one", number, head[:7])
            self.working = number
            try:
                row = self.gate.create(number, self._profile)
            except (ValueError, RuntimeError, httpx.HTTPError) as exc:
                self._record_failure(key, exc)
                continue
            finally:
                self.working = None
            logger.info("PR #%s: quiz ready at %s", number, self.gate.quiz_url(row["id"]))
            created.append(number)
        self.last_poll = time.strftime("%Y-%m-%d %H:%M:%S")
        return created

    def _record_failure(self, key: tuple[int, str, str], exc: Exception) -> None:
        """
        Count a failed generation; after the last attempt, tell the PR so it does not wait forever.
        Args:
            key: (PR number, head SHA, base SHA).
            exc: What went wrong.
        """
        self._failures[key] = self._failures.get(key, 0) + 1
        logger.warning("PR #%s: quiz generation failed (%s/%s): %s",
                       key[0], self._failures[key], self.MAX_FAILURES, exc)
        if self._failures[key] >= self.MAX_FAILURES:
            try:
                self.gate.github.publish_status(key[1], "error",
                                                "Quiz generation failed; push again or create it by hand",
                                                self.gate.settings.base_url + "/")
            except RuntimeError as status_exc:
                logger.warning("PR #%s: could not post the error status: %s", key[0], status_exc)


class GateApp:
    """
    The web application: the demo sign-in, the assessments, the history and the quiz pages. The
    FastAPI application is `app`; the gate's settings are read per request.
    """

    SESSION_COOKIE = "pr_gate_session"

    def __init__(self, gate: QuizGate, poller: Optional[Poller] = None) -> None:
        """
        Build the application.
        Args:
            gate: The gate whose quizzes it serves.
            poller: Started and stopped with the application; None serves quizzes without polling.
        """
        self.gate = gate
        self.poller = poller
        self.secret = gate.store.init()
        self.templates = Jinja2Templates(directory=TEMPLATES_DIR)
        self.submission_lock = threading.Lock()  # One submission or skip at a time
        self.app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=self.lifespan)
        self.app.middleware("http")(self.headers)
        self.app.get("/health")(self.health)
        self.app.get("/login", response_class=HTMLResponse)(self.login_page)
        self.app.post("/login", response_class=HTMLResponse)(self.login)
        self.app.get("/", response_class=HTMLResponse)(self.home)
        self.app.get("/history", response_class=HTMLResponse)(self.history_page)
        self.app.get("/q/{qid}", response_class=HTMLResponse)(self.quiz_page)
        self.app.post("/q/{qid}", response_class=HTMLResponse)(self.grade)
        self.app.post("/q/{qid}/skip", response_class=HTMLResponse)(self.skip)

    @asynccontextmanager
    async def lifespan(self, _app: FastAPI) -> AsyncIterator[None]:
        """Run the poller while the application runs."""
        if self.poller:
            self.poller.start()
        yield
        if self.poller:
            self.poller.stop()

    @staticmethod
    def local_time(stamp: Optional[str]) -> str:
        """
        Show a database time (UTC, "YYYY-MM-DD HH:MM:SS") in the server's local time zone.
        Args:
            stamp: The stored time, or None.
        Returns:
            str: e.g. "2026-10-07 00:14 IDT", or "" for None.
        """
        if not stamp:
            return ""
        moment = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).astimezone()
        return moment.strftime("%Y-%m-%d %H:%M %Z")

    def sign(self, value: str) -> str:
        """Sign a value with the secret key: the session cookie and the form tokens."""
        return hmac.new(self.secret.encode(), value.encode(), hashlib.sha256).hexdigest()

    def authenticate(self, request: Request) -> str:
        """
        Check the sign-in cookie.
        Args:
            request: The request.
        Returns:
            str: The signed-in user.
        Raises:
            HTTPException: 303 to the sign-in page, then back here, when not signed in.
        """
        user = self.gate.settings.web_user
        if not secrets.compare_digest(request.cookies.get(self.SESSION_COOKIE, ""), self.sign("session:" + user)):
            raise HTTPException(303, headers={"Location": "/login?next=" + quote(request.url.path)})
        return user

    def csrf(self, qid: str) -> str:
        """The form token of one quiz."""
        return self.sign(qid)

    def locked_submit(self, qid: str, answers: list[int]) -> dict[str, Any]:
        """Grade an attempt (QuizGate.submit), one at a time."""
        with self.submission_lock:
            return self.gate.submit(qid, answers)

    def locked_skip(self, qid: str) -> dict[str, Any]:
        """Skip a quiz (QuizGate.skip), one at a time."""
        with self.submission_lock:
            return self.gate.skip(qid)

    def login_context(self, target: str, failed: bool) -> dict[str, Any]:
        """The sign-in page's values: where to go next, and the demo credentials it shows."""
        settings = self.gate.settings
        return {"next": target, "user": settings.web_user, "password": settings.web_password, "failed": failed}

    @staticmethod
    async def headers(request: Request, call_next):
        """Add the security headers to every response."""
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
            "frame-ancestors 'none'; base-uri 'none'")
        return response

    @staticmethod
    def health():
        """GET /health: liveness, without authentication."""
        return {"status": "ok"}

    def login_page(self, request: Request, target: str = Query("/", alias="next")):
        """GET /login: the sign-in form."""
        return self.templates.TemplateResponse(request=request, name="login.html",
                                               context=self.login_context(target, False))

    async def login(self, request: Request):
        """POST /login: check the credentials and set the session cookie."""
        settings = self.gate.settings
        form = await request.form(max_fields=5)
        target = str(form.get("next", "/"))
        if not target.startswith("/") or target.startswith("//"):
            target = "/"  # Only redirect within this site
        valid_user = secrets.compare_digest(str(form.get("user", "")).encode(), settings.web_user.encode())
        valid_password = secrets.compare_digest(str(form.get("password", "")).encode(), settings.web_password.encode())
        if not (valid_user and valid_password):
            return self.templates.TemplateResponse(request=request, name="login.html", status_code=401,
                                                   context=self.login_context(target, True))
        response = RedirectResponse(target, status_code=303)
        response.set_cookie(self.SESSION_COOKIE, self.sign("session:" + settings.web_user), httponly=True,
                            samesite="lax")
        return response

    def home(self, request: Request):
        """GET /: the assessments and what the poller is doing."""
        user = self.authenticate(request)
        return self.templates.TemplateResponse(request=request, name="home.html", context={
            "rows": self.gate.store.quizzes(), "repo": self.gate.settings.repo, "user": user, "poller": self.poller})

    def history_page(self, request: Request, pr: Optional[int] = None):
        """GET /history: every assessment, or one pull request's."""
        self.authenticate(request)
        return self.templates.TemplateResponse(request=request, name="history.html", context={
            "rows": self.gate.history(pr), "repo": self.gate.settings.repo, "pr": pr, "local_time": self.local_time,
            "not_found": self.gate.NOT_FOUND})

    def quiz_page(self, qid: str, request: Request):
        """GET /q/<id>: a revision's assessment, and its quiz when it takes one."""
        self.authenticate(request)
        try:
            row = self.gate.store.get(qid)
        except KeyError:
            raise HTTPException(404, "Quiz not found") from None
        content = Quiz.model_validate_json(row["content"])
        # Only public fields enter the rendered form
        questions = [{"question": q.question, "options": q.options} for q in content.questions]
        state, description = self.gate.gate_state(row)
        return self.templates.TemplateResponse(request=request, name="quiz.html", context={
            "row": row, "title": content.title, "questions": questions, "csrf": self.csrf(qid),
            "repo": self.gate.settings.repo, "state": state, "description": description,
            "allow_skip": self.gate.settings.allow_skip})

    async def grade(self, qid: str, request: Request):
        """POST /q/<id>: grade the answers and post the result."""
        self.authenticate(request)
        form = await request.form(max_fields=10)
        if not secrets.compare_digest(str(form.get("csrf", "")), self.csrf(qid)):
            raise HTTPException(403, "Invalid form token. Reload the quiz.")
        try:
            row = self.gate.store.get(qid)
            count = len(Quiz.model_validate_json(row["content"]).questions)
            try:
                answers = [int(str(form[f"q{i}"])) for i in range(count)]
            except (KeyError, ValueError):
                raise ValueError("Please answer every question.") from None
            result = await run_in_threadpool(self.locked_submit, qid, answers)
        except KeyError:
            raise HTTPException(404, "Quiz not found") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        except (RuntimeError, TimeoutError):
            raise HTTPException(502, "GitHub could not confirm the result. Retry this submission.") from None
        return self.templates.TemplateResponse(request=request, name="result.html",
                                               context={"result": result, "row": row, "repo": self.gate.settings.repo})

    async def skip(self, qid: str, request: Request):
        """POST /q/<id>/skip: skip the quiz (proof-of-concept mode) and post the result."""
        self.authenticate(request)
        form = await request.form(max_fields=10)
        if not secrets.compare_digest(str(form.get("csrf", "")), self.csrf(qid)):
            raise HTTPException(403, "Invalid form token. Reload the quiz.")
        try:
            row = self.gate.store.get(qid)
            result = await run_in_threadpool(self.locked_skip, qid)
        except KeyError:
            raise HTTPException(404, "Quiz not found") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        except (RuntimeError, TimeoutError):
            raise HTTPException(502, "GitHub could not confirm the result. Retry.") from None
        return self.templates.TemplateResponse(request=request, name="result.html",
                                               context={"result": result, "row": row, "repo": self.gate.settings.repo})
