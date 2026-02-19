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
        if "moodle_session_agreement" not in columns:
            logger.info("Adding missing column 'moodle_session_agreement' to users table...")
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE users ADD COLUMN moodle_session_agreement BOOLEAN DEFAULT FALSE"
                    )
                )
            logger.info("Migration completed: added 'moodle_session_agreement' column.")
        if "moodle_student_confirmation" not in columns:
            logger.info("Adding missing column 'moodle_student_confirmation' to users table...")
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE users ADD COLUMN moodle_student_confirmation BOOLEAN DEFAULT FALSE"
                    )
                )
            logger.info("Migration completed: added 'moodle_student_confirmation' column.")

    # Check if session_refresh table exists and add columns
    if "session_refresh" in inspector.get_table_names():
        columns = [col["name"] for col in inspector.get_columns("session_refresh")]
        if "moodle_user_id" not in columns:
            logger.info("Adding missing column 'moodle_user_id' to session_refresh table...")
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE session_refresh ADD COLUMN moodle_user_id INTEGER"))
            logger.info("Migration completed: added 'moodle_user_id' column.")
        if "name" not in columns:
            logger.info("Adding missing column 'name' to session_refresh table...")
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE session_refresh ADD COLUMN name VARCHAR"))
            logger.info("Migration completed: added 'name' column.")
        if "oidc_data" not in columns:
            logger.info("Adding missing column 'oidc_data' to session_refresh table...")
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE session_refresh ADD COLUMN oidc_data JSON"))
            logger.info("Migration completed: added 'oidc_data' column.")

    # Create user_permissions table if it doesn't exist (backwards compatible migration)
    # Note: Base.metadata.create_all() is called before this, so if SQLAlchemy created it,
    # this migration will skip. If it doesn't exist, we create it manually.
    table_names = inspector.get_table_names()
    if "user_permissions" not in table_names:
        logger.info("Creating 'user_permissions' table...")
        with engine.begin() as conn:
            # Create the table with all columns
            conn.execute(
                text(
                    """
                    CREATE TABLE user_permissions (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        module VARCHAR NOT NULL,
                        granted_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        granted_by BIGINT,
                        CONSTRAINT fk_user_permissions_user_id
                            FOREIGN KEY (user_id)
                            REFERENCES users(id)
                            ON DELETE CASCADE,
                        CONSTRAINT uq_user_module
                            UNIQUE (user_id, module)
                    )
                    """
                )
            )
            # Create index on user_id for faster lookups (if not already created by SQLAlchemy)
            try:
                conn.execute(
                    text("CREATE INDEX ix_user_permissions_user_id ON user_permissions(user_id)")
                )
            except Exception as e:
                # Index might already exist if created by SQLAlchemy, ignore
                logger.debug(f"Index creation skipped (may already exist): {e}")
        logger.info("Migration completed: created 'user_permissions' table.")
    else:
        # Table exists, check if all columns are present (for future migrations)
        columns = [col["name"] for col in inspector.get_columns("user_permissions")]
        required_columns = ["id", "user_id", "module", "granted_at", "granted_by"]
        missing_columns = [col for col in required_columns if col not in columns]
        if missing_columns:
            logger.warning(
                f"user_permissions table exists but missing columns: {missing_columns}. "
                "Manual migration may be required."
            )
        # Ensure index exists (backwards compatible - won't fail if already exists)
        try:
            with engine.begin() as conn:
                # Check if index exists by querying pg_indexes
                result = conn.execute(
                    text(
                        """
                        SELECT COUNT(*) FROM pg_indexes
                        WHERE tablename = 'user_permissions'
                        AND indexname = 'ix_user_permissions_user_id'
                        """
                    )
                ).scalar()
                if result == 0:
                    conn.execute(
                        text(
                            "CREATE INDEX ix_user_permissions_user_id ON user_permissions(user_id)"
                        )
                    )
                    logger.info("Created missing index on user_permissions.user_id")
        except Exception as e:
            # Ignore errors - index might already exist or table structure might differ
            logger.debug(f"Index check/creation skipped: {e}")

    # Create moodle_request_logs table if it doesn't exist
    table_names = inspector.get_table_names()
    if "moodle_request_logs" not in table_names:
        logger.info("Creating 'moodle_request_logs' table...")
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE moodle_request_logs (
                        id SERIAL PRIMARY KEY,
                        timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        http_method VARCHAR NOT NULL,
                        endpoint_path VARCHAR NOT NULL,
                        api_method_name VARCHAR,
                        response_status_code INTEGER NOT NULL,
                        response_time_ms DOUBLE PRECISION NOT NULL,
                        request_size_bytes INTEGER,
                        response_size_bytes INTEGER,
                        success BOOLEAN NOT NULL,
                        session_refresh_id INTEGER,
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        CONSTRAINT fk_moodle_request_logs_session_refresh_id
                            FOREIGN KEY (session_refresh_id)
                            REFERENCES session_refresh(id)
                            ON DELETE SET NULL
                    )
                    """
                )
            )
            # Create indexes
            conn.execute(
                text(
                    "CREATE INDEX ix_moodle_request_logs_timestamp ON moodle_request_logs(timestamp)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX ix_moodle_request_logs_session_refresh_id ON moodle_request_logs(session_refresh_id)"
                )
            )
        logger.info("Migration completed: created 'moodle_request_logs' table.")
    else:
        # Table exists, check if it has the correct schema
        columns = [col["name"] for col in inspector.get_columns("moodle_request_logs")]
        required_columns = [
            "id",
            "timestamp",
            "http_method",
            "endpoint_path",
            "api_method_name",
            "response_status_code",
            "response_time_ms",
            "request_size_bytes",
            "response_size_bytes",
            "success",
            "session_refresh_id",
            "created_at",
        ]
        missing_columns = [col for col in required_columns if col not in columns]
        if missing_columns:
            logger.warning(
                f"moodle_request_logs table exists but missing columns: {missing_columns}. "
                "Dropping and recreating table (logging data will be lost)."
            )
            # Drop and recreate the table since it's a logging table and can be safely recreated
            with engine.begin() as conn:
                conn.execute(text("DROP TABLE IF EXISTS moodle_request_logs CASCADE"))
                conn.execute(
                    text(
                        """
                        CREATE TABLE moodle_request_logs (
                            id SERIAL PRIMARY KEY,
                            timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            http_method VARCHAR NOT NULL,
                            endpoint_path VARCHAR NOT NULL,
                            api_method_name VARCHAR,
                            response_status_code INTEGER NOT NULL,
                            response_time_ms DOUBLE PRECISION NOT NULL,
                            request_size_bytes INTEGER,
                            response_size_bytes INTEGER,
                            success BOOLEAN NOT NULL,
                            session_refresh_id INTEGER,
                            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            CONSTRAINT fk_moodle_request_logs_session_refresh_id
                                FOREIGN KEY (session_refresh_id)
                                REFERENCES session_refresh(id)
                                ON DELETE SET NULL
                        )
                        """
                    )
                )
                # Create indexes
                conn.execute(
                    text(
                        "CREATE INDEX ix_moodle_request_logs_timestamp ON moodle_request_logs(timestamp)"
                    )
                )
                conn.execute(
                    text(
                        "CREATE INDEX ix_moodle_request_logs_session_refresh_id ON moodle_request_logs(session_refresh_id)"
                    )
                )
            logger.info("Migration completed: recreated 'moodle_request_logs' table.")


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
