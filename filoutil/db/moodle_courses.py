"""Database functions for Moodle courses and grades."""

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from filoutil.db.models import MoodleCourse, MoodleGrade


def get_user_courses(
    db: Session, user_id: int, archived: bool | None = False
) -> list[MoodleCourse]:
    """Get user courses filtered by archive state."""
    stmt = select(MoodleCourse).where(MoodleCourse.user_id == user_id)
    if archived is not None:
        stmt = stmt.where(MoodleCourse.archived == archived)
    stmt = stmt.order_by(MoodleCourse.course_name.asc())
    return db.execute(stmt).scalars().all()


def get_course_by_moodle_id(db: Session, user_id: int, course_id: int) -> MoodleCourse | None:
    """Get a specific course by Moodle course ID."""
    return db.execute(
        select(MoodleCourse).where(
            MoodleCourse.user_id == user_id, MoodleCourse.course_id == course_id
        )
    ).scalar_one_or_none()


def upsert_course(db: Session, user_id: int, course_data: dict) -> MoodleCourse:
    """
    Insert or update a course in the database.

    Args:
        db: Database session
        user_id: User ID
        course_data: Course data from Moodle API (must include 'id', 'fullname', 'shortname')

    Returns:
        MoodleCourse object
    """
    course_id = course_data.get("id")
    if not course_id:
        raise ValueError("Course data must include 'id' field")

    existing = get_course_by_moodle_id(db, user_id, course_id)

    if existing:
        # Update existing course
        existing.course_name = course_data.get("fullname", existing.course_name)
        existing.shortname = course_data.get("shortname", existing.shortname)
        existing.archived = False
        db.commit()
        db.refresh(existing)
        return existing
    else:
        # Create new course
        course = MoodleCourse(
            user_id=user_id,
            course_id=course_id,
            course_name=course_data.get("fullname", ""),
            shortname=course_data.get("shortname"),
            archived=False,
        )
        db.add(course)
        db.commit()
        db.refresh(course)
        return course


def archive_missing_courses(
    db: Session, user_id: int, active_course_ids: Iterable[int]
) -> list[MoodleCourse]:
    """Archive user courses that were not present in the latest Moodle sync."""
    active_ids = {course_id for course_id in active_course_ids if course_id is not None}
    courses = get_user_courses(db, user_id, archived=None)
    archived_courses = []

    for course in courses:
        should_archive = course.course_id not in active_ids
        if should_archive and not course.archived:
            course.archived = True
            archived_courses.append(course)

    if archived_courses:
        db.commit()
        for course in archived_courses:
            db.refresh(course)

    return archived_courses


def get_course_grades(db: Session, user_id: int, course_id: int) -> list[MoodleGrade]:
    """Get all grades for a course."""
    return (
        db.execute(
            select(MoodleGrade).where(
                MoodleGrade.user_id == user_id, MoodleGrade.course_id == course_id
            )
        )
        .scalars()
        .all()
    )


def get_grade_by_item_id(
    db: Session, user_id: int, course_id: int, grade_item_id: int
) -> MoodleGrade | None:
    """Get a specific grade by grade item ID."""
    return db.execute(
        select(MoodleGrade).where(
            MoodleGrade.user_id == user_id,
            MoodleGrade.course_id == course_id,
            MoodleGrade.grade_item_id == grade_item_id,
        )
    ).scalar_one_or_none()


def upsert_grade(db: Session, user_id: int, course_id: int, grade_data: dict) -> MoodleGrade:
    """
    Insert or update a grade in the database.

    Args:
        db: Database session
        user_id: User ID
        course_id: Moodle course ID
        grade_data: Grade data from Moodle API (must include 'id', 'itemname')

    Returns:
        MoodleGrade object
    """
    grade_item_id = grade_data.get("id")
    if not grade_item_id:
        raise ValueError("Grade data must include 'id' field")

    existing = get_grade_by_item_id(db, user_id, course_id, grade_item_id)

    from datetime import datetime

    grade_raw = grade_data.get("graderaw")
    grade_formatted = grade_data.get("gradeformatted")

    if existing:
        existing.item_name = grade_data.get("itemname", existing.item_name)
        existing.item_type = grade_data.get("itemtype", existing.item_type)
        existing.item_module = grade_data.get("itemmodule", existing.item_module)
        existing.grade_raw = grade_raw if grade_raw is not None else existing.grade_raw
        existing.grade_formatted = grade_formatted or existing.grade_formatted
        existing.grade_max = grade_data.get("grademax", existing.grade_max)
        existing.grade_min = grade_data.get("grademin", existing.grade_min)
        existing.grade_date_submitted = grade_data.get(
            "gradedatesubmitted", existing.grade_date_submitted
        )
        existing.grade_date_graded = grade_data.get("gradedategraded", existing.grade_date_graded)
        existing.feedback = grade_data.get("feedback", existing.feedback)
        existing.last_checked_at = datetime.utcnow()
        db.commit()
        db.refresh(existing)
        return existing
    else:
        grade = MoodleGrade(
            user_id=user_id,
            course_id=course_id,
            grade_item_id=grade_item_id,
            item_name=grade_data.get("itemname", ""),
            item_type=grade_data.get("itemtype"),
            item_module=grade_data.get("itemmodule"),
            grade_raw=grade_raw,
            grade_formatted=grade_formatted,
            grade_max=grade_data.get("grademax"),
            grade_min=grade_data.get("grademin"),
            grade_date_submitted=grade_data.get("gradedatesubmitted"),
            grade_date_graded=grade_data.get("gradedategraded"),
            feedback=grade_data.get("feedback"),
            last_checked_at=datetime.utcnow(),
        )
        db.add(grade)
        db.commit()
        db.refresh(grade)
        return grade
