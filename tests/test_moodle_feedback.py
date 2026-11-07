"""Full assignment feedback, change detection, and Telegram views."""

import asyncio
import html
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from filoutil.commands.moodle import grades as views
from filoutil.db.models import Base, MoodleCourse, MoodleGrade, User
from filoutil.db.moodle_courses import upsert_grade
from filoutil.moodle_cabinet import assignment, course_watcher
from filoutil.utils.text import split_message


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(User(id=1, telegram_id=123))
        session.add(User(id=2, telegram_id=456))
        session.add(MoodleCourse(user_id=1, course_id=11, course_name="Course <&>*_"))
        session.commit()
        yield session
    engine.dispose()


@pytest.fixture
def assignment_html():
    # Matches Moodle's renderer: full contents are hidden alongside shortened summaries.
    return """<div class="submissionstatustable"><table>
      <tr><th>Submission status</th><td class="submissionstatussubmitted">Submitted for grading</td></tr>
      <tr><th>Time remaining</th><td class="latesubmission">Assignment was submitted 2 days 3 hours late</td></tr>
      <tr><th>Due date</th><td>Monday, 5 October 2026</td></tr>
      <tr><th>Extension due date</th><td>Tuesday, 6 October 2026</td></tr>
      <tr><th>Last modified</th><td>Thursday, 8 October 2026</td></tr>
      <tr><th>File submissions</th><td>
        <div class="plugincontentsummary summary_submission_file_91">2 files</div>
        <div class="hidefull full_submission_file_91">
          <a class="expandsummaryicon">Collapse</a>
          <a href="/pluginfile.php/1/assignsubmission_file/1/report.pdf">report.pdf</a>
          <a href="/pluginfile.php/1/assignsubmission_file/1/code.py">code.py</a>
        </div></td></tr></table></div>
      <div class="feedback"><div class="feedbacktable"><table>
        <tr><th>Feedback comments</th><td>
          <div class="plugincontentsummary summary_feedback_comments_81">Short...</div>
          <div class="hidefull full_feedback_comments_81">
            <a class="expandsummaryicon">Collapse</a>
            <p>Full <b>feedback</b> &amp; details.</p><p>Second paragraph.<br>Last line.</p>
          </div></td></tr>
        <tr><th>Feedback files</th><td><div class="summary_feedback_file_81">
          <a href="/pluginfile.php/1/assignfeedback_file/1/review.pdf">review.pdf</a>
        </div></td></tr></table></div></div>
      <div class="submissionhistory"><div class="full_submission_file_70">
        <a href="/pluginfile.php/1/assignsubmission_file/1/old.pdf">old.pdf</a></div>
        <div class="full_feedback_comments_70">Old comment</div></div>"""


def seed_grade(db, feedback="Old feedback", raw=80.0):
    return upsert_grade(
        db,
        1,
        11,
        {
            "id": 21,
            "itemname": "Assignment <&>*_",
            "itemmodule": "assign",
            "coursemoduleid": 31,
            "graderaw": raw,
            "gradeformatted": str(raw) if raw is not None else None,
            "feedback": feedback,
        },
    )


def payload(feedback, raw=80.0):
    return {
        "id": 21,
        "itemname": "Assignment <&>*_",
        "itemmodule": "assign",
        "coursemoduleid": 31,
        "graderaw": raw,
        "gradeformatted": str(raw) if raw is not None else None,
        "feedback": feedback,
    }


def test_full_feedback_files_and_moodle_timing(assignment_html):
    details = assignment.parse_assignment_page(assignment_html)
    assert details["feedback"] == "Full feedback & details.\nSecond paragraph.\nLast line."
    assert details["files"] == ["report.pdf", "code.py"]
    assert details["feedback_files"] == ["review.pdf"]
    assert details["timing"] == "late"
    assert details["timing_text"] == "Assignment was submitted 2 days 3 hours late"
    assert details["last_modified"] == "Thursday, 8 October 2026"
    assert details["extension_due_date"] == "Tuesday, 6 October 2026"


@pytest.mark.parametrize(
    "css,kind,text",
    [
        ("earlysubmission", "early", "Assignment was submitted 4 hours early"),
        ("overdue", "overdue", "Assignment is overdue by 3 hours"),
        ("timeremaining", "remaining", "2 days remaining"),
    ],
)
def test_timing_uses_moodle_status_and_duration(css, kind, text):
    details = assignment.parse_assignment_page(
        f'<div class="submissionstatustable"><td class="{css}">{text}</td></div>'
    )
    assert details["timing"] == kind
    assert details["timing_text"] == text
    assert details["feedback"] is None


def test_unshortened_summary_without_hidden_full():
    details = assignment.parse_assignment_page(
        """<div class="submissionstatustable">
      <td class="submissionstatusdraft">Draft (not submitted)</td>
      <div class="summary_submission_file_1"><a href="/pluginfile.php/1">a.pdf</a></div></div>
      <div class="feedback"><div class="summary_feedback_comments_1"><p>Complete comment</p></div></div>"""
    )
    assert details["feedback"] == "Complete comment"
    assert details["files"] == ["a.pdf"]
    assert details["status"] == "Draft (not submitted)"
    assert details["timing"] is None


@pytest.mark.parametrize("new_feedback", ["New complete feedback", None, ""])
def test_feedback_changes_notify_once_with_unchanged_grade(db, new_feedback):
    seed_grade(db)
    data = payload(new_feedback)
    changes = course_watcher.detect_grade_changes(db, 1, 11, [data])
    assert len(changes) == 1
    assert changes[0]["feedback_changed"] is True
    assert changes[0]["old_grade"] == changes[0]["new_grade"] == "80.0"
    upsert_grade(db, 1, 11, data)
    assert course_watcher.detect_grade_changes(db, 1, 11, [data]) == []


def test_unchanged_or_unavailable_feedback_does_not_notify(db):
    seed_grade(db)
    data = payload("Old feedback")
    assert course_watcher.detect_grade_changes(db, 1, 11, [data]) == []
    data.pop("feedback")
    assert course_watcher.detect_grade_changes(db, 1, 11, [data]) == []
    upsert_grade(db, 1, 11, data)
    assert db.query(MoodleGrade).one().feedback == "Old feedback"


def test_feedback_without_a_grade_and_grade_removal(db):
    seed_grade(db, feedback=None, raw=None)
    data = payload("Feedback before grading", raw=None)
    assert len(course_watcher.detect_grade_changes(db, 1, 11, [data])) == 1
    upsert_grade(db, 1, 11, data)
    assert course_watcher.detect_grade_changes(db, 1, 11, [data]) == []
    seed_grade(db)
    removed = payload("Old feedback", raw=None)
    assert len(course_watcher.detect_grade_changes(db, 1, 11, [removed])) == 1
    upsert_grade(db, 1, 11, removed)
    assert course_watcher.detect_grade_changes(db, 1, 11, [removed]) == []


def test_saved_metadata_and_feedback_clear(db):
    seed_grade(db)
    data = payload(None)
    data["submission_details"] = {"timing": "early", "files": ["answer.pdf"]}
    upsert_grade(db, 1, 11, data)
    db.expire_all()
    saved = db.query(MoodleGrade).one()
    assert saved.feedback is None
    assert saved.course_module_id == 31
    assert saved.submission_details == data["submission_details"]


@pytest.mark.asyncio
async def test_retrieval_requests_only_assignment_html(monkeypatch, assignment_html):
    requested = []

    def respond(request):
        requested.append(request.url.path)
        return httpx.Response(200, text=assignment_html)

    monkeypatch.setattr(assignment, "log_moodle_request", MagicMock())
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        ok, details, error = await assignment.fetch_assignment_details("cookie", 31, client=client)
    assert ok and error is None
    assert requested == ["/mod/assign/view.php"]
    assert details["files"] == ["report.pdf", "code.py"]


@pytest.mark.asyncio
@pytest.mark.parametrize("redirect", [True, False])
async def test_expired_session_is_failure(monkeypatch, redirect):
    def respond(request):
        if redirect and request.url.path != "/login/index.php":
            return httpx.Response(302, headers={"location": "/login/index.php"})
        return httpx.Response(200, text="<form>Login</form>")

    monkeypatch.setattr(assignment, "log_moodle_request", MagicMock())
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), follow_redirects=True
    ) as client:
        ok, details, error = await assignment.fetch_assignment_details("expired", 31, client=client)
    assert not ok and details is None and error


@pytest.mark.asyncio
async def test_gradebook_enrichment_and_failure_preserves_full_feedback(
    monkeypatch, assignment_html, tmp_path
):
    gradebook = """<table class="user-grade"><tbody><tr><th id="row_21_1">
      <a class="gradeitemheader" href="/mod/assign/view.php?id=31">Assignment</a></th>
      <td class="column-grade">80</td><td class="column-feedback">Short...</td></tr></tbody></table>"""
    fail = False

    def respond(request):
        return httpx.Response(
            200,
            text=(
                gradebook
                if request.url.path.startswith("/grade/")
                else ("Login" if fail else assignment_html)
            ),
        )

    monkeypatch.setattr(
        course_watcher,
        "create_async_client",
        lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    monkeypatch.setattr(course_watcher, "log_moodle_request", MagicMock())
    monkeypatch.setattr(assignment, "log_moodle_request", MagicMock())
    monkeypatch.chdir(tmp_path)
    ok, items, error = await course_watcher.fetch_course_grades("cookie", 11)
    assert ok and error is None
    assert items[0]["feedback"].startswith("Full feedback")
    assert items[0]["coursemoduleid"] == 31
    fail = True
    ok, items, error = await course_watcher.fetch_course_grades("cookie", 11)
    assert ok and "feedback" not in items[0]
    assert "submission_details" not in items[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True])
async def test_long_notifications_include_all_feedback_and_escape_markup(monkeypatch, batch):
    monkeypatch.setattr(course_watcher.asyncio, "sleep", AsyncMock())
    course = MoodleCourse(course_id=11, course_name="<&>_*Course")
    feedback = "<b>not markup</b> & *_ 😀\n" * 400
    data = payload(feedback)
    change = {
        "type": "updated",
        "feedback_changed": True,
        "old_grade": "80",
        "new_grade": "80",
        "grade_data": data,
    }
    app = MagicMock()
    app.bot.send_message = AsyncMock()
    if batch:
        changes = [
            (course, {**change, "grade_data": {**data, "feedback": f"Feedback {i}\n{feedback}"}})
            for i in range(26)
        ]
        assert await course_watcher.send_batch_grade_notification(app, 123, changes)
    else:
        assert await course_watcher.send_grade_notification(app, 123, course, change)
    messages = [call.kwargs for call in app.bot.send_message.await_args_list]
    combined = "".join(html.unescape(m["text"]) for m in messages)
    assert feedback in combined
    if batch:
        assert all(f"Feedback {i}\n{feedback}" in combined for i in range(26))
    for message in messages:
        assert message["parse_mode"] == "HTML"
        assert len(html.unescape(message["text"]).encode("utf-16-le")) // 2 <= 3500
        assert "<b>not markup</b>" not in message["text"]
    assert messages[-1]["reply_markup"]


def test_split_message_is_lossless_with_long_line_and_emoji():
    text = "😀" * 5000 + "\n<&>" * 3000
    pages = split_message(text)
    assert "".join(pages) == text
    assert all(len(p.encode("utf-16-le")) // 2 <= 3500 for p in pages)


def test_gradebook_has_short_feedback_and_ungraded_assignments(db):
    grade = seed_grade(db, "Feedback " * 100, raw=None)
    text = views.format_gradebook("Course", [grade])
    assert "Assignment" in text and "💬 Feedback" in text and "…" in text
    assert grade.feedback not in text
    assert len(text) < 300


@pytest.mark.asyncio
async def test_gradebook_buttons_pagination_and_detail_return(db):
    for i in range(12):
        upsert_grade(
            db, 1, 11, {**payload("Comment"), "id": 100 + i, "itemname": f"Assignment {i:02}"}
        )
    query = MagicMock()
    query.edit_message_text = AsyncMock()
    await views.show_gradebook(db, 1, 11, query, MagicMock(), page=1)
    result = query.edit_message_text.call_args.kwargs
    assert "Page 2 of 2" in query.edit_message_text.call_args.args[0]
    callbacks = [b.callback_data for row in result["reply_markup"].inline_keyboard for b in row]
    assert "moodle:grades:item:11:108:0:1" in callbacks
    await views.show_assignment_details(db, 1, 11, 108, query, MagicMock(), gradebook_page=1)
    callbacks = [
        b.callback_data
        for row in query.edit_message_text.call_args.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert "moodle:grades:course:11:1" in callbacks


@pytest.mark.asyncio
async def test_details_are_scoped_to_requesting_user(db, monkeypatch):
    seed_grade(db)
    retrieve = AsyncMock()
    monkeypatch.setattr(views, "fetch_assignment_details", retrieve)
    query = MagicMock()
    query.edit_message_text = AsyncMock()
    await views.show_assignment_details(db, 2, 11, 21, query, MagicMock())
    assert query.edit_message_text.call_args.args[0] == "❌ Assignment not found."
    retrieve.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_details_do_not_consume_pending_feedback_notification(db, monkeypatch):
    seed_grade(db)
    monkeypatch.setattr(
        views,
        "get_active_sessions_for_user",
        lambda *args: [MagicMock(moodleSession="cookie", id=9)],
    )
    monkeypatch.setattr(
        views,
        "fetch_assignment_details",
        AsyncMock(
            return_value=(
                True,
                {
                    "feedback": "New live feedback",
                    "status": "Submitted",
                    "timing_text": "2 hours early",
                    "files": ["answer.pdf"],
                    "feedback_files": [],
                },
                None,
            )
        ),
    )
    query = MagicMock()
    query.edit_message_text = AsyncMock()
    await views.show_assignment_details(db, 1, 11, 21, query, MagicMock())
    text = html.unescape(query.edit_message_text.call_args.args[0])
    assert "New live feedback" in text and "2 hours early" in text and "answer.pdf" in text
    assert "Saved submission details" not in text
    assert db.query(MoodleGrade).one().feedback == "Old feedback"
    assert len(course_watcher.detect_grade_changes(db, 1, 11, [payload("New live feedback")])) == 1


def test_grade_metadata_migration_is_idempotent(monkeypatch):
    from sqlalchemy import inspect, text

    from filoutil.db import postgres

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE moodle_grades DROP COLUMN course_module_id"))
        conn.execute(text("ALTER TABLE moodle_grades DROP COLUMN submission_details"))
    monkeypatch.setattr(postgres, "engine", engine)
    postgres.run_migrations()
    postgres.run_migrations()
    columns = {c["name"] for c in inspect(engine).get_columns("moodle_grades")}
    assert {"course_module_id", "submission_details"} <= columns
    engine.dispose()


@pytest.mark.asyncio
async def test_detail_pagination_keeps_complete_feedback_and_zero_grade(db):
    feedback = "<b>Not markup</b> & 😀" * 400
    seed_grade(db, feedback, raw=0.0)
    query = MagicMock()
    query.edit_message_text = AsyncMock()
    for page in range(20):
        await views.show_assignment_details(db, 1, 11, 21, query, MagicMock(), page=page)
        keyboard = query.edit_message_text.call_args.kwargs["reply_markup"].inline_keyboard
        if not any(b.text == "Next ➡️" for row in keyboard for b in row):
            break
    pages = [html.unescape(call.args[0]) for call in query.edit_message_text.call_args_list]
    assert "Grade: 0.0" in pages[0]
    # Page counters are separate display text, outside the lossless payload.
    combined = "".join(text.rsplit("\n\nPage ", 1)[0] for text in pages)
    assert feedback in combined
    assert all(len(text.encode("utf-16-le")) // 2 <= 4096 for text in pages)


@pytest.mark.asyncio
async def test_gradebook_pagination_handles_long_unicode_names(db):
    for i in range(20):
        data = {**payload("😀" * 200), "id": 100 + i, "itemname": "😀" * 250 + str(i)}
        upsert_grade(db, 1, 11, data)
    query = MagicMock()
    query.edit_message_text = AsyncMock()
    for page in range(20):
        await views.show_gradebook(db, 1, 11, query, MagicMock(), page=page)
        keyboard = query.edit_message_text.call_args.kwargs["reply_markup"].inline_keyboard
        if not any(b.text == "Next ➡️" for row in keyboard for b in row):
            break
    assert all(
        len(call.args[0].encode("utf-16-le")) // 2 <= 4096
        for call in query.edit_message_text.call_args_list
    )


@pytest.mark.asyncio
async def test_archived_details_use_saved_data_without_network(db, monkeypatch):
    grade = seed_grade(db)
    grade.submission_details = {
        "status": "Submitted",
        "timing_text": "1 hour late",
        "files": ["a.pdf"],
    }
    db.query(MoodleCourse).one().archived = True
    db.commit()
    retrieve = AsyncMock()
    monkeypatch.setattr(views, "fetch_assignment_details", retrieve)
    query = MagicMock()
    query.edit_message_text = AsyncMock()
    await views.show_assignment_details(db, 1, 11, 21, query, MagicMock())
    retrieve.assert_not_awaited()
    text = html.unescape(query.edit_message_text.call_args.args[0])
    assert "Saved submission details" in text and "1 hour late" in text and "a.pdf" in text


@pytest.mark.asyncio
async def test_hidden_zero_totals_clear_stale_error(monkeypatch, db, tmp_path):
    for item_id, name in [(41, "Register(not to edit) total"), (42, "Course total")]:
        upsert_grade(db, 1, 11, {"id": item_id, "itemname": name, "gradeformatted": "Error"})
    gradebook = """<table class="user-grade"><tbody>
      <tr><th id="row_41_1"><span class="gradeitemheader">Register(not to edit) total</span></th>
      <td class="column-grade">0.00</td></tr>
      <tr><th id="row_42_1"><span class="gradeitemheader">Course total</span></th>
      <td class="column-grade">0.00</td></tr></tbody></table>"""
    monkeypatch.setattr(
        course_watcher,
        "create_async_client",
        lambda **kwargs: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text=gradebook))
        ),
    )
    monkeypatch.setattr(course_watcher, "log_moodle_request", MagicMock())
    monkeypatch.chdir(tmp_path)

    ok, items, error = await course_watcher.fetch_course_grades("cookie", 11)
    assert ok and error is None and len(items) == 2
    assert course_watcher.detect_grade_changes(db, 1, 11, items) == []
    course_watcher.sync_grades_for_course(db, 1, 11, items)

    stored = db.query(MoodleGrade).filter_by(user_id=1, course_id=11).all()
    assert [(g.grade_raw, g.grade_formatted) for g in stored] == [(None, None), (None, None)]
    assert "Error" not in views.format_gradebook("Course", stored)


def test_sync_deletes_grade_items_removed_from_gradebook(db):
    seed_grade(db)
    upsert_grade(db, 1, 11, {"id": 22, "itemname": "Deleted quiz", "gradeformatted": "50.00"})
    upsert_grade(db, 1, 12, {"id": 23, "itemname": "Other course", "gradeformatted": "70.00"})

    course_watcher.sync_grades_for_course(db, 1, 11, [payload("Old feedback")])

    remaining = db.query(MoodleGrade).order_by(MoodleGrade.grade_item_id).all()
    assert [(g.course_id, g.grade_item_id) for g in remaining] == [(11, 21), (12, 23)]
