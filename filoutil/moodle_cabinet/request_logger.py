"""Request logging helper for Moodle API requests.

This module provides functionality to log HTTP requests sent to Moodle
for traffic analysis and optimization purposes.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from filoutil.db.models import MoodleRequestLog
from filoutil.db.postgres import SessionLocal

logger = logging.getLogger(__name__)


def log_moodle_request(
    http_method: str,
    endpoint_path: str,
    response_status_code: int,
    response_time_ms: float,
    success: bool,
    api_method_name: str | None = None,
    request_size_bytes: int | None = None,
    response_size_bytes: int | None = None,
    session_refresh_id: int | None = None,
) -> None:
    """
    Log a Moodle API request to the database.

    This function logs essential request metrics for traffic analysis.
    It handles errors gracefully to avoid interrupting the request flow.

    Args:
        http_method: HTTP method (GET, POST, etc.)
        endpoint_path: Endpoint path (e.g., "/lib/ajax/service.php")
        response_status_code: HTTP response status code
        response_time_ms: Response time in milliseconds
        success: Whether the request was successful
        api_method_name: Optional API method name (e.g., "core_session_touch")
        request_size_bytes: Optional request size in bytes
        response_size_bytes: Optional response size in bytes
        session_refresh_id: Optional session refresh ID to link the request
    """
    try:
        with SessionLocal() as db:
            request_log = MoodleRequestLog(
                timestamp=datetime.now(timezone.utc),
                http_method=http_method,
                endpoint_path=endpoint_path,
                api_method_name=api_method_name,
                response_status_code=response_status_code,
                response_time_ms=response_time_ms,
                request_size_bytes=request_size_bytes,
                response_size_bytes=response_size_bytes,
                success=success,
                session_refresh_id=session_refresh_id,
            )
            db.add(request_log)
            db.commit()
    except Exception as e:
        # Log errors but don't interrupt the request flow
        logger.warning(f"Failed to log Moodle request: {e}", exc_info=True)
