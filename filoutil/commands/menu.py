import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown

from filoutil.auth import ensure_user_and_check_whitelisted
from filoutil.db.postgres import SessionLocal
from filoutil.db.session_refresh import get_active_session_refresh
from filoutil.db.users import get_user_by_telegram_id

logger = logging.getLogger(__name__)


def get_main_menu_keyboard(
    user_permissions: list[str] = None, user_role: str = "user"
) -> InlineKeyboardMarkup:
    """Generate the main menu keyboard based on user permissions."""
    keyboard = []

    # Add Monitors button only if user has monitoring permission
    if user_permissions and "monitoring" in user_permissions:
        keyboard.append([InlineKeyboardButton("🎛 Monitors", callback_data="menu:monitors")])

    # Add Moodle button only if user has moodle permission
    if user_permissions and "moodle" in user_permissions:
        keyboard.append([InlineKeyboardButton("🎓 Moodle", callback_data="menu:moodle")])

    # Add Admin button only if user is an admin
    if user_role == "admin":
        keyboard.append([InlineKeyboardButton("👑 Admin", callback_data="menu:admin")])

    return InlineKeyboardMarkup(keyboard)


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /menu command to show the main menu."""
    if not await ensure_user_and_check_whitelisted(update, context):
        return

    with SessionLocal() as db:
        user = get_user_by_telegram_id(db, update.message.from_user.id)
        if not user:
            await update.message.reply_text("❌ User not found.")
            return

        # Get user permissions
        from filoutil.db.permissions import get_user_permissions

        user_permissions = get_user_permissions(db, user.id)

        # Check if user has active session refresh (only if has moodle permission)
        if "moodle" in user_permissions:
            from filoutil.db.session_refresh import get_active_sessions_for_user

            active_sessions = get_active_sessions_for_user(db, user.id)
            session_count = len(active_sessions)
            session_status = f"{session_count} active" if session_count > 0 else "None"

            text = (
                f"🏠 *Main Menu*\n\n"
                f"Welcome! Choose an option from the menu below.\n\n"
                f"*Moodle Sessions:* {session_status}"
            )
        else:
            text = f"🏠 *Main Menu*\n\n" f"Welcome! Choose an option from the menu below."

        await update.message.reply_text(
            text,
            reply_markup=get_main_menu_keyboard(user_permissions, user.role),
            parse_mode="Markdown",
        )


async def show_main_menu(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Helper function to show the main menu."""
    with SessionLocal() as db:
        user = get_user_by_telegram_id(db, query.from_user.id)
        if not user:
            return

        # Get user permissions
        from filoutil.db.permissions import get_user_permissions

        user_permissions = get_user_permissions(db, user.id)

        # Check if user has active session refresh (only if has moodle permission)
        if "moodle" in user_permissions:
            from filoutil.db.session_refresh import get_active_sessions_for_user

            active_sessions = get_active_sessions_for_user(db, user.id)
            session_count = len(active_sessions)
            session_status = f"{session_count} active" if session_count > 0 else "None"

            text = (
                f"🏠 *Main Menu*\n\n"
                f"Welcome! Choose an option from the menu below.\n\n"
                f"*Moodle Sessions:* {session_status}"
            )
        else:
            text = f"🏠 *Main Menu*\n\n" f"Welcome! Choose an option from the menu below."

        try:
            if query.message.photo:
                await query.message.delete()
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=text,
                    reply_markup=get_main_menu_keyboard(user_permissions, user.role),
                    parse_mode="Markdown",
                )
            else:
                await query.edit_message_text(
                    text,
                    reply_markup=get_main_menu_keyboard(user_permissions, user.role),
                    parse_mode="Markdown",
                )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle callback queries for the main menu."""
    query = update.callback_query
    if not query:
        return

    await query.answer()

    data = query.data.split(":")
    action = data[1]

    # Get user permissions for gating actions
    with SessionLocal() as db:
        user = get_user_by_telegram_id(db, query.from_user.id)
        if not user:
            await query.answer("❌ User not found.", show_alert=True)
            return

        from filoutil.db.permissions import get_user_permissions

        user_permissions = get_user_permissions(db, user.id)

    if action == "monitors":
        # Check permission
        if "monitoring" not in user_permissions:
            await query.answer("❌ You don't have permission to access Monitors.", show_alert=True)
            return
        # Show monitor list directly
        from filoutil.commands.monitor import get_monitor_list_keyboard
        from filoutil.db.monitors import get_all_monitors

        with SessionLocal() as db:
            monitors = get_all_monitors(db)
            if not monitors:
                text = "📊 *Monitors*\n\n" "No monitors found. Use `+ Add Monitor` to create one."
                keyboard = InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("➕ Add Monitor", callback_data="mon:add_start")],
                        [InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu:main")],
                    ]
                )
            else:
                text = "📊 *Monitors*\n\nSelect a monitor to view details:"
                keyboard = get_monitor_list_keyboard(monitors)

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

    elif action == "refresh_session":
        # Show instructions for refresh_session
        text = (
            "🔄 *Session Refresh*\n\n"
            "To start a session refresh, send:\n\n"
            '`/refresh_session {"sesskey": "...", "moodleSession": "..."}`\n\n'
            "Or use the command directly with your session data."
        )
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu:main")]]
        )
        try:
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise

    elif action == "refresh_settings":
        # Show refresh settings directly
        from filoutil.commands.session_refresh import get_settings_keyboard
        from filoutil.db.session_refresh import get_user_refresh_interval

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

            try:
                if query.message.photo:
                    await query.message.delete()
                    await context.bot.send_message(
                        chat_id=query.message.chat_id,
                        text=text,
                        reply_markup=get_settings_keyboard(current_interval),
                        parse_mode="Markdown",
                    )
                else:
                    await query.edit_message_text(
                        text,
                        reply_markup=get_settings_keyboard(current_interval),
                        parse_mode="Markdown",
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

    elif action == "moodle":
        # Check permission
        if "moodle" not in user_permissions:
            await query.answer("❌ You don't have permission to access Moodle.", show_alert=True)
            return

        # Show Moodle menu
        from filoutil.commands.moodle.menu import show_moodle_menu

        await show_moodle_menu(query, context)

    elif action == "admin":
        # Check if user is admin
        with SessionLocal() as db:
            user = get_user_by_telegram_id(db, query.from_user.id)
            if not user or user.role != "admin":
                await query.answer("❌ This command is only available to admins.", show_alert=True)
                return

        # Show admin panel - directly show the user list
        from sqlalchemy import select

        from filoutil.commands.admin_users import USERS_PER_PAGE, get_user_list_keyboard
        from filoutil.db.models import User

        with SessionLocal() as db:
            users = db.execute(select(User).order_by(User.created_at.desc())).scalars().all()

            if not users:
                try:
                    await query.edit_message_text("No users found.")
                except BadRequest:
                    pass
                return

            total_users = len(users)
            page = 0
            total_pages = (total_users + USERS_PER_PAGE - 1) // USERS_PER_PAGE

            text = f"👥 *Admin Panel*\n\n*Total Users:* {total_users}\n\n*Page {page + 1} of {total_pages}*\n\n"

            start_idx = page * USERS_PER_PAGE
            end_idx = start_idx + USERS_PER_PAGE
            page_users = users[start_idx:end_idx]

            for user_item in page_users:
                username_display = escape_markdown(user_item.username or "No username", version=1)
                whitelist_status = "✅" if user_item.whitelisted else "❌"
                role_display = "👑 Admin" if user_item.role == "admin" else "👤 User"
                text += f"{whitelist_status} {role_display}: {username_display}\n"

            reply_markup = get_user_list_keyboard(users, page=page)

            try:
                await query.edit_message_text(
                    text, reply_markup=reply_markup, parse_mode="Markdown"
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise

    elif action == "main":
        await show_main_menu(query, context)
