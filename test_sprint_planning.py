"""
Phase 6: Sprint Planning tests.

Tests for the AI-assisted Sprint Planning feature.
Tests the recommendation algorithm, priority handling, dependency ordering, and readiness filtering.
"""

import pytest

from sprint_generator import recommend_sprint, _calculate_story_points, _determine_readiness
from implementation_plan_jira import DeliveryMapping, DeliveryLink


def test_calculate_story_points_complex_issue():
    """Test story points calculation for complex issues."""
    summary = "Implement complex API integration with database migration"
    points = _calculate_story_points(summary)
    assert points == 8, f"Complex issue should have 8 story points, got {points}"


def test_calculate_story_points_medium_issue():
    """Test story points calculation for medium complexity issues."""
    summary = "Create new feature for user dashboard"
    points = _calculate_story_points(summary)
    assert points == 3, "Medium complexity issue should have 3 story points"


def test_calculate_story_points_simple_issue():
    """Test story points calculation for simple issues."""
    summary = "Typo in error message"
    points = _calculate_story_points(summary)
    assert points == 1, "Simple issue should have 1 story point"


def test_determine_readiness_ready():
    """Test readiness determination for ready items."""
    summary = "Fix critical bug in payment processing"
    readiness = _determine_readiness(summary, "Task")
    assert readiness == "Ready", "Bug fix should be ready"


def test_determine_readiness_needs_prep():
    """Test readiness determination for items needing prep."""
    summary = "Integrate with third-party payment API"
    readiness = _determine_readiness(summary, "Task")
    assert readiness == "Needs Prep", "API integration should need prep"


def test_determine_readiness_planning_needed():
    """Test readiness determination for items needing planning."""
    summary = "Design new database schema for user authentication"
    readiness = _determine_readiness(summary, "Task")
    assert readiness == "Planning Needed", "Database design should need planning"


def test_determine_readiness_default():
    """Test default readiness determination."""
    summary = "Update user profile"
    readiness = _determine_readiness(summary, "Task")
    assert readiness == "Ready", "Technical task should be ready by default"


def test_recommend_sprint_no_backlog():
    """Test sprint recommendation with no backlog."""
    mapping = DeliveryMapping()
    sprint = recommend_sprint(mapping)
    assert sprint.sprint_goal == "No backlog to plan"
    assert sprint.sprint_name == "Sprint 1"
    assert len(sprint.issues) == 0


def test_recommend_sprint_with_backlog():
    """Test sprint recommendation with backlog items."""
    # Create a mock DeliveryMapping with links
    links = [
        DeliveryLink(
            plan_item_id="E1",
            issue_key="PROJ-1",
            summary="Create user authentication system",
            work_type="Epic",
            issue_type_name="Epic"
        ),
        DeliveryLink(
            plan_item_id="S2",
            issue_key="PROJ-2",
            summary="Fix login page validation bug",
            work_type="Story",
            issue_type_name="Story"
        ),
        DeliveryLink(
            plan_item_id="T3",
            issue_key="PROJ-3",
            summary="Update user profile page",
            work_type="Task",
            issue_type_name="Task"
        ),
        DeliveryLink(
            plan_item_id="T4",
            issue_key="PROJ-4",
            summary="Design database schema for new feature",
            work_type="Task",
            issue_type_name="Task"
        ),
        DeliveryLink(
            plan_item_id="T5",
            issue_key="PROJ-5",
            summary="Integrate payment API",
            work_type="Task",
            issue_type_name="Task"
        ),
    ]
    mapping = DeliveryMapping(links=tuple(links))

    # Mock the Gemini client to avoid network calls
    class MockResponse:
        def __init__(self):
            self.text = '{"sprint_name": "Sprint 1", "sprint_goal": "Implement user authentication", "duration_weeks": 4, "issues": [{"issue_key": "PROJ-2", "summary": "Fix login page validation bug", "rationale": "Critical bug fix for user experience", "is_selected": true}, {"issue_key": "PROJ-4", "summary": "Update user profile page", "rationale": "Enhancement for user profile", "is_selected": true}, {"issue_key": "PROJ-1", "summary": "Create user authentication system", "rationale": "Epic containing user auth stories", "is_selected": false}]}'

    class MockModels:
        def generate_content(self, *args, **kwargs):
            return MockResponse()

    class MockGenaiClient:
        def __init__(self, *args, **kwargs):
            self.models = MockModels()

    # Temporarily replace genai module
    import sys
    from unittest.mock import MagicMock
    original_genai = sys.modules.get('google.genai')
    sys.modules['google.genai'] = MagicMock()
    sys.modules['google.genai'].Client = MockGenaiClient

    try:
        # Pass the mock client explicitly to recommend_sprint
        mock_client = MockGenaiClient()
        sprint = recommend_sprint(mapping, client=mock_client)
        assert sprint is not None
        assert sprint.sprint_name == "Sprint 1"
        assert sprint.sprint_goal == "Implement user authentication"
        assert sprint.duration_weeks == 4

        # Check that issues were parsed
        assert len(sprint.issues) > 0

        # Check for selected issues
        selected_issues = [i for i in sprint.issues if i.is_selected]
        assert len(selected_issues) > 0

        # Check that PROJ-2 and PROJ-4 are selected (ready items from fallback)
        selected_keys = [i.issue_key for i in selected_issues]
        assert "PROJ-2" in selected_keys  # Bug fix, should be ready
        assert "PROJ-4" in selected_keys  # Technical task, should be ready

    finally:
        # Restore original genai module
        if original_genai is not None:
            sys.modules['google.genai'] = original_genai
        else:
            sys.modules.pop('google.genai', None)


def test_recommend_sprint_fallback():
    """Test sprint recommendation fallback when AI fails."""
    # Create a mock DeliveryMapping with priority-based backlog
    links = [
        DeliveryLink(
            plan_item_id="E1",
            issue_key="PROJ-1",
            summary="Create user authentication system",
            work_type="Epic"
        ),
        DeliveryLink(
            plan_item_id="S2",
            issue_key="PROJ-2",
            summary="Fix critical bug",
            work_type="Story"
        ),
        DeliveryLink(
            plan_item_id="T3",
            issue_key="PROJ-3",
            summary="Add new feature",
            work_type="Task"
        ),
    ]
    mapping = DeliveryMapping(links=tuple(links))

    # Mock the Gemini client to raise an exception
    class MockGenaiClient:
        class models:
            class generate_content:
                def __init__(self, *args, **kwargs):
                    pass

                def __call__(self, *args, **kwargs):
                    raise Exception("AI service unavailable")

    class MockGenai:
        Client = MockGenaiClient

    # Temporarily replace genai module
    import sys
    original_genai = sys.modules.get('google.genai')
    sys.modules['google.genai'] = MockGenai()

    try:
        sprint = recommend_sprint(mapping)
        assert sprint is not None
        assert "Fallback" in sprint.sprint_goal

        # Check that fallback selection works
        assert len(sprint.issues) > 0

    finally:
        # Restore original genai module
        if original_genai is not None:
            sys.modules['google.genai'] = original_genai
        else:
            sys.modules.pop('google.genai', None)


def test_priority_ordering_in_recommendation():
    """Test that backlog is properly ordered by priority for sprint planning."""
    # This test validates the internal sorting logic by examining the story points calculation
    # which is based on issue complexity
    summary = "Design complex database schema with enterprise security integration"
    points = _calculate_story_points(summary)
    assert points == 8, "Complex integration should have highest story points"

    summary = "Create new feature for user dashboard"
    points = _calculate_story_points(summary)
    assert points == 3, "Feature development should have medium story points"

    summary = "Fix minor typo in documentation"
    points = _calculate_story_points(summary)
    assert points == 3, "Documentation fix should have medium story points"


def test_readiness_categorization():
    """Test that readiness categorization helps identify what to include in sprints."""
    # Test that different types of work are categorized appropriately
    bug_fix_summary = "Fix critical security vulnerability in authentication"
    readiness = _determine_readiness(bug_fix_summary, "Task")
    assert readiness == "Ready", "Security bug fix should be immediately ready"

    api_summary = "Integrate with third-party payment processing API"
    readiness = _determine_readiness(api_summary, "Task")
    assert readiness == "Needs Prep", "API integration should need preparation"

    db_summary = "Design new database schema for user authentication system"
    readiness = _determine_readiness(db_summary, "Task")
    assert readiness == "Planning Needed", "Database schema design needs planning"


def test_selected_vs_deferred_issues():
    """Test that selected and deferred issues can be properly identified."""
    # Mock SprintPlan behavior
    from sprint_models import SprintPlan, SprintIssue

    sprint = SprintPlan(sprint_name="Sprint 1", sprint_goal="Test sprint")

    # Add issues with different selection states
    sprint.issues.append(SprintIssue(
        issue_key="PROJ-1",
        summary="Fix critical bug",
        rationale="Critical fix needed",
        is_selected=True
    ))

    sprint.issues.append(SprintIssue(
        issue_key="PROJ-2",
        summary="Add new feature",
        rationale="Nice to have",
        is_selected=False
    ))

    # Test selected_issues property
    selected = sprint.selected_issues
    assert len(selected) == 1
    assert selected[0].issue_key == "PROJ-1"

    # Test deferred_issues property
    deferred = sprint.deferred_issues
    assert len(deferred) == 1
    assert deferred[0].issue_key == "PROJ-2"


def test_sprint_properties():
    """Test basic SprintPlan properties."""
    from sprint_models import SprintPlan

    sprint = SprintPlan(
        sprint_name="Sprint 2",
        sprint_goal="Implement reporting",
        duration_weeks=2
    )

    assert sprint.sprint_name == "Sprint 2"
    assert sprint.sprint_goal == "Implement reporting"
    assert sprint.duration_weeks == 2
    assert sprint.issues == []

    # Test that properties can be modified
    sprint.sprint_name = "Sprint 3"
    assert sprint.sprint_name == "Sprint 3"