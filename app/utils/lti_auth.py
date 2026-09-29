"""
LTI 1.3 Authentication & Just-in-Time Provisioning
Verifies Moodle LTI launches and syncs the launching user into the local
``users`` table, mirroring the Keycloak JIT pattern in
:mod:`app.utils.keycloak_auth`.
"""
import logging
import secrets
import threading
import time
from urllib.parse import urlencode

import redis
import requests
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.config import settings
from app.crud.courses import get_or_create_course_by_lti_context
from app.crud.users import get_user_by_email
from app.database import get_db
from app.models import CourseTeacher, User, UserRole

logger = logging.getLogger(__name__)

security = HTTPBearer()

LTI_ROLES_CLAIM = "https://purl.imsglobal.org/spec/lti/claim/roles"
LTI_CONTEXT_CLAIM = "https://purl.imsglobal.org/spec/lti/claim/context"
LTI_DEPLOYMENT_ID_CLAIM = "https://purl.imsglobal.org/spec/lti/claim/deployment_id"
LTI_MESSAGE_TYPE_CLAIM = "https://purl.imsglobal.org/spec/lti/claim/message_type"

# Exact IMS role vocabulary URNs. Substring matching (as the throwaway
# mock did) is unsafe — an unrelated role string containing "Instructor"
# would otherwise grant elevated access.
INSTRUCTOR_ROLE_URNS = {
    "http://purl.imsglobal.org/vocab/lis/v2/membership#Instructor",
    "http://purl.imsglobal.org/vocab/lis/v2/institution/person#Instructor",
}
ADMIN_ROLE_URNS = {
    "http://purl.imsglobal.org/vocab/lis/v2/system/person#Administrator",
    "http://purl.imsglobal.org/vocab/lis/v2/institution/person#Administrator",
}


# ----------------------------------------------------------------
# NONCE / STATE STORE (OIDC replay protection)
# ----------------------------------------------------------------
# Shared across processes via Redis — uvicorn runs multiple workers
# (see docker-compose.staging.yml), and Moodle's /lti/login and
# /lti/launch requests can land on different ones, so an in-memory
# dict here would randomly 401 launches that hit a worker that never
# saw the matching /lti/login call.
_LOGIN_ATTEMPT_TTL_SECONDS = 300
_redis_client: redis.Redis | None = None
_redis_lock = threading.Lock()


def _get_redis_client() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        with _redis_lock:
            if _redis_client is None:
                _redis_client = redis.Redis.from_url(
                    settings.LTI_NONCE_REDIS_URL, decode_responses=True
                )
    return _redis_client


def create_login_attempt() -> tuple[str, str]:
    """Generate and record a fresh (state, nonce) pair for one login attempt."""
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    key = f"lti_login_attempt:{state}:{nonce}"
    _get_redis_client().setex(key, _LOGIN_ATTEMPT_TTL_SECONDS, "1")
    return state, nonce


def consume_login_attempt(state: str, nonce: str) -> bool:
    """Verify a (state, nonce) pair was issued by us and not yet used.

    Single-use: a valid pair is removed on success (atomically, via
    GETDEL), so a replayed launch with the same values fails the second
    time — including when the replay lands on a different worker.
    """
    key = f"lti_login_attempt:{state}:{nonce}"
    return _get_redis_client().getdel(key) is not None


# ----------------------------------------------------------------
# PLATFORM JWKS (Moodle's signing keys)
# ----------------------------------------------------------------
# Same fetch-and-cache spirit as ``_get_realm_public_key_pem`` in
# keycloak_auth.py: the JWKS is fetched once and cached for the process
# lifetime. Re-fetched on an unknown ``kid`` so a Moodle key rotation is
# picked up without a restart.
_jwks_cache: dict | None = None
_jwks_lock = threading.Lock()


def _fetch_jwks() -> dict:
    headers = {}
    if settings.LTI_PLATFORM_JWKS_HOST_HEADER:
        headers["Host"] = settings.LTI_PLATFORM_JWKS_HOST_HEADER
    try:
        resp = requests.get(settings.LTI_PLATFORM_JWKS_URL, headers=headers, timeout=5)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        logger.error("Failed to fetch LTI platform JWKS from %s: %s", settings.LTI_PLATFORM_JWKS_URL, e)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not reach the LTI platform's key endpoint to verify the launch",
        )


def _get_jwks(*, force_refresh: bool = False) -> dict:
    global _jwks_cache
    if _jwks_cache is not None and not force_refresh:
        return _jwks_cache
    with _jwks_lock:
        if _jwks_cache is None or force_refresh:
            _jwks_cache = _fetch_jwks()
        return _jwks_cache


def _jwks_has_kid(jwks: dict, kid: str | None) -> bool:
    if not kid:
        return False
    return any(k.get("kid") == kid for k in jwks.get("keys", []))


# ----------------------------------------------------------------
# LAUNCH VALIDATION
# ----------------------------------------------------------------
def verify_lti_launch(id_token: str, state: str) -> dict:
    """Validate a Moodle LTI 1.3 launch ``id_token`` and return its claims.

    Checks, in order: signature (via Moodle's JWKS), issuer, audience
    (client_id), expiry, and that ``(state, nonce)`` matches a login
    attempt we actually issued and hasn't been consumed yet (CSRF +
    single-use replay protection combined — the state alone is
    meaningless without the nonce it was paired with at ``/lti/login``).
    Raises 401 on any failure — never returns claims from an unverified
    or replayed token.
    """
    try:
        unverified_header = jwt.get_unverified_header(id_token)
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Malformed LTI launch token: {e}",
        )

    kid = unverified_header.get("kid")
    jwks = _get_jwks()
    if not _jwks_has_kid(jwks, kid):
        # Moodle may have rotated its signing key; refresh once before
        # giving up, same as the Keycloak path re-fetches on cache miss.
        jwks = _get_jwks(force_refresh=True)

    try:
        claims = jwt.decode(
            id_token,
            jwks,
            algorithms=["RS256"],
            issuer=settings.LTI_PLATFORM_ISSUER,
            audience=settings.LTI_CLIENT_ID,
            options={
                "require_exp": True,
                "require_iat": True,
                "require_sub": True,
                "require_aud": True,
                "require_iss": True,
            },
        )
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid LTI launch token: {e}",
        )

    nonce = claims.get("nonce")
    if not nonce or not consume_login_attempt(state, nonce):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="LTI launch state/nonce missing, unknown, or already used",
        )

    deployment_id = claims.get(LTI_DEPLOYMENT_ID_CLAIM)
    if settings.LTI_DEPLOYMENT_ID and deployment_id != settings.LTI_DEPLOYMENT_ID:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="LTI launch deployment_id does not match the registered tool deployment",
        )

    return claims


# ----------------------------------------------------------------
# ROLE MAPPING
# ----------------------------------------------------------------
def map_lti_roles_to_app_role(lti_roles: list[str]) -> UserRole:
    """Map IMS LTI role URNs to the app's role enum. Priority: admin > teacher > student."""
    roles = set(lti_roles or [])
    if roles & ADMIN_ROLE_URNS:
        return UserRole.ADMIN
    if roles & INSTRUCTOR_ROLE_URNS:
        return UserRole.TEACHER
    return UserRole.STUDENT


# ----------------------------------------------------------------
# USER SYNC (Just-in-Time Provisioning)
# ----------------------------------------------------------------
def sync_user_from_lti(db: Session, lti_claims: dict) -> User:
    """Synchronize the launching Moodle user into the local DB.

    Match order:
      1. By email — links the LTI identity onto an existing account
         (e.g. one already provisioned via Keycloak) rather than
         creating a duplicate. The email claim is only ever read from a
         signature-verified token (see ``verify_lti_launch``), so it's
         safe to trust for this lookup.
      2. By (lti_iss, lti_sub) — repeat launches from the same Moodle
         user once no email match exists.
      3. Otherwise, create a new user.

    Also resolves/creates the Course from the LTI context claim and
    updates course-teacher membership for instructors.
    """
    iss = lti_claims.get("iss")
    sub = lti_claims.get("sub")
    email = lti_claims.get("email")
    if not email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="LTI launch has no email claim; refusing to provision.",
        )

    lti_roles = lti_claims.get(LTI_ROLES_CLAIM, [])
    app_role = map_lti_roles_to_app_role(lti_roles)
    name = lti_claims.get("name")
    given_name = lti_claims.get("given_name")
    family_name = lti_claims.get("family_name")
    username = lti_claims.get("preferred_username") or email

    user = get_user_by_email(db, email)
    if not user:
        user = (
            db.query(User)
            .filter(User.lti_iss == iss, User.lti_sub == sub)
            .first()
        )

    course = None
    context = lti_claims.get(LTI_CONTEXT_CLAIM) or {}
    context_id = context.get("id")
    if context_id:
        course = get_or_create_course_by_lti_context(
            db, context_id, context.get("title") or context.get("label") or context_id
        )

    if not user:
        user = User(
            lti_iss=iss,
            lti_sub=sub,
            email=email,
            username=username,
            role=app_role,
            firstName=given_name or name,
            lastName=family_name,
            courseId=course.courseId if course else None,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    else:
        updated = False
        if user.lti_iss != iss or user.lti_sub != sub:
            user.lti_iss = iss
            user.lti_sub = sub
            updated = True
        if user.role != app_role:
            user.role = app_role
            updated = True
        if course and user.courseId != course.courseId:
            user.courseId = course.courseId
            updated = True
        if updated:
            db.commit()
            db.refresh(user)

    if course and app_role == UserRole.TEACHER:
        already_teacher = (
            db.query(CourseTeacher)
            .filter(
                CourseTeacher.courseId == course.courseId,
                CourseTeacher.userId == user.userId,
            )
            .first()
        )
        if not already_teacher:
            db.add(CourseTeacher(courseId=course.courseId, userId=user.userId))
            db.commit()

    return user


# ----------------------------------------------------------------
# BACKEND-ISSUED SESSION TOKEN
# ----------------------------------------------------------------
# LTI users bypass Keycloak entirely, so the launch mints its own
# short-lived bearer token instead of a Keycloak-shaped one. The
# frontend's existing Authorization: Bearer plumbing treats it the same
# way. ``typ: lti-session`` distinguishes it from a Keycloak token in
# app.utils.permissions.get_current_user.
def create_lti_session_token(user: User) -> str:
    now = int(time.time())
    payload = {
        "sub": str(user.userId),
        "typ": "lti-session",
        "iat": now,
        "exp": now + settings.LTI_SESSION_TOKEN_TTL_SECONDS,
    }
    return jwt.encode(payload, settings.LTI_SESSION_SECRET, algorithm="HS256")


def verify_lti_session_token(token: str) -> dict:
    try:
        payload = jwt.decode(
            token,
            settings.LTI_SESSION_SECRET,
            algorithms=["HS256"],
            options={"require_exp": True, "require_iat": True, "require_sub": True},
        )
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid LTI session token: {e}",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if "typ" not in payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid LTI session token: missing typ claim",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return payload


def get_current_user_lti(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    """Validate a backend-issued LTI session bearer token and return the local User."""
    payload = verify_lti_session_token(credentials.credentials)
    if payload.get("typ") != "lti-session":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not an LTI session token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = db.query(User).filter(User.userId == payload["sub"]).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="LTI session refers to a user that no longer exists",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


# ----------------------------------------------------------------
# OIDC LOGIN INITIATION HELPER
# ----------------------------------------------------------------
def build_platform_auth_redirect(
    *, issuer: str, login_hint: str, client_id: str, redirect_uri: str,
    lti_message_hint: str | None, state: str, nonce: str,
) -> str:
    """Build the redirect URL to Moodle's ``mod/lti/auth.php`` for the OIDC
    third-party-initiated login step, with a real per-request state/nonce
    (unlike the throwaway mock's hardcoded constants)."""
    auth_params = {
        "response_type": "id_token",
        "scope": "openid",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "login_hint": login_hint,
        "state": state,
        "nonce": nonce,
        "response_mode": "form_post",
        "prompt": "none",
    }
    if lti_message_hint:
        auth_params["lti_message_hint"] = lti_message_hint
    return f"{issuer}/mod/lti/auth.php?{urlencode(auth_params)}"
