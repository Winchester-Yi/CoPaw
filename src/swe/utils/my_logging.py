# -*- coding: utf-8 -*-
"""Logging setup for SWE: asynchronous console output."""

import atexit
import io
import logging
import logging.handlers
import os
import platform
import queue
import sys
import threading
import traceback
from typing import TextIO

_ASYNC_LOG_QUEUE_MAXSIZE = 10000
_HIGH_PRIORITY_WAIT_SECONDS = 0.05
_SHUTDOWN_TIMEOUT_SECONDS = 2.0
_QUEUE_POLL_SECONDS = 0.2


_LEVEL_MAP = {
    "critical": logging.CRITICAL,
    "error": logging.ERROR,
    "warning": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
}

# Top-level name for this package; only loggers under this name are shown.
LOG_NAMESPACE = "swe"

_async_lock = threading.RLock()
_async_dispatcher: "_AsyncLogDispatcher | None" = None
_async_handler: "_AsyncQueueLogHandler | None" = None
_atexit_registered = False


def _enable_windows_ansi() -> None:
    """Enable ANSI escape code support on Windows 10+."""
    if platform.system() != "Windows":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        # STD_OUTPUT_HANDLE = -11, ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_ulong()  # pylint: disable=no-value-for-parameter
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


# Call once at import time
_enable_windows_ansi()


class ColorFormatter(logging.Formatter):
    COLORS = {
        logging.DEBUG: "\033[34m",
        logging.INFO: "\033[32m",
        logging.WARNING: "\033[33m",
        logging.ERROR: "\033[31m",
        logging.CRITICAL: "\033[41m\033[97m",
    }
    RESET = "\033[0m"

    @staticmethod
    def _escape_line_breaks(text: str) -> str:
        return text.replace("\r", r"\r").replace("\n", r"\n")

    def format(self, record):
        # Disable colors if output is not a terminal (e.g. piped/redirected)
        use_color = hasattr(sys.stderr, "isatty") and sys.stderr.isatty()
        color = self.COLORS.get(record.levelno, "") if use_color else ""
        reset = self.RESET if use_color else ""
        level = f"{color}{record.levelname}{reset}"

        full_path = record.pathname
        cwd = os.getcwd()
        # Use os.path for cross-platform path prefix stripping
        try:
            if os.path.commonpath([full_path, cwd]) == cwd:
                full_path = os.path.relpath(full_path, cwd)
        except ValueError:
            # Different drives on Windows (e.g., C: vs D:) are not comparable.
            pass

        prefix = f"{level} {full_path}:{record.lineno}"
        original_msg = self._escape_line_breaks(super().format(record))

        return f"{prefix} | {original_msg}"


class SuppressPathAccessLogFilter(logging.Filter):
    """
    Filter out uvicorn access log lines whose message contains any of the
    given path substrings. path_substrings: list of substrings; if any
    appears in the log message, the record is suppressed.
    Empty list = allow all.
    """

    def __init__(self, path_substrings: list[str]) -> None:
        super().__init__()
        self.path_substrings = path_substrings

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.path_substrings:
            return True
        try:
            msg = record.getMessage()
            return not any(s in msg for s in self.path_substrings)
        except Exception:
            return True


class _AsyncLogDispatcher:
    """Dispatch queued log records to output handlers in a background thread."""

    def __init__(
        self,
        log_queue: "queue.Queue[logging.LogRecord | None] | None" = None,
        target_handlers: list[logging.Handler] | None = None,
        high_priority_wait_seconds: float = _HIGH_PRIORITY_WAIT_SECONDS,
    ) -> None:
        self.log_queue = log_queue or queue.Queue(
            maxsize=_ASYNC_LOG_QUEUE_MAXSIZE,
        )
        self.target_handlers = list(target_handlers or [])
        self.high_priority_wait_seconds = high_priority_wait_seconds
        self._dropped_low_priority = 0
        self._drop_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the background dispatcher thread once."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="swe-async-logging",
            daemon=True,
        )
        self._thread.start()

    def enqueue(self, record: logging.LogRecord) -> None:
        """Enqueue a record using severity-aware backpressure rules."""
        if record.levelno < logging.WARNING:
            try:
                self.log_queue.put_nowait(record)
            except queue.Full:
                self._increment_dropped_low_priority()
            return

        try:
            self.log_queue.put(
                record,
                block=True,
                timeout=self.high_priority_wait_seconds,
            )
        except queue.Full:
            self._direct_emit(record)

    def stop(self, timeout: float = _SHUTDOWN_TIMEOUT_SECONDS) -> None:
        """Drain briefly and stop the dispatcher thread."""
        thread = self._thread
        if thread is None:
            self.emit_drop_summary()
            return
        try:
            self.log_queue.put_nowait(None)
        except queue.Full:
            self._stop_event.set()
        thread.join(timeout=timeout)
        self.emit_drop_summary()

    def emit_drop_summary(self) -> None:
        """Directly emit a summary for dropped debug/info records."""
        with self._drop_lock:
            count = self._dropped_low_priority
            self._dropped_low_priority = 0
        if count <= 0:
            return

        record = logging.LogRecord(
            name=f"{LOG_NAMESPACE}.logging",
            level=logging.WARNING,
            pathname=__file__,
            lineno=0,
            msg=(
                "dropped %d debug/info log records because async "
                "logging queue was full"
            ),
            args=(count,),
            exc_info=None,
        )
        self._direct_emit(record)

    def _run(self) -> None:
        while not self._stop_event.is_set() or not self.log_queue.empty():
            try:
                record = self.log_queue.get(timeout=_QUEUE_POLL_SECONDS)
            except queue.Empty:
                self.emit_drop_summary()
                continue
            try:
                if record is None:
                    break
                self._direct_emit(record)
            finally:
                try:
                    self.log_queue.task_done()
                except ValueError:
                    pass
                self.emit_drop_summary()
        self.emit_drop_summary()

    def _increment_dropped_low_priority(self) -> None:
        with self._drop_lock:
            self._dropped_low_priority += 1

    def _direct_emit(self, record: logging.LogRecord) -> None:
        for handler in self.target_handlers:
            try:
                handler.handle(record)
            except Exception:
                self._minimal_stderr_fallback(record)

    @staticmethod
    def _minimal_stderr_fallback(record: logging.LogRecord) -> None:
        try:
            sys.stderr.write(
                f"{record.levelname} {record.name}: {record.getMessage()}\n",
            )
            sys.stderr.flush()
        except Exception:
            pass


class _AsyncQueueLogHandler(logging.Handler):
    """Logging handler that delegates records to the async dispatcher."""

    def __init__(self, dispatcher: _AsyncLogDispatcher) -> None:
        super().__init__(level=logging.NOTSET)
        self.dispatcher = dispatcher

    def emit(self, record: logging.LogRecord) -> None:
        self.dispatcher.enqueue(record)


class _Utf8StderrStream(io.TextIOBase):
    """UTF-8 text wrapper that never closes the underlying stderr buffer."""

    encoding = "utf-8"
    errors = "replace"

    def __init__(self, text_stream: TextIO) -> None:
        self._text_stream = text_stream
        self._buffer = getattr(text_stream, "buffer", None)

    def write(self, text: str) -> int:
        if self._buffer is None:
            return self._text_stream.write(text)
        data = text.encode(self.encoding, errors=self.errors)
        self._buffer.write(data)
        if getattr(self, "line_buffering", False) and "\n" in text:
            self.flush()
        return len(text)

    def flush(self) -> None:
        if self._buffer is not None:
            self._buffer.flush()
            return
        self._text_stream.flush()

    def isatty(self) -> bool:
        return bool(
            hasattr(self._text_stream, "isatty")
            and self._text_stream.isatty(),
        )

    @property
    def line_buffering(self) -> bool:
        return bool(getattr(self._text_stream, "line_buffering", False))

    def close(self) -> None:
        self.flush()
        super().close()


class _SingleLineErrorStreamHandler(logging.StreamHandler):
    def handleError(self, record: logging.LogRecord) -> None:
        if not logging.raiseExceptions or sys.stderr is None:
            return
        try:
            details = "".join(traceback.format_exception(*sys.exc_info()))
            details += f"Message: {record.msg!r}\nArguments: {record.args!r}"
            details = ColorFormatter._escape_line_breaks(details)
            sys.stderr.write(f"--- Logging error --- | {details}\n")
        except Exception:
            pass


def _create_stderr_handler(formatter: logging.Formatter) -> logging.Handler:
    handler = _SingleLineErrorStreamHandler(_Utf8StderrStream(sys.stderr))
    handler.setFormatter(formatter)
    return handler


def setup_logger(level: int | str = logging.INFO):
    """Configure logging to only output from this package (swe), not deps."""
    global _async_dispatcher, _async_handler, _atexit_registered

    log_format = "%(asctime)s | %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    if isinstance(level, str):
        level = _LEVEL_MAP.get(level.lower(), logging.INFO)

    formatter = ColorFormatter(log_format, datefmt)

    # Suppress third-party: set root logger level and configure handlers.
    root = logging.getLogger()
    for handler in root.handlers:
        if isinstance(
            handler,
            (logging.FileHandler, logging.handlers.RotatingFileHandler),
        ):
            handler.setLevel(logging.INFO)
        else:
            handler.setLevel(logging.WARNING)

    # Only attach handler to our namespace so only swe.* logs are printed.
    logger = logging.getLogger(LOG_NAMESPACE)
    logger.setLevel(level)
    logger.propagate = False
    with _async_lock:
        if _async_dispatcher is None or _async_handler is None:
            target_handler = _create_stderr_handler(formatter)
            _async_dispatcher = _AsyncLogDispatcher(
                target_handlers=[target_handler],
            )
            _async_dispatcher.start()
            _async_handler = _AsyncQueueLogHandler(_async_dispatcher)

        for target in _async_dispatcher.target_handlers:
            target.setFormatter(formatter)

        for existing in list(logger.handlers):
            if existing is not _async_handler:
                logger.removeHandler(existing)
                existing.close()
        if _async_handler not in logger.handlers:
            logger.addHandler(_async_handler)

        if not _atexit_registered:
            atexit.register(shutdown_logger)
            _atexit_registered = True

    return logger


def shutdown_logger(timeout: float = _SHUTDOWN_TIMEOUT_SECONDS) -> None:
    """Stop the async logging dispatcher after a short best-effort drain."""
    global _async_dispatcher, _async_handler

    with _async_lock:
        dispatcher = _async_dispatcher
        handler = _async_handler
        _async_dispatcher = None
        _async_handler = None

    logger = logging.getLogger(LOG_NAMESPACE)
    if handler is not None and handler in logger.handlers:
        logger.removeHandler(handler)
        handler.close()
    if dispatcher is not None:
        dispatcher.stop(timeout=timeout)
