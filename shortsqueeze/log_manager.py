"""
Logging configuration and daily market-close log rotation.

Lives in the package (not the ``main.py`` entrypoint) so the bot's scheduler can
trigger ``rotate_daily_log`` without an import cycle. loguru's ``logger`` is a
process-wide singleton, so the file sink id stored here is shared app-wide.

Why not loguru's built-in rotation: loguru names a rotated archive by the file's
*open* time. With rotation shortly after the 16:00 ET close, that open time is
just after the *prior* day's close, so the archive ends up labelled with the
previous day even though it contains the current session. Instead we keep loguru
writing to a stable ``logs/bot.log`` and rename it explicitly at market close to
``logs/bot_<session-date>.log`` (driven by the bot's Eastern-time scheduler).
"""

import sys
import time
from pathlib import Path

from loguru import logger

from .timezone import LOCAL_TZ, now_eastern

LOG_DIR = Path("logs")
LOG_FILE = LOG_DIR / "bot.log"
RETENTION_DAYS = 30

# Sink id of the active file handler, so rotate_daily_log can swap it out.
_file_sink_id = None


def _stamp_arizona_time(record) -> None:
    """Stash the Arizona-time (MST, no DST) timestamp on the record for formatting."""
    record["extra"]["az_time"] = (
        record["time"].astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
    )


def _console_format(record) -> str:
    _stamp_arizona_time(record)
    return (
        "<green>{extra[az_time]}</green> | "
        "<level>{level: <8}</level> | <level>{message}</level>\n"
    )


def _file_format(record) -> str:
    _stamp_arizona_time(record)
    return "{extra[az_time]} | {level: <8} | {name}:{function} - {message}\n"


def _add_file_sink() -> int:
    """Add the loguru file sink for the active log and return its id.

    No ``rotation``/``retention`` is configured here — rotation is explicit via
    ``rotate_daily_log`` so archives can be named by session date.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return logger.add(str(LOG_FILE), format=_file_format, level="DEBUG")


def configure_logging(level: str = "INFO") -> None:
    """Configure console + file logging.

    The format callables return a loguru *template* containing the ``{message}``
    placeholder rather than interpolating the message text directly, so log
    messages that contain literal braces (e.g. ``{'class': 'screener_table'}``)
    do not raise ``KeyError`` inside the handler.
    """
    global _file_sink_id
    logger.remove()
    logger.add(sys.stderr, format=_console_format, level=level)
    _file_sink_id = _add_file_sink()


def rotate_daily_log() -> None:
    """Rename the active log to its session date and start a fresh one.

    Intended to run shortly after the 16:00 ET market close. The file renamed at
    16:15 ET on day D contains day D's full session, so it is named
    ``bot_<D>.log``.
    """
    global _file_sink_id

    session = now_eastern().strftime("%Y-%m-%d")
    target = LOG_DIR / f"bot_{session}.log"
    # Avoid clobbering if rotation runs twice in one ET day (e.g. manual re-run).
    if target.exists():
        target = LOG_DIR / f"bot_{session}_{now_eastern().strftime('%H%M%S')}.log"

    # Detach the file sink so the OS file handle is released before renaming.
    if _file_sink_id is not None:
        try:
            logger.remove(_file_sink_id)
        except ValueError:
            pass  # sink already gone; fall through and re-add
        _file_sink_id = None

    try:
        if LOG_FILE.exists():
            LOG_FILE.rename(target)
    except OSError as e:
        # Re-attach a sink even if the rename failed, so logging keeps working.
        _file_sink_id = _add_file_sink()
        logger.error(f"Daily log rotation failed to rename {LOG_FILE} -> {target}: {e}")
        return

    _file_sink_id = _add_file_sink()
    logger.info(f"Rotated daily log -> {target.name}")
    _cleanup_old_logs()


def _cleanup_old_logs(retention_days: int = RETENTION_DAYS) -> None:
    """Delete archived ``bot_*.log`` files older than the retention window."""
    cutoff = time.time() - retention_days * 86400
    for f in LOG_DIR.glob("bot_*.log"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            continue
