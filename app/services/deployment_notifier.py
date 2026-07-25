"""Build and send post-deploy notification mails.

Two flavours:

* ``send_user_mails(...)`` — one mail per user with their own access
  details and the list of teammates.
* ``send_owner_mail(...)`` — one mail to the deployment owner with
  every team's VM data and every user's credentials in one place.

Both consume the worker's terraform outputs (``team_vms``,
``user_accounts``, ``teams_summary``) plus the deployment's team/user
membership from the DB.

Every recipient is pulled fresh from Keycloak
(``refresh_user_from_keycloak``) right before the mail is composed so a
changed address is honoured; the refresh is best-effort and falls back
to the DB record when Keycloak is unreachable. Failures are logged at
the call site and never bubble up — sending mail is best-effort.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.config import settings
from app.crud import deployments as crud_deployments
from app.models import App, Deployment, Task, TaskStatus, TaskType, Team, User
from app.services import email_service
from app.utils.keycloak_auth import refresh_user_from_keycloak

logger = logging.getLogger(__name__)


def _display_name(user: User) -> str:
    """Render a user-friendly greeting name.

    Preference: firstName + lastName → firstName → username. Never
    returns an empty string.
    """
    parts = [p for p in (getattr(user, "firstName", None), getattr(user, "lastName", None)) if p]
    if parts:
        return " ".join(parts)
    return user.username or user.email.split("@")[0]


# ----------------------------------------------------------------------------
# Outputs parsing
# ----------------------------------------------------------------------------
#
# Worker tasks return ``terraform_outputs`` as the raw JSON object from
# ``terraform output -json`` (each top-level key is an output name with
# ``{value, type, sensitive}``). We read the ``value`` of three
# well-known outputs:
#
#   team_vms.value:    {"Team-1": {code_server_url, floating_ip,
#                       fixed_ip, instance_id, instance_name}, ...}
#   user_accounts.value: {"Team-1-luca": {auth, ip, port, type,
#                       username}, ...}
#   teams_summary.value: {"Team-1": 1, ...}  — member counts.
#
# ``user_accounts`` auth-type contract (``type`` slot):
#   * ``password`` (default) — ``auth`` is the password string.
#   * ``ssh_key``  — ``auth`` is the public key / hint.
#   * ``oauth``    — ``auth`` is the login URL.
#   * ``none``     — no credential shipped.
#   Unknown types fall back to ``password`` rendering.
#
# Missing outputs yield empty dicts and the mail omits those sections.


def _output_value(outputs: dict[str, Any] | None, key: str) -> Any:
    """Pluck ``outputs[key].value`` out, tolerating missing keys."""
    if not outputs:
        return None
    bag = outputs.get(key)
    if isinstance(bag, dict):
        return bag.get("value")
    return None


def _team_vms(outputs: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    val = _output_value(outputs, "team_vms")
    return val if isinstance(val, dict) else {}


def _user_accounts(outputs: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    val = _output_value(outputs, "user_accounts")
    return val if isinstance(val, dict) else {}


def _vm_for_team(outputs: dict[str, Any] | None, team_name: str) -> dict[str, Any] | None:
    """Pick the VM block for one team, normalised to the keys the
    template expects (``url``, ``floating_ip``, ``fixed_ip``,
    ``instance_name``). Only recognised keys are normalised.
    """
    raw = _team_vms(outputs).get(team_name)
    if not isinstance(raw, dict):
        return None
    return {
        "url": raw.get("code_server_url") or raw.get("url"),
        "floating_ip": raw.get("floating_ip"),
        "fixed_ip": raw.get("fixed_ip"),
        "instance_name": raw.get("instance_name"),
    }


def _normalise_account_key(value: str | None) -> str:
    """Normalise an account/username key for fuzzy matching.

    Worker templates derive account names from emails by replacing
    non-``[a-z0-9]`` characters. This collapses ``.``/``-``/``_``/spaces
    to a single ``-`` and lowercases, so forms like ``luca.baeck``,
    ``luca-baeck`` and ``LUCA_BAECK`` map to the same canonical string.
    """
    if not value:
        return ""
    out = []
    for ch in value.strip().lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in ".-_ ":
            out.append("-")
        # Other characters drop entirely.
    # Collapse runs of "-" into one.
    canonical = "".join(out)
    while "--" in canonical:
        canonical = canonical.replace("--", "-")
    return canonical.strip("-")


def _access_for_user(
    outputs: dict[str, Any] | None,
    team_name: str,
    user: User,
) -> dict[str, Any] | None:
    """Find the user's access entry in the worker's outputs.

    Templates key per-user accounts as ``"<team>-<account-name>"`` where
    ``<account-name>`` is some derivation of the user's email or
    username. To avoid coupling to one naming scheme we build candidate
    identifiers (username, email local-part, full name) plus their
    normalised forms and match any overlapping account.
    """
    accounts = _user_accounts(outputs)
    candidates_raw: set[str] = {
        user.username or "",
        (user.email or "").split("@")[0],
    }
    if user.firstName and user.lastName:
        candidates_raw.add(f"{user.firstName} {user.lastName}")
    candidates = {_normalise_account_key(c) for c in candidates_raw if c}
    candidates.discard("")

    for key, raw in accounts.items():
        if not isinstance(raw, dict):
            continue
        # Strip the team-name prefix so ``"Team-1-luca-baeck"`` becomes
        # ``"luca-baeck"`` before matching. The prefix is normalised the
        # same way so a team named ``"Team 1"`` also matches.
        team_prefix = _normalise_account_key(team_name) + "-"
        normalised_key = _normalise_account_key(key)
        suffix = normalised_key[len(team_prefix):] if normalised_key.startswith(team_prefix) else normalised_key

        candidates_with_inner_username = candidates | {_normalise_account_key(raw.get("username"))}
        candidates_with_inner_username.discard("")

        if suffix in candidates_with_inner_username:
            # Normalise the ``type`` slot; unknown values fall back to
            # ``password`` rendering.
            raw_type = raw.get("type")
            auth_type = raw_type if raw_type in ("password", "ssh_key", "oauth", "none") else "password"
            auth_value = raw.get("auth")
            # ``password`` is only populated for the password type;
            # templates check ``auth_type`` before reading it.
            password = auth_value if auth_type == "password" else None
            return {
                "username": raw.get("username") or suffix,
                "password": password,
                "ip": raw.get("ip"),
                "port": raw.get("port"),
                "auth_type": auth_type,
                # Raw credential regardless of type; templates use it
                # with ``auth_type`` to decide where to show it.
                "auth_value": auth_value,
                # Fallback URL from the team VM when the per-user output
                # doesn't carry one.
                "url": (_vm_for_team(outputs, team_name) or {}).get("url"),
            }
    return None


# ----------------------------------------------------------------------------
# Senders
# ----------------------------------------------------------------------------


def _team_members(db: Session, team: Team) -> list[User]:
    """Resolve team members via the join table, going through
    ``crud_deployments`` to keep the join logic centralised."""
    return crud_deployments.get_team_members(db, team.teamId)


def _send_user_mail(
    *,
    db: Session,
    user: User,
    teammates: list[User],
    team_name: str,
    deployment: Deployment,
    app: App,
    access: dict[str, Any],
) -> None:
    # Re-pull from Keycloak immediately before composing the mail so
    # the recipient reflects the current upstream address; best-effort
    # (falls back to the DB row when KC is unreachable). Done in the
    # sender so both the notify and resend call sites stay honest.
    user = refresh_user_from_keycloak(db, user)
    ctx = {
        "user": user,
        "user_display_name": _display_name(user),
        "teammates": [
            {"user": m, "display_name": _display_name(m)}
            for m in teammates
            if m.userId != user.userId
        ],
        "team_name": team_name,
        "deployment": {
            "name": deployment.name,
            "git_url": app.git_link,
            "release_tag": deployment.releaseTag,
            "app_name": app.name,
        },
        "access": access,
    }
    email_service.send_email(
        to=user.email,
        subject=f"[{deployment.name}] Your access details",
        html_body=email_service.render("user_invite.html", **ctx),
        text_body=email_service.render("user_invite.txt", **ctx),
    )


def _send_owner_mail(
    *,
    db: Session,
    owner: User,
    deployment: Deployment,
    app: App,
    teams_payload: list[dict[str, Any]],
) -> None:
    # Same refresh contract as ``_send_user_mail`` for the owner.
    owner = refresh_user_from_keycloak(db, owner)
    ctx = {
        "owner": owner,
        "owner_display_name": _display_name(owner),
        "deployment": {
            "name": deployment.name,
            "git_url": app.git_link,
            "release_tag": deployment.releaseTag,
            "app_name": app.name,
        },
        "teams": teams_payload,
        "detail_url": f"{settings.APP_BASE_URL.rstrip('/')}/deployments/{deployment.deploymentId}",
    }
    email_service.send_email(
        to=owner.email,
        subject=f"[{deployment.name}] Deployment summary",
        html_body=email_service.render("owner_summary.html", **ctx),
        text_body=email_service.render("owner_summary.txt", **ctx),
    )


def notify_deployment_succeeded(
    db: Session,
    deployment_id: UUID,
    terraform_outputs: dict[str, Any] | None,
) -> None:
    """Entry point — call from the celery event listener after a
    successful DEPLOY task.

    Loads the deployment with relations, walks its teams, and sends one
    mail per user plus one summary mail to the owner. No-op when the
    deployment is missing or the outputs are empty.
    """
    deployment = crud_deployments.get_deployment_with_details(db, deployment_id)
    if not deployment:
        logger.warning("notify: deployment %s not found", deployment_id)
        return

    app = deployment.app
    owner = deployment.user
    if not app or not owner:
        logger.warning(
            "notify: deployment %s missing app or owner relation", deployment_id
        )
        return

    if not terraform_outputs:
        logger.info(
            "notify: deployment %s has no terraform outputs, skipping mails",
            deployment_id,
        )
        return

    # Build the per-team payload once. Used for the owner mail and for
    # picking each user's individual access.
    teams_payload: list[dict[str, Any]] = []
    for team in deployment.teams or []:
        members = _team_members(db, team)
        # Refresh every team member from Keycloak up front so the
        # teammates section and owner summary reflect current records,
        # and to avoid N redundant KC roundtrips in ``_send_user_mail``.
        members = [refresh_user_from_keycloak(db, m) for m in members]
        member_payload: list[dict[str, Any]] = []
        for member in members:
            access = _access_for_user(terraform_outputs, team.name, member)
            if access is None:
                # No credential output for this user — include them in
                # the owner summary but skip the per-user mail.
                member_payload.append({
                    "user": member,
                    "display_name": _display_name(member),
                    "access": {
                        "username": "—",
                        "password": "—",
                        "auth_type": "password",
                        "auth_value": None,
                    },
                })
                continue
            member_payload.append({
                "user": member,
                "display_name": _display_name(member),
                "access": access,
            })

            # Per-user mail — fire-and-forget; failures already logged
            # inside ``email_service.send_email``.
            try:
                _send_user_mail(
                    db=db,
                    user=member,
                    teammates=members,
                    team_name=team.name,
                    deployment=deployment,
                    app=app,
                    access=access,
                )
            except Exception as e:
                logger.warning(
                    "notify: user mail to %s for deployment %s failed: %s",
                    member.email, deployment_id, e,
                )

        teams_payload.append({
            "name": team.name,
            "vm": _vm_for_team(terraform_outputs, team.name),
            "members": member_payload,
        })

    # Owner summary last so it includes everything we managed to
    # resolve.
    try:
        _send_owner_mail(
            db=db,
            owner=owner,
            deployment=deployment,
            app=app,
            teams_payload=teams_payload,
        )
    except Exception as e:
        logger.warning(
            "notify: owner mail for deployment %s failed: %s",
            deployment_id, e,
        )


# ----------------------------------------------------------------------------
# Single-user resend
# ----------------------------------------------------------------------------


class ResendError(Exception):
    """Resend prerequisites weren't met (no successful deploy, user not in team, no credentials)."""


def resend_user_access(
    db: Session,
    deployment_id: UUID,
    team_id: UUID,
    user_id: UUID,
) -> bool:
    """Re-send the access mail for one specific user of a deployment.

    Loads the latest successful DEPLOY task to recover the original
    ``terraform_outputs`` (which carry the credentials) and sends the
    same per-user mail to that user only.

    Raises ``ResendError`` when the deployment/task is missing, the team
    or user isn't part of the deployment, or no credential was produced.
    Returns ``True`` on successful SMTP handover, ``False`` if SMTP
    rejected the mail.
    """
    deployment = crud_deployments.get_deployment_with_details(db, deployment_id)
    if not deployment:
        raise ResendError("deployment_not_found")
    app = deployment.app
    if not app:
        raise ResendError("deployment_app_missing")

    # Find the team scoped to this deployment.
    team: Team | None = next(
        (t for t in (deployment.teams or []) if t.teamId == team_id),
        None,
    )
    if team is None:
        raise ResendError("team_not_in_deployment")

    members = _team_members(db, team)
    user: User | None = next((m for m in members if m.userId == user_id), None)
    if user is None:
        raise ResendError("user_not_in_team")

    # Most recent successful DEPLOY task; its ``outputs`` is the same
    # JSON the original notify ran against.
    last_deploy = (
        db.query(Task)
        .filter(
            Task.deploymentId == deployment_id,
            Task.type == TaskType.DEPLOY,
            Task.status == TaskStatus.SUCCESS,
        )
        .order_by(Task.created_at.desc())
        .first()
    )
    if not last_deploy or not last_deploy.outputs:
        raise ResendError("no_successful_deploy")

    try:
        outputs = json.loads(last_deploy.outputs) if isinstance(last_deploy.outputs, str) else last_deploy.outputs
    except json.JSONDecodeError:
        raise ResendError("outputs_unreadable")

    access = _access_for_user(outputs, team.name, user)
    if access is None:
        raise ResendError("no_credentials_for_user")

    try:
        _send_user_mail(
            db=db,
            user=user,
            teammates=members,
            team_name=team.name,
            deployment=deployment,
            app=app,
            access=access,
        )
        return True
    except Exception as e:
        logger.warning(
            "resend: user mail to %s for deployment %s failed: %s",
            user.email, deployment_id, e,
        )
        return False
