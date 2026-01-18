"""Moodle session management commands."""

import logging
from datetime import datetime, timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from filoutil.auth import require_module_permission
from filoutil.commands.moodle.menu import format_session_info, get_sessions_list_keyboard
from filoutil.config import format_time_for_display
from filoutil.db.postgres import SessionLocal
from filoutil.db.session_refresh import (
    get_active_sessions_for_user,
    get_session_refresh_by_id,
    stop_session_refresh,
    update_session_name,
)
from filoutil.db.users import get_user_by_telegram_id

logger = logging.getLogger(__name__)


async def show_sessions_list(
    db, user_id: int, query, context: ContextTypes.DEFAULT_TYPE, page: int = 0
) -> None:
    """Show list of user's Moodle sessions."""
    sessions = get_active_sessions_for_user(db, user_id)

    if not sessions:
        text = (
            "📋 *My Moodle Sessions*\n\n"
            "You don't have any active sessions.\n\n"
            'Use "➕ Add Session" to create a new session.'
        )
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("➕ Add Session", callback_data="moodle:add_session")],
                [InlineKeyboardButton("⬅️ Back to Moodle Menu", callback_data="moodle:menu")],
            ]
        )
    else:
        text = f"📋 *My Moodle Sessions*\n\n*Total Active Sessions:* {len(sessions)}\n\n"

        # Show sessions for current page
        per_page = 5
        total_pages = (len(sessions) + per_page - 1) // per_page
        start_idx = page * per_page
        end_idx = start_idx + per_page
        page_sessions = sessions[start_idx:end_idx]

        for idx, session in enumerate(page_sessions):
            display_idx = start_idx + idx
            text += format_session_info(session, display_idx) + "\n\n"

        if total_pages > 1:
            text += f"\n*Page {page + 1} of {total_pages}*"

        keyboard = get_sessions_list_keyboard(sessions, page=page)

    try:
        if query.message.photo:
            await query.message.delete()
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=text,
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
        else:
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def show_session_details(
    db, user_id: int, session_id: int, query, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show details for a specific session."""
    session = get_session_refresh_by_id(db, session_id)

    if not session or session.user_id != user_id:
        text = "❌ Session not found or you don't have permission to view it."
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Back to Sessions", callback_data="moodle:sessions")]]
        )
        try:
            await query.edit_message_text(text, reply_markup=keyboard)
        except BadRequest:
            pass
        return

    # Calculate duration
    if session.ended_at:
        duration = session.ended_at - session.started_at
    else:
        duration = datetime.utcnow() - session.started_at

    hours, remainder = divmod(int(duration.total_seconds()), 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours > 0:
        duration_str = f"{hours}h {minutes}m {seconds}s"
    elif minutes > 0:
        duration_str = f"{minutes}m {seconds}s"
    else:
        duration_str = f"{seconds}s"

    name = session.name or f"Session {session.id}"
    status_emoji = "🟢" if session.status == "running" else "🔴"

    # Show first 12 characters of moodleSession
    moodle_session_preview = (
        session.moodleSession[:12] + "..."
        if len(session.moodleSession) > 12
        else session.moodleSession
    )

    # Format timestamps in configured timezone
    started_at_dt = session.started_at
    if started_at_dt.tzinfo is None:
        started_at_dt = started_at_dt.replace(tzinfo=timezone.utc)
    started_str = format_time_for_display(started_at_dt)

    text = (
        f"{status_emoji} *{name}*\n\n"
        f"*Session ID:* `{session.id}`\n"
        f"*Status:* {session.status}\n"
        f"*Started:* {started_str}\n"
    )

    if session.ended_at:
        ended_at_dt = session.ended_at
        if ended_at_dt.tzinfo is None:
            ended_at_dt = ended_at_dt.replace(tzinfo=timezone.utc)
        ended_str = format_time_for_display(ended_at_dt)
        text += f"*Ended:* {ended_str}\n"

    text += f"*Duration:* {duration_str}\n" f"*Moodle Session:* `{moodle_session_preview}`\n"

    if session.moodle_user_id:
        text += f"*Moodle User ID:* {session.moodle_user_id}\n"

    keyboard_buttons = []

    # Edit and Stop buttons
    keyboard_buttons.append(
        [InlineKeyboardButton("✏️ Edit Name", callback_data=f"moodle:edit_name:{session.id}")]
    )

    if session.status == "running":
        keyboard_buttons.append(
            [
                InlineKeyboardButton(
                    "🛑 Stop Session", callback_data=f"moodle:stop_session:{session.id}"
                )
            ]
        )

    keyboard_buttons.append(
        [InlineKeyboardButton("⬅️ Back to Sessions", callback_data="moodle:sessions")]
    )

    keyboard = InlineKeyboardMarkup(keyboard_buttons)

    try:
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def stop_session_callback(
    db, user_id: int, session_id: int, query, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle stopping a session."""
    session = get_session_refresh_by_id(db, session_id)

    if not session or session.user_id != user_id:
        await query.answer("❌ Session not found or you don't have permission.", show_alert=True)
        return

    if session.status != "running":
        await query.answer("❌ Session is not running.", show_alert=True)
        return

    stopped_session = stop_session_refresh(db, session_id, status="stopped")

    if stopped_session:
        # Calculate duration
        if stopped_session.duration_seconds:
            hours, remainder = divmod(stopped_session.duration_seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            duration_str = f"{hours}h {minutes}m {seconds}s"
        else:
            duration_str = "Unknown"

        session_name = stopped_session.name or f"Session {stopped_session.id}"
        await query.answer(f"✅ Session '{session_name}' stopped. Duration: {duration_str}")

        # Show updated session details
        await show_session_details(db, user_id, session_id, query, context)
    else:
        await query.answer("❌ Failed to stop session.", show_alert=True)


async def edit_session_name_callback(
    db, user_id: int, session_id: int, query, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle editing session name - show input prompt."""
    session = get_session_refresh_by_id(db, session_id)

    if not session or session.user_id != user_id:
        await query.answer("❌ Session not found or you don't have permission.", show_alert=True)
        return

    # Store session_id in context for message handler
    context.user_data["editing_session_name"] = session_id
    context.user_data["editing_session_user_id"] = user_id

    current_name = session.name or f"Session {session.id}"

    text = (
        f"✏️ *Edit Session Name*\n\n"
        f"*Current name:* {current_name}\n\n"
        f"Please send the new name for this session.\n\n"
        f"Send `/cancel` to cancel."
    )

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Cancel", callback_data=f"moodle:session:{session_id}")]]
    )

    try:
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
        await query.answer("Please send the new session name")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def handle_session_name_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Handle text input for editing session name."""
    if not update.message or not update.message.text:
        return False

    if "editing_session_name" not in context.user_data:
        return False

    session_id = context.user_data.get("editing_session_name")
    user_id = context.user_data.get("editing_session_user_id")

    if not session_id or not user_id:
        return False

    # Handle cancel command
    text = update.message.text.strip()
    if text.lower() in ["/cancel", "cancel"]:
        context.user_data.pop("editing_session_name", None)
        context.user_data.pop("editing_session_user_id", None)
        await update.message.reply_text("❌ Cancelled editing session name.")
        return True

    new_name = text

    # Validate name length
    if len(new_name) > 50:
        await update.message.reply_text("❌ Session name is too long. Maximum 50 characters.")
        return True

    if not new_name:
        await update.message.reply_text("❌ Session name cannot be empty.")
        return True

    with SessionLocal() as db:
        session = get_session_refresh_by_id(db, session_id)

        if not session or session.user_id != user_id:
            await update.message.reply_text("❌ Session not found or you don't have permission.")
            context.user_data.pop("editing_session_name", None)
            context.user_data.pop("editing_session_user_id", None)
            return True

        updated_session = update_session_name(db, session_id, new_name)

        if updated_session:
            # Show updated session details directly
            session = get_session_refresh_by_id(db, session_id)
            if session:
                # Calculate duration
                if session.ended_at:
                    duration = session.ended_at - session.started_at
                else:
                    # Ensure started_at is timezone-aware for duration calculation
                    started_at = session.started_at
                    if started_at.tzinfo is None:
                        started_at = started_at.replace(tzinfo=timezone.utc)
                    duration = datetime.now(timezone.utc) - started_at

                hours, remainder = divmod(int(duration.total_seconds()), 3600)
                minutes, seconds = divmod(remainder, 60)

                if hours > 0:
                    duration_str = f"{hours}h {minutes}m {seconds}s"
                elif minutes > 0:
                    duration_str = f"{minutes}m {seconds}s"
                else:
                    duration_str = f"{seconds}s"

                name = session.name or f"Session {session.id}"
                status_emoji = "🟢" if session.status == "running" else "🔴"
                moodle_session_preview = (
                    session.moodleSession[:12] + "..."
                    if len(session.moodleSession) > 12
                    else session.moodleSession
                )

                # Format timestamps in configured timezone
                started_at_dt = session.started_at
                if started_at_dt.tzinfo is None:
                    started_at_dt = started_at_dt.replace(tzinfo=timezone.utc)
                started_str = format_time_for_display(started_at_dt)

                text = (
                    f"✅ *Session name updated!*\n\n"
                    f"{status_emoji} *{name}*\n\n"
                    f"*Session ID:* `{session.id}`\n"
                    f"*Status:* {session.status}\n"
                    f"*Started:* {started_str}\n"
                )

                if session.ended_at:
                    ended_at_dt = session.ended_at
                    if ended_at_dt.tzinfo is None:
                        ended_at_dt = ended_at_dt.replace(tzinfo=timezone.utc)
                    ended_str = format_time_for_display(ended_at_dt)
                    text += f"*Ended:* {ended_str}\n"

                text += (
                    f"*Duration:* {duration_str}\n"
                    f"*Moodle Session:* `{moodle_session_preview}`\n"
                )

                if session.moodle_user_id:
                    text += f"*Moodle User ID:* {session.moodle_user_id}\n"

                keyboard_buttons = [
                    [
                        InlineKeyboardButton(
                            "✏️ Edit Name", callback_data=f"moodle:edit_name:{session.id}"
                        )
                    ]
                ]

                if session.status == "running":
                    keyboard_buttons.append(
                        [
                            InlineKeyboardButton(
                                "🛑 Stop Session", callback_data=f"moodle:stop_session:{session.id}"
                            )
                        ]
                    )

                keyboard_buttons.append(
                    [InlineKeyboardButton("⬅️ Back to Sessions", callback_data="moodle:sessions")]
                )

                keyboard = InlineKeyboardMarkup(keyboard_buttons)

                await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")
            else:
                await update.message.reply_text(
                    f"✅ Session name updated to: *{new_name}*", parse_mode="Markdown"
                )
        else:
            await update.message.reply_text("❌ Failed to update session name.")

    # Clear editing state
    context.user_data.pop("editing_session_name", None)
    context.user_data.pop("editing_session_user_id", None)

    return True


async def sessions_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle callback queries for session management.

    Handles callbacks with pattern: moodle:sessions:{action}:{value}
    Currently handles:
    - moodle:sessions:page:{page_number} - pagination
    """
    query = update.callback_query
    if not query:
        return

    # Check permission
    if not await require_module_permission(update, context, "moodle"):
        return

    await query.answer()

    data = query.data.split(":")

    # Expected format: moodle:sessions:{action}:{value}
    if len(data) < 3:
        return

    action = data[2]

    with SessionLocal() as db:
        user = get_user_by_telegram_id(db, query.from_user.id)
        if not user:
            try:
                await query.edit_message_text("❌ User not found.")
            except BadRequest:
                pass
            return

        if action == "page":
            # Handle pagination: moodle:sessions:page:{page_number}
            if len(data) < 4:
                return
            try:
                page = int(data[3])
                await show_sessions_list(db, user.id, query, context, page=page)
            except (ValueError, IndexError):
                await query.answer("❌ Invalid page number.", show_alert=True)
        else:
            # Unknown action
            await query.answer(f"❌ Unknown action: {action}", show_alert=True)
