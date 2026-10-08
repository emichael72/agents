"""
Module: errors.py

Description:
    `ExceptionReport`: what the command-line entry points print when an exception ends the
    program: the error, the file and line where it was raised, and the invocation.
"""
import os
import sys
from types import TracebackType

from rich.console import Console
from rich.text import Text


class ExceptionReport:
    """
    The origin of an exception: the file and line of the innermost frame of its traceback.
    """

    def __init__(self, error: BaseException) -> None:
        """
        Find where an exception was raised.
        Args:
            error: The exception, with its traceback.
        """
        self.error = error
        self.file_name = "<unknown>"
        self.line_number = -1
        traceback: TracebackType | None = error.__traceback__
        while traceback is not None:  # Keep the origin of the innermost frame
            self.file_name = os.path.basename(traceback.tb_frame.f_code.co_filename)
            self.line_number = traceback.tb_lineno
            traceback = traceback.tb_next

    def print(self) -> None:
        """
        Print the error, its origin and the command line that was run.
        """
        console = Console(highlight=False, soft_wrap=True)  # Long lines are not re-wrapped
        console.print()
        console.print(Text.assemble(("Exception:", "red"), f" {self.error}."))
        console.print(f"File: {self.file_name}\nLine: {self.line_number}", markup=False)
        console.print(f"Invocation: {' '.join(sys.argv)}", markup=False)
        console.print()
