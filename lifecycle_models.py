"""
The project delivery lifecycle: which artifact exists, and how far it has been reviewed.

The product is moving from "generate one BRD" to a staged delivery lifecycle:

    Discovery -> BRD -> PRD -> Architecture -> Implementation Plan -> Sprint Planning
    -> Test Cases -> Test Execution -> Jira / Delivery Status

This module holds only the state of that progression. It deliberately does *not*
define a test case or a sprint: those generators do not exist yet, and a placeholder
shape for an artifact nobody can produce would be a guess that the real implementation
would have to undo. Until a stage has a generator it carries a ``StageState`` and
nothing more.

Nothing here duplicates an existing model. The BRD is the existing
``brd_models.BRDData``, the PRD the existing ``prd_models.PRDData``, the architecture the
existing ``architecture_models.ArchitectureData``, the implementation plan the existing
``implementation_plan_models.ImplementationPlan``; the Jira work plan and creation
results stay in ``jira_models`` and are read, not copied, when the delivery stage's
status is derived.
"""

from dataclasses import dataclass, field
from typing import Optional

from architecture_models import ArchitectureData
from brd_models import BRDData
from implementation_plan_models import ImplementationPlan
from prd_models import PRDData

# --- Stages ---------------------------------------------------------------

DISCOVERY_BRD = "discovery_brd"
PRD = "prd"
ARCHITECTURE = "architecture"
IMPLEMENTATION_PLAN = "implementation_plan"
SPRINT_PLAN = "sprint_plan"
TEST_CASES = "test_cases"
TEST_EXECUTION = "test_execution"
DELIVERY_STATUS = "delivery_status"

# Ordered, and the order is the lifecycle: a stage's inputs are the stages before it.
STAGE_LABELS = (
    (DISCOVERY_BRD, "Discovery → BRD"),
    (PRD, "Product Definition → PRD"),
    (ARCHITECTURE, "Architecture"),
    (IMPLEMENTATION_PLAN, "Implementation Plan"),
    (SPRINT_PLAN, "Sprint Planning"),
    (TEST_CASES, "Test Cases"),
    (TEST_EXECUTION, "Test Execution"),
    (DELIVERY_STATUS, "Jira / Delivery Status"),
)
LIFECYCLE_STAGES = tuple(stage for stage, _ in STAGE_LABELS)
STAGE_LABEL = dict(STAGE_LABELS)

# The stages that have a working implementation today. Everything else is navigable so
# the shape of the product is visible, and is reported as not implemented rather than
# given a control that would do nothing.
IMPLEMENTED_STAGES = (
    DISCOVERY_BRD,
    PRD,
    ARCHITECTURE,
    IMPLEMENTATION_PLAN,
    DELIVERY_STATUS,
)

# --- Stage state ----------------------------------------------------------

NOT_STARTED = "Not Started"
DRAFT = "Draft"
PENDING_REVIEW = "Pending Review"
APPROVED = "Approved"
IN_PROGRESS = "In Progress"
COMPLETED = "Completed"

STAGE_STATES = (NOT_STARTED, DRAFT, PENDING_REVIEW, APPROVED, IN_PROGRESS, COMPLETED)

# What a stage with no generator yet reports, so the UI never has to invent wording.
NOT_IMPLEMENTED_DETAIL = "Planned for a later phase — not implemented yet."


@dataclass(frozen=True)
class StageState:
    """One stage's status, and the one line that explains why it holds that status."""

    status: str = NOT_STARTED
    detail: str = ""

    def __post_init__(self) -> None:
        # A status outside the vocabulary would render as a state the rest of the app
        # cannot reason about, so it is refused where it is set rather than displayed.
        if self.status not in STAGE_STATES:
            raise ValueError(
                "Unknown lifecycle status {!r}. Expected one of: {}.".format(
                    self.status, ", ".join(STAGE_STATES)
                )
            )


@dataclass
class ProjectLifecycle:
    """
    One project's progress through the lifecycle.

    ``brd`` is the artifact that exists today, held by reference. ``discovery_source``
    records which ingestion route produced it, because "where did this come from" is
    part of the artifact's provenance and the transcript itself is not kept. ``prd`` is
    the product definition derived from an approved ``brd``, ``architecture`` the
    technical design derived from an approved ``prd``, and ``implementation_plan`` the
    engineering structure derived from both.
    """

    project_title: str = ""
    discovery_source: str = ""
    brd: Optional[BRDData] = None
    prd: Optional[PRDData] = None
    architecture: Optional[ArchitectureData] = None
    implementation_plan: Optional[ImplementationPlan] = None
    stages: dict = field(default_factory=dict)

    def state(self, stage: str) -> StageState:
        """This stage's state, defaulting to not started."""
        return self.stages.get(stage, StageState())

    def record(self, stage: str, status: str, detail: str = "") -> None:
        """Set one stage's state. Raises on an unknown stage or status."""
        if stage not in LIFECYCLE_STAGES:
            raise ValueError(
                "Unknown lifecycle stage {!r}. Expected one of: {}.".format(
                    stage, ", ".join(LIFECYCLE_STAGES)
                )
            )
        self.stages[stage] = StageState(status=status, detail=detail)

    @property
    def is_started(self) -> bool:
        return any(state.status != NOT_STARTED for state in self.stages.values())


def _prd_state(brd, brd_approved: bool, prd, prd_approved: bool):
    """
    The PRD stage's ``(status, detail)``.

    The gate is the *approved* BRD, not merely a generated one: the PRD stage says what
    the product does about requirements the business has signed off. Approval is only
    ever reported when the approval control set it.
    """
    if brd is None:
        return (
            NOT_STARTED,
            "Generate a BRD first. The PRD is derived from the approved BRD.",
        )
    if not brd_approved:
        return (
            NOT_STARTED,
            "The BRD is still pending review. Approve it to generate a PRD from it.",
        )
    if prd is None or getattr(prd, "is_empty", True):
        return (
            NOT_STARTED,
            "The approved BRD is ready. Generate a PRD from it, optionally with a "
            "product-refinement transcript.",
        )

    covered = len(getattr(prd, "covered_requirement_ids", ()) or ())
    total = len(getattr(prd, "source_requirement_ids", ()) or ())
    coverage = "{} feature(s) covering {} of {} BRD requirement(s)".format(
        len(getattr(prd, "features", ()) or ()), covered, total
    )
    if prd_approved:
        return (APPROVED, coverage + ". Approved.")
    if getattr(prd, "is_baseline", False):
        return (
            DRAFT,
            coverage + ". This is the deterministic fallback -- one feature per "
            "requirement, with no journeys or edge cases. Edit it, or generate again "
            "with the AI generator available.",
        )
    return (PENDING_REVIEW, coverage + ". Review, edit and approve it below.")


def _architecture_state(prd, prd_approved: bool, architecture, architecture_approved: bool):
    """
    The architecture stage's ``(status, detail)``.

    The gate is the *approved* PRD: an architecture designed against product behaviour
    nobody has signed off would have to be redesigned. As with the PRD, approval is only
    ever reported when the approval control set it.
    """
    if prd is None or getattr(prd, "is_empty", True):
        return (
            NOT_STARTED,
            "Generate a PRD first. The architecture is derived from the approved PRD.",
        )
    if not prd_approved:
        return (
            NOT_STARTED,
            "The PRD is still pending review. Approve it to generate an architecture "
            "from it.",
        )
    if architecture is None or getattr(architecture, "is_empty", True):
        return (
            NOT_STARTED,
            "The approved PRD is ready. Generate an architecture from it, optionally with "
            "an architecture discussion transcript.",
        )

    covered = len(getattr(architecture, "covered_feature_ids", ()) or ())
    total = len(getattr(architecture, "source_feature_ids", ()) or ())
    coverage = "{} component(s) realising {} of {} PRD feature(s)".format(
        len(getattr(architecture, "components", ()) or ()), covered, total
    )
    if architecture_approved:
        return (APPROVED, coverage + ". Approved.")
    if getattr(architecture, "is_baseline", False):
        return (
            DRAFT,
            coverage + ". This is the deterministic fallback -- one component per feature "
            "in each layer, with no decisions, flows or risks. Edit it, or generate again "
            "with the AI generator available.",
        )
    return (PENDING_REVIEW, coverage + ". Review, edit and approve it below.")


def _implementation_plan_state(
    architecture, architecture_approved: bool, plan, plan_approved: bool
):
    """
    The implementation plan stage's ``(status, detail)``.

    The gate is the *approved* architecture, which in turn carries the approved PRD and
    BRD behind it: work decomposed against a design nobody signed off would have to be
    re-decomposed. As with every stage above, approval is only ever reported when the
    approval control set it.
    """
    if architecture is None or getattr(architecture, "is_empty", True):
        return (
            NOT_STARTED,
            "Generate an architecture first. The implementation plan is derived from the "
            "approved PRD and architecture.",
        )
    if not architecture_approved:
        return (
            NOT_STARTED,
            "The architecture is still pending review. Approve it to generate an "
            "implementation plan from it.",
        )
    if plan is None or getattr(plan, "is_empty", True):
        return (
            NOT_STARTED,
            "The approved architecture is ready. Generate an implementation plan of "
            "epics, stories and technical tasks from it.",
        )

    coverage = (
        "{} epic(s), {} story/stories and {} task(s) covering {} of {} PRD feature(s)"
    ).format(
        len(getattr(plan, "epics", ()) or ()),
        len(getattr(plan, "stories", ()) or ()),
        getattr(plan, "task_count", 0),
        len(getattr(plan, "covered_feature_ids", ()) or ()),
        len(getattr(plan, "source_feature_ids", ()) or ()),
    )
    if plan_approved:
        return (APPROVED, coverage + ". Approved as the basis for delivery.")
    if getattr(plan, "is_baseline", False):
        return (
            DRAFT,
            coverage + ". This is the deterministic fallback -- one epic and story per "
            "feature, with no sequencing or estimates. Edit it, or generate again with "
            "the AI planner available.",
        )
    return (PENDING_REVIEW, coverage + ". Review, edit and approve it below.")


def lifecycle_from(
    brd: Optional[BRDData] = None,
    discovery_source: str = "",
    plan=None,
    created=(),
    brd_approved: bool = False,
    prd: Optional[PRDData] = None,
    prd_approved: bool = False,
    architecture: Optional[ArchitectureData] = None,
    architecture_approved: bool = False,
    implementation_plan: Optional[ImplementationPlan] = None,
    implementation_plan_approved: bool = False,
) -> ProjectLifecycle:
    """
    Derive the current lifecycle from the artifacts this session actually holds.

    Read-only: every status comes from something that exists, so no stage can report
    progress that was not made. A generated artifact reaches ``Pending Review`` and
    stops there; ``brd_approved``, ``prd_approved``, ``architecture_approved`` and
    ``implementation_plan_approved`` are set only by the explicit approval controls,
    because moving an artifact to ``Approved`` on its own is the silent state change the
    product direction forbids.

    ``plan`` is a ``JiraWorkPlan`` and ``created`` the ``CreatedIssue`` results of a
    creation run, both read positionally so this module stays independent of Jira. The
    lifecycle's own ``implementation_plan`` is a different artifact entirely: it is the
    engineering structure the delivery stage later turns into issues.
    """
    lifecycle = ProjectLifecycle(
        project_title=(brd.project_title if brd is not None else ""),
        discovery_source=str(discovery_source or ""),
        brd=brd,
        prd=prd,
        architecture=architecture,
        implementation_plan=implementation_plan,
    )

    if brd is None:
        lifecycle.record(
            DISCOVERY_BRD,
            NOT_STARTED,
            "No transcript has been converted into a BRD in this session yet.",
        )
    else:
        counts = "{} functional and {} non-functional requirement(s) generated{}.".format(
            len(brd.functional_requirements),
            len(brd.non_functional_requirements),
            " from {}".format(discovery_source) if discovery_source else "",
        )
        if brd_approved:
            lifecycle.record(
                DISCOVERY_BRD,
                APPROVED,
                counts + " Approved as the basis for the PRD.",
            )
        else:
            lifecycle.record(
                DISCOVERY_BRD, PENDING_REVIEW, counts + " Review and approve it below."
            )

    lifecycle.record(PRD, *_prd_state(brd, brd_approved, prd, prd_approved))
    # Revoking the BRD's approval unapproves everything derived from it: the PRD stage
    # returns to Not Started, so the architecture behind it cannot still read as ready.
    architecture_ready = prd_approved and brd_approved
    lifecycle.record(
        ARCHITECTURE,
        *_architecture_state(prd, architecture_ready, architecture, architecture_approved)
    )
    # The same cascade one stage further down: the plan's gate is an approved architecture
    # whose own upstream approvals still hold, so revoking any of them returns this stage
    # to Not Started rather than leaving work planned against an unapproved design.
    lifecycle.record(
        IMPLEMENTATION_PLAN,
        *_implementation_plan_state(
            architecture,
            architecture_approved and architecture_ready,
            implementation_plan,
            implementation_plan_approved,
        )
    )
    for stage in (SPRINT_PLAN, TEST_CASES, TEST_EXECUTION):
        lifecycle.record(stage, NOT_STARTED, NOT_IMPLEMENTED_DETAIL)

    succeeded = tuple(
        record for record in created or () if getattr(record, "succeeded", False)
    )
    if succeeded:
        lifecycle.record(
            DELIVERY_STATUS,
            IN_PROGRESS,
            "{} issue(s) created in Jira from the reviewed work plan.".format(len(succeeded)),
        )
    elif plan is not None and not getattr(plan, "is_empty", False):
        lifecycle.record(
            DELIVERY_STATUS,
            DRAFT,
            "A work plan of {} proposed issue(s) is awaiting review. Nothing has been "
            "created in Jira.".format(len(plan.issues)),
        )
    else:
        lifecycle.record(
            DELIVERY_STATUS,
            NOT_STARTED,
            "No Jira work plan has been generated in this session yet.",
        )

    return lifecycle
