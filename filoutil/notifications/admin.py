"""Admin notification utilities."""

import logging

from telegram import Bot
from telegram.helpers import escape_markdown

from filoutil.db.postgres import SessionLocal
from filoutil.db.status import get_admins

logger = logging.getLogger(__name__)


async def notify_admins_new_user(bot: Bot, telegram_id: int, username: str | None) -> None:
    """Notify all admins when a new user is encountered.

    Args:
        bot: Telegram bot instance
        telegram_id: New user's Telegram ID
        username: New user's username (can be None)
    """
    try:
        with SessionLocal() as db:
            admins = get_admins(db)
            if not admins:
                logger.debug("No admins found to notify about new user")
                return

            username_display = escape_markdown(username or "No username", version=1)
            message = (
                f"👤 *New User Encountered*\n\n"
                f"*Username:* {username_display}\n"
                f"*Telegram ID:* `{telegram_id}`\n\n"
                f"The user is not whitelisted yet. You can manage them in the admin panel."
            )

            for admin in admins:
                try:
                    await bot.send_message(
                        chat_id=admin.telegram_id,
                        text=message,
                        parse_mode="Markdown",
                    )
                except Exception as e:
                    logger.error(f"Failed to notify admin {admin.telegram_id} about new user: {e}")

    except Exception as e:
        logger.error(f"Error notifying admins about new user: {e}")
