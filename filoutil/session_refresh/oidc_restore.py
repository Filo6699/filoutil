import logging
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

from filoutil.config import OIDC_RECOVERY_USER_AGENT
from filoutil.utils.http import create_async_client

logger = logging.getLogger(__name__)

LMS_BASE_URL = "https://lms.astanait.edu.kz"
DEFAULT_OIDC_ENTRY_PATH = "/auth/oidc/?source=loginpage"
LOGIN_DOMAIN = "login.microsoftonline.com"
ALLOWED_OIDC_DOMAINS = {
    domain
    for domain in {
        urlparse(LMS_BASE_URL).hostname,
        LOGIN_DOMAIN,
    }
    if domain
}


class OidcFormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.form_action: str | None = None
        self.inputs: dict[str, str] = {}
        self._in_form = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = {k: (v or "") for k, v in attrs}
        if tag == "form":
            action = attrs_map.get("action", "")
            if "/auth/oidc/" in action:
                self._in_form = True
                self.form_action = action
                self.inputs = {}
        elif tag == "input" and self._in_form:
            name = attrs_map.get("name")
            if name:
                self.inputs[name] = attrs_map.get("value", "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._in_form:
            self._in_form = False


def _parse_cookie_string(raw: str) -> dict[str, str]:
    ignored_attrs = {"path", "domain", "expires", "max-age", "secure", "httponly", "samesite"}
    cookies: dict[str, str] = {}
    for chunk in raw.split(";"):
        piece = chunk.strip()
        if not piece or "=" not in piece:
            continue
        key, value = piece.split("=", 1)
        key = key.strip()
        if key and key.lower() not in ignored_attrs:
            cookies[key] = value.strip()
    return cookies


def parse_oidc_cookies(value: object) -> dict[str, str]:
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items() if str(k)}
    if isinstance(value, list):
        parsed: dict[str, str] = {}
        for item in value:
            if isinstance(item, dict):
                name = item.get("name")
                cookie_value = item.get("value")
                if name and cookie_value is not None:
                    parsed[str(name)] = str(cookie_value)
        return parsed
    if isinstance(value, str):
        return _parse_cookie_string(value)
    return {}


def _extract_sesskey(html: str) -> str | None:
    patterns = [
        r"M\.cfg\.sesskey[^A-Za-z0-9]*['\"]?([A-Za-z0-9]{6,})",
        r'"sesskey"\s*:\s*"([A-Za-z0-9]{6,})"',
        r"logout(?:\\\.)?php(?:\\\?)?sesskey=([A-Za-z0-9]{6,})",
    ]
    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


async def resolve_sesskey_from_moodle_session(
    moodle_session: str, base_url: str = LMS_BASE_URL
) -> tuple[bool, str | None, str | None]:
    cleaned_cookie = str(moodle_session or "").strip()
    if not cleaned_cookie:
        return False, None, "Missing MoodleSession cookie."

    root_url = urljoin(base_url.rstrip("/") + "/", "/")
    root_host = urlparse(base_url).hostname

    try:
        async with create_async_client(timeout=30.0, follow_redirects=True) as client:
            if root_host:
                client.cookies.set("MoodleSession", cleaned_cookie, domain=root_host, path="/")
            client.cookies.set("MoodleSession", cleaned_cookie)

            response = await client.get(root_url)
            if response.status_code != 200:
                return False, None, f"Failed to open Moodle page (HTTP {response.status_code})."

            final_path = urlparse(str(response.url)).path.lower()
            if final_path.startswith("/login/"):
                return (
                    False,
                    None,
                    "MoodleSession is not authenticated anymore. Please send a fresh MoodleSession.",
                )

            sesskey = _extract_sesskey(response.text)
            if not sesskey:
                return (
                    False,
                    None,
                    "Could not auto-fetch sesskey from Moodle page. Please send a fresh MoodleSession.",
                )

            return True, sesskey, None
    except httpx.RequestError as e:
        logger.warning("Moodle sesskey resolution request error: %s", e)
        return False, None, f"Failed to reach Moodle: {e}"
    except Exception as e:
        logger.error("Unexpected sesskey resolution error: %s", e, exc_info=True)
        return False, None, f"Unexpected sesskey resolution error: {e}"


def _extract_oidc_post_fields(html: str) -> tuple[str | None, dict[str, str]]:
    parser = OidcFormParser()
    parser.feed(html)
    required = ("code", "state", "session_state")
    if parser.form_action and all(parser.inputs.get(k) for k in required):
        fields = {k: parser.inputs[k] for k in required}
        return parser.form_action, fields
    return None, {}


def _collect_microsoft_cookies(client: httpx.AsyncClient) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for cookie in client.cookies.jar:
        domain = cookie.domain.lstrip(".")
        if domain.endswith(LOGIN_DOMAIN):
            cookies[cookie.name] = cookie.value
    return cookies


def _is_allowed_oidc_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    lowered = hostname.lower()
    return any(
        lowered == domain or lowered.endswith(f".{domain}") for domain in ALLOWED_OIDC_DOMAINS
    )


def _normalize_oidc_url(raw_value: object, default_url: str) -> str:
    if raw_value is None:
        return default_url
    value = str(raw_value).strip()
    if not value:
        return default_url
    return urljoin(f"{LMS_BASE_URL}/", value)


def _validate_oidc_url(url: str, field_name: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return f"`{field_name}` must use https."
    if not _is_allowed_oidc_host(parsed.hostname):
        allowed = ", ".join(sorted(ALLOWED_OIDC_DOMAINS))
        return f"`{field_name}` host is not allowed. Allowed domains: {allowed}."
    return None


async def bootstrap_moodle_session_via_oidc(
    oidc_data: dict | None,
) -> tuple[bool, str | None, str | None, dict | None, str | None]:
    if not oidc_data:
        return False, None, None, None, "Missing oidc data."

    ms_cookies = parse_oidc_cookies(oidc_data.get("microsoft_cookies"))
    entry_url = _normalize_oidc_url(
        oidc_data.get("oidc_entry_url"), urljoin(LMS_BASE_URL, DEFAULT_OIDC_ENTRY_PATH)
    )
    entry_url_error = _validate_oidc_url(entry_url, "oidc_entry_url")
    if entry_url_error:
        return False, None, None, None, entry_url_error

    callback_fields = {
        "code": str(oidc_data.get("code", "")),
        "state": str(oidc_data.get("state", "")),
        "session_state": str(oidc_data.get("session_state", "")),
    }
    has_callback_fields = all(callback_fields.values())
    if not ms_cookies and not has_callback_fields:
        return (
            False,
            None,
            None,
            None,
            "Missing OIDC data. Provide microsoft_cookies for autonomous recovery, or code/state/session_state for one-time bootstrap.",
        )

    try:
        async with create_async_client(
            timeout=30.0,
            follow_redirects=False,
            user_agent=OIDC_RECOVERY_USER_AGENT,
        ) as client:
            for name, value in ms_cookies.items():
                client.cookies.set(name, value, domain=f".{LOGIN_DOMAIN}", path="/")

            post_fields = callback_fields
            post_url = _normalize_oidc_url(
                oidc_data.get("oidc_callback_url"), urljoin(LMS_BASE_URL, "/auth/oidc/")
            )
            post_url_error = _validate_oidc_url(post_url, "oidc_callback_url")
            if post_url_error:
                return False, None, None, None, post_url_error

            if ms_cookies:
                entry_response = await client.get(entry_url)
                if entry_response.status_code not in {301, 302, 303, 307, 308}:
                    return (
                        False,
                        None,
                        None,
                        None,
                        f"Unexpected OIDC entry status: HTTP {entry_response.status_code}",
                    )

                authorize_url = entry_response.headers.get("location")
                if not authorize_url:
                    return False, None, None, None, "OIDC entry did not return authorize URL."

                authorize_url = urljoin(entry_url, authorize_url)
                authorize_url_error = _validate_oidc_url(authorize_url, "authorize_url")
                if authorize_url_error:
                    return False, None, None, None, authorize_url_error
                auth_response = await client.get(authorize_url, follow_redirects=True)

                form_action, extracted_fields = _extract_oidc_post_fields(auth_response.text)
                if not form_action:
                    parsed_auth_url = urlparse(str(auth_response.url))
                    if parsed_auth_url.netloc.endswith(LOGIN_DOMAIN):
                        return (
                            False,
                            None,
                            None,
                            None,
                            "OIDC requires interactive Microsoft sign-in. Refresh Microsoft cookies.",
                        )
                    return (
                        False,
                        None,
                        None,
                        None,
                        "Could not extract OIDC code/state from provider page.",
                    )

                post_fields = extracted_fields
                post_url = urljoin(str(auth_response.url), form_action)
                post_url_error = _validate_oidc_url(post_url, "oidc_callback_url")
                if post_url_error:
                    return False, None, None, None, post_url_error

            oidc_post_response = await client.post(
                post_url,
                data=post_fields,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )
            if oidc_post_response.status_code not in {301, 302, 303, 307, 308}:
                return (
                    False,
                    None,
                    None,
                    None,
                    f"OIDC callback failed: HTTP {oidc_post_response.status_code}",
                )

            redirect_url = oidc_post_response.headers.get("location") or "/"
            page_response = await client.get(
                urljoin(LMS_BASE_URL, redirect_url), follow_redirects=True
            )

            moodle_session = client.cookies.get(
                "MoodleSession", domain=urlparse(LMS_BASE_URL).hostname
            )
            if not moodle_session:
                moodle_session = client.cookies.get("MoodleSession")
            if not moodle_session:
                return (
                    False,
                    None,
                    None,
                    None,
                    "MoodleSession cookie was not issued after OIDC callback.",
                )

            sesskey = _extract_sesskey(page_response.text)
            if not sesskey:
                return False, None, None, None, "Could not extract sesskey from Moodle page."

            updated_oidc_data = dict(oidc_data)
            updated_oidc_data["oidc_entry_url"] = entry_url
            updated_oidc_data["microsoft_cookies"] = _collect_microsoft_cookies(client)

            return True, sesskey, moodle_session, updated_oidc_data, None
    except httpx.RequestError as e:
        logger.warning("OIDC bootstrap request error: %s", e)
        return False, None, None, None, f"OIDC request failed: {e}"
    except Exception as e:
        logger.error("OIDC bootstrap unexpected error: %s", e, exc_info=True)
        return False, None, None, None, f"OIDC bootstrap error: {e}"
