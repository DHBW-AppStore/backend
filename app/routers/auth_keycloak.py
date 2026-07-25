"""
Auth Router - Keycloak Version

Serves the public auth health check so operators can probe the auth
subsystem without authenticating. "Who am I" lives at ``/users/me``.
"""
from fastapi import APIRouter

router = APIRouter()


# ----------------------------------------------------------------
# HEALTH CHECK
# ----------------------------------------------------------------
@router.get("/health")
def auth_health():
    """Check if auth service is healthy"""
    return {
        "status": "healthy",
        "auth_method": "keycloak",
        "message": "Authentication via Keycloak"
    }
