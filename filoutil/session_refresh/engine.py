import asyncio
import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session
from telegram.ext import Application

from filoutil.db.models import SessionRefresh
from filoutil.db.postgres import SessionLocal
from filoutil.db.session_refresh import stop_session_refresh

logger = logging.getLogger(__name__)

LMS_BASE_URL = "https://lms.astanait.edu.kz"
LMS_ENDPOINT = "/lib/ajax/service.php"
MAX_RETRIES = 3
RETRY_DELAYS = [3, 5, 10]


async def get_session_time_remaining(
    sesskey: str, moodleSession: str
) -> tuple[bool, int | None, str | None]:
    """
    Get the time remaining in the session from Moodle AJAX.

    Returns:
        Tuple of (success: bool, time_remaining_seconds: int | None, error_message: str | None)
    """
    url = f"{LMS_BASE_URL}{LMS_ENDPOINT}"
    params = {"sesskey": sesskey, "info": "refresh"}
    headers = {"Content-Type": "application/json"}
    cookies = {"MoodleSession": moodleSession}
    payload = [{"index": 0, "methodname": "core_session_time_remaining", "args": {}}]

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.post(
                url, params=params, headers=headers, cookies=cookies, json=payload
            )

            if response.status_code == 200:
                try:
                    data = response.json()
                    logger.debug(f"LMS time_remaining response: {data}")
                    # Moodle AJAX responses are typically arrays with error objects
                    if isinstance(data, list) and len(data) > 0:
                        item = data[0]
                        # Check for error field
                        if "error" in item:
                            error_val = item["error"]
                            # If error is False or None, it's success
                            if error_val is False or error_val is None:
                                # Extract time remaining from data field
                                if "data" in item:
                                    timeremaining = item["data"].get("timeremaining")
                                    if timeremaining is not None:
                                        return True, int(timeremaining), None
                                    else:
                                        return False, None, "No timeremaining in response"
                                else:
                                    return False, None, "No data field in response"
                            # If error is True or a dict/string, it's an error
                            elif error_val is True:
                                error_msg = item.get("message", "Unknown error")
                                return False, None, f"LMS error: {error_msg}"
                            elif isinstance(error_val, dict):
                                error_msg = error_val.get("message", str(error_val))
                                return False, None, f"LMS error: {error_msg}"
                            else:
                                return False, None, f"LMS error: {error_val}"
                    return False, None, "Unexpected response format"
                except Exception as e:
                    logger.warning(
                        f"Failed to parse time_remaining response JSON: {e}, response text: {response.text[:500]}"
                    )
                    return False, None, f"Failed to parse response: {str(e)}"
            else:
                error_text = response.text[:200] if response.text else "No response body"
                logger.error(f"LMS returned status {response.status_code}: {error_text}")
                return False, None, f"HTTP {response.status_code}: {error_text}"

    except httpx.RequestError as e:
        return False, None, f"Request failed: {str(e)}"
    except Exception as e:
        return False, None, f"Unexpected error: {str(e)}"


async def refresh_session(sesskey: str, moodleSession: str) -> tuple[bool, str | None]:
    """
    Send a session refresh request to the LMS endpoint.

    Returns:
        Tuple of (success: bool, error_message: str | None)
    """
    url = f"{LMS_BASE_URL}{LMS_ENDPOINT}"
    params = {"sesskey": sesskey, "info": "refresh"}
    headers = {"Content-Type": "application/json"}
    cookies = {"MoodleSession": moodleSession}
    payload = [{"index": 0, "methodname": "core_session_touch", "args": {}}]

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.post(
                url, params=params, headers=headers, cookies=cookies, json=payload
            )

            if response.status_code == 200:
                # Check if response indicates success
                try:
                    data = response.json()
                    logger.debug(f"LMS response: {data}")
                    # Moodle AJAX responses are typically arrays with error objects
                    if isinstance(data, list) and len(data) > 0:
                        item = data[0]
                        # Check for error field - Moodle returns error: false on success, or error: {...} on failure
                        if "error" in item:
                            error_val = item["error"]
                            # If error is False or None, it's success
                            if error_val is False or error_val is None:
                                return True, None
                            # If error is True or a dict/string, it's an error
                            elif error_val is True:
                                error_msg = item.get("message", "Unknown error")
                                return False, f"LMS error: {error_msg}"
                            elif isinstance(error_val, dict):
                                error_msg = error_val.get("message", str(error_val))
                                return False, f"LMS error: {error_msg}"
                            else:
                                return False, f"LMS error: {error_val}"
                    # If no error field or empty response, assume success
                    return True, None
                except Exception as e:
                    logger.warning(
                        f"Failed to parse response JSON: {e}, response text: {response.text[:500]}"
                    )
                    # If status is 200, assume success even if JSON parsing fails
                    return True, None
            else:
                error_text = response.text[:200] if response.text else "No response body"
                logger.error(f"LMS returned status {response.status_code}: {error_text}")
                return False, f"HTTP {response.status_code}: {error_text}"

    except httpx.RequestError as e:
        return False, f"Request failed: {str(e)}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"


async def run_session_refresh_task(
    app: Application, session_refresh_id: int, user_telegram_id: int
) -> None:
    """
    Background task that runs a session refresh loop for a specific session.

    This function:
    1. Fetches the session refresh record from DB
    2. Loops while session is active
    3. Gets time remaining from Moodle AJAX
    4. Refreshes session about 30 seconds before expiration
    5. Implements retry logic on failure
    6. Stops and notifies user on final failure
    """
    logger.info(f"Starting session refresh task for session {session_refresh_id}")
    REFRESH_BEFORE_EXPIRY_S = 30  # Refresh 30 seconds before session expires

    while True:
        # Check if session is still active
        with SessionLocal() as db:
            session_refresh = db.execute(
                select(SessionRefresh).where(SessionRefresh.id == session_refresh_id)
            ).scalar_one_or_none()

            if not session_refresh or session_refresh.status != "running":
                logger.info(f"Session {session_refresh_id} is no longer active, stopping task")
                break

            sesskey = session_refresh.sesskey
            moodleSession = session_refresh.moodleSession

        # Get time remaining from Moodle
        time_remaining_success = False
        time_remaining_seconds = None
        time_remaining_error = None

        for attempt in range(MAX_RETRIES):
            time_remaining_success, time_remaining_seconds, time_remaining_error = (
                await get_session_time_remaining(sesskey, moodleSession)
            )

            if time_remaining_success and time_remaining_seconds is not None:
                logger.debug(
                    f"Session {session_refresh_id} has {time_remaining_seconds}s remaining"
                )
                break
            else:
                logger.warning(
                    f"Session {session_refresh_id} failed to get time remaining (attempt {attempt + 1}/{MAX_RETRIES}): {time_remaining_error}"
                )
                if attempt < MAX_RETRIES - 1:
                    delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                    logger.info(f"Retrying in {delay}s...")
                    await asyncio.sleep(delay)

        # If we couldn't get time remaining, stop the session
        if not time_remaining_success or time_remaining_seconds is None:
            logger.error(
                f"Session {session_refresh_id} could not get time remaining after {MAX_RETRIES} attempts, stopping"
            )

            with SessionLocal() as db:
                stopped_session = stop_session_refresh(db, session_refresh_id, status="failed")

                if stopped_session:
                    # Calculate and format duration
                    duration_seconds = stopped_session.duration_seconds or 0
                    hours, remainder = divmod(duration_seconds, 3600)
                    minutes, seconds = divmod(remainder, 60)

                    duration_str = f"{hours}h {minutes}m {seconds}s"

                    # Notify user
                    message = (
                        f"❌ *Session Refresh Stopped*\n\n"
                        f"Your LMS session refresh has stopped because we couldn't determine when the session expires.\n\n"
                        f"*Total session duration:* {duration_str}\n"
                        f"*Error:* `{time_remaining_error or 'Failed to get time remaining'}`"
                    )

                    try:
                        await app.bot.send_message(
                            chat_id=user_telegram_id, text=message, parse_mode="Markdown"
                        )
                    except Exception as e:
                        logger.error(f"Failed to notify user {user_telegram_id}: {e}")

            break

        # Calculate when to refresh (30 seconds before expiration)
        if time_remaining_seconds <= 0:
            # Session already expired, refresh immediately
            logger.warning(
                f"Session {session_refresh_id} has expired ({time_remaining_seconds}s), refreshing immediately"
            )
            sleep_time = 0
        elif time_remaining_seconds <= REFRESH_BEFORE_EXPIRY_S:
            # Session is about to expire, refresh immediately
            logger.info(
                f"Session {session_refresh_id} has {time_remaining_seconds}s remaining, refreshing immediately"
            )
            sleep_time = 0
        else:
            # Sleep until 30 seconds before expiration
            sleep_time = time_remaining_seconds - REFRESH_BEFORE_EXPIRY_S
            logger.info(
                f"Session {session_refresh_id} will refresh in {sleep_time}s (when {REFRESH_BEFORE_EXPIRY_S}s remain)"
            )

        # Sleep until it's time to refresh
        if sleep_time > 0:
            await asyncio.sleep(sleep_time)

        # Check again if session is still active before refreshing
        with SessionLocal() as db:
            session_refresh = db.execute(
                select(SessionRefresh).where(SessionRefresh.id == session_refresh_id)
            ).scalar_one_or_none()

            if not session_refresh or session_refresh.status != "running":
                logger.info(f"Session {session_refresh_id} is no longer active, stopping task")
                break

            sesskey = session_refresh.sesskey
            moodleSession = session_refresh.moodleSession

        # Attempt refresh with retry logic
        success = False
        last_error = None

        for attempt in range(MAX_RETRIES):
            success, error_msg = await refresh_session(sesskey, moodleSession)

            if success:
                logger.info(
                    f"Session {session_refresh_id} refreshed successfully (attempt {attempt + 1})"
                )
                break
            else:
                last_error = error_msg
                logger.warning(
                    f"Session {session_refresh_id} refresh failed (attempt {attempt + 1}/{MAX_RETRIES}): {error_msg}"
                )
                if attempt < MAX_RETRIES - 1:
                    delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                    logger.info(f"Retrying in {delay}s...")
                    await asyncio.sleep(delay)

        # If all retries failed, stop the session
        if not success:
            logger.error(
                f"Session {session_refresh_id} refresh failed after {MAX_RETRIES} attempts, stopping"
            )

            with SessionLocal() as db:
                stopped_session = stop_session_refresh(db, session_refresh_id, status="failed")

                if stopped_session:
                    # Calculate and format duration
                    duration_seconds = stopped_session.duration_seconds or 0
                    hours, remainder = divmod(duration_seconds, 3600)
                    minutes, seconds = divmod(remainder, 60)

                    duration_str = f"{hours}h {minutes}m {seconds}s"

                    # Notify user
                    message = (
                        f"❌ *Session Refresh Stopped*\n\n"
                        f"Your LMS session refresh has stopped after failing {MAX_RETRIES} times.\n\n"
                        f"*Total session duration:* {duration_str}\n"
                        f"*Error:* `{last_error}`"
                    )

                    try:
                        await app.bot.send_message(
                            chat_id=user_telegram_id, text=message, parse_mode="Markdown"
                        )
                    except Exception as e:
                        logger.error(f"Failed to notify user {user_telegram_id}: {e}")

            break

    logger.info(f"Session refresh task for session {session_refresh_id} completed")
