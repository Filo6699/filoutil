"""Scheduler for Moodle Cabinet notifications.

This module periodically checks for new Moodle notifications and sends them to users.
"""

import asyncio
import logging

from sqlalchemy import select
from telegram.ext import Application

from filoutil.commands.notification_settings import get_user_notification_settings
from filoutil.db.models import SessionRefresh, User
from filoutil.db.postgres import SessionLocal
from filoutil.db.session_refresh import get_all_active_sessions
from filoutil.moodle_cabinet.notifications_manager import process_notifications_for_user

logger = logging.getLogger(__name__)

# Default check interval for notifications (in seconds)
DEFAULT_NOTIFICATION_CHECK_INTERVAL = 300  # 5 minutes


async def notifications_task(app: Application) -> None:
    """
    Background task that periodically checks for new Moodle notifications
    for all users with active sessions and sends them notifications.

    This task:
    1. Gets all active session refresh records
    2. Checks user notification settings
    3. For each enabled session, fetches notifications from Moodle
    4. Stores new notifications in the database
    5. Sends new notifications to users via Telegram (respecting settings)
    """
    logger.info("Starting Moodle notifications task...")

    while True:
        try:
            with SessionLocal() as db:
                active_sessions = get_all_active_sessions(db)

                if not active_sessions:
                    logger.debug("No active sessions found, skipping notification check")
                else:
                    logger.debug(
                        f"Checking notifications for {len(active_sessions)} active sessions"
                    )

                    # Process notifications for each active session
                    tasks = []
                    for session in active_sessions:
                        user = db.execute(
                            select(User).where(User.id == session.user_id)
                        ).scalar_one_or_none()

                        if user:
                            # Check user's notification settings
                            settings = get_user_notification_settings(db, user.id)

                            # Skip if notifications are disabled
                            if not settings.get("enabled", True):
                                logger.debug(f"Notifications disabled for user {user.id}")
                                continue

                            tasks.append(
                                process_notifications_for_user(app, session.user_id, session)
                            )

                    if tasks:
                        await asyncio.gather(*tasks, return_exceptions=True)

        except Exception as e:
            logger.error(f"Error in notifications_task: {e}", exc_info=True)

        # Use dynamic interval based on user settings (for now use default)
        # In the future, we could use the minimum interval from all active users
        await asyncio.sleep(DEFAULT_NOTIFICATION_CHECK_INTERVAL)
