"""Commands for viewing and managing Moodle notifications."""

import logging
from datetime import datetime
from typing import Literal

from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from filoutil.auth import ensure_user_and_check_whitelisted
from filoutil.commands.notification_settings import get_user_notification_settings
from filoutil.db.models import MoodleNotification
from filoutil.db.moodle_notifications import (
    get_notification_by_moodle_id,
    get_unread_notifications_count,
    get_user_notifications,
)
from filoutil.db.postgres import SessionLocal
from filoutil.db.users import get_user_by_telegram_id
from filoutil.utils.keyboard import arrange_buttons_in_rows

logger = logging.getLogger(__name__)

DEFAULT_NOTIFICATIONS_PER_PAGE = 10


def format_notification_preview(notification, index: int) -> str:
    """Format a notification for list display."""
    read_icon = "✅" if notification.read else "🔔"
    time_str = notification.timecreatedpretty or datetime.fromtimestamp(
        notification.timecreated
    ).strftime("%Y-%m-%d %H:%M")

    subject = notification.shortenedsubject or notification.subject or "No subject"
    # Truncate if too long
    if len(subject) > 50:
        subject = subject[:47] + "..."

    return f"{read_icon} *{index}.* {subject}\n   ⏰ {time_str}"


def get_notifications_keyboard(
    page: int,
    total_pages: int,
    filter_type: Literal["all", "unread", "read"] = "all",
    notification_id: int | None = None,
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
                    callback_data=f"notif:detail:{notification.id}:{page}:{filter_type}",
                )
            )
        # Arrange notification buttons in rows (5 per row for compact display)
        notification_rows = arrange_buttons_in_rows(notification_buttons, buttons_per_row=5)
        keyboard.extend(notification_rows)

    # Filter buttons
    filter_row = []
    for ftype, label, icon in [
        ("all", "All", "📋"),
        ("unread", "Unread", "🔔"),
        ("read", "Read", "✅"),
    ]:
        if filter_type == ftype:
            label = f"◉ {icon} {label}"
        else:
            label = f"○ {icon} {label}"
        filter_row.append(InlineKeyboardButton(label, callback_data=f"notif:filter:{ftype}:0"))
    keyboard.append(filter_row)

    # Pagination buttons
    if total_pages > 1:
        nav_row = []
        if page > 0:
            nav_row.append(
                InlineKeyboardButton("◀️ Prev", callback_data=f"notif:page:{filter_type}:{page - 1}")
            )
        nav_row.append(
            InlineKeyboardButton(f"Page {page + 1}/{total_pages}", callback_data="notif:noop")
        )
        if page < total_pages - 1:
            nav_row.append(
                InlineKeyboardButton("Next ▶️", callback_data=f"notif:page:{filter_type}:{page + 1}")
            )
        keyboard.append(nav_row)

    # Back button
    keyboard.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu:main")])

    return InlineKeyboardMarkup(keyboard)


def get_notification_detail_keyboard(
    notification_id: int, page: int, filter_type: str
) -> InlineKeyboardMarkup:
    """Generate keyboard for notification detail view."""
    keyboard = [
        [InlineKeyboardButton("⬅️ Back to List", callback_data=f"notif:page:{filter_type}:{page}")],
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

        await show_notifications_list(
            db, user.id, update.message, context, page=0, filter_type="all"
        )


async def show_notifications_list(
    db,
    user_id: int,
    message,
    context: ContextTypes.DEFAULT_TYPE,
    page: int = 0,
    filter_type: str = "all",
) -> None:
    """Show paginated list of notifications."""
    # Get user's notifications per page setting
    settings = get_user_notification_settings(db, user_id)
    notifications_per_page = settings.get("notifications_per_page", DEFAULT_NOTIFICATIONS_PER_PAGE)

    # Get notifications based on filter
    all_notifications = get_user_notifications(db, user_id, limit=1000, offset=0)

    if filter_type == "unread":
        notifications = [n for n in all_notifications if not n.read and not n.deleted]
    elif filter_type == "read":
        notifications = [n for n in all_notifications if n.read and not n.deleted]
    else:
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
    unread_count = get_unread_notifications_count(db, user_id)

    text = f"🔔 *Moodle Notifications*\n\n"
    text += f"*Total:* {total_count} notifications\n"
    text += f"*Unread:* {unread_count}\n\n"

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
        filter_type,
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
    filter_type: str,
) -> None:
    """Show detailed view of a single notification."""
    notification = db.execute(
        select(MoodleNotification).where(MoodleNotification.id == notification_id)
    ).scalar_one_or_none()

    if not notification:
        await message.edit_message_text("❌ Notification not found.")
        return

    # Format detailed message
    read_status = "✅ Read" if notification.read else "🔔 Unread"
    time_str = notification.timecreatedpretty or datetime.fromtimestamp(
        notification.timecreated
    ).strftime("%Y-%m-%d %H:%M:%S")

    text = f"🔔 *Notification Details*\n\n"
    text += f"*Status:* {read_status}\n"
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

    keyboard = get_notification_detail_keyboard(notification_id, page, filter_type)

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
            filter_type = data[2]
            page = int(data[3])
            await show_notifications_list(db, user.id, query, context, page, filter_type)

        elif action == "filter":
            filter_type = data[2]
            page = 0  # Reset to first page when filtering
            await show_notifications_list(db, user.id, query, context, page, filter_type)

        elif action == "detail":
            notification_id = int(data[2])
            page = int(data[3]) if len(data) > 3 else 0
            filter_type = data[4] if len(data) > 4 else "all"
            await show_notification_detail(db, notification_id, query, context, page, filter_type)

        elif action == "noop":
            # Do nothing, just acknowledge
            pass
