"""
Helper module to build delivery mappings from sprint completion data.

This module provides utilities to construct DeliveryMapping objects
from sprint completion remaining backlog, enabling next-sprint recommendations.
"""

from typing import List

from implementation_plan_jira import DeliveryMapping, DeliveryLink
from sprint_completion_models import SprintCompletion
from sprint_models import SprintIssue


def build_delivery_mapping_from_backlog(
    project_identifier: str = "",
    project_label: str = "",
    site_url: str = "",
    remaining_backlog: List[SprintIssue] = None,
    work_type_lookup: dict = None,
) -> DeliveryMapping:
    """
    Build a DeliveryMapping from a sprint's remaining backlog.
    This allows the existing recommend_sprint function to work with
    leftover sprint items.
    """
    remaining_backlog = remaining_backlog or []
    work_type_lookup = work_type_lookup or {}

    links = []
    for issue in remaining_backlog:
        plan_item_id = issue.story_id or issue.issue_key
        if not plan_item_id:
            continue
        work_type = work_type_lookup.get(plan_item_id, "Story")
        link = DeliveryLink(
            plan_item_id=plan_item_id,
            issue_key=issue.issue_key,
            issue_id="",
            issue_type_name="",
            summary=issue.summary,
            work_type=work_type,
            feature_ids=(),
            component_ids=(),
        )
        links.append(link)

    return DeliveryMapping(
        project_identifier=project_identifier,
        project_label=project_label,
        site_url=site_url,
        links=tuple(links),
        notes=(),
    )


def extract_work_type_lookup(implementation_plan) -> dict:
    """
    Build a lookup mapping plan_item_id -> work_type from an implementation plan.
    """
    lookup = {}
    if implementation_plan is None:
        return lookup
    for story in getattr(implementation_plan, 'stories', ()):
        lookup[story.story_id] = story.work_type if hasattr(story, 'work_type') else "Story"
        for task in getattr(story, 'tasks', ()):
            lookup[task.task_id] = getattr(task, 'work_type', 'Task')
    for epic in getattr(implementation_plan, 'epics', ()):
        lookup[epic.epic_id] = "Epic"
    return lookup