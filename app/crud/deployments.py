import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, asc, desc, exists, func
from sqlalchemy.orm import Session, joinedload

from app.models import (
    Deployment,
    Task,
    TaskStatus,
    TaskType,
    Team,
    User,
    UserToDeployment,
    UserToTeam,
)
from app.schemas import DeploymentCreate
from app.utils.time import utcnow


def _is_destroyed_subq():
    """Correlated EXISTS: deployment has a successful DESTROY task."""
    return exists().where(
        (Task.deploymentId == Deployment.deploymentId)
        & (Task.type == TaskType.DESTROY)
        & (Task.status == TaskStatus.SUCCESS)
    )


def count_active_user_deployments(db: Session, user_id: UUID) -> int:
    """Number of *active* deployments owned by ``user_id``.

    "Active" here matches what the user actually sees on the Deployments
    page:

      * Owned by them (``Deployment.userId == user_id``).
      * Not soft-deleted (``deleted_at IS NULL``) — once the user has
        clicked "Löschen" we must not keep the credentials locked
        against a row that is no longer visible to them.
      * Has not been fully destroyed (no successful DESTROY task) —
        soft-delete happens AFTER destroy completes, so during the
        destroy-in-flight phase ``deleted_at`` may still be NULL but
        the deployment is on its way out. Keeping the destroyed-check
        is harmless and matches the historical contract.
    """
    return (
        db.query(Deployment)
        .filter(Deployment.userId == user_id)
        .filter(Deployment.deleted_at.is_(None))
        .filter(~_is_destroyed_subq())
        .count()
    )


def has_active_user_deployment(db: Session, user_id: UUID) -> bool:
    return (
        db.query(Deployment.deploymentId)
        .filter(Deployment.userId == user_id)
        .filter(Deployment.deleted_at.is_(None))
        .filter(~_is_destroyed_subq())
        .first() is not None
    )


def get_deployment(
    db: Session,
    deployment_id: UUID,
    include_deleted: bool = False,
) -> Deployment | None:
    """Get deployment by ID. Hides soft-deleted rows by default.

    ``include_deleted=True`` is for the rare audit/restore lookup; the
    HTTP API never sets it.
    """
    q = db.query(Deployment).filter(Deployment.deploymentId == deployment_id)
    if not include_deleted:
        q = q.filter(Deployment.deleted_at.is_(None))
    return q.first()


def get_deployment_with_details(
    db: Session,
    deployment_id: UUID,
    include_deleted: bool = False,
) -> Deployment | None:
    """Get deployment by ID with all relations loaded. Hides soft-deleted by default."""
    q = (
        db.query(Deployment)
        .options(
            joinedload(Deployment.user),
            joinedload(Deployment.app),
            joinedload(Deployment.teams),
        )
        .filter(Deployment.deploymentId == deployment_id)
    )
    if not include_deleted:
        q = q.filter(Deployment.deleted_at.is_(None))
    return q.first()


def get_latest_task(db: Session, deployment_id: UUID) -> Task | None:
    """Get the most recent task for a deployment"""
    return (
        db.query(Task)
        .filter(Task.deploymentId == deployment_id)
        .order_by(desc(Task.created_at))
        .first()
    )


def get_first_task(db: Session, deployment_id: UUID) -> Task | None:
    """Get the first task for a deployment (when deployment was created)"""
    return (
        db.query(Task)
        .filter(Task.deploymentId == deployment_id)
        .order_by(asc(Task.created_at))
        .first()
    )


def derive_status(
    task_status: TaskStatus | None,
    task_type: TaskType | None,
) -> str | None:
    """Synthesize the effective deployment status from the latest task's
    ``(status, type)`` pair.

    The bare ``task.status`` (pending/running/success/failed/cancelled) is
    not enough by itself: a destroy task in flight should surface as
    ``destroying`` and a finished destroy as ``destroyed``, neither of
    which exists as a stored enum value. We synthesize them here so the
    same mapping is used by the single-deployment path
    (``get_deployment_status``) and the bulk list path
    (``bulk_get_task_summary``).

    Pause/resume follow the same pattern:

    * ``(PAUSE,  pending|running)`` → ``pausing``
    * ``(PAUSE,  success)``         → ``paused``
    * ``(RESUME, pending|running)`` → ``resuming``
    * ``(RESUME, success)``         → falls through to ``success``,
      so a resumed deployment behaves identically to a fresh deploy
      from the lifecycle matrix's point of view (PAUSE is offered
      again, RESUME isn't).

    PAUSE/RESUME ``failed`` and ``cancelled`` are intentionally NOT
    rewritten — they bleed through unchanged so the user sees the
    pause/resume itself broke (vs. the original deploy succeeded).
    Destroy is still available from ``failed`` to recover.

    Returns ``None`` if the deployment has no tasks yet.
    """
    if task_status is None:
        return None

    raw_status = task_status.value
    raw_type = task_type.value if task_type else None

    if raw_type == "destroy":
        if raw_status in ("pending", "running"):
            return "destroying"
        if raw_status == "success":
            return "destroyed"
        # failed/cancelled bleed through unchanged so the user sees that
        # the destroy itself broke (vs. the original deploy succeeded).
    elif raw_type == "pause":
        if raw_status in ("pending", "running"):
            return "pausing"
        if raw_status == "success":
            return "paused"
        if raw_status == "failed":
            # Distinguish a *pause* failure from a *deploy* failure —
            # the OpenStack resources are still running, only the
            # stop-instances pass broke. The user shouldn't see this
            # as a generic "failed" because the deployment itself is
            # unaffected; the lifecycle matrix below still allows
            # destroy / pause-retry / resume from this synthetic
            # state.
            return "pause_failed"
        # cancelled bleeds through unchanged.
    elif raw_type == "resume":
        if raw_status in ("pending", "running"):
            return "resuming"
        if raw_status == "failed":
            # Same logic as pause_failed: the SHUTOFF instances are
            # still there, only the start pass tripped. Surface that
            # so the user can retry resume or destroy without first
            # being scared by a generic failed badge.
            return "resume_failed"
        # On resume success the deployment is "running again" — we
        # let raw_status = "success" pass through so the lifecycle
        # matrix treats it like a fresh successful deploy.
    return raw_status


def get_deployment_status(db: Session, deployment_id: UUID) -> str | None:
    """Effective deployment status for a single deployment.

    Thin wrapper around ``derive_status`` for the per-deployment path
    (detail endpoint, single-row callers). The list endpoint goes
    through ``bulk_get_task_summary`` instead so it doesn't fan out
    one query per row.

    Returns ``None`` if the deployment has no tasks yet.
    """
    task = get_latest_task(db, deployment_id)
    if task is None:
        return None
    return derive_status(task.status, task.type)


def get_deployment_created_at(db: Session, deployment_id: UUID):
    """Get deployment creation time from first task"""
    task = get_first_task(db, deployment_id)
    return task.created_at if task else None


def bulk_get_task_summary(
    db: Session, deployment_ids: list[UUID]
) -> dict[UUID, tuple[TaskStatus | None, TaskType | None, datetime | None]]:
    """Fetch the latest-task ``(status, type)`` and the first-task
    ``created_at`` for every deployment in ``deployment_ids`` — in two
    queries, regardless of how many deployments are passed.

    Replaces the per-row ``get_latest_task`` + ``get_first_task`` fan-out
    that the list endpoint used to do (1 + 2N queries → 3 queries for the
    same payload). Mirrors the window-function pattern used by
    ``routers/dashboard.py:get_dashboard_stats``.

    Returns a dict keyed by ``deploymentId``. Deployments with no tasks
    are simply absent from the map; the caller must handle that with
    ``.get(deployment_id, (None, None, None))``.
    """

    if not deployment_ids:
        return {}

    # Latest task per deployment via row_number() over (PARTITION BY ...
    # ORDER BY created_at DESC).
    latest_rn = (
        func.row_number()
        .over(partition_by=Task.deploymentId, order_by=desc(Task.created_at))
        .label("rn")
    )
    latest_subq = (
        db.query(
            Task.deploymentId.label("did"),
            Task.status.label("status"),
            Task.type.label("type"),
            latest_rn,
        )
        .filter(Task.deploymentId.in_(deployment_ids))
        .subquery()
    )
    latest_rows = (
        db.query(latest_subq.c.did, latest_subq.c.status, latest_subq.c.type)
        .filter(latest_subq.c.rn == 1)
        .all()
    )

    # First task per deployment (ascending) for created_at.
    first_rn = (
        func.row_number()
        .over(partition_by=Task.deploymentId, order_by=asc(Task.created_at))
        .label("rn")
    )
    first_subq = (
        db.query(
            Task.deploymentId.label("did"),
            Task.created_at.label("created_at"),
            first_rn,
        )
        .filter(Task.deploymentId.in_(deployment_ids))
        .subquery()
    )
    first_rows = (
        db.query(first_subq.c.did, first_subq.c.created_at)
        .filter(first_subq.c.rn == 1)
        .all()
    )

    first_map = {row.did: row.created_at for row in first_rows}
    return {
        row.did: (row.status, row.type, first_map.get(row.did))
        for row in latest_rows
    }


def get_team_members(db: Session, team_id: UUID) -> list[User]:
    """Get all users in a team"""
    user_ids = (
        db.query(UserToTeam.userId)
        .filter(UserToTeam.teamId == team_id)
        .all()
    )
    user_ids = [uid[0] for uid in user_ids]

    if not user_ids:
        return []

    return db.query(User).filter(User.userId.in_(user_ids)).all()


def get_deployment_teams_with_members(db: Session, deployment_id: UUID) -> list[dict[str, Any]]:
    """Get all teams for a deployment with their members"""
    teams = db.query(Team).filter(Team.deploymentId == deployment_id).all()

    result = []
    for team in teams:
        members = get_team_members(db, team.teamId)
        result.append({
            "teamId": team.teamId,
            "name": team.name,
            "members": [
                {
                    "userId": member.userId,
                    "email": member.email,
                    "username": member.username
                }
                for member in members
            ]
        })

    return result


def get_deployment_outputs(db: Session, deployment_id: UUID) -> dict[str, Any] | None:
    """Get parsed Terraform outputs from the latest successful task"""
    task = (
        db.query(Task)
        .filter(Task.deploymentId == deployment_id)
        .filter(Task.outputs.isnot(None))
        .order_by(desc(Task.created_at))
        .first()
    )

    if task and task.outputs:
        try:
            return json.loads(task.outputs)
        except json.JSONDecodeError:
            return None
    return None


def get_latest_successful_deploy_outputs(
    db: Session, deployment_id: UUID
) -> dict[str, Any] | None:
    """Parsed Terraform outputs of the most recent *successful DEPLOY* task.

    Stricter than :func:`get_deployment_outputs`, which returns the latest
    task carrying any non-null ``outputs`` regardless of type or status — a
    DESTROY task's (empty) outputs could win there. Credential extraction
    must read the outputs of an actual successful deploy, so this helper
    filters to ``type == DEPLOY`` and ``status == SUCCESS``.

    Returns ``None`` when no such task exists or its ``outputs`` are absent
    / unparseable. Shared by ``resend_user_access`` and the per-member
    ``/my-access`` endpoint so both agree on which outputs are authoritative.
    """
    task = (
        db.query(Task)
        .filter(
            Task.deploymentId == deployment_id,
            Task.type == TaskType.DEPLOY,
            Task.status == TaskStatus.SUCCESS,
        )
        .order_by(desc(Task.created_at))
        .first()
    )

    if task and task.outputs:
        try:
            return json.loads(task.outputs) if isinstance(task.outputs, str) else task.outputs
        except json.JSONDecodeError:
            return None
    return None


def get_deployments(
    db: Session,
    skip: int = 0,
    limit: int = 100,
    user_id: UUID | None = None,
    member_user_id: UUID | None = None,
    app_id: UUID | None = None,
    status: str | None = None,
    include_deleted: bool = False,
) -> list[Deployment]:
    """Get deployments with optional filters. Hides soft-deleted by default.

    ``user_id`` filters by deployment owner (``Deployment.userId``).

    ``member_user_id`` filters by **either** owner or membership: a
    deployment matches when the user is the creator OR appears in any
    team's ``UserToTeam`` row OR has a direct ``UserToDeployment``
    mapping. Used by the listing endpoint so a student sees deployments
    they were added to without seeing every deployment in the system.
    Mutually exclusive with ``user_id``; if both are set ``user_id``
    wins (caller bug, but the stricter filter is the safer default).
    """
    query = db.query(Deployment)

    if not include_deleted:
        # Backed by the partial index ix_deployments_live so this stays
        # cheap even with many rows.
        query = query.filter(Deployment.deleted_at.is_(None))
    if user_id:
        query = query.filter(Deployment.userId == user_id)
    elif member_user_id:
        # Owner OR team member OR direct mapping. Use a UNION-ish
        # approach via a subquery on teamIds the user belongs to so
        # the OR doesn't explode into a cartesian.
        member_team_ids = (
            db.query(UserToTeam.teamId).filter(UserToTeam.userId == member_user_id)
        )
        member_deployment_ids_via_teams = (
            db.query(Team.deploymentId).filter(Team.teamId.in_(member_team_ids))
        )
        member_deployment_ids_direct = (
            db.query(UserToDeployment.deploymentId)
            .filter(UserToDeployment.userId == member_user_id)
        )
        query = query.filter(
            (Deployment.userId == member_user_id)
            | (Deployment.deploymentId.in_(member_deployment_ids_via_teams))
            | (Deployment.deploymentId.in_(member_deployment_ids_direct))
        )
    if app_id:
        query = query.filter(Deployment.appId == app_id)

    # Filter by effective status. The status the API exposes is derived
    # from the LATEST task per deployment (see ``derive_status``), so we
    # join against a window-function subquery that pins the latest task
    # per deployment and apply the equivalent SQL predicate. Doing this
    # here — BEFORE offset/limit — keeps the page size correct: the old
    # post-filter loop only ever inspected the first ``limit`` rows, so
    # filtering by ``running`` could return fewer than ``limit`` matches
    # even when the DB had more.
    if status:
        latest_rn = (
            func.row_number()
            .over(partition_by=Task.deploymentId, order_by=desc(Task.created_at))
            .label("rn")
        )
        latest_subq = (
            db.query(
                Task.deploymentId.label("did"),
                Task.status.label("status"),
                Task.type.label("type"),
                latest_rn,
            ).subquery()
        )
        query = query.join(
            latest_subq,
            and_(
                latest_subq.c.did == Deployment.deploymentId,
                latest_subq.c.rn == 1,
            ),
        )

        if status == "destroying":
            query = query.filter(
                latest_subq.c.type == TaskType.DESTROY,
                latest_subq.c.status.in_((TaskStatus.PENDING, TaskStatus.RUNNING)),
            )
        elif status == "destroyed":
            query = query.filter(
                latest_subq.c.type == TaskType.DESTROY,
                latest_subq.c.status == TaskStatus.SUCCESS,
            )
        else:
            # Plain task statuses. Mirror the ``derive_status`` mapping:
            # - ``pending``/``running``/``success`` only match deploy-
            #   typed tasks (destroy + same status surfaces as
            #   ``destroying``/``destroyed``, not the raw value).
            # - ``failed``/``cancelled`` bleed through both task types,
            #   matching the comment in ``derive_status`` that a broken
            #   destroy must surface as ``failed``/``cancelled`` so the
            #   user sees the destroy itself broke.
            try:
                status_enum = TaskStatus(status)
            except ValueError:
                # Unknown status string → empty result, like the old
                # post-filter loop would have produced.
                return []
            query = query.filter(latest_subq.c.status == status_enum)
            if status_enum in (
                TaskStatus.PENDING,
                TaskStatus.RUNNING,
                TaskStatus.SUCCESS,
            ):
                query = query.filter(latest_subq.c.type != TaskType.DESTROY)

    # Order by deploymentId (UUID) - could also join with Task for created_at ordering
    query = query.order_by(desc(Deployment.deploymentId))

    return query.offset(skip).limit(limit).all()


def create_deployment(db: Session, deployment: DeploymentCreate, user_id: UUID) -> Deployment:
    """Insert a deployment row in the current transaction.

    Does NOT commit — the caller is expected to also insert teams/tasks
    in the same TX and commit once at the end. This is necessary so the
    advisory lock acquired at the start of the request stays held across
    all related inserts.
    """
    # Convert userInputVar dict to JSON string for database storage
    user_input_var_json = None
    if deployment.userInputVar is not None:
        user_input_var_json = json.dumps(deployment.userInputVar)

    db_deployment = Deployment(
        name=deployment.name,
        appId=deployment.appId,
        userId=user_id,
        releaseTag=deployment.releaseTag,
        userInputVar=user_input_var_json,
    )
    db.add(db_deployment)
    db.flush()
    db.refresh(db_deployment)
    return db_deployment

def soft_delete_deployment(db: Session, deployment_id: UUID) -> bool:
    """Mark a deployment as deleted without removing the row.

    Sets ``deleted_at = utcnow()`` so default queries skip it. The
    related tasks/teams/user-mappings are intentionally untouched —
    they're useful for audit and the partial-unique index on active
    tasks already prevents the deployment from accepting new work.

    Returns ``False`` if the deployment doesn't exist (or was already
    deleted), ``True`` on a successful soft-delete.
    """
    db_deployment = get_deployment(db, deployment_id)
    if not db_deployment:
        return False
    db_deployment.deleted_at = utcnow()
    db.commit()
    return True


def create_user_to_deployments(
    db: Session,
    deployment_id: UUID,
    user_ids: set[UUID],
) -> list[UserToDeployment]:
    """
    Create UserToDeployment entries for multiple users
    """
    user_to_deployments = []

    for user_id in user_ids:
        user_to_deployment = UserToDeployment(
            userId=user_id,
            deploymentId=deployment_id
        )
        db.add(user_to_deployment)
        user_to_deployments.append(user_to_deployment)

    return user_to_deployments
