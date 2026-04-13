"""
logger_config.py — Centralised structured JSON logging for MediLoop.

USAGE:
    from logger_config import get_logger
    logger = get_logger(__name__)

    # Basic log
    logger.info("Reminder sent", extra={"pharmacy_id": "abc", "patient_id": "xyz"})

    # Error with context
    logger.error("WhatsApp send failed", extra={
        "pharmacy_id": pharmacy_id,
        "phone": phone,
        "error": str(e)
    })

WHY JSON LOGS:
    Plain text logs can't be searched or filtered.
    JSON logs let you query in Logtail/Datadog:
        - "show all failed sends for pharmacy X"
        - "how many reminders sent in last hour"
        - "all errors from scheduler today"

SETUP (free):
    1. Go to logtail.com → create free account → new source → Python
    2. Copy your source token
    3. Add LOGTAIL_TOKEN=your_token to Railway environment variables
    4. Done — logs appear in Logtail automatically

    If LOGTAIL_TOKEN is not set, logs go to stdout only (Railway logs).
    This means existing deployments keep working with no changes.
"""

import os
import logging
import json
from datetime import datetime, timezone


class JSONFormatter(logging.Formatter):
    """
    Formats every log record as a single JSON line.
    Adds standard fields: timestamp, level, module, message.
    Any extra={} kwargs passed to logger calls are included at the top level.
    """

    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level":     record.levelname,
            "module":    record.name,
            "message":   record.getMessage(),
            "environment": os.getenv("ENVIRONMENT", "production"),
        }

        # Pull any extra fields passed via extra={} into the top level
        # Standard LogRecord attributes to skip
        skip = {
            "name", "msg", "args", "levelname", "levelno", "pathname",
            "filename", "module", "exc_info", "exc_text", "stack_info",
            "lineno", "funcName", "created", "msecs", "relativeCreated",
            "thread", "threadName", "processName", "process", "message",
            "taskName"
        }
        for key, value in record.__dict__.items():
            if key not in skip:
                log_obj[key] = value

        # Attach exception info if present
        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_obj, default=str)


def setup_logging():
    """
    Call once at app startup (in main.py).
    Configures root logger with JSON formatter.
    Optionally ships to Logtail if LOGTAIL_TOKEN is set.
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # Remove existing handlers to avoid duplicate logs
    root_logger.handlers.clear()

    # ── Stdout handler (always on — Railway captures this) ─────────────────
    stdout_handler = logging.StreamHandler()
    stdout_handler.setFormatter(JSONFormatter())
    root_logger.addHandler(stdout_handler)

    # ── Logtail handler (optional — only if token is set) ──────────────────
    logtail_token = os.getenv("LOGTAIL_TOKEN")
    if logtail_token:
        try:
            from logtail import LogtailHandler
            logtail_handler = LogtailHandler(source_token=logtail_token)
            logtail_handler.setFormatter(JSONFormatter())
            root_logger.addHandler(logtail_handler)
            logging.getLogger(__name__).info(
                "Logtail handler attached",
                extra={"token_prefix": logtail_token[:6]}
            )
        except ImportError:
            logging.getLogger(__name__).warning(
                "logtail-python not installed — add 'logtail-python' to requirements.txt"
            )
    else:
        logging.getLogger(__name__).info(
            "LOGTAIL_TOKEN not set — logging to stdout only"
        )

    # Silence noisy third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """
    Use this in every file instead of logging.getLogger(__name__).
    Ensures the module is always using the configured JSON formatter.

    Usage:
        from logger_config import get_logger
        logger = get_logger(__name__)
    """
    return logging.getLogger(name)