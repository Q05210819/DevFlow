"""Structured operational log events with an explicit, safe field list."""

import json
import logging
import sys

logger = logging.getLogger("devflow")


def configure_logging() -> None:
    """Emit application records to stderr without attaching sensitive context."""
    logger.setLevel(logging.INFO)
    for handler in logger.handlers[:]:
        if getattr(handler, "_devflow_handler", False):
            logger.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler._devflow_handler = True
    logger.addHandler(handler)


def log_request(request_id: str, status_code: int) -> None:
    logger.info(
        json.dumps(
            {
                "request_id": request_id,
                "job_id": None,
                "actor_id": None,
                "action": "http.request",
                "outcome": "success" if status_code < 400 else "error",
                "status_code": status_code,
            },
            separators=(",", ":"),
        )
    )
