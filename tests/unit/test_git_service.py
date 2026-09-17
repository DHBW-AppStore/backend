"""Unit tests for :mod:`app.services.git_service`.

DB-less Tests: GitPython wird vollständig gemockt; es passieren keine
Netzwerk- oder Dateisystem-Operationen jenseits eines tmp_path. Diese
Tests sichern das Verhalten von ``clone_release_vars`` in den vier
Phase-C4-Szenarien ab:

1. Shallow Fetch eines konkreten Tags
2. Verhalten bei nicht-HTTPS (Schema-)URLs
3. Tag nicht auffindbar im Remote
4. Authentifizierungs-Fehler werden propagiert

Wir patchen ``git.Repo.init`` direkt (statt der ``git_service``-Symbole),
weil der Service ``import git`` macht und ``git.Repo.init`` per
Attribut auflöst. ``autospec`` deaktivieren wir, da ``git.Repo``
dynamische Properties wie ``head.commit`` exponiert, die das echte
Spec-Modell nur umständlich nachbildet — die Tests kontrollieren die
Mock-Surface explizit über ``MagicMock``-Konfiguration.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.git_service import GitService

pytestmark = pytest.mark.unit


# ----------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------
def _make_repo_mock() -> MagicMock:
    """Build a ``git.Repo``-shaped MagicMock with the surface the
    service touches: ``create_remote``, ``head.commit.hexsha`` and the
    ``git.checkout`` shortcut. Caller mutates ``origin.fetch`` to
    inject error scenarios."""
    repo = MagicMock(name="Repo")
    origin = MagicMock(name="Origin")
    repo.create_remote.return_value = origin
    repo.head.commit.hexsha = "deadbeefcafebabe"
    repo.git.checkout = MagicMock(name="checkout")
    return repo


def _service(tmp_path, monkeypatch) -> GitService:
    """Instantiate the service with a redirected temp base path and a
    deterministic access token. Avoids relying on the real
    ``settings`` singleton."""
    svc = GitService()
    monkeypatch.setattr(svc, "base_path", tmp_path)
    monkeypatch.setattr(svc, "token", "test-token-xyz")
    return svc


# ----------------------------------------------------------------
# Tests
# ----------------------------------------------------------------
@patch("app.services.git_service.git.Repo")
def test_clone_repository_at_tag_uses_shallow_fetch(
    mock_repo_cls, tmp_path, monkeypatch
):
    """``clone_release_vars`` muss einen Shallow-Fetch (``depth=1``)
    auf ``refs/tags/<tag>`` ausführen und anschließend genau diesen
    Tag auschecken — sonst ist der Sparse-Checkout sinnlos großvolumig."""
    repo = _make_repo_mock()
    mock_repo_cls.init.return_value = repo

    svc = _service(tmp_path, monkeypatch)
    result_path = svc.clone_release_vars(
        git_url="https://github.com/acme/widgets.git",
        tag="v1.2.3",
        deployment_id="dep-001",
    )

    # Result path liegt unter base_path
    assert result_path.startswith(str(tmp_path))

    # Repo wurde initialisiert (nicht clone-d), Sparse-Setup folgt
    mock_repo_cls.init.assert_called_once()

    # Remote-URL trägt den Token (HTTPS-Authentifizierung)
    repo.create_remote.assert_called_once()
    remote_args = repo.create_remote.call_args
    assert remote_args.args[0] == "origin"
    assert "test-token-xyz@github.com/acme/widgets.git" in remote_args.args[1]

    # Fetch ist shallow und gezielt auf den Tag
    origin = repo.create_remote.return_value
    origin.fetch.assert_called_once_with(
        refspec="refs/tags/v1.2.3:refs/tags/v1.2.3", depth=1
    )

    # Checkout des Tag-Refs mit force=True
    repo.git.checkout.assert_called_once_with("refs/tags/v1.2.3", force=True)


@patch("app.services.git_service.git.Repo")
def test_clone_repository_rejects_non_https_url(
    mock_repo_cls, tmp_path, monkeypatch
):
    """Eine nicht-HTTPS-URL (hier: ``ftp://``) lässt den Fetch
    fehlschlagen — der Service muss den Fehler in einen
    ``"Failed to clone"``-Exception kapseln und das angelegte
    Repo-Verzeichnis aufräumen."""
    repo = _make_repo_mock()
    mock_repo_cls.init.return_value = repo
    origin = repo.create_remote.return_value
    # GitPython hebt bei unbekanntem Schema einen GitCommandError —
    # wir simulieren das mit einem RuntimeError, da der Service
    # ohnehin breit Exception fängt und neu wirft.
    origin.fetch.side_effect = RuntimeError(
        "fatal: unsupported protocol: ftp"
    )

    svc = _service(tmp_path, monkeypatch)

    with pytest.raises(Exception) as excinfo:
        svc.clone_release_vars(
            git_url="ftp://example.com/acme/widgets.git",
            tag="v1.0.0",
            deployment_id="dep-002",
        )

    assert "Failed to clone" in str(excinfo.value)
    # Cleanup: das Repo-Verzeichnis darf nach Fehler nicht zurückbleiben
    assert not (tmp_path / "deploy_dep-002").exists()


@patch("app.services.git_service.git.Repo")
def test_clone_repository_tag_not_found_raises(
    mock_repo_cls, tmp_path, monkeypatch
):
    """Existiert der angeforderte Tag im Remote nicht, propagiert
    GitPython einen Fetch-Fehler; der Service muss ihn als
    ``"Failed to clone"`` umverpacken und nicht stillschweigend
    weiterlaufen (sonst würde ein leeres Repo durchgereicht)."""
    repo = _make_repo_mock()
    mock_repo_cls.init.return_value = repo
    origin = repo.create_remote.return_value
    origin.fetch.side_effect = RuntimeError(
        "fatal: couldn't find remote ref refs/tags/v9.9.9"
    )

    svc = _service(tmp_path, monkeypatch)

    with pytest.raises(Exception) as excinfo:
        svc.clone_release_vars(
            git_url="https://github.com/acme/widgets.git",
            tag="v9.9.9",
            deployment_id="dep-003",
        )

    msg = str(excinfo.value)
    assert "Failed to clone" in msg
    assert "v9.9.9" in msg or "couldn't find remote ref" in msg

    # Checkout darf nicht mehr aufgerufen worden sein — Fetch hat ja
    # bereits abgebrochen
    repo.git.checkout.assert_not_called()


@patch("app.services.git_service.git.Repo")
def test_clone_repository_authentication_failure_propagates(
    mock_repo_cls, tmp_path, monkeypatch
):
    """Bei abgelaufenem oder falschem Token wirft GitPython eine
    Authentifizierungs-Exception. Der Service muss diese in seine
    ``Failed to clone``-Hülle einpacken, dabei aber die Original-
    Ursache via ``__cause__`` durchreichen (``raise ... from e``),
    damit die Logs das Auth-Problem nachvollziehen können."""
    repo = _make_repo_mock()
    mock_repo_cls.init.return_value = repo
    origin = repo.create_remote.return_value
    auth_error = RuntimeError(
        "fatal: Authentication failed for 'https://github.com/acme/widgets.git/'"
    )
    origin.fetch.side_effect = auth_error

    svc = _service(tmp_path, monkeypatch)

    with pytest.raises(Exception) as excinfo:
        svc.clone_release_vars(
            git_url="https://github.com/acme/widgets.git",
            tag="v1.0.0",
            deployment_id="dep-004",
        )

    # Wrapped error mentions clone failure
    assert "Failed to clone" in str(excinfo.value)
    # Original cause ist via __cause__ erreichbar (raise ... from e)
    assert excinfo.value.__cause__ is auth_error
    assert "Authentication failed" in str(excinfo.value.__cause__)


# ----------------------------------------------------------------
# GitHub App
# ----------------------------------------------------------------
def _response(status: int, body=None) -> MagicMock:
    resp = MagicMock(status_code=status)
    resp.json.return_value = body if body is not None else []
    return resp


@patch("app.services.git_service.github_app")
def test_clone_url_uses_installation_token(mock_app, tmp_path, monkeypatch):
    """Ist die App installiert, trägt die URL den festen Nutzer ``x-access-token``."""
    mock_app.is_configured.return_value = True
    mock_app.installation_token.return_value = "ghs_abc"
    svc = _service(tmp_path, monkeypatch)

    url = svc._get_authenticated_url("git@github.com:acme/widgets.git")

    assert url == "https://x-access-token:ghs_abc@github.com/acme/widgets.git"


@patch("app.services.git_service.github_app")
def test_clone_url_falls_back_to_token_when_app_not_installed(mock_app, tmp_path, monkeypatch):
    """Ohne Installation bleibt es beim bisherigen ``GIT_ACCESS_TOKEN``."""
    mock_app.is_configured.return_value = True
    mock_app.installation_token.return_value = None
    svc = _service(tmp_path, monkeypatch)

    url = svc._get_authenticated_url("https://github.com/acme/widgets.git")

    assert url == "https://test-token-xyz@github.com/acme/widgets.git"


@patch("app.services.git_service.github_app")
def test_other_hosts_never_ask_the_app(mock_app, tmp_path, monkeypatch):
    mock_app.is_configured.return_value = True
    svc = _service(tmp_path, monkeypatch)

    url = svc._get_authenticated_url("https://gitlab.com/group/project.git")

    assert url == "https://test-token-xyz@gitlab.com/group/project.git"
    mock_app.installation_token.assert_not_called()


@patch("app.services.git_service.github_app")
def test_versions_use_one_token_for_tags_and_releases(mock_app, tmp_path, monkeypatch):
    """Tags und Releases teilen sich einen Token, statt zwei zu erzeugen."""
    mock_app.is_configured.return_value = True
    mock_app.installation_token.return_value = "ghs_abc"
    svc = _service(tmp_path, monkeypatch)
    tags = [{"name": "v1.0.0", "commit": {"sha": "0123456789abcdef"}}]
    get = MagicMock(side_effect=[_response(200, tags), _response(200, [])])
    monkeypatch.setattr(svc._session, "get", get)

    versions = svc.get_versions("https://github.com/acme/widgets")

    assert [v["version"] for v in versions] == ["v1.0.0"]
    mock_app.installation_token.assert_called_once_with("acme", "widgets")
    for call in get.call_args_list:
        assert call.kwargs["headers"]["Authorization"] == "token ghs_abc"


@patch("app.services.git_service.github_app")
def test_verify_points_to_install_page_when_app_cannot_read(mock_app, tmp_path, monkeypatch):
    """Mit App ersetzt der Installationslink das Annehmen einer Einladung."""
    mock_app.is_configured.return_value = True
    mock_app.installation_token.return_value = None
    mock_app.install_url.return_value = "https://github.com/apps/x/installations/new"
    svc = _service(tmp_path, monkeypatch)
    monkeypatch.setattr(svc, "token", "")
    monkeypatch.setattr(svc._session, "get", MagicMock(return_value=_response(404)))
    accept = MagicMock()
    monkeypatch.setattr(svc, "_accept_github_invite", accept)

    result = svc.verify_repository_access("https://github.com/acme/private")

    assert result["success"] is False
    assert "https://github.com/apps/x/installations/new" in result["message"]
    accept.assert_not_called()


@patch("app.services.git_service.github_app")
def test_verify_public_repo_needs_no_credentials(mock_app, tmp_path, monkeypatch):
    """Öffentliche GitHub-Repos gehen auch ohne Token und ohne App durch."""
    mock_app.is_configured.return_value = False
    svc = _service(tmp_path, monkeypatch)
    monkeypatch.setattr(svc, "token", "")
    get = MagicMock(return_value=_response(200))
    monkeypatch.setattr(svc._session, "get", get)

    result = svc.verify_repository_access("https://github.com/acme/public")

    assert result["success"] is True
    assert "Authorization" not in get.call_args.kwargs["headers"]
