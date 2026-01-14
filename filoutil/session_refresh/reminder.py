import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application

from filoutil.db.models import SessionRefresh, User
from filoutil.db.postgres import SessionLocal
from filoutil.db.session_refresh import (
    get_all_active_sessions,
    mark_reminder_sent,
    stop_session_refresh,
    update_reminder_due_at,
)

logger = logging.getLogger(__name__)


def get_reminder_keyboard(session_id: int) -> InlineKeyboardMarkup:
    """Generate keyboard for reminder message."""
    keyboard = [
        [InlineKeyboardButton("🛑 Stop Session", callback_data=f"reminder:stop:{session_id}")],
        [
            InlineKeyboardButton("⏰ Remind in 1 day", callback_data=f"reminder:1d:{session_id}"),
            InlineKeyboardButton("⏰ Remind in 3 days", callback_data=f"reminder:3d:{session_id}"),
        ],
        [InlineKeyboardButton("⏰ Remind in 7 days", callback_data=f"reminder:7d:{session_id}")],
    ]
    return InlineKeyboardMarkup(keyboard)


async def send_reminder(app: Application, session_refresh: SessionRefresh, user: User) -> None:
    """Send a reminder message to the user about their active session."""
    duration = datetime.utcnow() - session_refresh.started_at
    hours, remainder = divmod(int(duration.total_seconds()), 3600)
    minutes, seconds = divmod(remainder, 60)
    duration_str = f"{hours}h {minutes}m {seconds}s"

    message = (
        f"⏰ *Session Refresh Reminder*\n\n"
        f"Your LMS session refresh is still running.\n\n"
        f"*Started:* {session_refresh.started_at.strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
        f"*Duration:* {duration_str}\n"
        f"*Refresh interval:* {session_refresh.refresh_interval_s // 60}m\n\n"
        f"Choose an option below:"
    )

    try:
        await app.bot.send_message(
            chat_id=user.telegram_id,
            text=message,
            reply_markup=get_reminder_keyboard(session_refresh.id),
            parse_mode="Markdown",
        )
        # Mark reminder as sent
        with SessionLocal() as db:
            mark_reminder_sent(db, session_refresh.id)
        logger.info(f"Sent reminder for session {session_refresh.id} to user {user.telegram_id}")
    except Exception as e:
        logger.error(f"Failed to send reminder to user {user.telegram_id}: {e}")


def get_user_timezone(user: User) -> ZoneInfo:
    """Get user's timezone from settings, default to UTC."""
    if user.settings and "reminder_timezone" in user.settings:
        try:
            return ZoneInfo(user.settings["reminder_timezone"])
        except Exception:
            logger.warning(f"Invalid timezone for user {user.id}, using UTC")
    return ZoneInfo("UTC")


def get_user_reminder_reset_time(user: User) -> tuple[int, int] | None:
    """Get user's reminder reset time (hour, minute) from settings."""
    if user.settings and "reminder_reset_time" in user.settings:
        try:
            time_str = user.settings["reminder_reset_time"]
            hour, minute = map(int, time_str.split(":"))
            return (hour, minute)
        except Exception:
            logger.warning(f"Invalid reminder_reset_time for user {user.id}")
    return None


def should_send_reminder(session_refresh: SessionRefresh, user: User, now_utc: datetime) -> bool:
    """Check if a reminder should be sent for this session."""
    # If reminder was sent recently (within last 5 minutes), don't send again
    if session_refresh.last_reminder_sent_at:
        time_since_last = (now_utc - session_refresh.last_reminder_sent_at).total_seconds()
        if time_since_last < 300:  # 5 minutes
            return False

    # Check if reminder is due
    if session_refresh.reminder_due_at:
        if now_utc < session_refresh.reminder_due_at:
            return False

    # Check if user is online (has activity in last 2 minutes)
    if user.last_activity_at:
        time_since_activity = (now_utc - user.last_activity_at).total_seconds()
        if time_since_activity > 120:  # 2 minutes
            return False

    # Check if it's past the reset time today
    reset_time = get_user_reminder_reset_time(user)
    if reset_time:
        tz = get_user_timezone(user)
        now_local = now_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
        reset_hour, reset_minute = reset_time

        # Check if we're past the reset time today
        reset_today = now_local.replace(
            hour=reset_hour, minute=reset_minute, second=0, microsecond=0
        )

        # If reset time hasn't passed today, don't send
        if now_local < reset_today:
            return False

        # Check if we already sent a reminder after this reset time
        if session_refresh.last_reminder_sent_at:
            last_sent_local = session_refresh.last_reminder_sent_at.replace(
                tzinfo=ZoneInfo("UTC")
            ).astimezone(tz)
            # If we sent after today's reset time, don't send again
            if last_sent_local >= reset_today:
                return False

    return True


async def reminder_task(app: Application) -> None:
    """Background task that checks for reminders and sends them when appropriate."""
    logger.info("Starting reminder task...")

    while True:
        try:
            now_utc = datetime.utcnow()

            with SessionLocal() as db:
                active_sessions = get_all_active_sessions(db)

                for session in active_sessions:
                    user = db.execute(
                        select(User).where(User.id == session.user_id)
                    ).scalar_one_or_none()

                    if not user:
                        continue

                    # Check if reminder should be sent
                    if should_send_reminder(session, user, now_utc):
                        await send_reminder(app, session, user)

        except Exception as e:
            logger.error(f"Error in reminder_task: {e}", exc_info=True)

        # Check every 30 seconds
        await asyncio.sleep(30)
