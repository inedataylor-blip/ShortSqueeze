"""
Timezone utilities for Short Squeeze Trading Bot.

Ensures all market hours calculations use US Eastern Time,
regardless of the server's local timezone.
"""

from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

# US Eastern timezone (handles EST/EDT automatically)
EASTERN_TZ = ZoneInfo("America/New_York")

# Local timezone for display (Arizona - no DST)
LOCAL_TZ = ZoneInfo("America/Phoenix")

# Market hours in Eastern Time
MARKET_OPEN = dt_time(9, 30)
MARKET_CLOSE = dt_time(16, 0)


def now_eastern() -> datetime:
    """Get current datetime in US Eastern timezone."""
    return datetime.now(EASTERN_TZ)


def is_market_hours() -> bool:
    """
    Check if we're currently in US stock market hours.

    Returns:
        True if current Eastern time is between 9:30 AM and 4:00 PM ET
        on a weekday (Mon-Fri).
    """
    now = now_eastern()

    # Check weekday (Monday=0, Sunday=6)
    if now.weekday() >= 5:
        return False

    # Check time
    current_time = now.time()
    return MARKET_OPEN <= current_time <= MARKET_CLOSE


def get_minutes_since_open() -> int:
    """
    Get number of minutes since market open (Eastern Time).

    Returns:
        Minutes since 9:30 AM ET, or 0 if before market open.
    """
    now = now_eastern()
    market_open_dt = datetime.combine(now.date(), MARKET_OPEN, tzinfo=EASTERN_TZ)

    if now < market_open_dt:
        return 0

    delta = now - market_open_dt
    return int(delta.total_seconds() / 60)


def get_market_open_today() -> datetime:
    """Get today's market open time in Eastern timezone."""
    now = now_eastern()
    return datetime.combine(now.date(), MARKET_OPEN, tzinfo=EASTERN_TZ)


def get_market_close_today() -> datetime:
    """Get today's market close time in Eastern timezone."""
    now = now_eastern()
    return datetime.combine(now.date(), MARKET_CLOSE, tzinfo=EASTERN_TZ)


def now_local() -> datetime:
    """Get current datetime in local timezone (Arizona)."""
    return datetime.now(LOCAL_TZ)


def to_local(dt: datetime) -> datetime:
    """Convert a datetime to local timezone (Arizona)."""
    return dt.astimezone(LOCAL_TZ)


def get_market_hours_local() -> tuple[str, str]:
    """Get market hours in local timezone for display."""
    market_open = get_market_open_today().astimezone(LOCAL_TZ)
    market_close = get_market_close_today().astimezone(LOCAL_TZ)
    return market_open.strftime("%I:%M %p"), market_close.strftime("%I:%M %p")
