"""
The project delivery lifecycle: which artifact exists, and how far it has been reviewed.

The product is moving from "generate one BRD" to a staged delivery lifecycle:

    Discovery -> BRD -> PRD -> Architecture -> Implementation Plan -> Sprint Planning
    -> Test Cases -> Test Execution -> Jira / Delivery Status

This module holds only the state of that progression. It deliberately does *not*
define a PRD, an architecture or a test case: those generators do not exist yet, and a
placeholder shape for an artifact nobody can produce would be a guess that the real
implementation would have to undo. Until a stage has a generator it carries a
``StageState`` and nothing more.

Nothing here duplicates an existing model. The BRD is the existing
``brd_models.BRDData``; the Jira work plan and creation results stay in
``jira_models`` and are read, not copied, when the delivery stage's status is derived.
"""

from dataclasses import dataclass, field
from typing import Optional

from brd_models import BRDData

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
IMPLEMENTED_STAGES = (DISCOVERY_BRD, DELIVERY_STATUS)

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
    part of the artifact's provenance and the transcript itself is not kept.
    """

    project_title: str = ""
    discovery_source: str = ""
    brd: Optional[BRDData] = None
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


def lifecycle_from(
    brd: Optional[BRDData] = None,
    discovery_source: str = "",
    plan=None,
    created=(),
) -> ProjectLifecycle:
    """
    Derive the current lifecycle from the artifacts this session actually holds.

    Read-only: every status comes from something that exists, so no stage can report
    progress that was not made. A BRD reaches ``Pending Review`` and not ``Approved``
    because this app has no BRD approval control yet -- claiming approval would be the
    silent state change the product direction forbids.

    ``plan`` is a ``JiraWorkPlan`` and ``created`` the ``CreatedIssue`` results of a
    creation run, both read positionally so this module stays independent of Jira.
    """
    lifecycle = ProjectLifecycle(
        project_title=(brd.project_title if brd is not None else ""),
        discovery_source=str(discovery_source or ""),
        brd=brd,
    )

    if brd is None:
        lifecycle.record(
            DISCOVERY_BRD,
            NOT_STARTED,
            "No transcript has been converted into a BRD in this session yet.",
        )
    else:
        lifecycle.record(
            DISCOVERY_BRD,
            PENDING_REVIEW,
            "{} functional and {} non-functional requirement(s) generated{}. "
            "Review and export it above.".format(
                len(brd.functional_requirements),
                len(brd.non_functional_requirements),
                " from {}".format(discovery_source) if discovery_source else "",
            ),
        )

    lifecycle.record(
        PRD,
        NOT_STARTED,
        "Will be generated from the approved BRD, with an optional product-refinement "
        "transcript. " + NOT_IMPLEMENTED_DETAIL,
    )
    for stage in (ARCHITECTURE, IMPLEMENTATION_PLAN, SPRINT_PLAN, TEST_CASES, TEST_EXECUTION):
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
