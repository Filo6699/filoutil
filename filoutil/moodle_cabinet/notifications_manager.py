"""Notifications Manager for Moodle Cabinet.

This module handles fetching notifications from Moodle and sending them to users.
"""

import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session
from telegram.ext import Application

from filoutil.db.models import MoodleNotification, SessionRefresh, User
from filoutil.db.postgres import SessionLocal

logger = logging.getLogger(__name__)

LMS_BASE_URL = "https://lms.astanait.edu.kz"
LMS_ENDPOINT = "/lib/ajax/service.php"
DEFAULT_NOTIFICATION_LIMIT = 20
DEFAULT_NOTIFICATION_OFFSET = 0


def format_time_ago(timestamp: int) -> str:
    """
    Format a Unix timestamp as a human-readable "time ago" string.
    Matches Moodle's format: "2 mins 14 secs ago", "1 hour 27 mins ago", "3 days 10 hours ago"

    Args:
        timestamp: Unix timestamp (seconds since epoch)

    Returns:
        Formatted string like "2 mins 14 secs ago", "1 hour 27 mins ago", "3 days 10 hours ago"
    """
    if not timestamp or timestamp <= 0:
        return "Unknown time"

    now = datetime.now(timezone.utc)
    created = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    delta = now - created

    total_seconds = int(delta.total_seconds())

    if total_seconds < 0:
        return "Just now"

    if total_seconds < 60:
        return f"{total_seconds} secs ago"

    # Calculate days, hours, minutes, seconds
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60

    parts = []

    # Moodle's pattern:
    # - Less than 1 hour: show minutes and seconds (e.g., "16 mins 43 secs ago")
    # - Less than 1 day: show hours and minutes (e.g., "1 hour 27 mins ago")
    # - 1 day or more: show days and hours (e.g., "3 days 1 hour ago", "2 days 7 hours ago")

    if days > 0:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
        if hours > 0:
            parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    elif hours > 0:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
        if minutes > 0:
            parts.append(f"{minutes} mins")
    else:
        # Less than an hour: show minutes and seconds
        if minutes > 0:
            parts.append(f"{minutes} mins")
        if seconds > 0:
            parts.append(f"{seconds} secs")

    if not parts:
        return "Just now"

    # Join parts with spaces and add "ago"
    return " ".join(parts) + " ago"


async def mark_all_notifications_as_read(
    sesskey: str, moodleSession: str, useridto: int, timecreatedto: int | None = None
) -> tuple[bool, str | None]:
    """
    Mark all notifications as read in Moodle.

    Args:
        sesskey: Moodle session key
        moodleSession: Moodle session cookie value
        useridto: User ID
        timecreatedto: Optional timestamp - mark notifications up to this time as read

    Returns:
        Tuple of (success: bool, error_message: str | None)
    """
    url = f"{LMS_BASE_URL}{LMS_ENDPOINT}"
    params = {"sesskey": sesskey, "info": "core_message_mark_all_notifications_as_read"}
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": LMS_BASE_URL,
        "Referer": f"{LMS_BASE_URL}/",
    }
    cookies = {"MoodleSession": moodleSession}

    args = {"useridto": str(useridto)}
    if timecreatedto:
        args["timecreatedto"] = timecreatedto

    payload = [
        {
            "index": 0,
            "methodname": "core_message_mark_all_notifications_as_read",
            "args": args,
        }
    ]

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.post(
                url, params=params, headers=headers, cookies=cookies, json=payload
            )

            if response.status_code == 200:
                try:
                    data = response.json()
                    logger.debug(f"Moodle mark all as read response: {data}")

                    if isinstance(data, list) and len(data) > 0:
                        item = data[0]
                        if "error" in item:
                            error_val = item["error"]
                            if error_val is False or error_val is None:
                                return True, None
                            elif error_val is True:
                                error_msg = item.get("message", "Unknown error")
                                return False, f"Moodle error: {error_msg}"
                            elif isinstance(error_val, dict):
                                error_msg = error_val.get("message", str(error_val))
                                return False, f"Moodle error: {error_msg}"
                            else:
                                return False, f"Moodle error: {error_val}"
                    return False, "Invalid response format"
                except Exception as e:
                    logger.warning(
                        f"Failed to parse mark all as read response JSON: {e}, response text: {response.text[:500]}"
                    )
                    return False, f"Failed to parse response: {str(e)}"
            else:
                error_text = response.text[:200] if response.text else "No response body"
                logger.error(f"Moodle returned status {response.status_code}: {error_text}")
                return False, f"HTTP {response.status_code}: {error_text}"

    except httpx.RequestError as e:
        return False, f"Request failed: {str(e)}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"


async def fetch_notifications(
    sesskey: str,
    moodleSession: str,
    useridto: int,
    limit: int = DEFAULT_NOTIFICATION_LIMIT,
    offset: int = DEFAULT_NOTIFICATION_OFFSET,
) -> tuple[bool, list[dict[str, Any]] | None, str | None]:
    """
    Fetch notifications from Moodle API.

    Args:
        sesskey: Moodle session key
        moodleSession: Moodle session cookie value
        useridto: User ID to fetch notifications for
        limit: Maximum number of notifications to fetch
        offset: Offset for pagination

    Returns:
        Tuple of (success: bool, notifications: list[dict] | None, error_message: str | None)
    """
    url = f"{LMS_BASE_URL}{LMS_ENDPOINT}"
    params = {"sesskey": sesskey, "info": "message_popup_get_popup_notifications"}
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": LMS_BASE_URL,
        "Referer": f"{LMS_BASE_URL}/",
    }
    cookies = {"MoodleSession": moodleSession}
    payload = [
        {
            "index": 0,
            "methodname": "message_popup_get_popup_notifications",
            "args": {"limit": limit, "offset": offset, "useridto": str(useridto)},
        }
    ]

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.post(
                url, params=params, headers=headers, cookies=cookies, json=payload
            )

            if response.status_code == 200:
                try:
                    data = response.json()
                    logger.debug(f"Moodle notifications response: {data}")

                    if isinstance(data, list) and len(data) > 0:
                        item = data[0]
                        if "error" in item:
                            error_val = item["error"]
                            if error_val is False or error_val is None:
                                # Success - extract notifications
                                notifications_data = item.get("data", {})
                                notifications = notifications_data.get("notifications", [])
                                return True, notifications, None
                            elif error_val is True:
                                error_msg = item.get("message", "Unknown error")
                                return False, None, f"Moodle error: {error_msg}"
                            elif isinstance(error_val, dict):
                                error_msg = error_val.get("message", str(error_val))
                                return False, None, f"Moodle error: {error_msg}"
                            else:
                                return False, None, f"Moodle error: {error_val}"
                    return False, None, "Invalid response format"
                except Exception as e:
                    logger.warning(
                        f"Failed to parse notifications response JSON: {e}, response text: {response.text[:500]}"
                    )
                    return False, None, f"Failed to parse response: {str(e)}"
            else:
                error_text = response.text[:200] if response.text else "No response body"
                logger.error(f"Moodle returned status {response.status_code}: {error_text}")
                return False, None, f"HTTP {response.status_code}: {error_text}"

    except httpx.RequestError as e:
        return False, None, f"Request failed: {str(e)}"
    except Exception as e:
        return False, None, f"Unexpected error: {str(e)}"


def store_notification(
    db: Session, user_id: int, notification_data: dict[str, Any]
) -> MoodleNotification | None:
    """
    Store a notification in the database if it doesn't already exist.

    Args:
        db: Database session
        user_id: User ID
        notification_data: Notification data from Moodle API

    Returns:
        MoodleNotification object if created, None if it already exists
    """
    notification_id = notification_data.get("id")
    if not notification_id:
        logger.warning("Notification data missing 'id' field")
        return None

    # Check if notification already exists
    existing = db.execute(
        select(MoodleNotification).where(MoodleNotification.notification_id == notification_id)
    ).scalar_one_or_none()

    if existing:
        return None  # Already exists

    # Create new notification
    notification = MoodleNotification(
        user_id=user_id,
        notification_id=notification_id,
        useridfrom=notification_data.get("useridfrom"),
        useridto=notification_data.get("useridto"),
        subject=notification_data.get("subject", ""),
        shortenedsubject=notification_data.get("shortenedsubject"),
        text=notification_data.get("text"),
        fullmessage=notification_data.get("fullmessage"),
        fullmessageformat=notification_data.get("fullmessageformat"),
        fullmessagehtml=notification_data.get("fullmessagehtml"),
        smallmessage=notification_data.get("smallmessage"),
        contexturl=notification_data.get("contexturl"),
        contexturlname=notification_data.get("contexturlname"),
        timecreated=notification_data.get("timecreated", 0),
        timecreatedpretty=notification_data.get("timecreatedpretty"),
        timeread=notification_data.get("timeread"),
        read=notification_data.get("read", False),
        deleted=notification_data.get("deleted", False),
        iconurl=notification_data.get("iconurl"),
        component=notification_data.get("component"),
        eventtype=notification_data.get("eventtype"),
        customdata=notification_data.get("customdata"),
        sent_to_user=False,
    )

    db.add(notification)
    db.commit()
    db.refresh(notification)
    return notification


def get_unsent_notifications(db: Session, user_id: int) -> list[MoodleNotification]:
    """
    Get all notifications that haven't been sent to the user yet.

    Args:
        db: Database session
        user_id: User ID

    Returns:
        List of MoodleNotification objects
    """
    return (
        db.execute(
            select(MoodleNotification)
            .where(
                MoodleNotification.user_id == user_id,
                MoodleNotification.sent_to_user == False,
                MoodleNotification.deleted == False,
            )
            .order_by(MoodleNotification.timecreated.desc())
        )
        .scalars()
        .all()
    )


def mark_notification_sent(db: Session, notification_id: int) -> MoodleNotification | None:
    """
    Mark a notification as sent to the user.

    Args:
        db: Database session
        notification_id: Database notification ID

    Returns:
        Updated MoodleNotification object or None if not found
    """
    notification = db.execute(
        select(MoodleNotification).where(MoodleNotification.id == notification_id)
    ).scalar_one_or_none()

    if notification:
        notification.sent_to_user = True
        notification.sent_at = datetime.utcnow()
        db.commit()
        db.refresh(notification)

    return notification


async def send_notification_to_user(
    app: Application, user_telegram_id: int, notification: MoodleNotification
) -> bool:
    """
    Send a notification to the user via Telegram.

    Args:
        app: Telegram application
        user_telegram_id: User's Telegram ID
        notification: MoodleNotification object

    Returns:
        True if sent successfully, False otherwise
    """
    try:
        # Format notification message
        subject = notification.subject or "New notification"
        time_str = format_time_ago(notification.timecreated)
        context_link = notification.contexturl or ""

        message = f"🔔 *{subject}*\n\n"
        if notification.smallmessage:
            message += f"{notification.smallmessage}\n\n"
        message += f"⏰ {time_str}"

        if context_link:
            message += f"\n🔗 [View details]({context_link})"

        await app.bot.send_message(
            chat_id=user_telegram_id,
            text=message,
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        return True
    except Exception as e:
        logger.error(f"Failed to send notification to user {user_telegram_id}: {e}")
        return False


async def process_notifications_for_user(
    app: Application, user_id: int, session_refresh: SessionRefresh
) -> None:
    """
    Fetch and process notifications for a user with an active session.

    Args:
        app: Telegram application
        user_id: User ID
        session_refresh: SessionRefresh object
    """
    try:
        # Get Moodle user ID - use stored value or extract from first notification
        moodle_user_id = session_refresh.moodle_user_id

        # If not stored, try to extract from first notification fetch
        if not moodle_user_id:
            # Fetch one notification to get useridto
            # Try with a dummy useridto first - Moodle might return the actual useridto in the response
            success, test_notifications, error_msg = await fetch_notifications(
                session_refresh.sesskey,
                session_refresh.moodleSession,
                0,  # Temporary - we'll get the actual ID from response
                limit=1,
            )
            if success and test_notifications and len(test_notifications) > 0:
                moodle_user_id = test_notifications[0].get("useridto")
                if moodle_user_id:
                    # Store it in the session refresh
                    with SessionLocal() as db:
                        from filoutil.db.models import SessionRefresh

                        stored_session = db.execute(
                            select(SessionRefresh).where(SessionRefresh.id == session_refresh.id)
                        ).scalar_one_or_none()
                        if stored_session:
                            stored_session.moodle_user_id = moodle_user_id
                            db.commit()
                            db.refresh(stored_session)
                            # Update the passed session_refresh object
                            session_refresh.moodle_user_id = moodle_user_id

        if not moodle_user_id:
            logger.warning(f"Could not determine Moodle user ID for user {user_id}")
            return

        # Fetch notifications from Moodle
        success, notifications, error_msg = await fetch_notifications(
            session_refresh.sesskey,
            session_refresh.moodleSession,
            moodle_user_id,
        )

        if not success:
            logger.warning(f"Failed to fetch notifications for user {user_id}: {error_msg}")
            return

        if not notifications:
            logger.debug(f"No notifications found for user {user_id}")
            return

        # Mark all notifications as read in Moodle
        # Use the latest notification's timestamp as timecreatedto
        latest_timestamp = max(
            (n.get("timecreated", 0) for n in notifications if n.get("timecreated")), default=None
        )

        mark_success, mark_error = await mark_all_notifications_as_read(
            session_refresh.sesskey,
            session_refresh.moodleSession,
            moodle_user_id,
            timecreatedto=latest_timestamp,
        )

        if mark_success:
            logger.debug(f"Marked all notifications as read for user {user_id}")
        else:
            logger.warning(f"Failed to mark notifications as read for user {user_id}: {mark_error}")

        # Store new notifications and send them
        with SessionLocal() as db:
            user = db.execute(select(User).where(User.id == user_id)).scalar_one_or_none()
            if not user:
                logger.warning(f"User {user_id} not found")
                return

            new_notifications = []
            for notification_data in notifications:
                stored_notification = store_notification(db, user_id, notification_data)
                if stored_notification:
                    new_notifications.append(stored_notification)
                    logger.info(
                        f"Stored new notification {stored_notification.notification_id} for user {user_id}"
                    )

            # Get user notification settings
            from filoutil.commands.notification_settings import get_user_notification_settings

            settings = get_user_notification_settings(db, user_id)

            # Apply filters based on settings
            notifications_to_send = []
            word_blacklist = settings.get("word_blacklist", [])

            for notification in new_notifications:
                # Filter by event type
                filter_event_types = settings.get("filter_event_types", [])
                if filter_event_types and notification.eventtype not in filter_event_types:
                    continue

                # Filter by component
                filter_components = settings.get("filter_components", [])
                if filter_components and notification.component not in filter_components:
                    continue

                # Filter by word blacklist (case-insensitive)
                if word_blacklist:
                    # Check all text fields for blacklisted words
                    text_fields = [
                        notification.subject or "",
                        notification.shortenedsubject or "",
                        notification.text or "",
                        notification.fullmessage or "",
                        notification.smallmessage or "",
                    ]
                    combined_text = " ".join(text_fields).lower()

                    # Check if any blacklisted word appears in the notification
                    if any(word.lower() in combined_text for word in word_blacklist):
                        logger.info(
                            f"Skipping notification {notification.notification_id} due to word blacklist"
                        )
                        continue

                notifications_to_send.append(notification)

            # Limit by max_notifications_per_batch
            max_batch = settings.get("max_notifications_per_batch", 5)
            notifications_to_send = notifications_to_send[:max_batch]

            # Send notifications
            for notification in notifications_to_send:
                sent = await send_notification_to_user(app, user.telegram_id, notification)
                if sent:
                    mark_notification_sent(db, notification.id)
                    logger.info(
                        f"Sent notification {notification.notification_id} to user {user.telegram_id}"
                    )

    except Exception as e:
        logger.error(f"Error processing notifications for user {user_id}: {e}", exc_info=True)
