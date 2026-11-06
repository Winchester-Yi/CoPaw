# -*- coding: utf-8 -*-
import io
import logging
import queue
import sys

from swe.utils import my_logging
from swe.utils.my_logging import ColorFormatter, setup_logger


def test_color_formatter_escapes_multiline_messages() -> None:
    formatter = ColorFormatter(
        "%(asctime)s | %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )
    record = logging.LogRecord(
        name="swe.test",
        level=logging.INFO,
        pathname="/tmp/example.py",
        lineno=12,
        msg="first line\nsecond line\r\nthird line",
        args=(),
        exc_info=None,
    )

    formatted = formatter.format(record)

    assert "\n" not in formatted
    assert "\r" not in formatted
    assert r"first line\nsecond line\r\nthird line" in formatted


def test_handler_error_is_single_line(monkeypatch, capsys) -> None:
    monkeypatch.setattr(logging, "raiseExceptions", True)
    handler = my_logging._create_stderr_handler(
        logging.Formatter("%(message)s"),
    )
    record = logging.LogRecord(
        "swe.test",
        logging.ERROR,
        __file__,
        1,
        "value: %d",
        ("bad",),
        None,
    )

    handler.handle(record)

    output = capsys.readouterr().err
    assert len(output.splitlines()) == 1
    assert "Logging error" in output
    assert "TypeError" in output
    assert r"\n" in output


def test_setup_logger_installs_one_async_handler() -> None:
    my_logging.shutdown_logger()

    logger = setup_logger("info")
    first_handlers = list(logger.handlers)
    setup_logger("debug")

    assert len(logger.handlers) == 1
    assert logger.handlers == first_handlers
    assert isinstance(logger.handlers[0], my_logging._AsyncQueueLogHandler)

    my_logging.shutdown_logger()


def test_debug_info_records_drop_when_async_queue_is_full() -> None:
    stream = io.StringIO()
    target = logging.StreamHandler(stream)
    target.setFormatter(logging.Formatter("%(levelname)s:%(message)s"))
    log_queue: queue.Queue[logging.LogRecord | None] = queue.Queue(maxsize=1)
    dispatcher = my_logging._AsyncLogDispatcher(log_queue, [target])
    handler = my_logging._AsyncQueueLogHandler(dispatcher)

    log_queue.put_nowait(
        logging.LogRecord(
            "swe.test",
            logging.INFO,
            __file__,
            1,
            "queued",
            (),
            None,
        ),
    )

    handler.emit(
        logging.LogRecord(
            "swe.test",
            logging.INFO,
            __file__,
            2,
            "dropped",
            (),
            None,
        ),
    )
    dispatcher.emit_drop_summary()

    assert "dropped 1 debug/info log records" in stream.getvalue()


def test_warning_records_fall_back_to_stderr_when_async_queue_is_full() -> (
    None
):
    stream = io.StringIO()
    target = logging.StreamHandler(stream)
    target.setFormatter(logging.Formatter("%(levelname)s:%(message)s"))
    log_queue: queue.Queue[logging.LogRecord | None] = queue.Queue(maxsize=1)
    dispatcher = my_logging._AsyncLogDispatcher(
        log_queue,
        [target],
        high_priority_wait_seconds=0.0,
    )
    handler = my_logging._AsyncQueueLogHandler(dispatcher)
    log_queue.put_nowait(
        logging.LogRecord(
            "swe.test",
            logging.INFO,
            __file__,
            1,
            "queued",
            (),
            None,
        ),
    )

    handler.emit(
        logging.LogRecord(
            "swe.test",
            logging.WARNING,
            __file__,
            2,
            "fallback warning",
            (),
            None,
        ),
    )

    assert "WARNING:fallback warning" in stream.getvalue()
    assert log_queue.qsize() == 1


def test_shutdown_logger_is_repeatable() -> None:
    setup_logger("info")

    my_logging.shutdown_logger()
    my_logging.shutdown_logger()

    assert logging.getLogger(my_logging.LOG_NAMESPACE).handlers == []


def test_setup_logger_preserves_utf8_output_on_non_utf8_stderr(
    monkeypatch,
) -> None:
    my_logging.shutdown_logger()

    raw_stderr = io.BytesIO()
    wrapped_stderr = io.TextIOWrapper(
        raw_stderr,
        encoding="ascii",
        errors="strict",
        write_through=True,
    )
    monkeypatch.setattr(sys, "stderr", wrapped_stderr)

    logger = setup_logger("info")
    logger.info("标题：你好")
    my_logging.shutdown_logger()

    assert "标题：你好".encode("utf-8") in raw_stderr.getvalue()


def test_file_log_handler_api_is_removed() -> None:
    assert not hasattr(my_logging, "add_swe_file_handler")
