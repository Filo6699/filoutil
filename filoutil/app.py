import asyncio
import logging
import os
import signal
from datetime import datetime

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from filoutil.commands.admin import db_query, shell_command
from filoutil.commands.menu import menu_callback, menu_command
from filoutil.commands.monitor import handle_monitor_edit_input, monitor_callback, monitor_command
from filoutil.commands.notification_settings import (
    handle_blacklist_word_input,
    notification_settings_callback,
    notification_settings_command,
)
from filoutil.commands.notifications import notifications_callback, notifications_command
from filoutil.commands.reminder import (
    handle_reminder_settings_input,
    reminder_callback,
    reminder_settings_command,
)
from filoutil.commands.session_refresh import (
    refresh_session_command,
    refresh_settings_callback,
    refresh_settings_command,
)
from filoutil.commands.start import start
from filoutil.db.postgres import SessionLocal, init_db
from filoutil.db.status import get_admins, get_bot_status, update_heartbeat
from filoutil.db.users import update_user_activity
from filoutil.monitor.scheduler import monitoring_task
from filoutil.moodle_cabinet.scheduler import notifications_task
from filoutil.session_refresh.reminder import reminder_task
from filoutil.session_refresh.scheduler import session_refresh_task


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.exception("Unhandled error while processing update", exc_info=context.error)


async def heartbeat_task(interval: int = 60):
    """Periodically updates the bot's heartbeat in the database."""
    while True:
        try:
            with SessionLocal() as db:
                update_heartbeat(db, status="online")
        except Exception as e:
            logging.error(f"Error updating heartbeat: {e}")
        await asyncio.sleep(interval)


async def notify_admins_online(app: Application):
    """Notifies admins when the bot comes online, including downtime information."""
    try:
        with SessionLocal() as db:
            status_rec = get_bot_status(db)
            admins = get_admins(db)

            if not status_rec or not admins:
                # No previous status or no admins to notify
                update_heartbeat(db, status="online", exit_reason=None)
                return

            last_hb = status_rec.last_heartbeat
            now = datetime.utcnow()
            downtime = now - last_hb

            reason = status_rec.exit_reason or "Unknown (likely heartbeat timeout)"
            status_val = status_rec.status

            # If the bot downtime was less than 5 minutes and the reason is Signal 2 then don't notify the admins
            if downtime.total_seconds() < 300 and reason == "Signal 2":
                update_heartbeat(db, status="online", exit_reason=None)
                return

            # Format downtime
            hours, remainder = divmod(int(downtime.total_seconds()), 3600)
            minutes, seconds = divmod(remainder, 60)
            downtime_str = f"{hours}h {minutes}m {seconds}s"

            message = (
                "🚀 *Bot is back online!*\n\n"
                f"⏱ *Downtime:* {downtime_str}\n"
                f"📅 *Last seen:* {last_hb.strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
                f"📂 *Previous status:* {status_val}\n"
                f"📝 *Reason:* {reason}"
            )

            for admin in admins:
                try:
                    await app.bot.send_message(
                        chat_id=admin.telegram_id, text=message, parse_mode="Markdown"
                    )
                except Exception as e:
                    logging.error(f"Failed to notify admin {admin.telegram_id}: {e}")

            # Reset status for the new session
            update_heartbeat(db, status="online", exit_reason=None)

    except Exception as e:
        logging.error(f"Error in notify_admins_online: {e}")


def build_app(token: str) -> Application:
    # Increase timeouts for better resilience on flaky networks
    request = HTTPXRequest(connect_timeout=20, read_timeout=20)
    app = ApplicationBuilder().token(token).request(request).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler(["monitor", "m"], monitor_command))
    app.add_handler(CallbackQueryHandler(monitor_callback, pattern="^mon:"))
    app.add_handler(CommandHandler("refresh_session", refresh_session_command))
    app.add_handler(CommandHandler("refresh_settings", refresh_settings_command))
    app.add_handler(CommandHandler("reminder_settings", reminder_settings_command))
    app.add_handler(CommandHandler("notifications", notifications_command))
    app.add_handler(CommandHandler("notification_settings", notification_settings_command))
    app.add_handler(CallbackQueryHandler(refresh_settings_callback, pattern="^refresh_settings:"))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern="^menu:"))
    app.add_handler(CallbackQueryHandler(reminder_callback, pattern="^reminder:"))
    app.add_handler(CallbackQueryHandler(notifications_callback, pattern="^notif:"))
    app.add_handler(
        CallbackQueryHandler(notification_settings_callback, pattern="^notif_settings:")
    )
    # Admin commands
    app.add_handler(CommandHandler("shell", shell_command))
    app.add_handler(CommandHandler("db", db_query))

    # Generic message handler for text input (e.g. monitor edits, reminder settings)
    async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        # Track user activity
        if update.message and update.message.from_user:
            with SessionLocal() as db:
                update_user_activity(db, update.message.from_user.id)

        if await handle_monitor_edit_input(update, context):
            return
        if await handle_reminder_settings_input(update, context):
            return
        if await handle_blacklist_word_input(update, context):
            return
        # If not handled by anything else, we could just ignore or log
        pass

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    load_dotenv()

    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=log_level,
    )

    # Important for production: httpx logs include full URLs (Telegram bot token is in the URL).
    # Default these libraries to WARNING unless explicitly overridden.
    httpx_level = os.getenv("HTTPX_LOG_LEVEL", "WARNING").upper()
    logging.getLogger("httpx").setLevel(httpx_level)
    logging.getLogger("httpcore").setLevel(httpx_level)

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit(
            "Missing TELEGRAM_BOT_TOKEN. Set it in your environment or create a .env file."
        )

    heartbeat_interval = int(os.getenv("HEARTBEAT_INTERVAL", "60"))

    async def run_app() -> None:
        # Initialize PostgreSQL database
        init_db()

        app = build_app(token)

        stop_event = asyncio.Event()
        exit_reason: dict[str, str | None] = {"reason": None}

        def request_shutdown(sig: signal.Signals) -> None:
            logging.info("Received %s, requesting shutdown...", sig)
            exit_reason["reason"] = f"Signal {int(sig)}"
            stop_event.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda s=sig: request_shutdown(s))
            except NotImplementedError:
                # Fallback (e.g. Windows). In containers on Linux, add_signal_handler works.
                signal.signal(sig, lambda *_args, s=sig: request_shutdown(s))

        await app.initialize()
        await notify_admins_online(app)

        # Start heartbeat
        heartbeat = asyncio.create_task(heartbeat_task(heartbeat_interval), name="heartbeat_task")

        # Start monitoring service
        monitoring = asyncio.create_task(monitoring_task(app), name="monitoring_task")

        # Start session refresh scheduler
        session_refresh = asyncio.create_task(
            session_refresh_task(app), name="session_refresh_task"
        )

        # Start reminder task
        reminder = asyncio.create_task(reminder_task(app), name="reminder_task")

        # Start Moodle notifications task
        moodle_notifications = asyncio.create_task(
            notifications_task(app), name="moodle_notifications_task"
        )

        # run_polling handles network errors during polling.
        # bootstrap_retries=-1 ensures it keeps trying to start even if network is down.
        await app.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            bootstrap_retries=-1,
        )
        await app.start()

        try:
            await stop_event.wait()
        finally:
            # Mark offline before stopping, best-effort
            try:
                with SessionLocal() as db:
                    update_heartbeat(db, status="offline", exit_reason=exit_reason["reason"])
            except Exception as e:
                logging.error("Error updating status on shutdown: %s", e)

            for t in (heartbeat, monitoring, session_refresh, reminder, moodle_notifications):
                t.cancel()
            for t in (heartbeat, monitoring, session_refresh, reminder, moodle_notifications):
                try:
                    await t
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logging.error("Background task failed during shutdown: %s", e, exc_info=True)

            # Stop telegram components
            try:
                await app.updater.stop()
            except Exception:
                logging.exception("Failed to stop updater cleanly")
            await app.stop()
            await app.shutdown()

    try:
        asyncio.run(run_app())
    except Exception as e:
        logging.error(f"Bot crashed: {e}")
        try:
            with SessionLocal() as db:
                update_heartbeat(db, status="crashed", exit_reason=str(e))
        except Exception as db_e:
            logging.error(f"Error logging crash to DB: {db_e}")
        raise


if __name__ == "__main__":
    main()
