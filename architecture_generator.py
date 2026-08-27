"""
Generating a technical architecture from an approved PRD.

The same three boundaries the PRD generator works under apply here, for the same
reasons.

**The approved PRD is the required input.** The architecture is derived from the PRD
alone; an optional architecture or design discussion may be supplied as extra evidence,
but the PRD is what the architecture traces to. Without a PRD holding at least one
feature there is nothing to design against, and ``generate_architecture`` refuses rather
than inventing a system.

**Components name the features they realise.** Feature ids are checked against the ids
the PRD actually minted, and unknown ids are dropped. Unlike a PRD feature, a component
naming no feature is *kept* -- gateways, auth services and CI pipelines are genuinely
cross-cutting -- but it is counted in the notes, so a reviewer can see what is not tied
to product behaviour instead of discovering it later.

**The model's answer is a proposal, not an architecture.** Everything it returns passes
through ``assemble_architecture``, which is ordinary deterministic code: text is coerced
and capped, lists are bounded, a component's layer comes from the section it arrived in
rather than from anything the model asserts, unknown feature ids are removed, and every
repair is recorded in ``ArchitectureData.notes``. There is no second model call to fix up
the first. If the model is unavailable, unreachable or returns nothing usable, a
deterministic skeleton derived straight from the PRD is used instead and it says so.

Nothing here creates a language-model client. The call is injected as
``generate(prompt) -> str``, so this module has no network access of its own, no
credential of its own, and no import of ``main``.
"""

from dataclasses import replace
from typing import Optional

from architecture_models import (
    BACKEND,
    LAYER_LABEL,
    LAYERS,
    MOBILE,
    WEB,
    ArchitectureData,
    Component,
    DataFlow,
    Decision,
    Integration,
    Risk,
    prd_feature_ids,
)
from brd_models import NormalizedTranscript
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
# reported in the architecture notes rather than silently dropped.
MAX_COMPONENTS_PER_LAYER = 20
MAX_DECISIONS = 20
MAX_FLOWS = 15
MAX_INTEGRATIONS = 15
MAX_RISKS = 15
MAX_LIST_ITEMS = 10
MAX_OVERVIEW_LENGTH = 2000
MAX_SUMMARY_LENGTH = 600
MAX_STATEMENT_LENGTH = 300

# How much of an optional architecture discussion is quoted into the prompt. The PRD is
# the input that matters; the discussion is supporting evidence.
MAX_DISCUSSION_LENGTH = 8000

# How many features the deterministic fallback covers. It emits three components per
# feature, so this is the cap that keeps the fallback readable.
MAX_BASELINE_FEATURES = 12

# How many items of a kind a note lists by name before it just gives the count.
_NOTE_SAMPLE = 5

# Words a model uses for each layer, so a component in a generic "components" list can
# still be placed. Anything unrecognised goes to the backend and is noted.
_LAYER_WORDS = {
    BACKEND: ("backend", "back-end", "server", "service", "services", "api", "platform"),
    WEB: ("web", "web app", "webapp", "web application", "frontend", "front-end", "browser"),
    MOBILE: ("mobile", "mobile app", "app", "ios", "android", "native"),
}


class ArchitectureResponseError(ModelResponseError):
    """
    The model's answer could not be read as an architecture.

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


def _feature_lookup(prd_data: PRDData) -> dict:
    """Comparison form of every PRD feature id, mapped to the id as the PRD spells it."""
    lookup: dict = {}
    for identifier in prd_feature_ids(prd_data):
        key = normalise_id(identifier)
        if key:
            lookup.setdefault(key, identifier)
    return lookup


def _resolve_layer(value) -> Optional[str]:
    """The layer a model's own label refers to, or ``None`` when it names none of them."""
    spoken = normalise_id(value)
    if not spoken:
        return None
    for layer, words in _LAYER_WORDS.items():
        if spoken in words:
            return layer
    for layer, words in _LAYER_WORDS.items():
        if any(word in spoken for word in words):
            return layer
    return None


# --- The prompt -----------------------------------------------------------

# The output contract, kept as a literal so no formatting pass has to escape its braces.
# Field names match ``architecture_models`` where they correspond. The layer sections are
# separate keys rather than a "layer" field, so a component cannot be mislabelled.
_JSON_SHAPE = """{
  "overview": "two or three sentences on the shape of the system and why it is shaped that way",
  "domains": ["a core business domain the backend owns, e.g. catalogue, cart, order, payment"],
  "backend": [
    {
      "name": "the service or module, named for what it owns",
      "responsibility": "what this component is responsible for, in technical terms",
      "feature_ids": ["F-1", "F-3"],
      "apis": ["the boundary it exposes or calls, e.g. POST /orders - place an order"],
      "data": ["what it stores or owns, e.g. orders table - order header and lines"],
      "dependencies": ["another component or external system it needs"]
    }
  ],
  "web": [
    {
      "name": "the application area or page group",
      "responsibility": "what the user does here and what this area renders",
      "feature_ids": ["F-1"],
      "apis": ["the backend boundary this area calls"],
      "data": ["the client state this area holds"],
      "dependencies": ["a component or library it depends on"]
    }
  ],
  "mobile": [
    {
      "name": "the screen or module",
      "responsibility": "what the user does on this screen",
      "feature_ids": ["F-1"],
      "apis": ["the backend boundary this screen calls"],
      "data": ["the on-device state or cache it holds"],
      "dependencies": ["a component it depends on"]
    }
  ],
  "authentication": ["how identity, sessions and authorization work across the clients"],
  "data_flows": [
    {
      "name": "the flow, e.g. place an order, or mobile navigation",
      "steps": ["one step of the flow, in order, naming the components involved"],
      "feature_ids": ["F-1"]
    }
  ],
  "integrations": [
    {
      "name": "the external system",
      "purpose": "what it is used for",
      "direction": "outbound, inbound, or bidirectional",
      "feature_ids": ["F-2"]
    }
  ],
  "decisions": [
    {
      "title": "the decision, e.g. synchronous payment capture",
      "choice": "what is chosen",
      "rationale": "why, in terms of the PRD features or constraints above",
      "feature_ids": ["F-1"]
    }
  ],
  "dependencies": ["a technical dependency or prerequisite the build needs"],
  "risks": [
    {
      "statement": "the technical risk",
      "impact": "what happens if it lands",
      "mitigation": "how it is reduced, only where you can say something concrete"
    }
  ]
}"""


def _section(heading: str, lines) -> list:
    """One prompt section, or nothing when there is nothing to say."""
    listed = [line(value) for value in lines if line(value)]
    if not listed:
        return []
    return ["{}:".format(heading)] + ["- {}".format(value) for value in listed] + [""]


def build_architecture_prompt(
    prd_data: PRDData, discussion: Optional[NormalizedTranscript] = None
) -> str:
    """
    The architecture prompt: the approved PRD, the optional discussion, the contract.

    Feature ids are given with their names and behaviour because the model has to name
    the ids each component realises, and it can only do that if it was told them.
    """
    lines = [
        "You are a senior technical architect designing the system for the product "
        "definition below.",
        "",
        "The Product Requirements Document below has been reviewed and approved. It is the "
        "product source of truth. Design the technical architecture that delivers it -- do "
        "not restate it.",
        "",
        "Project: {}".format(line(getattr(prd_data, "project_title", "")) or "Untitled Project"),
        "",
    ]

    overview = block(getattr(prd_data, "overview", ""))
    if overview:
        lines += ["Product overview:", overview[:MAX_OVERVIEW_LENGTH], ""]

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
                + list(getattr(feature, "acceptance_criteria", ()) or ())
            ),
        )

    lines += _section(
        "User journeys",
        [
            "{}: {}".format(
                line(getattr(journey, "name", "")),
                " → ".join(strings(getattr(journey, "steps", ()) or ())),
            )
            for journey in getattr(prd_data, "journeys", ()) or ()
        ],
    )
    lines += _section(
        "Personas",
        [
            " — ".join(
                part
                for part in (
                    line(getattr(persona, "name", "")),
                    line(getattr(persona, "description", "")),
                )
                if part
            )
            for persona in getattr(prd_data, "personas", ()) or ()
        ],
    )
    lines += _section("Success metrics", getattr(prd_data, "success_metrics", ()) or ())
    lines += _section("Product assumptions", getattr(prd_data, "assumptions", ()) or ())
    lines += _section("Open product questions", getattr(prd_data, "open_questions", ()) or ())

    discussion_text = block(getattr(discussion, "raw_text", "")) if discussion is not None else ""
    if discussion_text:
        lines += [
            "Optional architecture discussion (supporting evidence only; the PRD above "
            "still governs):",
            discussion_text[:MAX_DISCUSSION_LENGTH],
            "",
        ]

    lines += [
        "Rules:",
        "- Design a backend, a web application and a mobile application. Cover all three.",
        "- A component is a technical unit that owns something: a service, a module, a page "
        "area, a screen. One component usually realises several features.",
        "- Do not restate a feature as a component and do not copy PRD wording. Say what is "
        "built, where the API boundaries are, what stores what, and how data flows.",
        "- List the feature ids each component, decision, flow and integration serves, using "
        "the ids exactly as spelled above.",
        "- Never introduce a feature id that is not listed above.",
        "- A genuinely cross-cutting component may list no feature id, but say in its "
        "responsibility why it is cross-cutting.",
        "- Cover authentication and authorization once, across the clients.",
        "- State only technical risks and dependencies the material above supports. Leave a "
        "list empty rather than inventing an entry.",
        "",
        "Return JSON only, in exactly this shape:",
        _JSON_SHAPE,
    ]
    return "\n".join(lines)


# --- Reading the response -------------------------------------------------


def parse_architecture_response(raw) -> dict:
    """
    The model's answer as a dict of architecture sections.

    A bare list is accepted as the component list, because a model asked for a design
    will occasionally answer with just the components.
    """
    try:
        payload = json_payload(raw, "AI architecture generator")
    except ModelResponseError as error:
        raise ArchitectureResponseError(str(error)) from error

    if isinstance(payload, list):
        return {"components": payload}
    if not isinstance(payload, dict):
        raise ArchitectureResponseError(
            "The AI architecture generator's response was not an architecture object."
        )

    nested = first(payload, "architecture", "design", "document")
    if isinstance(nested, dict):
        payload = nested

    named = ("components", "backend", "web", "mobile", "services", "modules")
    if not any(isinstance(payload.get(name), (list, tuple, dict)) for name in named):
        raise ArchitectureResponseError(
            "The AI architecture generator's response contained no components."
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


def assemble_architecture(
    payload: dict,
    prd_data: PRDData,
    discussion: Optional[NormalizedTranscript] = None,
) -> ArchitectureData:
    """
    A validated architecture from a model proposal. Every repair is recorded in ``notes``.

    Deterministic: given the same proposal and the same PRD this returns the same
    architecture, with no second model call to correct the first.
    """
    lookup = _feature_lookup(prd_data)
    notes: list = []
    unknown_ids: list = []

    def feature_ids(row: dict) -> tuple:
        """PRD ids this row names, in PRD spelling; unknown ones recorded and dropped."""
        accepted: list = []
        for candidate in strings(first(row, "feature_ids", "features", "prd_feature_ids")):
            resolved = lookup.get(normalise_id(candidate))
            if resolved is None:
                if candidate not in unknown_ids:
                    unknown_ids.append(candidate)
            elif resolved not in accepted:
                accepted.append(resolved)
        return tuple(accepted)

    # Layer sections first, then any generic list, so a component's layer comes from where
    # it was declared and only an unplaced one has to be guessed at.
    candidates: list = []
    for layer, names in (
        (BACKEND, ("backend", "backend_components", "services")),
        (WEB, ("web", "web_components", "web_application")),
        (MOBILE, ("mobile", "mobile_components", "mobile_application")),
    ):
        candidates += [(layer, row) for row in _rows(payload, *names)]
    unplaced: list = []
    for row in _rows(payload, "components", "modules"):
        layer = _resolve_layer(first(row, "layer", "tier", "area"))
        if layer is None:
            layer = BACKEND
            unplaced.append(excerpt(first(row, "name", "component", "title"), 80))
        candidates.append((layer, row))

    components: list = []
    per_layer = {layer: 0 for layer in LAYERS}
    untraced: list = []
    capped_layers: list = []
    for layer, row in candidates:
        name = excerpt(first(row, "name", "component", "title", "module"), MAX_STATEMENT_LENGTH)
        if not name:
            continue
        if per_layer[layer] >= MAX_COMPONENTS_PER_LAYER:
            if layer not in capped_layers:
                capped_layers.append(layer)
            continue
        per_layer[layer] += 1
        served = feature_ids(row)
        if not served:
            untraced.append(name)
        components.append(
            Component(
                component_id="C-{}".format(len(components) + 1),
                name=name,
                layer=layer,
                responsibility=excerpt(
                    first(row, "responsibility", "purpose", "description", "summary"),
                    MAX_SUMMARY_LENGTH,
                ),
                feature_ids=served,
                apis=_capped(first(row, "apis", "api", "endpoints", "boundaries", "interfaces")),
                data=_capped(first(row, "data", "storage", "state", "data_stores", "entities")),
                dependencies=_capped(first(row, "dependencies", "depends_on", "requires")),
            )
        )

    for layer in capped_layers:
        notes.append(
            "The AI architecture generator proposed more than {} {} components; the rest "
            "were not kept.".format(MAX_COMPONENTS_PER_LAYER, LAYER_LABEL[layer].lower())
        )
    if unplaced:
        notes.append(
            "{} component(s) named no recognised layer and were placed in the backend: "
            "{}.".format(len(unplaced), _sample(unplaced))
        )
    if untraced:
        notes.append(
            "{} component(s) name no PRD feature and were kept as cross-cutting; check "
            "each one belongs: {}.".format(len(untraced), _sample(untraced))
        )
    if unknown_ids:
        notes.append(
            "{} feature id(s) the AI architecture generator referenced are not in the PRD "
            "and were removed: {}.".format(len(unknown_ids), _sample(unknown_ids))
        )

    decisions: list = []
    for row in _rows(payload, "decisions", "architecture_decisions", "key_decisions"):
        title = excerpt(first(row, "title", "decision", "name"), MAX_STATEMENT_LENGTH)
        if not title or len(decisions) >= MAX_DECISIONS:
            continue
        decisions.append(
            Decision(
                decision_id="AD-{}".format(len(decisions) + 1),
                title=title,
                choice=excerpt(first(row, "choice", "option", "selected"), MAX_SUMMARY_LENGTH),
                rationale=excerpt(
                    first(row, "rationale", "reason", "why", "justification"), MAX_SUMMARY_LENGTH
                ),
                feature_ids=feature_ids(row),
            )
        )

    flows: list = []
    for row in _rows(payload, "data_flows", "flows", "system_flows"):
        name = excerpt(first(row, "name", "flow", "title"), MAX_STATEMENT_LENGTH)
        steps = _capped(first(row, "steps", "sequence", "stages"))
        if not name or not steps or len(flows) >= MAX_FLOWS:
            continue
        flows.append(DataFlow(name=name, steps=steps, feature_ids=feature_ids(row)))

    integrations: list = []
    for row in _rows(payload, "integrations", "external_integrations", "third_parties"):
        name = excerpt(first(row, "name", "system", "service", "provider"), MAX_STATEMENT_LENGTH)
        if not name or len(integrations) >= MAX_INTEGRATIONS:
            continue
        integrations.append(
            Integration(
                name=name,
                purpose=excerpt(first(row, "purpose", "use", "description"), MAX_SUMMARY_LENGTH),
                direction=excerpt(first(row, "direction", "flow", "mode"), MAX_STATEMENT_LENGTH),
                feature_ids=feature_ids(row),
            )
        )

    risks: list = []
    for row in _rows(payload, "risks", "technical_risks"):
        statement = excerpt(first(row, "statement", "risk", "description", "title"), MAX_STATEMENT_LENGTH)
        if not statement or len(risks) >= MAX_RISKS:
            continue
        risks.append(
            Risk(
                statement=statement,
                impact=excerpt(first(row, "impact", "consequence", "effect"), MAX_SUMMARY_LENGTH),
                mitigation=excerpt(
                    first(row, "mitigation", "mitigations", "response"), MAX_SUMMARY_LENGTH
                ),
            )
        )

    architecture = ArchitectureData(
        project_title=line(getattr(prd_data, "project_title", "")) or "Untitled Project",
        overview=excerpt(
            first(payload, "overview", "summary", "architecture_overview"), MAX_OVERVIEW_LENGTH
        ),
        components=tuple(components),
        domains=_capped(first(payload, "domains", "business_domains", "bounded_contexts")),
        auth_approach=_capped(
            first(payload, "authentication", "auth", "authorization", "security")
        ),
        decisions=tuple(decisions),
        flows=tuple(flows),
        integrations=tuple(integrations),
        dependencies=_capped(first(payload, "dependencies", "prerequisites", "technical_dependencies")),
        risks=tuple(risks),
        source_prd_title=line(getattr(prd_data, "project_title", "")),
        source_feature_ids=prd_feature_ids(prd_data),
        discussion_source=line(getattr(discussion, "source", "")) if discussion is not None else "",
    )

    if architecture.components:
        for layer in architecture.missing_layers:
            notes.append(
                "The architecture proposes no {} component. Add one or record why the "
                "product does not need it.".format(LAYER_LABEL[layer].lower())
            )
        if architecture.uncovered_feature_ids:
            notes.append(
                "{} PRD feature(s) are not realised by any component, decision or flow: "
                "{}.".format(
                    len(architecture.uncovered_feature_ids),
                    _sample(architecture.uncovered_feature_ids),
                )
            )
    else:
        notes.append("The AI architecture generator's response held no usable component.")

    return replace(architecture, notes=tuple(notes))


def build_baseline_architecture(
    prd_data: PRDData, discussion: Optional[NormalizedTranscript] = None
) -> ArchitectureData:
    """
    An architecture derived from the PRD by deterministic mapping: one component per
    feature in each layer.

    This is the fallback, and it is deliberately shallow -- it carries no decision, no
    data flow and no risk, because none of those can be derived without judgement. It
    exists so a reviewer with no model available still gets a traceable starting point
    rather than an empty screen.
    """
    features = tuple(getattr(prd_data, "features", ()) or ())[:MAX_BASELINE_FEATURES]
    shapes = (
        (BACKEND, "{} service", "Own the data and rules behind {}."),
        (WEB, "{} (web)", "Render and drive {} in the browser."),
        (MOBILE, "{} (mobile)", "Render and drive {} on mobile."),
    )
    components: list = []
    for layer, name_shape, responsibility_shape in shapes:
        for feature in features:
            identifier = line(getattr(feature, "feature_id", ""))
            title = excerpt(getattr(feature, "name", ""), 120) or identifier
            if not identifier or not title:
                continue
            components.append(
                Component(
                    component_id="C-{}".format(len(components) + 1),
                    name=name_shape.format(title),
                    layer=layer,
                    responsibility=responsibility_shape.format(title),
                    feature_ids=(identifier,),
                )
            )

    return ArchitectureData(
        project_title=line(getattr(prd_data, "project_title", "")) or "Untitled Project",
        overview=excerpt(getattr(prd_data, "overview", ""), MAX_OVERVIEW_LENGTH),
        components=tuple(components),
        source_prd_title=line(getattr(prd_data, "project_title", "")),
        source_feature_ids=prd_feature_ids(prd_data),
        discussion_source=line(getattr(discussion, "source", "")) if discussion is not None else "",
        is_baseline=True,
    )


def _with_note(architecture: ArchitectureData, note: str) -> ArchitectureData:
    """The architecture with ``note`` first, because it explains everything after it."""
    return replace(architecture, notes=(note,) + tuple(architecture.notes))


def generate_architecture(
    prd_data: Optional[PRDData],
    discussion: Optional[NormalizedTranscript] = None,
    generate=None,
) -> ArchitectureData:
    """
    Propose an architecture for one approved PRD. Approves nothing and writes nothing.

    ``generate`` is a callable taking the prompt and returning the model's response text.
    Injected rather than built here so this module holds no client, no credential and no
    import of ``main``; passing ``None`` -- which is what happens when no API key is
    configured -- falls back to ``build_baseline_architecture``.

    With no PRD, or a PRD carrying no feature, this returns an empty architecture whose
    note says why. That is the safe answer: there is nothing to design against, and
    designing anyway would be inventing product scope.
    """
    if prd_data is None or not prd_feature_ids(prd_data):
        return ArchitectureData(
            notes=(
                "An architecture needs an approved PRD holding at least one feature. "
                "Generate and approve a PRD first.",
            )
        )

    baseline = lambda: build_baseline_architecture(prd_data, discussion)  # noqa: E731

    if generate is None:
        return _with_note(
            baseline(),
            "The AI architecture generator was not available, so this architecture maps "
            "each PRD feature to one component per layer rather than designing the "
            "system. Configure GEMINI_API_KEY to have a full architecture generated.",
        )

    try:
        raw = generate(build_architecture_prompt(prd_data, discussion))
    except Exception as error:  # noqa: BLE001 - any client failure means the same thing
        # The exception type only. A client's message can carry request context that has
        # no business in an architecture note.
        return _with_note(
            baseline(),
            "The AI architecture generator could not be reached ({}), so this architecture "
            "maps each PRD feature to one component per layer instead.".format(
                type(error).__name__
            ),
        )

    try:
        payload = parse_architecture_response(raw)
    except ArchitectureResponseError as error:
        return _with_note(
            baseline(),
            "{} This architecture maps each PRD feature to one component per layer "
            "instead.".format(error),
        )

    architecture = assemble_architecture(payload, prd_data, discussion)
    if architecture.is_empty:
        fallback = baseline()
        return replace(fallback, notes=tuple(architecture.notes) + tuple(fallback.notes))
    return architecture
