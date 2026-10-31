"""Unit: a project's status graph and who reaches which projects (no DB)."""

import pytest

from src.modules.projects.model import (
    PROJECT_TRANSITIONS,
    QUOTABLE_STATUSES,
    STATUS_LABELS,
    ProjectModel,
    ProjectStatus,
)
from src.modules.projects.service import ProjectService, owner_scope
from src.modules.users.model import UserModel
from src.shared.exceptions import BusinessRuleError, EntityNotFoundError


def _user(role: str, id: int = 7) -> UserModel:
    return UserModel(id=id, email="x@y.com", hashed_password="x", roles=[role])


def _project(status: ProjectStatus, owner_id: int = 7) -> ProjectModel:
    return ProjectModel(
        id=1, name="Cocina", owner_id=owner_id, status=status.value, payload={}
    )


def test_every_status_has_a_row_and_a_label():
    assert set(PROJECT_TRANSITIONS) == set(ProjectStatus)
    assert set(STATUS_LABELS) == set(ProjectStatus)


def test_quoted_is_no_patch_target_and_no_way_out():
    """Only creating a pre-order puts a project in ``quoted``, and it stays there."""
    assert all(
        ProjectStatus.quoted not in targets for targets in PROJECT_TRANSITIONS.values()
    )
    assert PROJECT_TRANSITIONS[ProjectStatus.quoted] == frozenset()


def test_an_archived_project_reopens_before_it_is_handed_over():
    assert PROJECT_TRANSITIONS[ProjectStatus.archived] == {ProjectStatus.in_progress}


def test_what_a_seller_can_quote():
    assert QUOTABLE_STATUSES == {ProjectStatus.ready, ProjectStatus.quoted}


@pytest.mark.parametrize("role", ["administrador", "vendedor"])
def test_the_office_reaches_every_project(role):
    assert owner_scope(_user(role)) is None


def test_the_designer_reaches_only_their_own():
    assert owner_scope(_user("proyectista", id=42)) == 42


def test_another_designers_project_is_a_404(mock_session):
    mock_session.get.return_value = _project(ProjectStatus.in_progress, owner_id=1)
    with pytest.raises(EntityNotFoundError):
        ProjectService(mock_session).get_owned_or_404(1, scope=2)


def test_ready_stamps_the_hand_over(mock_session):
    project = _project(ProjectStatus.in_progress)
    mock_session.get.return_value = project
    ProjectService(mock_session).change_status(1, ProjectStatus.ready, scope=7)
    assert project.status == "ready"
    assert project.ready_at is not None


@pytest.mark.parametrize(
    "current,target",
    [
        (ProjectStatus.in_progress, ProjectStatus.quoted),
        (ProjectStatus.archived, ProjectStatus.ready),
        (ProjectStatus.quoted, ProjectStatus.in_progress),
        (ProjectStatus.quoted, ProjectStatus.archived),
    ],
)
def test_a_move_off_the_graph_is_refused_untouched(mock_session, current, target):
    project = _project(current)
    mock_session.get.return_value = project
    with pytest.raises(BusinessRuleError) as exc:
        ProjectService(mock_session).change_status(1, target, scope=7)
    assert exc.value.field == "status"
    assert project.status == current.value
    mock_session.commit.assert_not_called()
