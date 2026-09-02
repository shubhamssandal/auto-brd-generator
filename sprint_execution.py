"""
Sprint Execution Orchestration: Executes all stories in an approved sprint.

Reuses the existing AI Coding Agent for story-level implementation.
"""
import os
import logging
from dataclasses import dataclass, field, replace
from typing import Any, List, Optional

from coding_agent import CodeChange, TestSuite, run_ai_coding_agent
from implementation_plan_jira import DeliveryMapping, DeliveryLink


logger = logging.getLogger(__name__)


@dataclass
class SprintExecutionResult:
    """Result of sprint-level coding execution."""
    sprint_id: str = ""
    sprint_name: str = ""
    total_stories: int = 0
    completed_stories: int = 0
    failed_stories: int = 0
    blocked_stories: int = 0
    not_started_stories: int = 0
    total_fix_attempts: int = 0
    files_changed: List[CodeChange] = field(default_factory=list)
    test_suites: List[TestSuite] = field(default_factory=list)
    overall_status: str = "Not Started"  # Not Started/In Progress/Completed/Blocked
    blockers: List[str] = field(default_factory=list)
    approved: bool = False
    human_approval_required: bool = False
    remaining_backlog: List[Any] = field(default_factory=list)


class SprintExecutionRunner:
    """Runner for sprint-level coding execution."""

    def __init__(self, client: Optional[Any] = None, workspace_root: Optional[str] = None):
        self.client = client
        self.workspace_root = workspace_root or os.getcwd()

    def run_sprint(self, sprint_plan: Any, lifecycle: Any, client: Optional[Any] = None) -> SprintExecutionResult:
        """Run a sprint execution."""
        return execute_sprint(
            sprint_plan=sprint_plan,
            lifecycle=lifecycle,
            client=client or self.client,
            workspace_root=self.workspace_root,
        )

    def approve_sprint(self, result: SprintExecutionResult) -> SprintExecutionResult:
        """Approve a sprint execution (human approval)."""
        if result.human_approval_required:
            result.approved = True
            result.human_approval_required = False
            logger.info(f"Sprint '{result.sprint_name}' approved by human")
        return result


def execute_sprint(
    sprint_plan: Any,
    lifecycle: Any,
    client: Optional[Any] = None,
    workspace_root: Optional[str] = None,
) -> SprintExecutionResult:
    """Execute all stories in an approved sprint sequentially.

    This is the main entry point for sprint-level coding execution.
    It:
    1. Checks that the sprint has been approved before execution.
    2. Iterates through all sprint stories in order.
    3. Runs the existing AI Coding Agent for each story.
    4. Aggregates evidence from all stories.
    5. Requires human approval before marking as approved.

    The execution follows the existing coding_agent pattern where:
    - Each story maintains independent state.
    - Failed/blocked stories are tracked but don't corrupt other stories.
    - Fix attempts are per-story.
    - Evidence is preserved per-story.
    """
    # Enforce sprint approval before any execution begins
    if not getattr(sprint_plan, "approved", False):
        # Sprint not approved – block execution and require human approval
        result = SprintExecutionResult(
            sprint_id=getattr(sprint_plan, "sprint_id", ""),
            sprint_name=getattr(sprint_plan, "sprint_name", ""),
            overall_status="Not Started",
            human_approval_required=True,
        )
        result.blockers.append("Sprint execution blocked: sprint not approved by human.")
        logger.info("Sprint execution blocked because sprint is not approved.")
        return result

    sprint_id = getattr(sprint_plan, 'sprint_id', '')
    sprint_name = getattr(sprint_plan, 'sprint_name', '')
    logger.info(f"Starting sprint execution: {sprint_name} (ID: {sprint_id})")

    workspace = os.path.abspath(workspace_root or os.getcwd())
    logger.info(f"Using workspace: {workspace}")

    # Initialize sprint result
    result = SprintExecutionResult(
        sprint_id=sprint_id,
        sprint_name=sprint_name,
        overall_status="In Progress",
        human_approval_required=True,
    )

    # Track which stories have been started to identify not-started stories
    stories_processed = 0
    stories_in_sprint = 0
    # Per-story outcomes for sprint completion to use
    story_outcomes = {}

    try:
        # Get all stories from the sprint
        for issue in sprint_plan.issues:
            if hasattr(issue, 'story_id') and issue.story_id:
                story_id = issue.story_id
                stories_in_sprint += 1

                # Find the story in the implementation plan
                story = None
                if lifecycle and hasattr(lifecycle, 'implementation_plan'):
                    story = lifecycle.implementation_plan.story(story_id)

                if not story:
                    logger.warning(f"Could not find story {story_id} for sprint issue {issue.issue_key}")
                    # Track as not started
                    continue

                logger.info(f"Executing story {story_id}: {story.title}")
                stories_processed += 1

                # Execute this story using the existing AI Coding Agent
                story_result = run_ai_coding_agent(
                    story=story,
                    client=client,
                    workspace_root=workspace,
                )

                # Update sprint result with story-level evidence
                result.total_stories += 1
                result.total_fix_attempts += story_result.fix_attempts

                # Classify story outcome based on coding agent result
                if story_result.blocked:
                    # Blocked (not ready or failed after max retries)
                    result.blockers.append(f"Story {story_id}: {story_result.blocked_reason}")
                    result.blocked_stories += 1
                    logger.info(f"Story {story_id} blocked: {story_result.blocked_reason}")
                    story_outcomes[story_id] = {'completed': False, 'failed': False, 'blocked': True, 'fix_attempts': story_result.fix_attempts}
                else:
                    # Implementation succeeded (may have had retries)
                    result.completed_stories += 1
                    logger.info(f"Story {story_id} completed successfully after {story_result.fix_attempts} fix attempt(s)")
                    story_outcomes[story_id] = {'completed': True, 'failed': False, 'blocked': False, 'fix_attempts': story_result.fix_attempts}

                # Aggregate files changed and test results
                result.files_changed.extend(story_result.files_changed)
                result.test_suites.extend(story_result.test_suites)

    except Exception as e:
        logger.error(f"Error during sprint execution: {e}")
        result.overall_status = "Blocked"
        result.blockers.append(f"Sprint execution error: {str(e)}")

    # Store per-story outcomes on lifecycle for sprint completion
    if lifecycle is not None:
        lifecycle.story_execution_outcomes = story_outcomes

    # Determine overall status
    if result.total_stories == 0:
        result.overall_status = "Not Started"
    elif result.blocked_stories > 0 or result.failed_stories > 0:
        # If any stories failed or were blocked, sprint is blocked
        result.overall_status = "Blocked"
    elif result.completed_stories == result.total_stories:
        result.overall_status = "Completed"
    else:
        result.overall_status = "In Progress"

    # Set not_started stories
    result.not_started_stories = stories_in_sprint - stories_processed

    # Store sprint execution in delivery mapping for traceability (preserve real issue keys if available)
    _record_sprint_execution(lifecycle, result, sprint_plan)

    logger.info(
        f"Sprint execution completed: {result.completed_stories}/{result.total_stories} stories completed, "
        f"status={result.overall_status}, human_approval_required={result.human_approval_required}"
    )

    # Store the result in lifecycle for sprint completion to use
    if lifecycle is not None:
        lifecycle.last_sprint_execution_result = result

    return result


def _record_sprint_execution(
    lifecycle: Any, result: SprintExecutionResult, sprint_plan: Any
) -> None:
    """Record sprint execution in delivery mapping, preserving any existing issue keys."""
    try:
        # Retrieve any existing delivery mapping from the lifecycle, if present
        existing_mapping = getattr(lifecycle, 'delivery_mapping', None)
        base_links = []
        if isinstance(existing_mapping, DeliveryMapping):
            # Preserve existing links (e.g., from implementation plan to Jira)
            base_links = list(existing_mapping.links)

        # Create a new delivery mapping for the sprint execution (or extend existing)
        delivery_mapping = DeliveryMapping(
            project_identifier="sprint_execution",
            project_label=f"Sprint {result.sprint_id or 'unknown'} Execution",
            links=tuple(),
            notes=(f"Sprint execution: {result.completed_stories}/{result.total_stories} stories completed",)
        )

        # Add sprint execution links for each story, reusing real issue keys when available
        links = []
        for issue in sprint_plan.issues:
            story_id = getattr(issue, 'story_id', '')
            if not story_id:
                continue
            # Check if there is an existing delivery link for this story_id
            existing_link = None
            if isinstance(existing_mapping, DeliveryMapping):
                existing_link = existing_mapping.link_for(story_id)
            if existing_link and existing_link.issue_key:
                # Preserve the real Jira issue key
                links.append(
                    DeliveryLink(
                        plan_item_id=story_id,
                        issue_key=existing_link.issue_key,
                        issue_type_name=existing_link.issue_type_name,
                        summary=existing_link.summary,
                        work_type=existing_link.work_type,
                        feature_ids=existing_link.feature_ids,
                        component_ids=existing_link.component_ids,
                    )
                )
            else:
                # No real issue; create an unlinked placeholder for traceability
                links.append(
                    DeliveryLink(
                        plan_item_id=story_id,
                        issue_key="",
                        issue_type_name="sprint_execution",
                        summary=f"Sprint {result.sprint_id} execution for story {story_id}",
                        work_type="sprint_execution",
                        feature_ids=(),
                        component_ids=(),
                    )
                )
        # Combine any base links with the new sprint execution links, avoiding duplicates by plan_item_id
        combined_links = {link.plan_item_id: link for link in base_links}
        for link in links:
            combined_links[link.plan_item_id] = link
        delivery_mapping = replace(delivery_mapping, links=tuple(combined_links.values()))

        # Store the delivery mapping for traceability on the lifecycle
        if not hasattr(lifecycle, 'sprint_executions'):
            lifecycle.sprint_executions = []
        lifecycle.sprint_executions.append(delivery_mapping)

        logger.info(f"Recorded sprint execution {result.sprint_id} in delivery mapping")

    except Exception as e:
        logger.warning(f"Could not record sprint execution in delivery mapping: {e}")


def run_sprint_and_collect_results(
    sprint_plan: Any,
    lifecycle: Any,
    client: Optional[Any] = None,
    workspace_root: Optional[str] = None,
) -> SprintExecutionResult:
    """Run a sprint and return results with Jira integration."""
    runner = SprintExecutionRunner(client=client, workspace_root=workspace_root)
    result = runner.run_sprint(sprint_plan, lifecycle)

    # Record results in Jira if configured
    _sync_sprint_results_to_jira(lifecycle, result)

    return result


def _sync_sprint_results_to_jira(lifecycle: Any, result: SprintExecutionResult) -> None:
    """Sync sprint results to Jira for visibility."""
    try:
        # Only sync if Jira is configured and results are approved
        if not result.approved:
            logger.info("Sprint not approved; skipping Jira sync")
            return

        # This would integrate with existing Jira service if available
        logger.info(
            f"Sprint execution results ready for Jira sync: "
            f"{result.completed_stories} completed, {result.failed_stories} failed, "
            f"{result.blocked_stories} blocked"
        )

    except Exception as e:
        logger.warning(f"Could not sync sprint results to Jira: {e}")


def approve_and_complete_sprint(
    result: SprintExecutionResult,
    sprint_plan: Any,
    lifecycle: Any,
) -> SprintExecutionResult:
    """Approve a sprint and mark it as completed."""
    # Human approval
    result = SprintExecutionRunner().approve_sprint(result)

    # Update status
    result.overall_status = "Completed" if result.completed_stories == result.total_stories else "In Progress"

    # Record in lifecycle
    if not hasattr(lifecycle, 'sprint_executions'):
        lifecycle.sprint_executions = []

    # Add to lifecycle evidence
    if hasattr(lifecycle, 'add_evidence'):
        lifecycle.add_evidence(
            evidence_type="sprint_execution",
            content=result,
            stage="Sprint Execution",
        )

    logger.info(f"Sprint '{result.sprint_name}' approved and completed")
    return result