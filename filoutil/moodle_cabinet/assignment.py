"""Read the student's assignment summary without requesting attached files."""

import logging
import re
import time
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from filoutil.moodle_cabinet.request_logger import log_moodle_request
from filoutil.utils.http import create_async_client

logger = logging.getLogger(__name__)
LMS_BASE_URL = "https://lms.astanait.edu.kz"
ASSIGNMENT_PATH = "/mod/assign/view.php"


def readable_text(element) -> str:
    """Keep paragraph and line breaks, excluding Moodle's expand/collapse controls."""
    if element is None:
        return ""
    copy = BeautifulSoup(str(element), "html.parser")
    for control in copy.select(".expandsummaryicon, noscript, script, style"):
        control.decompose()
    for br in copy.find_all("br"):
        br.replace_with("\n")
    for block in copy.find_all(["p", "div", "li", "tr", "h1", "h2", "h3", "h4", "pre"]):
        block.insert_before("\n")
        block.append("\n")
    lines = [re.sub(r"[^\S\n]+", " ", line).strip() for line in copy.get_text().splitlines()]
    return "\n".join(line for line in lines if line)


def parse_assignment_page(page_html: str) -> dict:
    """Prefer hidden full plugin contents to Moodle's shortened summaries.

    Scope everything to the current attempt; history can contain other submissions.
    Timing is Moodle's own text, including extensions and its duration rounding.
    """
    soup = BeautifulSoup(page_html, "html.parser")
    submission = soup.select_one(".submissionstatustable")
    if submission is None:
        raise ValueError("Assignment submission summary not found (session may have expired)")
    status_cell = submission.select_one('[class*="submissionstatus"]')
    timing = submission.select_one(".earlysubmission, .latesubmission, .overdue, .timeremaining")
    timing_kind = next(
        (
            kind
            for cls, kind in (
                ("earlysubmission", "early"),
                ("latesubmission", "late"),
                ("overdue", "overdue"),
                ("timeremaining", "remaining"),
            )
            if timing and cls in timing.get("class", [])
        ),
        None,
    )
    details = {
        "status": readable_text(status_cell) or None,
        "timing": timing_kind,
        "timing_text": readable_text(timing) or None,
        "files": [],
        "feedback_files": [],
        "feedback": None,
    }
    # These informational rows lack CSS classes in Moodle's renderer.
    labels = {
        "due date": "due_date",
        "extension due date": "extension_due_date",
        "last modified": "last_modified",
        "срок сдачи": "due_date",
        "последнее изменение": "last_modified",
    }
    for row in submission.select("tr"):
        label, value = row.find("th"), row.find("td")
        key = labels.get(readable_text(label).casefold())
        if key:
            details[key] = readable_text(value)

    for scope, prefix, key in (
        (submission, "submission_file", "files"),
        (soup.select_one(".feedback"), "feedback_file", "feedback_files"),
    ):
        if scope is None:
            continue
        # Full file lists are embedded in hidden HTML when the summary only shows a count.
        containers = scope.select(f'[class*="full_{prefix}_"]') or scope.select(
            f'[class*="summary_{prefix}_"]'
        )
        names = []
        for container in containers:
            for link in container.select('a[href*="pluginfile.php"]'):
                name = readable_text(link)
                if name:
                    names.append(name)
        details[key] = list(dict.fromkeys(names))

    feedback_scope = soup.select_one(".feedback")
    if feedback_scope:
        comments = feedback_scope.select(
            '[class*="full_feedback_comments_"]'
        ) or feedback_scope.select('[class*="summary_feedback_comments_"]')
        details["feedback"] = "\n\n".join(filter(None, map(readable_text, comments))) or None
    return details


async def fetch_assignment_details(
    moodle_session: str,
    course_module_id: int,
    session_refresh_id: int | None = None,
    *,
    client=None,
) -> tuple[bool, dict | None, str | None]:
    """Fetch only view.php. Never follow attachment links or download their contents."""
    if client is None:
        async with create_async_client(timeout=30.0, follow_redirects=True) as owned_client:
            return await fetch_assignment_details(
                moodle_session, course_module_id, session_refresh_id, client=owned_client
            )
    started = time.perf_counter()
    response = None
    success = False
    try:
        response = await client.get(
            f"{LMS_BASE_URL}{ASSIGNMENT_PATH}",
            params={"id": course_module_id},
            cookies={"MoodleSession": moodle_session},
            headers={"Accept-Language": "en-US,en;q=0.5"},
        )
        response.raise_for_status()
        url = urlparse(str(response.url))
        if (
            url.hostname != urlparse(LMS_BASE_URL).hostname
            or url.path != ASSIGNMENT_PATH
            or parse_qs(url.query).get("id") != [str(course_module_id)]
        ):
            return False, None, "Assignment unavailable or Moodle session expired"
        details = parse_assignment_page(response.text)
        success = True
        return True, details, None
    except Exception as exc:
        logger.warning("Unable to retrieve assignment %s: %s", course_module_id, exc)
        return False, None, "Could not retrieve assignment details"
    finally:
        log_moodle_request(
            http_method="GET",
            endpoint_path=ASSIGNMENT_PATH,
            response_status_code=response.status_code if response is not None else 0,
            response_time_ms=(time.perf_counter() - started) * 1000,
            success=success,
            response_size_bytes=len(response.content) if response is not None else None,
            session_refresh_id=session_refresh_id,
        )
