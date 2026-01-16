from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str] = mapped_column(String, nullable=True)
    role: Mapped[str] = mapped_column(String, default="user")
    whitelisted: Mapped[bool] = mapped_column(Boolean, default=False)
    settings: Mapped[dict] = mapped_column(JSON, default={})
    last_activity_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    # Relationships
    permissions: Mapped[list["UserPermission"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    def to_dict(self):
        return {
            "id": self.id,
            "telegram_id": self.telegram_id,
            "username": self.username,
            "role": self.role,
            "whitelisted": self.whitelisted,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class BotStatus(Base):
    __tablename__ = "bot_status"

    id: Mapped[int] = mapped_column(primary_key=True)
    last_heartbeat: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    status: Mapped[str] = mapped_column(String, default="online")  # online, offline, crashed
    exit_reason: Mapped[str] = mapped_column(String, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class Monitor(Base):
    __tablename__ = "monitors"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String)
    url: Mapped[str] = mapped_column(String)
    method: Mapped[str] = mapped_column(String, default="GET")
    interval_s: Mapped[int] = mapped_column(Integer, default=60)
    timeout_s: Mapped[int] = mapped_column(Integer, default=10)
    expected_status: Mapped[int] = mapped_column(Integer, default=200)
    keyword: Mapped[str] = mapped_column(String, nullable=True)
    json_schema: Mapped[dict] = mapped_column(JSON, nullable=True)
    headers: Mapped[dict] = mapped_column(JSON, nullable=True)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    alert_on_down: Mapped[bool] = mapped_column(Boolean, default=True)
    alert_on_up: Mapped[bool] = mapped_column(Boolean, default=True)

    last_check_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String, default="unknown")  # up, down, flapping

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    # Relationships
    check_runs: Mapped[list["CheckRun"]] = relationship(
        back_populates="monitor", cascade="all, delete-orphan"
    )
    incidents: Mapped[list["Incident"]] = relationship(
        back_populates="monitor", cascade="all, delete-orphan"
    )


class CheckRun(Base):
    __tablename__ = "check_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    monitor_id: Mapped[int] = mapped_column(ForeignKey("monitors.id", ondelete="CASCADE"))
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    is_up: Mapped[bool] = mapped_column(Boolean)
    latency_ms: Mapped[float] = mapped_column(Float)
    status_code: Mapped[int] = mapped_column(Integer, nullable=True)
    error: Mapped[str] = mapped_column(String, nullable=True)

    ssl_expiry: Mapped[datetime] = mapped_column(DateTime, nullable=True)

    # Relationships
    monitor: Mapped["Monitor"] = relationship(back_populates="check_runs")


class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(primary_key=True)
    monitor_id: Mapped[int] = mapped_column(ForeignKey("monitors.id", ondelete="CASCADE"))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    ended_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)

    last_error: Mapped[str] = mapped_column(String, nullable=True)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)

    # Relationships
    monitor: Mapped["Monitor"] = relationship(back_populates="incidents")


class SessionRefresh(Base):
    __tablename__ = "session_refresh"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    sesskey: Mapped[str] = mapped_column(String)
    moodleSession: Mapped[str] = mapped_column(String)
    moodle_user_id: Mapped[int] = mapped_column(
        Integer, nullable=True
    )  # Moodle's user ID (useridto)
    name: Mapped[str] = mapped_column(
        String, nullable=True
    )  # Optional name/description for the session
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    ended_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String, default="running")  # running, stopped, failed
    refresh_interval_s: Mapped[int] = mapped_column(Integer, default=300)

    # Relationships
    user: Mapped["User"] = relationship()


class UserPermission(Base):
    __tablename__ = "user_permissions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    module: Mapped[str] = mapped_column(String)  # "monitoring" or "moodle"
    granted_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    granted_by: Mapped[int] = mapped_column(BigInteger, nullable=True)  # admin telegram_id

    # Relationships
    user: Mapped["User"] = relationship(back_populates="permissions")

    __table_args__ = (UniqueConstraint("user_id", "module", name="uq_user_module"),)


class MoodleNotification(Base):
    __tablename__ = "moodle_notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    notification_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, index=True
    )  # Moodle's notification ID
    useridfrom: Mapped[int] = mapped_column(Integer, nullable=True)
    useridto: Mapped[int] = mapped_column(Integer)
    subject: Mapped[str] = mapped_column(String)
    shortenedsubject: Mapped[str] = mapped_column(String, nullable=True)
    text: Mapped[str] = mapped_column(String, nullable=True)
    fullmessage: Mapped[str] = mapped_column(String, nullable=True)
    fullmessageformat: Mapped[int] = mapped_column(Integer, nullable=True)
    fullmessagehtml: Mapped[str] = mapped_column(String, nullable=True)
    smallmessage: Mapped[str] = mapped_column(String, nullable=True)
    contexturl: Mapped[str] = mapped_column(String, nullable=True)
    contexturlname: Mapped[str] = mapped_column(String, nullable=True)
    timecreated: Mapped[int] = mapped_column(BigInteger)  # Unix timestamp
    timecreatedpretty: Mapped[str] = mapped_column(String, nullable=True)
    timeread: Mapped[int] = mapped_column(BigInteger, nullable=True)  # Unix timestamp
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    iconurl: Mapped[str] = mapped_column(String, nullable=True)
    component: Mapped[str] = mapped_column(String, nullable=True)
    eventtype: Mapped[str] = mapped_column(String, nullable=True)
    customdata: Mapped[dict] = mapped_column(JSON, nullable=True)

    # Track if notification was sent to user
    sent_to_user: Mapped[bool] = mapped_column(Boolean, default=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    # Relationships
    user: Mapped["User"] = relationship()


class MoodleCourse(Base):
    __tablename__ = "moodle_courses"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    course_id: Mapped[int] = mapped_column(Integer, index=True)  # Moodle's course ID
    course_name: Mapped[str] = mapped_column(String)
    shortname: Mapped[str] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    # Relationships
    user: Mapped["User"] = relationship()

    __table_args__ = (UniqueConstraint("user_id", "course_id", name="uq_user_course"),)


class MoodleGrade(Base):
    __tablename__ = "moodle_grades"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    course_id: Mapped[int] = mapped_column(Integer, index=True)  # Moodle's course ID
    grade_item_id: Mapped[int] = mapped_column(Integer, index=True)  # Moodle's grade item ID
    item_name: Mapped[str] = mapped_column(String)
    item_type: Mapped[str] = mapped_column(String, nullable=True)  # e.g., "mod", "category"
    item_module: Mapped[str] = mapped_column(
        String, nullable=True
    )  # e.g., "assign", "quiz" - can be filtered here if needed
    grade_raw: Mapped[float] = mapped_column(Float, nullable=True)
    grade_formatted: Mapped[str] = mapped_column(String, nullable=True)
    grade_max: Mapped[float] = mapped_column(Float, nullable=True)
    grade_min: Mapped[float] = mapped_column(Float, nullable=True)
    grade_date_submitted: Mapped[int] = mapped_column(BigInteger, nullable=True)  # Unix timestamp
    grade_date_graded: Mapped[int] = mapped_column(BigInteger, nullable=True)  # Unix timestamp
    feedback: Mapped[str] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    last_checked_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)

    # Relationships
    user: Mapped["User"] = relationship()

    __table_args__ = (
        UniqueConstraint("user_id", "course_id", "grade_item_id", name="uq_user_course_grade"),
    )
