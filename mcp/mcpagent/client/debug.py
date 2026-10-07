"""
Module: debug.py

Description:
    `DebugGuru`, which pretty-prints the client's JSON-RPC traffic in Rich panels for debugging
    (the client config's "debug_json_box").
"""
import json
from contextlib import suppress
from typing import Any, Optional

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.pretty import Pretty
from rich.text import Text


class DebugGuru:
    """
    Utility class for rendering debug information with Rich.
    Provides methods for displaying structured data in styled panels,
    automatically truncating long strings, and managing console output.
    """

    def __init__(self, console: Optional[Console] = None) -> None:
        """
        Initialize a DebugGuru instance.
        Args:
            console (Optional[Console], optional):
                A Rich Console instance to render into. If None, a new Console
                is created with `force_terminal=True`.
        """
        self._console = console or Console(force_terminal=True)

    def show_box(self,
                 title: str,
                 debug_data: object,
                 adjust_content: bool = True,
                 show_border: bool = False,
                 paint_background: bool = False) -> None:
        """
        Render debug information in a styled Rich panel.

        Args:
            title (str): Title text for the panel header.
            debug_data (object): Data to render (e.g., string, dict, list).
            adjust_content (bool, optional): Truncate long strings to fit terminal width. Defaults to True.
            show_border (bool, optional): Show a border around the panel. Defaults to False.
            paint_background (bool, optional): Apply a gray background behind the content. Defaults to False.
        """
        console_width = self._console.width
        max_str_len = max(20, console_width - 10)

        def _maybe_json(text: str) -> Any:
            """Try to parse a string as JSON; return original if not valid."""
            with suppress(Exception):
                return json.loads(text)
            return text

        def _process_content(_data: Any, _max_len: int) -> Any:
            """Recursively truncate and decode JSON strings inside the data."""
            if isinstance(_data, str):
                parsed = _maybe_json(_data.strip())
                if parsed is not _data:
                    return _process_content(parsed, _max_len)  # process decoded JSON
                return _data if len(_data) <= _max_len else _data[:_max_len] + "…"
            elif isinstance(_data, dict):
                return {k: _process_content(v, _max_len) for k, v in _data.items()}
            elif isinstance(_data, list):
                return [_process_content(v, _max_len) for v in _data]
            elif isinstance(_data, tuple):
                return tuple(_process_content(v, _max_len) for v in _data)
            return _data

        try:

            safe_data = (
                _process_content(debug_data, max_str_len)
                if adjust_content
                else debug_data
            )

            # Build renderable
            style = "dim on grey15" if paint_background else "dim"

            if safe_data in ("", None, {}, []):
                content = Text("<empty>", style=style)
            else:
                content = Pretty(safe_data, expand_all=True)

            if show_border:
                renderable = Panel(
                    content,
                    title=f"[white]DEBUG: {title}[/white]",
                    border_style="bright_black",
                    box=box.DOUBLE,
                    title_align="left",
                    padding=(0, 1),
                    expand=True,
                    style=style)
            else:
                # Wrap in Panel
                renderable = (
                    Panel(content, box=box.MINIMAL, padding=0, style=style)
                    if paint_background
                    else content)

            # Print
            self._console.print(renderable, width=console_width)
            self._console.print()

        except Exception as render_error:
            self._console.print(f"DebugGuru Error: {render_error}")
