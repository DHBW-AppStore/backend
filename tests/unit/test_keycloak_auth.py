"""Unit tests for offline token validation in :mod:`app.utils.keycloak_auth`.

The forged-HS256 test is the reason this file exists. python-jose carries an
unfixed algorithm-confusion advisory (CVE-2026-85394) that only bites when the
caller does not pin algorithms; these tests pin the behaviour that makes it
unreachable, so a later refactor cannot quietly reopen it.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from app.utils import keycloak_auth

pytestmark = pytest.mark.unit


@pytest.fixture
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def private_pem(rsa_key):
    return rsa_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


@pytest.fixture
def public_pem(rsa_key):
    return rsa_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


@pytest.fixture(autouse=True)
def realm_key(monkeypatch, public_pem):
    """Stand in for the realm key, which is otherwise fetched from Keycloak."""
    monkeypatch.setattr(keycloak_auth, "_get_realm_public_key_pem", lambda: public_pem)


def _claims(**overrides) -> dict:
    now = int(time.time())
    return {"sub": "user-1", "iat": now, "exp": now + 300, **overrides}


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def test_valid_token_returns_its_claims(private_pem):
    token = jwt.encode(_claims(preferred_username="alice"), private_pem, algorithm="RS256")

    assert keycloak_auth.verify_keycloak_token_offline(token)["preferred_username"] == "alice"


def test_expired_token_is_rejected(private_pem):
    token = jwt.encode(_claims(exp=int(time.time()) - 10), private_pem, algorithm="RS256")

    with pytest.raises(HTTPException) as excinfo:
        keycloak_auth.verify_keycloak_token_offline(token)

    assert excinfo.value.status_code == 401


def test_token_signed_by_another_key_is_rejected(rsa_key):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    token = jwt.encode(_claims(), other, algorithm="RS256")

    with pytest.raises(HTTPException) as excinfo:
        keycloak_auth.verify_keycloak_token_offline(token)

    assert excinfo.value.status_code == 401


def test_hs256_forgery_is_rejected(public_pem):
    """The algorithm-confusion attack: HMAC the token with the public key as secret.

    Built by hand because PyJWT refuses to sign with an asymmetric key - which is
    exactly the guard python-jose fails to apply to DER-encoded keys.
    """
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps(_claims(preferred_username="attacker")).encode())
    signature = _b64url(
        hmac.new(public_pem.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    )

    with pytest.raises(HTTPException) as excinfo:
        keycloak_auth.verify_keycloak_token_offline(f"{header}.{payload}.{signature}")

    assert excinfo.value.status_code == 401
