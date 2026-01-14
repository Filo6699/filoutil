from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from filoutil.db.models import CheckRun, Incident, Monitor


def get_monitor(db: Session, monitor_id: int) -> Monitor | None:
    """Fetch a single monitor by ID."""
    return db.execute(select(Monitor).where(Monitor.id == monitor_id)).scalar_one_or_none()


def get_all_monitors(db: Session) -> list[Monitor]:
    """Fetch all monitors."""
    return db.execute(select(Monitor).order_by(Monitor.id)).scalars().all()


def get_enabled_monitors(db: Session) -> list[Monitor]:
    """Fetch all enabled monitors."""
    return db.execute(select(Monitor).where(Monitor.enabled == True)).scalars().all()


def create_monitor(db: Session, **kwargs) -> Monitor:
    """Create a new monitor."""
    monitor = Monitor(**kwargs)
    db.add(monitor)
    db.commit()
    db.refresh(monitor)
    return monitor


def update_monitor(db: Session, monitor_id: int, **kwargs) -> Monitor | None:
    """Update an existing monitor."""
    monitor = get_monitor(db, monitor_id)
    if monitor:
        for key, value in kwargs.items():
            if hasattr(monitor, key):
                setattr(monitor, key, value)
        db.commit()
        db.refresh(monitor)
    return monitor


def delete_monitor(db: Session, monitor_id: int) -> bool:
    """Delete a monitor."""
    monitor = get_monitor(db, monitor_id)
    if monitor:
        db.delete(monitor)
        db.commit()
        return True
    return False


def add_check_run(
    db: Session,
    monitor_id: int,
    is_up: bool,
    latency_ms: float,
    status_code: int | None = None,
    error: str | None = None,
    ssl_expiry: datetime | None = None,
) -> CheckRun:
    """Add a new check run result and update the monitor status."""
    check_run = CheckRun(
        monitor_id=monitor_id,
        is_up=is_up,
        latency_ms=latency_ms,
        status_code=status_code,
        error=error,
        ssl_expiry=ssl_expiry,
    )
    db.add(check_run)

    # Update monitor last_check_at and basic status
    monitor = get_monitor(db, monitor_id)
    if monitor:
        monitor.last_check_at = datetime.utcnow()
        # Basic status; sophisticated logic like 'flapping' is handled by the engine
        monitor.status = "up" if is_up else "down"

    db.commit()
    db.refresh(check_run)
    return check_run


def get_recent_check_runs(db: Session, monitor_id: int, limit: int = 50) -> list[CheckRun]:
    """Fetch the most recent check runs for a monitor."""
    return (
        db.execute(
            select(CheckRun)
            .where(CheckRun.monitor_id == monitor_id)
            .order_by(CheckRun.timestamp.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )


def get_check_runs_by_time_range(
    db: Session, monitor_id: int, hours: int | None = None, days: int | None = None
) -> list[CheckRun]:
    """Fetch check runs for a monitor within a specific time range.

    Args:
        db: Database session
        monitor_id: ID of the monitor
        hours: Number of hours to look back (mutually exclusive with days)
        days: Number of days to look back (mutually exclusive with hours)

    Returns:
        List of CheckRun objects ordered by timestamp descending (newest first)
    """
    if hours is not None:
        cutoff = datetime.utcnow() - timedelta(hours=hours)
    elif days is not None:
        cutoff = datetime.utcnow() - timedelta(days=days)
    else:
        # Default to 24 hours
        cutoff = datetime.utcnow() - timedelta(hours=24)

    return (
        db.execute(
            select(CheckRun)
            .where(CheckRun.monitor_id == monitor_id, CheckRun.timestamp >= cutoff)
            .order_by(CheckRun.timestamp.desc())
        )
        .scalars()
        .all()
    )


def get_open_incident(db: Session, monitor_id: int) -> Incident | None:
    """Find if there is currently an open incident for this monitor."""
    return db.execute(
        select(Incident).where(Incident.monitor_id == monitor_id, Incident.ended_at == None)
    ).scalar_one_or_none()


def start_incident(db: Session, monitor_id: int, error: str | None = None) -> Incident:
    """Create a new incident record."""
    incident = Incident(monitor_id=monitor_id, last_error=error)
    db.add(incident)
    db.commit()
    db.refresh(incident)
    return incident


def resolve_incident(db: Session, monitor_id: int) -> Incident | None:
    """Close an open incident by setting its ended_at time."""
    incident = get_open_incident(db, monitor_id)
    if incident:
        incident.ended_at = datetime.utcnow()
        db.commit()
        db.refresh(incident)
    return incident
