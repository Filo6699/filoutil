import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session
from telegram.ext import Application

from filoutil.db.models import SessionRefresh, User
from filoutil.db.postgres import SessionLocal
from filoutil.db.session_refresh import stop_session_refresh
from filoutil.db.status import get_admins
from filoutil.moodle_cabinet.request_logger import log_moodle_request
from filoutil.session_refresh.oidc_restore import bootstrap_moodle_session_via_oidc

logger = logging.getLogger(__name__)

LMS_BASE_URL = "https://lms.astanait.edu.kz"
LMS_ENDPOINT = "/lib/ajax/service.php"
MAX_RETRIES = 3
RETRY_DELAYS = [3, 5, 10]
MAX_SLEEP_CHUNK_SECONDS = 3600


def _looks_like_auth_failure(error: str | None) -> bool:
    if not error:
        return False
    lowered = error.lower()
    indicators = [
        "servicerequireslogin",
        "requireloginerror",
        "invalidsesskey",
        "session expired",
        "not logged in",
        "http 401",
        "http 403",
        "http 302",
        "http 303",
        "lms error: unknown error",
    ]
    return any(marker in lowered for marker in indicators)


def _to_utc_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _oidc_retry_delay_seconds(next_retry_count: int) -> int:
    """Retry cadence: 15m, 30m, 60m, 90m, +30m up to 4h, then every 24h."""
    if next_retry_count <= 1:
        return 15 * 60
    if next_retry_count == 2:
        return 30 * 60
    if next_retry_count == 3:
        return 60 * 60
    if next_retry_count == 4:
        return 90 * 60
    if next_retry_count <= 9:
        return min((next_retry_count - 1) * 30 * 60, 4 * 60 * 60)
    return 24 * 60 * 60


def _reset_oidc_retry_state(db: Session, session_refresh: SessionRefresh) -> None:
    session_refresh.oidc_retry_count = 0
    session_refresh.oidc_next_retry_at = None
    session_refresh.oidc_last_attempt_at = None
    session_refresh.oidc_last_error = None
    db.commit()


async def _schedule_oidc_retry(
    app: Application,
    session_refresh_id: int,
    user_telegram_id: int,
    failure_reason: str,
) -> bool:
    now = datetime.now(timezone.utc)
    next_retry_count = 0
    delay_seconds = 0
    next_retry_at = now
    affected_telegram_id = user_telegram_id
    affected_user_db_id: int | str = "N/A"
    username_display: str | None = None

    with SessionLocal() as db:
        session_refresh = db.execute(
            select(SessionRefresh).where(SessionRefresh.id == session_refresh_id)
        ).scalar_one_or_none()
        if (
            not session_refresh
            or session_refresh.status != "running"
            or not session_refresh.oidc_data
        ):
            return False

        next_retry_count = (session_refresh.oidc_retry_count or 0) + 1
        delay_seconds = _oidc_retry_delay_seconds(next_retry_count)
        next_retry_at = now + timedelta(seconds=delay_seconds)

        session_refresh.oidc_retry_count = next_retry_count
        session_refresh.oidc_last_attempt_at = now
        session_refresh.oidc_last_error = failure_reason[:500] if failure_reason else None
        session_refresh.oidc_next_retry_at = next_retry_at
        affected_user = db.execute(
            select(User).where(User.id == session_refresh.user_id)
        ).scalar_one_or_none()
        if affected_user:
            affected_telegram_id = affected_user.telegram_id
            affected_user_db_id = affected_user.id
            username_display = affected_user.username
        db.commit()

    logger.warning(
        "Session %s OIDC recovery failed. Scheduled retry #%s at %s (in %ss). Reason: %s",
        session_refresh_id,
        next_retry_count,
        next_retry_at.isoformat(),
        delay_seconds,
        failure_reason,
    )

    with SessionLocal() as db:
        admins = get_admins(db)

    if not admins:
        logger.warning(
            "No admins available for OIDC delayed alert (session=%s, user_telegram_id=%s)",
            session_refresh_id,
            user_telegram_id,
        )
        return True

    username_display = username_display or None
    username_line = (
        f"Affected username: @{username_display}\n"
        if username_display
        else "Affected username: N/A\n"
    )
    message = (
        "⚠️ Moodle OIDC Recovery Delayed\n\n"
        "Session recovery failed, but the session will remain active and retry automatically.\n\n"
        f"Affected user DB ID: {affected_user_db_id}\n"
        f"Affected user Telegram ID: {affected_telegram_id}\n"
        f"{username_line}"
    )
    message += (
        f"Session ID: {session_refresh_id}\n"
        f"Retry attempt: {next_retry_count}\n"
        f"Next retry (UTC): {next_retry_at.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Last error: {(failure_reason or 'Unknown error')[:250]}"
    )

    for admin in admins:
        try:
            await app.bot.send_message(chat_id=admin.telegram_id, text=message)
        except Exception as e:
            logger.error(f"Failed to notify admin {admin.telegram_id}: {e}")

    return True


async def try_recover_session_via_oidc(
    app: Application, session_refresh_id: int, user_telegram_id: int, reason: str
) -> tuple[bool, str | None]:
    with SessionLocal() as db:
        session_refresh = db.execute(
            select(SessionRefresh).where(SessionRefresh.id == session_refresh_id)
        ).scalar_one_or_none()
        if not session_refresh:
            return False, "Session record not found."
        oidc_data = session_refresh.oidc_data

    if not oidc_data:
        return False, "OIDC data not configured for this session."

    logger.info(
        "Attempting OIDC recovery for session %s (reason=%s, has_oidc_data=%s)",
        session_refresh_id,
        reason,
        bool(oidc_data),
    )

    success, sesskey, moodle_session, updated_oidc_data, error_msg = (
        await bootstrap_moodle_session_via_oidc(oidc_data)
    )
    if not success or not sesskey or not moodle_session:
        return False, error_msg or "Unknown OIDC recovery error."

    with SessionLocal() as db:
        session_refresh = db.execute(
            select(SessionRefresh).where(SessionRefresh.id == session_refresh_id)
        ).scalar_one_or_none()
        if not session_refresh:
            return False, "Session disappeared during recovery."

        session_refresh.sesskey = sesskey
        session_refresh.moodleSession = moodle_session
        session_refresh.oidc_data = updated_oidc_data or oidc_data
        _reset_oidc_retry_state(db, session_refresh)

    logger.info("Session %s recovered via OIDC", session_refresh_id)
    try:
        await app.bot.send_message(
            chat_id=user_telegram_id,
            text=(
                "🔄 *Moodle Session Recovered*\n\n"
                "Your Moodle session expired, but it was automatically restored via OIDC."
                f"\n\n*Trigger:* `{reason}`"
            ),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Failed to notify user {user_telegram_id}: {e}")

    return True, None


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


async def refresh_session(
    sesskey: str, moodleSession: str, session_refresh_id: int | None = None
) -> tuple[bool, str | None]:
    """
    Send a session refresh request to the LMS endpoint.

    Args:
        sesskey: Moodle session key
        moodleSession: Moodle session cookie value
        session_refresh_id: Optional session refresh ID for logging

    Returns:
        Tuple of (success: bool, error_message: str | None)
    """
    url = f"{LMS_BASE_URL}{LMS_ENDPOINT}"
    params = {"sesskey": sesskey, "info": "refresh"}
    headers = {"Content-Type": "application/json"}
    cookies = {"MoodleSession": moodleSession}
    payload = [{"index": 0, "methodname": "core_session_touch", "args": {}}]

    # Measure request timing
    start_time = time.perf_counter()
    request_size_bytes = None
    response_size_bytes = None
    response_status_code = None
    success = False

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            # Calculate request size (approximate)
            import json

            request_body = json.dumps(payload)
            request_size_bytes = len(request_body.encode("utf-8"))

            response = await client.post(
                url, params=params, headers=headers, cookies=cookies, json=payload
            )

            response_status_code = response.status_code
            response_size_bytes = len(response.content) if response.content else None

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
                                success = True
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
                    success = True
                    return True, None
                except Exception as e:
                    logger.warning(
                        f"Failed to parse response JSON: {e}, response text: {response.text[:500]}"
                    )
                    # If status is 200, assume success even if JSON parsing fails
                    success = True
                    return True, None
            else:
                error_text = response.text[:200] if response.text else "No response body"
                logger.error(f"LMS returned status {response.status_code}: {error_text}")
                return False, f"HTTP {response.status_code}: {error_text}"

    except httpx.RequestError as e:
        response_status_code = 0  # No response received
        return False, f"Request failed: {str(e)}"
    except Exception as e:
        response_status_code = 0  # No response received
        return False, f"Unexpected error: {str(e)}"
    finally:
        # Log the request
        end_time = time.perf_counter()
        response_time_ms = (end_time - start_time) * 1000  # Convert to milliseconds

        log_moodle_request(
            http_method="POST",
            endpoint_path=LMS_ENDPOINT,
            response_status_code=response_status_code or 0,
            response_time_ms=response_time_ms,
            success=success,
            api_method_name="core_session_touch",
            request_size_bytes=request_size_bytes,
            response_size_bytes=response_size_bytes,
            session_refresh_id=session_refresh_id,
        )


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
    6. For OIDC sessions, schedules persistent backoff retries instead of stopping
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
            oidc_next_retry_at = _to_utc_aware(session_refresh.oidc_next_retry_at)
            has_oidc_data = bool(session_refresh.oidc_data)

        if has_oidc_data and oidc_next_retry_at:
            now = datetime.now(timezone.utc)
            if oidc_next_retry_at > now:
                sleep_seconds = int((oidc_next_retry_at - now).total_seconds())
                logger.info(
                    "Session %s waiting %ss for next scheduled OIDC retry at %s",
                    session_refresh_id,
                    sleep_seconds,
                    oidc_next_retry_at.isoformat(),
                )
                await asyncio.sleep(min(sleep_seconds, MAX_SLEEP_CHUNK_SECONDS))
                continue

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
                if has_oidc_data:
                    with SessionLocal() as db:
                        current = db.execute(
                            select(SessionRefresh).where(SessionRefresh.id == session_refresh_id)
                        ).scalar_one_or_none()
                        if (
                            current
                            and current.status == "running"
                            and (
                                (current.oidc_retry_count or 0) > 0
                                or current.oidc_next_retry_at is not None
                                or current.oidc_last_error is not None
                                or current.oidc_last_attempt_at is not None
                            )
                        ):
                            _reset_oidc_retry_state(db, current)
                break
            else:
                logger.warning(
                    f"Session {session_refresh_id} failed to get time remaining (attempt {attempt + 1}/{MAX_RETRIES}): {time_remaining_error}"
                )

                if _looks_like_auth_failure(time_remaining_error):
                    recovery_ok, recovery_error = await try_recover_session_via_oidc(
                        app,
                        session_refresh_id,
                        user_telegram_id,
                        reason=time_remaining_error or "time_remaining_auth_failure",
                    )
                    if recovery_ok:
                        logger.info(
                            "Session %s recovered during time-remaining retry loop",
                            session_refresh_id,
                        )
                        time_remaining_success = True
                        time_remaining_seconds = 0
                        break
                    logger.warning(
                        "Session %s OIDC recovery attempt failed during time-remaining retry: %s",
                        session_refresh_id,
                        recovery_error,
                    )

                if attempt < MAX_RETRIES - 1:
                    delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                    logger.info(f"Retrying in {delay}s...")
                    await asyncio.sleep(delay)

        # If we couldn't get time remaining, schedule OIDC backoff retry for OIDC sessions
        if not time_remaining_success or time_remaining_seconds is None:
            recovery_ok, recovery_error = await try_recover_session_via_oidc(
                app,
                session_refresh_id,
                user_telegram_id,
                reason=time_remaining_error or "time_remaining_failed",
            )
            if recovery_ok:
                logger.info(
                    "Session %s recovered after time-remaining failure; continuing refresh loop",
                    session_refresh_id,
                )
                await asyncio.sleep(1)
                continue

            scheduled = await _schedule_oidc_retry(
                app,
                session_refresh_id,
                user_telegram_id,
                failure_reason=recovery_error
                or time_remaining_error
                or "Failed to get session time remaining",
            )
            if scheduled:
                continue

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
                        f"*Error:* `{time_remaining_error or 'Failed to get time remaining'}`\n"
                        f"*OIDC recovery:* `{recovery_error or 'not available'}`"
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
            success, error_msg = await refresh_session(
                sesskey, moodleSession, session_refresh_id=session_refresh_id
            )

            if success:
                logger.info(
                    f"Session {session_refresh_id} refreshed successfully (attempt {attempt + 1})"
                )
                if has_oidc_data:
                    with SessionLocal() as db:
                        current = db.execute(
                            select(SessionRefresh).where(SessionRefresh.id == session_refresh_id)
                        ).scalar_one_or_none()
                        if (
                            current
                            and current.status == "running"
                            and (
                                (current.oidc_retry_count or 0) > 0
                                or current.oidc_next_retry_at is not None
                                or current.oidc_last_error is not None
                                or current.oidc_last_attempt_at is not None
                            )
                        ):
                            _reset_oidc_retry_state(db, current)
                break
            else:
                last_error = error_msg
                logger.warning(
                    f"Session {session_refresh_id} refresh failed (attempt {attempt + 1}/{MAX_RETRIES}): {error_msg}"
                )

                if _looks_like_auth_failure(error_msg):
                    recovery_ok, recovery_error = await try_recover_session_via_oidc(
                        app,
                        session_refresh_id,
                        user_telegram_id,
                        reason=error_msg or "refresh_auth_failure",
                    )
                    if recovery_ok:
                        logger.info(
                            "Session %s recovered during refresh retry loop", session_refresh_id
                        )
                        success = True
                        break
                    logger.warning(
                        "Session %s OIDC recovery attempt failed during refresh retry: %s",
                        session_refresh_id,
                        recovery_error,
                    )

                if attempt < MAX_RETRIES - 1:
                    delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                    logger.info(f"Retrying in {delay}s...")
                    await asyncio.sleep(delay)

        # If all retries failed, schedule OIDC backoff retry for OIDC sessions
        if not success:
            recovery_ok, recovery_error = await try_recover_session_via_oidc(
                app,
                session_refresh_id,
                user_telegram_id,
                reason=last_error or "refresh_failed",
            )
            if recovery_ok:
                logger.info(
                    "Session %s recovered after refresh failure; continuing refresh loop",
                    session_refresh_id,
                )
                await asyncio.sleep(1)
                continue

            scheduled = await _schedule_oidc_retry(
                app,
                session_refresh_id,
                user_telegram_id,
                failure_reason=recovery_error or last_error or "Session refresh failed",
            )
            if scheduled:
                continue

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
                        f"*Error:* `{last_error}`\n"
                        f"*OIDC recovery:* `{recovery_error or 'not available'}`"
                    )

                    try:
                        await app.bot.send_message(
                            chat_id=user_telegram_id, text=message, parse_mode="Markdown"
                        )
                    except Exception as e:
                        logger.error(f"Failed to notify user {user_telegram_id}: {e}")

            break

    logger.info(f"Session refresh task for session {session_refresh_id} completed")
