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
from filoutil.moodle_cabinet.notifications_manager import fetch_moodle_user_id
from filoutil.session_refresh.oidc_restore import (
    bootstrap_moodle_session_via_oidc,
    resolve_sesskey_from_moodle_session,
)
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
            "Usage: `/refresh_session {\"sesskey\": \"...\", \"moodleSession\": \"...\"}`\n"
            "or\n"
            "`/refresh_session {\"moodleSession\": \"...\"}`\n"
            "or\n"
            "`/refresh_session {\"oidc\": {\"microsoft_cookies\": \"ESTSAUTHPERSISTENT=...; ESTSAUTH=...\"}}`\n",
            parse_mode="Markdown",
        )
        return

    # Parse JSON
    try:
        data = json.loads(json_part)
    except json.JSONDecodeError as e:
        await update.message.reply_text(f"❌ Invalid JSON format: {str(e)}")
        return

    sesskey = data.get("sesskey")
    moodleSession = data.get("moodleSession")
    oidc_data = data.get("oidc") if isinstance(data.get("oidc"), dict) else None

    if (not sesskey or not moodleSession) and oidc_data:
        success, sesskey, moodleSession, updated_oidc_data, error_msg = (
            await bootstrap_moodle_session_via_oidc(oidc_data)
        )
        if not success or not sesskey or not moodleSession:
            await update.message.reply_text(
                f"❌ Failed to bootstrap Moodle session via OIDC: {error_msg or 'Unknown error'}"
            )
            return
        oidc_data = updated_oidc_data or oidc_data
    elif moodleSession and not sesskey:
        success, resolved_sesskey, error_msg = await resolve_sesskey_from_moodle_session(
            moodleSession
        )
        if not success or not resolved_sesskey:
            await update.message.reply_text(
                f"❌ Could not auto-fetch `sesskey`: {error_msg or 'Unknown error'}"
            )
            return
        sesskey = resolved_sesskey

    if not sesskey or not moodleSession:
        await update.message.reply_text(
            "❌ Missing required fields. Provide `moodleSession`, or pass `oidc` with Microsoft cookies (autonomous), or with `code/state/session_state` (one-time)."
        )
        return

    with SessionLocal() as db:
        user = get_user_by_telegram_id(db, update.message.from_user.id)
        if not user:
            await update.message.reply_text("❌ User not found.")
            return

        # Check if user has agreed to terms and confirmed student status
        if not user.moodle_session_agreement or not user.moodle_student_confirmation:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup

            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "📖 Read & Agree to Terms", callback_data="moodle:add_session"
                        )
                    ],
                    [InlineKeyboardButton("⬅️ Back to Moodle Menu", callback_data="moodle:menu")],
                ]
            )
            await update.message.reply_text(
                "⚠️ *Agreement Required*\n\n"
                "Before adding a Moodle session, you must:\n"
                "1. Confirm that you're a student\n"
                "2. Read and agree to the security notice\n\n"
                "This notice explains what access you're granting and the security implications.\n\n"
                'Please click "📖 Read & Agree to Terms" below to continue.',
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
            return

        # Get user's refresh interval setting
        refresh_interval = get_user_refresh_interval(db, user.id)

        # Extract optional name from data
        session_name = data.get("name")

        # Create session refresh job
        session_refresh = create_session_refresh(
            db,
            user.id,
            sesskey,
            moodleSession,
            refresh_interval,
            name=session_name,
            oidc_data=oidc_data,
        )

        # Immediately fetch and store Moodle user ID
        try:
            success, moodle_user_id, error_msg = await fetch_moodle_user_id(sesskey, moodleSession)
            if success and moodle_user_id:
                session_refresh.moodle_user_id = moodle_user_id
                db.commit()
                db.refresh(session_refresh)
                logger.info(
                    f"Successfully fetched Moodle user ID {moodle_user_id} for session {session_refresh.id}"
                )
            else:
                logger.warning(
                    f"Could not fetch Moodle user ID for session {session_refresh.id}: {error_msg}"
                )
        except Exception as e:
            logger.error(
                f"Error fetching Moodle user ID for session {session_refresh.id}: {e}",
                exc_info=True,
            )
            # Continue anyway - the user ID can be fetched later

        # Don't start the task here - let the scheduler pick it up to avoid duplicates
        # The scheduler will detect the new active session and start the task

        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu:main")]]
        )
        await update.message.reply_text(
            f"✅ Session refresh started!\n\n"
            f"*Session ID:* {session_refresh.id}\n\n"
            f"The session will be automatically refreshed based on Moodle's session expiry time.\n\n"
            f"{'✅ OIDC auto-recovery is enabled for this session.' if oidc_data else ''}",
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
