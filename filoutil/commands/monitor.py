import io
import logging
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from filoutil.auth import require_module_permission
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
    keyboard.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu:main")])
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


# --- Helper Functions ---


async def _show_monitor_view(db, query, context, m_id):
    """Helper function to show monitor view."""
    monitor = get_monitor(db, m_id)
    if not monitor:
        if query.message.photo:
            await query.message.delete()
            await context.bot.send_message(chat_id=query.message.chat_id, text="Monitor not found.")
        else:
            try:
                await query.edit_message_text("Monitor not found.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
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
        try:
            await query.edit_message_text(
                text,
                reply_markup=get_monitor_details_keyboard(monitor),
                parse_mode="Markdown",
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise


async def _show_edit_menu(db, query, m_id):
    """Helper function to show edit menu."""
    monitor = get_monitor(db, m_id)
    if not monitor:
        return
    try:
        await query.edit_message_text(
            f"📝 *Editing Monitor: {monitor.name}*\n" f"Choose a field to modify:",
            reply_markup=get_monitor_edit_keyboard(monitor),
            parse_mode="Markdown",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


# --- Handlers ---


async def monitor_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Entry point for /monitor command."""
    if not await require_module_permission(update, context, "monitoring"):
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

        # Get user's default time range
        user = get_user_by_telegram_id(db, update.message.from_user.id)
        default_range = (
            user.settings.get("default_time_range", "24h") if user and user.settings else "24h"
        )

        # Show dashboard by default
        status_msg = await update.message.reply_text("📊 Generating dashboard...")

        monitors_data = []
        for m in monitors:
            # Fetch data based on user's default time range
            if default_range == "1h":
                recent = get_check_runs_by_time_range(db, m.id, hours=1)
            elif default_range == "6h":
                recent = get_check_runs_by_time_range(db, m.id, hours=6)
            elif default_range == "24h":
                recent = get_check_runs_by_time_range(db, m.id, hours=24)
            elif default_range == "1month":
                recent = get_check_runs_by_time_range(db, m.id, days=30)
            else:
                recent = get_check_runs_by_time_range(db, m.id, hours=24)
            monitors_data.append(
                {"name": m.name, "recent_runs": recent, "time_range": default_range}
            )

        chart_buf = await generate_dashboard_chart(monitors_data)

        # Still provide access to the list via keyboard
        keyboard = [
            [InlineKeyboardButton("📋 Monitor List", callback_data="mon:list")],
            [InlineKeyboardButton("➕ Add Monitor", callback_data="mon:add_start")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_photo(
            photo=chart_buf,
            caption=f"📊 *Service Dashboard ({default_range})*",
            reply_markup=reply_markup,
            parse_mode="Markdown",
        )
        await status_msg.delete()


async def monitor_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles all monitoring related callbacks."""
    query = update.callback_query

    # Check permission
    if not await require_module_permission(update, context, "monitoring"):
        return

    await query.answer()

    data = query.data.split(":")
    action = data[1]

    with SessionLocal() as db:
        if action == "list":
            # Clear any adding/editing state when going back to list
            context.user_data.pop("adding_monitor", None)
            context.user_data.pop("adding_monitor_step", None)
            context.user_data.pop("new_monitor_name", None)
            context.user_data.pop("editing_monitor_id", None)
            context.user_data.pop("editing_field", None)

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
                try:
                    await query.edit_message_text(
                        text, reply_markup=reply_markup, parse_mode="Markdown"
                    )
                except BadRequest as e:
                    if "Message is not modified" not in str(e):
                        raise

        elif action == "view":
            m_id = int(data[2])
            await _show_monitor_view(db, query, context, m_id)

        elif action == "refresh":
            m_id = int(data[2])
            monitor = get_monitor(db, m_id)
            if monitor:
                back_kb = InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ Back", callback_data=f"mon:view:{m_id}")]]
                )
                try:
                    await query.edit_message_text(
                        f"⏳ Checking {monitor.name}...", reply_markup=back_kb
                    )
                except BadRequest as e:
                    if "Message is not modified" not in str(e):
                        raise
                await check_monitor(db, monitor)
                # Show view directly
                await _show_monitor_view(db, query, context, m_id)

        elif action == "toggle":
            m_id = int(data[2])
            monitor = get_monitor(db, m_id)
            if monitor:
                update_monitor(db, m_id, enabled=not monitor.enabled)
                # Show view directly
                await _show_monitor_view(db, query, context, m_id)

        elif action == "delete_confirm":
            m_id = int(data[2])
            monitor = get_monitor(db, m_id)
            keyboard = [
                [InlineKeyboardButton("✅ Yes, Delete", callback_data=f"mon:delete_final:{m_id}")],
                [InlineKeyboardButton("❌ Cancel", callback_data=f"mon:view:{m_id}")],
            ]
            try:
                await query.edit_message_text(
                    f"⚠️ Are you sure you want to delete *{monitor.name}*?",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="Markdown",
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise

        elif action == "delete_final":
            m_id = int(data[2])
            delete_monitor(db, m_id)
            try:
                await query.edit_message_text("✅ Monitor deleted.")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            # Go back to list after a short delay or just show back button
            keyboard = [[InlineKeyboardButton("⬅️ Back to List", callback_data="mon:list")]]
            try:
                await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise

        elif action == "edit_menu":
            m_id = int(data[2])
            await _show_edit_menu(db, query, m_id)

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
                # Show edit menu directly
                await _show_edit_menu(db, query, m_id)
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
            try:
                await query.edit_message_text(text, reply_markup=back_kb, parse_mode="Markdown")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise

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
                try:
                    await query.edit_message_text(
                        f"📊 Generating charts for {monitor.name}...", reply_markup=stats_kb
                    )
                except BadRequest as e:
                    if "Message is not modified" not in str(e):
                        raise
                await query.message.reply_photo(
                    photo=chart_buf,
                    caption=f"📊 *{monitor.name}* ({range_label}, {len(recent)} checks)",
                    reply_markup=stats_kb,
                    parse_mode="Markdown",
                )
                # Show the view again in the original message
                await _show_monitor_view(db, query, context, m_id)

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
                # Show stats view directly to show updated state (hide the pin button)
                monitor = get_monitor(db, m_id)
                if not monitor:
                    return
                if new_range == "1h":
                    recent = get_check_runs_by_time_range(db, m_id, hours=1)
                elif new_range == "6h":
                    recent = get_check_runs_by_time_range(db, m_id, hours=6)
                elif new_range == "24h":
                    recent = get_check_runs_by_time_range(db, m_id, hours=24)
                elif new_range == "1month":
                    recent = get_check_runs_by_time_range(db, m_id, days=30)
                else:
                    recent = get_check_runs_by_time_range(db, m_id, hours=24)
                if not recent:
                    await query.answer("No data yet for charts.")
                    return
                time_range_buttons = [
                    InlineKeyboardButton(
                        f"{'◉' if new_range == '1h' else '○'} 1h",
                        callback_data=f"mon:stats:{m_id}:1h",
                    ),
                    InlineKeyboardButton(
                        f"{'◉' if new_range == '6h' else '○'} 6h",
                        callback_data=f"mon:stats:{m_id}:6h",
                    ),
                    InlineKeyboardButton(
                        f"{'◉' if new_range == '24h' else '○'} 24h",
                        callback_data=f"mon:stats:{m_id}:24h",
                    ),
                    InlineKeyboardButton(
                        f"{'◉' if new_range == '1month' else '○'} 1 month",
                        callback_data=f"mon:stats:{m_id}:1month",
                    ),
                ]
                stats_kb = InlineKeyboardMarkup(
                    [
                        time_range_buttons,
                        [InlineKeyboardButton("⬅️ Back", callback_data=f"mon:view:{m_id}")],
                    ]
                )
                range_labels = {
                    "1h": "last hour",
                    "6h": "last 6 hours",
                    "24h": "last 24 hours",
                    "1month": "last month",
                }
                range_label = range_labels.get(new_range, new_range)
                chart_buf = await generate_monitor_charts(monitor.name, recent, new_range)
                if query.message.photo:
                    await context.bot.send_photo(
                        chat_id=query.message.chat_id,
                        photo=chart_buf,
                        caption=f"📊 *{monitor.name}* ({range_label}, {len(recent)} checks)",
                        reply_markup=stats_kb,
                        parse_mode="Markdown",
                    )
                    await query.message.delete()
                else:
                    try:
                        await query.edit_message_text(
                            f"📊 Generating charts for {monitor.name}...", reply_markup=stats_kb
                        )
                    except BadRequest as e:
                        if "Message is not modified" not in str(e):
                            raise
                    await query.message.reply_photo(
                        photo=chart_buf,
                        caption=f"📊 *{monitor.name}* ({range_label}, {len(recent)} checks)",
                        reply_markup=stats_kb,
                        parse_mode="Markdown",
                    )

        elif action == "add_start":
            # Start the add monitor flow - prompt for name
            context.user_data["adding_monitor"] = True
            context.user_data["adding_monitor_step"] = "name"

            text = (
                "➕ *Add New Monitor*\n\n"
                "Please send the monitor *name* now.\n\n"
                "Example: `My Website`"
            )

            cancel_kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data="mon:list")]]
            )

            if query.message.photo:
                await query.message.delete()
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=text,
                    reply_markup=cancel_kb,
                    parse_mode="Markdown",
                )
            else:
                try:
                    await query.edit_message_text(
                        text, reply_markup=cancel_kb, parse_mode="Markdown"
                    )
                except BadRequest as e:
                    if "Message is not modified" not in str(e):
                        raise


async def handle_monitor_edit_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Handles text input when a user is editing a monitor field or adding a new monitor.
    Returns True if the message was handled, False otherwise.
    """
    if not update.message or not update.message.text:
        return False

    # Check if user is adding a monitor
    if context.user_data.get("adding_monitor"):
        step = context.user_data.get("adding_monitor_step")
        text_input = update.message.text.strip()

        if step == "name":
            # Store the name and prompt for URL
            context.user_data["new_monitor_name"] = text_input
            context.user_data["adding_monitor_step"] = "url"

            text = (
                f"✅ Name set: `{text_input}`\n\n"
                "Now please send the monitor *URL*.\n\n"
                "Example: `https://example.com`"
            )

            cancel_kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data="mon:list")]]
            )

            await update.message.reply_text(text, reply_markup=cancel_kb, parse_mode="Markdown")
            return True

        elif step == "url":
            # Validate URL and create monitor
            url = text_input
            name = context.user_data.get("new_monitor_name")

            # Basic URL validation
            if not url.startswith(("http://", "https://")):
                await update.message.reply_text(
                    "❌ Invalid URL. Please provide a URL starting with `http://` or `https://`.\n"
                    "Please try again or cancel.",
                    parse_mode="Markdown",
                )
                return True

            try:
                with SessionLocal() as db:
                    monitor = create_monitor(db, name=name, url=url)
                    await update.message.reply_text(
                        f"✅ Monitor *{monitor.name}* created successfully!",
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup(
                            [
                                [
                                    InlineKeyboardButton(
                                        "👁️ View Monitor", callback_data=f"mon:view:{monitor.id}"
                                    ),
                                    InlineKeyboardButton("📋 List", callback_data="mon:list"),
                                ]
                            ]
                        ),
                    )
            except Exception as e:
                logger.error(f"Error creating monitor: {e}")
                await update.message.reply_text(
                    f"❌ Error creating monitor: {str(e)}\nPlease try again or cancel."
                )
                return True

            # Clear adding state
            del context.user_data["adding_monitor"]
            del context.user_data["adding_monitor_step"]
            del context.user_data["new_monitor_name"]
            return True

    # Original edit monitor flow
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
