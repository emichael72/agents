"""
Module: test_agent.py

Description:
    Offline tests for the Pydantic Agent: a scripted `FunctionModel` stands in for LM Studio,
    while the tools run for real.
"""
import io
import json
import re
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Callable, cast
from unittest.mock import Mock, patch

from rich.console import Console
import pydantic as pydantic_dependency
from pydantic_ai import ModelRetry, ToolFailed
from pydantic_ai.messages import (ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart,
                                  UserPromptPart)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from pydantic_agent import AGENT_FILE, CONTEXT_DIR, REPO_ROOT, TOOLS_DIR
from pydantic_agent.context import AgentContext
from pydantic_agent.output import Output
from pydantic_agent.profiles import ModelProfiles
from pydantic_agent.session import AgentSession
from pydantic_agent.guard import RepeatGuard
from pydantic_agent.toolset import LocalTools

# The scripted turn every agent's tests replay (tests/scenario.json)
SCENARIO = json.loads((REPO_ROOT / "tests" / "scenario.json").read_text())
CALLS = [(step["tool"], step["arguments"]) for step in SCENARIO["calls"]]


async def scripted_model(messages, _info: AgentInfo):
    """
    A stand-in model, scripted for two requests.
    The first request is answered with a call to every tool in CALLS, in one response; the
    next is answered with the text of the tool results it received.
    Args:
        messages: The conversation so far.
        _info: pydantic-ai's information about the run (unused).
    Yields:
        Tool-call deltas for the first request, then the answer text.
    """
    if len(messages) == 1:
        for i, (name, args) in enumerate(CALLS):
            yield {i: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id=f"call-{i}")}
        return
    returns = [part for part in messages[-1].parts if isinstance(part, ToolReturnPart)]
    yield " | ".join(part.model_response_str() for part in returns)


def tool_function(toolset: Any, name: str) -> Callable[..., str]:
    """Return a manifest tool's function; it takes the tool's arguments, not a RunContext."""
    return cast(Callable[..., str], toolset.tools[name].function)


class AgentTests(unittest.IsolatedAsyncioTestCase):
    """Agent tests with a scripted model and the real tools."""
    def setUp(self):
        """Send the agent's terminal output to a buffer so the test output stays clean."""
        self.output = io.StringIO()

    def session(self, trace: bool = False, parallel: bool = False) -> AgentSession:
        """
        A session that prints to the test's buffer.
        Args:
            trace: Print tool calls and results as gray lines.
            parallel: Run a response's tool calls concurrently.
        Returns:
            AgentSession: The session.
        """
        return AgentSession(trace=trace, parallel=parallel, console=Console(file=self.output))

    def test_package_namespace_and_dependency_do_not_conflict(self):
        project_dir = Path(__file__).resolve().parents[1]
        dependency_file = pydantic_dependency.__file__
        assert dependency_file is not None
        self.assertFalse(Path(dependency_file).resolve().is_relative_to(project_dir))
        self.assertEqual(CONTEXT_DIR, project_dir.parent / 'context')
        self.assertEqual(TOOLS_DIR, project_dir.parent / 'tools')

    async def run_tracked(self, parallel: bool):
        """
        Run the scripted turn, recording how many scripts were running as each one started.
        Args:
            parallel: Run the tool calls concurrently instead of one at a time.
        Returns:
            tuple: The message history and the list of concurrent-script counts.
        """
        running, overlaps, lock = [0], [], threading.Lock()
        real_run_script = LocalTools.run_script

        def tracked(*args, **kwargs):
            with lock:
                running[0] += 1
                overlaps.append(running[0])
            time.sleep(0.05)  # Widen the window in which a parallel call would overlap
            try:
                return real_run_script(*args, **kwargs)
            finally:
                with lock:
                    running[0] -= 1

        session = self.session(parallel=parallel)
        bot = session.build_agent(FunctionModel(stream_function=scripted_model))
        with patch.object(LocalTools, "run_script", tracked):
            history = await session.ask(bot, SCENARIO["prompt"], [])
        return history, overlaps

    def test_tool_calls_run_concurrently_by_default(self):
        self.assertTrue(AgentContext().own()["parallel_tool_calls"])  # pydantic/instructions.json
        self.assertTrue(AgentSession(console=Console(file=self.output)).parallel)

    async def test_parallel_runs_tools_concurrently(self):
        _, overlaps = await self.run_tracked(parallel=True)
        self.assertGreater(max(overlaps), 1)

    async def test_tools_run_one_at_a_time_and_failures_reach_the_model(self):
        history, overlaps = await self.run_tracked(parallel=False)
        self.assertEqual(overlaps, [1, 1, 1])  # never two scripts at once in sequential mode
        answer = history[-1].parts[0].content
        for step in SCENARIO["calls"]:
            self.assertIn(step["output"], answer)
        returns = [part.tool_name for message in history if isinstance(message, ModelRequest)
                   for part in message.parts if isinstance(part, ToolReturnPart)]
        self.assertEqual(returns, [name for name, _ in CALLS])

    def test_a_tool_added_to_the_tools_folder_is_discovered(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "hello").mkdir()
            (Path(folder) / "hello" / "tool.json").write_text(json.dumps({
                "description": "Say hello", "command": "echo", "args": ["hello"],
                "params": [{"name": "who", "type": "string", "style": "positional"}],
            }))
            toolset = LocalTools.load(Path(folder))
            self.assertEqual(list(toolset.tools), ["hello"])
            hello = tool_function(toolset, "hello")
            self.assertEqual(hello(who="world"), "hello -- world")  # Positionals follow "--"
            with self.assertRaises(ModelRetry):
                hello(who=1)  # validated against the manifest's schema

    def test_model_profiles_come_from_the_shared_models_file(self):
        profiles = ModelProfiles.load()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key-not-real"}):
            for name in ("LOCAL_LLM_BASE_URL", "LOCAL_LLM_MODEL", "LOCAL_LLM_API_KEY"):
                os.environ.pop(name, None)
            with patch.object(ModelProfiles, "loaded_model", return_value=None):  # The server reports no loaded model
                local = profiles.resolve()  # "default": "local"
            fallback = profiles.models["profiles"]["local"]  # Whatever the file names, so editing it never breaks this test
            self.assertEqual((local["base_url"], local["model"]), (fallback["base_url"], fallback["model"]))
            with patch.object(ModelProfiles, "loaded_model", return_value="qwen/loaded-now") as asked:
                self.assertEqual(profiles.resolve()["model"], "qwen/loaded-now")  # model_auto
                self.assertEqual(profiles.resolve(model="explicit")["model"], "explicit")
            asked.assert_called_once_with("http://boba:1234/v1", "lm-studio")
            self.assertEqual(local["api_key"], "lm-studio")  # the OpenAI key is never used for another server
            os.environ["LOCAL_LLM_MODEL"] = "from-env"
            self.assertEqual(profiles.resolve("local")["model"], "from-env")
            self.assertEqual(profiles.resolve("local", model="from-cli")["model"], "from-cli")
            self.assertEqual(profiles.resolve("openai")["api_key"], "test-key-not-real")
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            with self.assertRaisesRegex(ValueError, "OPENAI_API_KEY"):
                profiles.resolve("openai")
        with self.assertRaisesRegex(ValueError, "Unknown model profile 'nope'"):
            profiles.resolve("nope")

    def test_the_memory_index_joins_the_instructions(self):
        with tempfile.TemporaryDirectory() as folder:
            index = Path(folder) / "index.md"
            self.assertIn("memory is empty", AgentContext.memory_text(index))
            index.write_text("# Memory index\n\n- **preferences**: Prefers short answers (updated 2026-10-07)\n")
            text = AgentContext.memory_text(index)
            self.assertIn("- preferences: Prefers short answers", text)
            self.assertEqual(AgentContext.memory_text(None), "")

    def test_a_call_repeated_too_often_is_not_run(self):
        manifest = json.loads((TOOLS_DIR / "skill" / "tool.json").read_text())
        skill_tool = LocalTools.tool("skill", manifest).function
        with patch.object(LocalTools, "guard", RepeatGuard(2)):
            with patch.object(LocalTools, "run_script", return_value="Steps") as run:
                self.assertEqual(skill_tool(name="pull-request"), "Steps")
                self.assertEqual(skill_tool(name="pull-request"), "Steps")
                with self.assertRaisesRegex(ToolFailed, "Not run: you made this same skill call, with the same "
                                                        "arguments, 2 times in a row"):
                    skill_tool(name="pull-request")
                self.assertEqual(run.call_count, 2)  # The third did not run
                self.assertEqual(skill_tool(name="build"), "Steps")  # Another call runs
                LocalTools.guard.reset()  # A new turn
                skill_tool(name="pull-request"), skill_tool(name="pull-request")
                self.assertEqual(run.call_count, 5)

    def test_the_skills_join_the_instructions(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(AgentContext.skills_text(Path(folder)), "")  # No skills yet
            (Path(folder) / "build").mkdir()
            (Path(folder) / "build" / "SKILL.md").write_text("---\nname: build\ndescription: Build and test a project.\n---\n\n# Build\n\n---\n\ndescription: not a header line\n")
            (Path(folder) / "notes").mkdir()  # A folder without SKILL.md is not a skill
            text = AgentContext.skills_text(Path(folder))
            self.assertTrue(text.endswith("read it with the skill tool and follow it):\n- build: Build and test a project."))
        self.assertEqual(AgentContext.skills_text(None), "")
        context = AgentContext()
        self.assertIn("\n- pull-request: ", context.system_prompt(context.agent_settings()))

    def test_exit_saves_only_after_a_tool_call_or_several_exchanges(self):
        self.assertIn("Nothing to save", AgentContext().instructions("on_exit"))
        self.assertTrue(json.loads(AGENT_FILE.read_text())["save_on_exit"])
        ask = ModelRequest(parts=[UserPromptPart("hi")])
        answer = ModelResponse(parts=[TextPart("hello")], provider_details=None, provider_response_id=None)
        call = ModelResponse(parts=[ToolCallPart("skill", {})], provider_details=None, provider_response_id=None)
        self.assertFalse(AgentContext.worth_saving([]))
        self.assertFalse(AgentContext.worth_saving([ask, answer]))
        self.assertTrue(AgentContext.worth_saving([ask, answer, ask, answer]))
        self.assertTrue(AgentContext.worth_saving([ask, call, answer]))

    def test_the_agent_is_named_dantic(self):
        context = AgentContext()
        self.assertEqual(context.own()["name"], "dantic")  # From pydantic/instructions.json
        self.assertTrue(context.identity("dantic").startswith("Your name is dantic."))
        self.assertEqual(context.identity(None), "")
        prompt = context.system_prompt(context.agent_settings())
        self.assertTrue(prompt.startswith("Your name is dantic."))
        self.assertIn("\n\nYour tools run in parallel: all the tool calls in one response", prompt)

    def test_instructions_come_from_the_shared_context_file(self):
        instructions = AgentContext().instructions()
        self.assertTrue(instructions.startswith("You are an agent"))
        self.assertIn("allowed folder", instructions)

    async def test_each_tool_call_prints_next_to_its_result(self):
        session = self.session(trace=True)
        bot = session.build_agent(FunctionModel(stream_function=scripted_model))
        await session.ask(bot, SCENARIO["prompt"], [])
        lines = [line[:1] + " " + line[2:].split("(")[0].split(":")[0]
                 for line in self.output.getvalue().splitlines() if line[:1] in "→←✗" and line]
        expected = [line for step in SCENARIO["calls"]
                    for line in (f"→ {step['tool']}", f"{'←' if step['outcome'] == 'ok' else '✗'} {step['tool']}")]
        self.assertEqual(lines, expected)

    def test_tools_are_told_which_agent_runs_them(self):
        self.assertEqual(LocalTools.run_script("printenv", "AGENT_NAME"), "Pydantic Agent")

    def test_an_omitted_optional_argument_may_be_null(self):
        skill = tool_function(LocalTools.load(), "skill")
        self.assertIn("# Change code and open a pull request", skill(name="pull-request"))
        self.assertEqual(skill(name=None), skill())  # null means omitted: the list

    async def test_history_carries_across_turns(self):
        async def remember(messages, _info):
            yield f"{len(messages)} messages so far"
        session = self.session()
        bot = session.build_agent(FunctionModel(stream_function=remember))
        history = await session.ask(bot, "one", [])
        history = await session.ask(bot, "two", history)
        self.assertEqual(history[-1].parts[0].content, "3 messages so far")


# noinspection DuplicatedCode
class OutputTests(unittest.TestCase):
    """The shared terminal layout (README.md, "Terminal output")."""

    def setUp(self):
        self.printed = io.StringIO()
        self.output = Output(Console(file=self.printed), {"width": 30, "show_time": True})

    # Each agent keeps independent tests for the shared terminal behavior.
    # noinspection DuplicatedCode
    def test_by_default_only_the_answer_and_timing_print_and_tools_show_on_the_spinner(self):
        printed = io.StringIO()
        output = Output(Console(file=printed, force_terminal=True, width=120), {"width": 120, "show_time": True},
                        debug=False)
        spinner = Mock()
        output.out.status = Mock(return_value=spinner)  # rich's spinner, without drawing it
        output.start()
        label = lambda: str(spinner.update.call_args.args[0])  # noqa: E731
        output.line('→ shell({"command":"ls"})')
        self.assertEqual(label(), "Running shell…")
        output.line("← shell: a.c")
        self.assertEqual(label(), "Thinking…")
        output.text("Two files.")
        spinner.stop.assert_called_once()  # The answer replaces the spinner
        output.line('→ ed({"path":"a.c"})')  # A call after the answer text: the spinner comes back
        self.assertEqual(output.out.status.call_count, 2)
        self.assertEqual(str(output.out.status.call_args.args[0]), "Running ed…")
        output.line("banner or hint")
        output.finish()
        output.note("History cleared.")
        plain = re.sub(r"\x1b\[[0-9;]*m", "", printed.getvalue())
        self.assertEqual(plain.splitlines()[1:], ["Two files.", "Response time: 0.0s", "", "History cleared."])

    # noinspection DuplicatedCode
    def test_text_around_hidden_tool_calls_has_one_blank_line_between(self):
        for more_text in (True, False):
            printed = io.StringIO()
            output = Output(Console(file=printed), {"width": 120, "show_time": True}, debug=False)
            output.start()
            output.text("Let me check the build.")
            output.line('→ shell({"command":"make"})')
            output.line("← shell: ok")
            if more_text:
                output.text("It builds cleanly.")
            output.finish()
            expected = ["", "Let me check the build."] + (["", "It builds cleanly."] if more_text else [])
            self.assertEqual(printed.getvalue().splitlines(), expected + ["Response time: 0.0s", ""])

    def test_lines_wrap_with_an_indent_and_keep_long_words_whole(self):
        url = "http://minion:8000/q/" + "x" * 40
        lines = Output.wrap("← pr_gate: the quiz is waiting and the merge is blocked " + url, 30)
        self.assertTrue(all(len(line) <= 30 for line in lines if line.strip() != url))
        self.assertTrue(all(line.startswith("  ") for line in lines[1:]))
        self.assertEqual(lines[-1], "  " + url)
        self.assertEqual(Output.wrap("short\nlines", 30), ["short", "lines"])

    def test_streamed_answer_wraps_between_words_and_is_timed(self):
        answer = "The quiz service is running and pull request number one is still waiting for its quiz."
        for i in range(0, len(answer), 7):  # Chunks split mid-word, as models stream
            self.output.text(answer[i:i + 7])
        self.output.finish()
        lines = self.printed.getvalue().split("\n")
        body = [line for line in lines if line and not line.startswith("Response time")]
        self.assertTrue(all(len(line) <= 30 for line in body))
        self.assertEqual(" ".join(body), answer)
        self.assertEqual(lines[0], "")  # Blank line before the answer
        self.assertRegex(self.printed.getvalue(), r"[^\n]\nResponse time: \d+\.\ds\n\n$")

    def test_token_counts_follow_the_response_time(self):
        printed = io.StringIO()
        output = Output(Console(file=printed), {"width": 120, "show_time": True, "show_tokens": True})
        output.add_usage(1200, 34)
        output.add_usage(1300, 56, requests=2)
        output.finish()
        self.assertRegex(printed.getvalue(), r"Response time: \d+\.\ds · tokens: 2,500 in, 90 out · 3 model calls\n\n$")
        printed.truncate(0), printed.seek(0)
        output.start()
        output.finish()
        self.assertIn("tokens: not reported", printed.getvalue())

    def test_links_become_clickable_and_stay_whole_while_streaming(self):
        self.assertEqual(Output.link_segments("see [PR #5](https://x/y) and http://a.b/c."),
                         [("see ", None), ("PR #5", "https://x/y"), (" and ", None),
                          ("http://a.b/c", "http://a.b/c"), (".", None)])
        printed = io.StringIO()
        output = Output(Console(file=printed, force_terminal=True, width=120), {"width": 40, "links": True})
        answer = "Open [the pending quiz](http://minion:8000/q/abc) now."
        for i in range(0, len(answer), 4):
            output.text(answer[i:i + 4])
        output.end()
        self.assertEqual(printed.getvalue().count("\x1b]8;"), 2)  # One link: opened and closed
        self.assertIn("the pending quiz", printed.getvalue())
        self.assertNotIn("](", printed.getvalue())
        self.assertIn("\x1b[96m", printed.getvalue())  # The link is bright cyan, the one vivid color

    def test_a_link_in_a_gray_line_is_bright_cyan_and_the_rest_stays_gray(self):
        printed = io.StringIO()
        output = Output(Console(file=printed, force_terminal=True, width=120), {"width": 120, "links": True})
        output.line("← pr: quiz at http://minion:8000/q/abc for PR #12")
        raw = printed.getvalue()
        self.assertRegex(raw, r"\x1b\[90m← pr: quiz at ")  # Gray before the link
        self.assertIn("\x1b]8;", raw)  # Clickable
        self.assertIn("\x1b[96mhttp://minion:8000/q/abc", raw)  # The link, bright cyan
        self.assertRegex(raw, r"\x1b\[90m for PR #12")  # Gray again after it

    def test_layout_settings_come_from_the_shared_context_file(self):
        settings = AgentContext().output_settings()
        self.assertEqual(settings["width"], 120)
        self.assertTrue(settings["show_time"])


if __name__ == "__main__":
    unittest.main()
