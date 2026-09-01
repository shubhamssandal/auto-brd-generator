"""
The architecture artifact: how the product is built, derived from an approved PRD.

The PRD says what the product does. The architecture says which components exist, what
each one owns, where the API boundaries sit, how data flows and what the technical risks
are. It is a translation, not a restatement: a component is named for the technical
responsibility it holds, and one component usually realises several PRD features.

Traceability runs one way, to ``prd_models.Feature.feature_id``, so no new identifier
scheme is introduced and any component can be read back to the product behaviour it
exists for. Unlike a PRD feature -- which is an invention if it names no requirement --
a component that names no feature is *kept*: an API gateway, a CI pipeline or a shared
auth service is genuinely cross-cutting, and dropping it would remove real design.
Untraced components are counted in ``ArchitectureData.notes`` instead, so a reviewer
sees them rather than losing them.
"""

from dataclasses import dataclass
from typing import Optional

from prd_models import PRDData

# The three deployables the architecture stage covers. A component's layer is decided by
# which section of the response it arrived in, so an unknown layer cannot be recorded.
BACKEND = "backend"
WEB = "web"
MOBILE = "mobile"
LAYERS = (BACKEND, WEB, MOBILE)
LAYER_LABEL = {
    BACKEND: "Backend",
    WEB: "Web application",
    MOBILE: "Mobile application",
}


@dataclass
class Component:
    """
    One service, module, page area or screen, with what it owns and what it depends on.

    ``apis`` are the boundaries this component exposes or calls -- the contract other
    components integrate through. ``data`` is what it owns: stored data for a backend
    service, client state for a web or mobile module. ``feature_ids`` are PRD feature
    ids and are the only link back to the product definition.
    """

    component_id: str
    name: str
    layer: str = BACKEND
    responsibility: str = ""
    feature_ids: tuple = ()
    apis: tuple = ()
    data: tuple = ()
    dependencies: tuple = ()

    @property
    def is_traceable(self) -> bool:
        """True when this component names at least one PRD feature it realises."""
        return bool(self.feature_ids)


@dataclass
class Decision:
    """One architecture decision: what was chosen, and why it was chosen."""

    decision_id: str
    title: str
    choice: str = ""
    rationale: str = ""
    feature_ids: tuple = ()


@dataclass
class DataFlow:
    """An ordered path data or control takes across components, including navigation."""

    name: str
    steps: tuple = ()
    feature_ids: tuple = ()


@dataclass
class Integration:
    """An external system the architecture depends on, and what it is used for."""

    name: str
    purpose: str = ""
    direction: str = ""
    feature_ids: tuple = ()


@dataclass
class Risk:
    """A technical risk, with its impact and whatever mitigation was proposed."""

    statement: str
    impact: str = ""
    mitigation: str = ""


@dataclass
class ArchitectureData:
    """
    A technical architecture for one approved PRD.

    ``notes`` records every deterministic repair -- unknown feature ids, applied caps, a
    layer with no component, untraced components -- so a reviewer reads what was changed
    rather than trusting the result. ``source_prd_title``, ``source_feature_ids`` and
    ``discussion_source`` are the provenance: which PRD this came from, which features it
    was allowed to trace to, and whether an optional architecture discussion contributed.
    """

    project_title: str = ""
    overview: str = ""
    components: tuple = ()
    domains: tuple = ()
    auth_approach: tuple = ()
    decisions: tuple = ()
    flows: tuple = ()
    integrations: tuple = ()
    dependencies: tuple = ()
    risks: tuple = ()
    notes: tuple = ()
    source_prd_title: str = ""
    source_feature_ids: tuple = ()
    discussion_source: str = ""
    # True when this is the deterministic per-feature skeleton rather than a designed
    # architecture. Recorded rather than inferred from the notes, because the lifecycle
    # reports a fallback as a draft and a designed architecture as pending review.
    is_baseline: bool = False

    @property
    def is_empty(self) -> bool:
        """An architecture with no component is not an architecture."""
        return not self.components

    def layer(self, name: str) -> tuple:
        """Every component in one layer, in the order it was assembled."""
        return tuple(item for item in self.components if item.layer == name)

    @property
    def backend(self) -> tuple:
        return self.layer(BACKEND)

    @property
    def web(self) -> tuple:
        return self.layer(WEB)

    @property
    def mobile(self) -> tuple:
        return self.layer(MOBILE)

    @property
    def missing_layers(self) -> tuple:
        """Layers no component covers. Reported, because the PRD stage asked for all three."""
        return tuple(name for name in LAYERS if not self.layer(name))

    @property
    def covered_feature_ids(self) -> tuple:
        """PRD feature ids some component, decision, flow or integration names."""
        covered: list = []
        holders = (
            tuple(self.components)
            + tuple(self.decisions)
            + tuple(self.flows)
            + tuple(self.integrations)
        )
        for holder in holders:
            for identifier in getattr(holder, "feature_ids", ()) or ():
                if identifier not in covered:
                    covered.append(identifier)
        return tuple(covered)

    @property
    def uncovered_feature_ids(self) -> tuple:
        """PRD feature ids nothing in the architecture names. Reported, never hidden."""
        covered = set(self.covered_feature_ids)
        return tuple(
            identifier
            for identifier in self.source_feature_ids
            if identifier not in covered
        )


def prd_feature_ids(prd_data: Optional[PRDData]) -> tuple:
    """
    Every feature id the PRD offers, in PRD order.

    This is the set an architecture may trace to. Features with no id are skipped: there
    is nothing for a component to name.
    """
    if prd_data is None:
        return ()
    identifiers: list = []
    for feature in getattr(prd_data, "features", ()) or ():
        identifier = " ".join(str(getattr(feature, "feature_id", "") or "").split())
        if identifier and identifier not in identifiers:
            identifiers.append(identifier)
    return tuple(identifiers)
