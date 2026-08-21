from dataclasses import dataclass, field
from typing import Optional, Any



@dataclass
class Requirement:
    requirement_id: str
    statement: str
    source_evidence: str


@dataclass
class Stakeholder:
    name: str
    role: Optional[str] = None
    stated_interest: Optional[str] = None
    concern: Optional[str] = None
    requirement: Optional[str] = None
    responsibility: Optional[str] = None


@dataclass
class Assumption:
    statement: str
    confirmation_needed: str


@dataclass
class ProjectOverview:
    """High-level project summary information."""

    project_name: Optional[str] = None
    date: Optional[str] = None


@dataclass
class ActionItem:
    item: str
    owner: Optional[str] = None
    due_date: Optional[str] = None


@dataclass
class BRDData:
    functional_requirements: list[Requirement] = field(default_factory=list)
    non_functional_requirements: list[Requirement] = field(default_factory=list)
    business_objectives: list[str] = field(default_factory=list)
    stakeholders: list[Stakeholder] = field(default_factory=list)
    assumptions: list[Assumption] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    deferred_or_out_of_scope_items: list[str] = field(default_factory=list)
    background_information: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    key_decisions: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    action_items: list[ActionItem] = field(default_factory=list)
    project_overview: ProjectOverview = field(default_factory=ProjectOverview)
    project_title: str = "Untitled Project"



@dataclass
class NormalizedTranscript:
    """
    Standardized representation of a meeting transcript from any source.

    Every ingestion path (manual paste, .txt upload, Google Meet, Microsoft Teams)
    produces this one shape, which is the sole input to BRD generation. Fields the
    source did not actually supply stay None or empty; they are never inferred.
    """

    raw_text: str
    source: str = "manual"  # e.g., "manual", "upload", "google_meet", "microsoft_teams"
    meeting_title: Optional[str] = None
    meeting_date: Optional[str] = None
    participants: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    provider: Optional[str] = None  # provider adaptor name; None for local sources
    transcript_id: Optional[str] = None  # provider-side transcript/document identifier
