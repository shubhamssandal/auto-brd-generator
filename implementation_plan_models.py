"""
The implementation plan artifact: the engineering structure that builds an approved design.

The PRD says what the product does. The architecture says what the system is made of. The
implementation plan says *what gets built, in what order, by whom, and how anyone knows it
is done*. It is the last artifact before delivery tracking, and the first one shaped like
work rather than like a document.

The hierarchy is Epic -> Story -> Task/Subtask, and the three levels are not
interchangeable:

- An **epic** is a business or product capability. It groups stories; it is not itself
  executable work.
- A **story** is a user or product outcome, with acceptance criteria that decide whether
  it was achieved.
- A **task** or **subtask** is technical implementation work. Technical work is *not*
  forced into a user story: a database migration, a CI pipeline or an SDK upgrade is a
  task, and saying "as a user I want a migration" would be a lie about who wants it.

Traceability runs one way and reuses the identifiers already minted upstream: a story
names ``prd_models.Feature.feature_id`` values, and a story or task names
``architecture_models.Component.component_id`` values. No new identifier scheme crosses a
stage boundary, so any task can be read back to the requirement it exists for.

This module is deliberately **not** Jira-shaped. ``jira_models.PlannedIssue`` already
exists and is bound to a specific Jira project's issue types, hierarchy levels and
required fields -- it cannot be built before a project is selected. The implementation
plan is a lifecycle artifact that exists before any tracker is chosen, so the two are
separate on purpose and the delivery stage maps one onto the other. Adding Jira fields
here would make the plan un-generatable without a Jira connection, and would tie the
product's engineering structure to one vendor.

Nothing here is domain-specific. An epic, a story, an acceptance criterion and a
technical task mean the same thing for a payments product as for a streaming one; the
domain arrives entirely through the PRD and architecture that are read.
"""

from dataclasses import dataclass
from typing import Optional

# --- Work types -----------------------------------------------------------

EPIC = "epic"
STORY = "story"
TASK = "task"
SUBTASK = "subtask"

WORK_TYPES = (EPIC, STORY, TASK, SUBTASK)
WORK_TYPE_LABEL = {
    EPIC: "Epic",
    STORY: "Story",
    TASK: "Task",
    SUBTASK: "Subtask",
}

# The two types a technical work item may hold. An epic or a story is a different level of
# the hierarchy and is represented by its own class, so it cannot be set here by mistake.
TECHNICAL_WORK_TYPES = (TASK, SUBTASK)

# --- Priority -------------------------------------------------------------

# Five levels, matching the vocabulary trackers overwhelmingly default to, so the delivery
# stage can map a priority across without inventing a translation table. A value outside
# the vocabulary is normalised rather than stored, because a priority the rest of the app
# cannot order is worse than an unremarkable default.
PRIORITIES = ("Highest", "High", "Medium", "Low", "Lowest")
DEFAULT_PRIORITY = "Medium"

_PRIORITY_LOOKUP = {value.lower(): value for value in PRIORITIES}
_PRIORITY_SYNONYMS = {
    "critical": "Highest",
    "blocker": "Highest",
    "p0": "Highest",
    "must": "High",
    "must have": "High",
    "p1": "High",
    "should": "Medium",
    "should have": "Medium",
    "normal": "Medium",
    "moderate": "Medium",
    "p2": "Medium",
    "could": "Low",
    "could have": "Low",
    "nice to have": "Low",
    "minor": "Low",
    "p3": "Low",
    "trivial": "Lowest",
    "wont have": "Lowest",
    "p4": "Lowest",
}


def normalise_priority(value) -> Optional[str]:
    """
    ``value`` as one of ``PRIORITIES``, or ``None`` when it names none of them.

    ``None`` rather than the default, so a caller can tell "the model said nothing" from
    "the model said something unusable" and record the difference for a reviewer.
    """
    spoken = " ".join(str(value if value is not None else "").split()).strip(" .!").lower()
    if not spoken:
        return None
    if spoken in _PRIORITY_LOOKUP:
        return _PRIORITY_LOOKUP[spoken]
    return _PRIORITY_SYNONYMS.get(spoken)


# --- Work items -----------------------------------------------------------


@dataclass
class TechnicalTask:
    """
    One unit of technical implementation work under a story.

    ``component_ids`` are architecture component ids: the parts of the system this task
    changes. A task naming none is *kept* -- repository setup, a dependency upgrade or a
    release step genuinely touches no single component -- for the same reason a
    cross-cutting architecture component naming no feature is kept.

    ``work_type`` distinguishes a task from a subtask. Both are technical work; the
    difference is only depth, and a plan that has no use for the distinction can leave
    every item a ``TASK``.
    """

    task_id: str
    title: str
    work_type: str = TASK
    detail: str = ""
    component_ids: tuple = ()
    depends_on: tuple = ()
    estimate: str = ""

    @property
    def label(self) -> str:
        return WORK_TYPE_LABEL.get(self.work_type, WORK_TYPE_LABEL[TASK])


@dataclass
class Story:
    """
    One user or product outcome, and the technical work that delivers it.

    This is the unit a coding agent is eventually handed, so it carries everything that
    decision needs: what the outcome is (``user_story``), how anyone knows it was reached
    (``acceptance_criteria``), what to build (``tasks``), where in the system to build it
    (``component_ids``), what must exist first (``depends_on``) and what proving it
    involves (``test_expectations``).

    ``feature_ids`` are PRD feature ids and are the only link back to the product
    definition. A story that names none is not an engineering decision, it is invented
    scope -- so unlike a task, an untraceable story does not survive assembly.
    """

    story_id: str
    title: str
    epic_id: str = ""
    user_story: str = ""
    acceptance_criteria: tuple = ()
    feature_ids: tuple = ()
    component_ids: tuple = ()
    priority: str = DEFAULT_PRIORITY
    depends_on: tuple = ()
    tasks: tuple = ()
    test_expectations: tuple = ()
    estimate: str = ""

    @property
    def is_traceable(self) -> bool:
        """True when this story names at least one PRD feature it delivers."""
        return bool(self.feature_ids)

    @property
    def readiness_gaps(self) -> tuple:
        """
        What is missing before this story could be handed to an implementer.

        Reported rather than repaired: the gaps are judgement calls a reviewer makes, and
        filling them in automatically would put words in the product owner's mouth. The
        list is the reason a story is not ready, in the order a reviewer would fix it.
        """
        gaps: list = []
        if not (self.user_story.strip() or self.title.strip()):
            gaps.append("no description")
        if not self.acceptance_criteria:
            gaps.append("no acceptance criteria")
        if not self.tasks:
            gaps.append("no technical tasks")
        if not self.component_ids:
            gaps.append("no architecture component")
        if not self.feature_ids:
            gaps.append("no PRD feature")
        return tuple(gaps)

    @property
    def is_ready(self) -> bool:
        """
        True when this story carries enough context to be implemented against.

        "Ready" is a statement about the *plan*, not a promise that the work is easy or
        that any implementer exists yet.
        """
        return not self.readiness_gaps

    @property
    def task_count(self) -> int:
        return len(self.tasks)


@dataclass
class Epic:
    """
    One business or product capability that groups stories.

    Not executable work: an epic is delivered by its stories and holds no acceptance
    criteria of its own. ``goal`` is the outcome the capability exists for, in business
    terms, so the grouping can be reviewed on its merits rather than as a folder name.
    """

    epic_id: str
    name: str
    goal: str = ""
    feature_ids: tuple = ()
    component_ids: tuple = ()
    priority: str = DEFAULT_PRIORITY


@dataclass
class ImplementationPlan:
    """
    The engineering structure for one approved PRD and architecture.

    ``notes`` records every deterministic repair -- dropped untraceable stories, unknown
    feature or component ids, broken dependency cycles, applied caps -- so a reviewer
    reads what was changed instead of trusting the result. ``source_prd_title``,
    ``source_feature_ids`` and ``source_component_ids`` are the provenance: which product
    definition and design this plan was allowed to trace to.
    """

    project_title: str = ""
    overview: str = ""
    epics: tuple = ()
    stories: tuple = ()
    notes: tuple = ()
    source_prd_title: str = ""
    source_architecture_title: str = ""
    source_feature_ids: tuple = ()
    source_component_ids: tuple = ()
    # True when this is the deterministic per-feature skeleton rather than a planned
    # decomposition. Recorded rather than inferred from the notes, because the lifecycle
    # reports a fallback as a draft and a real plan as pending review.
    is_baseline: bool = False

    @property
    def is_empty(self) -> bool:
        """A plan with no story is not a plan: an epic on its own builds nothing."""
        return not self.stories

    def stories_for(self, epic_id: str) -> tuple:
        """Every story under one epic, in plan order."""
        return tuple(story for story in self.stories if story.epic_id == epic_id)

    @property
    def orphan_stories(self) -> tuple:
        """
        Stories under no epic.

        Kept, not dropped: work that fits no capability is still work, and hiding it
        would remove it from the plan a reviewer is approving.
        """
        known = {epic.epic_id for epic in self.epics}
        return tuple(story for story in self.stories if story.epic_id not in known)

    @property
    def tasks(self) -> tuple:
        """Every technical task across every story, in plan order."""
        return tuple(task for story in self.stories for task in story.tasks)

    @property
    def task_count(self) -> int:
        return len(self.tasks)

    @property
    def unready_stories(self) -> tuple:
        """Stories that do not yet carry enough context to be implemented against."""
        return tuple(story for story in self.stories if not story.is_ready)

    @property
    def covered_feature_ids(self) -> tuple:
        """PRD feature ids at least one epic or story names."""
        covered: list = []
        for holder in tuple(self.epics) + tuple(self.stories):
            for identifier in getattr(holder, "feature_ids", ()) or ():
                if identifier not in covered:
                    covered.append(identifier)
        return tuple(covered)

    @property
    def uncovered_feature_ids(self) -> tuple:
        """PRD feature ids nothing in the plan builds. Reported, never hidden."""
        covered = set(self.covered_feature_ids)
        return tuple(
            identifier
            for identifier in self.source_feature_ids
            if identifier not in covered
        )

    @property
    def covered_component_ids(self) -> tuple:
        """Architecture component ids at least one epic, story or task names."""
        covered: list = []
        holders = tuple(self.epics) + tuple(self.stories) + self.tasks
        for holder in holders:
            for identifier in getattr(holder, "component_ids", ()) or ():
                if identifier not in covered:
                    covered.append(identifier)
        return tuple(covered)

    @property
    def uncovered_component_ids(self) -> tuple:
        """
        Architecture component ids no work item builds.

        Worth reporting on its own: an unbuilt component means the design promises
        something the plan does not deliver.
        """
        covered = set(self.covered_component_ids)
        return tuple(
            identifier
            for identifier in self.source_component_ids
            if identifier not in covered
        )

    @property
    def total_items(self) -> int:
        """Epics plus stories plus tasks: the size of what a reviewer is approving."""
        return len(self.epics) + len(self.stories) + self.task_count

    def story(self, story_id: str) -> Optional[Story]:
        """One story by id, or ``None``."""
        for candidate in self.stories:
            if candidate.story_id == story_id:
                return candidate
        return None

    @property
    def ordered_story_ids(self) -> tuple:
        """
        Story ids in dependency order: a story appears after everything it depends on.

        Assembly guarantees the dependency graph is acyclic, so this is a total order.
        It is a plain stable topological sort -- plan order is preserved wherever
        dependencies do not force otherwise -- which is what a later planning stage needs
        to choose a scope that never schedules a story before its prerequisite.
        """
        remaining = list(self.stories)
        known = {story.story_id for story in remaining}
        placed: list = []
        satisfied: set = set()
        # Tracked by id, not by story: two stories can compare equal by value, and
        # removing "every story equal to this one" would drop the wrong rows.
        while remaining:
            ready = [
                story
                for story in remaining
                if all(
                    parent in satisfied
                    for parent in story.depends_on
                    if parent in known
                )
            ]
            if not ready:
                # Unreachable for an assembled plan, which is acyclic. Falling back to plan
                # order keeps this a total order rather than silently losing stories.
                placed += [story.story_id for story in remaining]
                break
            ready_ids = {story.story_id for story in ready}
            for story in ready:
                placed.append(story.story_id)
            satisfied |= ready_ids
            remaining = [story for story in remaining if story.story_id not in ready_ids]
        return tuple(placed)


def architecture_component_ids(architecture) -> tuple:
    """
    Every component id the architecture offers, in architecture order.

    This is the set an implementation plan may trace to. Components with no id are
    skipped: there is nothing for a task to name. Read by ``getattr`` so this module does
    not depend on the architecture module, mirroring ``prd_models.brd_requirement_ids``
    and ``architecture_models.prd_feature_ids``.
    """
    if architecture is None:
        return ()
    identifiers: list = []
    for component in getattr(architecture, "components", ()) or ():
        identifier = " ".join(str(getattr(component, "component_id", "") or "").split())
        if identifier and identifier not in identifiers:
            identifiers.append(identifier)
    return tuple(identifiers)


def component_index(architecture) -> dict:
    """
    Component id -> the component, for prompts and review screens that show a name.

    A plan stores ids, because an id is what survives a round trip through session state
    and what the next stage traces on; a reviewer still needs the name beside it.
    """
    index: dict = {}
    for component in getattr(architecture, "components", ()) or ():
        identifier = " ".join(str(getattr(component, "component_id", "") or "").split())
        if identifier:
            index.setdefault(identifier, component)
    return index
