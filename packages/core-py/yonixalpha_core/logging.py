import logging
import sys
import time
from typing import Any

import structlog


def configure_logging(level: str = "INFO") -> None:
    """Structured JSON logging. Every event carries timestamp, service, level,
    and whatever event-specific keys the caller binds (token/symbol, strategy,
    order_id, tx_id, state, error, latency_ms, ...) per docs/OPERATIONS.md.
    """
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=shared_processors + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
        foreign_pre_chain=shared_processors,
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    # One INFO line per HTTP request drowns the service's own events (and
    # carries request URLs); failures still surface through our own logs.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(service: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(service=service)


class Timer:
    """Context manager for recording latency_ms on a log event.

    with Timer() as t:
        ...
    log.info("order.submitted", latency_ms=t.elapsed_ms)
    """

    def __enter__(self) -> "Timer":
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.elapsed_ms = round((time.perf_counter() - self._start) * 1000, 2)
