import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from filoutil.auth import ensure_user_and_check_whitelisted
from filoutil.db.postgres import SessionLocal
from filoutil.db.session_refresh import get_active_session_refresh
from filoutil.db.users import get_user_by_telegram_id

logger = logging.getLogger(__name__)


def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    """Generate the main menu keyboard."""
    keyboard = [
        [InlineKeyboardButton("📊 Monitors", callback_data="menu:monitors")],
        [
            InlineKeyboardButton("🔄 Session Refresh", callback_data="menu:refresh_session"),
            InlineKeyboardButton("⚙️ Refresh Settings", callback_data="menu:refresh_settings"),
        ],
        [
            InlineKeyboardButton("🔔 Notifications", callback_data="menu:notifications"),
            InlineKeyboardButton(
                "⚙️ Notification Settings", callback_data="menu:notification_settings"
            ),
        ],
        [
            InlineKeyboardButton("⏰ Reminder Settings", callback_data="menu:reminder_settings"),
        ],
        [InlineKeyboardButton("ℹ️ Help", callback_data="menu:help")],
    ]
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

        # Check if user has active session refresh
        active_session = get_active_session_refresh(db, user.id)
        session_status = "🟢 Active" if active_session else "⚪ Inactive"

        text = (
            f"🏠 *Main Menu*\n\n"
            f"Welcome! Choose an option from the menu below.\n\n"
            f"*Session Refresh Status:* {session_status}"
        )

        await update.message.reply_text(
            text, reply_markup=get_main_menu_keyboard(), parse_mode="Markdown"
        )


async def show_main_menu(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Helper function to show the main menu."""
    with SessionLocal() as db:
        user = get_user_by_telegram_id(db, query.from_user.id)
        if not user:
            return

        active_session = get_active_session_refresh(db, user.id)
        session_status = "🟢 Active" if active_session else "⚪ Inactive"

        text = (
            f"🏠 *Main Menu*\n\n"
            f"Welcome! Choose an option from the menu below.\n\n"
            f"*Session Refresh Status:* {session_status}"
        )

        try:
            if query.message.photo:
                await query.message.delete()
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=text,
                    reply_markup=get_main_menu_keyboard(),
                    parse_mode="Markdown",
                )
            else:
                await query.edit_message_text(
                    text, reply_markup=get_main_menu_keyboard(), parse_mode="Markdown"
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

    if action == "monitors":
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

    elif action == "reminder_settings":
        # Show reminder settings directly
        with SessionLocal() as db:
            user = get_user_by_telegram_id(db, query.from_user.id)
            if not user:
                try:
                    await query.edit_message_text("❌ User not found.")
                except BadRequest:
                    pass
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

            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu:main")]]
            )

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

    elif action == "help":
        text = (
            "ℹ️ *Help*\n\n"
            "*Available Commands:*\n\n"
            "• `/menu` - Show main menu\n"
            "• `/monitor` or `/m` - Manage service monitors\n"
            "• `/refresh_session` - Start LMS session refresh\n"
            "• `/refresh_settings` - Configure refresh interval\n"
            "• `/notifications` - View Moodle notifications\n"
            "• `/notification_settings` - Configure notification settings\n"
            "• `/reminder_settings` - Configure reminder settings\n\n"
            "*Features:*\n\n"
            "📊 *Monitors* - Monitor your services and websites\n"
            "🔄 *Session Refresh* - Keep your LMS session alive automatically\n"
            "🔔 *Notifications* - View and manage Moodle notifications\n"
            "⚙️ *Settings* - Configure refresh intervals and notifications\n"
            "⏰ *Reminders* - Get notified about active sessions\n\n"
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

    elif action == "main":
        await show_main_menu(query, context)
