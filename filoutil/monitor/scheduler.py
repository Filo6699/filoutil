import asyncio
import logging
from datetime import datetime, timedelta, timezone

from telegram.ext import Application

from filoutil.db.monitors import get_enabled_monitors
from filoutil.db.postgres import SessionLocal
from filoutil.db.status import get_admins
from filoutil.monitor.engine import check_monitor

logger = logging.getLogger(__name__)


async def monitoring_task(app: Application):
    """Background task to run monitor checks."""
    logger.info("Starting monitoring task...")

    # Initialize notification queue on the loop
    if not hasattr(asyncio.get_running_loop(), "_notifications"):
        setattr(asyncio.get_running_loop(), "_notifications", [])

    while True:
        try:
            with SessionLocal() as db:
                monitors = get_enabled_monitors(db)
                now = datetime.now(timezone.utc)

                tasks = []
                for monitor in monitors:
                    # Check if it's time to run this monitor
                    should_run = False
                    if not monitor.last_check_at:
                        should_run = True
                    else:
                        # Ensure last_check_at is timezone-aware for comparison
                        last_check = monitor.last_check_at
                        if last_check.tzinfo is None:
                            last_check = last_check.replace(tzinfo=timezone.utc)

                        elapsed = (now - last_check).total_seconds()
                        if elapsed >= monitor.interval_s:
                            should_run = True

                    if should_run:
                        # We use a wrapper to ensure we use a fresh DB session for each check if needed,
                        # but for simplicity we'll pass the current session or create one inside.
                        tasks.append(run_single_check(monitor.id))

                if tasks:
                    logger.debug(f"Running {len(tasks)} monitor checks...")
                    await asyncio.gather(*tasks)

            # Process any queued notifications
            await process_notifications(app)

        except Exception as e:
            logger.error(f"Error in monitoring_task: {e}", exc_info=True)

        # Sleep for a short interval before checking again
        await asyncio.sleep(10)


async def run_single_check(monitor_id: int):
    """Runs a single monitor check in its own session."""
    try:
        with SessionLocal() as db:
            from filoutil.db.monitors import get_monitor

            monitor = get_monitor(db, monitor_id)
            if monitor:
                await check_monitor(db, monitor)
    except Exception as e:
        logger.error(f"Failed to run check for monitor {monitor_id}: {e}")


async def process_notifications(app: Application):
    """Sends queued notifications to all admins."""
    loop = asyncio.get_running_loop()
    notifications = getattr(loop, "_notifications", [])
    if not notifications:
        return

    # Clear notifications first to avoid double sending
    to_send = notifications[:]
    notifications.clear()

    with SessionLocal() as db:
        admins = get_admins(db)
        if not admins:
            return

        for msg in to_send:
            for admin in admins:
                try:
                    await app.bot.send_message(
                        chat_id=admin.telegram_id, text=msg, parse_mode="Markdown"
                    )
                except Exception as e:
                    logger.error(f"Failed to send monitor alert to {admin.telegram_id}: {e}")
