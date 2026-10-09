"""
Module: output.py

Description:
    `Output`: the terminal layout shared by the three agents (README.md, "Terminal output"): the
    spinner or the gray lines, the streamed answer word-wrapped to the width, clickable links,
    and the timing line; with `wrap`, `link_segments` and `readable`, the text helpers it uses.
"""
import json
import re
import time
from typing import Optional

from rich.console import Console
from rich.status import Status
from rich.style import Style
from rich.text import Text

# A Markdown link, [text](url), or a bare web address: shown as a clickable OSC 8 link, in LINK_COLOR
# This terminal behavior is intentionally repeated in the independent agents.
LINK_COLOR = "bright_cyan"
LINK = re.compile(r"\[([^]\n]+)]\((https?://[^\s)]+)\)|(https?://[^\s<>()\[\]\"'`]+)")
OPEN_LINK = re.compile(r"\[[^]\n]*$|]\([^)\s]*$")  # A Markdown link that is not finished yet


class Output:
    """
    The terminal layout shared by the three agents (README.md, "Terminal output"):
      - A spinner runs while the model thinks or a tool runs ("Running shell…"); while a reasoning
        model's thinking streams, it says for how long ("Thinking… 42s"). By default, only the
        answer and the timing line are printed.
      - With debug, everything except the model's answer (banner, hints, tool calls and results,
        timing) is also a dark gray line; the spinner runs between them.
      - The model's streamed answer is word-wrapped as it arrives, with exactly one blank line
        before and after it.
      - All output fits in the configured width (context/output.json), or the terminal's if narrower.
      - Each response ends with how long it took.
    """

    def __init__(self, out: Console, settings: dict | None = None, debug: bool = True):
        """
        Args:
            out: The console to print to.
            settings: The layout settings (context/output.json, from `AgentContext.output_settings`);
                None uses the defaults.
        """
        settings = settings or {}
        self.out = out
        self.width = int(settings.get("width", 120))
        if out.is_terminal:
            self.width = min(self.width, out.width)
        self.show_time = bool(settings.get("show_time", True))
        self.show_tokens = bool(settings.get("show_tokens", False))
        self.links = bool(settings.get("links", False))  # OSC 8 links (rich adds them only on a terminal)
        self.usage: dict[str, int] | None = None  # Tokens and model calls of this response, when the server reports them
        self.in_text = False  # A text block is open
        self.pending = ""  # Trailing newlines held back until more text follows
        self.column = 0  # Where the streamed answer's current line ends
        self.word = ""  # The streamed word being collected
        self.spaces = ""  # The spaces before it
        self.started = time.monotonic()
        self.debug = debug  # Print the gray lines; without it, a spinner shows the activity instead
        self.spinner: Optional[Status] = None
        self.blank_owed = False  # The last text ended without its blank line after it
        self.thinking_since: Optional[float] = None  # When the current stretch of thinking began
        self.thinking_shown = -1  # The seconds the spinner shows for it

    def start(self) -> None:
        """Start timing a response, and the spinner (on a terminal only)."""
        self.started = time.monotonic()
        self.usage = None
        self.spin("Thinking…")

    def spin(self, label: str) -> None:
        """
        Show a label on the spinner, starting it if needed; on a terminal only (piped output has none).
        A new label also ends a stretch of thinking (see `thinking`).
        Args:
            label: What the agent is doing, e.g. "Thinking…".
        """
        self.thinking_since, self.thinking_shown = None, -1
        self._label(label)

    def thinking(self) -> None:
        """
        A chunk of the model's thinking arrived: show on the spinner how long it has been thinking,
        so a reasoning model's long silence is visibly work, not a hang. Ignored once the answer streams.
        """
        if self.in_text:
            return
        now = time.monotonic()
        if self.thinking_since is None:
            self.thinking_since = now
        seconds = int(now - self.thinking_since)
        if seconds != self.thinking_shown:
            self.thinking_shown = seconds
            self._label(f"Thinking… {seconds}s" if seconds else "Thinking…")

    def _label(self, label: str) -> None:
        """
        Show a label on the spinner, starting it if needed.
        Args:
            label: The label.
        """
        if not self.out.is_terminal:
            return
        if self.spinner is None:
            spinner = self.out.status(Text(label, style="bright_black"), spinner="dots",
                                      spinner_style="bright_black")
            self.spinner = spinner
            spinner.start()
        else:
            self.spinner.update(Text(label, style="bright_black"))

    def stop_spinner(self) -> None:
        """Stop the spinner, if it runs; it leaves nothing on the screen."""
        if self.spinner is not None:
            self.spinner.stop()
            self.spinner = None

    def add_usage(self, input_tokens: int, output_tokens: int, requests: int = 1) -> None:
        """
        Count the tokens of model calls made for this response.
        Args:
            input_tokens: Tokens sent to the model (prompt, history, tool results).
            output_tokens: Tokens the model generated.
            requests: How many model calls these tokens cover.
        """
        usage = self.usage or {"input": 0, "output": 0, "requests": 0}
        self.usage = usage
        usage["input"] += input_tokens
        usage["output"] += output_tokens
        usage["requests"] += requests

    def text(self, chunk: str) -> None:
        """
        Print a chunk of streamed model text.
        Args:
            chunk: The text delta.
        """
        if not self.in_text:
            chunk = chunk.lstrip("\n")
            if not chunk:
                return
            self.stop_spinner()
            self.out.print()  # Blank line before the text
            self.blank_owed = False
            self.in_text = True
        body = chunk.rstrip("\n")
        if body:
            self.out.print(self._render(self._wrap_stream(self.pending + body)), end="", soft_wrap=True)
            self.pending = chunk[len(body):]
        else:
            self.pending += chunk

    def line(self, text: str) -> None:
        """
        Print a whole dark gray line (a tool call or result, the banner) in debug mode, then spin on
        while the tool or the model works; otherwise only show the activity on the spinner:
        "Running <tool>…" for a call, "Thinking…" after it.
        Args:
            text: The line; may contain newlines.
        """
        if not self.debug:
            if text.startswith("→ "):
                self.end(blank=False)  # A call after some answer text: end its line, and spin again
                self.spin(f"Running {text[2:].split('(')[0]}…")
            elif self.spinner is not None and text.startswith(("← ", "✗ ")):
                self.spin("Thinking…")
            return
        self.note(text)
        if text.startswith("→ "):  # The tool runs now (MCPAgent prints the call before running it)
            self.spin(f"Running {text[2:].split('(')[0]}…")
        elif text.startswith(("← ", "✗ ")):  # A result goes back to the model, which works on it
            self.spin("Thinking…")

    def note(self, text: str) -> None:
        """
        Print a whole dark gray line, also when not in debug mode (the timing line, replies to
        commands), wrapped to the width, closing any open text block first.
        Args:
            text: The line; may contain newlines.
        """
        self.stop_spinner()
        self.end()
        if self.blank_owed:  # Text ended without its blank line (a tool call followed it)
            self.out.print()
            self.blank_owed = False
        for line in self.wrap(text, self.width):
            self.out.print(self._render(line), style="bright_black", soft_wrap=True)

    def end(self, blank: bool = True) -> None:
        """
        Close the open text block, if any: end its line and add the blank line after it.
        Args:
            blank: Add the blank line now; False leaves it to what follows (more text adds its own,
                and a gray line adds one first), so text around a hidden tool call has only one.
        """
        if self.in_text:
            self.out.print(self._render(self._take_word()), end="", soft_wrap=True)
            self.out.print("\n" if blank else "")
            self.blank_owed = not blank
        self.in_text = False
        self.pending = ""
        self.column = 0
        self.word = self.spaces = ""

    def finish(self) -> None:
        """
        Close the response: the line with how long it took since `start` and the tokens it used goes
        right under the answer, and a blank line after it sets the response off from the next prompt.
        """
        self.stop_spinner()
        self.end(blank=False)
        self.blank_owed = False  # The timing line belongs to the answer: no blank line between them
        parts = [f"Response time: {time.monotonic() - self.started:.1f}s"] if self.show_time else []
        if self.show_tokens:
            if self.usage:
                calls = self.usage["requests"]
                parts.append(f"tokens: {self.usage['input']:,} in, {self.usage['output']:,} out")
                parts.append(f"{calls} model call{'s' if calls != 1 else ''}")
            else:
                parts.append("tokens: not reported")
        if parts:
            self.note(" · ".join(parts))
        self.out.print()

    def _render(self, text: str) -> Text:
        """
        Turn text into a rich Text, with its links as clickable OSC 8 links when links are on, in
        LINK_COLOR: the one vivid color, in the gray lines too, so a link such as the quiz's stands out.
        Args:
            text: Plain text, possibly with Markdown links or web addresses.
        Returns:
            Text: The text to print; rich writes the link codes only on a terminal.
        """
        if not self.links:
            return Text(text)
        rendered = Text()
        for part, url in self.link_segments(text):
            rendered.append(part, style=Style(link=url, color=LINK_COLOR) if url else None)
        return rendered

    def _wrap_stream(self, text: str) -> str:
        """
        Word-wrap streamed text: words are held until they end, so they can move to the next line.
        Args:
            text: The text to add.
        Returns:
            str: What can be printed now.
        """
        printed = []
        for char in text:
            if char == "\n":
                printed.append(self._take_word() + "\n")
                self.column = 0
                self.spaces = ""
            elif char == " " and self.links and OPEN_LINK.search(self.word) and len(self.word) < 300:
                self.word += char  # Inside [text](url): keep the link together
            elif char == " ":
                printed.append(self._take_word())
                self.spaces += " "
            else:
                self.word += char
        return "".join(printed)

    def _take_word(self) -> str:
        """
        Place the collected word on the current line, or on the next one if it does not fit.
        Returns:
            str: The word with what goes before it (its spaces, or a line break).
        """
        if not self.word:
            return ""
        length = sum(len(part) for part, _ in self.link_segments(self.word)) if self.links else len(self.word)
        if self.column and self.column + len(self.spaces) + length > self.width:
            placed = "\n" + self.word
            self.column = length
        else:
            placed = self.spaces + self.word
            self.column += len(self.spaces) + length
        self.word = self.spaces = ""
        return placed

    @staticmethod
    def wrap(text: str, width: int, indent: str = "  ") -> list[str]:
        """
        Word-wrap text to a width, the same way in all three agents. Each line wraps on its own;
        continuation lines start with `indent`. A word longer than the width (e.g. a URL) is kept whole.
        Args:
            text: The text; may contain newlines.
            width: The column to wrap at.
            indent: Prefix for continuation lines.
        Returns:
            list[str]: The wrapped lines.
        """
        lines = []
        for raw in text.split("\n"):
            words = raw.split(" ")
            line = words[0]
            for word in words[1:]:
                if line.strip() and len(line) + 1 + len(word) > width:
                    lines.append(line)
                    line = indent + word
                else:
                    line += " " + word
            lines.append(line)
        return lines

    # noinspection DuplicatedCode
    @staticmethod
    def link_segments(text: str) -> list[tuple[str, Optional[str]]]:
        """
        Split text into plain parts and links, the same way in all three agents.
        Args:
            text: The text.
        Returns:
            list[tuple[str, Optional[str]]]: (shown text, URL or None) pairs. A Markdown link shows only
                its text; a bare address shows itself, without trailing punctuation.
        """
        segments, position = [], 0
        for match in LINK.finditer(text):
            shown, url = (match.group(1), match.group(2)) if match.group(1) else (match.group(3), match.group(3))
            trailing = ""
            if not match.group(1):
                stripped = url.rstrip(".,;:!?")
                trailing, url, shown = url[len(stripped):], stripped, stripped
            segments += [(text[position:match.start()], None), (shown, url), (trailing, None)]
            position = match.end()
        segments.append((text[position:], None))
        return [(part, url) for part, url in segments if part]

    # noinspection DuplicatedCode
    @staticmethod
    def readable(output: str) -> str:
        """
        Make a tool's output readable for the terminal.
        Args:
            output: The output as sent to the model: plain text, or JSON such as MCPAgent's
                {"status", "logs", "summary"} result or an {"error": ...} failure.
        Returns:
            str: The "logs" lines or the error message when the output is such JSON, else the text.
        """
        try:
            data = json.loads(output)
        except (TypeError, ValueError):
            return output
        if isinstance(data, dict) and "logs" in data:
            return "\n".join(str(line) for line in data["logs"])
        if isinstance(data, dict) and "error" in data:
            return Output.readable(str(data["error"]))
        return output
