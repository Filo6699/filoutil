"""Database functions for Moodle notifications."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from filoutil.db.models import MoodleNotification


def get_notification_by_moodle_id(
    db: Session, moodle_notification_id: int
) -> MoodleNotification | None:
    """Get a notification by Moodle's notification ID."""
    return db.execute(
        select(MoodleNotification).where(
            MoodleNotification.notification_id == moodle_notification_id
        )
    ).scalar_one_or_none()


def get_user_notifications(
    db: Session, user_id: int, limit: int = 100, offset: int = 0
) -> list[MoodleNotification]:
    """Get notifications for a user."""
    return (
        db.execute(
            select(MoodleNotification)
            .where(MoodleNotification.user_id == user_id)
            .order_by(MoodleNotification.timecreated.desc())
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )


def get_unread_notifications_count(db: Session, user_id: int) -> int:
    """Get count of unread notifications for a user."""
    from sqlalchemy import func

    return (
        db.execute(
            select(func.count(MoodleNotification.id)).where(
                MoodleNotification.user_id == user_id,
                MoodleNotification.read == False,
                MoodleNotification.deleted == False,
            )
        ).scalar()
        or 0
    )
