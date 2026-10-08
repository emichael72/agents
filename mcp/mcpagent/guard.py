"""
Module: guard.py

Description:
    `RepeatGuard`: stops a model that repeats itself. A model can get stuck calling the same tool with
    the same arguments, for example checking a status that only a person can change; each call costs a
    model call and changes nothing. The guard counts identical calls in a row within one user turn,
    and refuses the call after max_repeated_calls (context/agent.json), telling the model to answer
    instead. The same rule is in all three agents.
"""
import json
from typing import Any, Optional


class RepeatGuard:
    """
    Counts identical tool calls in a row, within one user turn.
    """

    def __init__(self, limit: int = 0) -> None:
        """
        Args:
            limit: The most times in a row the same call may run; 0 means no limit.
        """
        self.limit = limit
        self.last: Optional[tuple[str, str]] = None
        self.count = 0

    def reset(self) -> None:
        """Start a new user turn: earlier calls no longer count."""
        self.last, self.count = None, 0

    def refuse(self, name: str, arguments: dict[str, Any]) -> Optional[str]:
        """
        Record a call, and say whether it must not run.
        Args:
            name: The tool.
            arguments: The model's arguments.
        Returns:
            Optional[str]: The message for the model when the call repeats the previous ones more than
                the limit allows; None when it may run.
        """
        key = (name, json.dumps(arguments, sort_keys=True))
        self.count = self.count + 1 if key == self.last else 1
        self.last = key
        if self.limit and self.count > self.limit:
            return (f"Not run: you made this same {name} call, with the same arguments, {self.limit} times in a "
                    f"row, and its result will not change. Do not call it again: answer the user now.")
        return None
