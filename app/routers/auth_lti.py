"""
LTI 1.3 Router — OIDC login initiation and launch callback for Moodle.

Mirrors the two-step flow of the throwaway lti-mock-server
(``lti-mock-server/src/ltimockserver/main.py``) but with real
signature/state/nonce validation (see app/utils/lti_auth.py) and a real
JIT-provisioned AppStore account instead of an inline HTML mockup.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.utils.lti_auth import (
    build_platform_auth_redirect,
    create_login_attempt,
    create_lti_session_token,
    sync_user_from_lti,
    verify_lti_launch,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ----------------------------------------------------------------
# OIDC THIRD-PARTY INITIATED LOGIN
# ----------------------------------------------------------------
@router.api_route("/login", methods=["GET", "POST"])
async def lti_login(request: Request):
    """Receives the OIDC login-initiation request from Moodle and redirects
    back to the platform's own auth endpoint with a freshly generated
    state/nonce pair (verified again on ``/lti/launch``)."""
    params = request.query_params if request.method == "GET" else await request.form()

    iss = params.get("iss")
    login_hint = params.get("login_hint")
    lti_message_hint = params.get("lti_message_hint")
    client_id = params.get("client_id")

    if not iss or not login_hint or not client_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing required OIDC login-initiation parameters",
        )
    if iss != settings.LTI_PLATFORM_ISSUER:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unknown LTI platform issuer",
        )

    redirect_uri = f"{settings.API_BASE_URL.rstrip('/')}/lti/launch"

    state, nonce = create_login_attempt()
    redirect_target = build_platform_auth_redirect(
        issuer=iss,
        login_hint=login_hint,
        client_id=client_id,
        redirect_uri=redirect_uri,
        lti_message_hint=lti_message_hint,
        state=state,
        nonce=nonce,
    )
    return RedirectResponse(url=redirect_target, status_code=status.HTTP_302_FOUND)


# ----------------------------------------------------------------
# LAUNCH CALLBACK
# ----------------------------------------------------------------
@router.post("/launch")
async def lti_launch(request: Request, db: Session = Depends(get_db)):
    """Receives the signed id_token launch from Moodle, verifies it, JIT
    provisions the local user, and redirects into the frontend with a
    short-lived backend-issued session token."""
    form = await request.form()
    id_token = form.get("id_token")
    state = form.get("state")

    if not id_token or not state:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="LTI launch missing id_token or state",
        )

    claims = verify_lti_launch(id_token, state=state)
    user = sync_user_from_lti(db, claims)
    session_token = create_lti_session_token(user)

    redirect_url = f"{settings.APP_BASE_URL}/lti/callback#token={session_token}"
    return RedirectResponse(url=redirect_url, status_code=status.HTTP_302_FOUND)
