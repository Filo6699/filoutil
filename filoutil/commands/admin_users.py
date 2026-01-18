"""Admin user management panel for managing bot users, whitelist, and permissions."""

import logging
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown

from filoutil.commands.admin import ensure_admin
from filoutil.config import format_time_for_display
from filoutil.db.permissions import (
    get_user_permissions,
    grant_permission,
    has_permission,
    revoke_permission,
)
from filoutil.db.postgres import SessionLocal
from filoutil.db.users import get_user_by_telegram_id

logger = logging.getLogger(__name__)

USERS_PER_PAGE = 10


def get_user_list_keyboard(users: list, page: int = 0) -> InlineKeyboardMarkup:
    """Generate keyboard for user list with pagination."""
    keyboard = []

    # Pagination
    total_pages = (len(users) + USERS_PER_PAGE - 1) // USERS_PER_PAGE if users else 1
    start_idx = page * USERS_PER_PAGE
    end_idx = start_idx + USERS_PER_PAGE
    page_users = users[start_idx:end_idx]

    # User buttons
    for user in page_users:
        username_display = escape_markdown(user.username or "No username", version=1)
        whitelist_status = "✅" if user.whitelisted else "❌"
        role_display = "👑" if user.role == "admin" else "👤"
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"{whitelist_status} {role_display} {username_display}",
                    callback_data=f"admin_users:view:{user.id}",
                )
            ]
        )

    # Pagination buttons
    nav_buttons = []
    if page > 0:
        nav_buttons.append(
            InlineKeyboardButton("⬅️ Previous", callback_data=f"admin_users:list:{page - 1}")
        )
    if page < total_pages - 1:
        nav_buttons.append(
            InlineKeyboardButton("Next ➡️", callback_data=f"admin_users:list:{page + 1}")
        )
    if nav_buttons:
        keyboard.append(nav_buttons)

    # Refresh button
    keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data=f"admin_users:list:{page}")])

    # Quiet hours management button
    keyboard.append(
        [InlineKeyboardButton("🔇 Quiet Hours", callback_data="admin:quiet_hours:menu")]
    )

    # Back to main menu button
    keyboard.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu:main")])

    return InlineKeyboardMarkup(keyboard)


def get_user_details_keyboard(user_id: int, user) -> InlineKeyboardMarkup:
    """Generate keyboard for user details view."""
    with SessionLocal() as db:
        permissions = get_user_permissions(db, user.id)
        has_monitoring = has_permission(db, user.id, "monitoring")
        has_moodle = has_permission(db, user.id, "moodle")

    keyboard = [
        [
            InlineKeyboardButton(
                f"{'✅' if user.whitelisted else '❌'} Whitelist",
                callback_data=f"admin_users:toggle_whitelist:{user.id}",
            )
        ],
        [
            InlineKeyboardButton(
                f"{'✅' if has_monitoring else '❌'} Monitoring",
                callback_data=f"admin_users:toggle_permission:{user.id}:monitoring",
            ),
            InlineKeyboardButton(
                f"{'✅' if has_moodle else '❌'} Moodle",
                callback_data=f"admin_users:toggle_permission:{user.id}:moodle",
            ),
        ],
        [InlineKeyboardButton("⬅️ Back to List", callback_data="admin_users:list:0")],
    ]
    return InlineKeyboardMarkup(keyboard)


async def admin_users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /admin command to show user management panel."""
    if not await ensure_admin(update, context):
        return

    with SessionLocal() as db:
        from sqlalchemy import select

        from filoutil.db.models import User

        users = db.execute(select(User).order_by(User.created_at.desc())).scalars().all()

        if not users:
            await update.message.reply_text("No users found.")
            return

        total_users = len(users)
        page = 0

        text = f"👥 *User Management*\n\n*Total Users:* {total_users}\n\n*Page 1*\n\n"

        # Show first page of users
        start_idx = page * USERS_PER_PAGE
        end_idx = start_idx + USERS_PER_PAGE
        page_users = users[start_idx:end_idx]

        for user in page_users:
            username_display = escape_markdown(user.username or "No username", version=1)
            whitelist_status = "✅" if user.whitelisted else "❌"
            role_display = "👑 Admin" if user.role == "admin" else "👤 User"
            text += f"{whitelist_status} {role_display}: {username_display}\n"

        reply_markup = get_user_list_keyboard(users, page=page)
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")


async def admin_users_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle callback queries for admin user management."""
    query = update.callback_query
    if not query:
        return

    await query.answer()

    # Check admin status
    if not query.from_user:
        return

    with SessionLocal() as db:
        admin_user = get_user_by_telegram_id(db, query.from_user.id)
        if not admin_user or admin_user.role != "admin":
            await query.answer("❌ This command is only available to admins.", show_alert=True)
            return

    data = query.data.split(":")
    action = data[1]

    if action == "list":
        # Show user list
        page = int(data[2]) if len(data) > 2 else 0

        with SessionLocal() as db:
            from sqlalchemy import select

            from filoutil.db.models import User

            users = db.execute(select(User).order_by(User.created_at.desc())).scalars().all()

            if not users:
                try:
                    await query.edit_message_text("No users found.")
                except BadRequest:
                    pass
                return

            total_users = len(users)
            total_pages = (total_users + USERS_PER_PAGE - 1) // USERS_PER_PAGE

            text = f"👥 *User Management*\n\n*Total Users:* {total_users}\n\n*Page {page + 1} of {total_pages}*\n\n"

            start_idx = page * USERS_PER_PAGE
            end_idx = start_idx + USERS_PER_PAGE
            page_users = users[start_idx:end_idx]

            for user in page_users:
                username_display = escape_markdown(user.username or "No username", version=1)
                whitelist_status = "✅" if user.whitelisted else "❌"
                role_display = "👑 Admin" if user.role == "admin" else "👤 User"
                text += f"{whitelist_status} {role_display}: {username_display}\n"

            reply_markup = get_user_list_keyboard(users, page=page)

            try:
                await query.edit_message_text(
                    text, reply_markup=reply_markup, parse_mode="Markdown"
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise

    elif action == "view":
        # Show user details
        user_id = int(data[2])

        with SessionLocal() as db:
            from filoutil.db.models import User

            user = db.get(User, user_id)
            if not user:
                await query.answer("❌ User not found.", show_alert=True)
                return

            permissions = get_user_permissions(db, user.id)
            has_monitoring = has_permission(db, user.id, "monitoring")
            has_moodle = has_permission(db, user.id, "moodle")

            username_display = escape_markdown(user.username or "No username", version=1)
            role_display = "👑 Admin" if user.role == "admin" else "👤 User"
            whitelist_status = "✅ Whitelisted" if user.whitelisted else "❌ Not whitelisted"

            # Format timestamps in configured timezone
            if user.last_activity_at:
                last_activity_dt = user.last_activity_at
                if last_activity_dt.tzinfo is None:
                    last_activity_dt = last_activity_dt.replace(tzinfo=timezone.utc)
                last_activity = format_time_for_display(last_activity_dt)
            else:
                last_activity = "Never"

            if user.created_at:
                created_at_dt = user.created_at
                if created_at_dt.tzinfo is None:
                    created_at_dt = created_at_dt.replace(tzinfo=timezone.utc)
                created_at = format_time_for_display(created_at_dt)
            else:
                created_at = "Unknown"

            text = (
                f"👤 *User Details*\n\n"
                f"*Username:* {username_display}\n"
                f"*Telegram ID:* `{user.telegram_id}`\n"
                f"*Role:* {role_display}\n"
                f"*Status:* {whitelist_status}\n\n"
                f"*Permissions:*\n"
                f"  • Monitoring: {'✅' if has_monitoring else '❌'}\n"
                f"  • Moodle: {'✅' if has_moodle else '❌'}\n\n"
                f"*Last Activity:* {last_activity}\n"
                f"*Created:* {created_at}\n"
            )

            reply_markup = get_user_details_keyboard(user_id, user)

            try:
                await query.edit_message_text(
                    text, reply_markup=reply_markup, parse_mode="Markdown"
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise

    elif action == "quiet_hours":
        # Handle quiet hours management
        from filoutil.commands.moodle.quiet_hours import quiet_hours_callback

        await quiet_hours_callback(update, context)

    elif action == "toggle_whitelist":
        # Toggle whitelist status
        user_id = int(data[2])

        with SessionLocal() as db:
            from filoutil.db.models import User

            user = db.get(User, user_id)
            if not user:
                await query.answer("❌ User not found.", show_alert=True)
                return

            user.whitelisted = not user.whitelisted
            db.commit()
            db.refresh(user)

            status = "whitelisted" if user.whitelisted else "removed from whitelist"
            await query.answer(f"✅ User {status}")

            # Refresh user details view
            permissions = get_user_permissions(db, user.id)
            has_monitoring = has_permission(db, user.id, "monitoring")
            has_moodle = has_permission(db, user.id, "moodle")

            username_display = escape_markdown(user.username or "No username", version=1)
            role_display = "👑 Admin" if user.role == "admin" else "👤 User"
            whitelist_status = "✅ Whitelisted" if user.whitelisted else "❌ Not whitelisted"

            # Format timestamps in configured timezone
            if user.last_activity_at:
                last_activity_dt = user.last_activity_at
                if last_activity_dt.tzinfo is None:
                    last_activity_dt = last_activity_dt.replace(tzinfo=timezone.utc)
                last_activity = format_time_for_display(last_activity_dt)
            else:
                last_activity = "Never"

            if user.created_at:
                created_at_dt = user.created_at
                if created_at_dt.tzinfo is None:
                    created_at_dt = created_at_dt.replace(tzinfo=timezone.utc)
                created_at = format_time_for_display(created_at_dt)
            else:
                created_at = "Unknown"

            text = (
                f"👤 *User Details*\n\n"
                f"*Username:* {username_display}\n"
                f"*Telegram ID:* `{user.telegram_id}`\n"
                f"*Role:* {role_display}\n"
                f"*Status:* {whitelist_status}\n\n"
                f"*Permissions:*\n"
                f"  • Monitoring: {'✅' if has_monitoring else '❌'}\n"
                f"  • Moodle: {'✅' if has_moodle else '❌'}\n\n"
                f"*Last Activity:* {last_activity}\n"
                f"*Created:* {created_at}\n"
            )

            reply_markup = get_user_details_keyboard(user_id, user)

            try:
                await query.edit_message_text(
                    text, reply_markup=reply_markup, parse_mode="Markdown"
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise

    elif action == "quiet_hours":
        # Handle quiet hours management
        from filoutil.commands.moodle.quiet_hours import quiet_hours_callback

        await quiet_hours_callback(update, context)

    elif action == "toggle_permission":
        # Toggle module permission
        user_id = int(data[2])
        module = data[3]

        if module not in ["monitoring", "moodle"]:
            await query.answer("❌ Invalid module.", show_alert=True)
            return

        with SessionLocal() as db:
            from filoutil.db.models import User

            user = db.get(User, user_id)
            if not user:
                await query.answer("❌ User not found.", show_alert=True)
                return

            admin_telegram_id = query.from_user.id if query.from_user else None

            if has_permission(db, user.id, module):
                revoke_permission(db, user.id, module)
                action_text = "revoked"
            else:
                grant_permission(db, user.id, module, granted_by=admin_telegram_id)
                action_text = "granted"

            module_display = "Monitoring" if module == "monitoring" else "Moodle"
            await query.answer(f"✅ {module_display} permission {action_text}")

            # Refresh user details view
            permissions = get_user_permissions(db, user.id)
            has_monitoring = has_permission(db, user.id, "monitoring")
            has_moodle = has_permission(db, user.id, "moodle")

            username_display = escape_markdown(user.username or "No username", version=1)
            role_display = "👑 Admin" if user.role == "admin" else "👤 User"
            whitelist_status = "✅ Whitelisted" if user.whitelisted else "❌ Not whitelisted"

            # Format timestamps in configured timezone
            if user.last_activity_at:
                last_activity_dt = user.last_activity_at
                if last_activity_dt.tzinfo is None:
                    last_activity_dt = last_activity_dt.replace(tzinfo=timezone.utc)
                last_activity = format_time_for_display(last_activity_dt)
            else:
                last_activity = "Never"

            if user.created_at:
                created_at_dt = user.created_at
                if created_at_dt.tzinfo is None:
                    created_at_dt = created_at_dt.replace(tzinfo=timezone.utc)
                created_at = format_time_for_display(created_at_dt)
            else:
                created_at = "Unknown"

            text = (
                f"👤 *User Details*\n\n"
                f"*Username:* {username_display}\n"
                f"*Telegram ID:* `{user.telegram_id}`\n"
                f"*Role:* {role_display}\n"
                f"*Status:* {whitelist_status}\n\n"
                f"*Permissions:*\n"
                f"  • Monitoring: {'✅' if has_monitoring else '❌'}\n"
                f"  • Moodle: {'✅' if has_moodle else '❌'}\n\n"
                f"*Last Activity:* {last_activity}\n"
                f"*Created:* {created_at}\n"
            )

            reply_markup = get_user_details_keyboard(user_id, user)

            try:
                await query.edit_message_text(
                    text, reply_markup=reply_markup, parse_mode="Markdown"
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise

    elif action == "quiet_hours":
        # Handle quiet hours management
        from filoutil.commands.moodle.quiet_hours import quiet_hours_callback

        await quiet_hours_callback(update, context)
