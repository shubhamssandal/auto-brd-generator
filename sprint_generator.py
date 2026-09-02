import json
import logging
from typing import Optional

from google import genai
from google.genai import types

from implementation_plan_jira import DeliveryMapping
from sprint_models import SprintPlan, SprintIssue

logger = logging.getLogger(__name__)


def _calculate_story_points(summary: str) -> int:
    """Calculate estimated story points based on issue complexity."""
    summary_lower = summary.lower()

    # High complexity keywords that should get 8 points
    high_complexity_keywords = ['complex', 'difficult', 'hard', 'challenging', 'integration', 'api', 'database', 'migration', 'schema', 'architecture', 'design', 'security', 'enterprise', 'critical', 'urgent']

    # Medium complexity keywords that should get 3 points
    medium_complexity_keywords = ['new', 'feature', 'implement', 'create', 'add', 'enhance', 'update', 'modify', 'improve', 'setup', 'configure', 'build', 'fix']

    point = 1  # Default (small/easiest)

    # Check high complexity first (more specific indicators)
    for keyword in high_complexity_keywords:
        if keyword in summary_lower:
            point = 8  # High complexity (8 points)
            break

    # If not high complexity, check medium complexity
    if point == 1:
        for keyword in medium_complexity_keywords:
            if keyword in summary_lower:
                point = 3  # Medium complexity (3 points)
                break

    return point


def _determine_readiness(summary: str, work_type: str) -> str:
    """Determine if an issue is ready for sprint planning."""
    summary_lower = summary.lower()

    # If it mentions testing, bug fixes, or documentation - likely ready
    if any(keyword in summary_lower for keyword in ['test', 'bug', 'fix', 'doc', 'documentation', 'minor', 'quick', 'simple']):
        return "Ready"

    # If it mentions integration with external systems - might need prep
    if 'integration' in summary_lower or 'api' in summary_lower:
        return "Needs Prep"

    # If it mentions database changes or complex logic - needs more work
    if any(keyword in summary_lower for keyword in ['database', 'sql', 'migration', 'complex', 'architecture', 'design', 'security', 'enterprise']):
        return "Planning Needed"

    # Default readiness based on work type
    if work_type.lower() in ['task', 'technical', 'subtask']:
        return "Ready"

    return "Evaluating"


def _get_issue_priority(summary: str, work_type: str) -> str:
    """Determine issue priority based on content and type."""
    summary_lower = summary.lower()

    # High priority for critical issues
    if any(keyword in summary_lower for keyword in ['critical', 'urgent', 'security', 'bug', 'fix', 'crash', 'error', 'broken']):
        return "Highest"

    # High priority for user authentication and payment systems
    if any(keyword in summary_lower for keyword in ['auth', 'login', 'password', 'payment', 'money', 'billing']):
        return "High"

    # Medium priority for features and enhancements
    if any(keyword in summary_lower for keyword in ['feature', 'new', 'add', 'implement', 'create', 'enhance', 'update']):
        return "Medium"

    # Lower priority for documentation, maintenance, minor fixes
    if any(keyword in summary_lower for keyword in ['doc', 'documentation', 'update docs', 'minor', 'cleanup', 'refactor']):
        return "Low"

    # Default based on work type
    if work_type.lower() == 'epic':
        return "High"
    elif work_type.lower() == 'story':
        return "Medium"
    else:  # task or subtask
        return "Medium"


def _fallback_sprint() -> SprintPlan:
    """Fallback sprint when AI client cannot be initialized."""
    return SprintPlan(sprint_goal="Initial Sprint (Fallback)")


def recommend_sprint(
    mapping: DeliveryMapping,
    client: Optional[genai.Client] = None
) -> SprintPlan:
    """
    Generate a sprint recommendation from the approved Jira backlog.
    Uses priority, dependency order, story readiness and effort to select work for a sprint.
    """
    if not mapping or not mapping.links:
        return SprintPlan(sprint_goal="No backlog to plan")

    # For testing, if client is None we try to create one only if we're not in a test environment
    # This allows tests to inject a mock client
    if client is None:
        try:
            client = genai.Client()
        except Exception:
            # If client creation fails (e.g., in test environment without API key), use fallback
            client = None

    # Build backlog with analysis data
    backlog = []
    for link in mapping.links:
        # Calculate story points based on complexity indicators
        story_points = _calculate_story_points(link.summary)

        # Determine readiness for sprint planning
        readiness = _determine_readiness(link.summary, link.work_type)

        # Basic priority - could be enhanced with Jira's priority field if available
        priority = getattr(link, 'priority', 'Medium')

        backlog.append({
            "issue_key": link.issue_key,
            "summary": link.summary,
            "work_type": link.work_type,
            "story_points": story_points,
            "readiness": readiness,
            "priority": priority
        })

    # Sort backlog by priority and readiness for sprint planning
    sorted_backlog = sorted(backlog, key=lambda x: (
        # Higher priority first (assuming priority strings like "Highest", "High", etc.)
        ["Highest", "High", "Medium", "Low", "Lowest"].index(x["priority"].split()[0]) if x["priority"].split()[0] in ["Highest", "High", "Medium", "Low", "Lowest"] else 2,
        # Ready items first
        ["Ready", "Needs Prep", "Planning Needed", "Evaluating"].index(x["readiness"])
    ))

    # Try to call the Gemini client if available
    if client is not None:
        try:
            prompt = (
                "You are an agile Sprint Planner.\n"
                "Review the following backlog of Jira issues and select a realistic scope for a 1-month sprint.\n"
                "Consider dependencies, readiness, and technical prerequisites.\n"
                "Return a JSON object with:\n"
                "sprint_name: string (e.g. 'Sprint 1')\n"
                "sprint_goal: string\n"
                "duration_weeks: 4\n"
                "issues: array of objects with keys: issue_key, summary, rationale, is_selected (boolean)\n"
                "Backlog:\n" + json.dumps(sorted_backlog, indent=2)
            )

            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                ),
            )
            data = json.loads(response.text)
            sprint = SprintPlan(
                sprint_name=data.get("sprint_name", "Sprint 1"),
                sprint_goal=data.get("sprint_goal", ""),
                duration_weeks=data.get("duration_weeks", 4)
            )
            for i in data.get("issues", []):
                sprint.issues.append(SprintIssue(
                    issue_key=i.get("issue_key", ""),
                    summary=i.get("summary", ""),
                    rationale=i.get("rationale", ""),
                    is_selected=bool(i.get("is_selected", False))
                ))
            return sprint

        except Exception as e:
            logger.warning("Sprint planning generation failed: %s", e)

    # Deterministic fallback with priority-based selection when AI fails
    sprint = SprintPlan(sprint_goal="Initial Sprint (Fallback)")

    # Sort by priority and readiness for fallback selection
    sorted_fallback = sorted(sorted_backlog, key=lambda x: (
        ["Highest", "High", "Medium", "Low", "Lowest"].index(x["priority"].split()[0]) if x["priority"].split()[0] in ["Highest", "High", "Medium", "Low", "Lowest"] else 2,
        ["Ready", "Needs Prep", "Planning Needed", "Evaluating"].index(x["readiness"])
    ))

    for i, link in enumerate(sorted_fallback):
        # Pick first 5 issues as a fallback, prioritizing ready items
        if i < 5:
            sprint.issues.append(SprintIssue(
                issue_key=link["issue_key"],
                summary=link["summary"],
                rationale="Fallback selection - high priority, ready work",
                is_selected=(i < 3)  # Select first 3 as selected
            ))
    return sprint


def generate_sprint_plan_from_implementation_plan(
    implementation_plan,
    delivery_mapping: Optional[DeliveryMapping] = None,
    sprint_name: str = "Sprint 1",
    sprint_goal: str = "",
    duration_weeks: int = 4,
) -> SprintPlan:
    """
    Build a SprintPlan directly from an approved ImplementationPlan.

    This is the lifecycle Sprint Planning stage: the source is the engineering structure
    the reviewer approved, not a Jira backlog. Every story is mapped to a SprintIssue
    with its ``story_id`` set so ``execute_sprint`` can resolve it against the
    implementation plan. ``issue_key`` is preserved from the existing delivery mapping
    where one exists, so a story that already became a Jira issue stays linked to it.

    The returned plan always starts with ``approved = False``. Approval is a separate
    human action handled by the lifecycle stage.
    """
    plan = SprintPlan(
        sprint_name=sprint_name,
        sprint_goal=sprint_goal,
        duration_weeks=duration_weeks,
        approved=False,
    )

    stories = getattr(implementation_plan, "stories", ()) or ()
    if not stories:
        return plan

    ordered_ids = tuple(getattr(implementation_plan, "ordered_story_ids", ()) or ())
    by_id = {story.story_id: story for story in stories}

    for story_id in ordered_ids:
        story = by_id.get(story_id)
        if story is None:
            continue
        issue_key = ""
        if isinstance(delivery_mapping, DeliveryMapping):
            link = delivery_mapping.link_for(story_id)
            if link is not None:
                issue_key = link.issue_key
        plan.issues.append(
            SprintIssue(
                issue_key=issue_key,
                summary=getattr(story, "title", "") or "",
                rationale=(
                    f"Story {story.story_id} from approved implementation plan"
                ),
                is_selected=True,
                story_id=story_id,
            )
        )

    return plan