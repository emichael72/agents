"""
Module: test_agent.py

Description:
    Offline tests for the Pydantic Agent: a scripted `FunctionModel` stands in for LM Studio,
    while the tools run for real.
"""
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Callable, cast
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console  # noqa: E402
from pydantic_ai import ModelRetry  # noqa: E402
from pydantic_ai.messages import (ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart,  # noqa: E402
                                  UserPromptPart)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel  # noqa: E402

import agent  # noqa: E402
import toolset as tools_module  # noqa: E402

# The scripted turn every agent's tests replay (tests/scenario.json)
SCENARIO = json.loads((Path(__file__).resolve().parents[2] / "tests" / "scenario.json").read_text())
CALLS = [(step["tool"], step["arguments"]) for step in SCENARIO["calls"]]


async def scripted_model(messages, info: AgentInfo):
    """
    A stand-in model, scripted for two requests.
    The first request is answered with a call to every tool in CALLS, in one response; the
    next is answered with the text of the tool results it received.
    Args:
        messages: The conversation so far.
        info: pydantic-ai's information about the run (unused).
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
        console_patch = patch.object(agent, "console", Console(file=self.output))
        console_patch.start()
        self.addCleanup(console_patch.stop)

    async def run_tracked(self, parallel):
        """
        Run the scripted turn, recording how many scripts were running as each one started.
        Args:
            parallel: Run the tool calls concurrently instead of one at a time.
        Returns:
            tuple: The message history and the list of concurrent-script counts.
        """
        running, overlaps, lock = [0], [], threading.Lock()
        real_run_script = tools_module.run_script

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

        bot = agent.build_agent(FunctionModel(stream_function=scripted_model))
        with patch.object(tools_module, "run_script", tracked):
            history = await agent.ask(bot, SCENARIO["prompt"], [], trace=False, parallel=parallel)
        return history, overlaps

    async def test_parallel_flag_runs_tools_concurrently(self):
        _, overlaps = await self.run_tracked(parallel=True)
        self.assertGreater(max(overlaps), 1)

    async def test_tools_run_one_at_a_time_and_failures_reach_the_model(self):
        history, overlaps = await self.run_tracked(parallel=False)
        self.assertEqual(overlaps, [1, 1, 1])  # never two scripts at once (MCPAgent's server requires this)
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
            toolset = tools_module.load_toolset(Path(folder))
            self.assertEqual(list(toolset.tools), ["hello"])
            hello = tool_function(toolset, "hello")
            self.assertEqual(hello(who="world"), "hello world")
            with self.assertRaises(ModelRetry):
                hello(who=1)  # validated against the manifest's schema

    def test_model_profiles_come_from_the_shared_models_file(self):
        models = agent.load_models()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key-not-real"}):
            for name in ("LOCAL_LLM_BASE_URL", "LOCAL_LLM_MODEL", "LOCAL_LLM_API_KEY"):
                os.environ.pop(name, None)
            with patch.object(agent, "loaded_model", return_value=None):  # The server reports no loaded model
                local = agent.resolve_model(models)  # "default": "local"
            fallback = models["profiles"]["local"]  # Whatever the file names, so editing it never breaks this test
            self.assertEqual((local["base_url"], local["model"]), (fallback["base_url"], fallback["model"]))
            with patch.object(agent, "loaded_model", return_value="qwen/loaded-now") as asked:
                self.assertEqual(agent.resolve_model(models)["model"], "qwen/loaded-now")  # model_auto
                self.assertEqual(agent.resolve_model(models, model="explicit")["model"], "explicit")
            asked.assert_called_once_with("http://boba:1234/v1", "lm-studio")
            self.assertEqual(local["api_key"], "lm-studio")  # the OpenAI key is never used for another server
            os.environ["LOCAL_LLM_MODEL"] = "from-env"
            self.assertEqual(agent.resolve_model(models, "local")["model"], "from-env")
            self.assertEqual(agent.resolve_model(models, "local", model="from-cli")["model"], "from-cli")
            self.assertEqual(agent.resolve_model(models, "openai")["api_key"], "test-key-not-real")
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            with self.assertRaisesRegex(ValueError, "OPENAI_API_KEY"):
                agent.resolve_model(models, "openai")
        with self.assertRaisesRegex(ValueError, "Unknown model profile 'nope'"):
            agent.resolve_model(models, "nope")

    def test_the_memory_index_joins_the_instructions(self):
        with tempfile.TemporaryDirectory() as folder:
            index = Path(folder) / "index.md"
            self.assertIn("memory is empty", agent.memory_text(index))
            index.write_text("# Memory index\n\n- **preferences**: Prefers short answers (updated 2026-10-07)\n")
            text = agent.memory_text(index)
            self.assertIn("- preferences: Prefers short answers", text)
            self.assertEqual(agent.memory_text(None), "")

    def test_exit_saves_only_after_a_tool_call_or_several_exchanges(self):
        self.assertIn("Nothing to save", agent.load_instructions(key="on_exit"))
        self.assertTrue(json.loads(agent.AGENT_FILE.read_text())["save_on_exit"])
        ask = ModelRequest(parts=[UserPromptPart("hi")])
        answer = ModelResponse(parts=[TextPart("hello")])
        call = ModelResponse(parts=[ToolCallPart("time", {})])
        self.assertFalse(agent.worth_saving([]))
        self.assertFalse(agent.worth_saving([ask, answer]))
        self.assertTrue(agent.worth_saving([ask, answer, ask, answer]))
        self.assertTrue(agent.worth_saving([ask, call, answer]))

    def test_the_agent_is_named_dantic(self):
        self.assertEqual(json.loads(agent.AGENT_FILE.read_text())["names"]["pydantic"], "dantic")
        self.assertTrue(agent.identity_text("dantic").startswith("Your name is dantic."))
        self.assertEqual(agent.identity_text(None), "")

    def test_instructions_come_from_the_shared_context_file(self):
        instructions = agent.load_instructions()
        self.assertTrue(instructions.startswith("You are an agent"))
        self.assertIn("allowed folder", instructions)

    async def test_each_tool_call_prints_next_to_its_result(self):
        bot = agent.build_agent(FunctionModel(stream_function=scripted_model))
        await agent.ask(bot, SCENARIO["prompt"], [], trace=True)
        lines = [line[:1] + " " + line[2:].split("(")[0].split(":")[0]
                 for line in self.output.getvalue().splitlines() if line[:1] in "→←✗" and line]
        expected = [line for step in SCENARIO["calls"]
                    for line in (f"→ {step['tool']}", f"{'←' if step['outcome'] == 'ok' else '✗'} {step['tool']}")]
        self.assertEqual(lines, expected)

    def test_tools_are_told_which_agent_runs_them(self):
        self.assertEqual(tools_module.run_script("printenv", "AGENT_NAME"), "Pydantic Agent")

    def test_an_omitted_optional_argument_may_be_null(self):
        clock = tool_function(tools_module.toolset, "time")
        self.assertIn("(UTC+00:00)", clock(timezone="UTC"))
        self.assertEqual(clock(timezone=None)[:4], clock()[:4])  # null means omitted: local time

    async def test_history_carries_across_turns(self):
        async def remember(messages, info):
            yield f"{len(messages)} messages so far"
        bot = agent.build_agent(FunctionModel(stream_function=remember))
        history = await agent.ask(bot, "one", [], trace=False)
        history = await agent.ask(bot, "two", history, trace=False)
        self.assertEqual(history[-1].parts[0].content, "3 messages so far")


class OutputTests(unittest.TestCase):
    """The shared terminal layout (README.md, "Terminal output")."""

    def setUp(self):
        self.printed = io.StringIO()
        self.output = agent.Output(Console(file=self.printed), {"width": 30, "show_time": True})

    def test_lines_wrap_with_an_indent_and_keep_long_words_whole(self):
        url = "http://minion:8000/q/" + "x" * 40
        lines = agent.wrap("← pr_gate: the quiz is waiting and the merge is blocked " + url, 30)
        self.assertTrue(all(len(line) <= 30 for line in lines if line.strip() != url))
        self.assertTrue(all(line.startswith("  ") for line in lines[1:]))
        self.assertEqual(lines[-1], "  " + url)
        self.assertEqual(agent.wrap("short\nlines", 30), ["short", "lines"])

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
        self.assertRegex(self.printed.getvalue(), r"\n\nResponse time: \d+\.\ds\n$")

    def test_token_counts_follow_the_response_time(self):
        printed = io.StringIO()
        output = agent.Output(Console(file=printed), {"width": 120, "show_time": True, "show_tokens": True})
        output.add_usage(1200, 34)
        output.add_usage(1300, 56, requests=2)
        output.finish()
        self.assertRegex(printed.getvalue(), r"Response time: \d+\.\ds · tokens: 2,500 in, 90 out \(3 model calls\)\n$")
        printed.truncate(0), printed.seek(0)
        output.start()
        output.finish()
        self.assertIn("tokens: not reported", printed.getvalue())

    def test_links_become_clickable_and_stay_whole_while_streaming(self):
        self.assertEqual(agent.link_segments("see [PR #5](https://x/y) and http://a.b/c."),
                         [("see ", None), ("PR #5", "https://x/y"), (" and ", None),
                          ("http://a.b/c", "http://a.b/c"), (".", None)])
        printed = io.StringIO()
        output = agent.Output(Console(file=printed, force_terminal=True, width=120), {"width": 40, "links": True})
        answer = "Open [the pending quiz](http://minion:8000/q/abc) now."
        for i in range(0, len(answer), 4):
            output.text(answer[i:i + 4])
        output.end()
        self.assertEqual(printed.getvalue().count("\x1b]8;"), 2)  # One link: opened and closed
        self.assertIn("the pending quiz", printed.getvalue())
        self.assertNotIn("](", printed.getvalue())

    def test_layout_settings_come_from_the_shared_context_file(self):
        settings = agent.load_output_settings()
        self.assertEqual(settings["width"], 120)
        self.assertTrue(settings["show_time"])


if __name__ == "__main__":
    unittest.main()
