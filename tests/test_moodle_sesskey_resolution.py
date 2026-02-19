import asyncio

import httpx

from filoutil.session_refresh import oidc_restore


class FakeResponse:
    def __init__(self, status_code: int, text: str, url: str) -> None:
        self.status_code = status_code
        self.text = text
        self.url = url


class FakeAsyncClient:
    response = FakeResponse(200, "", "https://lms.astanait.edu.kz/")
    raise_error: Exception | None = None

    def __init__(self, *args, **kwargs) -> None:
        self.cookies = httpx.Cookies()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url):
        if self.raise_error:
            raise self.raise_error
        return self.response


def test_extract_sesskey_from_m_cfg():
    html = "<script>M.cfg = {\"sesskey\":\"AbC123xYz0\"};</script>"
    assert oidc_restore._extract_sesskey(html) == "AbC123xYz0"


def test_extract_sesskey_from_logout_url():
    html = '<a href="https://example.test/login/logout.php?sesskey=KLMN456789">Logout</a>'
    assert oidc_restore._extract_sesskey(html) == "KLMN456789"


def test_resolve_sesskey_from_moodle_session_success(monkeypatch):
    FakeAsyncClient.raise_error = None
    FakeAsyncClient.response = FakeResponse(
        200,
        '<script>M.cfg={"sesskey":"ZXCVBN1234"};</script>',
        "https://lms.astanait.edu.kz/",
    )
    monkeypatch.setattr(oidc_restore.httpx, "AsyncClient", FakeAsyncClient)

    success, sesskey, error_msg = asyncio.run(
        oidc_restore.resolve_sesskey_from_moodle_session("cookievalue")
    )

    assert success is True
    assert sesskey == "ZXCVBN1234"
    assert error_msg is None


def test_resolve_sesskey_detects_login_redirect(monkeypatch):
    FakeAsyncClient.raise_error = None
    FakeAsyncClient.response = FakeResponse(
        200,
        "<html>Login page</html>",
        "https://lms.astanait.edu.kz/login/index.php",
    )
    monkeypatch.setattr(oidc_restore.httpx, "AsyncClient", FakeAsyncClient)

    success, sesskey, error_msg = asyncio.run(
        oidc_restore.resolve_sesskey_from_moodle_session("cookievalue")
    )

    assert success is False
    assert sesskey is None
    assert "not authenticated" in (error_msg or "").lower()


def test_resolve_sesskey_handles_request_error(monkeypatch):
    request = httpx.Request("GET", "https://lms.astanait.edu.kz/")
    FakeAsyncClient.raise_error = httpx.RequestError("boom", request=request)
    monkeypatch.setattr(oidc_restore.httpx, "AsyncClient", FakeAsyncClient)

    success, sesskey, error_msg = asyncio.run(
        oidc_restore.resolve_sesskey_from_moodle_session("cookievalue")
    )

    assert success is False
    assert sesskey is None
    assert "failed to reach moodle" in (error_msg or "").lower()
