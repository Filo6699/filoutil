"""Moodle menu commands and callbacks."""

import logging
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from filoutil.auth import require_module_permission
from filoutil.db.postgres import SessionLocal
from filoutil.db.session_refresh import get_active_sessions_for_user, get_user_refresh_interval
from filoutil.db.users import get_user_by_telegram_id

logger = logging.getLogger(__name__)


def get_moodle_menu_keyboard() -> InlineKeyboardMarkup:
    """Generate the Moodle menu keyboard."""
    keyboard = [
        [
            InlineKeyboardButton("📋 My Sessions", callback_data="moodle:sessions"),
            InlineKeyboardButton("➕ Add Session", callback_data="moodle:add_session"),
        ],
        [InlineKeyboardButton("🔔 Notifications", callback_data="moodle:notifications")],
        [
            InlineKeyboardButton("⚙️ Refresh Settings", callback_data="moodle:refresh_settings"),
            InlineKeyboardButton(
                "⚙️ Notification Settings", callback_data="moodle:notification_settings"
            ),
        ],
        [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="menu:main")],
    ]
    return InlineKeyboardMarkup(keyboard)


async def moodle_menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /moodle command to show the Moodle menu."""
    if not await require_module_permission(update, context, "moodle"):
        return

    with SessionLocal() as db:
        user = get_user_by_telegram_id(db, update.message.from_user.id)
        if not user:
            await update.message.reply_text("❌ User not found.")
            return

        active_sessions = get_active_sessions_for_user(db, user.id)
        session_count = len(active_sessions)

        text = (
            f"🎓 *Moodle Menu*\n\n"
            f"Manage your Moodle sessions and notifications.\n\n"
            f"*Active Sessions:* {session_count}"
        )

        await update.message.reply_text(
            text, reply_markup=get_moodle_menu_keyboard(), parse_mode="Markdown"
        )


async def show_moodle_menu(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Helper function to show the Moodle menu."""
    with SessionLocal() as db:
        user = get_user_by_telegram_id(db, query.from_user.id)
        if not user:
            return

        active_sessions = get_active_sessions_for_user(db, user.id)
        session_count = len(active_sessions)

        text = (
            f"🎓 *Moodle Menu*\n\n"
            f"Manage your Moodle sessions and notifications.\n\n"
            f"*Active Sessions:* {session_count}"
        )

        try:
            if query.message.photo:
                await query.message.delete()
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=text,
                    reply_markup=get_moodle_menu_keyboard(),
                    parse_mode="Markdown",
                )
            else:
                await query.edit_message_text(
                    text, reply_markup=get_moodle_menu_keyboard(), parse_mode="Markdown"
                )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise


def format_session_info(session, index: int = None) -> str:
    """Format session information for display."""
    name = session.name or f"Session {session.id}"
    index_str = f"{index + 1}. " if index is not None else ""

    # Calculate duration
    if session.ended_at:
        duration = session.ended_at - session.started_at
    else:
        duration = datetime.utcnow() - session.started_at

    hours, remainder = divmod(int(duration.total_seconds()), 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours > 0:
        duration_str = f"{hours}h {minutes}m"
    elif minutes > 0:
        duration_str = f"{minutes}m {seconds}s"
    else:
        duration_str = f"{seconds}s"

    status_emoji = "🟢" if session.status == "running" else "🔴"

    # Show first 8 characters of moodleSession
    moodle_session_preview = (
        session.moodleSession[:8] + "..."
        if len(session.moodleSession) > 8
        else session.moodleSession
    )

    return (
        f"{index_str}{status_emoji} *{name}*\n"
        f"   ID: `{session.id}`\n"
        f"   Status: {session.status}\n"
        f"   Duration: {duration_str}\n"
        f"   Refresh: {session.refresh_interval_s // 60}m\n"
        f"   Session: `{moodle_session_preview}`"
    )


def get_sessions_list_keyboard(
    sessions: list, page: int = 0, per_page: int = 5
) -> InlineKeyboardMarkup:
    """Generate keyboard for sessions list."""
    keyboard = []

    # Pagination
    total_pages = (len(sessions) + per_page - 1) // per_page if sessions else 1
    start_idx = page * per_page
    end_idx = start_idx + per_page
    page_sessions = sessions[start_idx:end_idx]

    # Session buttons
    for idx, session in enumerate(page_sessions):
        display_idx = start_idx + idx
        name = session.name or f"Session {session.id}"
        status_emoji = "🟢" if session.status == "running" else "🔴"
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"{status_emoji} {name}", callback_data=f"moodle:session:{session.id}"
                )
            ]
        )

    # Pagination buttons
    nav_buttons = []
    if page > 0:
        nav_buttons.append(
            InlineKeyboardButton("⬅️ Previous", callback_data=f"moodle:sessions:page:{page - 1}")
        )
    if page < total_pages - 1:
        nav_buttons.append(
            InlineKeyboardButton("Next ➡️", callback_data=f"moodle:sessions:page:{page + 1}")
        )
    if nav_buttons:
        keyboard.append(nav_buttons)

    # Back button
    keyboard.append([InlineKeyboardButton("⬅️ Back to Moodle Menu", callback_data="moodle:menu")])

    return InlineKeyboardMarkup(keyboard)


async def moodle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle callback queries for the Moodle menu."""
    query = update.callback_query
    if not query:
        return

    # Check permission
    if not await require_module_permission(update, context, "moodle"):
        return

    await query.answer()

    data = query.data.split(":")
    action = data[1]

    if action == "menu":
        await show_moodle_menu(query, context)

    elif action == "sessions":
        # Show sessions list
        from filoutil.commands.moodle.sessions import show_sessions_list

        with SessionLocal() as db:
            user = get_user_by_telegram_id(db, query.from_user.id)
            if not user:
                try:
                    await query.edit_message_text("❌ User not found.")
                except BadRequest:
                    pass
                return

            page = int(data[2]) if len(data) > 2 and data[2] == "page" else 0
            if len(data) > 3 and data[2] == "page":
                page = int(data[3])

            await show_sessions_list(db, user.id, query, context, page=page)

    elif action == "add_session":
        # Check current session count and show instructions
        with SessionLocal() as db:
            user = get_user_by_telegram_id(db, query.from_user.id)
            if not user:
                try:
                    await query.edit_message_text("❌ User not found.")
                except BadRequest:
                    pass
                return

            active_sessions = get_active_sessions_for_user(db, user.id)
            session_count = len(active_sessions)
            max_sessions = 5

            if session_count >= max_sessions:
                text = (
                    f"➕ *Add Moodle Session*\n\n"
                    f"❌ You already have {session_count} active sessions (maximum: {max_sessions}).\n\n"
                    f"Please stop a session before adding a new one.\n\n"
                    f'Use "📋 My Sessions" to manage your sessions.'
                )
            else:
                text = (
                    f"➕ *Add Moodle Session*\n\n"
                    f"*Current sessions:* {session_count}/{max_sessions}\n\n"
                    f"To add a new Moodle session, send:\n\n"
                    '`/moodle_add {"sesskey": "...", "moodleSession": "...", "name": "Optional Name"}`\n\n'
                    "Or use the command directly with your session data.\n\n"
                    "*Note:* You can have up to 2-3 active sessions at the same time."
                )

            keyboard = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("📋 My Sessions", callback_data="moodle:sessions")],
                    [InlineKeyboardButton("⬅️ Back to Moodle Menu", callback_data="moodle:menu")],
                ]
            )
            try:
                await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise

    elif action == "refresh_settings":
        # Show refresh settings
        from filoutil.commands.session_refresh import get_settings_keyboard

        with SessionLocal() as db:
            user = get_user_by_telegram_id(db, query.from_user.id)
            if not user:
                try:
                    await query.edit_message_text("❌ User not found.")
                except BadRequest:
                    pass
                return

            current_interval = get_user_refresh_interval(db, user.id)
            minutes = current_interval // 60

            text = (
                f"⚙️ *Session Refresh Settings*\n\n"
                f"*Current refresh interval:* {minutes}m ({current_interval}s)\n\n"
                f"Select a new interval:"
            )

            keyboard = get_settings_keyboard(current_interval)
            # Replace back button - convert tuple to list, modify, then create new keyboard
            keyboard_list = list(keyboard.inline_keyboard)
            keyboard_list[-1] = [
                InlineKeyboardButton("⬅️ Back to Moodle Menu", callback_data="moodle:menu")
            ]
            keyboard = InlineKeyboardMarkup(keyboard_list)

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
                    await query.edit_message_text(
                        text, reply_markup=keyboard, parse_mode="Markdown"
                    )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise

    elif action == "notifications":
        # Show notifications list
        from filoutil.commands.notifications import show_notifications_list

        with SessionLocal() as db:
            user = get_user_by_telegram_id(db, query.from_user.id)
            if not user:
                try:
                    await query.edit_message_text("❌ User not found.")
                except BadRequest:
                    pass
                return

            await show_notifications_list(db, user.id, query, context, page=0)

    elif action == "notification_settings":
        # Show notification settings
        from filoutil.commands.notification_settings import (
            get_user_notification_settings,
            show_notification_settings,
        )

        with SessionLocal() as db:
            user = get_user_by_telegram_id(db, query.from_user.id)
            if not user:
                try:
                    await query.edit_message_text("❌ User not found.")
                except BadRequest:
                    pass
                return

            settings = get_user_notification_settings(db, user.id)
            await show_notification_settings(query, context, settings)

    elif action == "session":
        # Show individual session details
        from filoutil.commands.moodle.sessions import show_session_details

        if len(data) < 3:
            return

        session_id = int(data[2])

        with SessionLocal() as db:
            user = get_user_by_telegram_id(db, query.from_user.id)
            if not user:
                try:
                    await query.edit_message_text("❌ User not found.")
                except BadRequest:
                    pass
                return

            await show_session_details(db, user.id, session_id, query, context)

    elif action == "stop_session":
        # Handle stopping a session
        from filoutil.commands.moodle.sessions import stop_session_callback

        if len(data) < 3:
            return

        session_id = int(data[2])

        with SessionLocal() as db:
            user = get_user_by_telegram_id(db, query.from_user.id)
            if not user:
                try:
                    await query.edit_message_text("❌ User not found.")
                except BadRequest:
                    pass
                return

            await stop_session_callback(db, user.id, session_id, query, context)

    elif action == "edit_name":
        # Handle editing session name
        from filoutil.commands.moodle.sessions import edit_session_name_callback

        if len(data) < 3:
            return

        session_id = int(data[2])

        with SessionLocal() as db:
            user = get_user_by_telegram_id(db, query.from_user.id)
            if not user:
                try:
                    await query.edit_message_text("❌ User not found.")
                except BadRequest:
                    pass
                return

            await edit_session_name_callback(db, user.id, session_id, query, context)
