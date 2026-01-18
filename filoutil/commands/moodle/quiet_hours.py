"""Admin commands for managing Moodle quiet hours."""

import logging
from datetime import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from filoutil.commands.admin import ensure_admin
from filoutil.config import format_time_for_display, get_current_time
from filoutil.db.moodle_quiet_hours import (
    create_quiet_hours,
    delete_quiet_hours,
    get_all_quiet_hours,
    get_quiet_hours_by_id,
    update_quiet_hours,
)
from filoutil.db.postgres import SessionLocal

logger = logging.getLogger(__name__)


def format_quiet_hours_list(quiet_hours_list: list) -> str:
    """Format a list of quiet hours intervals for display."""
    if not quiet_hours_list:
        return "No quiet hours intervals configured."

    text = ""
    for qh in quiet_hours_list:
        status = "✅" if qh.enabled else "❌"
        text += f"{status} {qh.start_time.strftime('%H:%M')} - {qh.end_time.strftime('%H:%M')}\n"

    return text.strip()


def get_quiet_hours_keyboard(quiet_hours_list: list) -> InlineKeyboardMarkup:
    """Generate keyboard for quiet hours management."""
    keyboard = []

    # Add buttons for each quiet hours interval
    for qh in quiet_hours_list:
        status = "✅" if qh.enabled else "❌"
        time_str = f"{qh.start_time.strftime('%H:%M')}-{qh.end_time.strftime('%H:%M')}"
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"{status} {time_str}",
                    callback_data=f"admin:quiet_hours:edit:{qh.id}",
                )
            ]
        )

    # Add new interval button
    keyboard.append(
        [InlineKeyboardButton("➕ Add Interval", callback_data="admin:quiet_hours:add")]
    )

    # Back button
    keyboard.append(
        [InlineKeyboardButton("⬅️ Back to Admin Panel", callback_data="admin_users:list:0")]
    )

    return InlineKeyboardMarkup(keyboard)


async def show_quiet_hours_menu(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the quiet hours management menu."""
    with SessionLocal() as db:
        quiet_hours_list = get_all_quiet_hours(db)

        # Get current time in configured timezone
        current_time = get_current_time()
        current_time_str = format_time_for_display(current_time)

        text = "🔇 *Quiet Hours Management*\n\n"
        text += "During quiet hours, only moodle refresh task can run.\n"
        text += "Grades task will be skipped during these times.\n\n"
        text += f"*Current Time:* {current_time_str}\n\n"
        text += "*Current Intervals:*\n"
        text += format_quiet_hours_list(quiet_hours_list)

        keyboard = get_quiet_hours_keyboard(quiet_hours_list)

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


async def show_edit_quiet_hours(
    query, context: ContextTypes.DEFAULT_TYPE, quiet_hours_id: int
) -> None:
    """Show edit interface for a quiet hours interval."""
    with SessionLocal() as db:
        quiet_hours = get_quiet_hours_by_id(db, quiet_hours_id)
        if not quiet_hours:
            await query.answer("❌ Quiet hours interval not found.", show_alert=True)
            return

        text = (
            f"✏️ *Edit Quiet Hours*\n\n"
            f"*Current Interval:*\n"
            f"Start: {quiet_hours.start_time.strftime('%H:%M')}\n"
            f"End: {quiet_hours.end_time.strftime('%H:%M')}\n"
            f"Enabled: {'Yes' if quiet_hours.enabled else 'No'}\n\n"
            f"Use the buttons below to edit:"
        )

        keyboard = [
            [
                InlineKeyboardButton(
                    "⏰ Edit Start Time",
                    callback_data=f"admin:quiet_hours:edit_start:{quiet_hours_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "⏰ Edit End Time", callback_data=f"admin:quiet_hours:edit_end:{quiet_hours_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    f"{'❌ Disable' if quiet_hours.enabled else '✅ Enable'}",
                    callback_data=f"admin:quiet_hours:toggle:{quiet_hours_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "🗑️ Delete", callback_data=f"admin:quiet_hours:delete:{quiet_hours_id}"
                )
            ],
            [InlineKeyboardButton("⬅️ Back to Quiet Hours", callback_data="admin:quiet_hours:menu")],
        ]

        try:
            await query.edit_message_text(
                text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise


async def quiet_hours_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle callback queries for quiet hours management."""
    query = update.callback_query
    if not query:
        return

    # Check admin status
    if not await ensure_admin(update, context):
        return

    await query.answer()

    data = query.data.split(":")
    # Handle "admin:quiet_hours:" prefix
    # Format: admin:quiet_hours:action or admin:quiet_hours:action:id
    if len(data) >= 3 and data[0] == "admin" and data[1] == "quiet_hours":
        action = data[2] if len(data) > 2 else None
    else:
        action = None

    if action == "menu":
        await show_quiet_hours_menu(query, context)

    elif action == "add":
        # Show add interface - for now, we'll use a simple format
        # In a real implementation, you might want to use conversation handlers
        text = (
            "➕ *Add Quiet Hours Interval*\n\n"
            "To add a new interval, send a message in this format:\n\n"
            '`/moodle_quiet_hours_add {"start": "22:00", "end": "06:00"}`\n\n'
            "Or use the format:\n"
            "`/moodle_quiet_hours_add 22:00 06:00`\n\n"
            "Times should be in 24-hour format (HH:MM)."
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "⬅️ Back to Quiet Hours", callback_data="admin:quiet_hours:menu"
                    )
                ]
            ]
        )
        try:
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise

    elif action == "edit":
        if len(data) > 3:
            quiet_hours_id = int(data[3])
            await show_edit_quiet_hours(query, context, quiet_hours_id)
        else:
            await query.answer("❌ Invalid quiet hours ID.", show_alert=True)

    elif action == "edit_start":
        if len(data) > 3:
            quiet_hours_id = int(data[3])
            text = (
                f"⏰ *Edit Start Time*\n\n"
                f"Send a message with the new start time in HH:MM format:\n\n"
                f"Example: `22:00`\n\n"
                f"Or use the command:\n"
                f"`/moodle_quiet_hours_edit_start {quiet_hours_id} 22:00`"
            )
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ Back", callback_data=f"admin:quiet_hours:edit:{quiet_hours_id}"
                        )
                    ]
                ]
            )
            try:
                await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
        else:
            await query.answer("❌ Invalid quiet hours ID.", show_alert=True)

    elif action == "edit_end":
        if len(data) > 3:
            quiet_hours_id = int(data[3])
            text = (
                f"⏰ *Edit End Time*\n\n"
                f"Send a message with the new end time in HH:MM format:\n\n"
                f"Example: `06:00`\n\n"
                f"Or use the command:\n"
                f"`/moodle_quiet_hours_edit_end {quiet_hours_id} 06:00`"
            )
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ Back", callback_data=f"admin:quiet_hours:edit:{quiet_hours_id}"
                        )
                    ]
                ]
            )
            try:
                await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
        else:
            await query.answer("❌ Invalid quiet hours ID.", show_alert=True)

    elif action == "toggle":
        if len(data) > 3:
            quiet_hours_id = int(data[3])
            with SessionLocal() as db:
                quiet_hours = get_quiet_hours_by_id(db, quiet_hours_id)
                if not quiet_hours:
                    await query.answer("❌ Quiet hours interval not found.", show_alert=True)
                    return

                update_quiet_hours(db, quiet_hours_id, enabled=not quiet_hours.enabled)
                await query.answer(
                    f"✅ Interval {'enabled' if not quiet_hours.enabled else 'disabled'}.",
                    show_alert=False,
                )
                # Refresh the edit view
                await show_edit_quiet_hours(query, context, quiet_hours_id)
        else:
            await query.answer("❌ Invalid quiet hours ID.", show_alert=True)

    elif action == "delete":
        if len(data) > 3:
            quiet_hours_id = int(data[3])
            with SessionLocal() as db:
                quiet_hours = get_quiet_hours_by_id(db, quiet_hours_id)
                if not quiet_hours:
                    await query.answer("❌ Quiet hours interval not found.", show_alert=True)
                    return

                delete_quiet_hours(db, quiet_hours_id)
                await query.answer("✅ Interval deleted.", show_alert=False)
                # Go back to menu
                await show_quiet_hours_menu(query, context)
        else:
            await query.answer("❌ Invalid quiet hours ID.", show_alert=True)


async def moodle_quiet_hours_add_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /moodle_quiet_hours_add command to add a new quiet hours interval."""
    if not await ensure_admin(update, context):
        return

    if not update.message or not update.message.text:
        await update.message.reply_text(
            "❌ Please provide start and end times.\n\n"
            "Usage: `/moodle_quiet_hours_add 22:00 06:00`\n"
            "Times should be in 24-hour format (HH:MM).",
            parse_mode="Markdown",
        )
        return

    # Parse command arguments
    args = context.args if context.args else []
    if len(args) < 2:
        await update.message.reply_text(
            "❌ Please provide start and end times.\n\n"
            "Usage: `/moodle_quiet_hours_add 22:00 06:00`\n"
            "Times should be in 24-hour format (HH:MM).",
            parse_mode="Markdown",
        )
        return

    try:
        # Parse start and end times
        start_str = args[0]
        end_str = args[1]

        # Parse time strings (HH:MM format)
        start_parts = start_str.split(":")
        end_parts = end_str.split(":")

        if len(start_parts) != 2 or len(end_parts) != 2:
            raise ValueError("Invalid time format")

        start_time = time(int(start_parts[0]), int(start_parts[1]))
        end_time = time(int(end_parts[0]), int(end_parts[1]))

        with SessionLocal() as db:
            quiet_hours = create_quiet_hours(db, start_time, end_time, enabled=True)
            await update.message.reply_text(
                f"✅ Quiet hours interval added:\n"
                f"Start: {start_time.strftime('%H:%M')}\n"
                f"End: {end_time.strftime('%H:%M')}",
                parse_mode="Markdown",
            )

    except (ValueError, IndexError) as e:
        await update.message.reply_text(
            "❌ Invalid time format. Please use HH:MM format (e.g., 22:00 or 06:00)."
        )


async def moodle_quiet_hours_edit_start_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /moodle_quiet_hours_edit_start command to edit start time."""
    if not await ensure_admin(update, context):
        return

    args = context.args if context.args else []
    if len(args) < 2:
        await update.message.reply_text(
            "❌ Please provide quiet hours ID and new start time.\n\n"
            "Usage: `/moodle_quiet_hours_edit_start <id> 22:00`",
            parse_mode="Markdown",
        )
        return

    try:
        quiet_hours_id = int(args[0])
        time_str = args[1]

        time_parts = time_str.split(":")
        if len(time_parts) != 2:
            raise ValueError("Invalid time format")

        new_start_time = time(int(time_parts[0]), int(time_parts[1]))

        with SessionLocal() as db:
            updated = update_quiet_hours(db, quiet_hours_id, start_time=new_start_time)
            if updated:
                await update.message.reply_text(
                    f"✅ Start time updated to {new_start_time.strftime('%H:%M')}"
                )
            else:
                await update.message.reply_text("❌ Quiet hours interval not found.")

    except (ValueError, IndexError) as e:
        await update.message.reply_text(
            "❌ Invalid format. Usage: `/moodle_quiet_hours_edit_start <id> HH:MM`",
            parse_mode="Markdown",
        )


async def moodle_quiet_hours_edit_end_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /moodle_quiet_hours_edit_end command to edit end time."""
    if not await ensure_admin(update, context):
        return

    args = context.args if context.args else []
    if len(args) < 2:
        await update.message.reply_text(
            "❌ Please provide quiet hours ID and new end time.\n\n"
            "Usage: `/moodle_quiet_hours_edit_end <id> 06:00`",
            parse_mode="Markdown",
        )
        return

    try:
        quiet_hours_id = int(args[0])
        time_str = args[1]

        time_parts = time_str.split(":")
        if len(time_parts) != 2:
            raise ValueError("Invalid time format")

        new_end_time = time(int(time_parts[0]), int(time_parts[1]))

        with SessionLocal() as db:
            updated = update_quiet_hours(db, quiet_hours_id, end_time=new_end_time)
            if updated:
                await update.message.reply_text(
                    f"✅ End time updated to {new_end_time.strftime('%H:%M')}"
                )
            else:
                await update.message.reply_text("❌ Quiet hours interval not found.")

    except (ValueError, IndexError) as e:
        await update.message.reply_text(
            "❌ Invalid format. Usage: `/moodle_quiet_hours_edit_end <id> HH:MM`",
            parse_mode="Markdown",
        )
