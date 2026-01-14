import logging
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from filoutil.auth import ensure_user_and_check_whitelisted
from filoutil.db.postgres import SessionLocal
from filoutil.db.session_refresh import (
    get_active_session_refresh,
    mark_reminder_sent,
    stop_session_refresh,
    update_reminder_due_at,
)
from filoutil.db.users import get_user_by_telegram_id, update_user_activity

logger = logging.getLogger(__name__)


async def reminder_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle callback queries for reminder actions."""
    query = update.callback_query
    if not query:
        return

    # Track user activity
    if query.from_user:
        with SessionLocal() as db:
            update_user_activity(db, query.from_user.id)

    await query.answer()

    data = query.data.split(":")
    action = data[1]

    if action == "stop":
        session_id = int(data[2])

        with SessionLocal() as db:
            session_refresh = db.execute(
                select(SessionRefresh).where(SessionRefresh.id == session_id)
            ).scalar_one_or_none()

            if not session_refresh or session_refresh.status != "running":
                await query.edit_message_text("❌ Session not found or already stopped.")
                return

            # Stop the session
            stopped_session = stop_session_refresh(db, session_id, status="stopped")

            if stopped_session:
                duration_seconds = stopped_session.duration_seconds or 0
                hours, remainder = divmod(duration_seconds, 3600)
                minutes, seconds = divmod(remainder, 60)
                duration_str = f"{hours}h {minutes}m {seconds}s"

                text = (
                    f"🛑 *Session Stopped*\n\n"
                    f"Your LMS session refresh has been stopped.\n\n"
                    f"*Total duration:* {duration_str}"
                )

                try:
                    await query.edit_message_text(text, parse_mode="Markdown")
                except BadRequest:
                    # If message was already deleted, send a new one
                    await context.bot.send_message(
                        chat_id=query.message.chat_id, text=text, parse_mode="Markdown"
                    )

    elif action in ["1d", "3d", "7d"]:
        session_id = int(data[2])
        days = int(action.replace("d", ""))

        with SessionLocal() as db:
            session_refresh = db.execute(
                select(SessionRefresh).where(SessionRefresh.id == session_id)
            ).scalar_one_or_none()

            if not session_refresh or session_refresh.status != "running":
                await query.answer("❌ Session not found or already stopped.", show_alert=True)
                return

            # Set reminder due date
            reminder_due_at = datetime.utcnow() + timedelta(days=days)
            update_reminder_due_at(db, session_id, reminder_due_at)
            mark_reminder_sent(db, session_id)

            text = f"✅ Reminder set for {days} day{'s' if days > 1 else ''} from now."

            try:
                await query.answer(text, show_alert=True)
                # Update the message to show confirmation
                await query.edit_message_text(
                    f"⏰ *Reminder Set*\n\n{text}\n\nYou'll be reminded when you're online.",
                    parse_mode="Markdown",
                )
            except BadRequest:
                await query.answer(text, show_alert=True)


async def reminder_settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /reminder_settings command to configure reminder settings."""
    if not await ensure_user_and_check_whitelisted(update, context):
        return

    # Check if arguments were provided
    if context.args and len(context.args) >= 1:
        time_str = context.args[0]
        timezone_str = context.args[1] if len(context.args) > 1 else "UTC"

        # Validate time format
        try:
            hour, minute = map(int, time_str.split(":"))
            if not (0 <= hour < 24 and 0 <= minute < 60):
                raise ValueError("Invalid time")
        except (ValueError, AttributeError):
            await update.message.reply_text("❌ Invalid time format. Use HH:MM (e.g., 03:00)")
            return

        # Validate timezone
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(timezone_str)
        except Exception:
            await update.message.reply_text(f"❌ Invalid timezone: {timezone_str}")
            return

        # Save settings
        with SessionLocal() as db:
            user = get_user_by_telegram_id(db, update.message.from_user.id)
            if user:
                settings = dict(user.settings) if user.settings else {}
                settings["reminder_reset_time"] = time_str
                settings["reminder_timezone"] = timezone_str
                user.settings = settings
                db.commit()

                # Calculate time until next reminder trigger
                from zoneinfo import ZoneInfo

                tz = ZoneInfo(timezone_str)
                now_utc = datetime.utcnow()
                now_local = now_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)

                hour, minute = map(int, time_str.split(":"))
                reset_today = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)

                # If reset time hasn't passed today, use today's reset time
                # Otherwise, use tomorrow's reset time
                if now_local < reset_today:
                    next_reset = reset_today
                else:
                    next_reset = reset_today + timedelta(days=1)

                # Calculate time difference
                time_until = next_reset - now_local
                total_minutes = int(time_until.total_seconds() / 60)
                hours = total_minutes // 60
                minutes = total_minutes % 60

                await update.message.reply_text(
                    f"✅ Reminder settings updated!\n\n"
                    f"*Reset time:* {time_str}\n"
                    f"*Timezone:* {timezone_str}\n\n"
                    f"*Next reminder in:* {hours}h {minutes}m",
                    parse_mode="Markdown",
                )
            else:
                await update.message.reply_text("❌ User not found.")
        return

    # Show current settings (no arguments provided)
    with SessionLocal() as db:
        user = get_user_by_telegram_id(db, update.message.from_user.id)
        if not user:
            await update.message.reply_text("❌ User not found.")
            return

        settings = user.settings or {}
        reset_time = settings.get("reminder_reset_time", "03:00")
        timezone = settings.get("reminder_timezone", "UTC")

        text = (
            f"⏰ *Reminder Settings*\n\n"
            f"*Reset time:* {reset_time}\n"
            f"*Timezone:* {timezone}\n\n"
            f"To configure, send:\n"
            f"`/reminder_settings <HH:MM> <timezone>`\n\n"
            f"Example: `/reminder_settings 03:00 Asia/Almaty`"
        )

        await update.message.reply_text(text, parse_mode="Markdown")


async def handle_reminder_settings_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """Handle reminder settings input."""
    if not update.message or not update.message.text:
        return False

    text = update.message.text.strip()

    # Check if it's a reminder settings command with arguments
    if text.startswith("/reminder_settings ") and len(text.split()) >= 2:
        parts = text.split()
        if len(parts) >= 2:
            time_str = parts[1]
            timezone_str = parts[2] if len(parts) > 2 else "UTC"

            # Validate time format
            try:
                hour, minute = map(int, time_str.split(":"))
                if not (0 <= hour < 24 and 0 <= minute < 60):
                    raise ValueError("Invalid time")
            except (ValueError, AttributeError):
                await update.message.reply_text("❌ Invalid time format. Use HH:MM (e.g., 03:00)")
                return True

            # Validate timezone
            try:
                from zoneinfo import ZoneInfo

                ZoneInfo(timezone_str)
            except Exception:
                await update.message.reply_text(f"❌ Invalid timezone: {timezone_str}")
                return True

            # Save settings
            with SessionLocal() as db:
                user = get_user_by_telegram_id(db, update.message.from_user.id)
                if user:
                    settings = dict(user.settings) if user.settings else {}
                    settings["reminder_reset_time"] = time_str
                    settings["reminder_timezone"] = timezone_str
                    user.settings = settings
                    db.commit()

                    await update.message.reply_text(
                        f"✅ Reminder settings updated!\n\n"
                        f"*Reset time:* {time_str}\n"
                        f"*Timezone:* {timezone_str}",
                        parse_mode="Markdown",
                    )
                else:
                    await update.message.reply_text("❌ User not found.")

            return True

    return False


from sqlalchemy import select

from filoutil.db.models import SessionRefresh
