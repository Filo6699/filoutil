import asyncio
import logging
import socket
import ssl
import time
from datetime import datetime, timezone
from typing import Any

import httpx

try:
    import jsonschema
except ImportError:
    jsonschema = None

from sqlalchemy.orm import Session

from filoutil.db.models import Monitor, User
from filoutil.db.monitors import (
    add_check_run,
    get_open_incident,
    get_recent_check_runs,
    resolve_incident,
    start_incident,
)
from filoutil.db.status import get_admins

logger = logging.getLogger(__name__)


async def get_ssl_expiry(url: str, timeout: int = 5) -> datetime | None:
    """Helper to get SSL certificate expiry date."""
    if not url.startswith("https://"):
        return None

    hostname = url.replace("https://", "").split("/")[0]
    try:
        context = ssl.create_default_context()
        # Use an async-friendly way or wrap in thread? For simplicity, we'll use a short timeout.
        # Ideally we'd use asyncio.to_thread for blocking socket ops.
        return await asyncio.to_thread(_get_ssl_expiry_sync, hostname, context, timeout)
    except Exception as e:
        logger.debug(f"Failed to get SSL expiry for {hostname}: {e}")
        return None


def _get_ssl_expiry_sync(hostname: str, context: ssl.SSLContext, timeout: int) -> datetime | None:
    with socket.create_connection((hostname, 443), timeout=timeout) as sock:
        with context.wrap_socket(sock, server_hostname=hostname) as ssock:
            cert = ssock.getpeercert()
            if not cert:
                return None
            expiry_str = cert.get("notAfter")
            if not expiry_str:
                return None
            # e.g., 'Jan  4 12:00:00 2026 GMT'
            return datetime.strptime(expiry_str, "%b %d %H:%M:%S %Y %Z").replace(
                tzinfo=timezone.utc
            )


async def check_monitor(db: Session, monitor: Monitor) -> None:
    """Perform the health check for a single monitor and update state."""
    start_time = time.perf_counter()
    is_up = False
    status_code = None
    error_msg = None
    latency_ms = 0.0
    ssl_expiry = None

    try:
        async with httpx.AsyncClient(timeout=monitor.timeout_s, follow_redirects=True) as client:
            headers = monitor.headers or {}
            response = await client.request(monitor.method, monitor.url, headers=headers)

            status_code = response.status_code
            latency_ms = (time.perf_counter() - start_time) * 1000

            # 1. Check status code
            if status_code == monitor.expected_status:
                is_up = True
            else:
                error_msg = f"Expected status {monitor.expected_status}, got {status_code}"
                is_up = False

            # 2. Check keyword
            if is_up and monitor.keyword:
                if monitor.keyword not in response.text:
                    is_up = False
                    error_msg = f"Keyword '{monitor.keyword}' not found in response"

            # 3. Check JSON schema
            if is_up and monitor.json_schema and jsonschema:
                try:
                    data = response.json()
                    jsonschema.validate(instance=data, schema=monitor.json_schema)
                except Exception as e:
                    is_up = False
                    error_msg = f"JSON schema validation failed: {str(e)}"

        # 4. Check SSL Expiry if HTTPS
        ssl_expiry = await get_ssl_expiry(monitor.url, timeout=monitor.timeout_s)

    except httpx.RequestError as e:
        latency_ms = (time.perf_counter() - start_time) * 1000
        is_up = False
        error_msg = f"Request failed: {str(e)}"
    except Exception as e:
        latency_ms = (time.perf_counter() - start_time) * 1000
        is_up = False
        error_msg = f"Unexpected error: {str(e)}"

    # Add check run to DB
    add_check_run(
        db,
        monitor.id,
        is_up,
        latency_ms,
        status_code=status_code,
        error=error_msg,
        ssl_expiry=ssl_expiry.replace(tzinfo=None) if ssl_expiry else None,
    )

    # Handle Incidents and Flap Detection
    await process_status_change(db, monitor, is_up, error_msg, ssl_expiry)


async def process_status_change(
    db: Session, monitor: Monitor, is_up: bool, error_msg: str | None, ssl_expiry: datetime | None
) -> None:
    """Decide if we need to alert or manage incidents."""

    # Simple flap detection: if status changed in last 3 checks
    recent = get_recent_check_runs(db, monitor.id, limit=5)
    changes = 0
    if len(recent) > 1:
        for i in range(len(recent) - 1):
            if recent[i].is_up != recent[i + 1].is_up:
                changes += 1

    is_flapping = changes >= 3
    new_status = "flapping" if is_flapping else ("up" if is_up else "down")
    old_status = monitor.status
    monitor.status = new_status
    db.commit()

    # Incident management
    incident = get_open_incident(db, monitor.id)

    alert_message = None

    if not is_up:
        if not incident:
            # Start new incident
            start_incident(db, monitor.id, error=error_msg)
            if monitor.alert_on_down:
                alert_message = (
                    f"🔴 *Monitor DOWN:* {monitor.name}\nURL: {monitor.url}\nError: {error_msg}"
                )
        else:
            # Update incident with latest error
            incident.last_error = error_msg
            db.commit()
    else:
        if incident:
            # Resolve incident
            resolve_incident(db, monitor.id)
            if monitor.alert_on_up:
                duration = datetime.utcnow() - incident.started_at
                alert_message = f"🟢 *Monitor RECOVERED:* {monitor.name}\nURL: {monitor.url}\nDowntime duration: {duration}"

    # SSL Expiry Alert
    if ssl_expiry:
        days_left = (ssl_expiry - datetime.now(timezone.utc)).days
        if days_left in [30, 14, 7, 3, 1, 0]:
            ssl_alert = f"⚠️ *SSL Expiry Warning:* {monitor.name}\nURL: {monitor.url}\nExpires in: {days_left} days ({ssl_expiry.strftime('%Y-%m-%d')})"
            await queue_notification(db, ssl_alert)

    if alert_message:
        await queue_notification(db, alert_message)


async def queue_notification(db: Session, message: str) -> None:
    """Helper to send notifications to admins.
    Note: We need a way to access the bot. For now we log,
    and the scheduler will handle the actual sending if it has the bot instance.
    """
    logger.info(f"Notification: {message}")
    # This will be picked up by the scheduler which has access to the telegram app
    if not hasattr(asyncio.get_running_loop(), "_notifications"):
        setattr(asyncio.get_running_loop(), "_notifications", [])
    asyncio.get_running_loop()._notifications.append(message)
