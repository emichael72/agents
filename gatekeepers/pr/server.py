"""
Module: server.py

Description:
    The quiz web service: the developer opens a quiz from the PR's `developer-quiz` check,
    answers it in the browser and the server grades it. A background poller watches GitHub and
    creates a quiz for every new revision of an open PR, so pushing a commit is all it takes.

    The service provides:
      - `/` lists the quizzes and what the poller is doing; `/q/<id>` shows and grades a quiz.
      - `/health` for liveness checks (no authentication).
      - `Poller`, which lists the open PRs every few seconds and calls `quiz.create_quiz` for
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
from contextlib import asynccontextmanager
from typing import Any, Optional
from urllib.parse import quote

# Third-party
import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

# Local imports
import quiz

MAX_FAILURES = 3  # Generation attempts per revision before the poller gives up on it
SESSION_COOKIE = "pr_gate_session"

logger = logging.getLogger("pr_gate")


class Poller:
    """
    Watches the repository's open PRs and creates a quiz for each new revision.
    A revision whose quiz cannot be generated is retried on the next polls, up to
    MAX_FAILURES times; pushing a new commit starts over.
    """

    def __init__(self, interval: float = 30, profile: Optional[str] = None) -> None:
        """
        Args:
            interval: Seconds between polls.
            profile: The model profile for generation; None uses the default.
        """
        self._interval = interval
        self._profile = profile
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._failures: dict[tuple[int, str, str], int] = {}
        self.working: Optional[int] = None  # The PR being quizzed right now, for the home page
        self.last_poll: Optional[str] = None
        self.last_error: Optional[str] = None

    def start(self) -> None:
        """Start polling in a background thread."""
        self._thread = threading.Thread(target=self._run, name="pr-gate-poller", daemon=True)
        self._thread.start()

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
            self._stop.wait(self._interval)

    def poll_once(self) -> list[int]:
        """
        Create the missing quizzes for the open PRs.
        Returns:
            list[int]: The PRs a quiz was created for.
        """
        created = []
        for pr in quiz.open_prs():
            if pr["user"]["login"] != quiz.DEVELOPER:
                continue
            number, head, base = pr["number"], pr["head"]["sha"], pr["base"]["sha"]
            key = (number, head, base)
            if quiz.find_quiz(*key) or self._failures.get(key, 0) >= MAX_FAILURES:
                continue
            logger.info("PR #%s at %s has no quiz; generating one", number, head[:7])
            self.working = number
            try:
                row = quiz.create_quiz(number, self._profile)
            except (ValueError, RuntimeError, httpx.HTTPError) as exc:
                self._record_failure(key, exc)
                continue
            finally:
                self.working = None
            logger.info("PR #%s: quiz ready at %s/q/%s", number, quiz.BASE_URL, row["id"])
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
                       key[0], self._failures[key], MAX_FAILURES, exc)
        if self._failures[key] >= MAX_FAILURES:
            try:
                quiz.publish_status(key[1], "error", "Quiz generation failed; push again or create it by hand",
                                    quiz.BASE_URL + "/")
            except RuntimeError as status_exc:
                logger.warning("PR #%s: could not post the error status: %s", key[0], status_exc)


def create_app(poller: Optional[Poller] = None) -> FastAPI:
    """
    Build the web application.
    Args:
        poller: Started and stopped with the application; None serves quizzes without polling.
    Returns:
        FastAPI: The application.
    """
    secret = quiz.init()
    templates = Jinja2Templates(directory=quiz.GATE_DIR / "templates")
    submission_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if poller:
            poller.start()
        yield
        if poller:
            poller.stop()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    def sign(value: str) -> str:
        return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()

    def authenticate(request: Request) -> str:
        # Not signed in: send the browser to the sign-in page, then back here
        if not secrets.compare_digest(request.cookies.get(SESSION_COOKIE, ""), sign("session:" + quiz.WEB_USER)):
            raise HTTPException(303, headers={"Location": "/login?next=" + quote(request.url.path)})
        return quiz.WEB_USER

    def csrf(qid: str) -> str:
        return sign(qid)

    def locked_submit(qid: str, answers: list[int]) -> dict[str, Any]:
        with submission_lock:
            return quiz.submit(qid, answers)

    def locked_skip(qid: str) -> dict[str, Any]:
        with submission_lock:
            return quiz.skip(qid)

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
            "frame-ancestors 'none'; base-uri 'none'")
        return response

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, target: str = Query("/", alias="next")):
        return templates.TemplateResponse(request=request, name="login.html", context={
            "next": target, "user": quiz.WEB_USER, "password": quiz.WEB_PASSWORD, "failed": False})

    @app.post("/login", response_class=HTMLResponse)
    async def login(request: Request):
        form = await request.form(max_fields=5)
        target = str(form.get("next", "/"))
        if not target.startswith("/") or target.startswith("//"):
            target = "/"  # Only redirect within this site
        valid_user = secrets.compare_digest(str(form.get("user", "")).encode(), quiz.WEB_USER.encode())
        valid_password = secrets.compare_digest(str(form.get("password", "")).encode(), quiz.WEB_PASSWORD.encode())
        if not (valid_user and valid_password):
            return templates.TemplateResponse(request=request, name="login.html", status_code=401, context={
                "next": target, "user": quiz.WEB_USER, "password": quiz.WEB_PASSWORD, "failed": True})
        response = RedirectResponse(target, status_code=303)
        response.set_cookie(SESSION_COOKIE, sign("session:" + quiz.WEB_USER), httponly=True, samesite="lax")
        return response

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, user: str = Depends(authenticate)):
        return templates.TemplateResponse(request=request, name="home.html", context={
            "rows": quiz.list_quizzes(), "repo": quiz.REPO, "user": user, "poller": poller})

    @app.get("/q/{qid}", response_class=HTMLResponse)
    def quiz_page(qid: str, request: Request, _user: str = Depends(authenticate)):
        try:
            row = quiz.get_quiz(qid)
        except KeyError:
            raise HTTPException(404, "Quiz not found")
        content = quiz.Quiz.model_validate_json(row["content"])
        # Only public fields enter the rendered form
        questions = [{"question": q.question, "options": q.options} for q in content.questions]
        state, description = quiz.gate_state(row)
        return templates.TemplateResponse(request=request, name="quiz.html", context={
            "row": row, "title": content.title, "questions": questions, "csrf": csrf(qid),
            "repo": quiz.REPO, "state": state, "description": description, "allow_skip": quiz.ALLOW_SKIP})

    @app.post("/q/{qid}", response_class=HTMLResponse)
    async def grade(qid: str, request: Request, _user: str = Depends(authenticate)):
        form = await request.form(max_fields=10)
        if not secrets.compare_digest(str(form.get("csrf", "")), csrf(qid)):
            raise HTTPException(403, "Invalid form token. Reload the quiz.")
        try:
            row = quiz.get_quiz(qid)
            count = len(quiz.Quiz.model_validate_json(row["content"]).questions)
            try:
                answers = [int(str(form[f"q{i}"])) for i in range(count)]
            except (KeyError, ValueError):
                raise ValueError("Please answer every question.")
            result = await run_in_threadpool(locked_submit, qid, answers)
        except KeyError:
            raise HTTPException(404, "Quiz not found")
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        except (RuntimeError, TimeoutError):
            raise HTTPException(502, "GitHub could not confirm the result. Retry this submission.")
        return templates.TemplateResponse(request=request, name="result.html",
                                          context={"result": result, "row": row, "repo": quiz.REPO})

    @app.post("/q/{qid}/skip", response_class=HTMLResponse)
    async def skip(qid: str, request: Request, _user: str = Depends(authenticate)):
        form = await request.form(max_fields=10)
        if not secrets.compare_digest(str(form.get("csrf", "")), csrf(qid)):
            raise HTTPException(403, "Invalid form token. Reload the quiz.")
        try:
            row = quiz.get_quiz(qid)
            result = await run_in_threadpool(locked_skip, qid)
        except KeyError:
            raise HTTPException(404, "Quiz not found")
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        except (RuntimeError, TimeoutError):
            raise HTTPException(502, "GitHub could not confirm the result. Retry.")
        return templates.TemplateResponse(request=request, name="result.html",
                                          context={"result": result, "row": row, "repo": quiz.REPO})

    return app
