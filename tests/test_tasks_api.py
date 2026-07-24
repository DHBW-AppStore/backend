"""Tests for the Tasks-API router — ``app.routers.tasks``.

Phase C10: read-only endpoints exposing task information for a deployment.
Covered cases:

  * Owner darf die Task-Liste einer eigenen Deployment lesen.
  * Nicht-Mitglied (STUDENT, kein Team, kein UserToDeployment) bekommt 403.
  * Owner darf eine einzelne Task per ID lesen.
  * Unbekannte Task-ID → 404.
  * Ohne Bearer-Token → 401 (oder 403 von HTTPBearer).
  * Ein Teacher, der Course-Teacher des Owner-Kurses ist, darf die Tasks
    (inkl. Terraform-Outputs) lesen; ein fremder Teacher außerhalb des
    Kurses bekommt 403 (course-scoped Owner-View, kein pauschaler
    Staff-Bypass mehr).
"""

import uuid
from datetime import datetime

import pytest

from app.models import (
    App,
    Course,
    CourseTeacher,
    Deployment,
    Task,
    TaskStatus,
    TaskType,
    User,
    UserRole,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_app(db, user_id):
    a = App(
        appId=uuid.uuid4(),
        name=f"App {uuid.uuid4().hex[:6]}",
        userId=user_id,
        git_link="https://example.com/repo.git",
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    return a


def _make_deployment(db, user_id, app_id, name="dep-tasks"):
    d = Deployment(
        deploymentId=uuid.uuid4(),
        name=name,
        userId=user_id,
        appId=app_id,
    )
    db.add(d)
    db.commit()
    db.refresh(d)
    return d


def _make_task(
    db,
    deployment_id,
    *,
    type_=TaskType.DEPLOY,
    status=TaskStatus.SUCCESS,
    celery_task_id="celery-task-1",
    created_at=None,
):
    t = Task(
        taskId=uuid.uuid4(),
        deploymentId=deployment_id,
        celeryTaskId=celery_task_id,
        type=type_,
        status=status,
        created_at=created_at or datetime(2026, 1, 1, 12, 0, 0),
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _make_course(db, name="Cloud SS26"):
    course = Course(courseId=uuid.uuid4(), name=name)
    db.add(course)
    db.commit()
    db.refresh(course)
    return course


def _make_teacher(db, *, course_id=None, email=None):
    """Create a TEACHER user, optionally enrolled as course-teacher of
    ``course_id``. ``course_id`` here is the course this teacher *teaches*
    (a ``course_teachers`` row), not the ``User.courseId`` enrolment column.
    """
    teacher = User(
        userId=uuid.uuid4(),
        keycloak_id=f"kc-{uuid.uuid4().hex[:8]}",
        email=email or f"teacher-{uuid.uuid4().hex[:6]}@dhbw.de",
        username=f"teacher-{uuid.uuid4().hex[:6]}",
        role=UserRole.TEACHER,
    )
    db.add(teacher)
    db.commit()
    db.refresh(teacher)
    if course_id is not None:
        db.add(CourseTeacher(courseId=course_id, userId=teacher.userId))
        db.commit()
    return teacher


def _make_student_owner(db, *, course_id):
    """Create a STUDENT enrolled in ``course_id`` (their ``User.courseId``)."""
    student = User(
        userId=uuid.uuid4(),
        keycloak_id=f"kc-{uuid.uuid4().hex[:8]}",
        email=f"owner-{uuid.uuid4().hex[:6]}@dhbw.de",
        username=f"owner-{uuid.uuid4().hex[:6]}",
        role=UserRole.STUDENT,
        courseId=course_id,
    )
    db.add(student)
    db.commit()
    db.refresh(student)
    return student



# ---------------------------------------------------------------------------
# GET /tasks/deployment/{deployment_id}
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_get_deployment_tasks_owner_ok(client, db, mock_user):
    """Owner einer Deployment liest die zugehörigen Tasks."""
    app = _make_app(db, mock_user.userId)
    deployment = _make_deployment(db, mock_user.userId, app.appId)
    task = _make_task(db, deployment.deploymentId)

    response = client.get(f"/tasks/deployment/{deployment.deploymentId}")

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert len(body) == 1
    assert body[0]["taskId"] == str(task.taskId)
    assert body[0]["deploymentId"] == str(deployment.deploymentId)


@pytest.mark.integration
def test_get_deployment_tasks_non_member_403(
    student_client, db, mock_admin, mock_student
):
    """Ein STUDENT ohne Team-/Direkt-Zuordnung bekommt 403."""
    # Deployment gehört dem Admin — der Student hat keinerlei Bezug.
    app = _make_app(db, mock_admin.userId)
    deployment = _make_deployment(db, mock_admin.userId, app.appId)
    _make_task(db, deployment.deploymentId)

    response = student_client.get(f"/tasks/deployment/{deployment.deploymentId}")

    assert response.status_code == 403


# ---------------------------------------------------------------------------
# GET /tasks/{task_id}
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_get_task_by_id_owner_ok(client, db, mock_user):
    """Owner liest eine einzelne Task per ID."""
    app = _make_app(db, mock_user.userId)
    deployment = _make_deployment(db, mock_user.userId, app.appId)
    task = _make_task(
        db,
        deployment.deploymentId,
        celery_task_id="celery-by-id",
    )

    response = client.get(f"/tasks/{task.taskId}")

    assert response.status_code == 200
    body = response.json()
    assert body["taskId"] == str(task.taskId)
    assert body["deploymentId"] == str(deployment.deploymentId)
    assert body["celeryTaskId"] == "celery-by-id"


@pytest.mark.integration
def test_get_task_404_for_unknown_id(client):
    """Unbekannte Task-ID liefert 404."""
    response = client.get(f"/tasks/{uuid.uuid4()}")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_get_task_unauthenticated_401(unauth_client):
    """Ohne Token verweigert die API den Zugriff auf Tasks.

    FastAPI's ``HTTPBearer`` mappt fehlende Credentials auf 403, mit
    ungültigem Token kommt 401 — beides sind valide „nicht erlaubt"-
    Antworten und werden hier akzeptiert.
    """
    response = unauth_client.get(f"/tasks/{uuid.uuid4()}")
    assert response.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Course-Teacher-Scoping (course-scoped Owner-View)
# ---------------------------------------------------------------------------
# The task payload carries the deployment's Terraform ``outputs`` — i.e. the
# per-user access credentials. The owner-view gate is course-scoped: a teacher
# only reaches it for deployments whose owner sits in a course they teach. A
# teacher outside that course must get a 403, even though they're staff.
def _auth_as(client, user):
    """Re-point the current-user dependency at ``user`` for one request.

    Mirrors the pattern in ``test_deployment_resources_endpoint.py``: the
    ``client`` fixture wires the override to ``mock_user``, so tests that
    need a different caller swap it on the shared FastAPI app instance.
    """
    from app.main import app
    from app.utils.keycloak_auth import get_current_user_keycloak

    app.dependency_overrides[get_current_user_keycloak] = lambda: user


@pytest.mark.integration
def test_get_deployment_tasks_course_teacher_ok(client, db, mock_user):
    """A teacher of the owner's course reads the deployment's tasks."""
    course = _make_course(db)
    owner = _make_student_owner(db, course_id=course.courseId)
    course_teacher = _make_teacher(db, course_id=course.courseId)

    app_row = _make_app(db, owner.userId)
    deployment = _make_deployment(db, owner.userId, app_row.appId)
    task = _make_task(db, deployment.deploymentId)

    _auth_as(client, course_teacher)
    try:
        response = client.get(f"/tasks/deployment/{deployment.deploymentId}")
    finally:
        _auth_as(client, mock_user)

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["taskId"] == str(task.taskId)


@pytest.mark.integration
def test_get_deployment_tasks_foreign_teacher_403(client, db, mock_user):
    """A teacher who does NOT teach the owner's course is denied.

    This is the regression guard for the leak: the legacy owner-view gate
    granted any staff member access, letting a teacher read the credentials
    of deployments in courses they don't teach.
    """
    owner_course = _make_course(db, name="Cloud SS26")
    other_course = _make_course(db, name="Networking SS26")
    owner = _make_student_owner(db, course_id=owner_course.courseId)
    # Teacher teaches a different course than the owner's.
    foreign_teacher = _make_teacher(db, course_id=other_course.courseId)

    app_row = _make_app(db, owner.userId)
    deployment = _make_deployment(db, owner.userId, app_row.appId)
    _make_task(db, deployment.deploymentId)

    _auth_as(client, foreign_teacher)
    try:
        response = client.get(f"/tasks/deployment/{deployment.deploymentId}")
    finally:
        _auth_as(client, mock_user)

    assert response.status_code == 403


@pytest.mark.integration
def test_get_task_by_id_foreign_teacher_403(client, db, mock_user):
    """Single-task fetch is course-scoped too — foreign teacher gets 403."""
    owner_course = _make_course(db, name="Cloud SS26")
    owner = _make_student_owner(db, course_id=owner_course.courseId)
    # Teacher with no course-teacher rows at all.
    foreign_teacher = _make_teacher(db)

    app_row = _make_app(db, owner.userId)
    deployment = _make_deployment(db, owner.userId, app_row.appId)
    task = _make_task(db, deployment.deploymentId)

    _auth_as(client, foreign_teacher)
    try:
        response = client.get(f"/tasks/{task.taskId}")
    finally:
        _auth_as(client, mock_user)

    assert response.status_code == 403


@pytest.mark.integration
def test_get_task_by_id_admin_ok(admin_client, db, mock_admin, mock_student):
    """Admins keep unconditional owner-view across every deployment."""
    app_row = _make_app(db, mock_student.userId)
    deployment = _make_deployment(db, mock_student.userId, app_row.appId)
    task = _make_task(db, deployment.deploymentId)

    response = admin_client.get(f"/tasks/{task.taskId}")

    assert response.status_code == 200
    assert response.json()["taskId"] == str(task.taskId)

