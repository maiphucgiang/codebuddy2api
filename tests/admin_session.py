"""Shared management-session helpers for offline tests."""
from unittest.mock import AsyncMock

from fastapi import Request


def credentials(password):
    return {"username": "admin", "password": password, "remember": False}


def login(client, *, username="admin", password="synthetic-key", origin="http://testserver"):
    result = client.post(
        "/admin/session",
        json={"username": username, "password": password, "remember": False},
        headers={"Origin": origin},
    )
    assert result.status_code == 200, result.text
    return {"Origin": origin, "X-CSRF-Token": result.json()["csrf_token"]}


def direct_request(body=None, *, identity="key:offline-test", method="POST", path="/admin/credentials"):
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "client": ("test", 1),
        "server": ("test", 80),
        "state": {"admin_identity": identity} if identity else {},
    }
    request = Request(scope)
    request.json = AsyncMock(return_value=body)
    return request
