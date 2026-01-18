"""Centralized configuration for the application."""

import logging
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# Get timezone from environment variable, default to Asia/Almaty
TIMEZONE_STR = os.getenv("TIMEZONE", "Asia/Almaty")

try:
    APP_TIMEZONE = ZoneInfo(TIMEZONE_STR)
    logger.info(f"Application timezone set to: {TIMEZONE_STR}")
except Exception as e:
    logger.error(f"Invalid timezone '{TIMEZONE_STR}', falling back to UTC: {e}")
    APP_TIMEZONE = ZoneInfo("UTC")
    TIMEZONE_STR = "UTC"


def get_current_time() -> datetime:
    """
    Get the current time in the configured application timezone.

    Returns:
        Timezone-aware datetime object in the configured timezone.
    """
    return datetime.now(APP_TIMEZONE)


def format_time_for_display(dt: datetime, include_tz: bool = True) -> str:
    """
    Format a datetime object for display in the configured timezone.

    Args:
        dt: Datetime object to format (can be naive or timezone-aware)
        include_tz: Whether to include timezone info in the output

    Returns:
        Formatted string like "2026-01-18 15:30:45 Asia/Almaty" or "2026-01-18 15:30:45"
    """
    # If datetime is naive, assume it's UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    # Convert to application timezone
    dt_local = dt.astimezone(APP_TIMEZONE)

    # Format the datetime
    formatted = dt_local.strftime("%Y-%m-%d %H:%M:%S")

    if include_tz:
        # Get timezone offset for display
        offset = dt_local.strftime("%z")
        # Format offset as +HH:MM
        if offset:
            offset_formatted = f"{offset[:3]}:{offset[3:]}"
            formatted += f" {TIMEZONE_STR} (UTC{offset_formatted})"
        else:
            formatted += f" {TIMEZONE_STR}"

    return formatted
