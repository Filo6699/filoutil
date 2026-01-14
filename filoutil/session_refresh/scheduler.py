import asyncio
import logging

from sqlalchemy import select
from telegram.ext import Application

from filoutil.db.models import User
from filoutil.db.postgres import SessionLocal
from filoutil.db.session_refresh import get_all_active_sessions
from filoutil.session_refresh.engine import run_session_refresh_task

logger = logging.getLogger(__name__)

# Track running tasks to avoid duplicates
_running_tasks: dict[int, asyncio.Task] = {}


async def session_refresh_task(app: Application):
    """Background task that manages active session refresh jobs.

    On startup, this task will recover any active sessions from the database
    and restart their refresh loops. It also periodically checks for new sessions.
    """
    logger.info("Starting session refresh scheduler task...")

    # Recover any active sessions from database (e.g., after bot restart)
    with SessionLocal() as db:
        active_sessions = get_all_active_sessions(db)
        for session in active_sessions:
            user = db.execute(select(User).where(User.id == session.user_id)).scalar_one_or_none()
            if user:
                logger.info(f"Recovering session refresh {session.id} for user {user.telegram_id}")
                task = asyncio.create_task(
                    run_session_refresh_task(app, session.id, user.telegram_id)
                )
                _running_tasks[session.id] = task

    # Main loop: periodically check for new active sessions that don't have running tasks
    while True:
        try:
            with SessionLocal() as db:
                active_sessions = get_all_active_sessions(db)

                for session in active_sessions:
                    # Check if task is already running for this session
                    if session.id not in _running_tasks:
                        user = db.execute(
                            select(User).where(User.id == session.user_id)
                        ).scalar_one_or_none()
                        if user:
                            logger.info(
                                f"Starting session refresh task for session {session.id} (user {user.telegram_id})"
                            )
                            task = asyncio.create_task(
                                run_session_refresh_task(app, session.id, user.telegram_id)
                            )
                            _running_tasks[session.id] = task
                    else:
                        # Check if task is still running
                        task = _running_tasks[session.id]
                        if task.done():
                            logger.info(f"Session refresh task {session.id} completed")
                            try:
                                # Check if there was an exception
                                task.result()
                            except Exception as e:
                                logger.error(
                                    f"Session refresh task {session.id} failed: {e}", exc_info=True
                                )
                            del _running_tasks[session.id]

                # Clean up tasks for sessions that are no longer active
                active_session_ids = {s.id for s in active_sessions}
                for session_id in list(_running_tasks.keys()):
                    if session_id not in active_session_ids:
                        logger.info(f"Cleaning up task for inactive session {session_id}")
                        task = _running_tasks.pop(session_id)
                        if not task.done():
                            task.cancel()

        except Exception as e:
            logger.error(f"Error in session_refresh_task: {e}", exc_info=True)

        # Check every 5 seconds for faster pickup of new sessions
        await asyncio.sleep(5)
