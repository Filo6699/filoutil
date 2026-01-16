from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from filoutil.db.models import SessionRefresh, User


def create_session_refresh(
    db: Session,
    user_id: int,
    sesskey: str,
    moodleSession: str,
    refresh_interval_s: int,
    name: str | None = None,
) -> SessionRefresh:
    """Create a new session refresh job."""
    session_refresh = SessionRefresh(
        user_id=user_id,
        sesskey=sesskey,
        moodleSession=moodleSession,
        refresh_interval_s=refresh_interval_s,
        status="running",
        name=name,
    )
    db.add(session_refresh)
    db.commit()
    db.refresh(session_refresh)
    return session_refresh


def get_active_session_refresh(db: Session, user_id: int) -> SessionRefresh | None:
    """Get the active (running) session refresh for a user.

    DEPRECATED: Use get_active_sessions_for_user() to support multiple sessions.
    This function returns only the first active session for backwards compatibility.
    """
    return db.execute(
        select(SessionRefresh)
        .where(SessionRefresh.user_id == user_id, SessionRefresh.status == "running")
        .limit(1)
    ).scalar_one_or_none()


def get_active_sessions_for_user(db: Session, user_id: int) -> list[SessionRefresh]:
    """Get all active (running) session refreshes for a user."""
    return (
        db.execute(
            select(SessionRefresh)
            .where(SessionRefresh.user_id == user_id, SessionRefresh.status == "running")
            .order_by(SessionRefresh.started_at.desc())
        )
        .scalars()
        .all()
    )


def get_session_refresh_by_id(db: Session, session_id: int) -> SessionRefresh | None:
    """Get a session refresh by its ID."""
    return db.execute(
        select(SessionRefresh).where(SessionRefresh.id == session_id)
    ).scalar_one_or_none()


def stop_session_refresh(
    db: Session, session_refresh_id: int, status: str = "stopped"
) -> SessionRefresh | None:
    """Stop a session refresh job and calculate duration."""
    session_refresh = db.execute(
        select(SessionRefresh).where(SessionRefresh.id == session_refresh_id)
    ).scalar_one_or_none()

    if session_refresh:
        session_refresh.ended_at = datetime.utcnow()
        session_refresh.status = status

        # Calculate duration in seconds
        if session_refresh.started_at and session_refresh.ended_at:
            duration = session_refresh.ended_at - session_refresh.started_at
            session_refresh.duration_seconds = int(duration.total_seconds())

        db.commit()
        db.refresh(session_refresh)

    return session_refresh


def get_user_refresh_interval(db: Session, user_id: int) -> int:
    """Get the refresh interval for a user from settings, defaulting to 300 seconds."""
    user = db.execute(select(User).where(User.id == user_id)).scalar_one_or_none()
    if user and user.settings:
        return user.settings.get("refresh_interval_s", 300)
    return 300


def get_all_active_sessions(db: Session) -> list[SessionRefresh]:
    """Get all active session refresh jobs."""
    return (
        db.execute(select(SessionRefresh).where(SessionRefresh.status == "running")).scalars().all()
    )


def update_session_name(
    db: Session, session_refresh_id: int, name: str | None
) -> SessionRefresh | None:
    """Update the name for a session refresh."""
    session_refresh = db.execute(
        select(SessionRefresh).where(SessionRefresh.id == session_refresh_id)
    ).scalar_one_or_none()

    if session_refresh:
        session_refresh.name = name
        db.commit()
        db.refresh(session_refresh)

    return session_refresh
