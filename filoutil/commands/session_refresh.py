import json
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from filoutil.auth import require_module_permission
from filoutil.db.postgres import SessionLocal
from filoutil.db.session_refresh import (
    create_session_refresh,
    get_active_session_refresh,
    get_user_refresh_interval,
)
from filoutil.db.users import get_user_by_telegram_id
from filoutil.session_refresh.engine import run_session_refresh_task

logger = logging.getLogger(__name__)

# Default refresh interval options (in seconds)
REFRESH_INTERVAL_OPTIONS = {
    "60": 60,
    "300": 300,
    "600": 600,
    "1800": 1800,
}


def get_settings_keyboard(current_interval: int) -> InlineKeyboardMarkup:
    """Generate keyboard for refresh settings menu."""
    keyboard = []
    for label, interval in REFRESH_INTERVAL_OPTIONS.items():
        # Format label: "1m", "5m", "10m", "30m"
        minutes = interval // 60
        label_text = f"{minutes}m" if minutes < 60 else f"{interval // 60}m"
        selected = "◉" if interval == current_interval else "○"
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"{selected} {label_text} ({interval}s)",
                    callback_data=f"refresh_settings:set:{interval}",
                )
            ]
        )
    keyboard.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu:main")])
    return InlineKeyboardMarkup(keyboard)


async def refresh_session_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /refresh_session command with JSON input."""
    if not await require_module_permission(update, context, "moodle"):
        return

    if not update.message or not update.message.text:
        await update.message.reply_text("Please provide session data in JSON format.")
        return

    # Extract JSON from message (remove command part)
    message_text = update.message.text.strip()
    if message_text.startswith("/refresh_session"):
        # Remove command and get JSON part
        json_part = message_text[len("/refresh_session") :].strip()
    else:
        json_part = message_text

    if not json_part:
        await update.message.reply_text(
            'Usage: `/refresh_session {"sesskey": "...", "moodleSession": "..."}`',
            parse_mode="Markdown",
        )
        return

    # Parse JSON
    try:
        data = json.loads(json_part)
    except json.JSONDecodeError as e:
        await update.message.reply_text(f"❌ Invalid JSON format: {str(e)}")
        return

    # Validate required fields
    if "sesskey" not in data or "moodleSession" not in data:
        await update.message.reply_text(
            "❌ Missing required fields. Please provide `sesskey` and `moodleSession`."
        )
        return

    sesskey = data["sesskey"]
    moodleSession = data["moodleSession"]

    if not sesskey or not moodleSession:
        await update.message.reply_text("❌ `sesskey` and `moodleSession` cannot be empty.")
        return

    with SessionLocal() as db:
        user = get_user_by_telegram_id(db, update.message.from_user.id)
        if not user:
            await update.message.reply_text("❌ User not found.")
            return

        # Get user's refresh interval setting
        refresh_interval = get_user_refresh_interval(db, user.id)

        # Extract optional name from data
        session_name = data.get("name")

        # Create session refresh job
        session_refresh = create_session_refresh(
            db, user.id, sesskey, moodleSession, refresh_interval, name=session_name
        )

        # Don't start the task here - let the scheduler pick it up to avoid duplicates
        # The scheduler will detect the new active session and start the task

        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu:main")]]
        )
        await update.message.reply_text(
            f"✅ Session refresh started!\n\n"
            f"*Refresh interval:* {refresh_interval}s ({refresh_interval // 60}m)\n"
            f"*Session ID:* {session_refresh.id}\n\n"
            f"Use `/refresh_settings` to configure the refresh interval.",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )


async def refresh_settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /refresh_settings command to show settings menu."""
    if not await require_module_permission(update, context, "moodle"):
        return

    with SessionLocal() as db:
        user = get_user_by_telegram_id(db, update.message.from_user.id)
        if not user:
            await update.message.reply_text("❌ User not found.")
            return

        current_interval = get_user_refresh_interval(db, user.id)
        minutes = current_interval // 60

        text = (
            f"⚙️ *Session Refresh Settings*\n\n"
            f"*Current refresh interval:* {minutes}m ({current_interval}s)\n\n"
            f"Select a new interval:"
        )

        await update.message.reply_text(
            text, reply_markup=get_settings_keyboard(current_interval), parse_mode="Markdown"
        )


async def refresh_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle callback queries for refresh settings."""
    query = update.callback_query
    if not query:
        return

    # Check permission
    if not await require_module_permission(update, context, "moodle"):
        return

    await query.answer()

    data = query.data.split(":")
    action = data[1]

    if action == "set":
        interval = int(data[2])

        with SessionLocal() as db:
            user = get_user_by_telegram_id(db, query.from_user.id)
            if not user:
                await query.edit_message_text("❌ User not found.")
                return

            # Update user settings
            settings = dict(user.settings) if user.settings else {}
            settings["refresh_interval_s"] = interval
            user.settings = settings
            db.commit()

            minutes = interval // 60
            await query.answer(f"✅ Refresh interval set to {minutes}m ({interval}s)")

            # Update the message to show new selection
            text = (
                f"⚙️ *Session Refresh Settings*\n\n"
                f"*Current refresh interval:* {minutes}m ({interval}s)\n\n"
                f"Select a new interval:"
            )

            try:
                await query.edit_message_text(
                    text, reply_markup=get_settings_keyboard(interval), parse_mode="Markdown"
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
