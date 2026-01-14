import logging
import os
import time

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from filoutil.db.models import Base

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    env = os.getenv("ENV", "").lower()
    if env in {"prod", "production"}:
        raise RuntimeError(
            "Missing DATABASE_URL in production. Set DATABASE_URL (e.g. "
            "'postgresql://postgres:postgres@db:5432/filoutil')."
        )
    # Development default: local compose port-forward
    DATABASE_URL = "postgresql://postgres:postgres@localhost:5001/filoutil"

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def run_migrations():
    """Run database migrations to add missing columns."""
    inspector = inspect(engine)

    # Check if users table exists and if last_activity_at column is missing
    if "users" in inspector.get_table_names():
        columns = [col["name"] for col in inspector.get_columns("users")]
        if "last_activity_at" not in columns:
            logger.info("Adding missing column 'last_activity_at' to users table...")
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE users ADD COLUMN last_activity_at TIMESTAMP"))
            logger.info("Migration completed: added 'last_activity_at' column.")

    # Check if session_refresh table exists and add reminder columns
    if "session_refresh" in inspector.get_table_names():
        columns = [col["name"] for col in inspector.get_columns("session_refresh")]
        if "last_reminder_sent_at" not in columns:
            logger.info("Adding missing column 'last_reminder_sent_at' to session_refresh table...")
            with engine.begin() as conn:
                conn.execute(
                    text("ALTER TABLE session_refresh ADD COLUMN last_reminder_sent_at TIMESTAMP")
                )
            logger.info("Migration completed: added 'last_reminder_sent_at' column.")
        if "reminder_due_at" not in columns:
            logger.info("Adding missing column 'reminder_due_at' to session_refresh table...")
            with engine.begin() as conn:
                conn.execute(
                    text("ALTER TABLE session_refresh ADD COLUMN reminder_due_at TIMESTAMP")
                )
            logger.info("Migration completed: added 'reminder_due_at' column.")


def init_db():
    retries = 5
    while retries > 0:
        try:
            Base.metadata.create_all(bind=engine)
            run_migrations()
            logger.info("Database initialized successfully.")
            return
        except OperationalError as e:
            retries -= 1
            logger.warning(f"Database not ready, retrying in 5 seconds... ({retries} retries left)")
            if retries == 0:
                logger.error("Could not connect to the database after multiple retries.")
                raise e
            time.sleep(5)
