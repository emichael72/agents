"""
Module: errors.py

Description:
    `ExceptionReport`: what the command-line entry points print when an exception ends the
    program: the error, the file and line where it was raised, and the invocation.
"""
import os
import sys

from colorama import Fore, Style


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
        traceback = error.__traceback__
        if traceback is not None:
            while traceback.tb_next is not None:  # The innermost frame raised it
                traceback = traceback.tb_next
            self.file_name = os.path.basename(traceback.tb_frame.f_code.co_filename)
            self.line_number = traceback.tb_lineno

    def print(self) -> None:
        """
        Print the error, its origin and the command line that was run.
        """
        print(f"\n{Fore.RED}Exception:{Style.RESET_ALL} {self.error}.\n"
              f"File: {self.file_name}\nLine: {self.line_number}")
        print(f"Invocation: {' '.join(sys.argv)}\n")
