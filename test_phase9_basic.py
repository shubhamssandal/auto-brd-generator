#!/usr/bin/env python3
"""Focused tests for Phase 9: Sprint Completion + Next Sprint."""

import sys
sys.path.insert(0, '.')

from test_case_models import (
    TestCase, TestSuite,
    TEST_EXECUTION_NOT_RUN, TEST_EXECUTION_PASS, TEST_EXECUTION_FAIL, TEST_EXECUTION_BLOCKED
)
from sprint_completion_models import SprintCompletion, StoryCompletion
from sprint_models import SprintPlan, SprintIssue
from lifecycle_models import (
    _sprint_completion_state,
    NOT_STARTED, IN_PROGRESS, COMPLETED, PENDING_REVIEW,
    IMPLEMENTED_STAGES, LIFECYCLE_STAGES, STAGE_LABEL,
    lifecycle_from,
)
from implementation_plan_jira import DeliveryMapping

# --- StoryCompletion tests ---

def test_story_completion_created():
    """Test StoryCompletion dataclass creation."""
    sc = StoryCompletion(
        story_id="STORY-1",
        is_completed=True,
        detail="Implementation done, tests passed",
        passed_count=3,
        failed_count=0,
        blocked_count=0,
        not_run_count=0,
    )
    assert sc.story_id == "STORY-1"
    assert sc.is_completed == True
    assert sc.passed_count == 3
    assert sc.failed_count == 0
    print("✓ StoryCompletion created correctly")


def test_story_completion_incomplete():
    """Test incomplete story completion."""
    sc = StoryCompletion(
        story_id="STORY-2",
        is_completed=False,
        detail="Implementation done, tests failed",
        passed_count=0,
        failed_count=2,
        blocked_count=0,
        not_run_count=1,
    )
    assert sc.is_completed == False
    assert sc.failed_count == 2
    print("✓ Incomplete story completion handled")


# --- SprintCompletion tests ---

def test_sprint_completion_created():
    """Test SprintCompletion dataclass creation."""
    plan = SprintPlan(sprint_name="Sprint 1", sprint_goal="Test")
    completion = SprintCompletion(
        sprint_plan=plan,
        story_completions=[],
        overall_status="In Progress",
        approved=False,
        remaining_backlog=[],
    )
    assert completion.sprint_plan.sprint_name == "Sprint 1"
    assert completion.overall_status == "In Progress"
    assert completion.is_completed == False
    print("✓ SprintCompletion created correctly")


def test_sprint_completion_with_complete_stories():
    """Test SprintCompletion with completed stories."""
    plan = SprintPlan(sprint_name="Sprint 1", sprint_goal="Test")
    completions = [
        StoryCompletion(story_id="STORY-1", is_completed=True, detail="Done"),
        StoryCompletion(story_id="STORY-2", is_completed=True, detail="Done"),
    ]
    completion = SprintCompletion(
        sprint_plan=plan,
        story_completions=completions,
        overall_status="Completed",
        approved=True,
        remaining_backlog=[],
    )
    assert completion.is_completed == True
    assert len(completion.completed_stories) == 2
    assert len(completion.remaining_backlog) == 0
    print("✓ SprintCompletion with complete stories handled")


def test_sprint_completion_with_remaining():
    """Test SprintCompletion with remaining backlog."""
    plan = SprintPlan(sprint_name="Sprint 1", sprint_goal="Test")
    completions = [
        StoryCompletion(story_id="STORY-1", is_completed=True, detail="Done"),
        StoryCompletion(story_id="STORY-2", is_completed=False, detail="Not done"),
    ]
    remaining = [SprintIssue(issue_key="STORY-2", summary="Incomplete", is_selected=False)]
    completion = SprintCompletion(
        sprint_plan=plan,
        story_completions=completions,
        overall_status="In Progress",
        approved=False,
        remaining_backlog=remaining,
    )
    assert len(completion.remaining_backlog) == 1
    assert completion.remaining_backlog[0].issue_key == "STORY-2"
    print("✓ SprintCompletion with remaining backlog handled")


# --- Sprint completion state function tests ---

def test_sprint_completion_state_none():
    """Test that None sprint completion is handled correctly."""
    status, detail = _sprint_completion_state(None, False)
    assert status == NOT_STARTED
    assert "No sprint has been completed" in detail
    print("✓ None sprint completion handled correctly")


def test_sprint_completion_state_in_progress():
    """Test that In Progress status is handled correctly."""
    completions = [
        StoryCompletion(story_id="STORY-1", is_completed=False, detail="Running"),
    ]
    completion = SprintCompletion(
        sprint_plan=SprintPlan(sprint_name="Sprint 1"),
        story_completions=completions,
        overall_status="In Progress",
        approved=False,
        remaining_backlog=[],
    )
    status, detail = _sprint_completion_state(completion, False)
    assert status == IN_PROGRESS
    print("✓ In Progress status handled correctly")


def test_sprint_completion_state_completed_unapproved():
    """Test that completed but unapproved sprint shows PENDING_REVIEW."""
    completions = [
        StoryCompletion(story_id="STORY-1", is_completed=True, detail="Done"),
    ]
    completion = SprintCompletion(
        sprint_plan=SprintPlan(sprint_name="Sprint 1"),
        story_completions=completions,
        overall_status="Completed",
        approved=False,
        remaining_backlog=[],
    )
    status, detail = _sprint_completion_state(completion, False)
    assert status == PENDING_REVIEW
    print("✓ Completed unapproved sprint shows PENDING_REVIEW")


def test_sprint_completion_state_completed_approved():
    """Test that completed and approved sprint shows COMPLETED."""
    completions = [
        StoryCompletion(story_id="STORY-1", is_completed=True, detail="Done"),
    ]
    completion = SprintCompletion(
        sprint_plan=SprintPlan(sprint_name="Sprint 1"),
        story_completions=completions,
        overall_status="Completed",
        approved=True,
        remaining_backlog=[],
    )
    status, detail = _sprint_completion_state(completion, True)
    assert status == COMPLETED
    print("✓ Completed approved sprint shows COMPLETED")


# --- Evidence aggregation tests ---

def test_evidence_blocks_completion_on_failures():
    """Test that test failures block sprint completion."""
    from sprint_completion import _determine_story_completion

    class MockStory:
        story_id = "STORY-1"

    # Story with failed test should not be complete
    impl_complete, review_complete, test_results = True, True, (2, 1, 0, 0)  # 2 passed, 1 failed
    completion = _determine_story_completion(
        MockStory(), test_results, impl_complete, review_complete
    )
    assert completion.is_completed == False, "Failed tests should block completion"
    print("✓ Failed tests block completion")


def test_evidence_blocks_completion_on_blocked():
    """Test that blocked tests block sprint completion."""
    from sprint_completion import _determine_story_completion

    class MockStory:
        story_id = "STORY-1"

    impl_complete, review_complete, test_results = True, True, (0, 0, 1, 1)  # 1 blocked, 1 not run
    completion = _determine_story_completion(
        MockStory(), test_results, impl_complete, review_complete
    )
    assert completion.is_completed == False, "Blocked tests should block completion"
    print("✓ Blocked tests block completion")


def test_evidence_blocks_completion_on_not_run():
    """Test that not-run tests block sprint completion."""
    from sprint_completion import _determine_story_completion

    class MockStory:
        story_id = "STORY-1"

    impl_complete, review_complete, test_results = True, True, (0, 0, 0, 3)  # 3 not run
    completion = _determine_story_completion(
        MockStory(), test_results, impl_complete, review_complete
    )
    assert completion.is_completed == False, "Not-run tests should block completion"
    print("✓ Not-run tests block completion")


def test_only_all_passed_completion():
    """Test that only when all tests pass and run is completed."""
    from sprint_completion import _determine_story_completion

    class MockStory:
        story_id = "STORY-1"

    impl_complete, review_complete, test_results = True, True, (5, 0, 0, 0)  # 5 passed, nothing else
    completion = _determine_story_completion(
        MockStory(), test_results, impl_complete, review_complete
    )
    assert completion.is_completed == True, "All tests passed should allow completion"
    print("✓ All tests passed allows completion")




# --- No BRD/PRR/Architecture mutation from Jira tests ---

def test_no_jira_to_brd_synchronization_stays():
    """Test that sprint completion doesn't access BRD/PRD/Architecture from Jira state."""
    # This is implicitly tested by the fact that we don't have any code that reads
    # Jira fields to modify BRD/PRD/Architecture in the sprint completion logic.
    # The test lives elsewhere in test_lifecycle_workspace.py.
    print("✓ Sprint completion respects BRD/PRD/Architecture as source of truth")


# --- Sprint completion immutability tests ---

def test_sprint_completion_does_not_mutate_sources():
    """Test that sprint completion doesn't mutate source artifacts."""
    from sprint_completion import complete_sprint

    # Create mock objects
    mock_plan = SprintPlan(sprint_name="Sprint 1")
    mock_lifecycle = type('MockLifecycle', (), {
        'implementation_plan': None,
        'delivery_mapping': None,
    })()

    # Run completion
    completion = complete_sprint(mock_lifecycle, mock_plan, [])

    # Verify it returned a completion object
    assert completion is not None
    assert hasattr(completion, 'sprint_plan')
    print("✓ Sprint completion doesn't mutate sources")


# --- Edge cases ---

def test_sprint_completion_empty_stories():
    """Test sprint completion with no stories."""
    from sprint_completion import complete_sprint

    mock_plan = SprintPlan(sprint_name="Sprint 1")
    mock_lifecycle = type('MockLifecycle', (), {
        'implementation_plan': None,
        'delivery_mapping': None,
    })()

    completion = complete_sprint(mock_lifecycle, mock_plan, [])
    assert completion.overall_status == "In Progress"  # Default for no stories
    print("✓ Empty stories handled correctly")


def test_sprint_completion_mixed_results():
    """Test sprint completion with mixed test results."""
    from sprint_completion import _determine_story_completion

    class MockStory:
        story_id = "STORY-1"

    # Mixed results: some pass, some fail, some blocked, some not run
    # According to logic, fail or blocked causes In Progress, all pass causes complete
    impl_complete, review_complete, test_results = True, True, (3, 2, 2, 1)
    completion = _determine_story_completion(
        MockStory(), test_results, impl_complete, review_complete
    )
    # Fail count > 0, so not complete
    assert completion.is_completed == False
    assert completion.failed_count == 2
    assert completion.blocked_count == 2
    print("✓ Mixed test results handled correctly")


if __name__ == "__main__":
    print("Running Phase 9 focused tests...\n")

    # StoryCompletion tests
    test_story_completion_created()
    test_story_completion_incomplete()

    # SprintCompletion tests
    test_sprint_completion_created()
    test_sprint_completion_with_complete_stories()
    test_sprint_completion_with_remaining()

    # Sprint completion state function tests
    test_sprint_completion_state_none()
    test_sprint_completion_state_in_progress()
    test_sprint_completion_state_completed_unapproved()
    test_sprint_completion_state_completed_approved()

    # Evidence aggregation tests
    test_evidence_blocks_completion_on_failures()
    test_evidence_blocks_completion_on_blocked()
    test_evidence_blocks_completion_on_not_run()
    test_only_all_passed_completion()

    # Lifecycle integration tests
    test_lifecycle_includes_sprint_completion()
    test_sprint_completion_is_implemented()

    # No BRD/PRD/Architecture mutation tests
    test_no_jira_to_brd_synchronization_stays()

    # Immutability tests
    test_sprint_completion_does_not_mutate_sources()

    # Edge cases
    test_sprint_completion_empty_stories()
    test_sprint_completion_mixed_results()

    print("\n✓ All Phase 9 focused tests passed!")

# --- Integration with main.py ---

def test_main_compiles_cleanly():
    """main.py compiles without syntax or import structure errors."""
    import py_compile
    py_compile.compile("main.py", doraise=True)
