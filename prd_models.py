"""
The PRD artifact: what the product does, derived from an approved BRD.

The BRD says what the business needs and is the requirements source of truth. The PRD
says how the product behaves, and it is a *different* document, not a restatement: it
adds personas, features, journeys, behaviour, edge cases and acceptance criteria that
a BRD requirement on its own does not give you.

Traceability runs one way. Every feature and journey names the BRD requirement ids it
serves, using the ids already minted in ``brd_models.Requirement.requirement_id``, so
no new identifier scheme is introduced and the PRD can always be read back against
the BRD it came from. A feature naming no requirement is not a product decision, it
is an invention, and ``is_traceable`` is what the generator uses to drop it.
"""

from dataclasses import dataclass
from typing import Optional

from brd_models import BRDData


@dataclass
class Persona:
    """Who the product is for, as the BRD's stakeholders and requirements describe."""

    name: str
    description: str = ""
    needs: tuple = ()


@dataclass
class Feature:
    """
    One unit of product behaviour.

    ``feature_id`` is the PRD's own key, minted by the generator; it addresses this
    feature in the review UI and in later stages. ``requirement_ids`` are BRD ids and
    are the only link back to the requirements source of truth.
    """

    feature_id: str
    name: str
    summary: str = ""
    requirement_ids: tuple = ()
    behaviours: tuple = ()
    edge_cases: tuple = ()
    acceptance_criteria: tuple = ()

    @property
    def is_traceable(self) -> bool:
        """True when this feature names at least one BRD requirement it serves."""
        return bool(self.requirement_ids)


@dataclass
class UserJourney:
    """An ordered path a persona takes through the features."""

    name: str
    persona: str = ""
    steps: tuple = ()
    requirement_ids: tuple = ()


@dataclass
class PRDData:
    """
    A product requirements document for one approved BRD.

    ``notes`` records every deterministic repair made to a model's proposal -- dropped
    features, unknown requirement ids, applied caps -- so a reviewer reads what was
    changed instead of trusting the result. ``source_brd_title`` and
    ``refinement_source`` are the provenance: which BRD this came from, and whether an
    optional product-refinement transcript contributed.
    """

    project_title: str = ""
    overview: str = ""
    goals: tuple = ()
    personas: tuple = ()
    features: tuple = ()
    journeys: tuple = ()
    success_metrics: tuple = ()
    assumptions: tuple = ()
    open_questions: tuple = ()
    notes: tuple = ()
    source_brd_title: str = ""
    source_requirement_ids: tuple = ()
    refinement_source: str = ""
    # True when this PRD is the deterministic one-feature-per-requirement fallback
    # rather than a generated product definition. Recorded rather than inferred from
    # the notes, because the lifecycle reports the two differently.
    is_baseline: bool = False

    @property
    def is_empty(self) -> bool:
        """A PRD with no feature is not a PRD, whatever else it carries."""
        return not self.features

    @property
    def covered_requirement_ids(self) -> tuple:
        """BRD requirement ids that at least one feature or journey serves."""
        covered: list = []
        for holder in tuple(self.features) + tuple(self.journeys):
            for identifier in getattr(holder, "requirement_ids", ()) or ():
                if identifier not in covered:
                    covered.append(identifier)
        return tuple(covered)

    @property
    def uncovered_requirement_ids(self) -> tuple:
        """BRD requirement ids no feature or journey mentions. Reported, never hidden."""
        covered = set(self.covered_requirement_ids)
        return tuple(
            identifier
            for identifier in self.source_requirement_ids
            if identifier not in covered
        )


def brd_requirement_ids(brd_data: Optional[BRDData]) -> tuple:
    """
    Every requirement id the BRD offers, functional first, in BRD order.

    This is the set a PRD may trace to. Requirements with no id are skipped: there is
    nothing for a feature to name.
    """
    if brd_data is None:
        return ()
    identifiers: list = []
    for requirement in tuple(getattr(brd_data, "functional_requirements", ()) or ()) + tuple(
        getattr(brd_data, "non_functional_requirements", ()) or ()
    ):
        identifier = " ".join(str(getattr(requirement, "requirement_id", "") or "").split())
        if identifier and identifier not in identifiers:
            identifiers.append(identifier)
    return tuple(identifiers)
