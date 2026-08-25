"""Data-flow trace: customer query -> orchestration -> agent -> tools -> response.

Every line already printed to the console for this trace (orchestrator.py's
turn header/routing/reply, crews/_base.py's tool-call trace) is mirrored into
a daily text file under backend/logs/, gated by QUERY_LOG_ENABLED in .env.
Disabling it only stops the file write — the console trace is unaffected.
"""
from __future__ import annotations

import logging
from datetime import datetime

from .config import settings

log = logging.getLogger(__name__)

_write_failed = False  # avoid spamming the log if the disk write keeps failing


def _log_path():
    return settings.logs_dir / f"query_log_{datetime.now():%Y-%m-%d}.txt"


def emit(line: str) -> None:
    """Print a trace line and, if enabled, append it to today's log file."""
    print(line)
    if not settings.query_log_enabled:
        return
    global _write_failed
    try:
        with open(_log_path(), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as exc:  # noqa: BLE001
        if not _write_failed:
            log.warning("query log file write failed (will stop retrying): %s", exc)
            _write_failed = True
