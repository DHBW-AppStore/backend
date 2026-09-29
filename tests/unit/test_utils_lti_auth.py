"""Unit-Tests für app.utils.lti_auth (Rollen-Mapping, Nonce-Replay, JIT-Sync,
JWKS-Caching, Launch-Verifikation, Session-Tokens, OIDC-Redirect)."""
from __future__ import annotations

import time
import uuid
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from jose import jwk, jwt

from app.config import settings
from app.crud.courses import get_course_by_lti_context_id
from app.models import CourseTeacher, User, UserRole
from app.utils import lti_auth
from app.utils.lti_auth import (
    LTI_CONTEXT_CLAIM,
    LTI_ROLES_CLAIM,
    build_platform_auth_redirect,
    consume_login_attempt,
    create_login_attempt,
    create_lti_session_token,
    get_current_user_lti,
    map_lti_roles_to_app_role,
    sync_user_from_lti,
    verify_lti_launch,
    verify_lti_session_token,
)

INSTRUCTOR_URN = "http://purl.imsglobal.org/vocab/lis/v2/membership#Instructor"
LEARNER_URN = "http://purl.imsglobal.org/vocab/lis/v2/membership#Learner"
TEST_KID = "test-kid-1"
TEST_ISSUER = "https://moodle.example"
TEST_CLIENT_ID = "client-123"


# ----------------------------------------------------------------
# role mapping
# ----------------------------------------------------------------
@pytest.mark.unit
def test_map_lti_roles_instructor_urn_maps_to_teacher():
    assert map_lti_roles_to_app_role([INSTRUCTOR_URN]) == UserRole.TEACHER


@pytest.mark.unit
def test_map_lti_roles_learner_urn_maps_to_student():
    assert map_lti_roles_to_app_role([LEARNER_URN]) == UserRole.STUDENT


@pytest.mark.unit
def test_map_lti_roles_empty_defaults_to_student():
    assert map_lti_roles_to_app_role([]) == UserRole.STUDENT


@pytest.mark.unit
def test_map_lti_roles_does_not_substring_match():
    """A role string merely containing 'Instructor' must not grant
    elevated access — only the exact IMS URN counts (the throwaway mock's
    ``"Instructor" in r`` check was unsafe)."""
    assert map_lti_roles_to_app_role(["SomeInstructorLookingButUnofficialRole"]) == UserRole.STUDENT


# ----------------------------------------------------------------
# nonce / state replay protection
# ----------------------------------------------------------------
# _fake_lti_redis_client is provided autouse by tests/conftest.py (shared
# with the router-level tests in tests/test_auth_lti_router.py, which also
# exercise create_login_attempt() indirectly via /lti/login).
@pytest.mark.unit
def test_login_attempt_is_single_use():
    state, nonce = create_login_attempt()
    assert consume_login_attempt(state, nonce) is True
    assert consume_login_attempt(state, nonce) is False


@pytest.mark.unit
def test_login_attempt_unknown_pair_rejected():
    assert consume_login_attempt("unknown-state", "unknown-nonce") is False


@pytest.mark.unit
def test_login_attempt_expired_pair_rejected(_fake_lti_redis_client):
    state, nonce = create_login_attempt()
    key = f"lti_login_attempt:{state}:{nonce}"
    value, _ = _fake_lti_redis_client._store[key]
    _fake_lti_redis_client._store[key] = (value, time.time() - 1)  # force expiry
    assert consume_login_attempt(state, nonce) is False


# ----------------------------------------------------------------
# sync_user_from_lti
# ----------------------------------------------------------------
def _claims(*, iss="https://moodle.example", sub="42", email, roles=None, context_id="course-1"):
    return {
        "iss": iss,
        "sub": sub,
        "email": email,
        "name": "Ada Lovelace",
        LTI_ROLES_CLAIM: roles or [LEARNER_URN],
        LTI_CONTEXT_CLAIM: {"id": context_id, "title": "Analytical Engines 101"},
    }


@pytest.mark.integration
def test_sync_user_from_lti_creates_new_user_and_course(db):
    claims = _claims(email="new.student@example.com")
    user = sync_user_from_lti(db, claims)

    assert user.email == "new.student@example.com"
    assert user.lti_iss == "https://moodle.example"
    assert user.lti_sub == "42"
    assert user.role == UserRole.STUDENT
    assert user.courseId is not None

    course = get_course_by_lti_context_id(db, "course-1")
    assert course is not None
    assert course.courseId == user.courseId


@pytest.mark.integration
def test_sync_user_from_lti_repeat_launch_matches_by_lti_identity(db):
    first = sync_user_from_lti(db, _claims(email="repeat@example.com", sub="99"))
    second = sync_user_from_lti(db, _claims(email="repeat@example.com", sub="99"))
    assert first.userId == second.userId


@pytest.mark.integration
def test_sync_user_from_lti_links_existing_keycloak_account_by_email(db):
    """An existing Keycloak-provisioned account with a matching email gets
    the LTI identity attached rather than a duplicate account being made."""
    existing = User(
        keycloak_id="kc-123",
        email="shared@example.com",
        username="shared",
        role=UserRole.STUDENT,
    )
    db.add(existing)
    db.commit()
    db.refresh(existing)

    claims = _claims(email="shared@example.com", sub="77")
    synced = sync_user_from_lti(db, claims)

    assert synced.userId == existing.userId
    assert synced.keycloak_id == "kc-123"
    assert synced.lti_iss == "https://moodle.example"
    assert synced.lti_sub == "77"


@pytest.mark.integration
def test_sync_user_from_lti_instructor_registered_as_course_teacher(db):
    user = sync_user_from_lti(
        db, _claims(email="prof@example.com", roles=[INSTRUCTOR_URN], context_id="course-2")
    )
    assert user.role == UserRole.TEACHER

    link = (
        db.query(CourseTeacher)
        .filter(
            CourseTeacher.courseId == user.courseId,
            CourseTeacher.userId == user.userId,
        )
        .first()
    )
    assert link is not None


@pytest.mark.integration
def test_sync_user_from_lti_rejects_missing_email(db):
    claims = _claims(email=None)
    with pytest.raises(HTTPException) as exc_info:
        sync_user_from_lti(db, claims)
    assert exc_info.value.status_code == 400


# ----------------------------------------------------------------
# JWKS fetch / cache
# ----------------------------------------------------------------
@pytest.fixture(autouse=True)
def _reset_jwks_cache():
    """Isolate the module-level JWKS cache between tests."""
    lti_auth._jwks_cache = None
    yield
    lti_auth._jwks_cache = None


def _fake_response(payload):
    return type("FakeResponse", (), {
        "json": lambda _self: payload,
        "raise_for_status": lambda _self: None,
    })()


@pytest.mark.unit
def test_get_jwks_fetches_once_and_caches():
    payload = {"keys": [{"kid": "k1"}]}
    with patch.object(lti_auth.requests, "get", return_value=_fake_response(payload)) as get_mock:
        first = lti_auth._get_jwks()
        second = lti_auth._get_jwks()
    assert first == payload
    assert second == payload
    get_mock.assert_called_once()


@pytest.mark.unit
def test_get_jwks_force_refresh_refetches():
    payload = {"keys": [{"kid": "k1"}]}
    with patch.object(lti_auth.requests, "get", return_value=_fake_response(payload)) as get_mock:
        lti_auth._get_jwks()
        lti_auth._get_jwks(force_refresh=True)
    assert get_mock.call_count == 2


@pytest.mark.unit
def test_fetch_jwks_sends_host_header_when_configured():
    payload = {"keys": []}
    with patch.object(settings, "LTI_PLATFORM_JWKS_HOST_HEADER", "moodle.internal"), \
         patch.object(lti_auth.requests, "get", return_value=_fake_response(payload)) as get_mock:
        lti_auth._fetch_jwks()
    _, kwargs = get_mock.call_args
    assert kwargs["headers"] == {"Host": "moodle.internal"}


@pytest.mark.unit
def test_fetch_jwks_network_failure_raises_502_not_bare_exception():
    with patch.object(
        lti_auth.requests, "get", side_effect=lti_auth.requests.ConnectionError("boom")
    ), pytest.raises(HTTPException) as exc_info:
        lti_auth._fetch_jwks()
    assert exc_info.value.status_code == 502


@pytest.mark.unit
def test_jwks_has_kid_true_and_false():
    jwks = {"keys": [{"kid": "a"}, {"kid": "b"}]}
    assert lti_auth._jwks_has_kid(jwks, "a") is True
    assert lti_auth._jwks_has_kid(jwks, "missing") is False
    assert lti_auth._jwks_has_kid(jwks, None) is False


# ----------------------------------------------------------------
# verify_lti_launch
# ----------------------------------------------------------------
@pytest.fixture(scope="module")
def _rsa_keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    public_jwk = jwk.construct(public_pem, algorithm="RS256").to_dict()
    public_jwk["kid"] = TEST_KID
    jwks = {"keys": [public_jwk]}
    return private_pem, jwks


def _sign_launch_token(private_pem, *, kid=TEST_KID, issuer=TEST_ISSUER,
                        audience=TEST_CLIENT_ID, nonce="nonce-1", exp_delta=3600,
                        deployment_id=None, extra_claims=None):
    now = int(time.time())
    claims = {
        "iss": issuer,
        "aud": audience,
        "sub": "moodle-user-1",
        "iat": now,
        "exp": now + exp_delta,
        "nonce": nonce,
    }
    if deployment_id is not None:
        claims[lti_auth.LTI_DEPLOYMENT_ID_CLAIM] = deployment_id
    if extra_claims:
        claims.update(extra_claims)
    headers = {"kid": kid} if kid else {}
    return jwt.encode(claims, private_pem, algorithm="RS256", headers=headers)


@pytest.mark.unit
def test_verify_lti_launch_happy_path(_rsa_keypair):
    private_pem, jwks = _rsa_keypair
    state, nonce = create_login_attempt()
    token = _sign_launch_token(private_pem, nonce=nonce)

    with patch.object(settings, "LTI_PLATFORM_ISSUER", TEST_ISSUER), \
         patch.object(settings, "LTI_CLIENT_ID", TEST_CLIENT_ID), \
         patch.object(settings, "LTI_DEPLOYMENT_ID", ""), \
         patch.object(lti_auth, "_get_jwks", return_value=jwks):
        claims = verify_lti_launch(token, state=state)

    assert claims["sub"] == "moodle-user-1"


@pytest.mark.unit
def test_verify_lti_launch_malformed_token_raises_401():
    with pytest.raises(HTTPException) as exc_info:
        verify_lti_launch("not-a-jwt", state="whatever")
    assert exc_info.value.status_code == 401


@pytest.mark.unit
def test_verify_lti_launch_signature_not_matching_jwks_raises_401(_rsa_keypair):
    """Token signed by a key the platform's JWKS doesn't actually contain
    (e.g. a rotated/unknown kid whose key material differs) must fail
    signature verification rather than being accepted."""
    _trusted_private_pem, jwks = _rsa_keypair
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_private_pem = other_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    state, nonce = create_login_attempt()
    token = _sign_launch_token(other_private_pem, kid=TEST_KID, nonce=nonce)

    with patch.object(settings, "LTI_PLATFORM_ISSUER", TEST_ISSUER), \
         patch.object(settings, "LTI_CLIENT_ID", TEST_CLIENT_ID), \
         patch.object(lti_auth, "_get_jwks", return_value=jwks), \
         pytest.raises(HTTPException) as exc_info:
        verify_lti_launch(token, state=state)
    assert exc_info.value.status_code == 401


@pytest.mark.unit
def test_verify_lti_launch_wrong_issuer_raises_401(_rsa_keypair):
    private_pem, jwks = _rsa_keypair
    state, nonce = create_login_attempt()
    token = _sign_launch_token(private_pem, issuer="https://not-moodle.example", nonce=nonce)

    with patch.object(settings, "LTI_PLATFORM_ISSUER", TEST_ISSUER), \
         patch.object(settings, "LTI_CLIENT_ID", TEST_CLIENT_ID), \
         patch.object(lti_auth, "_get_jwks", return_value=jwks), \
         pytest.raises(HTTPException) as exc_info:
        verify_lti_launch(token, state=state)
    assert exc_info.value.status_code == 401


@pytest.mark.unit
def test_verify_lti_launch_expired_token_raises_401(_rsa_keypair):
    private_pem, jwks = _rsa_keypair
    state, nonce = create_login_attempt()
    token = _sign_launch_token(private_pem, nonce=nonce, exp_delta=-10)

    with patch.object(settings, "LTI_PLATFORM_ISSUER", TEST_ISSUER), \
         patch.object(settings, "LTI_CLIENT_ID", TEST_CLIENT_ID), \
         patch.object(lti_auth, "_get_jwks", return_value=jwks), \
         pytest.raises(HTTPException) as exc_info:
        verify_lti_launch(token, state=state)
    assert exc_info.value.status_code == 401


@pytest.mark.unit
def test_verify_lti_launch_unknown_nonce_raises_401(_rsa_keypair):
    private_pem, jwks = _rsa_keypair
    token = _sign_launch_token(private_pem, nonce="never-issued")

    with patch.object(settings, "LTI_PLATFORM_ISSUER", TEST_ISSUER), \
         patch.object(settings, "LTI_CLIENT_ID", TEST_CLIENT_ID), \
         patch.object(lti_auth, "_get_jwks", return_value=jwks), \
         pytest.raises(HTTPException) as exc_info:
        verify_lti_launch(token, state="some-state")
    assert exc_info.value.status_code == 401


@pytest.mark.unit
def test_verify_lti_launch_deployment_id_mismatch_raises_401(_rsa_keypair):
    private_pem, jwks = _rsa_keypair
    state, nonce = create_login_attempt()
    token = _sign_launch_token(private_pem, nonce=nonce, deployment_id="deploy-a")

    with patch.object(settings, "LTI_PLATFORM_ISSUER", TEST_ISSUER), \
         patch.object(settings, "LTI_CLIENT_ID", TEST_CLIENT_ID), \
         patch.object(settings, "LTI_DEPLOYMENT_ID", "deploy-b"), \
         patch.object(lti_auth, "_get_jwks", return_value=jwks), \
         pytest.raises(HTTPException) as exc_info:
        verify_lti_launch(token, state=state)
    assert exc_info.value.status_code == 401


# ----------------------------------------------------------------
# session tokens
# ----------------------------------------------------------------
@pytest.mark.unit
def test_create_and_verify_lti_session_token_roundtrip(db):
    user = User(
        userId=uuid.uuid4(),
        email="session-user@example.com",
        username="session-user",
        role=UserRole.STUDENT,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    with patch.object(settings, "LTI_SESSION_SECRET", "test-secret"):
        token = create_lti_session_token(user)
        payload = verify_lti_session_token(token)

    assert payload["sub"] == str(user.userId)
    assert payload["typ"] == "lti-session"


@pytest.mark.unit
def test_verify_lti_session_token_invalid_signature_raises_401():
    with patch.object(settings, "LTI_SESSION_SECRET", "secret-a"):
        token = jwt.encode(
            {"sub": "x", "typ": "lti-session", "iat": int(time.time()), "exp": int(time.time()) + 60},
            "secret-b",
            algorithm="HS256",
        )
        with pytest.raises(HTTPException) as exc_info:
            verify_lti_session_token(token)
    assert exc_info.value.status_code == 401


@pytest.mark.unit
def test_verify_lti_session_token_missing_typ_raises_401():
    with patch.object(settings, "LTI_SESSION_SECRET", "test-secret"):
        token = jwt.encode(
            {"sub": "x", "iat": int(time.time()), "exp": int(time.time()) + 60},
            "test-secret",
            algorithm="HS256",
        )
        with pytest.raises(HTTPException) as exc_info:
            verify_lti_session_token(token)
    assert exc_info.value.status_code == 401


@pytest.mark.unit
def test_get_current_user_lti_returns_user(db):
    user = User(
        userId=uuid.uuid4(),
        email="current-lti-user@example.com",
        username="current-lti-user",
        role=UserRole.STUDENT,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    with patch.object(settings, "LTI_SESSION_SECRET", "test-secret"):
        token = create_lti_session_token(user)
        creds = type("Creds", (), {"credentials": token})()
        result = get_current_user_lti(credentials=creds, db=db)

    assert result.userId == user.userId


@pytest.mark.unit
def test_get_current_user_lti_wrong_typ_raises_401(db):
    with patch.object(settings, "LTI_SESSION_SECRET", "test-secret"):
        token = jwt.encode(
            {"sub": "x", "typ": "keycloak", "iat": int(time.time()), "exp": int(time.time()) + 60},
            "test-secret",
            algorithm="HS256",
        )
        creds = type("Creds", (), {"credentials": token})()
        with pytest.raises(HTTPException) as exc_info:
            get_current_user_lti(credentials=creds, db=db)
    assert exc_info.value.status_code == 401


@pytest.mark.unit
def test_get_current_user_lti_deleted_user_raises_401(db):
    with patch.object(settings, "LTI_SESSION_SECRET", "test-secret"):
        token = jwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "typ": "lti-session",
                "iat": int(time.time()),
                "exp": int(time.time()) + 60,
            },
            "test-secret",
            algorithm="HS256",
        )
        creds = type("Creds", (), {"credentials": token})()
        with pytest.raises(HTTPException) as exc_info:
            get_current_user_lti(credentials=creds, db=db)
    assert exc_info.value.status_code == 401


# ----------------------------------------------------------------
# build_platform_auth_redirect
# ----------------------------------------------------------------
@pytest.mark.unit
def test_build_platform_auth_redirect_contains_expected_params():
    url = build_platform_auth_redirect(
        issuer="https://moodle.example",
        login_hint="hint-1",
        client_id="client-1",
        redirect_uri="https://backend.example/lti/launch",
        lti_message_hint="msg-hint",
        state="state-1",
        nonce="nonce-1",
    )
    parsed = urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "moodle.example"
    assert parsed.path == "/mod/lti/auth.php"

    qs = parse_qs(parsed.query)
    assert qs["response_type"] == ["id_token"]
    assert qs["scope"] == ["openid"]
    assert qs["client_id"] == ["client-1"]
    assert qs["redirect_uri"] == ["https://backend.example/lti/launch"]
    assert qs["login_hint"] == ["hint-1"]
    assert qs["state"] == ["state-1"]
    assert qs["nonce"] == ["nonce-1"]
    assert qs["response_mode"] == ["form_post"]
    assert qs["prompt"] == ["none"]
    assert qs["lti_message_hint"] == ["msg-hint"]


@pytest.mark.unit
def test_build_platform_auth_redirect_omits_message_hint_when_absent():
    url = build_platform_auth_redirect(
        issuer="https://moodle.example",
        login_hint="hint-1",
        client_id="client-1",
        redirect_uri="https://backend.example/lti/launch",
        lti_message_hint=None,
        state="state-1",
        nonce="nonce-1",
    )
    qs = parse_qs(urlparse(url).query)
    assert "lti_message_hint" not in qs
