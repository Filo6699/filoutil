"""Course Watcher for Moodle Cabinet.

This module handles fetching courses and grades from Moodle and detecting changes.
"""

import html
import logging
import re
import time
from datetime import datetime
from typing import Any

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.orm import Session
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application

from filoutil.db.models import MoodleCourse, MoodleGrade, SessionRefresh, User
from filoutil.db.moodle_courses import (
    get_course_by_moodle_id,
    get_course_grades,
    get_grade_by_item_id,
    upsert_course,
    upsert_grade,
)
from filoutil.db.postgres import SessionLocal
from filoutil.moodle_cabinet.notifications_manager import fetch_moodle_user_id
from filoutil.moodle_cabinet.request_logger import log_moodle_request

logger = logging.getLogger(__name__)

LMS_BASE_URL = "https://lms.astanait.edu.kz"
LMS_ENDPOINT = "/lib/ajax/service.php"

# Check interval for course watcher (in seconds)
DEFAULT_COURSE_CHECK_INTERVAL = 300


async def fetch_user_courses(
    sesskey: str,
    moodleSession: str,
    moodle_user_id: int,
    session_refresh_id: int | None = None,
) -> tuple[bool, list[dict[str, Any]] | None, str | None]:
    """
    Fetch enrolled courses from Moodle API.

    Args:
        sesskey: Moodle session key
        moodleSession: Moodle session cookie value
        moodle_user_id: Moodle user ID
        session_refresh_id: Optional session refresh ID for logging

    Returns:
        Tuple of (success: bool, courses: list[dict] | None, error_message: str | None)
    """
    url = f"{LMS_BASE_URL}{LMS_ENDPOINT}"
    params = {
        "sesskey": sesskey,
        "info": "core_course_get_enrolled_courses_by_timeline_classification",
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": LMS_BASE_URL,
        "Referer": f"{LMS_BASE_URL}/",
    }
    cookies = {"MoodleSession": moodleSession}
    payload = [
        {
            "index": 0,
            "methodname": "core_course_get_enrolled_courses_by_timeline_classification",
            "args": {
                "classification": "inprogress",  # Get current/in-progress courses
                "limit": 0,  # 0 means no limit
                "offset": 0,
            },
        }
    ]

    # Measure request timing
    start_time = time.perf_counter()
    request_size_bytes = None
    response_size_bytes = None
    response_status_code = None
    success = False

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            # Calculate request size (approximate)
            import json

            request_body = json.dumps(payload)
            request_size_bytes = len(request_body.encode("utf-8"))

            response = await client.post(
                url, params=params, headers=headers, cookies=cookies, json=payload
            )

            response_status_code = response.status_code
            response_size_bytes = len(response.content) if response.content else None

            if response.status_code == 200:
                try:
                    data = response.json()

                    if isinstance(data, list) and len(data) > 0:
                        item = data[0]
                        if "error" in item:
                            error_val = item["error"]
                            if error_val is False or error_val is None:
                                # Success - extract courses
                                courses_data = item.get("data", {})
                                courses = courses_data.get("courses", [])
                                success = True
                                return True, courses, None
                            elif error_val is True:
                                error_msg = item.get("message", "Unknown error")
                                return False, None, f"Moodle error: {error_msg}"
                            elif isinstance(error_val, dict):
                                error_msg = error_val.get("message", str(error_val))
                                return False, None, f"Moodle error: {error_msg}"
                            else:
                                return False, None, f"Moodle error: {error_val}"
                    return False, None, "Invalid response format"
                except Exception as e:
                    logger.warning(
                        f"Failed to parse courses response JSON: {e}, response text: {response.text[:500]}"
                    )
                    return False, None, f"Failed to parse response: {str(e)}"
            else:
                error_text = response.text[:200] if response.text else "No response body"
                logger.error(f"Moodle returned status {response.status_code}: {error_text}")
                return False, None, f"HTTP {response.status_code}: {error_text}"

    except httpx.RequestError as e:
        response_status_code = 0  # No response received
        return False, None, f"Request failed: {str(e)}"
    except Exception as e:
        response_status_code = 0  # No response received
        return False, None, f"Unexpected error: {str(e)}"
    finally:
        # Log the request
        end_time = time.perf_counter()
        response_time_ms = (end_time - start_time) * 1000  # Convert to milliseconds

        log_moodle_request(
            http_method="POST",
            endpoint_path=LMS_ENDPOINT,
            response_status_code=response_status_code or 0,
            response_time_ms=response_time_ms,
            success=success,
            api_method_name="core_course_get_enrolled_courses_by_timeline_classification",
            request_size_bytes=request_size_bytes,
            response_size_bytes=response_size_bytes,
            session_refresh_id=session_refresh_id,
        )


async def fetch_course_grades(
    moodleSession: str, course_id: int, session_refresh_id: int | None = None
) -> tuple[bool, list[dict[str, Any]] | None, str | None]:
    """
    Fetch grade items for a course by scraping the gradebook HTML page.

    Args:
        moodleSession: Moodle session cookie value
        course_id: Moodle course ID
        session_refresh_id: Optional session refresh ID for logging

    Returns:
        Tuple of (success: bool, grade_items: list[dict] | None, error_message: str | None)
    """
    url = f"{LMS_BASE_URL}/grade/report/user/index.php"
    endpoint_path = "/grade/report/user/index.php"
    params = {"id": course_id}
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": f"{LMS_BASE_URL}/",
    }
    cookies = {"MoodleSession": moodleSession}

    # Measure request timing
    start_time = time.perf_counter()
    request_size_bytes = None
    response_size_bytes = None
    response_status_code = None
    success = False

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(url, params=params, headers=headers, cookies=cookies)

            response_status_code = response.status_code
            response_size_bytes = len(response.content) if response.content else None

            if response.status_code == 200:
                try:
                    # Save fetched HTML for debugging
                    try:
                        import os

                        os.makedirs("context", exist_ok=True)
                        with open("context/fetched_page.html", "w", encoding="utf-8") as f:
                            f.write(response.text)
                    except Exception as e:
                        logger.warning(f"fetch_course_grades: Failed to save HTML: {e}")

                    soup = BeautifulSoup(response.text, "html.parser")

                    # Find the grade table
                    grade_table = soup.find("table", class_="user-grade")
                    if not grade_table:
                        logger.warning(
                            f"fetch_course_grades: Grade table not found in HTML for course_id={course_id}"
                        )
                        # Debug: check if page contains expected elements
                        return True, [], None  # No grades table found

                    grade_items = []
                    tbody = grade_table.find("tbody")
                    if not tbody:
                        logger.warning(
                            f"fetch_course_grades: tbody not found in grade table for course_id={course_id}"
                        )
                        return True, [], None

                    # Find rows by looking for th with id="row_X_Y" pattern
                    # This is more reliable than class-based matching
                    all_ths_with_id = tbody.find_all("th", id=re.compile(r"^row_\d+_\d+$"))

                    # Get unique parent rows
                    rows = []
                    seen_rows = set()
                    for th in all_ths_with_id:
                        row = th.find_parent("tr")
                        if row and id(row) not in seen_rows:
                            rows.append(row)
                            seen_rows.add(id(row))

                    processed_count = 0
                    skipped_count = 0
                    for row in rows:
                        # Skip spacer rows (but NOT category rows, as they may contain categoryitem totals)
                        if "spacer" in row.get("class", []):
                            skipped_count += 1
                            continue

                        # Extract row ID to get grade_item_id
                        row_th = row.find("th", id=re.compile(r"^row_\d+_\d+$"))
                        if not row_th or not row_th.get("id"):
                            skipped_count += 1
                            continue

                        row_id = row_th.get("id")
                        # Extract grade_item_id from row ID (format: row_{grade_item_id}_{user_id})
                        match = re.match(r"row_(\d+)_\d+", row_id)
                        if not match:
                            skipped_count += 1
                            continue
                        grade_item_id = int(match.group(1))

                        # Extract item name
                        item_name_elem = row_th.find(
                            "span", class_="gradeitemheader"
                        ) or row_th.find("a", class_="gradeitemheader")
                        if not item_name_elem:
                            continue
                        item_name = item_name_elem.get_text(strip=True)

                        # Extract item type from the small text above the name
                        item_type_elem = row_th.find("span", class_="text-uppercase")
                        item_type = (
                            item_type_elem.get_text(strip=True).lower() if item_type_elem else None
                        )

                        # Extract item module from link if available
                        item_module = None
                        item_link = row_th.find("a", class_="gradeitemheader")
                        if item_link and item_link.get("href"):
                            href = item_link.get("href")
                            # Extract module from URL like /mod/assign/view.php
                            mod_match = re.search(r"/mod/(\w+)/", href)
                            if mod_match:
                                item_module = mod_match.group(1)

                        # Extract grade from grade column
                        grade_cell = row.find("td", class_=re.compile(r"column-grade\b"))
                        grade_text = grade_cell.get_text(strip=True) if grade_cell else "-"
                        # Strip "Grade analysis" text if present
                        if grade_text and "Grade analysis" in grade_text:
                            grade_text = grade_text.replace("Grade analysis", "").strip()
                        grade_raw = None
                        grade_formatted = grade_text if grade_text != "-" else None
                        if grade_formatted:
                            try:
                                # Try to parse as float
                                grade_raw = float(grade_formatted)
                            except ValueError:
                                pass

                        # Extract range
                        range_cell = row.find("td", class_=re.compile(r"column-range\b"))
                        range_text = range_cell.get_text(strip=True) if range_cell else None
                        grade_min = None
                        grade_max = None
                        if range_text and "–" in range_text:
                            try:
                                parts = range_text.split("–")
                                if len(parts) == 2:
                                    grade_min = float(parts[0].strip())
                                    grade_max = float(parts[1].strip())
                            except ValueError:
                                pass

                        # Extract percentage
                        percentage_cell = row.find("td", class_=re.compile(r"column-percentage\b"))
                        percentage_text = (
                            percentage_cell.get_text(strip=True) if percentage_cell else None
                        )

                        # Extract feedback
                        feedback_cell = row.find("td", class_=re.compile(r"column-feedback\b"))
                        feedback = None
                        if feedback_cell:
                            feedback_text = feedback_cell.get_text(strip=True)
                            if feedback_text and feedback_text != "&nbsp;" and feedback_text:
                                feedback = feedback_text

                        grade_item = {
                            "id": grade_item_id,
                            "itemname": item_name,
                            "itemtype": item_type,
                            "itemmodule": item_module,
                            "graderaw": grade_raw,
                            "gradeformatted": grade_formatted,
                            "grademin": grade_min,
                            "grademax": grade_max,
                            "percentageformatted": percentage_text,
                            "feedback": feedback,
                            # Dates are not available in the HTML, set to None
                            "gradedatesubmitted": None,
                            "gradedategraded": None,
                        }

                        grade_items.append(grade_item)
                        processed_count += 1

                    register_final_available = False
                    for item in grade_items:
                        item_name_lower = item["itemname"].lower()
                        if "register final" in item_name_lower:
                            if item["graderaw"] is not None or (
                                item["gradeformatted"] and item["gradeformatted"] != "-"
                            ):
                                register_final_available = True
                                break

                    filtered_items = []
                    for item in grade_items:
                        item_name_lower = item["itemname"].lower()
                        grade_raw = item["graderaw"]

                        is_register_total = (
                            "register total" in item_name_lower
                            or "register(not to edit) total" in item_name_lower
                            or "registertotal" in item_name_lower
                        )
                        is_course_total = (
                            "course total" in item_name_lower or item_name_lower == "total"
                        )

                        if (
                            (is_register_total or is_course_total)
                            and grade_raw is not None
                            and grade_raw == 0.0
                        ):
                            if is_register_total:
                                if not register_final_available:
                                    continue
                            else:
                                continue

                        filtered_items.append(item)

                    success = True
                    return True, filtered_items, None

                except Exception as e:
                    logger.warning(
                        f"Failed to parse grades HTML: {e}, response text: {response.text[:500]}"
                    )
                    return False, None, f"Failed to parse HTML: {str(e)}"
            else:
                error_text = response.text[:200] if response.text else "No response body"
                logger.error(f"Moodle returned status {response.status_code}: {error_text}")
                return False, None, f"HTTP {response.status_code}: {error_text}"

    except httpx.RequestError as e:
        response_status_code = 0  # No response received
        return False, None, f"Request failed: {str(e)}"
    except Exception as e:
        response_status_code = 0  # No response received
        return False, None, f"Unexpected error: {str(e)}"
    finally:
        # Log the request
        end_time = time.perf_counter()
        response_time_ms = (end_time - start_time) * 1000  # Convert to milliseconds

        log_moodle_request(
            http_method="GET",
            endpoint_path=endpoint_path,
            response_status_code=response_status_code or 0,
            response_time_ms=response_time_ms,
            success=success,
            api_method_name=None,  # GET request, no API method
            request_size_bytes=request_size_bytes,
            response_size_bytes=response_size_bytes,
            session_refresh_id=session_refresh_id,
        )


def sync_courses_for_user(
    db: Session, user_id: int, courses_data: list[dict[str, Any]]
) -> list[MoodleCourse]:
    """
    Sync courses to database for a user.

    Args:
        db: Database session
        user_id: User ID
        courses_data: List of course data from Moodle API

    Returns:
        List of MoodleCourse objects
    """
    synced_courses = []
    for course_data in courses_data:
        try:
            course = upsert_course(db, user_id, course_data)
            synced_courses.append(course)
        except Exception as e:
            logger.error(f"Failed to sync course {course_data.get('id')} for user {user_id}: {e}")
    return synced_courses


def sync_grades_for_course(
    db: Session, user_id: int, course_id: int, grade_items: list[dict[str, Any]]
) -> list[MoodleGrade]:
    """
    Sync grades to database for a course.

    Args:
        db: Database session
        user_id: User ID
        course_id: Moodle course ID
        grade_items: List of grade item data from Moodle API

    Returns:
        List of MoodleGrade objects
    """
    synced_grades = []
    for grade_data in grade_items:
        try:
            # Filtering can be added here if needed
            # For example, to only track assignments:
            # if grade_data.get("itemmodule") != "assign":
            #     continue
            grade = upsert_grade(db, user_id, course_id, grade_data)
            synced_grades.append(grade)
        except Exception as e:
            logger.error(
                f"Failed to sync grade {grade_data.get('id')} for course {course_id}, user {user_id}: {e}"
            )
    return synced_grades


def detect_grade_changes(
    db: Session, user_id: int, course_id: int, new_grades: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """
    Compare new grades with stored grades and detect changes.

    Args:
        db: Database session
        user_id: User ID
        course_id: Moodle course ID
        new_grades: List of new grade data from Moodle API

    Returns:
        List of grade change dictionaries with 'type' ('new' or 'updated'), 'old_grade', 'new_grade', 'grade_data'
    """
    changes = []
    stored_grades = get_course_grades(db, user_id, course_id)

    stored_grades_map = {grade.grade_item_id: grade for grade in stored_grades}

    register_final_available = False
    for grade_data in new_grades:
        item_name_lower = grade_data.get("itemname", "").lower()
        if "register final" in item_name_lower:
            grade_raw = grade_data.get("graderaw")
            grade_formatted = grade_data.get("gradeformatted")
            if grade_raw is not None or (grade_formatted and grade_formatted != "-"):
                register_final_available = True

    for grade_data in new_grades:
        grade_item_id = grade_data.get("id")
        if not grade_item_id:
            continue

        item_name_lower = grade_data.get("itemname", "").lower()
        new_grade_raw = grade_data.get("graderaw")
        new_grade_formatted = grade_data.get("gradeformatted")
        new_date_graded = grade_data.get("gradedategraded")

        is_register_total = (
            "register total" in item_name_lower
            or "register(not to edit) total" in item_name_lower
            or "registertotal" in item_name_lower
        )
        is_course_total = "course total" in item_name_lower or item_name_lower == "total"

        if (
            (is_register_total or is_course_total)
            and new_grade_raw is not None
            and new_grade_raw == 0.0
        ):
            if is_register_total:
                if not register_final_available:
                    continue
            else:
                continue

        stored_grade = stored_grades_map.get(grade_item_id)

        if not stored_grade:
            if new_grade_raw is not None or new_grade_formatted:
                changes.append(
                    {
                        "type": "new",
                        "old_grade": None,
                        "new_grade": (
                            new_grade_formatted or str(new_grade_raw)
                            if new_grade_raw is not None
                            else "N/A"
                        ),
                        "grade_data": grade_data,
                    }
                )
        else:
            old_grade_raw = stored_grade.grade_raw
            old_grade_formatted = stored_grade.grade_formatted
            old_date_graded = stored_grade.grade_date_graded

            grade_changed = False
            if new_grade_raw is not None and old_grade_raw is not None:
                if abs(new_grade_raw - old_grade_raw) > 0.01:
                    grade_changed = True
            elif new_grade_raw is not None and old_grade_raw is None:
                grade_changed = True
            elif new_grade_raw is None and old_grade_raw is not None:
                grade_changed = True

            date_changed = new_date_graded and new_date_graded != old_date_graded

            if grade_changed or date_changed:
                changes.append(
                    {
                        "type": "updated",
                        "old_grade": (
                            old_grade_formatted or str(old_grade_raw)
                            if old_grade_raw is not None
                            else "N/A"
                        ),
                        "new_grade": (
                            new_grade_formatted or str(new_grade_raw)
                            if new_grade_raw is not None
                            else "N/A"
                        ),
                        "grade_data": grade_data,
                    }
                )

    return changes


async def send_batch_grade_notification(
    app: Application,
    user_telegram_id: int,
    grade_changes: list[tuple[MoodleCourse, dict[str, Any]]],
) -> bool:
    """
    Send a batch notification when there are many grade changes at once.

    Args:
        app: Telegram application
        user_telegram_id: User's Telegram ID
        grade_changes: List of tuples (course, grade_change)

    Returns:
        True if sent successfully, False otherwise
    """
    try:

        def escape_md(text: str) -> str:
            special_chars = r"_*[]()~`>#+-=|{}.!"
            return "".join(f"\\{c}" if c in special_chars else c for c in str(text))

        count = len(grade_changes)
        if count == 0:
            return True

        if count > 25:
            message = f"📊 *New Grades \\({count}\\)*\n\n"
            message += "Too many changes\\. Check your grades in Moodle\\."
        else:
            message = f"📊 *New Grades \\({count}\\)*\n\n"
            for course, grade_change in grade_changes:
                grade_data = grade_change["grade_data"]
                item_name = html.unescape(grade_data.get("itemname", "Unknown"))
                new_grade = grade_change.get("new_grade", "N/A")
                course_name = html.unescape(course.course_name)
                course_name = course_name[:30] + "..." if len(course_name) > 30 else course_name
                item_name_short = item_name[:25] + "..." if len(item_name) > 25 else item_name
                message += f"• {escape_md(course_name)}: {escape_md(item_name_short)} — *{escape_md(new_grade)}*\n"

        await app.bot.send_message(
            chat_id=user_telegram_id,
            text=message,
            parse_mode="MarkdownV2",
            disable_web_page_preview=True,
        )
        return True
    except Exception as e:
        logger.error(f"Failed to send batch grade notification to user {user_telegram_id}: {e}")
        return False


async def send_grade_notification(
    app: Application, user_telegram_id: int, course: MoodleCourse, grade_change: dict[str, Any]
) -> bool:
    """
    Send a grade change notification to the user via Telegram.

    Args:
        app: Telegram application
        user_telegram_id: User's Telegram ID
        course: MoodleCourse object
        grade_change: Grade change dictionary with 'type', 'old_grade', 'new_grade', 'grade_data'

    Returns:
        True if sent successfully, False otherwise
    """
    try:
        grade_data = grade_change["grade_data"]
        item_name = grade_data.get("itemname", "Unknown")
        change_type = grade_change["type"]
        old_grade = grade_change.get("old_grade", "N/A")
        new_grade = grade_change.get("new_grade", "N/A")
        grade_date_graded = grade_data.get("gradedategraded")

        # Format date graded
        date_str = None
        if grade_date_graded is not None and grade_date_graded > 0:
            try:
                date_obj = datetime.fromtimestamp(grade_date_graded)
                date_str = date_obj.strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, OSError, TypeError):
                pass

        # Build message
        if change_type == "new":
            message = f"📊 *New Grade*\n\n"
        else:
            message = f"📊 *Grade Update*\n\n"

        message += f"*Course:* {course.course_name}\n"
        message += f"*Assignment:* {item_name}\n"

        if change_type == "new":
            message += f"*Grade:* {new_grade}\n"
        else:
            message += f"*Grade:* {old_grade} → {new_grade}\n"

        # Only include date graded if it's available
        if date_str:
            message += f"*Date Graded:* {date_str}\n"

        # Add course URL
        gradebook_url = f"{LMS_BASE_URL}/grade/report/user/index.php?id={course.course_id}"
        message += f"\n🔗 [View in Moodle]({gradebook_url})"

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "📊 View Full Grades",
                        callback_data=f"moodle:grades:course:{course.course_id}",
                    )
                ]
            ]
        )

        await app.bot.send_message(
            chat_id=user_telegram_id,
            text=message,
            parse_mode="Markdown",
            disable_web_page_preview=True,
            reply_markup=keyboard,
        )
        return True
    except Exception as e:
        logger.error(f"Failed to send grade notification to user {user_telegram_id}: {e}")
        return False


async def process_courses_for_user(
    app: Application, user_id: int, session_refresh: SessionRefresh
) -> dict[str, int]:
    """
    Fetch and process courses and grades for a user with an active session.

    Args:
        app: Telegram application
        user_id: User ID
        session_refresh: SessionRefresh object

    Returns:
        Dictionary with 'grades_changed' and 'grades_unchanged' counts
    """
    try:
        moodle_user_id = session_refresh.moodle_user_id

        # If not stored, try to fetch it now (fallback for sessions created before this fix)
        if not moodle_user_id:
            logger.info(f"Moodle user ID not found for user {user_id}, attempting to fetch...")
            success, fetched_user_id, error_msg = await fetch_moodle_user_id(
                session_refresh.sesskey, session_refresh.moodleSession
            )
            if success and fetched_user_id:
                moodle_user_id = fetched_user_id
                # Store it in the session refresh
                with SessionLocal() as db:
                    stored_session = db.execute(
                        select(SessionRefresh).where(SessionRefresh.id == session_refresh.id)
                    ).scalar_one_or_none()
                    if stored_session:
                        stored_session.moodle_user_id = moodle_user_id
                        db.commit()
                        db.refresh(stored_session)
                        # Update the passed session_refresh object
                        session_refresh.moodle_user_id = moodle_user_id
                        logger.info(
                            f"Successfully fetched and stored Moodle user ID {moodle_user_id} for session {session_refresh.id}"
                        )
            else:
                logger.warning(
                    f"Could not determine Moodle user ID for user {user_id}: {error_msg}"
                )
                return {"grades_changed": 0, "grades_unchanged": 0}

        # Fetch courses from Moodle
        success, courses_data, error_msg = await fetch_user_courses(
            session_refresh.sesskey,
            session_refresh.moodleSession,
            moodle_user_id,
            session_refresh_id=session_refresh.id,
        )

        if not success:
            logger.warning(f"Failed to fetch courses for user {user_id}: {error_msg}")
            return {"grades_changed": 0, "grades_unchanged": 0}

        if not courses_data:
            logger.info(f"No courses found for user {user_id}")
            return {"grades_changed": 0, "grades_unchanged": 0}

        # Sync courses to database
        with SessionLocal() as db:
            synced_courses = sync_courses_for_user(db, user_id, courses_data)

            # Get user for Telegram ID
            user = db.execute(select(User).where(User.id == user_id)).scalar_one_or_none()
            if not user:
                logger.warning(f"User {user_id} not found")
                return {"grades_changed": 0, "grades_unchanged": 0}

            # Collect all grade changes across all courses
            all_grade_changes = []
            total_grades_checked = 0

            # Process grades for each course
            for course in synced_courses:
                # Fetch grades for this course
                grades_success, grade_items, grades_error = await fetch_course_grades(
                    session_refresh.moodleSession,
                    course.course_id,
                    session_refresh_id=session_refresh.id,
                )

                if not grades_success:
                    logger.warning(
                        f"Failed to fetch grades for course {course.course_id}, user {user_id}: {grades_error}"
                    )
                    continue

                if not grade_items:
                    logger.info(f"No grades found for course {course.course_id}, user {user_id}")
                    continue

                # Detect grade changes before syncing
                grade_changes = detect_grade_changes(db, user_id, course.course_id, grade_items)

                # Sync grades to database
                sync_grades_for_course(db, user_id, course.course_id, grade_items)

                # Track total grades checked and changes
                total_grades_checked += len(grade_items)

                # Collect all grade changes for batch processing
                for grade_change in grade_changes:
                    all_grade_changes.append((course, grade_change))

            # Send notifications - batch if more than 5, otherwise individual
            if len(all_grade_changes) > 5:
                # Send batch notification
                await send_batch_grade_notification(app, user.telegram_id, all_grade_changes)
                logger.info(
                    f"Sent batch grade notification for {len(all_grade_changes)} grade changes, user {user_id}"
                )
            else:
                # Send individual notifications
                for course, grade_change in all_grade_changes:
                    sent = await send_grade_notification(
                        app, user.telegram_id, course, grade_change
                    )
                    if sent:
                        logger.info(
                            f"Sent grade change notification for course {course.course_id}, user {user_id}"
                        )

            # Return statistics
            grades_changed = len(all_grade_changes)
            grades_unchanged = total_grades_checked - grades_changed
            return {"grades_changed": grades_changed, "grades_unchanged": grades_unchanged}

    except Exception as e:
        logger.error(f"Error processing courses for user {user_id}: {e}", exc_info=True)
        return {"grades_changed": 0, "grades_unchanged": 0}
