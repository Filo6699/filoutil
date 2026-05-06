from __future__ import annotations

import logging
import os
import secrets
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from dotenv import load_dotenv
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy.exc import OperationalError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Mount, Route

from filoutil.db.models import SessionRefresh, User
from filoutil.db.monitors import create_monitor, delete_monitor, get_all_monitors, get_monitor, update_monitor
from filoutil.db.moodle_courses import get_course_grades, get_user_courses
from filoutil.db.moodle_notifications import get_unread_notifications_count, get_user_notifications
from filoutil.db.permissions import get_user_permissions
from filoutil.db.postgres import SessionLocal, init_db
from filoutil.db.session_refresh import (
    create_session_refresh,
    get_active_sessions_for_user,
    get_session_refresh_by_id,
    get_user_refresh_interval,
    stop_session_refresh,
)
from filoutil.moodle_client import (
    fetch_assignment_page,
    fetch_calendar_events,
    fetch_course_grades,
    fetch_moodle_user_id,
    fetch_user_courses,
    get_moodle_file_metadata,
)
from filoutil.utils.http import create_async_client
from filoutil.session_refresh.oidc_restore import (
    bootstrap_moodle_session_via_oidc,
    parse_oidc_cookies,
)

logger = logging.getLogger(__name__)

_current_principal: ContextVar[AuthenticatedPrincipal | None] = ContextVar(
    "mcp_authenticated_principal", default=None
)


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    estsauthpersistent: str | None
    estsauth: str | None
    user_id: int | None
    telegram_id: int | None
    username: str | None
    permissions: tuple[str, ...]
    matched_session_id: int | None


@dataclass(frozen=True)
class DownloadTicket:
    file_url: str
    moodle_session: str
    filename: str
    content_type: str
    size_bytes: int | None
    expires_at: datetime


_download_tickets: dict[str, DownloadTicket] = {}
_DOWNLOAD_TICKET_TTL = timedelta(minutes=10)


def _compare_secret(candidate: str | None, expected: str | None) -> bool:
    if not candidate or not expected:
        return False
    return secrets.compare_digest(candidate, expected)


def _prune_download_tickets(now: datetime | None = None) -> None:
    current_time = now or datetime.now(timezone.utc)
    expired_tokens = [
        token for token, ticket in _download_tickets.items() if ticket.expires_at <= current_time
    ]
    for token in expired_tokens:
        _download_tickets.pop(token, None)


def _issue_download_ticket(
    *,
    file_url: str,
    moodle_session: str,
    filename: str,
    content_type: str,
    size_bytes: int | None,
) -> tuple[str, DownloadTicket]:
    _prune_download_tickets()
    token = secrets.token_urlsafe(24)
    ticket = DownloadTicket(
        file_url=file_url,
        moodle_session=moodle_session,
        filename=filename,
        content_type=content_type,
        size_bytes=size_bytes,
        expires_at=datetime.now(timezone.utc) + _DOWNLOAD_TICKET_TTL,
    )
    _download_tickets[token] = ticket
    return token, ticket


def _build_download_url(request: Request, token: str) -> str:
    return str(request.url_for("download_ticket", token=token))


def _serialize_session(session: SessionRefresh) -> dict[str, object]:
    return {
        "id": session.id,
        "name": session.name,
        "status": session.status,
        "sesskey": session.sesskey,
        "moodleSession": session.moodleSession,
        "moodle_user_id": session.moodle_user_id,
        "refresh_interval_s": session.refresh_interval_s,
        "started_at": session.started_at.isoformat() if session.started_at else None,
        "ended_at": session.ended_at.isoformat() if session.ended_at else None,
        "has_oidc_data": bool(session.oidc_data),
        "oidc_retry_count": session.oidc_retry_count,
        "oidc_last_error": session.oidc_last_error,
    }


def _serialize_monitor(monitor) -> dict[str, object]:
    return {
        "id": monitor.id,
        "name": monitor.name,
        "url": monitor.url,
        "method": monitor.method,
        "interval_s": monitor.interval_s,
        "timeout_s": monitor.timeout_s,
        "expected_status": monitor.expected_status,
        "keyword": monitor.keyword,
        "enabled": monitor.enabled,
        "alert_on_down": monitor.alert_on_down,
        "alert_on_up": monitor.alert_on_up,
        "status": monitor.status,
        "last_check_at": monitor.last_check_at.isoformat() if monitor.last_check_at else None,
    }


def _serialize_notification(notification) -> dict[str, object]:
    return {
        "id": notification.id,
        "notification_id": notification.notification_id,
        "subject": notification.subject,
        "shortenedsubject": notification.shortenedsubject,
        "text": notification.text,
        "fullmessage": notification.fullmessage,
        "contexturl": notification.contexturl,
        "contexturlname": notification.contexturlname,
        "timecreated": notification.timecreated,
        "read": notification.read,
        "deleted": notification.deleted,
        "component": notification.component,
        "eventtype": notification.eventtype,
    }


def _serialize_course(course) -> dict[str, object]:
    return {
        "id": course.id,
        "course_id": course.course_id,
        "course_name": course.course_name,
        "shortname": course.shortname,
        "archived": course.archived,
        "created_at": course.created_at.isoformat() if course.created_at else None,
        "updated_at": course.updated_at.isoformat() if course.updated_at else None,
    }


def _serialize_grade(grade) -> dict[str, object]:
    return {
        "id": grade.id,
        "course_id": grade.course_id,
        "grade_item_id": grade.grade_item_id,
        "item_name": grade.item_name,
        "item_type": grade.item_type,
        "item_module": grade.item_module,
        "grade_raw": grade.grade_raw,
        "grade_formatted": grade.grade_formatted,
        "grade_max": grade.grade_max,
        "grade_min": grade.grade_min,
        "grade_date_submitted": grade.grade_date_submitted,
        "grade_date_graded": grade.grade_date_graded,
        "feedback": grade.feedback,
        "last_checked_at": grade.last_checked_at.isoformat() if grade.last_checked_at else None,
    }


def _match_principal_from_rows(
    rows: list[tuple[SessionRefresh, User]],
    estsauthpersistent: str | None,
    estsauth: str | None,
    permission_loader,
) -> AuthenticatedPrincipal | None:
    for session_refresh, user in rows:
        oidc_data = session_refresh.oidc_data or {}
        cookies = parse_oidc_cookies(oidc_data.get("microsoft_cookies"))
        if not cookies:
            continue

        persistent_match = _compare_secret(estsauthpersistent, cookies.get("ESTSAUTHPERSISTENT"))
        auth_match = _compare_secret(estsauth, cookies.get("ESTSAUTH"))
        if not persistent_match and not auth_match:
            continue

        permissions = tuple(sorted(permission_loader(user.id)))
        return AuthenticatedPrincipal(
            estsauthpersistent=estsauthpersistent,
            estsauth=estsauth,
            user_id=user.id,
            telegram_id=user.telegram_id,
            username=user.username,
            permissions=permissions,
            matched_session_id=session_refresh.id,
        )

    return None


def _resolve_principal_from_ests(
    estsauthpersistent: str | None, estsauth: str | None
) -> AuthenticatedPrincipal | None:
    if not estsauthpersistent and not estsauth:
        return None

    with SessionLocal() as db:
        rows = (
            db.query(SessionRefresh, User)
            .join(User, SessionRefresh.user_id == User.id)
            .filter(SessionRefresh.status == "running")
            .order_by(SessionRefresh.started_at.desc(), SessionRefresh.id.desc())
            .all()
        )
        return _match_principal_from_rows(
            rows,
            estsauthpersistent,
            estsauth,
            lambda user_id: get_user_permissions(db, user_id),
        )


def _principal_from_request(request: Request) -> AuthenticatedPrincipal | None:
    estsauthpersistent = request.cookies.get("ESTSAUTHPERSISTENT") or request.headers.get(
        "X-ESTSAUTHPERSISTENT"
    )
    estsauth = request.cookies.get("ESTSAUTH") or request.headers.get("X-ESTSAUTH")
    if not estsauthpersistent and not estsauth:
        return None

    try:
        bound_principal = _resolve_principal_from_ests(estsauthpersistent, estsauth)
    except OperationalError as exc:
        logger.warning("Database unavailable during MCP auth lookup: %s", exc)
        bound_principal = None
    except Exception:
        logger.exception("Unexpected MCP auth lookup failure")
        bound_principal = None

    if bound_principal is not None:
        return bound_principal

    return AuthenticatedPrincipal(
        estsauthpersistent=estsauthpersistent,
        estsauth=estsauth,
        user_id=None,
        telegram_id=None,
        username=None,
        permissions=(),
        matched_session_id=None,
    )


def _require_principal(
    module: str | None = None, *, require_bound_user: bool = False
) -> AuthenticatedPrincipal:
    principal = _current_principal.get()
    if principal is None:
        raise ToolError(
            "Unauthorized. Send ESTSAUTHPERSISTENT or ESTSAUTH as a cookie or X- header."
        )

    if require_bound_user and principal.user_id is None:
        raise ToolError(
            "This tool requires a filoutil database-backed user session. The database is unavailable, "
            "or the supplied ESTS cookies are not linked to a stored filoutil Moodle session."
        )

    if module and module not in principal.permissions:
        raise ToolError(f"Authenticated user lacks `{module}` permission.")

    return principal


class MCPAuthMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not path.startswith("/mcp"):
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        principal = _principal_from_request(request)
        if principal is None:
            response = JSONResponse(
                {
                    "error": (
                        "Unauthorized. Provide ESTSAUTHPERSISTENT or ESTSAUTH as a cookie "
                        "or X-ESTSAUTHPERSISTENT / X-ESTSAUTH header, and ensure the value "
                        "matches an existing active Moodle OIDC session in filoutil."
                    )
                },
                status_code=401,
            )
            await response(scope, receive, send)
            return

        token = _current_principal.set(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_principal.reset(token)


@asynccontextmanager
async def _mcp_lifespan(_server: FastMCP):
    try:
        init_db()
    except OperationalError as exc:
        logger.warning("MCP started without database connectivity: %s", exc)
    yield None


mcp = FastMCP(
    name="filoutil",
    instructions=(
        "Access filoutil monitoring and Moodle data. Authenticate every HTTP request by sending "
        "ESTSAUTHPERSISTENT or ESTSAUTH as a cookie or X- header. The credential must match an "
        "active stored Moodle OIDC session for a filoutil user."
    ),
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
    lifespan=_mcp_lifespan,
)


@mcp.custom_route("/downloads/{token}", methods=["GET"], name="download_ticket", include_in_schema=False)
async def download_ticket(request: Request) -> StreamingResponse | JSONResponse:
    token = request.path_params.get("token")
    if not isinstance(token, str) or not token:
        return JSONResponse({"error": "Missing download token."}, status_code=400)

    _prune_download_tickets()
    ticket = _download_tickets.get(token)
    if ticket is None:
        return JSONResponse({"error": "Download token not found or expired."}, status_code=404)

    try:
        client = create_async_client(timeout=None, follow_redirects=True)
        response = await client.send(
            client.build_request("GET", ticket.file_url, cookies={"MoodleSession": ticket.moodle_session}),
            stream=True,
        )
    except Exception as exc:
        return JSONResponse({"error": f"Failed to stream Moodle file: {exc}"}, status_code=502)

    if response.status_code != 200:
        try:
            body = await response.aread()
        finally:
            await response.aclose()
            await client.aclose()
        preview = body[:200].decode("utf-8", errors="replace") if body else "No response body"
        return JSONResponse({"error": f"Moodle returned HTTP {response.status_code}: {preview}"}, status_code=502)

    if response.url.path.startswith("/login/"):
        await response.aclose()
        await client.aclose()
        return JSONResponse({"error": "Moodle redirected the file download to the login page."}, status_code=502)

    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(ticket.filename)}",
    }
    content_length = response.headers.get("content-length")
    if content_length:
        headers["Content-Length"] = content_length

    async def body_iter():
        try:
            async for chunk in response.aiter_bytes():
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()

    return StreamingResponse(body_iter(), media_type=ticket.content_type, headers=headers)


@mcp.tool()
def whoami() -> dict[str, object]:
    """Return the currently authenticated filoutil user."""
    principal = _require_principal()
    return {
        "user_id": principal.user_id,
        "telegram_id": principal.telegram_id,
        "username": principal.username,
        "permissions": list(principal.permissions),
        "matched_session_id": principal.matched_session_id,
        "has_estsauthpersistent": bool(principal.estsauthpersistent),
        "has_estsauth": bool(principal.estsauth),
        "database_bound": principal.user_id is not None,
    }


@mcp.tool()
def list_moodle_sessions() -> list[dict[str, object]]:
    """List active Moodle sessions for the authenticated user."""
    principal = _require_principal("moodle", require_bound_user=True)
    with SessionLocal() as db:
        sessions = get_active_sessions_for_user(db, principal.user_id)
        return [_serialize_session(session) for session in sessions]


@mcp.tool()
async def add_moodle_oidc_session(
    estsauthpersistent: str | None = None,
    estsauth: str | None = None,
    name: str | None = None,
) -> dict[str, object]:
    """Create a Moodle session for the authenticated user from Microsoft ESTS cookies."""
    principal = _require_principal("moodle")
    if principal.user_id is None:
        raise ToolError(
            "Creating a persisted Moodle session requires the filoutil database to be available."
        )

    cookies: dict[str, str] = {}
    if estsauthpersistent:
        cookies["ESTSAUTHPERSISTENT"] = estsauthpersistent
    if estsauth:
        cookies["ESTSAUTH"] = estsauth
    if not cookies:
        raise ToolError("Provide at least one of `estsauthpersistent` or `estsauth`.")

    success, sesskey, moodle_session, updated_oidc_data, error_msg = (
        await bootstrap_moodle_session_via_oidc({"microsoft_cookies": cookies})
    )
    if not success or not sesskey or not moodle_session:
        raise ToolError(f"Failed to bootstrap Moodle session via OIDC: {error_msg or 'Unknown error'}")

    with SessionLocal() as db:
        user = db.query(User).filter(User.id == principal.user_id).one_or_none()
        if user is None:
            raise ToolError("Authenticated user no longer exists.")
        if not user.moodle_session_agreement or not user.moodle_student_confirmation:
            raise ToolError(
                "The authenticated user has not accepted the Moodle session agreement in Telegram yet."
            )

        active_sessions = get_active_sessions_for_user(db, principal.user_id)
        if len(active_sessions) >= 5:
            raise ToolError("The authenticated user already has 5 active Moodle sessions.")

        refresh_interval = get_user_refresh_interval(db, principal.user_id)
        session_refresh = create_session_refresh(
            db,
            principal.user_id,
            sesskey,
            moodle_session,
            refresh_interval,
            name=name,
            oidc_data=updated_oidc_data or {"microsoft_cookies": cookies},
        )

        try:
            moodle_user_success, moodle_user_id, _error = await fetch_moodle_user_id(
                sesskey, moodle_session
            )
            if moodle_user_success and moodle_user_id:
                session_refresh.moodle_user_id = moodle_user_id
                db.commit()
                db.refresh(session_refresh)
        except Exception:
            logger.exception(
                "Failed to fetch Moodle user id for MCP-created session %s", session_refresh.id
            )

        return _serialize_session(session_refresh)


@mcp.tool()
def stop_moodle_session_by_id(
    session_id: int, clear_oidc_data: bool = False
) -> dict[str, object]:
    """Stop one of the authenticated user's Moodle sessions."""
    principal = _require_principal("moodle")
    if principal.user_id is None:
        raise ToolError("Stopping persisted Moodle sessions requires the filoutil database.")
    with SessionLocal() as db:
        session_refresh = get_session_refresh_by_id(db, session_id)
        if session_refresh is None or session_refresh.user_id != principal.user_id:
            raise ToolError("Moodle session not found for the authenticated user.")

        stopped = stop_session_refresh(
            db,
            session_id,
            status="stopped",
            clear_oidc_data=clear_oidc_data,
        )
        if stopped is None:
            raise ToolError("Failed to stop Moodle session.")
        return _serialize_session(stopped)


@mcp.tool()
def list_moodle_notifications(
    limit: int = 20, unread_only: bool = False
) -> list[dict[str, object]]:
    """List Moodle notifications already synced for the authenticated user."""
    principal = _require_principal("moodle")
    if principal.user_id is None:
        raise ToolError("Reading synced Moodle notifications requires the filoutil database.")
    bounded_limit = max(1, min(limit, 100))
    with SessionLocal() as db:
        notifications = get_user_notifications(db, principal.user_id, limit=bounded_limit)
        if unread_only:
            notifications = [n for n in notifications if not n.read and not n.deleted]
        return [_serialize_notification(notification) for notification in notifications]


@mcp.tool()
def get_unread_moodle_notification_count() -> dict[str, int]:
    """Return the unread Moodle notification count for the authenticated user."""
    principal = _require_principal("moodle")
    if principal.user_id is None:
        raise ToolError("Reading synced Moodle notifications requires the filoutil database.")
    with SessionLocal() as db:
        return {"count": get_unread_notifications_count(db, principal.user_id)}


@mcp.tool()
def list_moodle_courses(include_archived: bool = False) -> list[dict[str, object]]:
    """List Moodle courses already synced for the authenticated user."""
    principal = _require_principal("moodle")
    if principal.user_id is None:
        raise ToolError("Reading synced Moodle courses requires the filoutil database.")
    archived = None if include_archived else False
    with SessionLocal() as db:
        courses = get_user_courses(db, principal.user_id, archived=archived)
        return [_serialize_course(course) for course in courses]


@mcp.tool()
def get_moodle_course_grades(course_id: int) -> list[dict[str, object]]:
    """Return grade entries for one Moodle course."""
    principal = _require_principal("moodle")
    if principal.user_id is None:
        raise ToolError("Reading synced Moodle grades requires the filoutil database.")
    with SessionLocal() as db:
        grades = get_course_grades(db, principal.user_id, course_id)
        return [_serialize_grade(grade) for grade in grades]


@mcp.tool()
def list_monitors() -> list[dict[str, object]]:
    """List all configured monitors."""
    _require_principal("monitoring", require_bound_user=True)
    with SessionLocal() as db:
        monitors = get_all_monitors(db)
        return [_serialize_monitor(monitor) for monitor in monitors]


@mcp.tool()
def get_monitor_details(monitor_id: int) -> dict[str, object]:
    """Return one monitor by ID."""
    _require_principal("monitoring", require_bound_user=True)
    with SessionLocal() as db:
        monitor = get_monitor(db, monitor_id)
        if monitor is None:
            raise ToolError("Monitor not found.")
        return _serialize_monitor(monitor)


@mcp.tool()
def create_monitor_config(
    name: str,
    url: str,
    method: str = "GET",
    interval_s: int = 60,
    timeout_s: int = 10,
    expected_status: int = 200,
    keyword: str | None = None,
    enabled: bool = True,
    alert_on_down: bool = True,
    alert_on_up: bool = True,
) -> dict[str, object]:
    """Create a monitor."""
    _require_principal("monitoring", require_bound_user=True)
    with SessionLocal() as db:
        monitor = create_monitor(
            db,
            name=name,
            url=url,
            method=method,
            interval_s=interval_s,
            timeout_s=timeout_s,
            expected_status=expected_status,
            keyword=keyword,
            enabled=enabled,
            alert_on_down=alert_on_down,
            alert_on_up=alert_on_up,
        )
        return _serialize_monitor(monitor)


@mcp.tool()
def update_monitor_config(
    monitor_id: int,
    name: str | None = None,
    url: str | None = None,
    method: str | None = None,
    interval_s: int | None = None,
    timeout_s: int | None = None,
    expected_status: int | None = None,
    keyword: str | None = None,
    enabled: bool | None = None,
    alert_on_down: bool | None = None,
    alert_on_up: bool | None = None,
) -> dict[str, object]:
    """Update one monitor."""
    _require_principal("monitoring", require_bound_user=True)
    updates = {
        key: value
        for key, value in {
            "name": name,
            "url": url,
            "method": method,
            "interval_s": interval_s,
            "timeout_s": timeout_s,
            "expected_status": expected_status,
            "keyword": keyword,
            "enabled": enabled,
            "alert_on_down": alert_on_down,
            "alert_on_up": alert_on_up,
        }.items()
        if value is not None
    }
    if not updates:
        raise ToolError("No monitor fields were provided for update.")

    with SessionLocal() as db:
        monitor = update_monitor(db, monitor_id, **updates)
        if monitor is None:
            raise ToolError("Monitor not found.")
        return _serialize_monitor(monitor)


@mcp.tool()
def delete_monitor_config(monitor_id: int) -> dict[str, object]:
    """Delete one monitor."""
    _require_principal("monitoring", require_bound_user=True)
    with SessionLocal() as db:
        deleted = delete_monitor(db, monitor_id)
        if not deleted:
            raise ToolError("Monitor not found.")
        return {"deleted": True, "monitor_id": monitor_id}


def _format_assignment_grade(item: dict[str, Any]) -> str | None:
    grade_formatted = item.get("gradeformatted")
    if grade_formatted and grade_formatted != "-":
        return str(grade_formatted)

    grade_raw = item.get("graderaw")
    if grade_raw is None:
        return None
    try:
        return f"{float(grade_raw):.2f}".rstrip("0").rstrip(".")
    except (ValueError, TypeError):
        return str(grade_raw)


async def _bootstrap_live_moodle_context() -> tuple[AuthenticatedPrincipal, str, str, int]:
    principal = _require_principal()

    cookies: dict[str, str] = {}
    if principal.estsauthpersistent:
        cookies["ESTSAUTHPERSISTENT"] = principal.estsauthpersistent
    if principal.estsauth:
        cookies["ESTSAUTH"] = principal.estsauth

    success, sesskey, moodle_session, _updated_oidc_data, error_msg = (
        await bootstrap_moodle_session_via_oidc({"microsoft_cookies": cookies})
    )
    if not success or not sesskey or not moodle_session:
        raise ToolError(f"Failed to bootstrap Moodle session via OIDC: {error_msg or 'Unknown error'}")

    user_success, moodle_user_id, user_error = await fetch_moodle_user_id(sesskey, moodle_session)
    if not user_success or not moodle_user_id:
        raise ToolError(f"Failed to resolve Moodle user id: {user_error or 'Unknown error'}")

    return principal, sesskey, moodle_session, moodle_user_id


def _is_assignment_event(event: dict[str, Any]) -> bool:
    module = str(event.get("module") or "").lower()
    url = str(event.get("url") or "").lower()
    name = str(event.get("name") or "").lower()
    return module == "assign" or "/mod/assign/" in url or "assignment" in name


def _normalize_deadline_event(event: dict[str, Any]) -> dict[str, object]:
    timestamp = event.get("timesort") or event.get("timestart")
    iso_time = None
    if isinstance(timestamp, (int, float)):
        iso_time = datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
    return {
        **event,
        "deadline_at": iso_time,
    }


@mcp.tool()
async def fetch_assignments(course_name_contains: str | None = None) -> list[dict[str, object]]:
    """Fetch assignment-like grade items live from Moodle using the authenticated ESTS cookies."""
    _principal, sesskey, moodle_session, moodle_user_id = await _bootstrap_live_moodle_context()

    courses_success, courses, courses_error = await fetch_user_courses(
        sesskey, moodle_session, moodle_user_id
    )
    if not courses_success or courses is None:
        raise ToolError(f"Failed to fetch Moodle courses: {courses_error or 'Unknown error'}")

    course_filter = (course_name_contains or "").strip().lower()
    matching_courses = [
        course
        for course in courses
        if not course_filter or course_filter in str(course.get("fullname", "")).lower()
    ]

    assignments: list[dict[str, object]] = []
    for course in matching_courses:
        course_id = course.get("id")
        if not course_id:
            continue

        grades_success, grade_items, grades_error = await fetch_course_grades(
            moodle_session, int(course_id)
        )
        if not grades_success:
            assignments.append(
                {
                    "course_id": course_id,
                    "course_name": course.get("fullname"),
                    "error": grades_error or "Failed to fetch course gradebook",
                }
            )
            continue

        for item in grade_items or []:
            item_name = str(item.get("itemname", ""))
            item_name_lower = item_name.lower()
            item_module = item.get("itemmodule")
            if item_module != "assign" and "assignment" not in item_name_lower:
                continue

            assignments.append(
                {
                    "course_id": course_id,
                    "course_name": course.get("fullname"),
                    "assignment_id": item.get("id"),
                    "assignment_name": item_name,
                    "item_module": item_module,
                    "grade": _format_assignment_grade(item),
                    "grade_min": item.get("grademin"),
                    "grade_max": item.get("grademax"),
                    "feedback": item.get("feedback"),
                }
            )

    return assignments


@mcp.tool()
async def get_calendar_events(
    start_unix: int,
    end_unix: int,
    limit: int = 100,
    course_name_contains: str | None = None,
) -> list[dict[str, object]]:
    """Fetch Moodle calendar action events in a time range."""
    _principal, sesskey, moodle_session, _moodle_user_id = await _bootstrap_live_moodle_context()
    success, events, error = await fetch_calendar_events(
        sesskey,
        moodle_session,
        time_from=start_unix,
        time_to=end_unix,
        limit=max(1, min(limit, 200)),
    )
    if not success or events is None:
        raise ToolError(f"Failed to fetch Moodle calendar events: {error or 'Unknown error'}")

    course_filter = (course_name_contains or "").strip().lower()
    return [
        _normalize_deadline_event(event)
        for event in events
        if not course_filter or course_filter in str(event.get("course_name") or "").lower()
    ]


@mcp.tool()
async def get_upcoming_assignments(
    days: int = 14,
    limit: int = 100,
    course_name_contains: str | None = None,
) -> list[dict[str, object]]:
    """Fetch upcoming assignment deadlines from Moodle calendar events."""
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=max(1, min(days, 180)))
    events = await get_calendar_events(
        int(now.timestamp()),
        int(end.timestamp()),
        limit=limit,
        course_name_contains=course_name_contains,
    )
    return [_normalize_deadline_event(event) for event in events if _is_assignment_event(event)]


@mcp.tool()
async def get_course_assignments(
    course_id: int,
    days: int = 180,
) -> list[dict[str, object]]:
    """Fetch assignment-like calendar events for one course."""
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=max(1, min(days, 365)))
    events = await get_calendar_events(int(now.timestamp()), int(end.timestamp()), limit=200)
    return [
        _normalize_deadline_event(event)
        for event in events
        if event.get("course_id") == course_id and _is_assignment_event(event)
    ]


@mcp.tool()
async def get_assignment_detail(assignment_url: str) -> dict[str, object]:
    """Fetch assignment detail by Moodle assignment URL."""
    _principal, _sesskey, moodle_session, _moodle_user_id = await _bootstrap_live_moodle_context()
    success, detail, error = await fetch_assignment_page(moodle_session, assignment_url)
    if not success or detail is None:
        raise ToolError(f"Failed to fetch assignment detail: {error or 'Unknown error'}")
    return detail


@mcp.tool()
async def list_assignment_files(assignment_url: str) -> list[dict[str, object]]:
    """List downloadable files visible on a Moodle assignment page."""
    detail = await get_assignment_detail(assignment_url)
    files = detail.get("files")
    if not isinstance(files, list):
        return []
    return files


@mcp.tool()
async def download_assignment_file(file_url: str, ctx: Context) -> dict[str, object]:
    """Create a temporary proxy URL that streams a Moodle pluginfile attachment as raw bytes."""
    _principal, _sesskey, moodle_session, _moodle_user_id = await _bootstrap_live_moodle_context()
    success, metadata, error = await get_moodle_file_metadata(moodle_session, file_url)
    if not success or metadata is None:
        raise ToolError(f"Failed to prepare Moodle file download: {error or 'Unknown error'}")

    request = ctx.request_context.request
    if request is None:
        raise ToolError("This tool requires an HTTP-backed MCP request context.")

    filename = str(metadata.get("filename") or "download.bin")
    content_type = str(metadata.get("content_type") or "application/octet-stream")
    size_bytes_raw = metadata.get("size_bytes")
    size_bytes = size_bytes_raw if isinstance(size_bytes_raw, int) else None

    token, ticket = _issue_download_ticket(
        file_url=str(metadata.get("url") or file_url),
        moodle_session=moodle_session,
        filename=filename,
        content_type=content_type,
        size_bytes=size_bytes,
    )

    return {
        "filename": filename,
        "content_type": content_type,
        "size_bytes": size_bytes,
        "download_url": _build_download_url(request, token),
        "expires_at": ticket.expires_at.isoformat(),
    }


async def _root(_request: Request):
    return JSONResponse(
        {
            "service": "filoutil-mcp",
            "endpoint": "/mcp",
            "transport": "streamable-http",
            "auth": {
                "cookies": ["ESTSAUTHPERSISTENT", "ESTSAUTH"],
                "headers": ["X-ESTSAUTHPERSISTENT", "X-ESTSAUTH"],
            },
        }
    )


async def _healthz(_request: Request):
    return JSONResponse({"ok": True})


def build_asgi_app() -> Starlette:
    app = mcp.streamable_http_app()
    app.router.routes.insert(0, Route("/healthz", _healthz))
    app.router.routes.insert(0, Route("/", _root))
    app.add_middleware(MCPAuthMiddleware)
    return app


app = build_asgi_app()


def main() -> None:
    import uvicorn

    load_dotenv()

    host = os.getenv("MCP_HOST", "127.0.0.1")
    port = int(os.getenv("MCP_PORT", "8765"))
    log_level = os.getenv("MCP_LOG_LEVEL", os.getenv("LOG_LEVEL", "info")).lower()

    uvicorn.run(app, host=host, port=port, log_level=log_level)


if __name__ == "__main__":
    main()
