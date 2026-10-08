"""
Module: logger.py

Description:
    `MCPAgentLogger`: the logger of the client and the server, a minimal `logging.Logger` subclass
    with a consistent console format.
"""

import logging
import sys


class MCPAgentLogger(logging.Logger):
    """
    As simple as it gets: logger that inherits from logging.Logger
    """

    # noinspection SpellCheckingInspection
    _log_format: str = '[%(asctime)s %(levelname)-8s] %(name)-14s: %(message)s'
    _date_format: str = '%d-%m %H:%M:%S'

    def __init__(self, name: str = "MCPAgent", level: int = logging.INFO):
        """
        Initialize logger with specified name and level.
        Args:
            name: Logger name (defaults to "MCPAgent")
            level: Logging level (defaults to INFO)
        """
        super().__init__(name, level)
        if not self.handlers:
            handler = logging.StreamHandler(sys.stdout)
            formatter = logging.Formatter(self._log_format, self._date_format)
            handler.setFormatter(formatter)
            self.addHandler(handler)
