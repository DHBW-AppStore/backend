"""Unit-Tests für app.utils.permissions (Rollen- und Deployment-Helfer)."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException, status

from app.models import UserRole
from app.utils.permissions import (
    ADMIN_ROLES,
    STAFF_ROLES,
    ensure_deployment_owner_view,
    get_current_active_user,
    is_deployment_owner_view,
    require_roles,
)


def _make_user(role: UserRole, user_id: str = "user-1", course_id: str | None = None) -> MagicMock:
    """Stubt einen User mit role / userId / courseId."""
    user = MagicMock()
    user.role = role
    user.userId = user_id
    user.courseId = course_id
    return user


def _make_deployment(user_id: str = "owner-1", deployment_id: str = "dep-1") -> MagicMock:
    deployment = MagicMock()
    deployment.userId = user_id
    deployment.deploymentId = deployment_id
    return deployment


# ----------------------------------------------------------------
# role tuples
# ----------------------------------------------------------------
@pytest.mark.unit
def test_admin_roles_tuple_contains_only_admin():
    assert ADMIN_ROLES == (UserRole.ADMIN,)


@pytest.mark.unit
def test_staff_roles_tuple_contains_teacher_and_admin():
    assert set(STAFF_ROLES) == {UserRole.TEACHER, UserRole.ADMIN}


# ----------------------------------------------------------------
# get_current_active_user
# ----------------------------------------------------------------
@pytest.mark.unit
def test_get_current_active_user_returns_user_unchanged():
    user = _make_user(UserRole.STUDENT)
    assert get_current_active_user(current_user=user) is user


# ----------------------------------------------------------------
# require_roles factory
# ----------------------------------------------------------------
@pytest.mark.unit
def test_require_roles_without_args_raises_value_error():
    with pytest.raises(ValueError):
        require_roles()


@pytest.mark.unit
def test_require_roles_admin_allows_admin_user():
    dep = require_roles(UserRole.ADMIN)
    admin = _make_user(UserRole.ADMIN)
    assert dep(user=admin) is admin


@pytest.mark.unit
def test_require_roles_admin_denies_student():
    dep = require_roles(UserRole.ADMIN)
    student = _make_user(UserRole.STUDENT)
    with pytest.raises(HTTPException) as exc:
        dep(user=student)
    assert exc.value.status_code == status.HTTP_403_FORBIDDEN
    assert exc.value.detail["code"] == "role_required"
    assert exc.value.detail["required"] == [UserRole.ADMIN.value]


@pytest.mark.unit
def test_require_roles_admin_denies_teacher():
    dep = require_roles(UserRole.ADMIN)
    teacher = _make_user(UserRole.TEACHER)
    with pytest.raises(HTTPException) as exc:
        dep(user=teacher)
    assert exc.value.status_code == 403


@pytest.mark.unit
def test_require_roles_staff_allows_teacher_and_admin():
    dep = require_roles(UserRole.TEACHER, UserRole.ADMIN)
    teacher = _make_user(UserRole.TEACHER)
    admin = _make_user(UserRole.ADMIN)
    assert dep(user=teacher) is teacher
    assert dep(user=admin) is admin


@pytest.mark.unit
def test_require_roles_staff_denies_student():
    dep = require_roles(UserRole.TEACHER, UserRole.ADMIN)
    student = _make_user(UserRole.STUDENT)
    with pytest.raises(HTTPException) as exc:
        dep(user=student)
    assert exc.value.status_code == 403
    assert set(exc.value.detail["required"]) == {
        UserRole.TEACHER.value,
        UserRole.ADMIN.value,
    }


# ----------------------------------------------------------------
# is_deployment_owner_view / ensure_deployment_owner_view
# ----------------------------------------------------------------
@pytest.mark.unit
def test_is_deployment_owner_view_owner_returns_true():
    user = _make_user(UserRole.STUDENT, user_id="owner-1")
    deployment = _make_deployment(user_id="owner-1")
    assert is_deployment_owner_view(deployment, user) is True


@pytest.mark.unit
def test_is_deployment_owner_view_non_owner_student_returns_false():
    user = _make_user(UserRole.STUDENT, user_id="someone-else")
    deployment = _make_deployment(user_id="owner-1")
    assert is_deployment_owner_view(deployment, user) is False


@pytest.mark.unit
def test_is_deployment_owner_view_teacher_bypass_returns_true():
    teacher = _make_user(UserRole.TEACHER, user_id="teacher-1")
    deployment = _make_deployment(user_id="owner-1")
    assert is_deployment_owner_view(deployment, teacher) is True


@pytest.mark.unit
def test_is_deployment_owner_view_admin_bypass_returns_true():
    admin = _make_user(UserRole.ADMIN, user_id="admin-1")
    deployment = _make_deployment(user_id="owner-1")
    assert is_deployment_owner_view(deployment, admin) is True


@pytest.mark.unit
def test_ensure_deployment_owner_view_passes_for_owner():
    user = _make_user(UserRole.STUDENT, user_id="owner-1")
    deployment = _make_deployment(user_id="owner-1")
    ensure_deployment_owner_view(deployment, user)


@pytest.mark.unit
def test_ensure_deployment_owner_view_passes_for_staff():
    teacher = _make_user(UserRole.TEACHER, user_id="teacher-1")
    deployment = _make_deployment(user_id="owner-1")
    ensure_deployment_owner_view(deployment, teacher)


@pytest.mark.unit
def test_ensure_deployment_owner_view_raises_for_non_owner_student():
    student = _make_user(UserRole.STUDENT, user_id="member-1")
    deployment = _make_deployment(user_id="owner-1")
    with pytest.raises(HTTPException) as exc:
        ensure_deployment_owner_view(deployment, student)
    assert exc.value.status_code == 403
