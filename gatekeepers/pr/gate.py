"""
Module: gate.py

Description:
    `QuizGate`: the developer quiz gate. A model reads a pull request's diff and writes a short
    multiple-choice quiz about it; the PR may merge only after its author passes that quiz. The
    PR must also build and pass its tests, its changed C/C++ files must be correctly documented
    (the doxy tool), and a change that only touches comments, formatting or documentation needs
    no quiz.

    Key design points:
      - It gates every project marked "pr_gated" in context/paths.json (settings.Project); each
        quiz records its repository (owner/name), and a PR must target that repository's default
        branch.
      - A quiz belongs to one revision: the PR's head SHA and its base SHA. New commits or a moved
        base need a new quiz, and the old one can no longer be submitted.
      - Answer keys stay in the server's database; they are never rendered or sent to GitHub.
      - The diff is untrusted data: it goes to the model as text and is never executed.
      - Single-user demo: only PRs opened by the developer (QUIZ_DEVELOPER, else the account gh is
        signed in as) are assessed.
      - The gate passes when the build and the documentation are correct and either the change is
        cosmetic or the quiz was passed. A change counts as cosmetic only when the model says so
        and the server's own comparison of the code (ChangeInspector) agrees.
"""

import logging
import secrets
from pathlib import Path
from typing import Any, Optional

from gatekeepers.pr.changes import ChangeInspector
from gatekeepers.pr.generator import QuizGenerator
from gatekeepers.pr.github import GitHub
from gatekeepers.pr.quiz import Quiz
from gatekeepers.pr.settings import GateSettings, Project
from gatekeepers.pr.store import QuizStore


class QuizGate:
    """
    Assesses pull request revisions and posts the outcome to GitHub.
    """

    COMMENT_MARKER = "<!-- pr_gate -->"  # Finds the gate's own comment on a pull request
    NOT_FOUND = "(not found in this repository)"  # The title of an assessment whose pull request is gone
    MAX_DIFF_CHARS = 60_000

    def __init__(self, settings: GateSettings, store: Optional[QuizStore] = None,
                 generator: Optional[QuizGenerator] = None) -> None:
        """
        Args:
            settings: The gate's settings, with the gated projects.
            store: The quiz database; None uses the settings' data folder.
            generator: The quiz writer; None uses the settings' model profile.
        """
        self.settings = settings
        self.store: QuizStore = store or QuizStore(settings.data_dir)
        self.generator: QuizGenerator = generator or QuizGenerator(settings.profile)
        self.projects: dict[str, Project] = {p.repo: p for p in settings.projects}  # By owner/name
        self._github: dict[str, GitHub] = {}
        self._developer = settings.developer

    def github(self, repo: str) -> GitHub:
        """
        A gated repository on GitHub.
        Args:
            repo: owner/name.
        Returns:
            GitHub: Its client, made once.
        """
        if repo not in self._github:
            self._github[repo] = GitHub(repo)
        return self._github[repo]

    @property
    def developer(self) -> str:
        """The only author whose PRs are assessed: QUIZ_DEVELOPER, else the account gh is signed in as."""
        if not self._developer:
            self._developer = GitHub.login()
        return self._developer

    def project(self, name: Optional[str] = None) -> Project:
        """
        Find a gated project.
        Args:
            name: Its folder name in context/paths.json (core_dump) or its repository (owner/name);
                None when only one project is gated.
        Returns:
            Project: The project.
        Raises:
            ValueError: If no such project is gated, or name is None and not exactly one is.
        """
        projects = list(self.projects.values())
        if name is None:
            if len(projects) != 1:
                names = ", ".join(p.name for p in projects) or "none (set pr_gated in context/paths.json)"
                raise ValueError(f"Name the project; the gated projects are: {names}.")
            return projects[0]
        for project in projects:
            if name in (project.name, project.repo):
                return project
        raise ValueError(f"'{name}' is not a gated project; the gated projects are: "
                         f"{', '.join(p.name for p in projects) or 'none'}.")

    def init_store(self) -> str:
        """
        Create the database if missing (QuizStore.init). Quizzes from when the gate served one
        repository are given to the gated project, when there is exactly one.
        Returns:
            str: The secret key.
        """
        secret = self.store.init()
        if len(self.projects) == 1:
            self.store.adopt(next(iter(self.projects)))
        return secret

    def access_problems(self) -> list[str]:
        """
        Check that gh's sign-in can gate every project; a project it cannot gate is dropped, so the
        gate never claims a repository it cannot post to. Gated folders that could not be read
        (settings.skipped) are reported too.
        Returns:
            list[str]: "<project>: error: ..." for each dropped project, "<project>: warning: ..."
                for each that merges need not wait on, and each skipped folder's reason.
        """
        problems = [f"{line} (skipped)" for line in self.settings.skipped]
        for repo, project in list(self.projects.items()):
            for problem in self.github(repo).access_problems():
                problems.append(f"{project.name}: {problem}")
                if problem.startswith("error:"):
                    del self.projects[repo]
        return problems

    @classmethod
    def load(cls) -> "QuizGate":
        """
        The gate as settings.json and the environment configure it.
        Returns:
            QuizGate: The gate.
        Raises:
            ValueError: If a required setting is missing.
        """
        return cls(GateSettings.load())

    def quiz_url(self, qid: str) -> str:
        """The quiz page's address."""
        return f"{self.settings.base_url}/q/{qid}"

    def find_quiz(self, repo: str, number: int, sha: str, base_sha: str) -> Optional[dict[str, Any]]:
        """
        Find the latest quiz for one revision of the developer's PR.
        Args:
            repo: The repository, owner/name.
            number: The PR number.
            sha: The head SHA.
            base_sha: The base SHA.
        Returns:
            Optional[dict]: The quiz row, or None if that revision has no quiz yet.
        """
        return self.store.find(repo, number, sha, base_sha, self.developer)

    def check_pr(self, repo: str, info: dict[str, Any], sha: Optional[str] = None,
                 base_sha: Optional[str] = None) -> None:
        """
        Check that a PR may be assessed and, optionally, that it is still at a given revision.
        Args:
            repo: The repository, owner/name; it must be gated.
            info: The PR, as returned by `GitHub.pr_info`.
            sha: The head SHA the PR must still have.
            base_sha: The base SHA the PR must still have.
        Raises:
            ValueError: If the repository is not gated, the PR is closed, targets another branch than
                the default, belongs to another developer, or has moved past the given revision.
        """
        self.project(repo)
        base = self.github(repo).default_branch()
        if info["state"] != "open" or info["base"]["ref"] != base:
            raise ValueError(f"The PR must be open and target {base}.")
        if info["user"]["login"] != self.developer:
            raise ValueError("This single-user demo only assesses the configured developer's PRs.")
        if sha and info["head"]["sha"] != sha:
            raise ValueError("The PR has new commits. Generate a new quiz for its latest revision.")
        if base_sha and info["base"]["sha"] != base_sha:
            raise ValueError("The base branch changed. Update the PR and generate a new quiz.")

    @staticmethod
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

    def comment_body(self, row: dict[str, Any], failed_attempt: bool = False) -> str:
        """
        The gate's pull request comment for a revision: build and tests, documentation, and the quiz.
        Args:
            row: The quiz row.
            failed_attempt: The developer just failed the quiz.
        Returns:
            str: Markdown, starting with COMMENT_MARKER.
        """
        url = self.quiz_url(row["id"])

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
        return (f"{self.COMMENT_MARKER}\n**Pull Request Gate** · revision `{row['sha'][:7]}`\n\n"
                f"| Check | Result |\n| --- | --- |\n| Build and tests | {build} |\n"
                f"| Documentation | {docs} |\n| Quiz | {quiz} |\n")

    def publish_comment(self, row: dict[str, Any], failed_attempt: bool = False) -> None:
        """
        Post the gate's comment on the pull request, or update it: one comment per pull request, kept
        current with the latest revision. A failure is logged, never raised: the status check is what
        gates the merge.
        Args:
            row: The quiz row.
            failed_attempt: The developer just failed the quiz.
        """
        if not self.settings.pr_comment:
            return
        body = self.comment_body(row, failed_attempt)
        github = self.github(row["repo"])
        try:
            mine = [c for c in github.comments(row["pr"]) if (c.get("body") or "").startswith(self.COMMENT_MARKER)]
            if mine and mine[-1]["body"] == body:
                return
            if mine:
                github.edit_comment(mine[-1]["id"], body)
            else:
                github.add_comment(row["pr"], body)
        except (RuntimeError, ValueError, KeyError, TypeError) as exc:
            logging.getLogger("pr_gate").warning("PR #%s: could not post the gate's comment: %s", row["pr"], exc)

    def publish(self, row: dict[str, Any], failed_attempt: bool = False) -> None:
        """
        Post a revision's status (see `gate_state`) and comment, and record what was posted.
        Args:
            row: The quiz row.
            failed_attempt: The developer just failed the quiz.
        """
        state, description = self.gate_state(row, failed_attempt)
        self.publish_comment(row, failed_attempt)
        self.github(row["repo"]).publish_status(row["sha"], state, description, self.quiz_url(row["id"]))
        self.store.set_published(row["id"], state)

    def history(self, repo: Optional[str] = None, pr: Optional[int] = None) -> list[dict[str, Any]]:
        """
        Every assessment, newest first, with its attempts and outcome, for the history page. Titles
        missing from assessments made before they were stored are fetched from GitHub once and saved,
        but only when the pull request contains the assessed commit: a pull request number can belong
        to a different request (a repository that was recreated), which is marked NOT_FOUND instead.
        Args:
            repo: Only this repository (owner/name); None for all.
            pr: Only this pull request; None for all.
        Returns:
            list[dict]: Each assessment's row (without content or answer keys), plus attempts (count),
                best and total (best score), last_attempt (time) and state / outcome (see gate_state).
        """
        rows = self.store.assessments(repo, pr)
        for owner_name, number in sorted({(row["repo"], row["pr"]) for row in rows if not row["pr_title"]}):
            if not owner_name:
                continue  # A quiz from when the gate served one repository, not yet adopted
            try:
                title = self.github(owner_name).pr_info(number).get("title", "")
                commits = self.github(owner_name).pr_commits(number)
            except (RuntimeError, ValueError, KeyError, TypeError):
                continue  # GitHub unreachable: show the number only, and try again next time
            for row in rows:
                if (row["repo"], row["pr"]) == (owner_name, number) and not row["pr_title"]:
                    row["pr_title"] = title if row["sha"] in commits else self.NOT_FOUND
                    self.store.set_title(row["id"], row["pr_title"])
        for row in rows:
            row["state"], row["outcome"] = self.gate_state(row)
        return rows

    def create(self, repo: str, number: int, profile: Optional[str] = None,
               fixed: Optional[str] = None) -> dict[str, Any]:
        """
        Make sure the PR's current revision is assessed, and post its status: check the changed files'
        documentation and whether the change is cosmetic, then have the model write the quiz.
        An existing assessment of the same revision is reused (re-posting its status), so a pass is
        never thrown away.
        Args:
            repo: The repository, owner/name; it must be gated.
            number: The PR number.
            profile: The model profile; None uses QUIZ_MODEL_PROFILE, then the models file's default.
            fixed: A JSON quiz file to use instead of the model (for demos and tests).
        Returns:
            dict: The quiz row.
        Raises:
            ValueError: If the repository is not gated, the PR may not be assessed, its diff is empty or too
                large, it moved
                during generation, the model did not return a valid quiz, or a fixed quiz calls a
                code change cosmetic.
            RuntimeError: If a GitHub request fails.
        """
        settings, project, github = self.settings, self.project(repo), self.github(repo)
        self.init_store()
        before = github.pr_info(number)
        self.check_pr(repo, before)
        head, base = before["head"]["sha"], before["base"]["sha"]
        existing = self.find_quiz(repo, number, head, base)
        if existing:
            self.publish(existing)
            return existing

        diff = github.pr_diff(number)
        if not diff.strip() or len(diff) > self.MAX_DIFF_CHARS:
            raise ValueError(f"Diff must be nonempty and at most {self.MAX_DIFF_CHARS:,} characters.")
        github.publish_status(head, "pending", "Checking documentation and preparing the developer quiz",
                              settings.base_url + "/")
        inspector = ChangeInspector(github.run, repo, project.build_command, project.test_target,
                                    project.fail_on_warnings)
        inspection = inspector.inspect(number, head, base)
        if fixed:
            quiz, source = Quiz.model_validate_json(Path(fixed).read_text()), "fixed fixture"
            if quiz.cosmetic and inspection["code_files"]:
                raise ValueError("The fixed quiz calls the change cosmetic, but its code changed.")
        else:
            quiz, source = self.generator.generate(diff, profile, inspection["code_files"],
                                                   QuizGenerator.context(before, inspection.get("build_report", "")))

        # Generation takes a while: make sure the quiz still matches the PR
        after = github.pr_info(number)
        self.check_pr(repo, after, head, base)

        # Shuffle choices so the model's preferred answer position is not a hint
        for question in quiz.questions:
            pairs = list(enumerate(question.options))
            secrets.SystemRandom().shuffle(pairs)
            question.correct = next(i for i, (old, _) in enumerate(pairs) if old == question.correct)
            question.options = [text for _, text in pairs]

        row = self.store.add({
            "id": secrets.token_urlsafe(16), "repo": repo, "pr": number, "sha": head, "base_sha": base,
            "developer": self.developer, "content": quiz.model_dump_json(), "source": source,
            "build_ok": int(inspection.get("build_ok", True)), "build_report": inspection.get("build_report", ""),
            "docs_ok": int(inspection["docs_ok"]), "docs_report": inspection["docs_report"],
            "cosmetic": int(quiz.cosmetic), "pr_title": after.get("title", "")})
        self.publish(row)
        return row

    @staticmethod
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

    def skip(self, qid: str) -> dict[str, Any]:
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
        if not self.settings.allow_skip:
            raise ValueError("Skipping the quiz is turned off (QUIZ_ALLOW_SKIP).")
        row = self.store.get(qid)
        self.takes_quiz(row)
        self.check_pr(row["repo"], self.github(row["repo"]).pr_info(row["pr"]), row["sha"], row["base_sha"])
        self.store.mark_skipped(qid)
        row = self.store.get(qid)
        self.publish(row)
        quiz = Quiz.model_validate_json(row["content"])
        return {"skipped": bool(row["skipped"]), "passed": True, "score": 0, "total": len(quiz.questions),
                "questions": quiz.questions}

    def submit(self, qid: str, answers: list[int]) -> dict[str, Any]:
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
                answers are malformed, the PR moved past the quiz's revision, or its repository is no
                longer gated.
            RuntimeError: If a GitHub request fails (the attempt is already saved; resubmit).
        """
        row = self.store.get(qid)
        self.takes_quiz(row)
        github = self.github(row["repo"])
        self.check_pr(row["repo"], github.pr_info(row["pr"]), row["sha"], row["base_sha"])
        quiz = Quiz.model_validate_json(row["content"])
        if len(answers) != len(quiz.questions) or any(type(a) is not int or a not in range(4) for a in answers):
            raise ValueError("Answer every question with one of its four choices.")
        score = sum(a == q.correct for a, q in zip(answers, quiz.questions, strict=True))
        passed = score == len(quiz.questions)

        # Persist before publishing, so a GitHub failure can be retried
        self.store.add_attempt(qid, row["developer"], score, len(quiz.questions), passed)
        row = self.store.get(qid)

        # Recheck after grading, before writing success to the exact tested SHA
        self.check_pr(row["repo"], github.pr_info(row["pr"]), row["sha"], row["base_sha"])
        self.publish(row, failed_attempt=not row["passed"])
        return {"score": score, "total": len(quiz.questions), "passed": bool(row["passed"]),
                "questions": quiz.questions if row["passed"] else []}
