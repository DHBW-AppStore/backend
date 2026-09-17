"""Installation tokens for a GitHub App, used in place of a personal access token.

Kept in step with ``worker/app/services/github_app.py``; ``install_url`` is backend-only.
"""

import base64
import json
import time
import urllib.error
import urllib.request
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from ..config import settings

API_URL = "https://api.github.com"


def is_configured() -> bool:
    return bool(settings.GITHUB_APP_ID and settings.GITHUB_APP_PRIVATE_KEY)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _load_private_key() -> rsa.RSAPrivateKey:
    # A raw PEM is accepted too, which is easier when testing locally.
    raw = settings.GITHUB_APP_PRIVATE_KEY.strip()
    pem = raw.encode() if raw.startswith("-----BEGIN") else base64.b64decode(raw)
    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError("GITHUB_APP_PRIVATE_KEY is not an RSA key")
    return key


def _app_jwt() -> str:
    # GitHub caps the lifetime at ten minutes; iat is backdated for clock skew.
    now = int(time.time())
    claims = {"iat": now - 60, "exp": now + 540, "iss": settings.GITHUB_APP_ID}
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps(claims).encode())
    signature = _load_private_key().sign(
        f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256()
    )
    return f"{header}.{payload}.{_b64url(signature)}"


def _request(method: str, path: str, app_jwt: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{API_URL}{path}",
        method=method,
        headers={
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        data: dict[str, Any] = json.load(response)
    return data


def installation_token(owner: str, repo: str) -> str | None:
    """Return a one-hour token for ``owner/repo``, or None if the App is not installed there."""
    app_jwt = _app_jwt()
    try:
        installation = _request("GET", f"/repos/{owner}/{repo}/installation", app_jwt)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    token = _request("POST", f"/app/installations/{installation['id']}/access_tokens", app_jwt)
    return str(token["token"])


def install_url() -> str:
    """Page where a repository owner installs this App."""
    app = _request("GET", "/app", _app_jwt())
    return f"{app['html_url']}/installations/new"
