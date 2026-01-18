"""Database functions for managing Moodle quiet hours."""

from datetime import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from filoutil.db.models import MoodleQuietHours


def get_all_quiet_hours(db: Session) -> list[MoodleQuietHours]:
    """Get all quiet hours intervals."""
    return (
        db.execute(select(MoodleQuietHours).order_by(MoodleQuietHours.start_time)).scalars().all()
    )


def get_enabled_quiet_hours(db: Session) -> list[MoodleQuietHours]:
    """Get all enabled quiet hours intervals."""
    return (
        db.execute(
            select(MoodleQuietHours)
            .where(MoodleQuietHours.enabled == True)
            .order_by(MoodleQuietHours.start_time)
        )
        .scalars()
        .all()
    )


def get_quiet_hours_by_id(db: Session, quiet_hours_id: int) -> MoodleQuietHours | None:
    """Get a quiet hours interval by ID."""
    return db.execute(
        select(MoodleQuietHours).where(MoodleQuietHours.id == quiet_hours_id)
    ).scalar_one_or_none()


def create_quiet_hours(
    db: Session, start_time: time, end_time: time, enabled: bool = True
) -> MoodleQuietHours:
    """Create a new quiet hours interval."""
    quiet_hours = MoodleQuietHours(start_time=start_time, end_time=end_time, enabled=enabled)
    db.add(quiet_hours)
    db.commit()
    db.refresh(quiet_hours)
    return quiet_hours


def update_quiet_hours(
    db: Session,
    quiet_hours_id: int,
    start_time: time | None = None,
    end_time: time | None = None,
    enabled: bool | None = None,
) -> MoodleQuietHours | None:
    """Update a quiet hours interval."""
    quiet_hours = get_quiet_hours_by_id(db, quiet_hours_id)
    if not quiet_hours:
        return None

    if start_time is not None:
        quiet_hours.start_time = start_time
    if end_time is not None:
        quiet_hours.end_time = end_time
    if enabled is not None:
        quiet_hours.enabled = enabled

    db.commit()
    db.refresh(quiet_hours)
    return quiet_hours


def delete_quiet_hours(db: Session, quiet_hours_id: int) -> bool:
    """Delete a quiet hours interval."""
    quiet_hours = get_quiet_hours_by_id(db, quiet_hours_id)
    if not quiet_hours:
        return False

    db.delete(quiet_hours)
    db.commit()
    return True


def is_in_quiet_hours(db: Session, current_time: time | None = None) -> bool:
    """
    Check if the current time (or provided time) is within any enabled quiet hours interval.

    Note: Quiet hours are checked against the configured application timezone (TIMEZONE env var).
    All time comparisons use the application timezone, not UTC or server local time.

    Args:
        db: Database session
        current_time: Optional time to check. If None, uses current time in app timezone.

    Returns:
        True if current time is within quiet hours, False otherwise.
    """
    from filoutil.config import get_current_time

    if current_time is None:
        current_time = get_current_time().time()

    enabled_intervals = get_enabled_quiet_hours(db)

    for interval in enabled_intervals:
        start = interval.start_time
        end = interval.end_time

        # Handle case where end time is before start time (spans midnight)
        if start <= end:
            # Normal case: start <= current <= end
            if start <= current_time <= end:
                return True
        else:
            # Spans midnight: current >= start OR current <= end
            if current_time >= start or current_time <= end:
                return True

    return False
