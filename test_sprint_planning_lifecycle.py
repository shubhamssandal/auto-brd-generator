"""
Lifecycle tests for Sprint Planning stage.
Tests that the Sprint Planning stage is implemented correctly within the project lifecycle,
generates plans from implementation plans, and integrates with sprint execution.
"""
import pytest
from unittest.mock import Mock

from implementation_plan_models import (
    ImplementationPlan,
    Story,
    TechnicalTask,
)
from lifecycle_models import (
    DISCOVERY_BRD,
    IMPLEMENTED_STAGES,
    IMPLEMENTATION_PLAN,
    LIFECYCLE_STAGES,
    NOT_STARTED,
    PENDING_REVIEW,
    ProjectLifecycle,
    StageState,
    SPRINT_PLAN,
    lifecycle_from,
)
from sprint_generator import generate_sprint_plan_from_implementation_plan
from sprint_models import SprintPlan, SprintIssue


def test_sprint_plan_stage_is_implemented():
    """Sprint Planning is now listed as an implemented lifecycle stage."""
    assert SPRINT_PLAN in IMPLEMENTED_STAGES


def test_sprint_plan_stage_label_exists():
    """Sprint Planning has a proper label in STAGE_LABEL."""
    from lifecycle_models import STAGE_LABEL
    assert SPRINT_PLAN in STAGE_LABEL
    assert STAGE_LABEL[SPRINT_PLAN] == "Sprint Planning"


def test_sprint_plan_stage_position_in_lifecycle():
    """Sprint Planning appears after Implementation Plan and before Test Cases."""
    stages_list = list(LIFECYCLE_STAGES)
    impl_idx = stages_list.index(IMPLEMENTATION_PLAN)
    sprint_idx = stages_list.index(SPRINT_PLAN)
    test_idx = stages_list.index("test_cases")  # TEST_CASES constant
    assert sprint_idx == impl_idx + 1
    assert test_idx == sprint_idx + 1


def test_lifecycle_records_sprint_plan_not_started_when_no_inputs():
    """When no implementation plan exists, Sprint Planning reports Not Started."""
    lifecycle = lifecycle_from()
    state = lifecycle.state(SPRINT_PLAN)
    assert state.status == NOT_STARTED
    assert state.detail == "Generate a sprint plan from the approved implementation plan."


def test_lifecycle_records_sprint_plan_not_started_when_unapproved_plan():
    """When implementation plan exists but is not approved, Sprint Planning reports Not Started."""
    plan = ImplementationPlan(stories=[
        Story(story_id="S1", title="Task", user_story="U", acceptance_criteria=("A",),
              tasks=(TechnicalTask(task_id="T1", title="T"),))
    ])
    lifecycle = lifecycle_from(
        implementation_plan=plan,
        implementation_plan_approved=False,  # explicitly unapproved
    )
    state = lifecycle.state(SPRINT_PLAN)
    # The gate is the approved implementation plan, so sprint planning is blocked
    assert state.status == NOT_STARTED
    assert "Generate a sprint plan" in state.detail


def test_lifecycle_records_sprint_plan_pending_review_when_generated_but_not_approved():
    """When a sprint plan has been generated but not approved, Sprint Planning reports Pending Review."""
    from sprint_models import SprintPlan
    plan = ImplementationPlan(stories=[
        Story(story_id="S1", title="Task", user_story="U", acceptance_criteria=("A",),
              tasks=(TechnicalTask(task_id="T1", title="T"),))
    ])
    # Need upstream approvals for sprint plan to be considered "ready"
    # And we need to provide a generated sprint plan that's not approved
    lifecycle = lifecycle_from(
        implementation_plan=plan,
        implementation_plan_approved=True,
        prd_approved=True,      # Need approved PRD for approved architecture
        brd_approved=True,      # Need approved BRD for approved PRD
        architecture_approved=True,  # Need approved architecture for approved impl plan
        sprint_plan=SprintPlan(sprint_name="Sprint 1"),  # Generated but not approved plan
        sprint_plan_approved=False,
    )
    state = lifecycle.state(SPRINT_PLAN)
    assert state.status == PENDING_REVIEW
    assert "Review the generated sprint plan" in state.detail


def test_lifecycle_records_sprint_plan_approved_when_approved():
    """When a sprint plan has been generated and approved, Sprint Planning reports Approved."""
    from sprint_models import SprintPlan
    plan = ImplementationPlan(stories=[
        Story(story_id="S1", title="Task", user_story="U", acceptance_criteria=("A",),
              tasks=(TechnicalTask(task_id="T1", title="T"),))
    ])
    # Need upstream approvals for sprint plan to be considered "ready"
    # And we need to provide a generated sprint plan that's approved
    lifecycle = lifecycle_from(
        implementation_plan=plan,
        implementation_plan_approved=True,
        prd_approved=True,      # Need approved PRD for approved architecture
        brd_approved=True,      # Need approved BRD for approved PRD
        architecture_approved=True,  # Need approved architecture for approved impl plan
        sprint_plan=SprintPlan(sprint_name="Sprint 1"),  # Generated and approved plan
        sprint_plan_approved=True,
    )
    state = lifecycle.state(SPRINT_PLAN)
    assert state.status == "Approved"
    assert "Sprint plan approved" in state.detail


def test_generate_sprint_plan_preserves_story_ids():
    """Generated sprint plan sets story_id on each SprintIssue for execution resolution."""
    plan = ImplementationPlan(stories=[
        Story(story_id="STORY-1", title="Login", user_story="U", acceptance_criteria=("A",),
              tasks=(TechnicalTask(task_id="T1", title="Validate password"),)),
        Story(story_id="STORY-2", title="Checkout", user_story="U", acceptance_criteria=("A",),
              tasks=(TechnicalTask(task_id="T2", title="Process payment"),)),
    ])
    sprint_plan = generate_sprint_plan_from_implementation_plan(plan)
    assert len(sprint_plan.issues) == 2
    assert sprint_plan.issues[0].story_id == "STORY-1"
    assert sprint_plan.issues[1].story_id == "STORY-2"
    # All issues should be selected by default
    assert all(issue.is_selected for issue in sprint_plan.issues)


def test_generate_sprint_plan_preserves_existing_jira_issue_keys():
    """When a delivery mapping exists, the generated sprint plan preserves existing issue_key mappings."""
    from implementation_plan_jira import DeliveryMapping, DeliveryLink

    plan = ImplementationPlan(stories=[
        Story(story_id="S1", title="Login", user_story="U", acceptance_criteria=("A",),
              tasks=(TechnicalTask(task_id="T1", title="Validate password"),)),
        Story(story_id="S2", title="Checkout", user_story="U", acceptance_criteria=("A",),
              tasks=(TechnicalTask(task_id="T2", title="Process payment"),)),
    ])
    # Pre-existing mapping: S1 already created as PROJ-101 in Jira
    delivery_mapping = DeliveryMapping(
        project_identifier="TEST",
        project_label="Test Project",
        links=(
            DeliveryLink(
                plan_item_id="S1",
                issue_key="PROJ-101",
                issue_type_name="Story",
                summary="Login",
                work_type="Story",
            ),
        ),
    )
    sprint_plan = generate_sprint_plan_from_implementation_plan(
        plan,
        delivery_mapping=delivery_mapping,
    )
    assert len(sprint_plan.issues) == 2
    # First issue preserves the Jira key
    assert sprint_plan.issues[0].issue_key == "PROJ-101"
    # Second issue has no existing mapping, so empty issue_key
    assert sprint_plan.issues[1].issue_key == ""
    # Both have correct story IDs
    assert sprint_plan.issues[0].story_id == "S1"
    assert sprint_plan.issues[1].story_id == "S2"


def test_generate_sprint_plan_defaults_approved_false():
    """Generated sprint plan always starts with approved=False regardless of inputs."""
    plan = ImplementationPlan(stories=[
        Story(story_id="S1", title="Task", user_story="U", acceptance_criteria=("A",),
              tasks=(TechnicalTask(task_id="T1", title="T"),))
    ])
    # Test with and without delivery mapping
    sprint_plan1 = generate_sprint_plan_from_implementation_plan(plan)
    assert sprint_plan1.approved is False

    from implementation_plan_jira import DeliveryMapping, DeliveryLink
    delivery_mapping = DeliveryMapping(
        project_identifier="TEST",
        project_label="Test Project",
        links=(DeliveryLink(plan_item_id="S1", issue_key="PROJ-1"),),
    )
    sprint_plan2 = generate_sprint_plan_from_implementation_plan(
        plan, delivery_mapping=delivery_mapping
    )
    assert sprint_plan2.approved is False


def test_sprint_plan_can_be_approved_and_revoked():
    """Sprint plan approval flag can be toggled True/False."""
    plan = ImplementationPlan(stories=[
        Story(story_id="S1", title="Task", user_story="U", acceptance_criteria=("A",),
              tasks=(TechnicalTask(task_id="T1", title="T"),))
    ])
    sprint_plan = generate_sprint_plan_from_implementation_plan(plan)
    assert sprint_plan.approved is False

    # Approve it
    sprint_plan.approved = True
    assert sprint_plan.approved is True

    # Revoke it
    sprint_plan.approved = False
    assert sprint_plan.approved is False


def test_sprint_plan_contains_expected_issue_count():
    """Sprint plan contains exactly one issue per story in the implementation plan."""
    plan = ImplementationPlan(stories=[
        Story(story_id=f"S{i}", title=f"Story {i}", user_story="U", acceptance_criteria=("A",),
              tasks=(TechnicalTask(task_id=f"T{i}", title=f"Task {i}"),))
        for i in range(5)
    ])
    sprint_plan = generate_sprint_plan_from_implementation_plan(plan)
    assert len(sprint_plan.issues) == 5
    for i, issue in enumerate(sprint_plan.issues):
        assert issue.story_id == f"S{i}"
        assert issue.summary == f"Story {i}"


def test_sprint_plan_respects_story_order():
    """Sprint plan issues follow the implementation plan's dependency order."""
    # Create stories with dependencies: S2 depends on S1, S3 depends on S2
    plan = ImplementationPlan(stories=[
        Story(story_id="S1", title="Foundation", user_story="U", acceptance_criteria=("A",),
              tasks=(TechnicalTask(task_id="T1", title="Setup"),)),
        Story(story_id="S2", title="Middleware", user_story="U", acceptance_criteria=("A",),
              tasks=(TechnicalTask(task_id="T2", title="Build"),), depends_on=("S1",)),
        Story(story_id="S3", title="UI", user_story="U", acceptance_criteria=("A",),
              tasks=(TechnicalTask(task_id="T3", title="Render"),), depends_on=("S2",)),
    ])
    sprint_plan = generate_sprint_plan_from_implementation_plan(plan)
    # Should maintain order: S1, S2, S3
    assert [issue.story_id for issue in sprint_plan.issues] == ["S1", "S2", "S3"]


def test_empty_implementation_plan_yields_empty_sprint():
    """An implementation plan with no stories yields a sprint plan with no issues."""
    plan = ImplementationPlan(stories=())
    sprint_plan = generate_sprint_plan_from_implementation_plan(plan)
    assert len(sprint_plan.issues) == 0
    assert sprint_plan.sprint_name == "Sprint 1"
    assert sprint_plan.sprint_goal == ""
    assert sprint_plan.duration_weeks == 4
    assert sprint_plan.approved is False