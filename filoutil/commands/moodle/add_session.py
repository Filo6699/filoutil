"""Command to add a new Moodle session."""

import json
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from filoutil.auth import require_module_permission
from filoutil.db.postgres import SessionLocal
from filoutil.db.session_refresh import (
    create_session_refresh,
    get_active_sessions_for_user,
    get_user_refresh_interval,
)
from filoutil.db.users import get_user_by_telegram_id

logger = logging.getLogger(__name__)


async def moodle_add_session_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /moodle_add command with JSON input."""
    if not await require_module_permission(update, context, "moodle"):
        return

    if not update.message or not update.message.text:
        await update.message.reply_text("Please provide session data in JSON format.")
        return

    # Extract JSON from message (remove command part)
    message_text = update.message.text.strip()
    if message_text.startswith("/moodle_add"):
        # Remove command and get JSON part
        json_part = message_text[len("/moodle_add") :].strip()
    else:
        json_part = message_text

    if not json_part:
        await update.message.reply_text(
            'Usage: `/moodle_add {"sesskey": "...", "moodleSession": "...", "name": "Optional Name"}`',
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
    session_name = data.get("name")  # Optional

    if not sesskey or not moodleSession:
        await update.message.reply_text("❌ `sesskey` and `moodleSession` cannot be empty.")
        return

    # Check for existing active sessions (allow up to 3)
    with SessionLocal() as db:
        user = get_user_by_telegram_id(db, update.message.from_user.id)
        if not user:
            await update.message.reply_text("❌ User not found.")
            return

        active_sessions = get_active_sessions_for_user(db, user.id)
        max_sessions = 5

        if len(active_sessions) >= max_sessions:
            await update.message.reply_text(
                f"❌ You already have {len(active_sessions)} active sessions (maximum: {max_sessions}). "
                "Please stop one before adding a new session.\n\n"
                'Use `/moodle` → "📋 My Sessions" to manage your sessions.'
            )
            return

        # Get user's refresh interval setting
        refresh_interval = get_user_refresh_interval(db, user.id)

        # Create session refresh job
        session_refresh = create_session_refresh(
            db, user.id, sesskey, moodleSession, refresh_interval, name=session_name
        )

        # Format session name for display
        display_name = session_name or f"Session {session_refresh.id}"

        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📋 View Sessions", callback_data="moodle:sessions")],
                [InlineKeyboardButton("⬅️ Back to Moodle Menu", callback_data="moodle:menu")],
            ]
        )

        await update.message.reply_text(
            f"✅ *Moodle Session Added!*\n\n"
            f"*Name:* {display_name}\n"
            f"*Session ID:* {session_refresh.id}\n"
            f"*Refresh interval:* {refresh_interval // 60}m ({refresh_interval}s)\n\n"
            f"You now have {len(active_sessions) + 1} active session(s).",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
