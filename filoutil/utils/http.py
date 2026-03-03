"""HTTP client helpers."""

import httpx

from filoutil.config import HTTP_USER_AGENT


def create_async_client(**kwargs) -> httpx.AsyncClient:
    """Create an AsyncClient with the project User-Agent applied by default."""
    user_agent = kwargs.pop("user_agent", HTTP_USER_AGENT)
    headers = dict(kwargs.pop("headers", {}) or {})
    headers.setdefault("User-Agent", user_agent)
    return httpx.AsyncClient(headers=headers, **kwargs)
