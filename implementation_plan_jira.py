"""
An approved implementation plan onto the selected Jira project's own issue hierarchy.

This is the one place the tracker-agnostic plan meets a real tracker.
``implementation_plan_models.ImplementationPlan`` says nothing about issue types
because it is written before a Jira project is chosen; ``jira_models.PlannedIssue`` is
expressed entirely in one project's vocabulary because it is what a creation call
sends. Neither model is changed to accommodate the other -- this module translates,
which is why the plan stays usable against a tracker that is not Jira.

Four properties are deliberate.

**The project's hierarchy decides the shape, not this app.** Rungs come from
``jira_processor.plannable_levels``, which orders whatever Jira reported for the
selected project and names no level. A plan epic goes on the top rung, a story on the
next, a technical task on the one below that. Where a project offers fewer rungs than
the plan needs, the items that will not fit are reported and left unselected rather
than flattened onto the wrong parent: a task created as a sibling of its own story
would misstate the plan, and a hierarchy this tracker cannot express is something the
reviewer has to know about.

**A plan item's own identifier is the issue's plan key.** ``PlannedIssue.plan_key`` is
``E1``/``S2``/``T3`` exactly as the implementation plan minted it, so the mapping from
plan item to Jira issue key falls out of the creation record that already exists and
is stable across reruns, edits and retries. No second identifier scheme is introduced.

**Creation is idempotent through the mapping, not through Jira.** Jira's create
endpoint has no idempotency key, so a repeat POST makes a second issue. The defence is
therefore local: ``DeliveryMapping`` records what was created, ``known_issue_keys``
hands those keys to the creation loop as already-satisfied, and a retry after a partial
failure creates only what has no key yet while still parenting new children onto issues
an earlier run created.

**Nothing here writes to Jira, and nothing here reads the BRD, PRD or architecture in
order to change them.** Traceability travels one way: the description carries the PRD
feature ids and architecture component ids the plan already recorded, so a reviewer
reading the issue in Jira can get back to the artifact it came from.
"""

from dataclasses import dataclass, replace
from typing import Optional

from implementation_plan_models import (
    EPIC,
    STORY,
    SUBTASK,
    TASK,
    WORK_TYPE_LABEL,
)
from jira_models import JiraWorkPlan, PlannedIssue
from jira_processor import plannable_levels, summary_line

# The plan's own depth for each work type. Absolute, not compressed: a story is created
# on the story rung whether or not the plan has any epics, so a plan without epics
# leaves the top rung unused instead of having its stories created as epics.
PLAN_DEPTH = {EPIC: 0, STORY: 1, TASK: 2, SUBTASK: 2}

# The Jira issue-type name conventionally used for each plan work type. Tried by exact
# name at the assigned rung and never fuzzily; a project that has no type by that name
# gets the rung's own default and a note saying so. The list is a courtesy to the
# common case, not a requirement -- a project calling its rungs Initiative/Feature/Work
# maps just as well, it simply uses each rung's default type.
PREFERRED_TYPE_NAMES = {
    EPIC: ("Epic",),
    STORY: ("Story",),
    TASK: ("Task",),
    SUBTASK: ("Subtask", "Sub-task"),
}

# One creation run is bounded. The plan's own caps allow far more items than anyone
# would want to push into a tracker in a single confirmed write, and an unbounded run
# is also an unbounded number of POSTs. Items past the cap are kept in the proposal and
# left unselected, so they are visible and can be created by a later run.
MAX_DELIVERY_ISSUES = 150


# --- The mapping from plan item to Jira issue -----------------------------


def _reference_line(label: str, values) -> str:
    identifiers = [str(value).strip() for value in values or () if str(value).strip()]
    return "{}: {}".format(label, ", ".join(identifiers)) if identifiers else ""


def _lines(*parts) -> str:
    return "\n".join(part for part in parts if part).strip()


def _epic_description(epic, plan) -> str:
    """What an epic issue says in Jira, including where it came from."""
    return _lines(
        str(epic.goal or "").strip(),
        "",
        _reference_line("Implementation plan item", (epic.epic_id,)),
        _reference_line("PRD features", epic.feature_ids),
        _reference_line("Architecture components", epic.component_ids),
        "Priority (plan): {}".format(epic.priority) if epic.priority else "",
        _provenance(plan),
    )


def _story_description(story, plan) -> str:
    return _lines(
        str(story.user_story or "").strip(),
        "",
        _reference_line("Implementation plan item", (story.story_id,)),
        _reference_line("PRD features", story.feature_ids),
        _reference_line("Architecture components", story.component_ids),
        _reference_line("Depends on (plan items)", story.depends_on),
        "Priority (plan): {}".format(story.priority) if story.priority else "",
        "Estimate (plan): {}".format(story.estimate) if story.estimate else "",
        _provenance(plan),
    )


def _task_description(task, story, plan) -> str:
    return _lines(
        str(task.detail or "").strip(),
        "",
        _reference_line("Implementation plan item", (task.task_id,)),
        _reference_line("Implements story", (story.story_id,)),
        _reference_line("Architecture components", task.component_ids),
        _reference_line("Depends on (plan items)", task.depends_on),
        "Estimate (plan): {}".format(task.estimate) if task.estimate else "",
        _provenance(plan),
    )


def _provenance(plan) -> str:
    """
    The artifacts this issue descends from, named so the trail is readable in Jira.

    One line, and only what the plan actually recorded. The plan is the source of
    truth for delivery structure; the PRD and architecture remain the source of truth
    for what is being built, and nothing here proposes changing them.
    """
    parts = []
    if getattr(plan, "source_prd_title", ""):
        parts.append("PRD '{}'".format(plan.source_prd_title))
    if getattr(plan, "source_architecture_title", ""):
        parts.append("architecture '{}'".format(plan.source_architecture_title))
    if not parts:
        return "Generated from a reviewed implementation plan."
    return "Generated from a reviewed implementation plan derived from {}.".format(
        " and ".join(parts)
    )


def _issue_type_for(level, work_type: str):
    """
    The issue type to use at ``level`` for a plan work type, and whether it was named.

    Returns ``(issue_type, matched_by_name)``. An exact name match keeps a normal
    project reading naturally -- an epic becomes an ``Epic`` -- while the fall back to
    the rung's default is what makes an unconventionally named project work at all.

    A plan task landing on a subtask rung looks for the subtask names first. At that
    rung a subtask type *is* the right answer, so treating it as a fallback would report
    a shortcoming that does not exist.
    """
    wanted = list(PREFERRED_TYPE_NAMES.get(work_type, ()))
    if getattr(level, "subtask", False):
        for name in PREFERRED_TYPE_NAMES[SUBTASK]:
            if name not in wanted:
                wanted.insert(0, name)
    for name in wanted:
        found = level.type_named(name)
        if found is not None:
            return found, True
    return level.default_type, False


def _planned(plan_key, summary, level, issue_type, description, parent="", criteria=()):
    return PlannedIssue(
        plan_key=str(plan_key),
        summary=summary_line(summary),
        issue_type_id=issue_type.id,
        issue_type_name=issue_type.name or issue_type.id,
        # Jira's own number for the rung, so ``validate_work_plan`` can check the type
        # against the level the issue occupies rather than against a name.
        hierarchy_level=issue_type.hierarchy_level,
        description=description,
        acceptance_criteria=tuple(criteria),
        parent_plan_key=str(parent),
        selected=True,
    )


def map_plan_to_work_plan(plan, metadata, project=None) -> JiraWorkPlan:
    """
    An approved implementation plan expressed as issues in one Jira project.

    Creates nothing and sends nothing. Every epic, story and technical task becomes a
    ``PlannedIssue`` keyed by its own plan identifier, parented by the plan's own
    Epic → Story → Task relationships, and typed with an issue type Jira reported for
    this project. An item that the project's hierarchy cannot hold is included but left
    unselected, and the reason is recorded in ``notes`` -- the reviewer sees the whole
    plan and what will not be created from it, rather than a shorter list than the plan.
    """
    identifier = ""
    label = ""
    if project is not None:
        identifier = project.api_identifier
        label = project.display_label
    elif metadata is not None:
        identifier = metadata.project_identifier

    if plan is None or plan.is_empty:
        return JiraWorkPlan(
            project_identifier=identifier,
            project_label=label,
            notes=(
                "There is no approved implementation plan to deliver. Generate and approve "
                "an implementation plan first.",
            ),
        )

    levels = plannable_levels(metadata) if metadata is not None else ()
    if not levels:
        return JiraWorkPlan(
            project_identifier=identifier,
            project_label=label,
            notes=(
                "This project reported no issue type whose required fields this app can "
                "supply, so nothing from the plan can be created in it. Choose a different "
                "project, or reduce what its create screen requires.",
            ),
        )

    notes: list = []
    issues: list = []
    unnamed_rungs: list = []
    budget = [MAX_DELIVERY_ISSUES]
    # Which plan depths this project can hold at all. Anything deeper is reported once,
    # by count, rather than item by item.
    depth_available = len(levels)

    def rung_for(work_type: str):
        depth = PLAN_DEPTH[work_type]
        return levels[depth] if depth < depth_available else None

    def add(plan_key, summary, work_type, description, parent="", criteria=()) -> bool:
        """Append one issue. Returns whether it is selected for creation."""
        level = rung_for(work_type)
        if level is None:
            issues.append(
                PlannedIssue(
                    plan_key=str(plan_key),
                    summary=summary_line(summary),
                    description=description,
                    acceptance_criteria=tuple(criteria),
                    parent_plan_key=str(parent),
                    selected=False,
                )
            )
            return False

        issue_type, by_name = _issue_type_for(level, work_type)
        if not by_name and level.depth not in unnamed_rungs:
            unnamed_rungs.append(level.depth)
            notes.append(
                "This project offers no issue type named {} at the rung a plan {} "
                "occupies, so `{}` is used there instead.".format(
                    " or ".join(
                        "'{}'".format(name) for name in PREFERRED_TYPE_NAMES[work_type]
                    ),
                    WORK_TYPE_LABEL.get(work_type, work_type),
                    issue_type.name or issue_type.id,
                )
            )

        # A subtask rung requires a parent, so an item that would land there without one
        # cannot be created. Reported rather than reparented onto something arbitrary.
        if level.subtask and not parent:
            issues.append(
                PlannedIssue(
                    plan_key=str(plan_key),
                    summary=summary_line(summary),
                    description=description,
                    acceptance_criteria=tuple(criteria),
                    selected=False,
                )
            )
            notes.append(
                "`{}` would be created at a subtask rung but has no parent in the plan, "
                "so it is left out. Jira cannot create a subtask without a parent.".format(
                    plan_key
                )
            )
            return False

        if budget[0] <= 0:
            issues.append(
                replace(
                    _planned(
                        plan_key, summary, level, issue_type, description, parent, criteria
                    ),
                    selected=False,
                )
            )
            return False

        budget[0] -= 1
        issues.append(
            _planned(plan_key, summary, level, issue_type, description, parent, criteria)
        )
        return True

    epics_selected: set = set()
    for epic in plan.epics:
        if add(epic.epic_id, epic.name or epic.epic_id, EPIC, _epic_description(epic, plan)):
            epics_selected.add(epic.epic_id)

    stories_selected: set = set()
    for story in plan.stories:
        # A story whose epic could not be created is created without a parent rather than
        # not at all: the work is real, and the plan's own epic id stays in the
        # description so the grouping is not lost.
        parent = story.epic_id if story.epic_id in epics_selected else ""
        if story.epic_id and not parent:
            notes.append(
                "`{}` names epic `{}`, which is not being created, so it is created "
                "without a parent. Its plan epic is recorded in the description.".format(
                    story.story_id, story.epic_id
                )
            )
        if add(
            story.story_id,
            story.title or story.story_id,
            STORY,
            _story_description(story, plan),
            parent,
            story.acceptance_criteria,
        ):
            stories_selected.add(story.story_id)

    for story in plan.stories:
        for task in story.tasks:
            if story.story_id not in stories_selected:
                issues.append(
                    PlannedIssue(
                        plan_key=str(task.task_id),
                        summary=summary_line(task.title or task.task_id),
                        description=_task_description(task, story, plan),
                        selected=False,
                    )
                )
                continue
            add(
                task.task_id,
                task.title or task.task_id,
                task.work_type if task.work_type in PLAN_DEPTH else TASK,
                _task_description(task, story, plan),
                story.story_id,
            )

    excluded = tuple(issue for issue in issues if not issue.selected)
    deepest_needed = _deepest_depth_needed(plan)
    if deepest_needed >= depth_available:
        notes.insert(
            0,
            "This project reports {} hierarchy rung(s) ({}), but this plan needs {}. The "
            "items that do not fit are listed unselected and will not be created; nothing "
            "is flattened onto the wrong parent.".format(
                depth_available,
                ", ".join(
                    "/".join(level.type_names) or "unnamed" for level in levels
                ),
                deepest_needed + 1,
            ),
        )
    if excluded and deepest_needed < depth_available:
        notes.append(
            "{} plan item(s) are not selected for creation. Each one's reason is above.".format(
                len(excluded)
            )
        )

    return JiraWorkPlan(
        project_identifier=identifier,
        project_label=label,
        issues=tuple(issues),
        notes=tuple(notes),
    )


def _deepest_depth_needed(plan) -> int:
    """The deepest plan rung that actually holds an item."""
    deepest = -1
    if plan.epics:
        deepest = max(deepest, PLAN_DEPTH[EPIC])
    if plan.stories:
        deepest = max(deepest, PLAN_DEPTH[STORY])
    for story in plan.stories:
        for task in story.tasks:
            work_type = task.work_type if task.work_type in PLAN_DEPTH else TASK
            deepest = max(deepest, PLAN_DEPTH[work_type])
    return deepest


# --- The stable plan item -> Jira issue mapping ---------------------------


@dataclass(frozen=True)
class DeliveryLink:
    """
    One implementation-plan item and the Jira issue it was created as.

    ``plan_item_id`` is the plan's own identifier and is the key of this mapping, which
    is what makes the mapping stable: it does not change when the plan is re-rendered,
    when a creation run is retried, or when a later run adds more issues.

    The PRD feature and architecture component ids are copied here at creation time
    rather than looked up in the plan afterwards. The plan can be regenerated in the
    same session, and a mapping that pointed into a plan no longer holding that item
    would lose the traceability it exists to record.
    """

    plan_item_id: str
    issue_key: str = ""
    issue_id: str = ""
    issue_type_name: str = ""
    summary: str = ""
    work_type: str = ""
    feature_ids: tuple = ()
    component_ids: tuple = ()

    @property
    def is_created(self) -> bool:
        return bool(self.issue_key or self.issue_id)


@dataclass(frozen=True)
class DeliveryMapping:
    """
    What an approved implementation plan actually became in one Jira project.

    The delivery record, and the reason a retry cannot duplicate anything: an item with
    a link here is already in Jira, so a later run skips it. Bound to one project on
    purpose -- the same plan delivered into a different project is a different mapping,
    and reusing one project's issue keys against another would be nonsense.

    Failures are deliberately not recorded as links. A link means "this exists in
    Jira"; a failure means the item is still pending, and recording it would make a
    retry skip the very item that needs creating.
    """

    project_identifier: str = ""
    project_label: str = ""
    site_url: str = ""
    links: tuple = ()
    notes: tuple = ()

    @property
    def issue_keys(self) -> dict:
        """Plan item id → Jira issue key, for every item that was created."""
        return {
            link.plan_item_id: link.issue_key
            for link in self.links
            if link.plan_item_id and link.issue_key
        }

    def key_for(self, plan_item_id: str) -> str:
        return self.issue_keys.get(str(plan_item_id), "")

    def link_for(self, plan_item_id: str) -> Optional[DeliveryLink]:
        for link in self.links:
            if link.plan_item_id == str(plan_item_id):
                return link
        return None

    @property
    def created_count(self) -> int:
        return len(tuple(link for link in self.links if link.is_created))

    @property
    def is_empty(self) -> bool:
        return not self.links


def _plan_item_index(plan) -> dict:
    """Plan item id → (work type, feature ids, component ids) for every item."""
    index: dict = {}
    for epic in getattr(plan, "epics", ()) or ():
        index[epic.epic_id] = (EPIC, tuple(epic.feature_ids), tuple(epic.component_ids))
    for story in getattr(plan, "stories", ()) or ():
        index[story.story_id] = (
            STORY,
            tuple(story.feature_ids),
            tuple(story.component_ids),
        )
        for task in story.tasks:
            index[task.task_id] = (
                task.work_type if task.work_type in PLAN_DEPTH else TASK,
                tuple(story.feature_ids),
                tuple(task.component_ids),
            )
    return index


def record_created_issues(mapping, results, work_plan=None, plan=None) -> DeliveryMapping:
    """
    Fold one creation run's outcomes into the mapping.

    Only successes become links, and a plan item already linked is replaced in place
    rather than appended -- so running this twice with the same results leaves one link
    per plan item, which is what makes a retry safe.
    """
    base = mapping if isinstance(mapping, DeliveryMapping) else DeliveryMapping()
    by_key = {
        issue.plan_key: issue for issue in getattr(work_plan, "issues", ()) or ()
    }
    traceability = _plan_item_index(plan) if plan is not None else {}

    links = list(base.links)
    positions = {link.plan_item_id: index for index, link in enumerate(links)}

    for result in results or ():
        if not getattr(result, "succeeded", False):
            continue
        plan_item_id = str(getattr(result, "plan_key", "") or "")
        if not plan_item_id:
            continue
        issue = by_key.get(plan_item_id)
        work_type, feature_ids, component_ids = traceability.get(plan_item_id, ("", (), ()))
        link = DeliveryLink(
            plan_item_id=plan_item_id,
            issue_key=str(getattr(result, "issue_key", "") or ""),
            issue_id=str(getattr(result, "issue_id", "") or ""),
            issue_type_name=getattr(issue, "issue_type_name", "") if issue else "",
            summary=str(getattr(result, "summary", "") or ""),
            work_type=work_type,
            feature_ids=feature_ids,
            component_ids=component_ids,
        )
        if plan_item_id in positions:
            links[positions[plan_item_id]] = link
        else:
            positions[plan_item_id] = len(links)
            links.append(link)

    return replace(base, links=tuple(links))


def known_issue_keys(mapping) -> dict:
    """
    The plan items a creation run must not create again, and the keys they already have.

    Handed to the creation loop, which skips an item it already has a key for while
    still using that key as the parent of a child created now. That is the whole of the
    idempotency guarantee: Jira's create endpoint offers no idempotency key, so a
    repeat has to be prevented before the request rather than detected after it.
    """
    if not isinstance(mapping, DeliveryMapping):
        return {}
    return dict(mapping.issue_keys)


def pending_plan_keys(work_plan, mapping) -> tuple:
    """The selected plan items that have not been created yet, in plan order."""
    created = known_issue_keys(mapping)
    return tuple(
        issue.plan_key
        for issue in getattr(work_plan, "issues", ()) or ()
        if issue.selected and issue.plan_key not in created
    )


# --- Delivery status, read from the evidence this app holds ---------------


@dataclass(frozen=True)
class DeliveryProgress:
    """
    How much of the mapped plan exists in Jira, counted from creation evidence.

    Derived from what this app recorded when it created the issues, not from a Jira
    workflow read: no read-issue endpoint exists in this app's Jira service, and the
    per-issue read that used to exist was removed with the requirement-drift feature it
    served. So this reports delivery *creation* state, which is a fact the app owns,
    and never claims to know an issue's workflow status.
    """

    total: int = 0
    created: int = 0
    failed: int = 0
    excluded: int = 0

    @property
    def pending(self) -> int:
        return max(self.total - self.created, 0)

    @property
    def is_complete(self) -> bool:
        return self.total > 0 and self.created >= self.total

    @property
    def has_started(self) -> bool:
        return self.created > 0


def delivery_progress(work_plan, mapping, results=()) -> DeliveryProgress:
    """Creation progress for one mapped plan against one project."""
    issues = tuple(getattr(work_plan, "issues", ()) or ())
    selected = tuple(issue for issue in issues if issue.selected)
    created = known_issue_keys(mapping)
    failed = tuple(
        result
        for result in results or ()
        if not getattr(result, "succeeded", False)
        and str(getattr(result, "plan_key", "") or "") not in created
    )
    return DeliveryProgress(
        total=len(selected),
        created=len([issue for issue in selected if issue.plan_key in created]),
        failed=len(failed),
        excluded=len(issues) - len(selected),
    )
