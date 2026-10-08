"""
Offline tests for the pr_gate tool: temporary storage, and mocked GitHub and model calls, so no
real PR is ever marked. Run from the repository root:
    .venv/bin/python -m unittest discover -s gatekeepers/pr/tests
"""

import copy
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from gatekeepers.pr import SETTINGS_FILE
from gatekeepers.pr.changes import ChangeInspector
from gatekeepers.pr.clone import LocalClone
from gatekeepers.pr.gate import QuizGate
from gatekeepers.pr.generator import QuizGenerator
from gatekeepers.pr.github import GitHub
from gatekeepers.pr.pr_gate import PrGateCli
from gatekeepers.pr.quiz import Quiz
from gatekeepers.pr.server import GateApp, Poller
from gatekeepers.pr.settings import GateSettings

# Fixed settings, so the tests depend on neither settings.json nor the environment; each test
# gives the gate its own temporary data folder
SETTINGS = GateSettings(repo="owner/name", developer="dev", base_url="http://gate.test", pr_comment=False,
                        allow_skip=False, data_dir=Path("/nonexistent"))

FIXTURE: dict[str, Any] = {"title": "C math quiz", "questions": [
    {"question": f"What does the changed code do in case {i}?",
     "options": ["Answer A", "Answer B", "Answer C", "Answer D"],
     "correct": i, "explanation": "Private explanation sentinel " + str(i)}
    for i in range(3)]}
INFO: dict[str, Any] = {"number": 1, "state": "open", "base": {"ref": "main", "sha": "b" * 40},
        "head": {"sha": "a" * 40}, "user": {"login": SETTINGS.developer}, "title": "Compute pi"}
CODE_CHANGE = {"build_ok": True, "build_report": "$ make && make check: succeeded", "docs_ok": True, "docs_report": "All 1 changed C/C++ file(s) are documented.",
               "cosmetic": False, "code_files": ["src/pi.c"]}
MODELS = {"default": "local", "profiles": {
    "local": {"name": "Local", "base_url": "http://boba:1234/v1", "model": "qwen", "api_key": "lm-studio"},
    "openai": {"name": "OpenAI", "base_url": "https://api.openai.com/v1", "model": "gpt",
               "api_key_env": "TEST_OPENAI_KEY"}}}


def reply(content, finish_reason="stop"):
    """A chat completions response body with one choice."""
    return {"choices": [{"finish_reason": finish_reason, "message": {"content": content}}]}


# noinspection SqlNoDataSourceInspection
class QuizTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.gate = QuizGate(replace(SETTINGS, data_dir=Path(self.temp.name)))
        self.info = patch.object(self.gate.github, "pr_info", return_value=copy.deepcopy(INFO))
        self.mock_info = self.info.start()
        self.gh = patch.object(self.gate.github, "run", return_value="diff --git a/pi.c b/pi.c")
        self.mock_gh = self.gh.start()
        self.model = patch.object(self.gate.generator, "generate", return_value=(Quiz.model_validate(FIXTURE), "test model"))
        self.mock_generate = self.model.start()
        self.inspect = patch.object(ChangeInspector, "inspect", return_value=dict(CODE_CHANGE))
        self.mock_inspect = self.inspect.start()
        self.row = self.gate.create(1)
        self.qid = self.row["id"]
        self.content = Quiz.model_validate_json(self.gate.store.get(self.qid)["content"])
        self.answers = [q.correct for q in self.content.questions]
        self.app = GateApp(self.gate).app
        self.client = TestClient(self.app)
        self.client.post("/login", data={"user": "user", "password": "pass"})

    def tearDown(self):
        self.client.close()
        self.inspect.stop()
        self.model.stop()
        self.gh.stop()
        self.info.stop()
        self.temp.cleanup()

    def enabled(self, **changes):
        """Patch the gate's settings, e.g. enabled(allow_skip=True)."""
        return patch.object(self.gate, "settings", replace(self.gate.settings, **changes))

    def wrong(self):
        return [(x + 1) % 4 for x in self.answers]

    def test_generation_publishes_pending_with_quiz_link_to_exact_sha(self):
        call = self.mock_gh.call_args
        self.assertEqual(call.kwargs["payload"]["state"], "pending")
        self.assertEqual(call.kwargs["payload"]["context"], "developer-quiz")
        self.assertEqual(call.kwargs["payload"]["target_url"], f"{SETTINGS.base_url}/q/{self.qid}")
        self.assertIn(INFO["head"]["sha"], call.args[1])
        self.assertEqual(self.row["source"], "test model")

    def test_answers_not_rendered_and_auth_required(self):
        page = self.client.get("/q/" + self.qid)
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("Private explanation sentinel", page.text)
        self.assertNotIn('"correct"', page.text)
        self.assertIn('name="q0"', page.text)
        self.assertEqual(TestClient(self.app).get("/health").status_code, 200)

    def test_sign_in_page_shows_demo_credentials_and_guards_pages(self):
        anonymous = TestClient(self.app)
        redirect = anonymous.get("/q/" + self.qid, follow_redirects=False)
        self.assertEqual(redirect.status_code, 303)
        self.assertEqual(redirect.headers["location"], "/login?next=/q/" + self.qid)
        page = anonymous.get("/login")
        self.assertIn("<code>user</code>", page.text)
        self.assertIn("<code>pass</code>", page.text)
        self.assertEqual(anonymous.post("/login", data={"user": "user", "password": "bad"}).status_code, 401)
        offsite = anonymous.post("/login", data={"user": "user", "password": "pass", "next": "//evil.example"},
                                 follow_redirects=False)
        self.assertEqual(offsite.headers["location"], "/")
        self.assertEqual(anonymous.get("/").status_code, 200)

    def test_pass_is_persisted_and_published(self):
        self.assertTrue(self.gate.submit(self.qid, self.answers)["passed"])
        self.assertEqual(self.gate.store.get(self.qid)["published"], "success")
        with self.gate.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)

    def test_failure_allows_retry_and_hides_explanations(self):
        result = self.gate.submit(self.qid, self.wrong())
        self.assertFalse(result["passed"])
        self.assertEqual(result["questions"], [])
        self.assertEqual(self.gate.store.get(self.qid)["published"], "failure")
        self.assertTrue(self.gate.submit(self.qid, self.answers)["passed"])

    def test_pass_cannot_be_downgraded(self):
        self.gate.submit(self.qid, self.answers)
        self.gate.submit(self.qid, self.wrong())
        self.assertEqual(self.gate.store.get(self.qid)["published"], "success")

    def test_changed_head_or_base_rejected_without_publishing(self):
        for field in ("head", "base"):
            info = copy.deepcopy(INFO)
            info[field]["sha"] = "c" * 40
            self.mock_info.return_value = info
            self.mock_gh.reset_mock()
            with self.assertRaises(ValueError):
                self.gate.submit(self.qid, self.answers)
            self.mock_gh.assert_not_called()

    def test_closed_pr_or_wrong_developer_rejected(self):
        for info in [dict(INFO, state="closed"), dict(INFO, user={"login": "someone-else"})]:
            self.mock_info.return_value = info
            with self.assertRaises(ValueError):
                self.gate.submit(self.qid, self.answers)

    def test_publication_failure_can_be_retried(self):
        self.mock_gh.side_effect = RuntimeError("network unavailable")
        with self.assertRaises(RuntimeError):
            self.gate.submit(self.qid, self.answers)
        self.assertTrue(self.gate.store.get(self.qid)["passed"])
        self.assertEqual(self.gate.store.get(self.qid)["published"], "pending")
        self.mock_gh.side_effect = None
        self.gate.submit(self.qid, self.answers)
        self.assertEqual(self.gate.store.get(self.qid)["published"], "success")

    def test_invalid_answers_rejected(self):
        invalid_answers: list[Any] = [[], [4, 0, 0], [True, 0, 0], ["0", 0, 0]]
        for answers in invalid_answers:
            with self.assertRaises(ValueError):
                self.gate.submit(self.qid, answers)

    def test_browser_form_csrf_and_grading(self):
        url = "/q/" + self.qid
        self.assertEqual(self.client.post(url, data={"q0": "0"}).status_code, 403)
        match = re.search(r'name="csrf" value="([0-9a-f]+)"', self.client.get(url).text)
        self.assertIsNotNone(match)
        assert match is not None
        token = match.group(1)
        data = {"csrf": token, **{f"q{i}": str(a) for i, a in enumerate(self.answers)}}
        response = self.client.post(url, data=data)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Ready to merge.", response.text)

    def test_schema_rejects_invalid_answer_and_duplicate_options(self):
        for mutation in ("index", "duplicates"):
            fixture = copy.deepcopy(FIXTURE)
            if mutation == "index":
                fixture["questions"][0]["correct"] = 7
            else:
                fixture["questions"][0]["options"] = ["same"] * 4
            with self.assertRaises(ValueError):
                Quiz.model_validate(fixture)

    def test_same_revision_reuses_quiz(self):
        self.assertEqual(self.gate.create(1)["id"], self.qid)

    def test_new_revision_gets_new_quiz(self):
        info = copy.deepcopy(INFO)
        info["head"]["sha"] = "d" * 40
        self.mock_info.return_value = info
        row = self.gate.create(1)
        self.assertNotEqual(row["id"], self.qid)
        self.assertEqual(row["sha"], "d" * 40)

    def test_mid_generation_change_rejected(self):
        self.mock_info.side_effect = [dict(INFO, head={"sha": "e" * 40}), dict(INFO, head={"sha": "f" * 40})]
        with self.assertRaises(ValueError):
            self.gate.create(1)

    def test_html_escapes_model_text(self):
        fixture = copy.deepcopy(FIXTURE)
        fixture["title"] = "<script>alert(1)</script>"
        with self.gate.store.connect() as db:
            db.execute("UPDATE quizzes SET content=? WHERE id=?", (json.dumps(fixture), self.qid))
        page = self.client.get("/q/" + self.qid)
        self.assertNotIn("<script>", page.text)
        self.assertIn("&lt;script&gt;", page.text)

    def new_revision(self, sha, inspection, content=None):
        """Assess a new head commit with the given inspection result (and model reply)."""
        info = copy.deepcopy(INFO)
        info["head"]["sha"] = sha * 40
        self.mock_info.return_value = info
        self.mock_inspect.return_value = inspection
        if content is not None:
            self.mock_generate.return_value = (Quiz.model_validate(content), "test model")
        return self.gate.create(1)

    def test_documentation_problems_fail_the_check_and_show_on_the_page(self):
        report = "src/pi.c:6: error: Member print_pi() (function) of file pi.c is not documented."
        row = self.new_revision("d", dict(CODE_CHANGE, docs_ok=False, docs_report=report))
        self.assertEqual(self.mock_gh.call_args.kwargs["payload"]["state"], "failure")
        self.assertIn("Documentation problems", self.mock_gh.call_args.kwargs["payload"]["description"])
        page = self.client.get("/q/" + row["id"]).text
        self.assertIn("print_pi() (function) of file pi.c is not documented", page)
        self.assertNotIn('name="q0"', page)  # No quiz until the documentation is fixed
        with self.assertRaisesRegex(ValueError, "documentation"):
            self.gate.submit(row["id"], [q.correct for q in Quiz.model_validate_json(row["content"]).questions])

    def test_pull_request_gets_one_comment_with_the_quiz_link_kept_current(self):
        comments, calls = [], []

        def fake_gh(*args, payload=None):
            calls.append((args, payload))
            if args[1].endswith("/comments?per_page=100"):
                return json.dumps(comments)
            if "--method" in args and args[args.index("--method") + 1] == "POST" and args[1].endswith("/comments"):
                comments.append({"id": 41, "body": payload["body"]})
            elif "--method" in args and args[args.index("--method") + 1] == "PATCH":
                comments[0]["body"] = payload["body"]
            return "{}"

        with self.enabled(pr_comment=True), patch.object(self.gate.github, "run", side_effect=fake_gh):
            row = self.gate.store.get(self.qid)
            self.gate.publish(row)
            self.assertEqual(len(comments), 1)
            self.assertIn(f"[Take the quiz]({SETTINGS.base_url}/q/{self.qid})", comments[0]["body"])
            self.assertIn("| Build and tests | ✅ make && make check: succeeded |", comments[0]["body"])
            self.gate.publish(row)  # Nothing changed: no new comment, no edit
            self.assertEqual(sum(1 for args, _ in calls if "PATCH" in args or "POST" in args and "comments" in args[1]), 1)
            self.gate.submit(self.qid, self.answers)
            self.assertEqual(len(comments), 1)  # Edited in place
            self.assertIn("✅ [Passed]", comments[0]["body"])
            self.assertTrue(comments[0]["body"].startswith(QuizGate.COMMENT_MARKER))

    def test_skip_button_unlocks_the_merge_and_is_recorded_as_skipped(self):
        url = "/q/" + self.qid
        with self.enabled(allow_skip=True):
            page = self.client.get(url).text
            self.assertIn('formaction="/q/%s/skip"' % self.qid, page)
            match = re.search(r'name="csrf" value="([0-9a-f]+)"', page)
            self.assertIsNotNone(match)
            assert match is not None
            token = match.group(1)
            self.assertEqual(self.client.post(url + "/skip", data={}).status_code, 403)  # Needs the form token
            response = self.client.post(url + "/skip", data={"csrf": token})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Quiz skipped", response.text)
        payload = self.mock_gh.call_args.kwargs["payload"]
        self.assertEqual((payload["state"], payload["description"]), ("success", "Quiz skipped (proof-of-concept mode)"))
        row = self.gate.store.get(self.qid)
        self.assertEqual((row["passed"], row["skipped"]), (1, 1))

    def test_skip_is_refused_when_turned_off_or_before_the_build_passes(self):
        with self.enabled(allow_skip=False):
            self.assertNotIn("/skip", self.client.get("/q/" + self.qid).text)
            with self.assertRaisesRegex(ValueError, "turned off"):
                self.gate.skip(self.qid)
        row = self.new_revision("b", dict(CODE_CHANGE, build_ok=False, build_report="$ make: FAILED"))
        with self.enabled(allow_skip=True), self.assertRaisesRegex(ValueError, "build"):
            self.gate.skip(row["id"])

    def test_history_lists_every_assessment_with_its_attempts_and_outcome(self):
        self.gate.submit(self.qid, self.wrong())
        self.gate.submit(self.qid, self.answers)
        self.new_revision("d", dict(CODE_CHANGE, docs_ok=False, docs_report="x.c:1: error: undocumented"))
        with self.gate.store.connect() as db:  # An assessment from before titles were stored
            db.execute("UPDATE quizzes SET pr_title=''")
        commits = json.dumps([{"sha": "a" * 40}])  # PR #1's commits: only the first revision's
        with patch.object(self.gate.github, "run", return_value=commits):
            rows = self.gate.history()
        self.assertEqual([row["outcome"] for row in rows],
                         ["Documentation problems in the changed files; see Details",
                          "Developer passed the revision-specific quiz"])
        self.assertEqual((rows[1]["attempts"], rows[1]["best"], rows[1]["total"]), (2, 3, 3))
        # Fetched once and saved, only where the pull request contains the assessed commit
        self.assertEqual([row["pr_title"] for row in rows], [QuizGate.NOT_FOUND, INFO["title"]])
        page = self.client.get("/history").text
        self.assertIn("Compute pi", page)
        self.assertIn("2 (best 3/3)", page)
        self.assertIn(f'href="/q/{self.qid}"', page)
        self.assertEqual(len(self.gate.history(pr=999)), 0)
        self.assertIn("Show all pull requests", self.client.get("/history?pr=1").text)
        self.assertEqual(self.client.get("/history", follow_redirects=False).status_code, 200)
        text = PrGateCli(self.gate).history(pr=1)  # The agents' pr_gate tool, action history
        self.assertIn("PR #1 Compute pi", text)
        self.assertIn("(2 attempt(s), best 3/3)", text)
        self.assertTrue(text.endswith(f"{SETTINGS.base_url}/history?pr=1"))
        self.assertEqual(TestClient(self.app).get("/history", follow_redirects=False).status_code, 303)  # Sign-in

    def test_failed_build_or_tests_fail_the_check_and_show_on_the_page(self):
        report = "$ make && make check: FAILED\ncore_dump: invalid option or unexpected option argument"
        row = self.new_revision("c", dict(CODE_CHANGE, build_ok=False, build_report=report))
        payload = self.mock_gh.call_args.kwargs["payload"]
        self.assertEqual((payload["state"], payload["description"]), ("failure", "Build or tests failed, or compiler warnings; see Details"))
        page = self.client.get("/q/" + row["id"]).text
        self.assertIn("invalid option or unexpected option argument", page)
        self.assertNotIn('name="q0"', page)
        with self.assertRaisesRegex(ValueError, "build"):
            self.gate.submit(row["id"], [q.correct for q in Quiz.model_validate_json(row["content"]).questions])

    def test_cosmetic_change_passes_without_a_quiz(self):
        cosmetic = {"title": "Comment updates", "cosmetic": True, "questions": []}
        row = self.new_revision("e", dict(CODE_CHANGE, cosmetic=True, code_files=[]), cosmetic)
        payload = self.mock_gh.call_args.kwargs["payload"]
        self.assertEqual((payload["state"], payload["description"]),
                         ("success", "Cosmetic change: no quiz needed; builds, documentation OK"))
        page = self.client.get("/q/" + row["id"]).text
        self.assertIn("No quiz needed", page)
        self.assertNotIn('name="q0"', page)

    def test_model_judges_a_code_free_change_worth_a_quiz(self):
        row = self.new_revision("f", dict(CODE_CHANGE, cosmetic=True, code_files=[]), FIXTURE)
        self.assertEqual(row["cosmetic"], 0)
        self.assertEqual(self.mock_gh.call_args.kwargs["payload"]["state"], "pending")

    def test_fixed_quiz_cannot_call_a_code_change_cosmetic(self):
        fixture = Path(self.temp.name) / "cosmetic.json"
        fixture.write_text(json.dumps({"title": "Comment updates", "cosmetic": True, "questions": []}))
        info = copy.deepcopy(INFO)
        info["head"]["sha"] = "9" * 40
        self.mock_info.return_value = info
        with self.assertRaisesRegex(ValueError, "cosmetic"):
            self.gate.create(1, fixed=str(fixture))

    def test_quiz_kind_and_question_count_must_match(self):
        with self.assertRaises(ValueError):
            Quiz.model_validate({"title": "Comment updates", "cosmetic": True, "questions": FIXTURE["questions"]})
        with self.assertRaises(ValueError):
            Quiz.model_validate({"title": "Code change", "questions": []})


class ModelTests(unittest.TestCase):

    def setUp(self):
        self.models = Path(tempfile.mkdtemp()) / "models.json"
        self.models.write_text(json.dumps(MODELS))

    def test_settings_come_from_the_manifest_and_the_environment_overrides_them(self):
        manifest = json.loads(SETTINGS_FILE.read_text())["settings"]
        self.assertEqual(GateSettings.setting("QUIZ_DEVELOPER"), os.environ.get("QUIZ_DEVELOPER") or manifest["QUIZ_DEVELOPER"])
        with patch.dict(os.environ, {"QUIZ_REPO": "someone/else"}):
            self.assertEqual(GateSettings.setting("QUIZ_REPO"), "someone/else")
        with self.assertRaisesRegex(ValueError, "QUIZ_NOT_SET"):
            GateSettings.setting("QUIZ_NOT_SET")
        self.assertEqual(GateSettings.setting("QUIZ_NOT_SET", required=False), "")

    def test_profiles_come_from_the_shared_models_file(self):
        self.assertEqual(QuizGenerator(models_file=self.models).resolve_model(None)["base_url"], "http://boba:1234/v1")
        with patch.dict(os.environ, {"TEST_OPENAI_KEY": "sk-test"}):
            settings = QuizGenerator(models_file=self.models).resolve_model("openai")
        self.assertEqual((settings["model"], settings["api_key"]), ("gpt", "sk-test"))

    def test_model_attempts_come_from_settings_and_environment(self):
        manifest = json.loads(SETTINGS_FILE.read_text())["settings"]
        with patch.dict(os.environ, {"QUIZ_MODEL_ATTEMPTS": ""}):
            self.assertEqual(QuizGenerator().attempts, int(manifest["QUIZ_MODEL_ATTEMPTS"]))
        settings = {"name": "Local", "base_url": "http://x/v1", "model": "m", "api_key": "k", "timeout": 5}
        for attempts in (1, 3):
            with self.subTest(attempts=attempts), patch.dict(os.environ, {"QUIZ_MODEL_ATTEMPTS": str(attempts)}), \
                    patch.object(QuizGenerator, "resolve_model", return_value=settings), \
                    patch("gatekeepers.pr.generator.httpx.post") as post:
                post.return_value.json.return_value = reply("not JSON")
                with self.assertRaisesRegex(ValueError, "No quiz was created"):
                    QuizGenerator().generate("a small diff")
                self.assertEqual(post.call_count, attempts)

    def test_invalid_model_attempt_counts_are_rejected(self):
        for value in ("0", "-1", "not a number", "1.5"):
            with self.subTest(value=value), patch.dict(os.environ, {"QUIZ_MODEL_ATTEMPTS": value}), \
                    self.assertRaises(ValueError):
                QuizGenerator()

    def test_missing_key_and_unknown_profile(self):
        with patch.dict(os.environ, {"TEST_OPENAI_KEY": ""}), self.assertRaises(ValueError):
            QuizGenerator(models_file=self.models).resolve_model("openai")
        with self.assertRaises(ValueError):
            QuizGenerator(models_file=self.models).resolve_model("nope")

    def test_fenced_json_is_accepted_and_invalid_reply_retried(self):
        settings = {"name": "Local", "base_url": "http://x/v1", "model": "m", "api_key": "k", "timeout": 5}
        with patch.object(QuizGenerator, "resolve_model", return_value=settings), patch("gatekeepers.pr.generator.httpx.post") as post:
            post.return_value.json.side_effect = [reply("not JSON"), reply("```json\n" + json.dumps(FIXTURE) + "\n```")]
            content, source = QuizGenerator().generate("a small diff", "local")
            self.assertEqual(len(content.questions), 3)
            self.assertEqual(source, "Local / m")
            self.assertEqual(post.call_count, 2)
            self.assertEqual(post.call_args.args[0], "http://x/v1/chat/completions")
            self.assertEqual(post.call_args.kwargs["json"]["messages"][0]["content"], QuizGenerator().instructions())
            self.assertIn("untrusted data", QuizGenerator().instructions())
            post.return_value.json.side_effect = [reply("not JSON"), reply(json.dumps(FIXTURE), "length")]
            with self.assertRaises(ValueError):
                QuizGenerator().generate("a small diff", "local")

    def test_the_pull_request_and_test_output_reach_the_quiz_writer(self):
        settings = {"name": "Local", "base_url": "http://x/v1", "model": "m", "api_key": "k", "timeout": 5}
        context = QuizGenerator.context({"title": "Add -w", "body": "Works with -dpw."}, "$ make && make check: succeeded\n./core_dump -d")
        with patch.object(QuizGenerator, "resolve_model", return_value=settings), patch("gatekeepers.pr.generator.httpx.post") as post:
            post.return_value.json.return_value = reply(json.dumps(FIXTURE))
            QuizGenerator().generate("a diff", "local", ["src/main.c"], context)
        prompt = post.call_args.kwargs["json"]["messages"][1]["content"]
        for part in ("Pull request title: Add -w", "untrusted, may be wrong):\nWorks with -dpw.",
                     "./core_dump -d", "Code diff:\na diff"):
            self.assertIn(part, prompt)
        self.assertLess(prompt.index("Pull request title"), prompt.index("Code diff"))

    def test_a_code_change_called_cosmetic_is_rejected(self):
        settings = {"name": "Local", "base_url": "http://x/v1", "model": "m", "api_key": "k", "timeout": 5}
        cosmetic = json.dumps({"title": "Comment updates", "cosmetic": True, "questions": []})
        with patch.object(QuizGenerator, "resolve_model", return_value=settings), patch("gatekeepers.pr.generator.httpx.post") as post:
            post.return_value.json.side_effect = [reply(cosmetic), reply(cosmetic)]
            with self.assertRaisesRegex(ValueError, "cosmetic"):
                QuizGenerator().generate("a small diff", "local", ["src/pi.c"])
            self.assertIn("Server analysis: code changed in: src/pi.c",
                          post.call_args.kwargs["json"]["messages"][1]["content"])
            post.return_value.json.side_effect = [reply(cosmetic)]
            self.assertTrue(QuizGenerator().generate("a small diff", "local", [])[0].cosmetic)


class PollerTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.gate = QuizGate(replace(SETTINGS, data_dir=Path(self.temp.name)))
        patches = [patch.object(self.gate.github, "run", return_value="diff --git a/pi.c b/pi.c"),
                   patch.object(self.gate.github, "pr_info", return_value=copy.deepcopy(INFO)),
                   patch.object(ChangeInspector, "inspect", return_value=dict(CODE_CHANGE)),
                   patch.object(self.gate.github, "open_prs",
                                return_value=[copy.deepcopy(INFO), dict(INFO, number=2, user={"login": "someone-else"})])]
        self.mocks = [p.start() for p in patches]
        self.mock_gh = self.mocks[0]
        for p in patches:
            self.addCleanup(p.stop)
        self.addCleanup(self.temp.cleanup)
        self.gate.store.init()

    def test_new_revision_gets_a_quiz_once_and_other_authors_are_skipped(self):
        poller = Poller(self.gate)
        with patch.object(self.gate.generator, "generate", return_value=(Quiz.model_validate(FIXTURE), "test")) as generate:
            self.assertEqual(poller.poll_once(), [1])
            self.assertEqual(poller.poll_once(), [])
        generate.assert_called_once()
        self.assertEqual(len(self.gate.store.quizzes()), 1)

    def test_failing_revision_is_retried_then_marked_error(self):
        poller = Poller(self.gate)
        with patch.object(self.gate.generator, "generate", side_effect=ValueError("bad reply")) as generate:
            for _ in range(Poller.MAX_FAILURES + 2):
                self.assertEqual(poller.poll_once(), [])
        self.assertEqual(generate.call_count, Poller.MAX_FAILURES)
        self.assertEqual(self.mock_gh.call_args.kwargs["payload"]["state"], "error")


class ChangesTests(unittest.TestCase):
    """The cosmetic verdict and the documentation check (changes.py)."""

    BEFORE = '#include "pi.h"\n#define WIDTH 15\nint print_pi(void)\n{\n    return printf("%.15f", x) < 0;\n}\n'

    def test_comments_and_formatting_are_cosmetic(self):
        after = ('/** @file pi.c\n * @brief Pi. */\n#include "pi.h"\n#define WIDTH 15\n'
                 'int print_pi( void ) { return printf( "%.15f", x )<0; }  // Print it\n')
        self.assertTrue(ChangeInspector.is_cosmetic("src/pi.c", self.BEFORE, after))
        self.assertTrue(ChangeInspector.is_cosmetic("README.md", "old", "new"))

    def test_code_literals_directives_and_other_files_are_not_cosmetic(self):
        for after in (self.BEFORE.replace("< 0", "<= 0"),            # Code
                      self.BEFORE.replace('"%.15f"', '"%.15f "'),  # Space inside a string literal
                      self.BEFORE.replace("#define WIDTH 15\nint", "#define WIDTH 15 int"),  # Directive end
                      self.BEFORE.replace('"%.15f"', '"/* %.15f */"')):  # Comment marker inside a literal
            self.assertFalse(ChangeInspector.is_cosmetic("src/pi.c", self.BEFORE, after), after)
        self.assertFalse(ChangeInspector.is_cosmetic("Makefile", "LDLIBS =", "LDLIBS = -lm"))
        self.assertFalse(ChangeInspector.is_cosmetic("src/new.c", None, "int x;"))
        self.assertTrue(ChangeInspector.is_cosmetic("src/new.h", None, "/* Only a comment */"))

    @unittest.skipUnless(shutil.which("bwrap"), "bubblewrap is not installed")
    def test_build_and_tests_run_in_the_sandbox(self):
        with tempfile.TemporaryDirectory() as folder:
            tree = Path(folder)
            inspector = ChangeInspector(GitHub("owner/name").run, "owner/name")
            (tree / "Makefile").write_text("all:\n\techo built > out.txt\ncheck: all\n\tgrep -q built out.txt\n")
            ok, report = inspector.check_build(tree)
            self.assertTrue(ok, report)
            self.assertIn("$ make && make check: succeeded", report)
            (tree / "Makefile").write_text("all:\n\ttrue\ncheck:\n\techo test failed; false\n")
            ok, report = inspector.check_build(tree)
            self.assertFalse(ok)
            self.assertIn("test failed", report)
            # A compiler warning fails the check, unless warnings are allowed
            (tree / "w.c").write_text("int main(void) { int unused; return 0; }\n")
            (tree / "Makefile").write_text("all:\n\tcc -Wall -c w.c -o w.o\n")
            ok, report = inspector.check_build(tree)
            self.assertFalse(ok)
            self.assertIn("built with 1 compiler warning(s), which fail the check", report)
            self.assertIn("w.c:1:", report.splitlines()[1])  # The warning is listed first
            self.assertTrue(ChangeInspector(GitHub("owner/name").run, "owner/name", fail_on_warnings=False).check_build(tree)[0])
            (tree / "Makefile").write_text("all:\n\tcat /etc/os-release\n")  # The sandbox sees only the tree (and no host /etc)
            self.assertFalse(inspector.check_build(tree)[0])
            (tree / "Makefile").unlink()
            self.assertEqual(inspector.check_build(tree), (True, "No Makefile: nothing to build."))

    @unittest.skipUnless(shutil.which("doxygen"), "doxygen is not installed")
    def test_documentation_is_checked_on_the_whole_tree_but_reported_for_changed_files(self):
        header = '/** @file pi.h\n * @brief Pi. */\n/** @brief Print pi.\n * @return 0 on success. */\nint print_pi(void);\n'
        source = '/** @file pi.c\n * @brief Pi. */\n#include "pi.h"\nint print_pi(void) { return 0; }\n'
        bare = 'int other(void) { return 1; }\n'
        with tempfile.TemporaryDirectory() as folder:
            tree = Path(folder)
            (tree / "src").mkdir()
            (tree / "src" / "pi.h").write_text(header)
            (tree / "src" / "pi.c").write_text(source)
            (tree / "src" / "other.c").write_text(bare)
            # pi.c's function is documented in its header; other.c is not changed, so not reported
            self.assertEqual(ChangeInspector.check_docs(tree, ["src/pi.c"]),
                             (True, "All 1 changed C/C++ file(s) are documented."))
            ok, report = ChangeInspector.check_docs(tree, ["src/pi.c", "src/other.c"])
            self.assertFalse(ok)
            self.assertIn("src/other.c:1: error: File has no @file", report)
            self.assertNotIn("pi.c", report)
        self.assertEqual(ChangeInspector.check_docs(Path("."), []), (True, "No C/C++ files changed."))


class StatusTests(unittest.TestCase):
    """The agents' status report."""

    def test_a_waiting_quiz_tells_the_model_to_stop_checking(self):
        gate = QuizGate(SETTINGS)
        pr = {"number": 7, "title": "Add a module", "state": "open", "user": {"login": "dev"},
              "head": {"sha": "a" * 40}, "base": {"sha": "b" * 40}}
        waiting = {"id": "q1", "build_ok": 1, "docs_ok": 1, "cosmetic": 0, "passed": 0, "skipped": 0}
        with patch.object(gate.store, "init"), patch.object(gate.github, "open_prs", return_value=[pr]), \
                patch.object(gate, "find_quiz", return_value=waiting), \
                patch.object(PrGateCli, "running", return_value=True):
            text = PrGateCli(gate).status()
            self.assertIn("quiz waiting, merge blocked: http://gate.test/q/q1", text)
            self.assertTrue(text.endswith("checking again will not change it. Give the user the quiz link and stop."))
            with patch.object(gate, "find_quiz", return_value={**waiting, "passed": 1}):
                self.assertNotIn("checking again", PrGateCli(gate).status())  # Nothing to wait for


class ServiceControlTests(unittest.TestCase):
    """The agents may start the gate's own service, and nothing else: never stop or restart it."""

    def test_only_the_gate_unit_is_started(self):
        calls = []

        def fake_run(command, **_kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch("gatekeepers.pr.pr_gate.subprocess.run", side_effect=fake_run), \
                patch.object(PrGateCli, "running", return_value=True), \
                patch.dict(os.environ, {"AGENT_NAME": "Test Agent"}):
            text = PrGateCli(QuizGate(SETTINGS)).control("start")
        self.assertIn("Started the pr-gate service (asked by Test Agent)", text)
        self.assertEqual(calls, [["systemctl", "--user", "cat", "pr-gate"], ["systemctl", "--user", "start", "pr-gate"]])

    def test_the_agents_cannot_stop_or_restart_the_gate(self):
        for action in ("stop", "restart", "disable"):
            with patch("gatekeepers.pr.pr_gate.subprocess.run") as run, \
                    self.assertRaisesRegex(ValueError, "Only the user stops or restarts the gate"):
                PrGateCli(QuizGate(SETTINGS)).control(action)
            run.assert_not_called()
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            PrGateCli.build_parser(5).parse_args(["status", "--action", "stop"])

    def test_a_missing_unit_is_reported(self):
        missing = subprocess.CompletedProcess([], 1, "", "No files found")
        with patch("gatekeepers.pr.pr_gate.subprocess.run", return_value=missing), \
                self.assertRaisesRegex(ValueError, "install.sh --gate install"):
            PrGateCli(QuizGate(SETTINGS)).control("start")


class CloneSyncTests(unittest.TestCase):
    """The service keeps the local clone current, only when that is safe."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.remote, self.clone, self.other = root / "remote.git", root / "clone", root / "other"
        self.git("init", "-q", "--bare", "-b", "main", str(self.remote), cwd=root)
        self.git("clone", "-q", str(self.remote), str(self.other), cwd=root)
        self.commit(self.other, "a.c", "First")
        self.git("clone", "-q", str(self.remote), str(self.clone), cwd=root)

    @staticmethod
    def git(*args, cwd):
        subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@x", *args], cwd=cwd, check=True,
                       capture_output=True)

    def commit(self, repo, name, message):
        (repo / name).write_text(message + "\n")
        self.git("add", name, cwd=repo)
        self.git("commit", "-q", "-m", message, cwd=repo)
        self.git("push", "-q", "origin", "main", cwd=repo)

    def test_new_commits_on_github_reach_a_clean_clone(self):
        self.assertEqual(LocalClone(str(self.clone)).sync()[0], "current")
        self.commit(self.other, "b.c", "Second")
        outcome, message = LocalClone(str(self.clone)).sync()
        self.assertEqual(outcome, "updated", message)
        self.assertIn("Second", message)
        self.assertTrue((self.clone / "b.c").exists())

    def test_work_in_progress_local_commits_and_other_branches_are_left_alone(self):
        self.commit(self.other, "b.c", "Second")
        (self.clone / "draft.c").write_text("int x;\n")  # An agent is working: even a new file counts
        self.assertIn("changes in progress", LocalClone(str(self.clone)).sync()[1])
        self.assertFalse((self.clone / "b.c").exists())
        (self.clone / "draft.c").unlink()
        self.git("checkout", "-q", "-b", "topic", cwd=self.clone)
        self.assertIn("not on main", LocalClone(str(self.clone)).sync()[1])
        self.git("checkout", "-q", "main", cwd=self.clone)
        (self.clone / "local.c").write_text("int y;\n")
        self.git("add", "local.c", cwd=self.clone)
        self.git("commit", "-q", "-m", "Local only", cwd=self.clone)
        self.assertIn("commits that are not on GitHub", LocalClone(str(self.clone)).sync()[1])
        self.assertEqual(LocalClone(str(Path(self.temp.name))).sync()[0], "skipped")  # Not a clone

    def test_the_poller_syncs_on_its_own_schedule(self):
        settings = replace(SETTINGS, data_dir=Path(self.temp.name), local_clone=str(self.clone), sync_seconds=60)
        poller = Poller(QuizGate(settings))
        self.commit(self.other, "b.c", "Second")
        poller.sync_clone()
        self.assertTrue((self.clone / "b.c").exists())
        self.assertIsNotNone(poller.last_sync)
        assert poller.last_sync is not None
        self.assertIn("fast-forwarded by 1 commit", poller.last_sync)
        self.commit(self.other, "c.c", "Third")
        poller.sync_clone()  # Not due yet
        self.assertFalse((self.clone / "c.c").exists())


if __name__ == "__main__":
    unittest.main()
