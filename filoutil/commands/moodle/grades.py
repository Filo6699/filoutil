"""Grades menu and gradebook view commands."""

import html
import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown

from filoutil.db.moodle_courses import get_course_grades, get_user_courses
from filoutil.db.postgres import SessionLocal

logger = logging.getLogger(__name__)


def _escape_moodle_markdown(value: Any) -> str:
    """Unescape HTML entities and escape Telegram Markdown (v1)."""
    return escape_markdown(html.unescape(str(value or "")), version=1)


def get_grades_menu_keyboard(
    courses: list, page: int = 0, per_page: int = 8
) -> InlineKeyboardMarkup:
    """Generate keyboard for grades menu with course buttons."""
    keyboard = []

    # Pagination
    total_pages = (len(courses) + per_page - 1) // per_page if courses else 1
    start_idx = page * per_page
    end_idx = start_idx + per_page
    page_courses = courses[start_idx:end_idx]

    # Course buttons (2 per row)
    for i in range(0, len(page_courses), 2):
        row = []
        for j in range(2):
            if i + j < len(page_courses):
                course = page_courses[i + j]
                # Unescape HTML entities and truncate course name if too long
                course_name = html.unescape(course.course_name)
                if len(course_name) > 30:
                    course_name = course_name[:27] + "..."
                row.append(
                    InlineKeyboardButton(
                        course_name, callback_data=f"moodle:grades:course:{course.course_id}"
                    )
                )
        keyboard.append(row)

    # Sync button
    keyboard.append(
        [InlineKeyboardButton("📊 Sync Courses & Grades", callback_data="moodle:sync_courses")]
    )
    keyboard.append([InlineKeyboardButton("🗃 Archive", callback_data="moodle:grades:archive")])

    # Pagination buttons
    nav_buttons = []
    if page > 0:
        nav_buttons.append(
            InlineKeyboardButton("⬅️ Previous", callback_data=f"moodle:grades:page:{page - 1}")
        )
    if page < total_pages - 1:
        nav_buttons.append(
            InlineKeyboardButton("Next ➡️", callback_data=f"moodle:grades:page:{page + 1}")
        )
    if nav_buttons:
        keyboard.append(nav_buttons)

    # Back button
    keyboard.append([InlineKeyboardButton("⬅️ Back to Moodle Menu", callback_data="moodle:menu")])

    return InlineKeyboardMarkup(keyboard)


async def show_grades_menu(
    db, user_id: int, query, context: ContextTypes.DEFAULT_TYPE, page: int = 0
) -> None:
    """Show the grades menu with list of courses."""
    courses = get_user_courses(db, user_id)
    archived_courses = get_user_courses(db, user_id, archived=True)

    if not courses:
        text = (
            "📊 *Grades*\n\n"
            "You don't have any courses synced yet.\n\n"
            'Use "📊 Sync Courses & Grades" to sync your courses first.'
        )
        keyboard_rows = [
            [InlineKeyboardButton("📊 Sync Courses & Grades", callback_data="moodle:sync_courses")]
        ]
        if archived_courses:
            keyboard_rows.append(
                [InlineKeyboardButton("🗃 Archive", callback_data="moodle:grades:archive")]
            )
        keyboard_rows.append(
            [InlineKeyboardButton("⬅️ Back to Moodle Menu", callback_data="moodle:menu")]
        )
        keyboard = InlineKeyboardMarkup(keyboard_rows)
    else:
        per_page = 8
        total_pages = (len(courses) + per_page - 1) // per_page
        start_idx = page * per_page
        end_idx = start_idx + per_page
        page_courses = courses[start_idx:end_idx]

        text = f"📊 *Grades*\n\n*Total Courses:* {len(courses)}\n\n"
        text += "Select a course to view grades:"

        if total_pages > 1:
            text += f"\n\n*Page {page + 1} of {total_pages}*"

        keyboard = get_grades_menu_keyboard(courses, page=page)

    try:
        if query.message.photo:
            await query.message.delete()
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=text,
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
        else:
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


def format_gradebook(course_name: str, grades: list) -> str:
    """
    Format gradebook view from course and grades data.

    Args:
        course_name: Course name
        grades: List of MoodleGrade objects

    Returns:
        Formatted gradebook text
    """
    # Find course total and GPA if they exist
    course_total = None
    gpa = None
    scholarship_emoji = ""

    # Categorize grades
    register_midterm = None
    register_endterm = None
    register_term = None
    register_final = None
    register_total = None
    attendance = None
    assignments = []
    other_grades = []

    for grade in grades:
        item_name_lower = grade.item_name.lower()

        # Check for course total
        if "course total" in item_name_lower or (
            "total" == item_name_lower and grade.item_type == "calculated grade"
        ):
            if grade.grade_formatted and grade.grade_formatted != "-":
                try:
                    course_total = float(grade.grade_formatted)
                except (ValueError, TypeError):
                    course_total = grade.grade_formatted
            continue

        # Check for GPA
        if "gpa" in item_name_lower:
            if grade.grade_formatted and grade.grade_formatted != "-":
                try:
                    gpa = float(grade.grade_formatted)
                except (ValueError, TypeError):
                    gpa = grade.grade_formatted
            continue

        # Check for scholarship
        if "scholarship" in item_name_lower:
            scholarship_emoji = " 🎉"
            continue

        # Format grade display
        if grade.grade_formatted and grade.grade_formatted != "-":
            grade_display = grade.grade_formatted
        elif grade.grade_raw is not None:
            grade_display = f"{grade.grade_raw:.2f}".rstrip("0").rstrip(".")
        else:
            # Skip empty grades
            continue

        # Categorize the grade (unescape HTML entities in item name)
        item_name_unescaped = html.unescape(grade.item_name)
        if "register midterm" in item_name_lower:
            register_midterm = (item_name_unescaped, grade_display)
        elif "register endterm" in item_name_lower:
            register_endterm = (item_name_unescaped, grade_display)
        elif (
            "register term" in item_name_lower
            and "register total" not in item_name_lower
            and "registertotal" not in item_name_lower
        ):
            register_term = (item_name_unescaped, grade_display)
        elif "register final" in item_name_lower:
            register_final = (item_name_unescaped, grade_display)
        elif (
            "register total" in item_name_lower
            or "register(not to edit) total" in item_name_lower
            or "registertotal" in item_name_lower
        ):
            register_total = (item_name_unescaped, grade_display)
        elif "attendance" in item_name_lower:
            attendance = (item_name_unescaped, grade_display)
        elif grade.item_module == "assign" or "assignment" in item_name_lower:
            assignments.append((item_name_unescaped, grade_display))
        else:
            other_grades.append((item_name_unescaped, grade_display))

    # Build the message (unescape HTML entities in course name)
    text = f"*{_escape_moodle_markdown(course_name)}*{scholarship_emoji}\n\n"

    # Add course total and GPA if available
    if course_total is not None:
        text += f"*TOTAL* → {_escape_moodle_markdown(course_total)}\n"
    if gpa is not None:
        text += f"*GPA* → {_escape_moodle_markdown(gpa)}\n"

    if course_total is not None or gpa is not None:
        text += "\n"

    # Always show register fields (even if empty, but we skip empty ones)
    if register_midterm:
        text += f"{_escape_moodle_markdown(register_midterm[0])} → {_escape_moodle_markdown(register_midterm[1])}\n"
    if register_endterm:
        text += f"{_escape_moodle_markdown(register_endterm[0])} → {_escape_moodle_markdown(register_endterm[1])}\n"
    if register_term:
        text += f"{_escape_moodle_markdown(register_term[0])} → {_escape_moodle_markdown(register_term[1])}\n"
    if register_final:
        text += f"{_escape_moodle_markdown(register_final[0])} → {_escape_moodle_markdown(register_final[1])}\n"
    if register_total:
        display_name = register_total[0].replace("(not to edit) ", "")
        if display_name.lower() == "registertotal":
            display_name = "Register Total"
        text += f"{_escape_moodle_markdown(display_name)} → {_escape_moodle_markdown(register_total[1])}\n"

    # Add spacing before attendance
    if register_midterm or register_endterm or register_term or register_final or register_total:
        text += "\n"

    # Attendance
    if attendance:
        text += (
            f"{_escape_moodle_markdown(attendance[0])} → {_escape_moodle_markdown(attendance[1])}\n"
        )

    # Add spacing before assignments if there are any
    if assignments:
        text += "\n"
        # Add assignments
        for item_name, grade_display in assignments:
            text += (
                f"{_escape_moodle_markdown(item_name)} → {_escape_moodle_markdown(grade_display)}\n"
            )

    # Add other grades if any
    if other_grades:
        if assignments:
            text += "\n"
        for item_name, grade_display in other_grades:
            text += (
                f"{_escape_moodle_markdown(item_name)} → {_escape_moodle_markdown(grade_display)}\n"
            )

    return text


async def show_gradebook(
    db, user_id: int, course_id: int, query, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show gradebook for a specific course."""
    from filoutil.db.moodle_courses import get_course_by_moodle_id

    course = get_course_by_moodle_id(db, user_id, course_id)

    if not course:
        text = "❌ Course not found."
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Back to Grades", callback_data="moodle:grades")]]
        )
        try:
            await query.edit_message_text(text, reply_markup=keyboard)
        except BadRequest:
            pass
        return

    back_callback = "moodle:grades:archive" if course.archived else "moodle:grades"

    # Get grades for this course
    grades = get_course_grades(db, user_id, course_id)

    if not grades:
        text = (
            f"📊 *{_escape_moodle_markdown(course.course_name)}*\n\n"
            "No grades available yet.\n\n"
            "Grades will appear here once they are synced."
        )
    else:
        # Sort grades: course total first, then GPA, then by item name
        def sort_key(grade):
            item_name_lower = grade.item_name.lower()
            if "course total" in item_name_lower or (
                "total" == item_name_lower and grade.item_type == "calculated grade"
            ):
                return (0, item_name_lower)
            elif "gpa" in item_name_lower:
                return (1, item_name_lower)
            else:
                return (2, item_name_lower)

        sorted_grades = sorted(grades, key=sort_key)
        text = format_gradebook(course.course_name, sorted_grades)

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Back to Grades", callback_data=back_callback)]]
    )

    try:
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


def get_archived_courses_keyboard(
    courses: list, page: int = 0, per_page: int = 8
) -> InlineKeyboardMarkup:
    """Generate keyboard for archived courses view."""
    keyboard = []

    total_pages = (len(courses) + per_page - 1) // per_page if courses else 1
    start_idx = page * per_page
    end_idx = start_idx + per_page
    page_courses = courses[start_idx:end_idx]

    for i in range(0, len(page_courses), 2):
        row = []
        for j in range(2):
            if i + j < len(page_courses):
                course = page_courses[i + j]
                course_name = html.unescape(course.course_name)
                if len(course_name) > 30:
                    course_name = course_name[:27] + "..."
                row.append(
                    InlineKeyboardButton(
                        course_name, callback_data=f"moodle:grades:course:{course.course_id}"
                    )
                )
        keyboard.append(row)

    nav_buttons = []
    if page > 0:
        nav_buttons.append(
            InlineKeyboardButton(
                "⬅️ Previous", callback_data=f"moodle:grades:archive:page:{page - 1}"
            )
        )
    if page < total_pages - 1:
        nav_buttons.append(
            InlineKeyboardButton("Next ➡️", callback_data=f"moodle:grades:archive:page:{page + 1}")
        )
    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([InlineKeyboardButton("⬅️ Back to Grades", callback_data="moodle:grades")])
    return InlineKeyboardMarkup(keyboard)


async def show_archived_courses_menu(
    db, user_id: int, query, context: ContextTypes.DEFAULT_TYPE, page: int = 0
) -> None:
    """Show archived courses in a separate menu."""
    courses = get_user_courses(db, user_id, archived=True)

    if not courses:
        text = "🗃 *Archived Courses*\n\nArchive is empty."
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Back to Grades", callback_data="moodle:grades")]]
        )
    else:
        per_page = 8
        total_pages = (len(courses) + per_page - 1) // per_page
        text = f"🗃 *Archived Courses*\n\n*Total Courses:* {len(courses)}\n\n"
        text += "Select a course to view saved grades."
        if total_pages > 1:
            text += f"\n\n*Page {page + 1} of {total_pages}*"
        keyboard = get_archived_courses_keyboard(courses, page=page, per_page=per_page)

    try:
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
