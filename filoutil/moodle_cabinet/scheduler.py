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
from filoutil.moodle_cabinet.course_watcher import (
    DEFAULT_COURSE_CHECK_INTERVAL,
    process_courses_for_user,
)
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


async def course_watcher_task(app: Application) -> None:
    """
    Background task that periodically checks for course and grade changes
    for all users with active sessions and sends them notifications.

    This task:
    1. Gets all active session refresh records
    2. For each session, fetches courses from Moodle
    3. Syncs courses to database
    4. For each course, fetches grades from Moodle
    5. Detects grade changes (new grades or updated grades)
    6. Syncs grades to database
    7. Sends notifications for grade changes

    Note: This task is skipped during quiet hours (only session refresh task runs).
    """
    logger.info("Starting Moodle course watcher task...")

    while True:
        try:
            with SessionLocal() as db:
                # Check if we're in quiet hours
                from filoutil.db.moodle_quiet_hours import is_in_quiet_hours

                if is_in_quiet_hours(db):
                    logger.debug("Skipping course watcher task - currently in quiet hours")
                    # Sleep for a shorter interval during quiet hours to check more frequently
                    await asyncio.sleep(60)  # Check every minute during quiet hours
                    continue

                active_sessions = get_all_active_sessions(db)

                if not active_sessions:
                    logger.debug("No active sessions found, skipping course check")
                else:
                    logger.debug(
                        f"Checking courses and grades for {len(active_sessions)} active sessions"
                    )

                    # Track processed user IDs to avoid processing the same user multiple times
                    # (users can have multiple sessions from different devices)
                    processed_user_ids = set()
                    tasks = []
                    skipped_sessions = 0

                    for session in active_sessions:
                        if session.user_id in processed_user_ids:
                            skipped_sessions += 1
                            logger.debug(
                                f"Skipping session {session.id} - user {session.user_id} already processed"
                            )
                            continue

                        processed_user_ids.add(session.user_id)
                        tasks.append(process_courses_for_user(app, session.user_id, session))

                    if skipped_sessions > 0:
                        logger.info(
                            f"Skipped {skipped_sessions} duplicate session(s) for already processed users"
                        )

                    if tasks:
                        await asyncio.gather(*tasks, return_exceptions=True)

        except Exception as e:
            logger.error(f"Error in course_watcher_task: {e}", exc_info=True)

        # Sleep for the configured interval
        await asyncio.sleep(DEFAULT_COURSE_CHECK_INTERVAL)
