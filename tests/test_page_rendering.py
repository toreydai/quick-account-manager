from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from app.api import audit, users


@pytest.fixture
def admin_request():
    return Request({
        "type": "http", "method": "GET", "path": "/", "root_path": "",
        "scheme": "http", "server": ("testserver", 80), "headers": [],
        "query_string": b"", "session": {"user": {"email": "admin@example.test"}},
    })


@pytest.mark.asyncio
async def test_user_list_renders_with_current_template_api(admin_request, monkeypatch):
    monkeypatch.setattr(users, "run_in_threadpool", AsyncMock(return_value=([], None)))
    response = await users.user_list(admin_request, kc=None)
    assert response.status_code == 200
    assert admin_request.session["csrf_token"].encode() in response.body


@pytest.mark.asyncio
async def test_batch_upload_renders_with_current_template_api(admin_request):
    response = await users.batch_create_form(admin_request)
    assert response.status_code == 200
    assert b'name="csrf_token"' in response.body


@pytest.mark.asyncio
async def test_audit_page_has_csrf_template_helper(admin_request, monkeypatch):
    monkeypatch.setattr(audit.audit_service, "list_recent", lambda *args, **kwargs: [])
    response = await audit.audit_log_list(admin_request, db=None)
    assert response.status_code == 200
    assert admin_request.session["csrf_token"].encode() in response.body
