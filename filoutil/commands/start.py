from telegram import Update
from telegram.ext import ContextTypes

from filoutil.commands.menu import menu_command
from filoutil.db.postgres import SessionLocal
from filoutil.db.users import ensure_user_by_telegram_id, get_user_by_telegram_id


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
        ensure_user_by_telegram_id(db, telegram_id, username)
        user = get_user_by_telegram_id(db, telegram_id)

        if not user or not user.whitelisted:
            await update.message.reply_text(
                "Hello! You're not whitelisted yet.\n\n"
                f"Your Telegram ID is: {telegram_id}\n"
                "Ask an admin to whitelist you, then try /hello or send a message."
            )
            return

    # Show the main menu
    await menu_command(update, context)
