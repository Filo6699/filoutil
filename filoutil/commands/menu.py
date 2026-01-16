import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from filoutil.auth import ensure_user_and_check_whitelisted
from filoutil.db.postgres import SessionLocal
from filoutil.db.session_refresh import get_active_session_refresh
from filoutil.db.users import get_user_by_telegram_id

logger = logging.getLogger(__name__)


def get_main_menu_keyboard(user_permissions: list[str] = None) -> InlineKeyboardMarkup:
    """Generate the main menu keyboard based on user permissions."""
    keyboard = []

    # Add Monitors button only if user has monitoring permission
    if user_permissions and "monitoring" in user_permissions:
        keyboard.append([InlineKeyboardButton("📊 Monitors", callback_data="menu:monitors")])

    # Add Moodle button only if user has moodle permission
    if user_permissions and "moodle" in user_permissions:
        keyboard.append([InlineKeyboardButton("🎓 Moodle", callback_data="menu:moodle")])

    # Always show help
    keyboard.append([InlineKeyboardButton("ℹ️ Help", callback_data="menu:help")])

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
            text, reply_markup=get_main_menu_keyboard(user_permissions), parse_mode="Markdown"
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
                    reply_markup=get_main_menu_keyboard(user_permissions),
                    parse_mode="Markdown",
                )
            else:
                await query.edit_message_text(
                    text,
                    reply_markup=get_main_menu_keyboard(user_permissions),
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

    elif action == "help":
        text = (
            "ℹ️ *Help*\n\n"
            "*Available Commands:*\n\n"
            "• `/menu` - Show main menu\n"
            "• `/monitor` or `/m` - Manage service monitors\n"
            "• `/moodle` - Open Moodle menu\n"
            "• `/moodle_add` - Add a new Moodle session\n"
            "• `/refresh_session` - Start LMS session refresh (legacy)\n"
            "• `/refresh_settings` - Configure refresh interval\n"
            "• `/notifications` - View Moodle notifications\n"
            "• `/notification_settings` - Configure notification settings\n\n"
            "*Features:*\n\n"
            "📊 *Monitors* - Monitor your services and websites\n"
            "🎓 *Moodle* - Manage multiple Moodle sessions (up to 2-3)\n"
            "🔄 *Session Refresh* - Keep your LMS sessions alive automatically\n"
            "🔔 *Notifications* - View and manage Moodle notifications\n"
            "⚙️ *Settings* - Configure refresh intervals and notifications\n\n"
            "Use the menu buttons to navigate!"
        )
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu:main")]]
        )
        try:
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise

    elif action == "moodle":
        # Check permission
        if "moodle" not in user_permissions:
            await query.answer("❌ You don't have permission to access Moodle.", show_alert=True)
            return

        # Show Moodle menu
        from filoutil.commands.moodle.menu import show_moodle_menu

        await show_moodle_menu(query, context)

    elif action == "main":
        await show_main_menu(query, context)
