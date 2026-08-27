"""
Generating an implementation plan from an approved PRD and an approved architecture.

The same three boundaries the PRD and architecture generators work under apply here, for
the same reasons.

**Both approved artifacts are required inputs.** A plan needs the PRD to know what
outcomes to deliver and the architecture to know where in the system to deliver them. With
either one missing there is nothing to decompose, and ``generate_implementation_plan``
refuses rather than inventing scope or inventing a system.

**Stories trace to features, tasks trace to components.** Feature ids are checked against
the ids the PRD minted and component ids against the ids the architecture minted; unknown
ids are dropped. A *story* naming no feature does not survive -- that is product scope
nobody approved, and technical work belongs in a task. A *task* naming no component is
kept, because repository setup, a dependency upgrade and a release step genuinely touch no
single component.

**The model's answer is a proposal, not a plan.** Everything it returns passes through
``assemble_implementation_plan``, which is ordinary deterministic code: text is coerced and
capped, lists are bounded, unknown ids are removed, unresolvable and self-referential
dependencies are dropped, dependency cycles are broken, priorities outside the vocabulary
are normalised, and every repair is recorded in ``ImplementationPlan.notes``. There is no
second model call to fix up the first. If the model is unavailable, unreachable or returns
nothing usable, a deterministic skeleton derived straight from the PRD and architecture is
used instead and it says so.

Nothing here writes to a tracker. This stage produces the plan; the delivery stage is what
turns an approved plan into issues, and doing it here would create work items nobody had
reviewed. Nothing here creates a language-model client either: the call is injected as
``generate(prompt) -> str``, so this module has no network access of its own, no credential
of its own, and no import of ``main``.
"""

from dataclasses import replace
from typing import Optional

from architecture_models import LAYER_LABEL, ArchitectureData, prd_feature_ids
from implementation_plan_models import (
    DEFAULT_PRIORITY,
    SUBTASK,
    TASK,
    Epic,
    ImplementationPlan,
    Story,
    TechnicalTask,
    architecture_component_ids,
    normalise_priority,
)
from model_output import (
    ModelResponseError,
    block,
    excerpt,
    first,
    json_payload,
    line,
    normalise_id,
    strings,
)
from prd_models import PRDData

# Caps on what one model response may turn into. Not style limits: they bound how much a
# single response can inflate session state and the review screen. Anything trimmed is
# reported in the plan notes rather than silently dropped.
MAX_EPICS = 15
MAX_STORIES = 60
MAX_TASKS_PER_STORY = 12
MAX_LIST_ITEMS = 12
MAX_OVERVIEW_LENGTH = 2000
MAX_SUMMARY_LENGTH = 600
MAX_STATEMENT_LENGTH = 300
MAX_TITLE_LENGTH = 200

# How many PRD features the deterministic fallback covers. It emits one epic and one story
# per feature plus a task per realising component, so this is the cap that keeps the
# fallback readable.
MAX_BASELINE_FEATURES = 15

# How many components the prompt describes per architecture layer. The plan needs to know
# what exists; it does not need every field of every component.
MAX_PROMPT_COMPONENTS_PER_LAYER = 20

# How many items of a kind a note lists by name before it just gives the count.
_NOTE_SAMPLE = 5

# Words a model uses when it means a subtask rather than a task. Anything else is a task,
# which is the safe default: a task is a valid work item everywhere, a subtask is not.
_SUBTASK_WORDS = ("subtask", "sub-task", "sub task", "child", "step")


class ImplementationPlanResponseError(ModelResponseError):
    """
    The model's answer could not be read as an implementation plan.

    Carries a message safe to show a reviewer: what was wrong with the shape of the
    response, never the response itself.
    """


def _sample(values, limit: int = _NOTE_SAMPLE) -> str:
    """``limit`` values quoted for a note, with a count when there are more."""
    listed = list(values)
    shown = ", ".join("'{}'".format(excerpt(value, 80)) for value in listed[:limit])
    if len(listed) > limit:
        shown += " and {} more".format(len(listed) - limit)
    return shown


def _capped(values, limit: int = MAX_LIST_ITEMS) -> tuple:
    """Single-line strings, each capped, the list itself bounded."""
    return tuple(excerpt(value, MAX_STATEMENT_LENGTH) for value in strings(values)[:limit])


def _lookup(identifiers) -> dict:
    """Comparison form of every id, mapped to the id as its own artifact spells it."""
    table: dict = {}
    for identifier in identifiers:
        key = normalise_id(identifier)
        if key:
            table.setdefault(key, identifier)
    return table


def _work_type(value) -> str:
    """``SUBTASK`` when the model said so, ``TASK`` otherwise."""
    spoken = normalise_id(value)
    if spoken and any(word in spoken for word in _SUBTASK_WORDS):
        return SUBTASK
    return TASK


# --- The prompt -----------------------------------------------------------

# The output contract, kept as a literal so no formatting pass has to escape its braces.
# Field names match ``implementation_plan_models`` where they correspond.
_JSON_SHAPE = """{
  "overview": "two or three sentences on how the build is sequenced and why in that order",
  "epics": [
    {
      "key": "E1",
      "name": "the capability, named for the business outcome it delivers",
      "goal": "the outcome this capability exists for, in business terms",
      "feature_ids": ["F-1", "F-2"],
      "priority": "Highest, High, Medium, Low or Lowest"
    }
  ],
  "stories": [
    {
      "key": "S1",
      "epic": "E1",
      "title": "the outcome, short enough to read in a backlog",
      "user_story": "As a <role>, I want <capability> so that <benefit>",
      "acceptance_criteria": ["a condition that decides whether this story is done"],
      "feature_ids": ["F-1"],
      "component_ids": ["C-1", "C-4"],
      "priority": "Highest, High, Medium, Low or Lowest",
      "depends_on": ["S2"],
      "estimate": "your own size or point value, or leave empty",
      "test_expectations": ["what proving this story involves, e.g. integration test across C-1 and C-4"],
      "tasks": [
        {
          "title": "the technical work, e.g. add the order table and migration",
          "work_type": "task or subtask",
          "detail": "what is changed, in technical terms",
          "component_ids": ["C-1"],
          "depends_on": ["another task title in this story"],
          "estimate": "your own size or point value, or leave empty"
        }
      ]
    }
  ]
}"""


def _section(heading: str, lines) -> list:
    """One prompt section, or nothing when there is nothing to say."""
    listed = [line(value) for value in lines if line(value)]
    if not listed:
        return []
    return ["{}:".format(heading)] + ["- {}".format(value) for value in listed] + [""]


def _in_layer(architecture, layer: str) -> tuple:
    """
    One layer's components, tolerating an architecture that predates ``layer()``.

    Read defensively for the same reason ``architecture_component_ids`` uses ``getattr``:
    the prompt is the one place a partially-shaped artifact must not raise.
    """
    reader = getattr(architecture, "layer", None)
    if callable(reader):
        return tuple(reader(layer))
    return tuple(
        component
        for component in getattr(architecture, "components", ()) or ()
        if getattr(component, "layer", "") == layer
    )


def build_implementation_plan_prompt(
    prd_data: PRDData, architecture: ArchitectureData
) -> str:
    """
    The prompt: the approved PRD, the approved architecture, the contract.

    Feature ids and component ids are given with their names, because the model has to name
    the ids each story and task belongs to, and it can only do that if it was told them.
    """
    title = line(getattr(prd_data, "project_title", "")) or "Untitled Project"
    lines = [
        "You are a principal engineer turning an approved product definition and an "
        "approved technical architecture into an implementation plan a delivery team can "
        "execute.",
        "",
        "Both documents below have been reviewed and approved. The PRD is the product "
        "source of truth and the architecture is the technical source of truth. Decompose "
        "them into work -- do not restate them.",
        "",
        "Project: {}".format(title),
        "",
    ]

    overview = block(getattr(architecture, "overview", ""))
    if overview:
        lines += ["Architecture overview:", overview[:MAX_OVERVIEW_LENGTH], ""]

    lines += _section("Product goals", getattr(prd_data, "goals", ()) or ())

    for feature in getattr(prd_data, "features", ()) or ():
        identifier = line(getattr(feature, "feature_id", ""))
        if not identifier:
            continue
        lines += _section(
            "Feature {} — {}".format(identifier, line(getattr(feature, "name", ""))),
            (
                [line(getattr(feature, "summary", ""))]
                + list(getattr(feature, "behaviours", ()) or ())
                + list(getattr(feature, "edge_cases", ()) or ())
                + [
                    "Acceptance: {}".format(value)
                    for value in getattr(feature, "acceptance_criteria", ()) or ()
                ]
            ),
        )

    for layer, label in LAYER_LABEL.items():
        in_layer = _in_layer(architecture, layer)[:MAX_PROMPT_COMPONENTS_PER_LAYER]
        lines += _section(
            "{} components".format(label),
            [
                "{} {} — {} (realises {})".format(
                    line(getattr(component, "component_id", "")),
                    line(getattr(component, "name", "")),
                    line(getattr(component, "responsibility", "")),
                    ", ".join(getattr(component, "feature_ids", ()) or ()) or "cross-cutting",
                )
                for component in in_layer
            ],
        )

    lines += _section(
        "Architecture decisions",
        [
            "{}: {} — {}".format(
                line(getattr(decision, "title", "")),
                line(getattr(decision, "choice", "")),
                line(getattr(decision, "rationale", "")),
            )
            for decision in getattr(architecture, "decisions", ()) or ()
        ],
    )
    lines += _section(
        "Data flows",
        [
            "{}: {}".format(
                line(getattr(flow, "name", "")),
                " → ".join(strings(getattr(flow, "steps", ()) or ())),
            )
            for flow in getattr(architecture, "flows", ()) or ()
        ],
    )
    lines += _section(
        "External integrations",
        [
            "{} — {}".format(
                line(getattr(integration, "name", "")),
                line(getattr(integration, "purpose", "")),
            )
            for integration in getattr(architecture, "integrations", ()) or ()
        ],
    )
    lines += _section(
        "Authentication and authorization", getattr(architecture, "auth_approach", ()) or ()
    )
    lines += _section(
        "Technical dependencies", getattr(architecture, "dependencies", ()) or ()
    )
    lines += _section(
        "Technical risks",
        [
            "{} (impact: {})".format(
                line(getattr(risk, "statement", "")), line(getattr(risk, "impact", ""))
            )
            for risk in getattr(architecture, "risks", ()) or ()
        ],
    )

    lines += [
        "Rules:",
        "- Use three levels. An epic is a business capability and groups stories. A story "
        "is a user or product outcome with acceptance criteria. A task or subtask is "
        "technical implementation work.",
        "- Do not force technical work into a user story. A migration, a pipeline or an "
        "SDK upgrade is a task under the story it enables, not a story of its own.",
        "- Every story must name at least one feature id it delivers, using the ids exactly "
        "as spelled above. A story that delivers no listed feature will be discarded.",
        "- Name the component ids each story and task changes, using the ids exactly as "
        "spelled above. A genuinely cross-cutting task may name none.",
        "- Never introduce a feature id or component id that is not listed above.",
        "- Cover every feature and every component at least once, across the whole plan.",
        "- Give each story acceptance criteria that can be checked, and at least one "
        "technical task, so the story can be implemented without guesswork.",
        "- Order the work with depends_on: name the story keys that must be delivered "
        "first. Do not create circular dependencies.",
        "- Prioritise by business value and dependency order, not by how hard the work is.",
        "- State only work the material above supports. Leave a list empty rather than "
        "inventing an entry.",
        "",
        "Return JSON only, in exactly this shape:",
        _JSON_SHAPE,
    ]
    return "\n".join(lines)


# --- Reading the response -------------------------------------------------


def parse_implementation_plan_response(raw) -> dict:
    """
    The model's answer as a dict of plan sections.

    A bare list is accepted as the story list, because a model asked for a plan will
    occasionally answer with just the stories.
    """
    try:
        payload = json_payload(raw, "AI implementation planner")
    except ModelResponseError as error:
        raise ImplementationPlanResponseError(str(error)) from error

    if isinstance(payload, list):
        return {"stories": payload}
    if not isinstance(payload, dict):
        raise ImplementationPlanResponseError(
            "The AI implementation planner's response was not an implementation plan object."
        )

    nested = first(payload, "implementation_plan", "plan", "backlog")
    if isinstance(nested, dict):
        payload = nested

    named = ("stories", "epics", "user_stories", "work_items", "items")
    if not any(isinstance(payload.get(name), (list, tuple, dict)) for name in named):
        raise ImplementationPlanResponseError(
            "The AI implementation planner's response contained no epics or stories."
        )
    return payload


def _rows(payload: dict, *names) -> list:
    """The first named field read as a list of dict rows."""
    value = first(payload, *names)
    if isinstance(value, dict):
        value = list(value.values())
    if not isinstance(value, (list, tuple)):
        return []
    return [row for row in value if isinstance(row, dict)]


# --- Deterministic assembly -----------------------------------------------


def break_dependency_cycles(stories) -> tuple:
    """
    ``(stories, dropped)`` with every dependency edge that closes a cycle removed.

    A cycle means no story in it can ever start, so it has to go. Only the closing edge is
    dropped, not the whole dependency, and which edge closes a cycle is decided by plan
    order -- so the same proposal always yields the same plan. ``dropped`` describes each
    removed edge for the notes.

    Public because the review editor needs it too: a reviewer re-pointing dependencies by
    hand can close a cycle just as a model can, and repairing it in two places would be
    two chances to repair it differently.
    """
    stories = list(stories)
    position = {story.story_id: index for index, story in enumerate(stories)}
    edges = {
        story.story_id: [
            parent for parent in story.depends_on if parent in position
        ]
        for story in stories
    }
    dropped: list = []
    # Depth-first search over the "depends on" graph. ``open_path`` is the current
    # recursion stack: an edge reaching a node already on it closes a cycle.
    settled: set = set()
    open_path: list = []

    def walk(node: str) -> None:
        open_path.append(node)
        for parent in list(edges.get(node, ())):
            if parent in open_path:
                edges[node].remove(parent)
                dropped.append("{} → {}".format(node, parent))
            elif parent not in settled:
                walk(parent)
        open_path.pop()
        settled.add(node)

    for story in stories:
        if story.story_id not in settled:
            walk(story.story_id)

    repaired = [
        replace(story, depends_on=tuple(edges.get(story.story_id, ())))
        if tuple(edges.get(story.story_id, ())) != tuple(story.depends_on)
        else story
        for story in stories
    ]
    return repaired, tuple(dropped)


def assemble_implementation_plan(
    payload: dict, prd_data: PRDData, architecture: ArchitectureData
) -> ImplementationPlan:
    """
    A validated implementation plan from a model proposal. Every repair is in ``notes``.

    Deterministic: given the same proposal, the same PRD and the same architecture this
    returns the same plan, with no second model call to correct the first.
    """
    feature_lookup = _lookup(prd_feature_ids(prd_data))
    component_lookup = _lookup(architecture_component_ids(architecture))
    notes: list = []
    unknown_features: list = []
    unknown_components: list = []
    coerced_priorities: list = []

    def features_of(row: dict) -> tuple:
        """PRD ids this row names, in PRD spelling; unknown ones recorded and dropped."""
        accepted: list = []
        for candidate in strings(first(row, "feature_ids", "features", "prd_feature_ids")):
            resolved = feature_lookup.get(normalise_id(candidate))
            if resolved is None:
                if candidate not in unknown_features:
                    unknown_features.append(candidate)
            elif resolved not in accepted:
                accepted.append(resolved)
        return tuple(accepted)

    def components_of(row: dict) -> tuple:
        """Architecture ids this row names; unknown ones recorded and dropped."""
        accepted: list = []
        for candidate in strings(
            first(row, "component_ids", "components", "architecture_components")
        ):
            resolved = component_lookup.get(normalise_id(candidate))
            if resolved is None:
                if candidate not in unknown_components:
                    unknown_components.append(candidate)
            elif resolved not in accepted:
                accepted.append(resolved)
        return tuple(accepted)

    def priority_of(row: dict) -> str:
        """A priority from the vocabulary; anything else recorded and defaulted."""
        raw = first(row, "priority", "importance", "severity")
        resolved = normalise_priority(raw)
        if resolved is None:
            spoken = line(raw)
            if spoken and spoken not in coerced_priorities:
                coerced_priorities.append(spoken)
            return DEFAULT_PRIORITY
        return resolved

    # --- Epics. The model's own key is remembered so a story can point at an epic by the
    # key it used, while the plan stores ids this module minted.
    epics: list = []
    epic_by_key: dict = {}
    capped_epics = 0
    for row in _rows(payload, "epics", "epic_list", "capabilities"):
        name = excerpt(first(row, "name", "title", "epic", "summary"), MAX_TITLE_LENGTH)
        if not name:
            continue
        if len(epics) >= MAX_EPICS:
            capped_epics += 1
            continue
        epic = Epic(
            epic_id="E-{}".format(len(epics) + 1),
            name=name,
            goal=excerpt(first(row, "goal", "outcome", "objective", "description"), MAX_SUMMARY_LENGTH),
            feature_ids=features_of(row),
            component_ids=components_of(row),
            priority=priority_of(row),
        )
        epics.append(epic)
        for key in (first(row, "key", "id", "epic_id"), name):
            spoken = normalise_id(key)
            if spoken:
                epic_by_key.setdefault(spoken, epic.epic_id)

    # --- Stories.
    stories: list = []
    story_by_key: dict = {}
    dropped_untraceable: list = []
    unknown_epic_refs: list = []
    capped_stories = 0
    capped_task_stories: list = []
    missing_criteria: list = []
    missing_tasks: list = []
    # Dependencies are resolved in a second pass: a story may depend on one declared after
    # it, and a forward reference is not a mistake.
    pending_dependencies: list = []

    for row in _rows(payload, "stories", "user_stories", "work_items", "items"):
        title = excerpt(first(row, "title", "name", "summary", "story"), MAX_TITLE_LENGTH)
        user_story = excerpt(
            first(row, "user_story", "narrative", "as_a", "description"), MAX_SUMMARY_LENGTH
        )
        if not (title or user_story):
            continue
        if len(stories) >= MAX_STORIES:
            capped_stories += 1
            continue

        served = features_of(row)
        if not served:
            # A story delivering no approved feature is invented scope. Named in the notes
            # so a reviewer can re-add it as a task if it was real technical work.
            dropped_untraceable.append(title or user_story)
            continue

        epic_reference = first(row, "epic", "epic_key", "epic_id", "parent")
        epic_id = epic_by_key.get(normalise_id(epic_reference), "")
        if epic_reference and not epic_id:
            spoken = line(epic_reference)
            if spoken not in unknown_epic_refs:
                unknown_epic_refs.append(spoken)

        story_id = "S-{}".format(len(stories) + 1)
        tasks: list = []
        dropped_tasks = 0
        for task_row in _rows(row, "tasks", "technical_tasks", "subtasks", "work"):
            task_title = excerpt(
                first(task_row, "title", "name", "task", "summary"), MAX_TITLE_LENGTH
            )
            if not task_title:
                continue
            if len(tasks) >= MAX_TASKS_PER_STORY:
                dropped_tasks += 1
                continue
            tasks.append(
                TechnicalTask(
                    task_id="{}-T-{}".format(story_id, len(tasks) + 1),
                    title=task_title,
                    work_type=_work_type(first(task_row, "work_type", "type", "level")),
                    detail=excerpt(
                        first(task_row, "detail", "description", "notes", "implementation"),
                        MAX_SUMMARY_LENGTH,
                    ),
                    component_ids=components_of(task_row),
                    depends_on=_capped(first(task_row, "depends_on", "dependencies", "after")),
                    estimate=excerpt(
                        first(task_row, "estimate", "size", "points", "effort"), 40
                    ),
                )
            )
        if dropped_tasks:
            capped_task_stories.append(story_id)

        criteria = _capped(
            first(row, "acceptance_criteria", "acceptance", "criteria", "done_when")
        )
        if not criteria:
            missing_criteria.append(story_id)
        if not tasks:
            missing_tasks.append(story_id)

        story = Story(
            story_id=story_id,
            title=title or excerpt(user_story, MAX_TITLE_LENGTH),
            epic_id=epic_id,
            user_story=user_story,
            acceptance_criteria=criteria,
            feature_ids=served,
            component_ids=components_of(row),
            priority=priority_of(row),
            tasks=tuple(tasks),
            test_expectations=_capped(
                first(row, "test_expectations", "tests", "testing", "verification")
            ),
            estimate=excerpt(first(row, "estimate", "size", "points", "effort"), 40),
        )
        stories.append(story)
        pending_dependencies.append(strings(first(row, "depends_on", "dependencies", "after")))
        for key in (first(row, "key", "id", "story_id"), title):
            spoken = normalise_id(key)
            if spoken:
                story_by_key.setdefault(spoken, story_id)

    # --- Dependencies, now that every story id is known.
    unresolved_dependencies: list = []
    self_dependencies = 0
    for index, declared in enumerate(pending_dependencies):
        story = stories[index]
        accepted: list = []
        for candidate in declared:
            resolved = story_by_key.get(normalise_id(candidate))
            if resolved is None:
                spoken = line(candidate)
                if spoken and spoken not in unresolved_dependencies:
                    unresolved_dependencies.append(spoken)
            elif resolved == story.story_id:
                self_dependencies += 1
            elif resolved not in accepted:
                accepted.append(resolved)
        if tuple(accepted) != tuple(story.depends_on):
            stories[index] = replace(story, depends_on=tuple(accepted))

    stories, cyclic_edges = break_dependency_cycles(stories)

    # --- An epic that named no feature inherits the coverage of its own stories. A
    # grouping is not an invention, so this is filled in rather than reported as a gap.
    for index, epic in enumerate(epics):
        if epic.feature_ids:
            continue
        inherited: list = []
        for story in stories:
            if story.epic_id != epic.epic_id:
                continue
            for identifier in story.feature_ids:
                if identifier not in inherited:
                    inherited.append(identifier)
        if inherited:
            epics[index] = replace(epic, feature_ids=tuple(inherited))

    # --- Notes. Everything above that changed the proposal is reported here.
    if capped_epics:
        notes.append(
            "The AI implementation planner proposed more than {} epics; {} were not "
            "kept.".format(MAX_EPICS, capped_epics)
        )
    if capped_stories:
        notes.append(
            "The AI implementation planner proposed more than {} stories; {} were not "
            "kept.".format(MAX_STORIES, capped_stories)
        )
    if capped_task_stories:
        notes.append(
            "{} story/stories proposed more than {} technical tasks; the rest were not "
            "kept: {}.".format(
                len(capped_task_stories), MAX_TASKS_PER_STORY, _sample(capped_task_stories)
            )
        )
    if dropped_untraceable:
        notes.append(
            "{} story/stories named no PRD feature and were dropped as invented scope. If "
            "any was real technical work, add it as a task under the story it enables: "
            "{}.".format(len(dropped_untraceable), _sample(dropped_untraceable))
        )
    if unknown_features:
        notes.append(
            "{} feature id(s) the AI implementation planner referenced are not in the PRD "
            "and were removed: {}.".format(len(unknown_features), _sample(unknown_features))
        )
    if unknown_components:
        notes.append(
            "{} component id(s) the AI implementation planner referenced are not in the "
            "architecture and were removed: {}.".format(
                len(unknown_components), _sample(unknown_components)
            )
        )
    if unknown_epic_refs:
        notes.append(
            "{} story/stories named an epic that is not in the plan and were left "
            "ungrouped: {}.".format(len(unknown_epic_refs), _sample(unknown_epic_refs))
        )
    if unresolved_dependencies:
        notes.append(
            "{} dependency reference(s) matched no story in the plan and were removed: "
            "{}.".format(len(unresolved_dependencies), _sample(unresolved_dependencies))
        )
    if self_dependencies:
        notes.append(
            "{} story/stories depended on themselves; those dependencies were "
            "removed.".format(self_dependencies)
        )
    if cyclic_edges:
        notes.append(
            "{} dependency/dependencies formed a cycle, which would leave the work "
            "unstartable, and the closing link was removed: {}. Check the order is still "
            "what you intend.".format(len(cyclic_edges), _sample(cyclic_edges))
        )
    if coerced_priorities:
        notes.append(
            "{} priority value(s) were not one of {} and were set to {}: {}.".format(
                len(coerced_priorities),
                "/".join(("Highest", "High", "Medium", "Low", "Lowest")),
                DEFAULT_PRIORITY,
                _sample(coerced_priorities),
            )
        )
    if missing_criteria:
        notes.append(
            "{} story/stories carry no acceptance criteria and cannot be verified as "
            "written: {}. Add criteria before approving.".format(
                len(missing_criteria), _sample(missing_criteria)
            )
        )
    if missing_tasks:
        notes.append(
            "{} story/stories carry no technical task, so nothing says what to build: "
            "{}.".format(len(missing_tasks), _sample(missing_tasks))
        )

    plan = ImplementationPlan(
        project_title=line(getattr(prd_data, "project_title", "")) or "Untitled Project",
        overview=excerpt(
            first(payload, "overview", "summary", "sequencing", "approach"),
            MAX_OVERVIEW_LENGTH,
        ),
        epics=tuple(epics),
        stories=tuple(stories),
        source_prd_title=line(getattr(prd_data, "project_title", "")),
        source_architecture_title=line(getattr(architecture, "project_title", "")),
        source_feature_ids=prd_feature_ids(prd_data),
        source_component_ids=architecture_component_ids(architecture),
    )

    if plan.stories:
        if plan.orphan_stories:
            notes.append(
                "{} story/stories sit under no epic: {}. Either group them or record why "
                "they stand alone.".format(
                    len(plan.orphan_stories),
                    _sample(story.story_id for story in plan.orphan_stories),
                )
            )
        if plan.uncovered_feature_ids:
            notes.append(
                "{} PRD feature(s) are not delivered by any story: {}. Either that is "
                "deliberate, or the plan is incomplete.".format(
                    len(plan.uncovered_feature_ids), _sample(plan.uncovered_feature_ids)
                )
            )
        if plan.uncovered_component_ids:
            notes.append(
                "{} architecture component(s) are not built by any story or task: {}. The "
                "design promises them, so either plan the work or revisit the "
                "design.".format(
                    len(plan.uncovered_component_ids), _sample(plan.uncovered_component_ids)
                )
            )
    else:
        notes.append(
            "The AI implementation planner's response held no usable story."
        )

    return replace(plan, notes=tuple(notes))


def _realising_ids(components, feature_id: str) -> tuple:
    """The ids of every component that realises one PRD feature, in architecture order."""
    identifiers: list = []
    for component in components:
        if feature_id not in (getattr(component, "feature_ids", ()) or ()):
            continue
        identifier = line(getattr(component, "component_id", ""))
        if identifier and identifier not in identifiers:
            identifiers.append(identifier)
    return tuple(identifiers)


def build_baseline_implementation_plan(
    prd_data: PRDData, architecture: ArchitectureData
) -> ImplementationPlan:
    """
    A plan derived by deterministic mapping: one epic and one story per PRD feature, and
    one task per architecture component that realises it.

    This is the fallback, and it is deliberately shallow -- it proposes no sequencing, no
    dependency and no estimate, because none of those can be derived without judgement. It
    exists so a reviewer with no model available still gets a traceable starting point
    rather than an empty screen, and its acceptance criteria are the PRD's own rather than
    invented ones.
    """
    features = tuple(getattr(prd_data, "features", ()) or ())[:MAX_BASELINE_FEATURES]
    components = tuple(getattr(architecture, "components", ()) or ())

    epics: list = []
    stories: list = []
    for feature in features:
        identifier = line(getattr(feature, "feature_id", ""))
        title = excerpt(getattr(feature, "name", ""), MAX_TITLE_LENGTH) or identifier
        if not identifier or not title:
            continue

        epic = Epic(
            epic_id="E-{}".format(len(epics) + 1),
            name=title,
            goal=excerpt(getattr(feature, "summary", ""), MAX_SUMMARY_LENGTH),
            feature_ids=(identifier,),
            component_ids=_realising_ids(components, identifier),
            priority=DEFAULT_PRIORITY,
        )
        epics.append(epic)

        story_id = "S-{}".format(len(stories) + 1)
        realising = tuple(
            component
            for component in components
            if identifier in (getattr(component, "feature_ids", ()) or ())
            and line(getattr(component, "component_id", ""))
        )
        tasks = tuple(
            TechnicalTask(
                task_id="{}-T-{}".format(story_id, position),
                title="Implement {} in {}".format(
                    title, line(getattr(component, "name", ""))
                ),
                work_type=TASK,
                detail=excerpt(getattr(component, "responsibility", ""), MAX_SUMMARY_LENGTH),
                component_ids=(line(getattr(component, "component_id", "")),),
            )
            for position, component in enumerate(realising, start=1)
        )
        stories.append(
            Story(
                story_id=story_id,
                title=title,
                epic_id=epic.epic_id,
                user_story=excerpt(getattr(feature, "summary", ""), MAX_SUMMARY_LENGTH),
                # The PRD's own criteria, not generated ones: a fallback that invented
                # acceptance criteria would be asserting product behaviour nobody wrote.
                acceptance_criteria=_capped(
                    getattr(feature, "acceptance_criteria", ()) or ()
                ),
                feature_ids=(identifier,),
                component_ids=tuple(
                    line(getattr(component, "component_id", "")) for component in realising
                ),
                priority=DEFAULT_PRIORITY,
                tasks=tasks,
            )
        )

    return ImplementationPlan(
        project_title=line(getattr(prd_data, "project_title", "")) or "Untitled Project",
        overview=excerpt(getattr(architecture, "overview", ""), MAX_OVERVIEW_LENGTH),
        epics=tuple(epics),
        stories=tuple(stories),
        source_prd_title=line(getattr(prd_data, "project_title", "")),
        source_architecture_title=line(getattr(architecture, "project_title", "")),
        source_feature_ids=prd_feature_ids(prd_data),
        source_component_ids=architecture_component_ids(architecture),
        is_baseline=True,
    )


def _with_note(plan: ImplementationPlan, note: str) -> ImplementationPlan:
    """The plan with ``note`` first, because it explains everything after it."""
    return replace(plan, notes=(note,) + tuple(plan.notes))


def generate_implementation_plan(
    prd_data: Optional[PRDData],
    architecture: Optional[ArchitectureData] = None,
    generate=None,
) -> ImplementationPlan:
    """
    Propose an implementation plan for one approved PRD and architecture.

    Approves nothing and writes nothing -- in particular, nothing reaches a tracker here.

    ``generate`` is a callable taking the prompt and returning the model's response text.
    Injected rather than built here so this module holds no client, no credential and no
    import of ``main``; passing ``None`` -- which is what happens when no API key is
    configured -- falls back to ``build_baseline_implementation_plan``.

    With no PRD features or no architecture components this returns an empty plan whose
    note says which input is missing. That is the safe answer: planning without a product
    definition invents scope, and planning without a design invents a system.
    """
    if prd_data is None or not prd_feature_ids(prd_data):
        return ImplementationPlan(
            notes=(
                "An implementation plan needs an approved PRD holding at least one "
                "feature. Generate and approve a PRD first.",
            )
        )
    if architecture is None or not architecture_component_ids(architecture):
        return ImplementationPlan(
            notes=(
                "An implementation plan needs an approved architecture holding at least "
                "one component. Generate and approve an architecture first.",
            )
        )

    baseline = lambda: build_baseline_implementation_plan(prd_data, architecture)  # noqa: E731

    if generate is None:
        return _with_note(
            baseline(),
            "The AI implementation planner was not available, so this plan maps each PRD "
            "feature to one epic and story and each realising component to one task, "
            "rather than sequencing the build. Configure GEMINI_API_KEY to have a full "
            "implementation plan generated.",
        )

    try:
        raw = generate(build_implementation_plan_prompt(prd_data, architecture))
    except Exception as error:  # noqa: BLE001 - any client failure means the same thing
        # The exception type only. A client's message can carry request context that has no
        # business in a plan note.
        return _with_note(
            baseline(),
            "The AI implementation planner could not be reached ({}), so this plan maps "
            "each PRD feature to one epic and story instead.".format(type(error).__name__),
        )

    try:
        payload = parse_implementation_plan_response(raw)
    except ImplementationPlanResponseError as error:
        return _with_note(
            baseline(),
            "{} This plan maps each PRD feature to one epic and story instead.".format(error),
        )

    plan = assemble_implementation_plan(payload, prd_data, architecture)
    if plan.is_empty:
        fallback = baseline()
        return replace(fallback, notes=tuple(plan.notes) + tuple(fallback.notes))
    return plan
