from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

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
