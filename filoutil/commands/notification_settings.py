"""Commands for configuring Moodle notification settings."""

import logging
from typing import Literal

from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from filoutil.auth import ensure_user_and_check_whitelisted
from filoutil.db.models import User
from filoutil.db.postgres import SessionLocal
from filoutil.db.users import get_user_by_telegram_id
from filoutil.utils.keyboard import arrange_buttons_in_rows

logger = logging.getLogger(__name__)

# Default notification settings
DEFAULT_NOTIFICATION_SETTINGS = {
    "enabled": True,
    "check_interval_s": 300,  # 5 minutes
    "send_immediately": True,
    "filter_event_types": [],  # Empty = all event types
    "filter_components": [],  # Empty = all components
    "max_notifications_per_batch": 5,  # Max notifications to send at once
    "word_blacklist": [],  # List of words to filter out (case-insensitive)
    "notifications_per_page": 10,  # Number of notifications per page in list view
}

CHECK_INTERVAL_OPTIONS = {
    "60": 60,  # 1 minute
    "300": 300,  # 5 minutes
    "600": 600,  # 10 minutes
    "1800": 1800,  # 30 minutes
    "3600": 3600,  # 1 hour
}


def get_notification_settings_keyboard(settings: dict) -> InlineKeyboardMarkup:
    """Generate keyboard for notification settings menu."""
    keyboard = []

    # Enable/Disable
    enabled_icon = "✅" if settings.get("enabled", True) else "❌"
    keyboard.append(
        [
            InlineKeyboardButton(
                f"{enabled_icon} Notifications: {'Enabled' if settings.get('enabled', True) else 'Disabled'}",
                callback_data="notif_settings:toggle:enabled",
            )
        ]
    )

    # Check Interval
    current_interval = settings.get("check_interval_s", 300)
    minutes = current_interval // 60
    keyboard.append(
        [
            InlineKeyboardButton(
                f"⏱ Check Interval: {minutes}m", callback_data="notif_settings:interval:menu"
            )
        ]
    )

    # Send Immediately
    send_immediately_icon = "✅" if settings.get("send_immediately", True) else "❌"
    keyboard.append(
        [
            InlineKeyboardButton(
                f"{send_immediately_icon} Send Immediately: {'Yes' if settings.get('send_immediately', True) else 'No'}",
                callback_data="notif_settings:toggle:send_immediately",
            )
        ]
    )

    # Max Notifications Per Batch
    max_batch = settings.get("max_notifications_per_batch", 5)
    keyboard.append(
        [
            InlineKeyboardButton(
                f"📦 Max Per Batch: {max_batch}", callback_data="notif_settings:max_batch:menu"
            )
        ]
    )

    # Notifications Per Page
    per_page = settings.get("notifications_per_page", 10)
    keyboard.append(
        [
            InlineKeyboardButton(
                f"📄 Per Page: {per_page}", callback_data="notif_settings:per_page:menu"
            )
        ]
    )

    # Word Blacklist
    blacklist_count = len(settings.get("word_blacklist", []))
    keyboard.append(
        [
            InlineKeyboardButton(
                f"🚫 Word Blacklist ({blacklist_count} words)",
                callback_data="notif_settings:blacklist:menu",
            )
        ]
    )

    # Filter Settings
    keyboard.append(
        [InlineKeyboardButton("🔍 Filter Settings", callback_data="notif_settings:filter:menu")]
    )

    keyboard.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu:main")])

    return InlineKeyboardMarkup(keyboard)


def get_interval_keyboard(current_interval: int) -> InlineKeyboardMarkup:
    """Generate keyboard for check interval selection."""
    buttons = []
    for label, interval in CHECK_INTERVAL_OPTIONS.items():
        minutes = interval // 60
        if minutes < 60:
            label_text = f"{minutes}m"
        else:
            hours = minutes // 60
            label_text = f"{hours}h"

        selected = "◉" if interval == current_interval else "○"
        buttons.append(
            InlineKeyboardButton(
                f"{selected} {label_text} ({interval}s)",
                callback_data=f"notif_settings:interval:set:{interval}",
            )
        )

    # Arrange buttons in rows (2 per row for compact display)
    keyboard = arrange_buttons_in_rows(buttons, buttons_per_row=2)
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="notif_settings:main")])
    return InlineKeyboardMarkup(keyboard)


async def show_word_blacklist_menu(
    query, context: ContextTypes.DEFAULT_TYPE, settings: dict, db, user_id: int
) -> None:
    """Show word blacklist management menu."""
    blacklist = settings.get("word_blacklist", [])

    text = "🚫 *Word Blacklist*\n\n"
    if blacklist:
        text += "*Blacklisted words:*\n"
        for idx, word in enumerate(blacklist):
            text += f"{idx + 1}. `{word}`\n"
        text += "\n"
    else:
        text += "No words in blacklist.\n\n"

    text += "Notifications containing any blacklisted word will not be sent to Telegram."

    keyboard = []

    # Add remove buttons for each word
    if blacklist:
        for idx, word in enumerate(blacklist):
            # Truncate if too long for button
            display_word = word if len(word) <= 20 else word[:17] + "..."
            keyboard.append(
                [
                    InlineKeyboardButton(
                        f"❌ Remove: {display_word}",
                        callback_data=f"notif_settings:blacklist:remove:{idx}",
                    )
                ]
            )

    # Add/Clear buttons
    keyboard.append(
        [InlineKeyboardButton("➕ Add Word", callback_data="notif_settings:blacklist:add")]
    )
    if blacklist:
        keyboard.append(
            [InlineKeyboardButton("🗑 Clear All", callback_data="notif_settings:blacklist:clear")]
        )

    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="notif_settings:main")])

    try:
        await query.edit_message_text(
            text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def handle_blacklist_word_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Handle text input for adding words to blacklist."""
    if not update.message or not update.message.text:
        return False

    user_id = context.user_data.get("blacklist_add_user_id")
    if not user_id:
        return False

    # Check if user canceled
    if update.message.text.strip().lower() in ["/cancel", "cancel"]:
        context.user_data.pop("blacklist_add_user_id", None)
        await update.message.reply_text("❌ Cancelled adding word to blacklist.")
        return True

    word = update.message.text.strip()
    if not word:
        await update.message.reply_text("❌ Please provide a valid word or phrase.")
        return True

    with SessionLocal() as db:
        settings = get_user_notification_settings(db, user_id)
        blacklist = settings.get("word_blacklist", [])

        # Check if word already exists (case-insensitive)
        if any(w.lower() == word.lower() for w in blacklist):
            await update.message.reply_text(
                f"❌ `{word}` is already in the blacklist.", parse_mode="Markdown"
            )
            return True

        # Add word to blacklist
        blacklist.append(word)
        settings["word_blacklist"] = blacklist
        save_user_notification_settings(db, user_id, settings)

        context.user_data.pop("blacklist_add_user_id", None)
        await update.message.reply_text(
            f"✅ Added `{word}` to blacklist.\n\n"
            f"Notifications containing this word will not be sent.",
            parse_mode="Markdown",
        )
        return True


def get_max_batch_keyboard(current_max: int) -> InlineKeyboardMarkup:
    """Generate keyboard for max notifications per batch selection."""
    buttons = []
    options = [1, 3, 5, 10, 20]
    for max_val in options:
        selected = "◉" if max_val == current_max else "○"
        buttons.append(
            InlineKeyboardButton(
                f"{selected} {max_val}", callback_data=f"notif_settings:max_batch:set:{max_val}"
            )
        )

    # Arrange buttons in rows (3 per row)
    keyboard = arrange_buttons_in_rows(buttons, buttons_per_row=3)
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="notif_settings:main")])
    return InlineKeyboardMarkup(keyboard)


def get_per_page_keyboard(current_per_page: int) -> InlineKeyboardMarkup:
    """Generate keyboard for notifications per page selection."""
    buttons = []
    options = [2, 3, 5, 10]
    for per_page_val in options:
        selected = "◉" if per_page_val == current_per_page else "○"
        buttons.append(
            InlineKeyboardButton(
                f"{selected} {per_page_val}",
                callback_data=f"notif_settings:per_page:set:{per_page_val}",
            )
        )

    # Arrange buttons in rows (2 per row for compact display)
    keyboard = arrange_buttons_in_rows(buttons, buttons_per_row=2)
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="notif_settings:main")])
    return InlineKeyboardMarkup(keyboard)


def get_user_notification_settings(db, user_id: int) -> dict:
    """Get user's notification settings with defaults."""
    user = db.execute(select(User).where(User.id == user_id)).scalar_one_or_none()
    if not user:
        return DEFAULT_NOTIFICATION_SETTINGS.copy()

    settings = user.settings or {}
    notification_settings = settings.get("notification_settings", {})

    # Merge with defaults
    result = DEFAULT_NOTIFICATION_SETTINGS.copy()
    result.update(notification_settings)
    return result


def save_user_notification_settings(db, user_id: int, settings: dict) -> None:
    """Save user's notification settings."""
    user = db.execute(select(User).where(User.id == user_id)).scalar_one_or_none()
    if not user:
        return

    user_settings = dict(user.settings) if user.settings else {}
    user_settings["notification_settings"] = settings
    user.settings = user_settings
    db.commit()


async def notification_settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /notification_settings command."""
    if not await ensure_user_and_check_whitelisted(update, context):
        return

    with SessionLocal() as db:
        user = get_user_by_telegram_id(db, update.message.from_user.id)
        if not user:
            await update.message.reply_text("❌ User not found.")
            return

        settings = get_user_notification_settings(db, user.id)
        await show_notification_settings(update.message, context, settings)


async def show_notification_settings(
    message, context: ContextTypes.DEFAULT_TYPE, settings: dict
) -> None:
    """Show notification settings menu."""
    enabled_status = "✅ Enabled" if settings.get("enabled", True) else "❌ Disabled"
    check_interval = settings.get("check_interval_s", 300)
    minutes = check_interval // 60

    blacklist_count = len(settings.get("word_blacklist", []))
    per_page = settings.get("notifications_per_page", 10)
    text = (
        f"⚙️ *Moodle Notification Settings*\n\n"
        f"*Status:* {enabled_status}\n"
        f"*Check Interval:* {minutes}m ({check_interval}s)\n"
        f"*Send Immediately:* {'Yes' if settings.get('send_immediately', True) else 'No'}\n"
        f"*Max Per Batch:* {settings.get('max_notifications_per_batch', 5)}\n"
        f"*Notifications Per Page:* {per_page}\n"
        f"*Word Blacklist:* {blacklist_count} word(s)\n\n"
        f"Configure your notification preferences:"
    )

    keyboard = get_notification_settings_keyboard(settings)

    try:
        if hasattr(message, "edit_message_text"):
            await message.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
        else:
            await message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def notification_settings_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle callback queries for notification settings."""
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

        settings = get_user_notification_settings(db, user.id)

        if action == "toggle":
            setting_key = data[2]
            current_value = settings.get(setting_key, False)
            settings[setting_key] = not current_value
            save_user_notification_settings(db, user.id, settings)
            await query.answer(
                f"✅ {setting_key.replace('_', ' ').title()} set to {not current_value}"
            )
            await show_notification_settings(query, context, settings)

        elif action == "interval":
            sub_action = data[2]
            if sub_action == "menu":
                current_interval = settings.get("check_interval_s", 300)
                text = "⏱ *Check Interval*\n\nSelect how often to check for new notifications:"
                keyboard = get_interval_keyboard(current_interval)
                try:
                    await query.edit_message_text(
                        text, reply_markup=keyboard, parse_mode="Markdown"
                    )
                except BadRequest as e:
                    if "Message is not modified" not in str(e):
                        raise
            elif sub_action == "set":
                interval = int(data[3])
                settings["check_interval_s"] = interval
                save_user_notification_settings(db, user.id, settings)
                minutes = interval // 60
                await query.answer(f"✅ Check interval set to {minutes}m ({interval}s)")
                await show_notification_settings(query, context, settings)

        elif action == "max_batch":
            sub_action = data[2]
            if sub_action == "menu":
                current_max = settings.get("max_notifications_per_batch", 5)
                text = "📦 *Max Notifications Per Batch*\n\nSelect maximum notifications to send at once:"
                keyboard = get_max_batch_keyboard(current_max)
                try:
                    await query.edit_message_text(
                        text, reply_markup=keyboard, parse_mode="Markdown"
                    )
                except BadRequest as e:
                    if "Message is not modified" not in str(e):
                        raise
            elif sub_action == "set":
                max_val = int(data[3])
                settings["max_notifications_per_batch"] = max_val
                save_user_notification_settings(db, user.id, settings)
                await query.answer(f"✅ Max per batch set to {max_val}")
                await show_notification_settings(query, context, settings)

        elif action == "per_page":
            sub_action = data[2]
            if sub_action == "menu":
                current_per_page = settings.get("notifications_per_page", 10)
                text = (
                    "📄 *Notifications Per Page*\n\nSelect how many notifications to show per page:"
                )
                keyboard = get_per_page_keyboard(current_per_page)
                try:
                    await query.edit_message_text(
                        text, reply_markup=keyboard, parse_mode="Markdown"
                    )
                except BadRequest as e:
                    if "Message is not modified" not in str(e):
                        raise
            elif sub_action == "set":
                per_page_val = int(data[3])
                settings["notifications_per_page"] = per_page_val
                save_user_notification_settings(db, user.id, settings)
                await query.answer(f"✅ Notifications per page set to {per_page_val}")
                await show_notification_settings(query, context, settings)

        elif action == "blacklist":
            sub_action = data[2] if len(data) > 2 else "menu"
            if sub_action == "menu":
                await show_word_blacklist_menu(query, context, settings, db, user.id)
            elif sub_action == "add":
                # Store user_id in context for text input handler
                context.user_data["blacklist_add_user_id"] = user.id
                await query.edit_message_text(
                    "🚫 *Add Word to Blacklist*\n\n"
                    "Send me a word or phrase to add to the blacklist.\n"
                    "Notifications containing this word will not be sent.\n\n"
                    "Example: `assignment` or `quiz`\n\n"
                    "Send /cancel to cancel.",
                    parse_mode="Markdown",
                )
            elif sub_action == "remove":
                if len(data) > 3:
                    word_index = int(data[3])
                    blacklist = settings.get("word_blacklist", [])
                    if 0 <= word_index < len(blacklist):
                        removed_word = blacklist.pop(word_index)
                        settings["word_blacklist"] = blacklist
                        save_user_notification_settings(db, user.id, settings)
                        await query.answer(f"✅ Removed '{removed_word}' from blacklist")
                        await show_word_blacklist_menu(query, context, settings, db, user.id)
                    else:
                        await query.answer("❌ Invalid word index", show_alert=True)
                else:
                    await query.answer("❌ Invalid command", show_alert=True)
            elif sub_action == "clear":
                settings["word_blacklist"] = []
                save_user_notification_settings(db, user.id, settings)
                await query.answer("✅ Blacklist cleared")
                await show_word_blacklist_menu(query, context, settings, db, user.id)

        elif action == "filter":
            # Show filter settings info
            filter_event_types = settings.get("filter_event_types", [])
            filter_components = settings.get("filter_components", [])
            text = "🔍 *Filter Settings*\n\n" "*Event Types:*\n"
            if filter_event_types:
                text += "\n".join(f"• {et}" for et in filter_event_types)
            else:
                text += "All event types allowed"

            text += "\n\n*Components:*\n"
            if filter_components:
                text += "\n".join(f"• {comp}" for comp in filter_components)
            else:
                text += "All components allowed"

            text += "\n\n*Note:* Filter settings are currently read-only. Use word blacklist to filter notifications."

            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back", callback_data="notif_settings:main")]]
            )
            try:
                await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise

        elif action == "main":
            await show_notification_settings(query, context, settings)
