import logging
import sys
import warnings

import structlog


class _ExceptionPrivacyFilter(logging.Filter):
    """Keep failure categories without untrusted exception details."""

    def __init__(self, source: str, *, min_level: int = logging.CRITICAL + 1) -> None:
        super().__init__()
        self._source = source
        self._min_level = min_level

    def filter(self, record: logging.LogRecord) -> bool:
        if not (
            record.exc_info
            or record.exc_text
            or record.stack_info
            or record.levelno >= self._min_level
        ):
            return True
        exception = record.exc_info[1] if record.exc_info else None
        category = type(exception).__name__ if exception is not None else "unknown"
        record.msg = "%s failure (%s)"
        record.args = (self._source, category)
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


_web_exception_filter = _ExceptionPrivacyFilter("Web framework")
_server_exception_filter = _ExceptionPrivacyFilter("ASGI server")
_asyncio_exception_filter = _ExceptionPrivacyFilter(
    "Asyncio", min_level=logging.WARNING
)


def configure_web_logger(logger: logging.Logger) -> None:
    logger.addFilter(_web_exception_filter)


def configure_logging():
    """Configure application logs and suppress known sensitive library output."""
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=logging.INFO)

    # Library messages can contain Telegram updates, URLs, headers, or provider errors.
    # Application logs record safe operation and error categories instead.
    for name in (
        "telethon",
        "redis",
        "push_response",
        "httpx",
        "httpcore",
        "hpack",
        "google_genai",
    ):
        library = logging.getLogger(name)
        library.handlers = [logging.NullHandler()]
        library.propagate = False

    logging.getLogger("hypercorn.error").addFilter(_server_exception_filter)
    logging.getLogger("hypercorn.access").disabled = True
    logging.getLogger("asyncio").addFilter(_asyncio_exception_filter)

    # These warnings can include provider values or connection representations.
    for category, module in (
        (UserWarning, r"google\.genai\._common$"),
        (ResourceWarning, r"redis\.asyncio\.connection$"),
        (ResourceWarning, r"asyncio\.streams$"),
    ):
        warnings.filterwarnings("ignore", category=category, module=module)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str):
    """Get a structured logger instance."""
    return structlog.get_logger(name)
