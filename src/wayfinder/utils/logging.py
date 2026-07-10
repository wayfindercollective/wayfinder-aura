"""
Logging infrastructure for Wayfinder Aura.

Provides structured logging with appropriate levels, file rotation,
and XDG-compliant log file locations.

Usage:
    from wayfinder.utils.logging import get_logger
    
    logger = get_logger(__name__)
    logger.info("Application started")
    logger.debug("Debug info: %s", data)
    logger.error("Something went wrong", exc_info=True)
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from wayfinder.utils.fs_security import (
    ensure_private_dir,
    owner_only_opener,
    restrict_owner_only,
)


class OwnerOnlyRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler that creates and keeps log files owner-only (0600)."""

    def _open(self):
        # Create with 0600 at open time (not create-then-chmod race).
        # Mirror FileHandler._open kwargs + opener=.
        stream = open(
            self.baseFilename,
            self.mode,
            encoding=self.encoding,
            errors=self.errors,
            opener=owner_only_opener,
        )
        restrict_owner_only(self.baseFilename)
        return stream

    def doRollover(self):
        super().doRollover()
        # Rollover renames the current file and opens a new one; re-assert modes.
        try:
            restrict_owner_only(self.baseFilename)
            # Rotated sibling: wayfinder.log.1 etc.
            for i in range(1, (self.backupCount or 0) + 1):
                rotated = f"{self.baseFilename}.{i}"
                if os.path.exists(rotated):
                    restrict_owner_only(rotated)
        except OSError:
            pass


# XDG Base Directory Specification
def _get_log_dir() -> Path:
    """Get the log directory following XDG spec (owner-only 0700)."""
    cache_home = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    log_dir = cache_home / "wayfinder-aura" / "logs"
    return ensure_private_dir(log_dir)


# Default configuration
DEFAULT_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_LOG_LEVEL = logging.INFO
MAX_LOG_SIZE = 5 * 1024 * 1024  # 5 MB
BACKUP_COUNT = 3

# Module-level logger cache
_loggers: dict = {}
_configured = False


def configure_logging(
    level: int = DEFAULT_LOG_LEVEL,
    log_to_file: bool = True,
    log_to_console: bool = True,
    log_format: str = DEFAULT_LOG_FORMAT,
    date_format: str = DEFAULT_DATE_FORMAT,
) -> None:
    """
    Configure the logging system.
    
    Should be called once at application startup.
    
    Args:
        level: Logging level (logging.DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_to_file: Whether to log to file
        log_to_console: Whether to log to console (stderr)
        log_format: Log message format string
        date_format: Date/time format string
    """
    global _configured
    
    # Get root logger for wayfinder
    root_logger = logging.getLogger("wayfinder")
    root_logger.setLevel(level)
    
    # Clear existing handlers
    root_logger.handlers.clear()
    
    formatter = logging.Formatter(log_format, datefmt=date_format)
    
    if log_to_console:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(level)
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)
    
    if log_to_file:
        try:
            log_dir = _get_log_dir()
            log_file = log_dir / "wayfinder.log"
            
            file_handler = OwnerOnlyRotatingFileHandler(
                log_file,
                maxBytes=MAX_LOG_SIZE,
                backupCount=BACKUP_COUNT,
                encoding="utf-8",
            )
            restrict_owner_only(log_file)
            file_handler.setLevel(level)
            file_handler.setFormatter(formatter)
            root_logger.addHandler(file_handler)
        except Exception as e:
            # If file logging fails, log to console only
            if log_to_console:
                root_logger.warning(f"Could not set up file logging: {e}")
    
    _configured = True


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance for the given module name.
    
    Args:
        name: Module name (usually __name__)
    
    Returns:
        Logger instance
    
    Example:
        logger = get_logger(__name__)
        logger.info("Hello world")
    """
    global _configured
    
    # Auto-configure if not done yet
    if not _configured:
        configure_logging()
    
    # Ensure name is under wayfinder namespace
    if not name.startswith("wayfinder"):
        name = f"wayfinder.{name}"
    
    if name not in _loggers:
        _loggers[name] = logging.getLogger(name)
    
    return _loggers[name]


def set_level(level: int) -> None:
    """
    Change the logging level for all wayfinder loggers.
    
    Args:
        level: New logging level
    """
    root_logger = logging.getLogger("wayfinder")
    root_logger.setLevel(level)
    for handler in root_logger.handlers:
        handler.setLevel(level)


def get_log_file_path() -> Optional[Path]:
    """
    Get the path to the current log file.
    
    Returns:
        Path to log file, or None if file logging is disabled
    """
    try:
        return _get_log_dir() / "wayfinder.log"
    except Exception:
        return None


# Convenience functions for quick logging without getting a logger
def debug(msg: str, *args, **kwargs) -> None:
    """Log a debug message."""
    get_logger("wayfinder").debug(msg, *args, **kwargs)


def info(msg: str, *args, **kwargs) -> None:
    """Log an info message."""
    get_logger("wayfinder").info(msg, *args, **kwargs)


def warning(msg: str, *args, **kwargs) -> None:
    """Log a warning message."""
    get_logger("wayfinder").warning(msg, *args, **kwargs)


def error(msg: str, *args, **kwargs) -> None:
    """Log an error message."""
    get_logger("wayfinder").error(msg, *args, **kwargs)


def exception(msg: str, *args, **kwargs) -> None:
    """Log an exception with traceback."""
    get_logger("wayfinder").exception(msg, *args, **kwargs)
