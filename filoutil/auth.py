from __future__ import annotations

from functools import wraps

from telegram import Update
from telegram.ext import ContextTypes

from filoutil.db.permissions import has_permission
from filoutil.db.postgres import SessionLocal
from filoutil.db.users import ensure_user_by_telegram_id, get_user_by_telegram_id


async def ensure_user_and_check_whitelisted(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    """Ensure a user row exists, then gate handlers on `whitelisted`.

    Returns:
        True if the user is whitelisted and the handler should proceed.
        False if the user is not whitelisted (a response is sent).
    """

    if update.message is None or update.message.from_user is None:
        return False

    telegram_user = update.message.from_user
    telegram_id = telegram_user.id
    username = telegram_user.username

    with SessionLocal() as db:
        ensure_user_by_telegram_id(db, telegram_id, username)
        user = get_user_by_telegram_id(db, telegram_id)

        # Track activity
        if user:
            from filoutil.db.users import update_user_activity

            update_user_activity(db, telegram_id)

        if not user:
            await update.message.reply_text(
                "Something went wrong while looking up your user record. Please try again."
            )
            return False

        if not user.whitelisted:
            await update.message.reply_text(
                "You're not whitelisted yet.\n\n"
                f"Your Telegram ID is: {telegram_id}\n"
                "Ask an admin to whitelist you, then try again."
            )
            return False

    return True


async def require_module_permission(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    module: str,
) -> bool:
    """Check if user is whitelisted AND has permission for a specific module.
    Works with both message updates and callback query updates.

    Args:
        update: Telegram update object
        context: Telegram context object
        module: Module name ("monitoring" or "moodle")

    Returns:
        True if the user is whitelisted and has the module permission.
        False if the user is not whitelisted or lacks permission (a response is sent).
    """
    # Handle callback queries
    if update.callback_query and update.callback_query.from_user:
        telegram_id = update.callback_query.from_user.id

        with SessionLocal() as db:
            user = get_user_by_telegram_id(db, telegram_id)
            if not user:
                await update.callback_query.answer(
                    "Something went wrong while looking up your user record.", show_alert=True
                )
                return False

            if not user.whitelisted:
                await update.callback_query.answer(
                    "You're not whitelisted yet. Ask an admin to whitelist you.", show_alert=True
                )
                return False

            # Check module permission
            if not has_permission(db, user.id, module):
                module_display = "Monitoring" if module == "monitoring" else "Moodle"
                await update.callback_query.answer(
                    f"You don't have permission to use the {module_display} module. "
                    f"Ask an admin to grant you access.",
                    show_alert=True,
                )
                return False

        return True

    # Handle messages (original logic)
    # First check whitelist
    if not await ensure_user_and_check_whitelisted(update, context):
        return False

    if update.message is None or update.message.from_user is None:
        return False

    telegram_id = update.message.from_user.id

    with SessionLocal() as db:
        user = get_user_by_telegram_id(db, telegram_id)
        if not user:
            await update.message.reply_text(
                "Something went wrong while looking up your user record. Please try again."
            )
            return False

        # Check module permission
        if not has_permission(db, user.id, module):
            module_display = "Monitoring" if module == "monitoring" else "Moodle"
            await update.message.reply_text(
                f"You don't have permission to use the {module_display} module.\n\n"
                f"Your Telegram ID is: {telegram_id}\n"
                f"Ask an admin to grant you access to the {module_display} module."
            )
            return False

    return True
