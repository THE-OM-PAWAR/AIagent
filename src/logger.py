"""Structured logging setup and utility functions."""

import logging
import sys


def setup_logging(log_level: str = "INFO") -> None:
    """Configure structured console logging across the application.

    Args:
        log_level: Desired log level string (DEBUG, INFO, WARNING, ERROR, CRITICAL).
    """
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)

    log_format = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    formatter = logging.Formatter(fmt=log_format, datefmt=date_format)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)

    # Avoid duplicate handlers if setup is called multiple times
    root_logger.handlers.clear()
    root_logger.addHandler(handler)

    # Suppress verbose third-party loggers unless running at DEBUG
    if numeric_level > logging.DEBUG:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("telegram").setLevel(logging.INFO)


def get_logger(name: str) -> logging.Logger:
    """Obtain a logger instance for a given module name."""
    return logging.getLogger(name)

