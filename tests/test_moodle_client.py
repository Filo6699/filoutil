import asyncio

from filoutil import moodle_client


class FakeResponse:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload
        self.content = b"{}"
        self.text = "{}"

    def json(self):
        return self._payload


class FakeAsyncClient:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, *args, **kwargs):
        return self._response


class FakeStreamResponse:
    def __init__(self, *, url, headers, status_code=200, body=b""):
        self.url = url
        self.headers = headers
        self.status_code = status_code
        self._body = body

    async def aread(self):
        return self._body


class FakeStreamContext:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeMetadataClient:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream(self, *args, **kwargs):
        return FakeStreamContext(self._response)


def test_call_moodle_ajax_includes_structured_error_details(monkeypatch):
    response = FakeResponse(
        [
            {
                "error": True,
                "message": "",
                "exception": "invalid_parameter_exception",
                "errorcode": "invalidparameter",
                "debuginfo": "Missing required key 'userid'",
            }
        ]
    )

    monkeypatch.setattr(
        moodle_client,
        "create_async_client",
        lambda **kwargs: FakeAsyncClient(response),
    )

    success, data, error = asyncio.run(
        moodle_client._call_moodle_ajax("sesskey", "session", "method", {})
    )

    assert success is False
    assert data is None
    assert error == (
        "Moodle error: invalid_parameter_exception; invalidparameter; "
        "Missing required key 'userid'"
    )


def test_get_moodle_file_metadata_extracts_headers(monkeypatch):
    response = FakeStreamResponse(
        url="https://lms.astanait.edu.kz/pluginfile.php/1/mod_assign/introattachment/0/example.pdf",
        headers={
            "content-type": "application/pdf",
            "content-length": "12345",
            "content-disposition": 'attachment; filename="example.pdf"',
        },
    )

    monkeypatch.setattr(
        moodle_client,
        "create_async_client",
        lambda **kwargs: FakeMetadataClient(response),
    )

    success, data, error = asyncio.run(
        moodle_client.get_moodle_file_metadata(
            "session", "https://lms.astanait.edu.kz/pluginfile.php/1/mod_assign/introattachment/0/example.pdf"
        )
    )

    assert success is True
    assert error is None
    assert data == {
        "url": "https://lms.astanait.edu.kz/pluginfile.php/1/mod_assign/introattachment/0/example.pdf",
        "filename": "example.pdf",
        "content_type": "application/pdf",
        "size_bytes": 12345,
    }


def test_get_moodle_file_metadata_rejects_login_redirect(monkeypatch):
    response = FakeStreamResponse(
        url="https://lms.astanait.edu.kz/login/index.php",
        headers={"content-type": "text/html; charset=utf-8"},
    )

    monkeypatch.setattr(
        moodle_client,
        "create_async_client",
        lambda **kwargs: FakeMetadataClient(response),
    )

    success, data, error = asyncio.run(
        moodle_client.get_moodle_file_metadata(
            "session", "https://lms.astanait.edu.kz/pluginfile.php/1/mod_assign/introattachment/0/example.pdf"
        )
    )

    assert success is False
    assert data is None
    assert error == "Moodle redirected the file download to the login page."
