"""Tests for ``app/routers/auth_lti.py`` — the OIDC login-initiation and
launch-callback endpoints for Moodle LTI 1.3.

Unlike the rest of the API these endpoints are unauthenticated (they
*establish* a session rather than requiring one), so they only need the
``get_db`` override, not ``get_current_user``. ``verify_lti_launch`` /
``sync_user_from_lti`` are exercised directly in
``tests/unit/test_utils_lti_auth.py``; here they're patched so the
launch-callback tests focus purely on the router's request-parsing and
redirect-building contract.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import get_db
from app.main import app
from app.models import User, UserRole

TEST_ISSUER = "https://moodle.example"


@pytest.fixture
def lti_client(db):
    TestingSessionLocal = sessionmaker(bind=db.get_bind())

    def override_get_db():
        session = TestingSessionLocal()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, follow_redirects=False) as c:
        yield c
    app.dependency_overrides.clear()


# ----------------------------------------------------------------
# GET/POST /lti/login
# ----------------------------------------------------------------
@pytest.mark.unit
def test_login_get_happy_path_redirects_to_platform(lti_client):
    with patch.object(settings, "LTI_PLATFORM_ISSUER", TEST_ISSUER), \
         patch.object(settings, "API_BASE_URL", "https://appstore.example/api"):
        resp = lti_client.get(
            "/lti/login",
            params={"iss": TEST_ISSUER, "login_hint": "hint-1", "client_id": "client-1"},
        )
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith(f"{TEST_ISSUER}/mod/lti/auth.php?")
    assert "state=" in location
    assert "nonce=" in location
    assert "redirect_uri=https%3A%2F%2Fappstore.example%2Fapi%2Flti%2Flaunch" in location


@pytest.mark.unit
def test_login_post_form_happy_path_redirects_to_platform(lti_client):
    with patch.object(settings, "LTI_PLATFORM_ISSUER", TEST_ISSUER), \
         patch.object(settings, "API_BASE_URL", "https://appstore.example/api"):
        resp = lti_client.post(
            "/lti/login",
            data={"iss": TEST_ISSUER, "login_hint": "hint-1", "client_id": "client-1"},
        )
    assert resp.status_code == 302
    assert resp.headers["location"].startswith(f"{TEST_ISSUER}/mod/lti/auth.php?")


@pytest.mark.unit
@pytest.mark.parametrize("missing", ["iss", "login_hint", "client_id"])
def test_login_missing_required_param_returns_400(lti_client, missing):
    params = {"iss": TEST_ISSUER, "login_hint": "hint-1", "client_id": "client-1"}
    del params[missing]
    with patch.object(settings, "LTI_PLATFORM_ISSUER", TEST_ISSUER):
        resp = lti_client.get("/lti/login", params=params)
    assert resp.status_code == 400


@pytest.mark.unit
def test_login_unknown_issuer_returns_400(lti_client):
    with patch.object(settings, "LTI_PLATFORM_ISSUER", TEST_ISSUER):
        resp = lti_client.get(
            "/lti/login",
            params={"iss": "https://not-moodle.example", "login_hint": "h", "client_id": "c"},
        )
    assert resp.status_code == 400


# ----------------------------------------------------------------
# POST /lti/launch
# ----------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.parametrize("missing", ["id_token", "state"])
def test_launch_missing_field_returns_400(lti_client, missing):
    data = {"id_token": "token-value", "state": "state-value"}
    del data[missing]
    resp = lti_client.post("/lti/launch", data=data)
    assert resp.status_code == 400


@pytest.mark.unit
def test_launch_happy_path_redirects_with_session_token(lti_client, db):
    user = User(
        email="launch-user@example.com",
        username="launch-user",
        role=UserRole.STUDENT,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    with patch("app.routers.auth_lti.verify_lti_launch", return_value={"sub": "1"}), \
         patch("app.routers.auth_lti.sync_user_from_lti", return_value=user), \
         patch("app.routers.auth_lti.create_lti_session_token", return_value="fake-session-token"), \
         patch.object(settings, "APP_BASE_URL", "https://frontend.example"):
        resp = lti_client.post(
            "/lti/launch", data={"id_token": "signed-token", "state": "state-value"}
        )

    assert resp.status_code == 302
    assert resp.headers["location"] == "https://frontend.example/lti/callback#token=fake-session-token"
