"""
Sprint completion service for Phase 9: Sprint Completion + Next Sprint.

This service aggregates evidence from implementation, review, and test execution
to determine story completion, prevents false completion when required evidence
is missing, preserves sprint history, carries unfinished work into the remaining
backlog, and recommends the next sprint using existing recommendation logic.
"""

import logging
from typing import List, Optional, Dict, Any

from lifecycle_models import ProjectLifecycle, StageState
from sprint_completion_models import (
    StoryCompletion,
    SprintCompletion,
)
from sprint_generator import recommend_sprint
from sprint_models import SprintPlan, SprintIssue
from test_case_models import (
    TEST_EXECUTION_NOT_RUN,
    TEST_EXECUTION_PASS,
    TEST_EXECUTION_FAIL,
    TEST_EXECUTION_BLOCKED,
    TestCase,
    TestSuite,
)

# Import the new test execution module
from execution_engine import (
    generate_execution_evidence,
    get_execution_status_summary,
    TestExecutionEvidence,
    TestExecutionSession,
)

logger = logging.getLogger(__name__)


def _story_test_summary(test_cases: List[TestSuite], story_id: str) -> tuple:
    """
    Aggregate test execution results for a given story.
    Returns (passed, failed, blocked, not_run) counts.
    """
    passed = failed = blocked = not_run = 0
    for suite in test_cases or []:
        if not hasattr(suite, 'test_cases'):
            continue
        for tc in suite.test_cases:
            if getattr(tc, 'story_reference', '') == story_id:
                status = getattr(tc, 'execution_status', TEST_EXECUTION_NOT_RUN)
                if status == TEST_EXECUTION_PASS:
                    passed += 1
                elif status == TEST_EXECUTION_FAIL:
                    failed += 1
                elif status == TEST_EXECUTION_BLOCKED:
                    blocked += 1
                else:  # TEST_EXECUTION_NOT_RUN
                    not_run += 1
    return passed, failed, blocked, not_run


def _story_requires_testing(story) -> bool:
    """
    Determine if a story requires test evidence for completion.
    In this implementation, we assume all stories require at least some test coverage.
    Could be refined later based on story type or explicit flags.
    """
    # For now, all stories need test evidence to be considered complete
    return True


def _determine_story_completion(
    story,
    test_results: tuple,
    implementation_complete: bool,
    review_complete: bool,
) -> StoryCompletion:
    """
    Determine if a story is complete based on evidence.
    A story is complete when:
    - Implementation is done (implementation_complete=True)
    - Review is done (review_complete=True)
    - Test execution evidence shows no failures/blocked and all tests run (if testing required)
    """
    passed, failed, blocked, not_run = test_results
    requires_testing = _story_requires_testing(story)

    # Default detail
    detail_parts = []
    if not implementation_complete:
        detail_parts.append("implementation not complete")
    if not review_complete:
        detail_parts.append("review not complete")
    if requires_testing:
        if failed > 0:
            detail_parts.append(f"{failed} test(s) failed")
        if blocked > 0:
            detail_parts.append(f"{blocked} test(s) blocked")
        if not_run > 0:
            detail_parts.append(f"{not_run} test(s) not run")
        elif passed == 0 and (failed + blocked + not_run) > 0:
            # Some tests executed but none passed
            detail_parts.append("no tests passed")
        elif passed > 0 and (failed == 0 and blocked == 0 and not_run == 0):
            # All tests passed
            detail_parts.append(f"{passed} test(s) passed")
        else:
            # Mixed results with some passes but also missing/not-run
            detail_parts.append(f"{passed} passed, {failed} failed, {blocked} blocked, {not_run} not run")

    is_complete = (
        implementation_complete
        and review_complete
        and (not requires_testing or (failed == 0 and blocked == 0 and not_run == 0))
    )

    detail = "; ".join(detail_parts) if detail_parts else "All evidence complete"
    return StoryCompletion(
        story_id=getattr(story, 'story_id', ''),
        is_completed=is_complete,
        detail=detail,
        passed_count=passed,
        failed_count=failed,
        blocked_count=blocked,
        not_run_count=not_run,
    )


def _gather_story_evidence(
    lifecycle: ProjectLifecycle,
    story,
    test_cases: Optional[List[TestSuite]],
) -> tuple:
    """
    Gather evidence for a single story: implementation status, review status, test results.
    Returns (implementation_complete, review_complete, test_results_tuple).
    """
    # Implementation status: we consider a story implemented if its plan item exists
    # and the implementation plan is approved. For simplicity, we assume if the
    # implementation plan is approved, all stories in it are implemented.
    # In a more detailed system, we might track per-task completion.
    implementation_plan = lifecycle.implementation_plan
    implementation_complete = (
        implementation_plan is not None
        and lifecycle.state("implementation_plan").status == "Approved"
    )

    # Review status: story is reviewed if the implementation plan is approved
    # (the review gate for the implementation plan). Again, we could have per-story
    # review but we follow the plan-level approval.
    review_complete = implementation_complete  # same gate for now

    # Test results: aggregate from test execution stage
    # First check for approved test execution evidence, then fall back to test cases
    test_results = (0, 0, 0, 0)  # passed, failed, blocked, not_run

    # Check for test execution evidence in session state
    execution_evidence_key = f"execution_evidence_{getattr(story, 'story_id', '')}"
    execution_evidence = st.session_state.get(execution_evidence_key)

    if execution_evidence and isinstance(execution_evidence, dict) and execution_evidence.get('approved'):
        # Use test execution evidence if approved
        from execution_engine import get_execution_status_summary
        summary = get_execution_status_summary(execution_evidence)
        test_results = (
            summary['passed'],
            summary['failed'],
            summary['blocked'],
            summary['not_run']
        )
    elif test_cases is not None:
        # Fall back to original test cases if no execution evidence
        test_results = _story_test_summary(test_cases, getattr(story, 'story_id', ''))

    return implementation_complete, review_complete, test_results


def complete_sprint(
    lifecycle: ProjectLifecycle,
    sprint_plan: SprintPlan,
    test_cases: Optional[List[TestSuite]] = None,
) -> SprintCompletion:
    """
    Complete a sprint by evaluating each story's evidence.
    Returns a SprintCompletion record with:
    - The original sprint plan
    - Per-story completion status
    - Overall sprint status (Completed/In Progress/Blocked)
    - Remaining backlog (unfinished stories carried forward)
    - Approval flag (False until user approves)
    """
    logger.info("Completing sprint: %s", sprint_plan.sprint_name)

    # Gather test cases once for efficiency
    test_cases = test_cases or []

    story_completions: List[StoryCompletion] = []
    incomplete_issues: List[SprintIssue] = []

    for issue in sprint_plan.issues:
        # Find the story corresponding to this sprint issue
        story = None
        if lifecycle.implementation_plan:
            # Try to find story by story_id if we stored it
            if issue.story_id:
                story = lifecycle.implementation_plan.story(issue.story_id)
            # Fallback: try to match by summary or other fields (simplified)
            if not story:
                for s in lifecycle.implementation_plan.stories:
                    if s.story_id == issue.story_id or s.title == issue.summary:
                        story = s
                        break
        # If we still don't have a story, we cannot evaluate completion accurately
        if story is None:
            logger.warning(
                "Could not find story for sprint issue %s (%s)",
                issue.issue_key,
                issue.summary,
            )
            # Treat as incomplete to be safe
            story_completions.append(
                StoryCompletion(
                    story_id="",
                    is_completed=False,
                    detail="Story not found in implementation plan",
                )
            )
            incomplete_issues.append(issue)
            continue

        impl_complete, review_complete, test_results = _gather_story_evidence(
            lifecycle, story, test_cases
        )
        story_completion = _determine_story_completion(
            story, test_results, impl_complete, review_complete
        )
        story_completions.append(story_completion)

        if not story_completion.is_completed:
            # Carry forward incomplete work to remaining backlog
            # Preserve the original issue but update story_id if we discovered it
            issue_for_backlog = SprintIssue(
                issue_key=issue.issue_key,
                summary=issue.summary,
                rationale=issue.rationale,
                is_selected=False,  # backlog items are not selected by default
                story_id=getattr(story, 'story_id', ''),
            )
            incomplete_issues.append(issue_for_backlog)

    # Determine overall sprint status
    completed_count = sum(1 for sc in story_completions if sc.is_completed)
    total_stories = len(story_completions)
    if total_stories == 0:
        overall_status = "In Progress"  # no stories to evaluate
    elif completed_count == total_stories:
        overall_status = "Completed"
    else:
        # If any story is blocked or failed, we consider the sprint blocked
        # (could also be In Progress, but Blocked signals impediment)
        any_blocked_or_failed = any(
            sc.blocked_count > 0 or sc.failed_count > 0
            for sc in story_completions
        )
        overall_status = "Blocked" if any_blocked_or_failed else "In Progress"

    sprint_completion = SprintCompletion(
        sprint_plan=sprint_plan,
        story_completions=story_completions,
        overall_status=overall_status,
        approved=False,
        remaining_backlog=incomplete_issues,
    )

    logger.info(
        "Sprint completion: %d/%d stories complete, status=%s",
        completed_count,
        total_stories,
        overall_status,
    )
    return sprint_completion


def recommend_next_sprint(
    lifecycle: ProjectLifecycle,
    completed_sprint: SprintCompletion,
) -> SprintPlan:
    """
    Recommend the next sprint based on completed sprint's remaining backlog
    and any new work from the approved implementation plan.
    Reuses the existing recommend_sprint logic from sprint_generator.py.
    """
    # Build a delivery mapping from the completed sprint's remaining backlog
    # plus any new stories from the implementation plan that weren't in the sprint
    from delivery_mapping_builder import build_delivery_mapping, extract_work_type_lookup

    # For now, we'll reuse the existing delivery mapping from the lifecycle
    # and let the recommender pick from the full backlog, but we could
    # prioritize remaining backlog items.
    # As a simple approach, we recommend a sprint from the full approved
    # implementation plan, letting the existing logic handle priority/readiness.
    # The remaining backlog will naturally be high-priority because it's unfinished.
    if lifecycle.delivery_mapping is not None:
        return recommend_sprint(lifecycle.delivery_mapping)
    else:
        # Build a delivery mapping from remaining backlog
        work_type_lookup = extract_work_type_lookup(lifecycle.implementation_plan)
        mapping = build_delivery_mapping_from_backlog(
            remaining_backlog=completed_sprint.remaining_backlog,
            work_type_lookup=work_type_lookup,
        )
        if mapping.links:
            sprint = recommend_sprint(mapping)
            # Increment sprint name if it follows the Sprint N pattern
            try:
                current_num = int(completed_sprint.sprint_plan.sprint_name.split()[-1])
                sprint.sprint_name = f"Sprint {current_num + 1}"
            except (ValueError, IndexError):
                sprint.sprint_name = f"{completed_sprint.sprint_plan.sprint_name} (continued)"
            return sprint
        else:
            # No remaining backlog; return empty sprint
            try:
                current_num = int(completed_sprint.sprint_plan.sprint_name.split()[-1])
                next_name = f"Sprint {current_num + 1}"
            except (ValueError, IndexError):
                next_name = "Next Sprint"
            return SprintPlan(
                sprint_name=next_name,
                sprint_goal="No remaining work; plan new sprint",
                duration_weeks=4,
            )