import io
import logging
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from filoutil.auth import ensure_user_and_check_whitelisted
from filoutil.db.monitors import (
    create_monitor,
    delete_monitor,
    get_all_monitors,
    get_check_runs_by_time_range,
    get_monitor,
    get_recent_check_runs,
    update_monitor,
)
from filoutil.db.postgres import SessionLocal
from filoutil.db.users import get_user_by_telegram_id
from filoutil.monitor.charts import generate_dashboard_chart, generate_monitor_charts
from filoutil.monitor.engine import check_monitor

logger = logging.getLogger(__name__)

# --- Keyboards ---


def get_monitor_list_keyboard(monitors):
    keyboard = []
    for m in monitors:
        status_emoji = "✅" if m.status == "up" else "❌" if m.status == "down" else "🔄"
        if m.status == "flapping":
            status_emoji = "⚠️"
        keyboard.append(
            [InlineKeyboardButton(f"{status_emoji} {m.name}", callback_data=f"mon:view:{m.id}")]
        )

    keyboard.append([InlineKeyboardButton("➕ Add Monitor", callback_data="mon:add_start")])
    keyboard.append([InlineKeyboardButton("🔄 Refresh List", callback_data="mon:list")])
    return InlineKeyboardMarkup(keyboard)


def get_monitor_details_keyboard(monitor):
    status_text = "Enabled" if monitor.enabled else "Disabled"
    toggle_text = "⏸ Disable" if monitor.enabled else "▶️ Enable"

    keyboard = [
        [
            InlineKeyboardButton("🔄 Refresh", callback_data=f"mon:refresh:{monitor.id}"),
            InlineKeyboardButton("📊 Stats", callback_data=f"mon:stats:{monitor.id}"),
        ],
        [
            InlineKeyboardButton("📝 Edit", callback_data=f"mon:edit_menu:{monitor.id}"),
            InlineKeyboardButton(toggle_text, callback_data=f"mon:toggle:{monitor.id}"),
        ],
        [
            InlineKeyboardButton("🗑️ Delete", callback_data=f"mon:delete_confirm:{monitor.id}"),
            InlineKeyboardButton("⬅️ Back to List", callback_data="mon:list"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_monitor_edit_keyboard(monitor):
    keyboard = [
        [
            InlineKeyboardButton("🏷️ Name", callback_data=f"mon:edit:{monitor.id}:name"),
            InlineKeyboardButton("🔗 URL", callback_data=f"mon:edit:{monitor.id}:url"),
        ],
        [
            InlineKeyboardButton("⏱️ Interval", callback_data=f"mon:edit:{monitor.id}:interval_s"),
            InlineKeyboardButton("⌛ Timeout", callback_data=f"mon:edit:{monitor.id}:timeout_s"),
        ],
        [
            InlineKeyboardButton(
                "🎯 Status", callback_data=f"mon:edit:{monitor.id}:expected_status"
            ),
            InlineKeyboardButton("🔑 Method", callback_data=f"mon:edit:{monitor.id}:method"),
        ],
        [
            InlineKeyboardButton(
                "🔔 Down: " + ("ON" if monitor.alert_on_down else "OFF"),
                callback_data=f"mon:edit:{monitor.id}:alert_on_down",
            ),
            InlineKeyboardButton(
                "🔔 Up: " + ("ON" if monitor.alert_on_up else "OFF"),
                callback_data=f"mon:edit:{monitor.id}:alert_on_up",
            ),
        ],
        [InlineKeyboardButton("⬅️ Back to Monitor", callback_data=f"mon:view:{monitor.id}")],
    ]
    return InlineKeyboardMarkup(keyboard)


# --- Handlers ---


async def monitor_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Entry point for /monitor command."""
    if not await ensure_user_and_check_whitelisted(update, context):
        return

    # Check for arguments (simple CLI-style add)
    if context.args and context.args[0] == "add":
        if len(context.args) < 3:
            await update.message.reply_text(
                "Usage: `/monitor add <name> <url>`", parse_mode="Markdown"
            )
            return
        name = context.args[1]
        url = context.args[2]
        with SessionLocal() as db:
            create_monitor(db, name=name, url=url)
        await update.message.reply_text(f"✅ Monitor '{name}' added!")
        return

    with SessionLocal() as db:
        monitors = get_all_monitors(db)
        if not monitors:
            text = "No monitors found. Use `+ Add Monitor` or `/monitor add <name> <url>` to create one."
            await update.message.reply_text(text, parse_mode="Markdown")
            return

        # Show dashboard by default
        status_msg = await update.message.reply_text("📊 Generating dashboard...")

        monitors_data = []
        for m in monitors:
            recent = get_check_runs_by_time_range(db, m.id, hours=24)
            monitors_data.append({"name": m.name, "recent_runs": recent, "time_range": "24h"})

        chart_buf = await generate_dashboard_chart(monitors_data)

        # Still provide access to the list via keyboard
        keyboard = [
            [InlineKeyboardButton("📋 Monitor List", callback_data="mon:list")],
            [InlineKeyboardButton("➕ Add Monitor", callback_data="mon:add_start")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_photo(
            photo=chart_buf,
            caption="📊 *Service Dashboard (24h)*",
            reply_markup=reply_markup,
            parse_mode="Markdown",
        )
        await status_msg.delete()


async def monitor_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles all monitoring related callbacks."""
    query = update.callback_query
    await query.answer()

    data = query.data.split(":")
    action = data[1]

    with SessionLocal() as db:
        if action == "list":
            monitors = get_all_monitors(db)
            text = "📋 *Service Monitors:*"
            reply_markup = get_monitor_list_keyboard(monitors)

            if query.message.photo:
                await query.message.delete()
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode="Markdown",
                )
            else:
                await query.edit_message_text(
                    text, reply_markup=reply_markup, parse_mode="Markdown"
                )

        elif action == "view":
            m_id = int(data[2])
            monitor = get_monitor(db, m_id)
            if not monitor:
                if query.message.photo:
                    await query.message.delete()
                    await context.bot.send_message(
                        chat_id=query.message.chat_id, text="Monitor not found."
                    )
                else:
                    await query.edit_message_text("Monitor not found.")
                return

            recent = get_recent_check_runs(db, m_id, limit=1)
            last_run = recent[0] if recent else None

            text = (
                f"🖥 *Monitor Details*\n\n"
                f"*Name:* {monitor.name}\n"
                f"*URL:* {monitor.url}\n"
                f"*Status:* {monitor.status.upper()}\n"
                f"*Interval:* {monitor.interval_s}s\n"
                f"*Last Check:* {monitor.last_check_at.strftime('%Y-%m-%d %H:%M:%S') if monitor.last_check_at else 'Never'}\n"
            )
            if last_run:
                text += f"*Latency:* {last_run.latency_ms:.2f}ms\n"
                if last_run.error:
                    text += f"*Last Error:* `{last_run.error}`\n"
                if last_run.ssl_expiry:
                    days = (last_run.ssl_expiry - datetime.utcnow()).days
                    text += f"*SSL Expires in:* {days} days\n"

            if query.message.photo:
                await query.message.delete()
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=text,
                    reply_markup=get_monitor_details_keyboard(monitor),
                    parse_mode="Markdown",
                )
            else:
                await query.edit_message_text(
                    text, reply_markup=get_monitor_details_keyboard(monitor), parse_mode="Markdown"
                )

        elif action == "refresh":
            m_id = int(data[2])
            monitor = get_monitor(db, m_id)
            if monitor:
                back_kb = InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ Back", callback_data=f"mon:view:{m_id}")]]
                )
                await query.edit_message_text(
                    f"⏳ Checking {monitor.name}...", reply_markup=back_kb
                )
                await check_monitor(db, monitor)
                # Re-trigger view
                query.data = f"mon:view:{m_id}"
                await monitor_callback(update, context)

        elif action == "toggle":
            m_id = int(data[2])
            monitor = get_monitor(db, m_id)
            if monitor:
                update_monitor(db, m_id, enabled=not monitor.enabled)
                # Re-trigger view
                query.data = f"mon:view:{m_id}"
                await monitor_callback(update, context)

        elif action == "delete_confirm":
            m_id = int(data[2])
            monitor = get_monitor(db, m_id)
            keyboard = [
                [InlineKeyboardButton("✅ Yes, Delete", callback_data=f"mon:delete_final:{m_id}")],
                [InlineKeyboardButton("❌ Cancel", callback_data=f"mon:view:{m_id}")],
            ]
            await query.edit_message_text(
                f"⚠️ Are you sure you want to delete *{monitor.name}*?",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown",
            )

        elif action == "delete_final":
            m_id = int(data[2])
            delete_monitor(db, m_id)
            await query.edit_message_text("✅ Monitor deleted.")
            # Go back to list after a short delay or just show back button
            keyboard = [[InlineKeyboardButton("⬅️ Back to List", callback_data="mon:list")]]
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))

        elif action == "edit_menu":
            m_id = int(data[2])
            monitor = get_monitor(db, m_id)
            if not monitor:
                return

            await query.edit_message_text(
                f"📝 *Editing Monitor: {monitor.name}*\n" f"Choose a field to modify:",
                reply_markup=get_monitor_edit_keyboard(monitor),
                parse_mode="Markdown",
            )

        elif action == "edit":
            m_id = int(data[2])
            field = data[3]
            monitor = get_monitor(db, m_id)
            if not monitor:
                return

            # Boolean toggles don't need text input
            if field in ["alert_on_down", "alert_on_up"]:
                current_val = getattr(monitor, field)
                update_monitor(db, m_id, **{field: not current_val})
                # Show menu again
                query.data = f"mon:edit_menu:{m_id}"
                await monitor_callback(update, context)
                return

            # For other fields, we need text input
            context.user_data["editing_monitor_id"] = m_id
            context.user_data["editing_field"] = field

            field_names = {
                "name": "Name",
                "url": "URL",
                "interval_s": "Check Interval (seconds)",
                "timeout_s": "Timeout (seconds)",
                "expected_status": "Expected HTTP Status",
                "method": "HTTP Method (GET, POST, etc.)",
            }

            field_name = field_names.get(field, field)
            current_val = getattr(monitor, field)

            text = (
                f"📝 *Editing {field_name}* for `{monitor.name}`\n\n"
                f"Current value: `{current_val}`\n\n"
                f"Please send the new value now."
            )

            back_kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data=f"mon:edit_menu:{m_id}")]]
            )
            await query.edit_message_text(text, reply_markup=back_kb, parse_mode="Markdown")

        elif action == "stats":
            m_id = int(data[2])

            user = get_user_by_telegram_id(db, query.from_user.id)
            default_range = (
                user.settings.get("default_time_range", "24h") if user and user.settings else "24h"
            )

            # Get time range from callback data (default to user's preference or 24h)
            time_range = data[3] if len(data) > 3 else default_range

            monitor = get_monitor(db, m_id)
            if not monitor:
                return

            # Fetch data based on time range
            if time_range == "1h":
                recent = get_check_runs_by_time_range(db, m_id, hours=1)
            elif time_range == "6h":
                recent = get_check_runs_by_time_range(db, m_id, hours=6)
            elif time_range == "24h":
                recent = get_check_runs_by_time_range(db, m_id, hours=24)
            elif time_range == "1month":
                recent = get_check_runs_by_time_range(db, m_id, days=30)
            else:
                recent = get_check_runs_by_time_range(db, m_id, hours=24)

            if not recent:
                await query.answer("No data yet for charts.")
                return

            # Build keyboard with time range buttons
            time_range_buttons = [
                InlineKeyboardButton(
                    f"{'◉' if time_range == '1h' else '○'} 1h", callback_data=f"mon:stats:{m_id}:1h"
                ),
                InlineKeyboardButton(
                    f"{'◉' if time_range == '6h' else '○'} 6h", callback_data=f"mon:stats:{m_id}:6h"
                ),
                InlineKeyboardButton(
                    f"{'◉' if time_range == '24h' else '○'} 24h",
                    callback_data=f"mon:stats:{m_id}:24h",
                ),
                InlineKeyboardButton(
                    f"{'◉' if time_range == '1month' else '○'} 1 month",
                    callback_data=f"mon:stats:{m_id}:1month",
                ),
            ]

            # Button to set current range as default
            is_default = time_range == default_range
            default_btn = []
            if not is_default:
                default_btn = [
                    InlineKeyboardButton(
                        "📌 Set as Default",
                        callback_data=f"mon:set_default_range:{time_range}:{m_id}",
                    )
                ]

            stats_kb = InlineKeyboardMarkup(
                [
                    time_range_buttons,
                    default_btn,
                    [InlineKeyboardButton("⬅️ Back", callback_data=f"mon:view:{m_id}")],
                ]
            )

            range_labels = {
                "1h": "last hour",
                "6h": "last 6 hours",
                "24h": "last 24 hours",
                "1month": "last month",
            }
            range_label = range_labels.get(time_range, time_range)

            chart_buf = await generate_monitor_charts(monitor.name, recent, time_range)

            # Handle photo vs text message differently
            if query.message.photo:
                # Send new photo first
                await context.bot.send_photo(
                    chat_id=query.message.chat_id,
                    photo=chart_buf,
                    caption=f"📊 *{monitor.name}* ({range_label}, {len(recent)} checks)",
                    reply_markup=stats_kb,
                    parse_mode="Markdown",
                )
                # Then delete the old one
                await query.message.delete()
            else:
                # Text message - show loading then send photo
                await query.edit_message_text(
                    f"📊 Generating charts for {monitor.name}...", reply_markup=stats_kb
                )
                await query.message.reply_photo(
                    photo=chart_buf,
                    caption=f"📊 *{monitor.name}* ({range_label}, {len(recent)} checks)",
                    reply_markup=stats_kb,
                    parse_mode="Markdown",
                )
                # Show the view again in the original message
                query.data = f"mon:view:{m_id}"
                await monitor_callback(update, context)

        elif action == "set_default_range":
            new_range = data[2]
            m_id = int(data[3])
            user = get_user_by_telegram_id(db, query.from_user.id)
            if user:
                settings = dict(user.settings) if user.settings else {}
                settings["default_time_range"] = new_range
                user.settings = settings
                db.commit()
                await query.answer(f"✅ Default time range set to {new_range}")
                # Re-trigger stats view to show updated state (hide the pin button)
                query.data = f"mon:stats:{m_id}:{new_range}"
                await monitor_callback(update, context)


async def handle_monitor_edit_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Handles text input when a user is editing a monitor field.
    Returns True if the message was handled, False otherwise.
    """
    if not update.message or not update.message.text:
        return False

    m_id = context.user_data.get("editing_monitor_id")
    field = context.user_data.get("editing_field")

    if m_id is None or field is None:
        return False

    new_value = update.message.text.strip()

    # Validation and type conversion
    try:
        if field in ["interval_s", "timeout_s", "expected_status"]:
            new_value = int(new_value)
            if new_value <= 0 and field != "expected_status":
                raise ValueError("Value must be positive.")
        elif field == "method":
            new_value = new_value.upper()
            if new_value not in ["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "PATCH"]:
                raise ValueError("Invalid HTTP method.")
    except ValueError as e:
        await update.message.reply_text(f"❌ Invalid value: {str(e)}\nPlease try again or cancel.")
        return True

    with SessionLocal() as db:
        monitor = update_monitor(db, m_id, **{field: new_value})
        if monitor:
            await update.message.reply_text(
                f"✅ Updated *{field}* to `{new_value}` for *{monitor.name}*.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "⬅️ Back to Edit Menu", callback_data=f"mon:edit_menu:{m_id}"
                            )
                        ]
                    ]
                ),
            )
        else:
            await update.message.reply_text("❌ Error: Monitor not found.")

    # Clear editing state
    del context.user_data["editing_monitor_id"]
    del context.user_data["editing_field"]
    return True
