from dataclasses import dataclass, field
from typing import Optional


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
class BRDData:
    functional_requirements: list[Requirement] = field(default_factory=list)
    non_functional_requirements: list[Requirement] = field(default_factory=list)
    business_objectives: list[str] = field(default_factory=list)
    stakeholders: list[Stakeholder] = field(default_factory=list)
    assumptions: list[Assumption] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    deferred_or_out_of_scope_items: list[str] = field(default_factory=list)
    background_information: list[str] = field(default_factory=list)
    project_overview: ProjectOverview = field(default_factory=ProjectOverview)
    project_title: str = "Untitled Project"