"""
BRD-to-Jira work-plan transformation.

Turns a reviewed BRD into a proposal of Jira issues. Nothing here talks to Jira:
this module imports no HTTP client and takes no access token, so generating a plan
cannot create, change, or even read anything in Jira. ``TECHNICAL_ARCHITECTURE.md``
puts that boundary plainly -- "Generating a plan must never create issues" -- and
keeping the transformation in a module with no network access is how that is
enforced rather than merely promised. Review edits, deletion and validation live
here too and inherit the same boundary: they replace a local plan object and never
talk to Jira.

Two rules shape the mapping.

**The project's hierarchy is the project's own.** ``PRD.md`` requires that no
project be assumed to use the same hierarchy as another, so the levels a plan uses
are chosen from the issue types Jira actually reported for the selected project,
ordered by the ``hierarchyLevel`` Jira gave them. No issue type is matched by name.
A project whose validated types occupy one level gets a flat plan; a project with
no validated subtask type gets no subtasks. That is what "as supported" means, and
it is why a plan cannot be built before ``JiraService.get_project_metadata`` has
run.

**Nothing is invented.** Every proposed issue restates BRD content that already
passed the evidence check in ``main.validate_and_create_brd_data``, and carries the
requirement id and verbatim transcript quote that content came from. Where the BRD
does not record a link -- it holds acceptance criteria and action items as flat
lists, not as children of a requirement -- the only link drawn here is an explicit
textual one: an acceptance criterion or action item that *names* a requirement id
belongs to that requirement. Anything that names none stays where the BRD put it,
or is reported in ``JiraWorkPlan.notes``. Guessing which requirement a criterion
"probably" belongs to would be this module inventing a relationship nobody stated,
which is the one thing this app is built not to do.

There is deliberately no second AI call here. The requirements were already
validated against the transcript; re-generating prose from them would reopen the
fabrication risk that validation closed, and it would make the plan untestable
without a live model.
"""

import re
from dataclasses import replace

from brd_models import BRDData, Requirement
from jira_models import (
    MAX_SUMMARY_LENGTH,
    JiraProject,
    JiraProjectMetadata,
    JiraWorkPlan,
    PlannedIssue,
)

# Plan-local key for the single container issue, when the project offers a level to
# put one at. Not a Jira key -- no issue exists yet.
_ROOT_PLAN_KEY = "BRD"

# Appended to every proposed description. Survives into Jira if a later ticket
# creates the issue, which is where it earns its place: someone reading the issue in
# Jira can tell where it came from.
_PROVENANCE = (
    "_Proposed by Auto-BRD Generator from the reviewed BRD. "
    "Not yet created in Jira._"
)


def _summary(text: str) -> str:
    """
    A Jira-acceptable summary line.

    Jira refuses a summary containing a line break or longer than
    ``MAX_SUMMARY_LENGTH``, and the summary is a field this app fills in itself, so
    the constraint is applied when the plan is built rather than discovered at
    creation time. The full text is never lost -- it is repeated in the description.
    """
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) <= MAX_SUMMARY_LENGTH:
        return collapsed
    return collapsed[: MAX_SUMMARY_LENGTH - 1].rstrip() + "…"


def _names_requirement(text: str, requirement_id: str) -> bool:
    """
    Whether ``text`` explicitly names ``requirement_id``.

    Bounded so ``FR-1`` does not match inside ``FR-12``: an off-by-one here would
    attach someone's acceptance criterion to the wrong requirement, which is worse
    than leaving it unattached.
    """
    identifier = str(requirement_id or "").strip()
    if not identifier:
        return False
    pattern = r"(?<![0-9A-Za-z])" + re.escape(identifier) + r"(?![0-9A-Za-z])"
    return re.search(pattern, str(text or "")) is not None


def _quote(text: str) -> str:
    """Render text as a Markdown block quote, preserving its own line breaks."""
    return "\n".join("> {}".format(line) for line in str(text or "").splitlines())


def _bullets(heading: str, items) -> list:
    """A bolded heading followed by one bullet per item, or nothing if empty."""
    kept = [str(item).strip() for item in items if str(item).strip()]
    if not kept:
        return []
    return ["**{}**".format(heading)] + ["- {}".format(item) for item in kept]


def choose_issue_types(metadata: JiraProjectMetadata) -> tuple:
    """
    Pick the issue types a plan may use, from what Jira reported for this project.

    Returns ``(container, item, subtask)``, any of which may be ``None``. Only issue
    types that passed the required-field check are eligible: an issue type Jira would
    refuse for want of a field this app cannot supply has no business in a plan that
    a later step is meant to be able to create.

    Ordering is Jira's ``hierarchyLevel``, never a name. The lowest reported level
    among the validated non-subtask types holds the requirements; if a higher level
    exists, the highest one holds a single container issue. Where several types share
    a level -- a project commonly has Story, Task and Bug together at one level --
    the first Jira listed is used, because choosing between them on any other basis
    would mean ranking names. Letting the user pick among them is review-time work
    (``FRONTEND_SPEC.md`` lists "Issue-type selection where supported" under the
    review interface), not generation-time work.

    A type that reported no hierarchy level cannot be ordered against one that did,
    so it is only ever used as a flat item level, and only when nothing reported a
    level at all.
    """
    plannable = metadata.plannable_issue_types
    subtask_types = [issue_type for issue_type in plannable if issue_type.subtask]
    standard = [issue_type for issue_type in plannable if not issue_type.subtask]

    by_level: dict = {}
    for issue_type in standard:
        if issue_type.hierarchy_level is not None:
            by_level.setdefault(issue_type.hierarchy_level, []).append(issue_type)

    container = None
    item = None
    if by_level:
        levels = sorted(by_level)
        item = by_level[levels[0]][0]
        if len(levels) > 1:
            container = by_level[levels[-1]][0]
    elif standard:
        item = standard[0]

    return container, item, (subtask_types[0] if subtask_types else None)


def _container_description(brd_data: BRDData) -> str:
    lines: list = []
    overview = brd_data.project_overview
    if overview.project_name:
        lines.append("**Project:** {}".format(overview.project_name))
    if overview.date:
        lines.append("**Meeting date:** {}".format(overview.date))
    if lines:
        lines.append("")

    objectives = _bullets("Business objectives", brd_data.business_objectives)
    if objectives:
        lines.extend(objectives + [""])
    background = _bullets("Background", brd_data.background_information)
    if background:
        lines.extend(background + [""])

    lines.append(_PROVENANCE)
    return "\n".join(lines).strip()


def _requirement_description(requirement: Requirement) -> str:
    lines = [str(requirement.statement or "").strip(), ""]
    if requirement.requirement_id:
        lines.extend(["**BRD requirement:** {}".format(requirement.requirement_id), ""])
    evidence = str(requirement.source_evidence or "").strip()
    if evidence:
        lines.extend(
            [
                "**Source evidence — verbatim from the transcript**",
                _quote(evidence),
                "",
            ]
        )
    lines.append(_PROVENANCE)
    return "\n".join(lines).strip()


def _action_item_description(action_item, parent_plan_key: str) -> str:
    lines = [str(action_item.item or "").strip(), ""]
    if action_item.owner:
        lines.append("**Owner stated in the meeting:** {}".format(action_item.owner))
    if action_item.due_date:
        lines.append("**Due date stated in the meeting:** {}".format(action_item.due_date))
    lines.extend(["**Names BRD requirement:** {}".format(parent_plan_key), "", _PROVENANCE])
    return "\n".join(lines).strip()


def _plan_keys(requirements: list) -> list:
    """
    One unique plan-local key per requirement, preferring its BRD id.

    Ids come from a language model, so two requirements carrying the same id is
    possible. Left alone, a duplicate key would make a subtask appear under both,
    so the second occurrence is disambiguated.
    """
    keys: list = []
    used = set()
    for index, requirement in enumerate(requirements, start=1):
        candidate = str(requirement.requirement_id or "").strip() or "REQ-{}".format(index)
        if candidate in used:
            candidate = "{} ({})".format(candidate, index)
        used.add(candidate)
        keys.append(candidate)
    return keys


def build_work_plan(
    brd_data: BRDData,
    project: JiraProject,
    metadata: JiraProjectMetadata,
) -> JiraWorkPlan:
    """
    Propose Jira issues for one project from one BRD. Creates nothing.

    One issue per confirmed requirement, at the lowest validated level the project
    reported; a single container issue above them when the project has a level to
    put one at; a subtask for each action item that names a requirement, when the
    project has a validated subtask type. Anything the BRD holds that could not be
    placed is reported in ``JiraWorkPlan.notes``, never dropped quietly.
    """
    identifier = project.api_identifier
    label = project.display_label
    notes: list = []

    def empty() -> JiraWorkPlan:
        return JiraWorkPlan(
            project_identifier=identifier,
            project_label=label,
            notes=tuple(notes),
        )

    # Metadata read for a different project describes a different set of issue types.
    # Planning against it is exactly the assumed-hierarchy mistake the PRD forbids.
    read_for = metadata.project_identifier
    if identifier and read_for and identifier != read_for:
        notes.append(
            "No plan was generated: the issue types on hand were read for project "
            "'{}', not for '{}'. Read this project's issue types and required fields "
            "first.".format(read_for, identifier)
        )
        return empty()

    if metadata.truncated:
        notes.append(
            "The issue types for this project could not all be read, so the levels used "
            "below were chosen from an incomplete list."
        )

    container_type, item_type, subtask_type = choose_issue_types(metadata)

    if item_type is None:
        if not metadata.issue_types:
            notes.append(
                "No plan was generated: Jira reported no issue type for this project's "
                "create screen."
            )
        elif subtask_type is not None:
            # Reporting a failed field check here would be untrue: a subtask type that
            # reached this point passed it. What it cannot do is stand on its own.
            notes.append(
                "No plan was generated: every issue type that passed the required-field "
                "check for this project is a subtask type, and a subtask needs a parent. "
                "There is no type an issue could be proposed at above them."
            )
        else:
            notes.append(
                "No plan was generated: none of the {} issue type(s) Jira reported for this "
                "project passed the required-field check, so there is no type an issue could "
                "be proposed as.".format(len(metadata.issue_types))
            )
        return empty()

    requirements = [
        requirement
        for requirement in (
            list(brd_data.functional_requirements) + list(brd_data.non_functional_requirements)
        )
        if str(requirement.statement or "").strip()
    ]
    if not requirements:
        notes.append(
            "No plan was generated: this BRD holds no confirmed requirement. Only "
            "requirements whose evidence was found verbatim in the transcript reach the "
            "BRD, so there is nothing here to propose as an issue."
        )
        return empty()

    keys = _plan_keys(requirements)

    # Acceptance criteria are stated for the BRD as a whole. One is attached to a
    # requirement only when it names that requirement.
    attached: dict = {}
    unattached: list = []
    for criterion in (str(c).strip() for c in brd_data.acceptance_criteria):
        if not criterion:
            continue
        named = [
            key
            for requirement, key in zip(requirements, keys)
            if _names_requirement(criterion, requirement.requirement_id)
        ]
        if named:
            for key in named:
                attached.setdefault(key, []).append(criterion)
        else:
            unattached.append(criterion)

    issues: list = []

    root_key = ""
    if container_type is not None:
        root_key = _ROOT_PLAN_KEY
        issues.append(
            PlannedIssue(
                plan_key=root_key,
                summary=_summary(brd_data.project_title or "Untitled Project"),
                issue_type_id=container_type.id,
                issue_type_name=container_type.name,
                hierarchy_level=container_type.hierarchy_level,
                description=_container_description(brd_data),
                acceptance_criteria=tuple(unattached),
            )
        )
    else:
        notes.append(
            "This project reported no validated issue type above '{}', so the plan is flat: "
            "every requirement is proposed at that one level with no issue grouping "
            "them.".format(item_type.name or item_type.id)
        )
        if unattached:
            notes.append(
                "{} acceptance criterion(s) name no requirement and there is no grouping "
                "issue to hold them, so they are not attached to anything below. They remain "
                "in the BRD.".format(len(unattached))
            )

    for requirement, key in zip(requirements, keys):
        issues.append(
            PlannedIssue(
                plan_key=key,
                summary=_summary(requirement.statement),
                issue_type_id=item_type.id,
                issue_type_name=item_type.name,
                hierarchy_level=item_type.hierarchy_level,
                description=_requirement_description(requirement),
                acceptance_criteria=tuple(attached.get(key, ())),
                parent_plan_key=root_key,
                source_requirement_id=str(requirement.requirement_id or ""),
            )
        )

    action_items = [
        action_item
        for action_item in brd_data.action_items
        if str(action_item.item or "").strip()
    ]
    unreferenced = 0
    unsupported = 0
    for index, action_item in enumerate(action_items, start=1):
        target = next(
            (
                key
                for requirement, key in zip(requirements, keys)
                if _names_requirement(action_item.item, requirement.requirement_id)
            ),
            None,
        )
        if target is None:
            unreferenced += 1
            continue
        if subtask_type is None:
            unsupported += 1
            continue
        issues.append(
            PlannedIssue(
                plan_key="{}-A{}".format(target, index),
                summary=_summary(action_item.item),
                issue_type_id=subtask_type.id,
                issue_type_name=subtask_type.name,
                hierarchy_level=subtask_type.hierarchy_level,
                description=_action_item_description(action_item, target),
                parent_plan_key=target,
            )
        )

    if unreferenced:
        notes.append(
            "{} action item(s) name no requirement, so none was proposed as a subtask. "
            "Which requirement an action item belongs to is not something this app will "
            "guess.".format(unreferenced)
        )
    if unsupported:
        notes.append(
            "{} action item(s) name a requirement, but this project reported no validated "
            "subtask type, so no subtask was proposed.".format(unsupported)
        )

    return JiraWorkPlan(
        project_identifier=identifier,
        project_label=label,
        issues=tuple(issues),
        notes=tuple(notes),
    )


# Fields a reviewer may change on one proposed issue. Hierarchy identity -- plan key
# and parent -- is not among them: those are structural, and a parent change would
# be this module inventing a relationship. Type changes go through
# ``set_planned_issue_type`` so a level cannot be swapped by passing a new id alone.
_EDITABLE_ISSUE_FIELDS = frozenset(
    {"summary", "description", "acceptance_criteria", "selected"}
)


def _issue_by_key(plan: JiraWorkPlan, plan_key: str):
    for issue in plan.issues:
        if issue.plan_key == plan_key:
            return issue
    return None


def _with_issue(plan: JiraWorkPlan, updated: PlannedIssue) -> JiraWorkPlan:
    return replace(
        plan,
        issues=tuple(
            updated if issue.plan_key == updated.plan_key else issue
            for issue in plan.issues
        ),
    )


def _type_by_id(metadata: JiraProjectMetadata, type_id: str):
    wanted = str(type_id or "")
    for issue_type in metadata.issue_types:
        if issue_type.id == wanted:
            return issue_type
    return None


def _descendant_keys(plan: JiraWorkPlan, plan_key: str) -> set:
    """``plan_key`` and every issue that names it as an ancestor, however deep."""
    drop = {plan_key}
    growing = True
    while growing:
        growing = False
        for issue in plan.issues:
            if issue.parent_plan_key in drop and issue.plan_key not in drop:
                drop.add(issue.plan_key)
                growing = True
    return drop


def update_planned_issue(plan: JiraWorkPlan, plan_key: str, **changes) -> JiraWorkPlan:
    """
    Replace editable fields on one proposed issue. Unknown keys are ignored.

    Returns the original plan when ``plan_key`` is not in it, so a stale widget
    cannot invent an issue.
    """
    issue = _issue_by_key(plan, plan_key)
    if issue is None:
        return plan
    applied = {key: value for key, value in changes.items() if key in _EDITABLE_ISSUE_FIELDS}
    if not applied:
        return plan
    return _with_issue(plan, replace(issue, **applied))


def delete_planned_issue(plan: JiraWorkPlan, plan_key: str) -> JiraWorkPlan:
    """
    Remove one proposed issue and every descendant.

    Cascade is required: a leftover child would name a parent that is no longer in
    the plan, which is the orphaned-hierarchy case the review step must not leave
    behind.
    """
    if _issue_by_key(plan, plan_key) is None:
        return plan
    drop = _descendant_keys(plan, plan_key)
    return replace(
        plan,
        issues=tuple(issue for issue in plan.issues if issue.plan_key not in drop),
    )


def compatible_issue_types(issue: PlannedIssue, metadata: JiraProjectMetadata) -> tuple:
    """
    Plannable issue types that occupy the same place in this project's hierarchy.

    Same ``hierarchy_level`` and same ``subtask`` flag as the type the issue already
    carries. A project often lists Story, Task and Bug together at one level;
    switching among those is the review-time choice ``FRONTEND_SPEC.md`` names.
    Switching to a type at a different level -- or between a subtask type and a
    standard type -- would move the issue in the hierarchy, which this step does
    not do.

    If the issue's current type is not in the metadata, nothing is compatible:
    there is no reported flag to match against, and guessing one would invent
    hierarchy.
    """
    current = _type_by_id(metadata, issue.issue_type_id)
    if current is None:
        return ()
    return tuple(
        issue_type
        for issue_type in metadata.plannable_issue_types
        if issue_type.subtask == current.subtask
        and issue_type.hierarchy_level == current.hierarchy_level
    )


def set_planned_issue_type(plan: JiraWorkPlan, plan_key: str, issue_type, metadata) -> JiraWorkPlan:
    """
    Point one issue at a different type, only when that type is compatible.

    Copies the type's id, name and reported hierarchy level. Refuses anything else
    so a caller cannot promote or demote an issue by passing an arbitrary type.
    """
    issue = _issue_by_key(plan, plan_key)
    if issue is None or issue_type is None:
        return plan
    allowed = {candidate.id for candidate in compatible_issue_types(issue, metadata)}
    if issue_type.id not in allowed:
        return plan
    return _with_issue(
        plan,
        replace(
            issue,
            issue_type_id=issue_type.id,
            issue_type_name=issue_type.name,
            hierarchy_level=issue_type.hierarchy_level,
        ),
    )


def _description_is_required(issue_type) -> bool:
    """Whether Jira marked ``description`` required and does not default it."""
    if issue_type is None:
        return False
    return any(
        field.field_id == "description" and field.required and not field.has_default_value
        for field in issue_type.required_fields
    )


def validate_work_plan(plan: JiraWorkPlan, metadata: JiraProjectMetadata, project=None) -> tuple:
    """
    Reasons this proposal is not ready. Empty means the selected issues are
    internally consistent against the metadata on hand.

    Creates nothing. The checks are the ones a later creation step would have to
    trust: the project's own identifier, the issue types Jira reported, required
    fields this app knows how to fill, and parent/child selection. Unknown extra
    identifiers are not invented here.
    """
    errors: list = []
    identifier = plan.project_identifier
    read_for = metadata.project_identifier
    if identifier and read_for and identifier != read_for:
        errors.append(
            "The issue types on hand were read for project '{}', not for '{}'.".format(
                read_for, identifier
            )
        )
        return tuple(errors)

    if project is not None:
        project_identifier = project.api_identifier
        if identifier and project_identifier and identifier != project_identifier:
            errors.append(
                "This plan was built for project '{}', not for '{}'.".format(
                    identifier, project_identifier
                )
            )
            return tuple(errors)

    by_key = {issue.plan_key: issue for issue in plan.issues}
    selected = tuple(issue for issue in plan.issues if issue.selected)
    if not selected:
        errors.append("No issue is selected.")
        return tuple(errors)

    for issue in plan.issues:
        if issue.parent_plan_key and issue.parent_plan_key not in by_key:
            errors.append(
                "{} names parent '{}', which is not in this plan.".format(
                    issue.plan_key, issue.parent_plan_key
                )
            )

    for issue in selected:
        summary = issue.summary if isinstance(issue.summary, str) else ""
        collapsed = summary.strip()
        if not collapsed:
            errors.append("{} has an empty summary.".format(issue.plan_key))
        elif "\n" in summary or "\r" in summary:
            errors.append("{} has a summary containing a line break.".format(issue.plan_key))
        elif len(summary) > MAX_SUMMARY_LENGTH:
            errors.append(
                "{} has a summary longer than {} characters.".format(
                    issue.plan_key, MAX_SUMMARY_LENGTH
                )
            )

        issue_type = _type_by_id(metadata, issue.issue_type_id)
        if issue_type is None or issue_type.validation_state != "ok":
            errors.append(
                "{} uses an unsupported issue type.".format(issue.plan_key)
            )
        else:
            if issue_type.hierarchy_level != issue.hierarchy_level:
                errors.append(
                    "{} uses an issue type at a different hierarchy level than this "
                    "issue occupies.".format(issue.plan_key)
                )
            if _description_is_required(issue_type) and not str(issue.description or "").strip():
                errors.append("{} is missing a description.".format(issue.plan_key))
            if issue_type.subtask and not issue.parent_plan_key:
                errors.append(
                    "{} is a subtask and has no parent.".format(issue.plan_key)
                )

        if issue.parent_plan_key:
            parent = by_key.get(issue.parent_plan_key)
            if parent is not None and not parent.selected:
                errors.append(
                    "{} is selected but its parent '{}' is not.".format(
                        issue.plan_key, issue.parent_plan_key
                    )
                )

    return tuple(errors)
