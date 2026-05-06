import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from filoutil import mcp_server
from filoutil.mcp_server import _match_principal_from_rows


def _row(
    session_id: int,
    user_id: int,
    *,
    cookies: dict[str, str] | None = None,
):
    session = SimpleNamespace(id=session_id, oidc_data={"microsoft_cookies": cookies or {}})
    user = SimpleNamespace(
        id=user_id,
        telegram_id=1000 + user_id,
        username=f"user-{user_id}",
    )
    return session, user


def test_match_principal_by_estsauthpersistent():
    principal = _match_principal_from_rows(
        [
            _row(1, 10, cookies={"ESTSAUTHPERSISTENT": "match-me"}),
            _row(2, 20, cookies={"ESTSAUTHPERSISTENT": "other"}),
        ],
        estsauthpersistent="match-me",
        estsauth=None,
        permission_loader=lambda _user_id: ["moodle"],
    )

    assert principal is not None
    assert principal.user_id == 10
    assert principal.matched_session_id == 1
    assert principal.permissions == ("moodle",)


def test_match_principal_by_estsauth():
    principal = _match_principal_from_rows(
        [_row(3, 30, cookies={"ESTSAUTH": "auth-cookie"})],
        estsauthpersistent=None,
        estsauth="auth-cookie",
        permission_loader=lambda _user_id: ["monitoring", "moodle"],
    )

    assert principal is not None
    assert principal.user_id == 30
    assert principal.permissions == ("monitoring", "moodle")


def test_match_principal_without_whitelist_gate():
    principal = _match_principal_from_rows(
        [_row(4, 40, cookies={"ESTSAUTHPERSISTENT": "allowed"})],
        estsauthpersistent="allowed",
        estsauth=None,
        permission_loader=lambda _user_id: ["moodle"],
    )

    assert principal is not None
    assert principal.user_id == 40


def test_match_principal_returns_none_for_mismatch():
    principal = _match_principal_from_rows(
        [_row(5, 50, cookies={"ESTSAUTH": "expected"})],
        estsauthpersistent=None,
        estsauth="different",
        permission_loader=lambda _user_id: ["moodle"],
    )

    assert principal is None


def test_download_assignment_file_returns_proxy_url(monkeypatch):
    async def fake_bootstrap():
        return SimpleNamespace(), "sesskey", "moodle-session", 1

    async def fake_metadata(moodle_session, file_url):
        assert moodle_session == "moodle-session"
        assert file_url == "https://lms.astanait.edu.kz/pluginfile.php/1/test.bin"
        return True, {
            "url": file_url,
            "filename": "test.bin",
            "content_type": "application/octet-stream",
            "size_bytes": 987,
        }, None

    request = SimpleNamespace(url_for=lambda name, token: f"http://127.0.0.1:8765/downloads/{token}")
    ctx = SimpleNamespace(request_context=SimpleNamespace(request=request))

    monkeypatch.setattr(mcp_server, "_bootstrap_live_moodle_context", fake_bootstrap)
    monkeypatch.setattr(mcp_server, "get_moodle_file_metadata", fake_metadata)
    mcp_server._download_tickets.clear()

    result = asyncio.run(
        mcp_server.download_assignment_file(
            "https://lms.astanait.edu.kz/pluginfile.php/1/test.bin",
            ctx,
        )
    )

    assert result["filename"] == "test.bin"
    assert result["content_type"] == "application/octet-stream"
    assert result["size_bytes"] == 987
    assert result["download_url"].startswith("http://127.0.0.1:8765/downloads/")
    assert datetime.fromisoformat(result["expires_at"]) > datetime.now(timezone.utc)
    assert len(mcp_server._download_tickets) == 1
