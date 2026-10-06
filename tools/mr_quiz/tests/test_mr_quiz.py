"""
Offline tests for the mr_quiz tool: temporary storage, and mocked GitHub and model calls, so no
real PR is ever marked. Run from the repository root:
    .venv/bin/python -m unittest discover -s tools/mr_quiz/tests
"""

import copy
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Keep the module-level data directory away from live data, then import the tool's modules
IMPORT_DATA = tempfile.TemporaryDirectory()
os.environ["QUIZ_DATA_DIR"] = IMPORT_DATA.name
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import quiz  # noqa: E402
import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

FIXTURE = {"title": "C math quiz", "questions": [
    {"question": f"What does the changed code do in case {i}?",
     "options": ["Answer A", "Answer B", "Answer C", "Answer D"],
     "correct": i, "explanation": "Private explanation sentinel " + str(i)}
    for i in range(3)]}
INFO = {"number": 1, "state": "open", "base": {"ref": "main", "sha": "b" * 40},
        "head": {"sha": "a" * 40}, "user": {"login": quiz.DEVELOPER}, "title": "Compute pi"}
MODELS = {"default": "local", "profiles": {
    "local": {"name": "Local", "base_url": "http://boba:1234/v1", "model": "qwen", "api_key": "lm-studio"},
    "openai": {"name": "OpenAI", "base_url": "https://api.openai.com/v1", "model": "gpt",
               "api_key_env": "TEST_OPENAI_KEY"}}}


def reply(content, finish_reason="stop"):
    """A chat completions response body with one choice."""
    return {"choices": [{"finish_reason": finish_reason, "message": {"content": content}}]}


class QuizTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_patch = patch.object(quiz, "DATA", Path(self.temp.name))
        self.data_patch.start()
        self.info = patch.object(quiz, "pr_info", return_value=copy.deepcopy(INFO))
        self.mock_info = self.info.start()
        self.gh = patch.object(quiz, "gh", return_value="diff --git a/pi.c b/pi.c")
        self.mock_gh = self.gh.start()
        self.model = patch.object(quiz, "generate", return_value=(quiz.Quiz.model_validate(FIXTURE), "test model"))
        self.model.start()
        self.row = quiz.create_quiz(1)
        self.qid = self.row["id"]
        self.content = quiz.Quiz.model_validate_json(quiz.get_quiz(self.qid)["content"])
        self.answers = [q.correct for q in self.content.questions]
        self.app = server.create_app()
        self.client = TestClient(self.app)
        self.client.auth = (quiz.DEVELOPER, self.app.state.password)

    def tearDown(self):
        self.client.close()
        self.model.stop()
        self.gh.stop()
        self.info.stop()
        self.data_patch.stop()
        self.temp.cleanup()

    def wrong(self):
        return [(x + 1) % 4 for x in self.answers]

    def test_generation_publishes_pending_with_quiz_link_to_exact_sha(self):
        call = self.mock_gh.call_args
        self.assertEqual(call.kwargs["payload"]["state"], "pending")
        self.assertEqual(call.kwargs["payload"]["context"], "developer-quiz")
        self.assertEqual(call.kwargs["payload"]["target_url"], f"{quiz.BASE_URL}/q/{self.qid}")
        self.assertIn(INFO["head"]["sha"], call.args[1])
        self.assertEqual(self.row["source"], "test model")

    def test_answers_not_rendered_and_auth_required(self):
        page = self.client.get("/q/" + self.qid)
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("Private explanation sentinel", page.text)
        self.assertNotIn('"correct"', page.text)
        self.assertIn('name="q0"', page.text)
        self.assertEqual(self.client.get("/", auth=None).status_code, 401)
        self.assertEqual(self.client.get("/", auth=("bad", "bad")).status_code, 401)
        self.assertEqual(self.client.get("/health", auth=None).status_code, 200)

    def test_pass_is_persisted_and_published(self):
        self.assertTrue(quiz.submit(self.qid, self.answers)["passed"])
        self.assertEqual(quiz.get_quiz(self.qid)["published"], "success")
        with quiz.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)

    def test_failure_allows_retry_and_hides_explanations(self):
        result = quiz.submit(self.qid, self.wrong())
        self.assertFalse(result["passed"])
        self.assertEqual(result["questions"], [])
        self.assertEqual(quiz.get_quiz(self.qid)["published"], "failure")
        self.assertTrue(quiz.submit(self.qid, self.answers)["passed"])

    def test_pass_cannot_be_downgraded(self):
        quiz.submit(self.qid, self.answers)
        quiz.submit(self.qid, self.wrong())
        self.assertEqual(quiz.get_quiz(self.qid)["published"], "success")

    def test_changed_head_or_base_rejected_without_publishing(self):
        for field in ("head", "base"):
            info = copy.deepcopy(INFO)
            info[field]["sha"] = "c" * 40
            self.mock_info.return_value = info
            self.mock_gh.reset_mock()
            with self.assertRaises(ValueError):
                quiz.submit(self.qid, self.answers)
            self.mock_gh.assert_not_called()

    def test_closed_pr_or_wrong_developer_rejected(self):
        for info in [dict(INFO, state="closed"), dict(INFO, user={"login": "someone-else"})]:
            self.mock_info.return_value = info
            with self.assertRaises(ValueError):
                quiz.submit(self.qid, self.answers)

    def test_publication_failure_can_be_retried(self):
        self.mock_gh.side_effect = RuntimeError("network unavailable")
        with self.assertRaises(RuntimeError):
            quiz.submit(self.qid, self.answers)
        self.assertTrue(quiz.get_quiz(self.qid)["passed"])
        self.assertEqual(quiz.get_quiz(self.qid)["published"], "pending")
        self.mock_gh.side_effect = None
        quiz.submit(self.qid, self.answers)
        self.assertEqual(quiz.get_quiz(self.qid)["published"], "success")

    def test_invalid_answers_rejected(self):
        for answers in [[], [4, 0, 0], [True, 0, 0], ["0", 0, 0]]:
            with self.assertRaises(ValueError):
                quiz.submit(self.qid, answers)

    def test_browser_form_csrf_and_grading(self):
        url = "/q/" + self.qid
        self.assertEqual(self.client.post(url, data={"q0": "0"}).status_code, 403)
        token = re.search(r'name="csrf" value="([0-9a-f]+)"', self.client.get(url).text).group(1)
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
                quiz.Quiz.model_validate(fixture)

    def test_same_revision_reuses_quiz(self):
        self.assertEqual(quiz.create_quiz(1)["id"], self.qid)

    def test_new_revision_gets_new_quiz(self):
        info = copy.deepcopy(INFO)
        info["head"]["sha"] = "d" * 40
        self.mock_info.return_value = info
        row = quiz.create_quiz(1)
        self.assertNotEqual(row["id"], self.qid)
        self.assertEqual(row["sha"], "d" * 40)

    def test_mid_generation_change_rejected(self):
        self.mock_info.side_effect = [dict(INFO, head={"sha": "e" * 40}), dict(INFO, head={"sha": "f" * 40})]
        with self.assertRaises(ValueError):
            quiz.create_quiz(1)

    def test_html_escapes_model_text(self):
        fixture = copy.deepcopy(FIXTURE)
        fixture["title"] = "<script>alert(1)</script>"
        with quiz.connect() as db:
            db.execute("UPDATE quizzes SET content=? WHERE id=?", (json.dumps(fixture), self.qid))
        page = self.client.get("/q/" + self.qid)
        self.assertNotIn("<script>", page.text)
        self.assertIn("&lt;script&gt;", page.text)


class ModelTests(unittest.TestCase):

    def setUp(self):
        self.models = Path(tempfile.mkdtemp()) / "models.json"
        self.models.write_text(json.dumps(MODELS))

    def test_profiles_come_from_the_shared_models_file(self):
        self.assertEqual(quiz.resolve_model(None, self.models)["base_url"], "http://boba:1234/v1")
        with patch.dict(os.environ, {"TEST_OPENAI_KEY": "sk-test"}):
            settings = quiz.resolve_model("openai", self.models)
        self.assertEqual((settings["model"], settings["api_key"]), ("gpt", "sk-test"))

    def test_missing_key_and_unknown_profile(self):
        with patch.dict(os.environ, {"TEST_OPENAI_KEY": ""}), self.assertRaises(ValueError):
            quiz.resolve_model("openai", self.models)
        with self.assertRaises(ValueError):
            quiz.resolve_model("nope", self.models)

    def test_fenced_json_is_accepted_and_invalid_reply_retried(self):
        settings = {"name": "Local", "base_url": "http://x/v1", "model": "m", "api_key": "k", "timeout": 5}
        with patch.object(quiz, "resolve_model", return_value=settings), patch.object(quiz.httpx, "post") as post:
            post.return_value.json.side_effect = [reply("not JSON"), reply("```json\n" + json.dumps(FIXTURE) + "\n```")]
            content, source = quiz.generate("a small diff", "local")
            self.assertEqual(len(content.questions), 3)
            self.assertEqual(source, "Local / m")
            self.assertEqual(post.call_count, 2)
            self.assertEqual(post.call_args.args[0], "http://x/v1/chat/completions")
            self.assertEqual(post.call_args.kwargs["json"]["messages"][0]["content"], quiz.load_instructions())
            self.assertIn("untrusted data", quiz.load_instructions())
            post.return_value.json.side_effect = [reply("not JSON"), reply(json.dumps(FIXTURE), "length")]
            with self.assertRaises(ValueError):
                quiz.generate("a small diff", "local")


class PollerTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        patches = [patch.object(quiz, "DATA", Path(self.temp.name)),
                   patch.object(quiz, "gh", return_value="diff --git a/pi.c b/pi.c"),
                   patch.object(quiz, "pr_info", return_value=copy.deepcopy(INFO)),
                   patch.object(quiz, "open_prs", return_value=[copy.deepcopy(INFO),
                                                                dict(INFO, number=2, user={"login": "someone-else"})])]
        self.mocks = [p.start() for p in patches]
        self.mock_gh = self.mocks[1]
        for p in patches:
            self.addCleanup(p.stop)
        self.addCleanup(self.temp.cleanup)
        quiz.init()

    def test_new_revision_gets_a_quiz_once_and_other_authors_are_skipped(self):
        poller = server.Poller()
        with patch.object(quiz, "generate", return_value=(quiz.Quiz.model_validate(FIXTURE), "test")) as generate:
            self.assertEqual(poller.poll_once(), [1])
            self.assertEqual(poller.poll_once(), [])
        generate.assert_called_once()
        self.assertEqual(len(quiz.list_quizzes()), 1)

    def test_failing_revision_is_retried_then_marked_error(self):
        poller = server.Poller()
        with patch.object(quiz, "generate", side_effect=ValueError("bad reply")) as generate:
            for _ in range(server.MAX_FAILURES + 2):
                self.assertEqual(poller.poll_once(), [])
        self.assertEqual(generate.call_count, server.MAX_FAILURES)
        self.assertEqual(self.mock_gh.call_args.kwargs["payload"]["state"], "error")


if __name__ == "__main__":
    unittest.main()
