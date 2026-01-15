"""Commands for viewing and managing Moodle notifications."""

import logging
from datetime import datetime

from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from filoutil.auth import ensure_user_and_check_whitelisted
from filoutil.commands.notification_settings import get_user_notification_settings
from filoutil.db.models import MoodleNotification
from filoutil.db.moodle_notifications import get_notification_by_moodle_id, get_user_notifications
from filoutil.db.postgres import SessionLocal
from filoutil.db.users import get_user_by_telegram_id
from filoutil.utils.keyboard import arrange_buttons_in_rows

logger = logging.getLogger(__name__)

DEFAULT_NOTIFICATIONS_PER_PAGE = 10


def format_notification_preview(notification, index: int) -> str:
    """Format a notification for list display."""
    time_str = notification.timecreatedpretty or datetime.fromtimestamp(
        notification.timecreated
    ).strftime("%Y-%m-%d %H:%M")

    subject = notification.shortenedsubject or notification.subject or "No subject"
    # Truncate if too long
    if len(subject) > 50:
        subject = subject[:47] + "..."

    return f"*{index}.* {subject}\n   ⏰ {time_str}"


def get_notifications_keyboard(
    page: int,
    total_pages: int,
    page_notifications: list | None = None,
    notifications_per_page: int = DEFAULT_NOTIFICATIONS_PER_PAGE,
) -> InlineKeyboardMarkup:
    """Generate keyboard for notifications list with pagination."""
    keyboard = []

    # Add buttons for each notification on current page (for viewing details)
    # Note: Telegram has a 64-byte limit for callback_data, so we keep it minimal
    # Make buttons as small as possible - just the number
    # Arrange them in rows for compact display
    if page_notifications:
        notification_buttons = []
        for idx, notification in enumerate(page_notifications):
            start_idx = page * notifications_per_page
            notification_index = start_idx + idx + 1
            # Use just the number for smallest possible button
            notification_buttons.append(
                InlineKeyboardButton(
                    str(notification_index),
                    callback_data=f"notif:detail:{notification.id}:{page}",
                )
            )
        # Arrange notification buttons in rows (5 per row for compact display)
        notification_rows = arrange_buttons_in_rows(notification_buttons, buttons_per_row=5)
        keyboard.extend(notification_rows)

    # Pagination buttons
    if total_pages > 1:
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton("◀️ Prev", callback_data=f"notif:page:{page - 1}"))
        nav_row.append(
            InlineKeyboardButton(f"Page {page + 1}/{total_pages}", callback_data="notif:noop")
        )
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"notif:page:{page + 1}"))
        keyboard.append(nav_row)

    # Back button
    keyboard.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu:main")])

    return InlineKeyboardMarkup(keyboard)


def get_notification_detail_keyboard(notification_id: int, page: int) -> InlineKeyboardMarkup:
    """Generate keyboard for notification detail view."""
    keyboard = [
        [InlineKeyboardButton("⬅️ Back to List", callback_data=f"notif:page:{page}")],
        [InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu:main")],
    ]
    return InlineKeyboardMarkup(keyboard)


async def notifications_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /notifications command to show notifications list."""
    if not await ensure_user_and_check_whitelisted(update, context):
        return

    with SessionLocal() as db:
        user = get_user_by_telegram_id(db, update.message.from_user.id)
        if not user:
            await update.message.reply_text("❌ User not found.")
            return

        await show_notifications_list(db, user.id, update.message, context, page=0)


async def show_notifications_list(
    db,
    user_id: int,
    message,
    context: ContextTypes.DEFAULT_TYPE,
    page: int = 0,
) -> None:
    """Show paginated list of notifications."""
    # Get user's notifications per page setting
    settings = get_user_notification_settings(db, user_id)
    notifications_per_page = settings.get("notifications_per_page", DEFAULT_NOTIFICATIONS_PER_PAGE)

    # Get all notifications
    all_notifications = get_user_notifications(db, user_id, limit=1000, offset=0)
    notifications = [n for n in all_notifications if not n.deleted]

    total_count = len(notifications)
    total_pages = (
        (total_count + notifications_per_page - 1) // notifications_per_page
        if total_count > 0
        else 1
    )

    if page >= total_pages:
        page = max(0, total_pages - 1)

    # Get page slice
    start_idx = page * notifications_per_page
    end_idx = start_idx + notifications_per_page
    page_notifications = notifications[start_idx:end_idx]

    # Format message
    text = f"🔔 *Moodle Notifications*\n\n"
    text += f"*Total:* {total_count} notifications\n\n"

    if not page_notifications:
        text += "No notifications found."
    else:
        text += "*Notifications:*\n\n"
        for idx, notification in enumerate(page_notifications, start=start_idx + 1):
            text += format_notification_preview(notification, idx) + "\n\n"

    # Pass notifications_per_page to keyboard generator
    keyboard = get_notifications_keyboard(
        page,
        total_pages,
        page_notifications=page_notifications,
        notifications_per_page=notifications_per_page,
    )

    try:
        if hasattr(message, "edit_message_text"):
            await message.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
        else:
            await message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def show_notification_detail(
    db,
    notification_id: int,
    message,
    context: ContextTypes.DEFAULT_TYPE,
    page: int,
) -> None:
    """Show detailed view of a single notification."""
    notification = db.execute(
        select(MoodleNotification).where(MoodleNotification.id == notification_id)
    ).scalar_one_or_none()

    if not notification:
        await message.edit_message_text("❌ Notification not found.")
        return

    # Format detailed message
    time_str = notification.timecreatedpretty or datetime.fromtimestamp(
        notification.timecreated
    ).strftime("%Y-%m-%d %H:%M:%S")

    text = f"🔔 *Notification Details*\n\n"
    text += f"*Time:* {time_str}\n\n"
    text += f"*Subject:*\n{notification.subject}\n\n"

    if notification.smallmessage:
        text += f"*Message:*\n{notification.smallmessage}\n\n"

    if notification.contexturl:
        text += f"🔗 [View in Moodle]({notification.contexturl})\n\n"

    if notification.component:
        text += f"*Component:* {notification.component}\n"
    if notification.eventtype:
        text += f"*Event Type:* {notification.eventtype}\n"

    keyboard = get_notification_detail_keyboard(notification_id, page)

    try:
        await message.edit_message_text(
            text, reply_markup=keyboard, parse_mode="Markdown", disable_web_page_preview=True
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def notifications_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle callback queries for notifications."""
    query = update.callback_query
    if not query:
        return

    await query.answer()

    data = query.data.split(":")
    action = data[1]

    with SessionLocal() as db:
        user = get_user_by_telegram_id(db, query.from_user.id)
        if not user:
            try:
                await query.edit_message_text("❌ User not found.")
            except BadRequest:
                pass
            return

        if action == "page":
            page = int(data[2])
            await show_notifications_list(db, user.id, query, context, page)

        elif action == "detail":
            notification_id = int(data[2])
            page = int(data[3]) if len(data) > 3 else 0
            await show_notification_detail(db, notification_id, query, context, page)

        elif action == "noop":
            # Do nothing, just acknowledge
            pass
