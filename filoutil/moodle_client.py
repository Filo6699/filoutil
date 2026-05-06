from __future__ import annotations

import logging
import mimetypes
import re
import time
from html import unescape
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from filoutil.moodle_cabinet.request_logger import log_moodle_request
from filoutil.utils.http import create_async_client

logger = logging.getLogger(__name__)

LMS_BASE_URL = "https://lms.astanait.edu.kz"
LMS_ENDPOINT = "/lib/ajax/service.php"
LMS_HOST = urlparse(LMS_BASE_URL).hostname


def _is_allowed_moodle_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname == LMS_HOST


def _normalize_moodle_url(raw_url: str) -> str:
    return urljoin(f"{LMS_BASE_URL}/", raw_url)


async def _call_moodle_ajax(
    sesskey: str,
    moodle_session: str,
    methodname: str,
    args: dict[str, Any],
    *,
    session_refresh_id: int | None = None,
) -> tuple[bool, Any | None, str | None]:
    url = f"{LMS_BASE_URL}{LMS_ENDPOINT}"
    params = {"sesskey": sesskey, "info": methodname}
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": LMS_BASE_URL,
        "Referer": f"{LMS_BASE_URL}/",
    }
    cookies = {"MoodleSession": moodle_session}
    payload = [{"index": 0, "methodname": methodname, "args": args}]

    start_time = time.perf_counter()
    request_size_bytes = None
    response_size_bytes = None
    response_status_code = None
    success = False

    try:
        async with create_async_client(timeout=30.0, follow_redirects=True) as client:
            import json

            request_body = json.dumps(payload)
            request_size_bytes = len(request_body.encode("utf-8"))

            response = await client.post(
                url, params=params, headers=headers, cookies=cookies, json=payload
            )
            response_status_code = response.status_code
            response_size_bytes = len(response.content) if response.content else None

            if response.status_code != 200:
                error_text = response.text[:200] if response.text else "No response body"
                logger.error("Moodle returned status %s: %s", response.status_code, error_text)
                return False, None, f"HTTP {response.status_code}: {error_text}"

            try:
                data = response.json()
                if isinstance(data, list) and data:
                    item = data[0]
                    if "error" in item:
                        error_val = item["error"]
                        if error_val is False or error_val is None:
                            success = True
                            return True, item.get("data"), None
                        if error_val is True:
                            details = [str(item.get("message") or "").strip()]
                            for key in ("exception", "errorcode", "debuginfo"):
                                value = item.get(key)
                                if value:
                                    details.append(str(value).strip())
                            message = "; ".join(part for part in details if part) or "Unknown error"
                            return False, None, f"Moodle error: {message}"
                        if isinstance(error_val, dict):
                            details = [str(error_val.get("message") or "").strip()]
                            for key in ("exception", "errorcode", "debuginfo"):
                                value = error_val.get(key)
                                if value:
                                    details.append(str(value).strip())
                            message = "; ".join(part for part in details if part) or str(error_val)
                            return False, None, f"Moodle error: {message}"
                        return False, None, f"Moodle error: {error_val}"
                return False, None, "Invalid response format"
            except Exception as exc:
                logger.warning(
                    "Failed to parse Moodle AJAX response for %s: %s, response text: %s",
                    methodname,
                    exc,
                    response.text[:500],
                )
                return False, None, f"Failed to parse response: {exc}"
    except httpx.RequestError as exc:
        response_status_code = 0
        return False, None, f"Request failed: {exc}"
    except Exception as exc:
        response_status_code = 0
        return False, None, f"Unexpected error: {exc}"
    finally:
        response_time_ms = (time.perf_counter() - start_time) * 1000
        log_moodle_request(
            http_method="POST",
            endpoint_path=LMS_ENDPOINT,
            response_status_code=response_status_code or 0,
            response_time_ms=response_time_ms,
            success=success,
            api_method_name=methodname,
            request_size_bytes=request_size_bytes,
            response_size_bytes=response_size_bytes,
            session_refresh_id=session_refresh_id,
        )


def _extract_assignment_file_links(soup: BeautifulSoup) -> list[dict[str, str | None]]:
    files: list[dict[str, str | None]] = []
    seen_urls: set[str] = set()
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if not href or "pluginfile.php" not in href:
            continue
        absolute_url = _normalize_moodle_url(href)
        if not _is_allowed_moodle_url(absolute_url) or absolute_url in seen_urls:
            continue
        seen_urls.add(absolute_url)
        filename = anchor.get_text(" ", strip=True) or absolute_url.rsplit("/", 1)[-1]
        files.append(
            {
                "name": unescape(filename),
                "url": absolute_url,
                "path": urlparse(absolute_url).path,
                "section": anchor.find_parent(["li", "div", "p", "td"]).get_text(" ", strip=True)
                if anchor.find_parent(["li", "div", "p", "td"])
                else None,
            }
        )
    return files


def _extract_assignment_metadata(soup: BeautifulSoup, assignment_url: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "assignment_url": assignment_url,
        "title": None,
        "course_name": None,
        "intro": None,
        "dates": {},
        "submission_status": {},
        "files": _extract_assignment_file_links(soup),
    }

    title = soup.select_one(".activity-header h1, #page-header .page-header-headings h1, h1")
    if title:
        result["title"] = unescape(title.get_text(" ", strip=True))

    breadcrumb_links = [
        (unescape(x.get_text(" ", strip=True)), x.get("href", ""))
        for x in soup.select(".breadcrumb a, nav[aria-label='breadcrumb'] a")
        if x.get_text(strip=True)
    ]
    for text, href in breadcrumb_links:
        if "/course/view.php" in href:
            result["course_name"] = text
    if breadcrumb_links and not result["title"]:
        result["title"] = breadcrumb_links[-1][0]
    if breadcrumb_links and not result["course_name"]:
        result["course_name"] = breadcrumb_links[-2][0] if len(breadcrumb_links) >= 2 else breadcrumb_links[-1][0]

    intro = soup.select_one("div[role='main'] .activity-description, div[role='main'] .box.py-3.generalbox, #intro")
    if intro:
        result["intro"] = unescape(intro.get_text("\n", strip=True))

    date_rows = soup.select("table td")
    current_label: str | None = None
    for cell in date_rows:
        text = unescape(cell.get_text(" ", strip=True))
        lowered = text.lower()
        if lowered in {
            "due date",
            "cut-off date",
            "allow submissions from",
            "remind me to grade by",
            "last modified",
            "submitted on",
            "grading status",
            "submission status",
        }:
            current_label = lowered
            continue
        if current_label:
            if current_label in {"grading status", "submission status"}:
                result["submission_status"][current_label.replace(" ", "_")] = text
            else:
                result["dates"][current_label.replace(" ", "_").replace("-", "_")] = text
            current_label = None

    for row in soup.select("tr"):
        headers = row.select("th, td")
        if len(headers) < 2:
            continue
        label = unescape(headers[0].get_text(" ", strip=True)).lower()
        value = unescape(headers[1].get_text(" ", strip=True))
        if label in {
            "due date",
            "cut-off date",
            "allow submissions from",
            "remind me to grade by",
            "grading status",
            "submission status",
            "submission comments",
        }:
            target = result["submission_status"] if "status" in label or "comments" in label else result["dates"]
            target[label.replace(" ", "_").replace("-", "_")] = value

    return result


def _normalize_calendar_event(event: dict[str, Any]) -> dict[str, Any]:
    course = event.get("course") or {}
    course_id = course.get("id") or event.get("courseid")
    url = event.get("url")
    absolute_url = _normalize_moodle_url(url) if url else None
    module = event.get("modulename") or event.get("activityname") or event.get("type")
    return {
        "id": event.get("id"),
        "name": event.get("name"),
        "description": event.get("description"),
        "course_id": course_id,
        "course_name": course.get("fullname") or course.get("shortname") or event.get("coursefullname"),
        "timesort": event.get("timesort") or event.get("timestart"),
        "timestart": event.get("timestart") or event.get("timesort"),
        "timeduration": event.get("timeduration"),
        "formattedtime": event.get("formattedtime"),
        "formattedtimeuntil": event.get("formattedtimeuntil"),
        "url": absolute_url,
        "action": event.get("action"),
        "module": module,
        "eventtype": event.get("eventtype"),
        "categoryid": event.get("categoryid"),
    }


async def fetch_calendar_events(
    sesskey: str,
    moodle_session: str,
    *,
    time_from: int,
    time_to: int,
    limit: int = 100,
    session_refresh_id: int | None = None,
) -> tuple[bool, list[dict[str, Any]] | None, str | None]:
    success, data, error = await _call_moodle_ajax(
        sesskey,
        moodle_session,
        "core_calendar_get_action_events_by_timesort",
        {
            "timesortfrom": int(time_from),
            "timesortto": int(time_to),
            "aftereventid": 0,
            "limitnum": int(limit),
        },
        session_refresh_id=session_refresh_id,
    )
    if success and isinstance(data, dict):
        events = data.get("events") or data.get("actionevents") or []
        return True, [_normalize_calendar_event(event) for event in events], None
    return success, None, error or "Unexpected calendar response"


async def fetch_assignment_page(
    moodle_session: str, assignment_url: str
) -> tuple[bool, dict[str, Any] | None, str | None]:
    absolute_url = _normalize_moodle_url(assignment_url)
    if not _is_allowed_moodle_url(absolute_url):
        return False, None, "Assignment URL must point to the configured Moodle host."

    try:
        async with create_async_client(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(absolute_url, cookies={"MoodleSession": moodle_session})
            if response.status_code != 200:
                error_text = response.text[:200] if response.text else "No response body"
                return False, None, f"HTTP {response.status_code}: {error_text}"

            soup = BeautifulSoup(response.text, "html.parser")
            return True, _extract_assignment_metadata(soup, absolute_url), None
    except httpx.RequestError as exc:
        return False, None, f"Request failed: {exc}"
    except Exception as exc:
        return False, None, f"Unexpected error: {exc}"


async def download_moodle_file(
    moodle_session: str,
    file_url: str,
    *,
    max_bytes: int | None = 10 * 1024 * 1024,
) -> tuple[bool, dict[str, Any] | None, str | None]:
    absolute_url = _normalize_moodle_url(file_url)
    if not _is_allowed_moodle_url(absolute_url) or "pluginfile.php" not in absolute_url:
        return False, None, "File URL must be a Moodle pluginfile URL on the configured host."

    try:
        async with create_async_client(timeout=60.0, follow_redirects=True) as client:
            response = await client.get(absolute_url, cookies={"MoodleSession": moodle_session})
            if response.status_code != 200:
                error_text = response.text[:200] if response.text else "No response body"
                return False, None, f"HTTP {response.status_code}: {error_text}"

            content = response.content or b""
            if max_bytes is not None and len(content) > max_bytes:
                return False, None, f"File exceeds max_bytes limit ({len(content)} > {max_bytes})."

            parsed = urlparse(str(response.url))
            filename = parsed.path.rsplit("/", 1)[-1] or "download.bin"
            content_type = response.headers.get("content-type") or mimetypes.guess_type(filename)[0] or "application/octet-stream"
            disposition = response.headers.get("content-disposition", "")
            filename_match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', disposition)
            if filename_match:
                filename = filename_match.group(1)

            return (
                True,
                {
                    "url": absolute_url,
                    "filename": filename,
                    "content_type": content_type,
                    "size_bytes": len(content),
                    "content_bytes": content,
                },
                None,
            )
    except httpx.RequestError as exc:
        return False, None, f"Request failed: {exc}"
    except Exception as exc:
        return False, None, f"Unexpected error: {exc}"


def _extract_download_filename(response: httpx.Response) -> str:
    parsed = urlparse(str(response.url))
    filename = parsed.path.rsplit("/", 1)[-1] or "download.bin"
    disposition = response.headers.get("content-disposition", "")
    filename_match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', disposition)
    if filename_match:
        filename = filename_match.group(1)
    return filename


def _looks_like_moodle_login_response(response: httpx.Response) -> bool:
    parsed = urlparse(str(response.url))
    if parsed.path.startswith("/login/"):
        return True

    content_type = (response.headers.get("content-type") or "").lower()
    return "text/html" in content_type and "pluginfile.php" not in parsed.path


async def get_moodle_file_metadata(
    moodle_session: str,
    file_url: str,
) -> tuple[bool, dict[str, Any] | None, str | None]:
    absolute_url = _normalize_moodle_url(file_url)
    if not _is_allowed_moodle_url(absolute_url) or "pluginfile.php" not in absolute_url:
        return False, None, "File URL must be a Moodle pluginfile URL on the configured host."

    try:
        async with create_async_client(timeout=60.0, follow_redirects=True) as client:
            async with client.stream("GET", absolute_url, cookies={"MoodleSession": moodle_session}) as response:
                if response.status_code != 200:
                    error_text = await response.aread()
                    preview = error_text[:200].decode("utf-8", errors="replace") if error_text else "No response body"
                    return False, None, f"HTTP {response.status_code}: {preview}"

                if _looks_like_moodle_login_response(response):
                    return False, None, "Moodle redirected the file download to the login page."

                filename = _extract_download_filename(response)
                content_type = response.headers.get("content-type") or mimetypes.guess_type(filename)[0] or "application/octet-stream"
                size_header = response.headers.get("content-length")
                size_bytes = int(size_header) if size_header and size_header.isdigit() else None

                return (
                    True,
                    {
                        "url": absolute_url,
                        "filename": filename,
                        "content_type": content_type,
                        "size_bytes": size_bytes,
                    },
                    None,
                )
    except httpx.RequestError as exc:
        return False, None, f"Request failed: {exc}"
    except Exception as exc:
        return False, None, f"Unexpected error: {exc}"


async def fetch_notifications(
    sesskey: str,
    moodle_session: str,
    useridto: int,
    *,
    limit: int = 1,
    offset: int = 0,
    session_refresh_id: int | None = None,
) -> tuple[bool, list[dict[str, Any]] | None, str | None]:
    url = f"{LMS_BASE_URL}{LMS_ENDPOINT}"
    params = {"sesskey": sesskey, "info": "message_popup_get_popup_notifications"}
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": LMS_BASE_URL,
        "Referer": f"{LMS_BASE_URL}/",
    }
    cookies = {"MoodleSession": moodle_session}
    payload = [
        {
            "index": 0,
            "methodname": "message_popup_get_popup_notifications",
            "args": {"limit": limit, "offset": offset, "useridto": str(useridto)},
        }
    ]

    start_time = time.perf_counter()
    request_size_bytes = None
    response_size_bytes = None
    response_status_code = None
    success = False

    try:
        async with create_async_client(timeout=30.0, follow_redirects=True) as client:
            import json

            request_body = json.dumps(payload)
            request_size_bytes = len(request_body.encode("utf-8"))

            response = await client.post(
                url, params=params, headers=headers, cookies=cookies, json=payload
            )
            response_status_code = response.status_code
            response_size_bytes = len(response.content) if response.content else None

            if response.status_code != 200:
                error_text = response.text[:200] if response.text else "No response body"
                logger.error("Moodle returned status %s: %s", response.status_code, error_text)
                return False, None, f"HTTP {response.status_code}: {error_text}"

            try:
                data = response.json()
                if isinstance(data, list) and data:
                    item = data[0]
                    if "error" in item:
                        error_val = item["error"]
                        if error_val is False or error_val is None:
                            notifications_data = item.get("data", {})
                            success = True
                            return True, notifications_data.get("notifications", []), None
                        if error_val is True:
                            return False, None, f"Moodle error: {item.get('message', 'Unknown error')}"
                        if isinstance(error_val, dict):
                            return False, None, f"Moodle error: {error_val.get('message', str(error_val))}"
                        return False, None, f"Moodle error: {error_val}"
                return False, None, "Invalid response format"
            except Exception as exc:
                logger.warning(
                    "Failed to parse notifications response JSON: %s, response text: %s",
                    exc,
                    response.text[:500],
                )
                return False, None, f"Failed to parse response: {exc}"
    except httpx.RequestError as exc:
        response_status_code = 0
        return False, None, f"Request failed: {exc}"
    except Exception as exc:
        response_status_code = 0
        return False, None, f"Unexpected error: {exc}"
    finally:
        response_time_ms = (time.perf_counter() - start_time) * 1000
        log_moodle_request(
            http_method="POST",
            endpoint_path=LMS_ENDPOINT,
            response_status_code=response_status_code or 0,
            response_time_ms=response_time_ms,
            success=success,
            api_method_name="message_popup_get_popup_notifications",
            request_size_bytes=request_size_bytes,
            response_size_bytes=response_size_bytes,
            session_refresh_id=session_refresh_id,
        )


async def fetch_moodle_user_id(
    sesskey: str, moodle_session: str
) -> tuple[bool, int | None, str | None]:
    success, test_notifications, error_msg = await fetch_notifications(
        sesskey, moodle_session, 0, limit=1
    )
    if success and test_notifications:
        moodle_user_id = test_notifications[0].get("useridto")
        if moodle_user_id:
            return True, moodle_user_id, None
        return False, None, "No useridto found in notification response"
    return False, None, error_msg or "Failed to fetch notifications"


async def fetch_user_courses(
    sesskey: str,
    moodle_session: str,
    moodle_user_id: int,
    session_refresh_id: int | None = None,
) -> tuple[bool, list[dict[str, Any]] | None, str | None]:
    del moodle_user_id

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
    cookies = {"MoodleSession": moodle_session}
    payload = [
        {
            "index": 0,
            "methodname": "core_course_get_enrolled_courses_by_timeline_classification",
            "args": {"classification": "inprogress", "limit": 0, "offset": 0},
        }
    ]

    start_time = time.perf_counter()
    request_size_bytes = None
    response_size_bytes = None
    response_status_code = None
    success = False

    try:
        async with create_async_client(timeout=30.0, follow_redirects=True) as client:
            import json

            request_body = json.dumps(payload)
            request_size_bytes = len(request_body.encode("utf-8"))

            response = await client.post(
                url, params=params, headers=headers, cookies=cookies, json=payload
            )
            response_status_code = response.status_code
            response_size_bytes = len(response.content) if response.content else None

            if response.status_code != 200:
                error_text = response.text[:200] if response.text else "No response body"
                logger.error("Moodle returned status %s: %s", response.status_code, error_text)
                return False, None, f"HTTP {response.status_code}: {error_text}"

            try:
                data = response.json()
                if isinstance(data, list) and data:
                    item = data[0]
                    if "error" in item:
                        error_val = item["error"]
                        if error_val is False or error_val is None:
                            success = True
                            courses_data = item.get("data", {})
                            return True, courses_data.get("courses", []), None
                        if error_val is True:
                            return False, None, f"Moodle error: {item.get('message', 'Unknown error')}"
                        if isinstance(error_val, dict):
                            return False, None, f"Moodle error: {error_val.get('message', str(error_val))}"
                        return False, None, f"Moodle error: {error_val}"
                return False, None, "Invalid response format"
            except Exception as exc:
                logger.warning(
                    "Failed to parse courses response JSON: %s, response text: %s",
                    exc,
                    response.text[:500],
                )
                return False, None, f"Failed to parse response: {exc}"
    except httpx.RequestError as exc:
        response_status_code = 0
        return False, None, f"Request failed: {exc}"
    except Exception as exc:
        response_status_code = 0
        return False, None, f"Unexpected error: {exc}"
    finally:
        response_time_ms = (time.perf_counter() - start_time) * 1000
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
    moodle_session: str, course_id: int, session_refresh_id: int | None = None
) -> tuple[bool, list[dict[str, Any]] | None, str | None]:
    url = f"{LMS_BASE_URL}/grade/report/user/index.php"
    endpoint_path = "/grade/report/user/index.php"
    params = {"id": course_id}
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": f"{LMS_BASE_URL}/",
    }
    cookies = {"MoodleSession": moodle_session}

    start_time = time.perf_counter()
    request_size_bytes = None
    response_size_bytes = None
    response_status_code = None
    success = False

    try:
        async with create_async_client(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(url, params=params, headers=headers, cookies=cookies)
            response_status_code = response.status_code
            response_size_bytes = len(response.content) if response.content else None

            if response.status_code != 200:
                error_text = response.text[:200] if response.text else "No response body"
                logger.error("Moodle returned status %s: %s", response.status_code, error_text)
                return False, None, f"HTTP {response.status_code}: {error_text}"

            try:
                soup = BeautifulSoup(response.text, "html.parser")
                grade_table = soup.find("table", class_="user-grade")
                if not grade_table:
                    return True, [], None

                tbody = grade_table.find("tbody")
                if not tbody:
                    return True, [], None

                all_ths_with_id = tbody.find_all("th", id=re.compile(r"^row_\d+_\d+$"))
                rows = []
                seen_rows = set()
                for th in all_ths_with_id:
                    row = th.find_parent("tr")
                    if row and id(row) not in seen_rows:
                        rows.append(row)
                        seen_rows.add(id(row))

                grade_items = []
                for row in rows:
                    if "spacer" in row.get("class", []):
                        continue

                    row_th = row.find("th", id=re.compile(r"^row_\d+_\d+$"))
                    if not row_th or not row_th.get("id"):
                        continue

                    match = re.match(r"row_(\d+)_\d+", row_th.get("id"))
                    if not match:
                        continue
                    grade_item_id = int(match.group(1))

                    item_name_elem = row_th.find("span", class_="gradeitemheader") or row_th.find(
                        "a", class_="gradeitemheader"
                    )
                    if not item_name_elem:
                        continue
                    item_name = item_name_elem.get_text(strip=True)

                    item_type_elem = row_th.find("span", class_="text-uppercase")
                    item_type = item_type_elem.get_text(strip=True).lower() if item_type_elem else None

                    item_module = None
                    item_link = row_th.find("a", class_="gradeitemheader")
                    if item_link and item_link.get("href"):
                        mod_match = re.search(r"/mod/(\w+)/", item_link.get("href"))
                        if mod_match:
                            item_module = mod_match.group(1)

                    grade_cell = row.find("td", class_=re.compile(r"column-grade\b"))
                    grade_text = grade_cell.get_text(strip=True) if grade_cell else "-"
                    if grade_text and "Grade analysis" in grade_text:
                        grade_text = grade_text.replace("Grade analysis", "").strip()
                    grade_raw = None
                    grade_formatted = grade_text if grade_text != "-" else None
                    if grade_formatted:
                        try:
                            grade_raw = float(grade_formatted)
                        except ValueError:
                            pass

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

                    percentage_cell = row.find("td", class_=re.compile(r"column-percentage\b"))
                    percentage_text = percentage_cell.get_text(strip=True) if percentage_cell else None

                    feedback_cell = row.find("td", class_=re.compile(r"column-feedback\b"))
                    feedback = None
                    if feedback_cell:
                        feedback_text = feedback_cell.get_text(strip=True)
                        if feedback_text and feedback_text != "&nbsp;":
                            feedback = feedback_text

                    grade_items.append(
                        {
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
                            "gradedatesubmitted": None,
                            "gradedategraded": None,
                        }
                    )

                register_final_available = any(
                    "register final" in item["itemname"].lower()
                    and (
                        item["graderaw"] is not None
                        or (item["gradeformatted"] and item["gradeformatted"] != "-")
                    )
                    for item in grade_items
                )

                filtered_items = []
                for item in grade_items:
                    item_name_lower = item["itemname"].lower()
                    grade_raw = item["graderaw"]
                    is_register_total = (
                        "register total" in item_name_lower
                        or "register(not to edit) total" in item_name_lower
                        or "registertotal" in item_name_lower
                    )
                    is_course_total = "course total" in item_name_lower or item_name_lower == "total"

                    if (is_register_total or is_course_total) and grade_raw == 0.0:
                        if is_register_total and register_final_available:
                            pass
                        else:
                            continue

                    filtered_items.append(item)

                success = True
                return True, filtered_items, None
            except Exception as exc:
                logger.warning(
                    "Failed to parse grades HTML: %s, response text: %s",
                    exc,
                    response.text[:500],
                )
                return False, None, f"Failed to parse HTML: {exc}"
    except httpx.RequestError as exc:
        response_status_code = 0
        return False, None, f"Request failed: {exc}"
    except Exception as exc:
        response_status_code = 0
        return False, None, f"Unexpected error: {exc}"
    finally:
        response_time_ms = (time.perf_counter() - start_time) * 1000
        log_moodle_request(
            http_method="GET",
            endpoint_path=endpoint_path,
            response_status_code=response_status_code or 0,
            response_time_ms=response_time_ms,
            success=success,
            api_method_name=None,
            request_size_bytes=request_size_bytes,
            response_size_bytes=response_size_bytes,
            session_refresh_id=session_refresh_id,
        )
