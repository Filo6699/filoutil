"""Approved notification designs and neutral handling of incomparable grades."""

import html
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from filoutil.db.models import MoodleCourse
from filoutil.moodle_cabinet import course_watcher as watcher


@pytest.fixture
def course():
    return MoodleCourse(course_id=11, course_name="Entrepreneurship | Dina Zhumaldinova")


def change(old="75", new="90", *, item="Assignment 3", **extra):
    return {
        "type": "updated",
        "old_grade": old,
        "new_grade": new,
        "grade_data": {"id": 21, "itemname": item},
        **extra,
    }


@pytest.mark.parametrize(
    "old,new,expected",
    [
        ("86.67", "87.50", "🟢 ↑ 86.67 → 87.50 · +0.83 points"),
        ("87.50", "85.00", "🔴 ↓ 87.50 → 85.00 · −2.50 points"),
    ],
)
def test_attendance_uses_dedicated_compact_design(course, old, new, expected):
    message = watcher.format_grade_notification(course, change(old, new, item="Attendance"))
    assert message == (
        "📋 Attendance Update\n" "Entrepreneurship | Dina Zhumaldinova\n" f"{expected}"
    )
    assert "Assignment:" not in message
    assert "%" not in message


@pytest.mark.parametrize(
    "old,new,extra,title,grade_line",
    [
        ("75", "90", {}, "📊 Grade Update", "🟢 ↑ 75 → 90 · +15 points"),
        ("90", "82", {}, "📊 Grade Update", "🔴 ↓ 90 → 82 · −8 points"),
        (None, "88", {"type": "new"}, "🆕 New Grade", "Grade: 88"),
        (None, "88", {}, "🆕 New Grade", "Grade: 88"),
        ("-", "0", {}, "🆕 New Grade", "Grade: 0"),
        ("82", None, {}, "⚪ Grade Removed", "82 → No grade"),
        ("82", "-", {}, "⚪ Grade Removed", "82 → No grade"),
        ("0", "1", {}, "📊 Grade Update", "🟢 ↑ 0 → 1 · +1 point"),
        ("1", "0", {}, "📊 Grade Update", "🔴 ↓ 1 → 0 · −1 point"),
    ],
)
def test_grade_designs(course, old, new, extra, title, grade_line):
    message = watcher.format_grade_notification(course, change(old, new, **extra))
    assert message == f"{title}\n{course.course_name}\nAssignment 3\n{grade_line}"


@pytest.mark.parametrize("feedback", ["Add a description of the algorithm.", None])
def test_feedback_only_changes_have_no_direction(course, feedback):
    event = change("90", "90", grade_changed=False, feedback_changed=True)
    event["grade_data"]["feedback"] = feedback
    message = watcher.format_grade_notification(course, event)
    title = "💬 Feedback Updated" if feedback else "💬 Feedback Removed"
    expected = f"{title}\n{course.course_name}\nAssignment 3\nGrade: 90 · unchanged"
    if feedback:
        expected += f"\n\nFeedback:\n{feedback}"
    assert message == expected


def test_grade_and_feedback_changes_both_remain_visible(course):
    event = change(feedback_changed=True)
    event["grade_data"]["feedback"] = "Full comment\nSecond paragraph."
    message = watcher.format_grade_notification(course, event)
    assert message.startswith("📊 Grade Update\n")
    assert "🟢 ↑ 75 → 90 · +15 points" in message
    assert "💬 Feedback Updated" in message
    assert message.endswith("\n\nFeedback:\nFull comment\nSecond paragraph.")


@pytest.mark.parametrize(
    "old,new",
    [("Pass", "Distinction"), ("B", "A"), ("75%", "90%"), ("NaN", "90"), ("80", "Infinity")],
)
def test_incomparable_grades_do_not_claim_improvement(course, old, new):
    message = watcher.format_grade_notification(course, change(old, new))
    assert f"⚪ {old} → {new}" in message
    assert "🟢" not in message and "🔴" not in message
    assert "points" not in message


def test_raw_grades_drive_direction_when_formatted_values_are_labels(course):
    event = change("C", "A", old_grade_raw=75, new_grade_raw=90)
    assert "🟢 ↑ C → A · +15 points" in watcher.format_grade_notification(course, event)


@pytest.mark.parametrize(
    "item,module", [("ATTENDANCE", None), ("Lecture Attendance", None), ("Seminars", "attendance")]
)
def test_attendance_names_and_modules_are_recognized(course, item, module):
    event = change(item=item)
    event["grade_data"]["itemmodule"] = module
    message = watcher.format_grade_notification(course, event)
    assert message.startswith("📋 Attendance Update\n")
    if item != "ATTENDANCE":
        assert f"\n{item}\n" in message


@pytest.mark.parametrize(
    "old,new,feedback,grade_line",
    [
        (None, "90", None, "Grade: 90"),
        ("90", None, None, "90 → No grade"),
        ("90", "90", "Corrected comment", "Grade: 90 · unchanged"),
    ],
)
def test_attendance_keeps_its_identity_for_all_events(course, old, new, feedback, grade_line):
    event = change(old, new, item="Attendance", feedback_changed=bool(feedback))
    event["grade_data"]["feedback"] = feedback
    message = watcher.format_grade_notification(course, event)
    assert message.startswith("📋 Attendance Update\n")
    assert grade_line in message
    if feedback:
        assert "💬 Feedback Updated" in message


def test_detection_preserves_raw_values_for_the_renderer(course, monkeypatch):
    stored = SimpleNamespace(
        grade_item_id=21,
        grade_raw=86.6666667,
        grade_formatted="86.67",
        grade_date_graded=None,
        feedback=None,
    )
    monkeypatch.setattr(watcher, "get_course_grades", lambda *args: [stored])
    data = {
        "id": 21,
        "itemname": "Attendance",
        "graderaw": 87.5,
        "gradeformatted": "87.50",
    }
    events = watcher.detect_grade_changes(MagicMock(), 1, 11, [data])
    assert len(events) == 1
    assert events[0]["old_grade_raw"] == stored.grade_raw
    assert events[0]["new_grade_raw"] == 87.5
    assert events[0]["grade_changed"] is True
    assert "🟢 ↑ 86.67 → 87.50 · +0.83 points" in watcher.format_grade_notification(
        course, events[0]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True])
async def test_transport_uses_same_design_and_preserves_moodle_buttons(course, batch):
    course.course_name = "Course &amp; Instructor <test>"
    event = change("86.67", "87.50", item="Attendance")
    app = MagicMock()
    app.bot.send_message = AsyncMock()
    if batch:
        result = await watcher.send_batch_grade_notification(app, 123, [(course, event)])
    else:
        result = await watcher.send_grade_notification(app, 123, course, event)
    assert result is True
    sent = app.bot.send_message.await_args.kwargs
    assert watcher.format_grade_notification(course, event) in html.unescape(sent["text"])
    assert "Course &amp; Instructor &lt;test&gt;" in sent["text"]
    assert sent["parse_mode"] == "HTML"
    buttons = [button for row in sent["reply_markup"].inline_keyboard for button in row]
    assert any(button.text == "📊 View Full Grades" for button in buttons)
    if not batch:
        assert any(
            button.text == "🔗 View in Moodle"
            and button.url.endswith("/grade/report/user/index.php?id=11")
            for button in buttons
        )


def test_valid_grade_date_is_preserved_and_invalid_date_is_ignored(course):
    event = change()
    event["grade_data"]["gradedategraded"] = 1
    assert "Date graded:" in watcher.format_grade_notification(course, event)
    event["grade_data"]["gradedategraded"] = "invalid"
    assert "Date graded:" not in watcher.format_grade_notification(course, event)
