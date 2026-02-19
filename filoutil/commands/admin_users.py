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
ANNOUNCEMENT_PENDING_KEY = "admin_announcement_pending"


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

    # Announcement broadcast button
    keyboard.append(
        [
            InlineKeyboardButton(
                "📣 Make Announcement", callback_data="admin_users:announcement:start"
            )
        ]
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
        [
            InlineKeyboardButton(
                "📢 Notify Permissions",
                callback_data=f"admin_users:notify_permissions:{user.id}",
            )
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

    await query.answer()

    data = query.data.split(":")
    action = data[1]

    if action == "list":
        # Show user list
        try:
            page = int(data[2]) if len(data) > 2 else 0
            if page < 0:
                raise ValueError("Invalid page number")
        except (ValueError, IndexError):
            await query.answer("❌ Invalid request.", show_alert=True)
            return

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
        try:
            user_id = int(data[2])
            if user_id <= 0:
                raise ValueError("Invalid user ID")
        except (ValueError, IndexError):
            await query.answer("❌ Invalid request.", show_alert=True)
            return

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
        try:
            user_id = int(data[2])
            if user_id <= 0:
                raise ValueError("Invalid user ID")
        except (ValueError, IndexError):
            await query.answer("❌ Invalid request.", show_alert=True)
            return

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
        try:
            user_id = int(data[2])
            if user_id <= 0:
                raise ValueError("Invalid user ID")
            if len(data) < 4:
                raise IndexError("Missing module name")
            module = data[3]
        except (ValueError, IndexError):
            await query.answer("❌ Invalid request.", show_alert=True)
            return

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

    elif action == "notify_permissions":
        # Notify user about their current permissions
        try:
            user_id = int(data[2])
            if user_id <= 0:
                raise ValueError("Invalid user ID")
        except (ValueError, IndexError):
            await query.answer("❌ Invalid request.", show_alert=True)
            return

        with SessionLocal() as db:
            from filoutil.db.models import User

            user = db.get(User, user_id)
            if not user:
                await query.answer("❌ User not found.", show_alert=True)
                return

            # Get user's granted permissions
            permissions = get_user_permissions(db, user.id)

            if not permissions:
                await query.answer("❌ User has no permissions to notify about.", show_alert=True)
                return

            # Format permission names for display
            permission_names = {
                "monitoring": "Monitoring",
                "moodle": "Moodle",
            }

            # Build the notification message
            permission_list = "\n".join(
                [f"  • {permission_names.get(perm, perm.title())}" for perm in permissions]
            )

            message = (
                f"🔔 *Permission Update*\n\n"
                f"Your permissions have been updated. Your current permissions are:\n\n"
                f"{permission_list}"
            )

            keyboard = None
            if "moodle" in permissions:
                message += (
                    "\n\n*How to connect Moodle:*\n"
                    "1. Open 🎓 Moodle from the menu\n"
                    "2. Add your Moodle account\n"
                    "3. The bot will automatically sync grades\n"
                    "4. You will receive notifications about new grades"
                )
                keyboard = InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("🎓 Moodle", callback_data="menu:moodle")],
                        [InlineKeyboardButton("🏠 Main Menu", callback_data="menu:main")],
                    ]
                )

            try:
                await context.bot.send_message(
                    chat_id=user.telegram_id,
                    text=message,
                    parse_mode="Markdown",
                    reply_markup=keyboard,
                )
                await query.answer("✅ Notification sent to user", show_alert=False)
            except Exception as e:
                logger.error(f"Failed to send permission notification to user {user_id}: {e}")
                await query.answer("❌ Failed to send notification.", show_alert=True)

    elif action == "announcement":
        sub_action = data[2] if len(data) > 2 else ""

        if sub_action == "start":
            context.user_data[ANNOUNCEMENT_PENDING_KEY] = True
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "❌ Cancel", callback_data="admin_users:announcement:cancel"
                        )
                    ]
                ]
            )
            await query.edit_message_text(
                "📣 *Announcement Mode*\n\n"
                "Send the next message you want to broadcast to all whitelisted users.\n"
                "The bot will resend your message to every whitelisted user in DM.",
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
            return

        if sub_action == "cancel":
            context.user_data.pop(ANNOUNCEMENT_PENDING_KEY, None)
            # Return to user list page 0
            with SessionLocal() as db:
                from sqlalchemy import select

                from filoutil.db.models import User

                users = db.execute(select(User).order_by(User.created_at.desc())).scalars().all()
                total_users = len(users)
                total_pages = (total_users + USERS_PER_PAGE - 1) // USERS_PER_PAGE if users else 1
                text = (
                    f"👥 *User Management*\n\n"
                    f"*Total Users:* {total_users}\n\n"
                    f"*Page 1 of {total_pages}*\n\n"
                )
                for user in users[:USERS_PER_PAGE]:
                    username_display = escape_markdown(user.username or "No username", version=1)
                    whitelist_status = "✅" if user.whitelisted else "❌"
                    role_display = "👑 Admin" if user.role == "admin" else "👤 User"
                    text += f"{whitelist_status} {role_display}: {username_display}\n"

                reply_markup = get_user_list_keyboard(users, page=0)
                await query.edit_message_text(
                    text,
                    reply_markup=reply_markup,
                    parse_mode="Markdown",
                )
            return

        await query.answer("❌ Invalid request.", show_alert=True)
        return

    elif action == "quiet_hours":
        # Handle quiet hours management
        from filoutil.commands.moodle.quiet_hours import quiet_hours_callback

        await quiet_hours_callback(update, context)


async def handle_admin_announcement_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """Handle admin announcement broadcast message forwarding."""
    if not update.message:
        return False

    if not context.user_data.get(ANNOUNCEMENT_PENDING_KEY):
        return False

    # Clear state immediately so one message == one broadcast
    context.user_data.pop(ANNOUNCEMENT_PENDING_KEY, None)

    admin_user = update.message.from_user
    if not admin_user:
        return False

    with SessionLocal() as db:
        from sqlalchemy import select

        from filoutil.db.models import User

        db_admin = get_user_by_telegram_id(db, admin_user.id)
        if not db_admin or db_admin.role != "admin":
            await update.message.reply_text("❌ This command is only available to admins.")
            return True

        recipients = db.execute(select(User).where(User.whitelisted.is_(True))).scalars().all()

    if not recipients:
        await update.message.reply_text("❌ No whitelisted users found.")
        return True

    success_count = 0
    failed_count = 0

    for recipient in recipients:
        try:
            await context.bot.copy_message(
                chat_id=recipient.telegram_id,
                from_chat_id=update.message.chat_id,
                message_id=update.message.message_id,
            )
            success_count += 1
        except Exception as e:
            failed_count += 1
            logger.warning(
                "Announcement delivery failed for telegram_id=%s: %s",
                recipient.telegram_id,
                e,
            )

    await update.message.reply_text(
        f"📣 Announcement sent.\n✅ Delivered: {success_count}\n❌ Failed: {failed_count}"
    )
    return True
