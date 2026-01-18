from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from filoutil.db.models import User


def get_user_by_telegram_id(db: Session, telegram_id: int):
    """Fetch a user from PostgreSQL by Telegram ID."""
    return db.execute(select(User).where(User.telegram_id == telegram_id)).scalar_one_or_none()


def ensure_user_by_telegram_id(db: Session, telegram_id: int, username: str | None):
    """Create the user if missing (default whitelisted=false) and refresh username/updated_at.

    Returns:
        tuple: (user, is_new) where is_new is True if the user was just created
    """
    user = get_user_by_telegram_id(db, telegram_id)
    is_new = False
    if not user:
        user = User(telegram_id=telegram_id, username=username, whitelisted=False, role="user")
        db.add(user)
        is_new = True
    else:
        user.username = username
        user.updated_at = datetime.utcnow()
        user.last_activity_at = datetime.utcnow()

    db.commit()
    db.refresh(user)
    return user, is_new


def update_user_activity(db: Session, telegram_id: int) -> None:
    """Update the last activity timestamp for a user."""
    user = get_user_by_telegram_id(db, telegram_id)
    if user:
        user.last_activity_at = datetime.utcnow()
        db.commit()
