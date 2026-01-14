from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from filoutil.db.models import BotStatus, User


def get_bot_status(db: Session) -> BotStatus | None:
    return db.execute(select(BotStatus).limit(1)).scalar_one_or_none()


def update_heartbeat(db: Session, status: str = "online", exit_reason: str | None = None):
    bot_status = get_bot_status(db)
    if not bot_status:
        bot_status = BotStatus(
            last_heartbeat=datetime.utcnow(), status=status, exit_reason=exit_reason
        )
        db.add(bot_status)
    else:
        bot_status.last_heartbeat = datetime.utcnow()
        bot_status.status = status
        if exit_reason is not None:
            bot_status.exit_reason = exit_reason
    db.commit()


def get_admins(db: Session):
    return db.execute(select(User).where(User.role == "admin")).scalars().all()
