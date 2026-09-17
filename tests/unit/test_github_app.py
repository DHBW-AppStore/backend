"""Unit tests for :mod:`app.services.github_app`."""
from __future__ import annotations

import base64
import io
import json
import urllib.error
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.services import github_app

pytestmark = pytest.mark.unit


def _unb64url(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("url", code, "error", {}, io.BytesIO())  # type: ignore[arg-type]


@pytest.fixture
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def pem(rsa_key):
    return rsa_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )


@pytest.fixture
def app_settings(pem):
    with patch("app.services.github_app.settings") as mock_settings:
        mock_settings.GITHUB_APP_ID = "4711"
        mock_settings.GITHUB_APP_PRIVATE_KEY = base64.b64encode(pem).decode()
        yield mock_settings


def test_needs_both_id_and_key(app_settings):
    assert github_app.is_configured()
    app_settings.GITHUB_APP_ID = ""
    assert not github_app.is_configured()


def test_accepts_raw_pem(app_settings, pem):
    app_settings.GITHUB_APP_PRIVATE_KEY = pem.decode()
    assert isinstance(github_app._load_private_key(), rsa.RSAPrivateKey)


def test_jwt_is_signed_with_the_app_key(app_settings, rsa_key):
    header, payload, signature = github_app._app_jwt().split(".")

    rsa_key.public_key().verify(
        _unb64url(signature), f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256()
    )
    assert json.loads(_unb64url(header)) == {"alg": "RS256", "typ": "JWT"}


def test_jwt_stays_within_githubs_ten_minute_limit(app_settings):
    claims = json.loads(_unb64url(github_app._app_jwt().split(".")[1]))

    assert claims["iss"] == "4711"
    assert claims["exp"] - claims["iat"] <= 600


def test_returns_token_for_installed_repo(app_settings):
    responses = [{"id": 42}, {"token": "ghs_abc"}]
    with patch.object(github_app, "_request", side_effect=responses) as req:
        assert github_app.installation_token("owner", "repo") == "ghs_abc"

    assert req.call_args_list[0].args[:2] == ("GET", "/repos/owner/repo/installation")
    assert req.call_args_list[1].args[:2] == ("POST", "/app/installations/42/access_tokens")


def test_returns_none_when_app_not_installed(app_settings):
    with patch.object(github_app, "_request", side_effect=_http_error(404)):
        assert github_app.installation_token("owner", "public-repo") is None


def test_other_errors_are_raised(app_settings):
    with (
        patch.object(github_app, "_request", side_effect=_http_error(401)),
        pytest.raises(urllib.error.HTTPError),
    ):
        github_app.installation_token("owner", "repo")


def test_install_url_comes_from_the_app_page(app_settings):
    app_page = {"html_url": "https://github.com/apps/dhbw-appstore"}
    with patch.object(github_app, "_request", return_value=app_page):
        assert github_app.install_url() == (
            "https://github.com/apps/dhbw-appstore/installations/new"
        )
