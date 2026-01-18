from telegram import Update
from telegram.ext import ContextTypes

from filoutil.commands.menu import menu_command
from filoutil.db.postgres import SessionLocal
from filoutil.db.users import ensure_user_by_telegram_id


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    if update.message.from_user is None:
        await update.message.reply_text("Hello!")
        return

    telegram_user = update.message.from_user
    telegram_id = telegram_user.id
    username = telegram_user.username

    with SessionLocal() as db:
        user, is_new = ensure_user_by_telegram_id(db, telegram_id, username)

        # Notify admins if this is a new user
        if is_new:
            from filoutil.notifications.admin import notify_admins_new_user

            await notify_admins_new_user(context.bot, telegram_id, username)

        if not user or not user.whitelisted:
            await update.message.reply_text(
                "Hello! You're not whitelisted yet.\n\n"
                f"Your Telegram ID is: {telegram_id}\n"
                "Wait for an admin to whitelist you, then try again."
            )
            return

    # Show the main menu
    await menu_command(update, context)
