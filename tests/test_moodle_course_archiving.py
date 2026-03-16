from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from filoutil.db.models import Base, MoodleCourse, User
from filoutil.db.moodle_courses import get_user_courses, upsert_course
from filoutil.moodle_cabinet.course_watcher import sync_courses_for_user


def _build_db_session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    testing_session_local = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return testing_session_local()


def _seed_user(db) -> User:
    user = User(telegram_id=987654321, username="courses", whitelisted=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_sync_archives_courses_missing_from_latest_payload():
    db = _build_db_session()
    try:
        user = _seed_user(db)
        upsert_course(db, user.id, {"id": 100, "fullname": "Active Course", "shortname": "AC"})
        upsert_course(db, user.id, {"id": 200, "fullname": "Old Course", "shortname": "OC"})

        synced_courses, archived_count = sync_courses_for_user(
            db,
            user.id,
            [{"id": 100, "fullname": "Active Course", "shortname": "AC"}],
        )

        assert len(synced_courses) == 1
        assert archived_count == 1
        assert [course.course_id for course in get_user_courses(db, user.id)] == [100]
        assert [course.course_id for course in get_user_courses(db, user.id, archived=True)] == [
            200
        ]
    finally:
        db.close()


def test_upsert_restores_archived_course_back_to_active():
    db = _build_db_session()
    try:
        user = _seed_user(db)
        course = MoodleCourse(
            user_id=user.id,
            course_id=300,
            course_name="Archived Course",
            shortname="ARC",
            archived=True,
        )
        db.add(course)
        db.commit()

        restored = upsert_course(
            db, user.id, {"id": 300, "fullname": "Archived Course", "shortname": "ARC"}
        )

        assert restored.archived is False
        assert [course.course_id for course in get_user_courses(db, user.id)] == [300]
        assert get_user_courses(db, user.id, archived=True) == []
    finally:
        db.close()
