"""
Sprint completion tracking for Phase 9: Sprint Completion + Next Sprint.

This module defines data structures for tracking sprint completion based on
actual implementation, review, and test evidence, preserving sprint history,
and carrying unfinished work into the remaining backlog.
"""

from dataclasses import dataclass, field
from typing import List, Optional

from sprint_models import SprintPlan, SprintIssue
from test_case_models import TestCase, TestSuite, \
    TEST_EXECUTION_NOT_RUN, TEST_EXECUTION_PASS, TEST_EXECUTION_FAIL, TEST_EXECUTION_BLOCKED


@dataclass
class StoryCompletion:
    """Completion status for a single story within a sprint."""
    story_id: str
    is_completed: bool
    detail: str
    # Optional: aggregated test execution results for this story
    passed_count: int = 0
    failed_count: int = 0
    blocked_count: int = 0
    not_run_count: int = 0


@dataclass
class SprintCompletion:
    """Result of a sprint: completion status, history, and remaining backlog."""
    sprint_plan: SprintPlan
    story_completions: List[StoryCompletion] = field(default_factory=list)
    overall_status: str = "In Progress"  # "Completed", "In Progress", "Blocked"
    approved: bool = False
    # Unfinished work from this sprint carried forward to the next sprint's backlog
    remaining_backlog: List[SprintIssue] = field(default_factory=list)

    @property
    def is_completed(self) -> bool:
        """Convenience property for overall completion."""
        return self.overall_status == "Completed"

    @property
    def completed_stories(self) -> List[StoryCompletion]:
        """Stories that are completed in this sprint."""
        return [sc for sc in self.story_completions if sc.is_completed]

    @property
    def incomplete_stories(self) -> List[StoryCompletion]:
        """Stories that are not completed in this sprint."""
        return [sc for sc in self.story_completions if not sc.is_completed]