"""
Phase 1 of the delivery-lifecycle migration: the lifecycle foundation and its workspace.

Two things are pinned here. First, that every stage status is *derived* from an
artifact the session actually holds, so no stage can claim progress that was not made
-- in particular that a generated BRD stops at "Pending Review", because this app has
no approval control and silently approving an artifact is what the product direction
forbids. Second, that the removed JIRA-010 requirement-change governance stays removed:
Jira must not be able to rewrite the BRD, so the symbols that did it are asserted gone
rather than merely deleted once.
"""

import pytest
import streamlit as st

import main
from brd_models import BRDData, ProjectOverview, Requirement
from jira_models import CreatedIssue
from lifecycle_models import (
    APPROVED,
    ARCHITECTURE,
    DELIVERY_STATUS,
    DISCOVERY_BRD,
    DRAFT,
    IMPLEMENTATION_PLAN,
    IMPLEMENTED_STAGES,
    IN_PROGRESS,
    LIFECYCLE_STAGES,
    NOT_STARTED,
    PENDING_REVIEW,
    PRD,
    STAGE_LABEL,
    TEST_CASES,
    TEST_EXECUTION,
    ProjectLifecycle,
    StageState,
    lifecycle_from,
)
from test_jira_sites import (  # noqa: F401 -- fixtures are used by pytest, not by name
    clean_session,
    forbid_write_requests,
)
# The same builders the Jira tests use, so a plan here is the object the review step
# actually produces rather than a fixture shaped to suit the lifecycle.
from test_jira_creation import a_plan
from test_jira_work_plan import PROJECT, a_brd


# --- The stage vocabulary -------------------------------------------------

def test_the_lifecycle_is_the_eight_stages_in_delivery_order():
    """The navigation is the product's flow, so its order is part of the contract."""
    assert [STAGE_LABEL[stage] for stage in LIFECYCLE_STAGES] == [
        "Discovery → BRD",
        "Product Definition → PRD",
        "Architecture",
        "Implementation Plan",
        "Sprint Planning",
        "Test Cases",
        "Test Execution",
        "Jira / Delivery Status",
    ]
    # Only what is built is claimed as built.
    assert IMPLEMENTED_STAGES == (
        DISCOVERY_BRD,
        PRD,
        ARCHITECTURE,
        IMPLEMENTATION_PLAN,
        TEST_CASES,
        TEST_EXECUTION,
        DELIVERY_STATUS,
    )


@pytest.mark.parametrize(
    "call",
    [
        lambda: StageState(status="Shipped"),
        lambda: ProjectLifecycle().record(DISCOVERY_BRD, "Shipped"),
        lambda: ProjectLifecycle().record("deployment", NOT_STARTED),
    ],
    ids=["unknown-status", "unknown-status-recorded", "unknown-stage"],
)
def test_an_unknown_stage_or_status_is_refused_where_it_is_set(call):
    with pytest.raises(ValueError):
        call()


# --- Derived status -------------------------------------------------------

def test_without_a_brd_nothing_reports_progress():
    lifecycle = lifecycle_from()

    assert lifecycle.brd is None
    assert lifecycle.is_started is False
    for stage in LIFECYCLE_STAGES:
        assert lifecycle.state(stage).status == NOT_STARTED, stage
        assert lifecycle.state(stage).detail


def test_a_generated_brd_is_pending_review_and_never_silently_approved():
    brd = a_brd()

    lifecycle = lifecycle_from(brd=brd, discovery_source="google_meet")

    discovery = lifecycle.state(DISCOVERY_BRD)
    assert discovery.status == PENDING_REVIEW
    assert discovery.status != APPROVED
    assert lifecycle.project_title == brd.project_title
    assert lifecycle.brd is brd
    # The counts and the route it arrived by are reported, not guessed at.
    assert "2 functional" in discovery.detail and "1 non-functional" in discovery.detail
    assert "google_meet" in discovery.detail


def test_the_later_stages_report_that_they_are_not_implemented():
    lifecycle = lifecycle_from(brd=a_brd())

    later = [
        stage
        for stage in LIFECYCLE_STAGES
        if stage not in IMPLEMENTED_STAGES
    ]
    assert later, "the lifecycle should have stages beyond the ones that are built"
    for stage in later:
        state = lifecycle.state(stage)
        assert state.status == NOT_STARTED, stage
        assert "not implemented yet" in state.detail, stage


@pytest.mark.parametrize(
    "plan, created, expected",
    [
        (None, (), NOT_STARTED),
        ("plan", (), DRAFT),
        ("plan", (CreatedIssue(plan_key="FR-1", error="Jira returned HTTP 400"),), DRAFT),
        ("plan", (CreatedIssue(plan_key="FR-1", issue_key="ENG-1", issue_id="1"),), IN_PROGRESS),
    ],
    ids=["nothing", "plan-only", "failed-creation", "created"],
)
def test_the_delivery_stage_follows_the_jira_work_that_actually_exists(plan, created, expected):
    """A proposal is a draft; only a created issue is delivery in progress."""
    lifecycle = lifecycle_from(
        brd=a_brd(), plan=a_plan() if plan else None, created=created
    )

    assert lifecycle.state(DELIVERY_STATUS).status == expected


def test_a_jira_status_change_cannot_reach_the_brd():
    """
    The rule the removed feature broke: Jira is the delivery source of truth, the BRD is
    the requirements source of truth. Moving an issue to Done changes the delivery
    stage's reading and nothing about the requirement it came from.
    """
    brd = a_brd()
    before = [(r.requirement_id, r.statement) for r in brd.functional_requirements]

    done = lifecycle_from(
        brd=brd,
        plan=a_plan(),
        created=(CreatedIssue(plan_key="FR-1", issue_key="ENG-1", issue_id="1"),),
    )

    assert done.state(DELIVERY_STATUS).status == IN_PROGRESS
    assert done.state(DISCOVERY_BRD).status == PENDING_REVIEW
    assert [(r.requirement_id, r.statement) for r in brd.functional_requirements] == before


# --- The workspace in main ------------------------------------------------

def test_storing_a_brd_records_the_route_it_arrived_by():
    main._store_brd(a_brd(), "microsoft_teams")

    assert st.session_state[main.BRD_SOURCE_SESSION_KEY] == "microsoft_teams"
    assert isinstance(st.session_state[main.BRD_SESSION_KEY], BRDData)


def test_the_workspace_lists_every_stage_and_holds_no_jira_service(monkeypatch):
    """
    Rendering the workspace reads session state only. It takes no token and makes no
    call, so displaying delivery status cannot mutate anything.
    """
    shown = []
    for name in ("markdown", "caption", "info", "success", "subheader"):
        monkeypatch.setattr(main.st, name, lambda text, **k: shown.append(str(text)))
    monkeypatch.setattr(
        main.JiraService, "__init__", lambda *a, **k: pytest.fail("the workspace takes no service")
    )

    main._store_brd(a_brd(), "manual")
    st.session_state[main._skey(main.JIRA_STATE_NAME, "created")] = (
        CreatedIssue(plan_key="FR-1", issue_key="ENG-1", issue_id="1"),
    )
    main._render_lifecycle_workspace()

    rendered = " ".join(shown)
    # Every stage is navigable, and each one reports the status derived above.
    for stage in LIFECYCLE_STAGES:
        assert STAGE_LABEL[stage] in rendered, stage
    assert "**Discovery → BRD** — Pending Review" in rendered
    assert "**Jira / Delivery Status** — In Progress" in rendered
    # The stages with no generator say so instead of offering a control.
    assert rendered.count("(not implemented yet)") == len(LIFECYCLE_STAGES) - len(
        IMPLEMENTED_STAGES
    )


# --- The removed governance stays removed ---------------------------------

def test_no_jira_to_brd_synchronization_remains():
    for gone in (
        "detect_meeting_changes",
        "detect_jira_changes",
        "apply_approved_changes",
        "decide_change",
        "synchronized_baseline",
        "_render_requirement_changes_section",
        "_render_jira_drift_detection",
        "_record_change_decision",
    ):
        assert not hasattr(main, gone), gone

    with pytest.raises(ImportError):
        import jira_change_detector  # noqa: F401

    import jira_models
    import jira_service

    for gone in ("RequirementChange", "ChangeProposal", "APPROVAL_STATES", "CHANGE_TYPES"):
        assert not hasattr(jira_models, gone), gone
    # The drift-only read is gone; the one write and the read-only discovery stay.
    assert not hasattr(jira_service.JiraService, "get_issue_fields")
    assert hasattr(jira_service.JiraService, "create_issue")


def test_no_requirement_change_state_survives_a_stored_brd():
    for suffix in ("changes", "change_baseline"):
        assert suffix not in main._JIRA_PROJECT_SUFFIXES

    st.session_state[main._skey(main.JIRA_STATE_NAME, "created")] = (
        CreatedIssue(plan_key="FR-1", issue_key="ENG-1", issue_id="1"),
    )
    main._store_brd(a_brd(project_title="A later meeting"), "manual")

    assert main._skey(main.JIRA_STATE_NAME, "created") not in st.session_state


# --- Traceability the new lifecycle still depends on ----------------------

def test_created_issues_still_carry_the_requirements_they_came_from():
    """
    The mapping the delivery stage reads. Removing requirement-change governance must
    not have removed the link from a Jira issue back to its BRD requirement.
    """
    record = CreatedIssue(
        plan_key="FR-1",
        issue_key="ENG-1",
        issue_id="1",
        source_requirement_ids=("FR-1",),
        source_action_item_ids=("AI-1",),
    )

    assert record.succeeded is True
    assert record.source_requirement_ids == ("FR-1",)
    assert record.source_action_item_ids == ("AI-1",)

    plan = a_plan()
    assert any(issue.requirement_ids for issue in plan.issues)
    assert PROJECT.key
    assert isinstance(a_brd().project_overview, ProjectOverview)
    assert isinstance(a_brd().functional_requirements[0], Requirement)
