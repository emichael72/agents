"""
Offline tests for the pr_gate tool: temporary storage, and mocked GitHub and model calls, so no
real PR is ever marked. Run from the repository root:
    .venv/bin/python -m unittest discover -s tools/pr_gate/tests
"""

import copy
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Keep the module-level data directory away from live data, then import the tool's modules
IMPORT_DATA = tempfile.TemporaryDirectory()
os.environ["QUIZ_DATA_DIR"] = IMPORT_DATA.name
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import changes  # noqa: E402
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
CODE_CHANGE = {"build_ok": True, "build_report": "$ make && make check: succeeded", "docs_ok": True, "docs_report": "All 1 changed C/C++ file(s) are documented.",
               "cosmetic": False, "code_files": ["src/pi.c"]}
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
        self.inspect = patch.object(quiz.changes, "inspect_pr", return_value=dict(CODE_CHANGE))
        self.mock_inspect = self.inspect.start()
        self.row = quiz.create_quiz(1)
        self.qid = self.row["id"]
        self.content = quiz.Quiz.model_validate_json(quiz.get_quiz(self.qid)["content"])
        self.answers = [q.correct for q in self.content.questions]
        self.app = server.create_app()
        self.client = TestClient(self.app)
        self.client.post("/login", data={"user": "user", "password": "pass"})

    def tearDown(self):
        self.client.close()
        self.inspect.stop()
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

    def new_revision(self, sha, inspection, content=None):
        """Assess a new head commit with the given inspection result (and model reply)."""
        info = copy.deepcopy(INFO)
        info["head"]["sha"] = sha * 40
        self.mock_info.return_value = info
        self.mock_inspect.return_value = inspection
        if content is not None:
            quiz.generate.return_value = (quiz.Quiz.model_validate(content), "test model")
        return quiz.create_quiz(1)

    def test_documentation_problems_fail_the_check_and_show_on_the_page(self):
        report = "src/pi.c:6: error: Member print_pi() (function) of file pi.c is not documented."
        row = self.new_revision("d", dict(CODE_CHANGE, docs_ok=False, docs_report=report))
        self.assertEqual(self.mock_gh.call_args.kwargs["payload"]["state"], "failure")
        self.assertIn("Documentation problems", self.mock_gh.call_args.kwargs["payload"]["description"])
        page = self.client.get("/q/" + row["id"]).text
        self.assertIn("print_pi() (function) of file pi.c is not documented", page)
        self.assertNotIn('name="q0"', page)  # No quiz until the documentation is fixed
        with self.assertRaisesRegex(ValueError, "documentation"):
            quiz.submit(row["id"], [q.correct for q in quiz.Quiz.model_validate_json(row["content"]).questions])

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

        with patch.object(quiz, "PR_COMMENT", True), patch.object(quiz, "gh", side_effect=fake_gh):
            row = quiz.get_quiz(self.qid)
            quiz.publish(row)
            self.assertEqual(len(comments), 1)
            self.assertIn(f"[Take the quiz]({quiz.BASE_URL}/q/{self.qid})", comments[0]["body"])
            self.assertIn("| Build and tests | ✅ make && make check: succeeded |", comments[0]["body"])
            quiz.publish(row)  # Nothing changed: no new comment, no edit
            self.assertEqual(sum(1 for args, _ in calls if "PATCH" in args or "POST" in args and "comments" in args[1]), 1)
            quiz.submit(self.qid, self.answers)
            self.assertEqual(len(comments), 1)  # Edited in place
            self.assertIn("✅ [Passed]", comments[0]["body"])
            self.assertTrue(comments[0]["body"].startswith(quiz.COMMENT_MARKER))

    def test_failed_build_or_tests_fail_the_check_and_show_on_the_page(self):
        report = "$ make && make check: FAILED\ncore_dump: invalid option or unexpected option argument"
        row = self.new_revision("c", dict(CODE_CHANGE, build_ok=False, build_report=report))
        payload = self.mock_gh.call_args.kwargs["payload"]
        self.assertEqual((payload["state"], payload["description"]), ("failure", "Build or tests failed; see Details"))
        page = self.client.get("/q/" + row["id"]).text
        self.assertIn("invalid option or unexpected option argument", page)
        self.assertNotIn('name="q0"', page)
        with self.assertRaisesRegex(ValueError, "build"):
            quiz.submit(row["id"], [q.correct for q in quiz.Quiz.model_validate_json(row["content"]).questions])

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
            quiz.create_quiz(1, fixed=str(fixture))

    def test_quiz_kind_and_question_count_must_match(self):
        with self.assertRaises(ValueError):
            quiz.Quiz.model_validate({"title": "Comment updates", "cosmetic": True, "questions": FIXTURE["questions"]})
        with self.assertRaises(ValueError):
            quiz.Quiz.model_validate({"title": "Code change", "questions": []})


class ModelTests(unittest.TestCase):

    def setUp(self):
        self.models = Path(tempfile.mkdtemp()) / "models.json"
        self.models.write_text(json.dumps(MODELS))

    def test_settings_come_from_the_manifest_and_the_environment_overrides_them(self):
        manifest = json.loads(quiz.MANIFEST_FILE.read_text())["env"]
        self.assertEqual(quiz.setting("QUIZ_DEVELOPER"), os.environ.get("QUIZ_DEVELOPER") or manifest["QUIZ_DEVELOPER"])
        with patch.dict(os.environ, {"QUIZ_REPO": "someone/else"}):
            self.assertEqual(quiz.setting("QUIZ_REPO"), "someone/else")
        with self.assertRaisesRegex(ValueError, "QUIZ_NOT_SET"):
            quiz.setting("QUIZ_NOT_SET")
        self.assertEqual(quiz.setting("QUIZ_NOT_SET", required=False), "")

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

    def test_a_code_change_called_cosmetic_is_rejected(self):
        settings = {"name": "Local", "base_url": "http://x/v1", "model": "m", "api_key": "k", "timeout": 5}
        cosmetic = json.dumps({"title": "Comment updates", "cosmetic": True, "questions": []})
        with patch.object(quiz, "resolve_model", return_value=settings), patch.object(quiz.httpx, "post") as post:
            post.return_value.json.side_effect = [reply(cosmetic), reply(cosmetic)]
            with self.assertRaisesRegex(ValueError, "cosmetic"):
                quiz.generate("a small diff", "local", ["src/pi.c"])
            self.assertIn("Server analysis: code changed in: src/pi.c",
                          post.call_args.kwargs["json"]["messages"][1]["content"])
            post.return_value.json.side_effect = [reply(cosmetic)]
            self.assertTrue(quiz.generate("a small diff", "local", [])[0].cosmetic)


class PollerTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        patches = [patch.object(quiz, "DATA", Path(self.temp.name)),
                   patch.object(quiz, "gh", return_value="diff --git a/pi.c b/pi.c"),
                   patch.object(quiz, "pr_info", return_value=copy.deepcopy(INFO)),
                   patch.object(quiz.changes, "inspect_pr", return_value=dict(CODE_CHANGE)),
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


class ChangesTests(unittest.TestCase):
    """The cosmetic verdict and the documentation check (changes.py)."""

    BEFORE = '#include "pi.h"\n#define WIDTH 15\nint print_pi(void)\n{\n    return printf("%.15f", x) < 0;\n}\n'

    def test_comments_and_formatting_are_cosmetic(self):
        after = ('/** @file pi.c\n * @brief Pi. */\n#include "pi.h"\n#define WIDTH 15\n'
                 'int print_pi( void ) { return printf( "%.15f", x )<0; }  // Print it\n')
        self.assertTrue(changes.is_cosmetic("src/pi.c", self.BEFORE, after))
        self.assertTrue(changes.is_cosmetic("README.md", "old", "new"))

    def test_code_literals_directives_and_other_files_are_not_cosmetic(self):
        for after in (self.BEFORE.replace("< 0", "<= 0"),            # Code
                      self.BEFORE.replace('"%.15f"', '"%.15f "'),  # Space inside a string literal
                      self.BEFORE.replace("#define WIDTH 15\nint", "#define WIDTH 15 int"),  # Directive end
                      self.BEFORE.replace('"%.15f"', '"/* %.15f */"')):  # Comment marker inside a literal
            self.assertFalse(changes.is_cosmetic("src/pi.c", self.BEFORE, after), after)
        self.assertFalse(changes.is_cosmetic("Makefile", "LDLIBS =", "LDLIBS = -lm"))
        self.assertFalse(changes.is_cosmetic("src/new.c", None, "int x;"))
        self.assertTrue(changes.is_cosmetic("src/new.h", None, "/* Only a comment */"))

    @unittest.skipUnless(shutil.which("bwrap"), "bubblewrap is not installed")
    def test_build_and_tests_run_in_the_sandbox(self):
        with tempfile.TemporaryDirectory() as folder:
            tree = Path(folder)
            (tree / "Makefile").write_text("all:\n\techo built > out.txt\ncheck: all\n\tgrep -q built out.txt\n")
            ok, report = changes.check_build(tree)
            self.assertTrue(ok, report)
            self.assertIn("$ make && make check: succeeded", report)
            (tree / "Makefile").write_text("all:\n\ttrue\ncheck:\n\techo test failed; false\n")
            ok, report = changes.check_build(tree)
            self.assertFalse(ok)
            self.assertIn("test failed", report)
            (tree / "Makefile").write_text("all:\n\tcat /etc/passwd\n")  # The sandbox sees only the tree
            self.assertFalse(changes.check_build(tree)[0])
            (tree / "Makefile").unlink()
            self.assertEqual(changes.check_build(tree), (True, "No Makefile: nothing to build."))

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
            self.assertEqual(changes.check_docs(tree, ["src/pi.c"]),
                             (True, "All 1 changed C/C++ file(s) are documented."))
            ok, report = changes.check_docs(tree, ["src/pi.c", "src/other.c"])
            self.assertFalse(ok)
            self.assertIn("src/other.c:1: error: File has no @file", report)
            self.assertNotIn("pi.c", report)
        self.assertEqual(changes.check_docs(Path("."), []), (True, "No C/C++ files changed."))


if __name__ == "__main__":
    unittest.main()
