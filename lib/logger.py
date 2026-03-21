"""Shared logging setup for all cratedigger scripts."""

import logging
import sys
from pathlib import Path


def setup_logger(name: str, log_file: Path | None = None) -> logging.Logger:
    """Return a logger with console + optional file handlers.

    Format: ``YYYY-MM-DD HH:MM:SS LEVEL message``
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    logger.addHandler(console)

    if log_file is not None:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    return logger
