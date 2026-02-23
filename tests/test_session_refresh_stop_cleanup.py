from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from filoutil.db.models import Base, SessionRefresh, User
from filoutil.db.session_refresh import stop_session_refresh


def _build_db_session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    testing_session_local = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return testing_session_local()


def _seed_user_and_session(db, oidc_data: dict | None = None) -> SessionRefresh:
    user = User(telegram_id=123456789, username="testuser", whitelisted=True)
    db.add(user)
    db.commit()
    db.refresh(user)

    session_refresh = SessionRefresh(
        user_id=user.id,
        sesskey="sesskey123",
        moodleSession="moodlesession123",
        refresh_interval_s=300,
        status="running",
        oidc_data=oidc_data,
    )
    db.add(session_refresh)
    db.commit()
    db.refresh(session_refresh)
    return session_refresh


def test_stop_session_refresh_clears_oidc_data_when_requested():
    db = _build_db_session()
    try:
        session_refresh = _seed_user_and_session(
            db,
            oidc_data={"microsoft_cookies": "ESTSAUTHPERSISTENT=abc; ESTSAUTH=def"},
        )

        stopped = stop_session_refresh(
            db,
            session_refresh.id,
            status="stopped",
            clear_oidc_data=True,
        )

        assert stopped is not None
        assert stopped.status == "stopped"
        assert stopped.oidc_data is None
    finally:
        db.close()


def test_stop_session_refresh_keeps_oidc_data_by_default():
    db = _build_db_session()
    try:
        original_oidc_data = {"microsoft_cookies": "ESTSAUTHPERSISTENT=abc; ESTSAUTH=def"}
        session_refresh = _seed_user_and_session(db, oidc_data=original_oidc_data)

        stopped = stop_session_refresh(db, session_refresh.id, status="failed")

        assert stopped is not None
        assert stopped.status == "failed"
        assert stopped.oidc_data == original_oidc_data
    finally:
        db.close()
