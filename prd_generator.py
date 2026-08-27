"""
Generating a PRD from an approved BRD.

The same three boundaries the Jira planner works under apply here, for the same
reasons.

**The approved BRD is the required input.** A PRD is generated from the BRD alone; an
optional product-refinement transcript may be supplied to enrich it, but the BRD is
what the PRD traces to. Without an approved BRD holding at least one identified
requirement there is nothing to derive a PRD from, and ``generate_prd`` refuses rather
than inventing a product.

**Every feature must name the requirements it serves.** Requirement ids are checked
against the ids the BRD actually minted; unknown ids are dropped, and a feature left
naming none is dropped with it -- see ``Feature.is_traceable``. That is the check that
stops a plausible-sounding invented feature from reaching the review screen.

**The model's answer is a proposal, not a PRD.** Everything it returns passes through
``assemble_prd``, which is ordinary deterministic code: text is coerced and capped,
lists are bounded, unknown requirement ids are removed, untraceable features and empty
journeys are dropped, and everything that changed is recorded in ``PRDData.notes``.
There is no second model call to fix up the first. If the model is unavailable,
unreachable, or returns nothing usable, a deterministic PRD derived straight from the
BRD is used instead and the PRD says so.

Nothing here creates a language-model client. The call is injected as
``generate(prompt) -> str``, so this module has no network access of its own, no
credential of its own, and no import of ``main``.
"""

from dataclasses import replace
from typing import Optional

from brd_models import BRDData, NormalizedTranscript
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
from prd_models import Feature, PRDData, Persona, UserJourney, brd_requirement_ids

# Caps on what one model response may turn into. Not style limits: they bound how much
# a single response can inflate session state and the review screen. Anything trimmed
# is reported in the PRD notes rather than silently dropped.
MAX_FEATURES = 40
MAX_JOURNEYS = 15
MAX_PERSONAS = 12
MAX_LIST_ITEMS = 12
MAX_OVERVIEW_LENGTH = 2000
MAX_SUMMARY_LENGTH = 600
MAX_STATEMENT_LENGTH = 300

# How much of an optional refinement transcript is quoted into the prompt. A full
# transcript can be tens of thousands of characters; the BRD is the input that matters
# and the transcript is supporting evidence.
MAX_REFINEMENT_LENGTH = 8000

# How many items of a kind a note lists by name before it just gives the count.
_NOTE_SAMPLE = 5


class PRDResponseError(ModelResponseError):
    """
    The model's answer could not be read as a PRD.

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
    return tuple(
        excerpt(value, MAX_STATEMENT_LENGTH) for value in strings(values)[:limit]
    )


def _requirement_lookup(brd_data: BRDData) -> dict:
    """Comparison form of every BRD requirement id, mapped to the id as the BRD spells it."""
    lookup: dict = {}
    for identifier in brd_requirement_ids(brd_data):
        key = normalise_id(identifier)
        if key:
            lookup.setdefault(key, identifier)
    return lookup


def _requirement_statements(brd_data: BRDData) -> dict:
    """BRD requirement id to its statement, for prompts and for the deterministic PRD."""
    statements: dict = {}
    for requirement in tuple(getattr(brd_data, "functional_requirements", ()) or ()) + tuple(
        getattr(brd_data, "non_functional_requirements", ()) or ()
    ):
        identifier = line(getattr(requirement, "requirement_id", ""))
        if identifier:
            statements.setdefault(identifier, line(getattr(requirement, "statement", "")))
    return statements


# --- The prompt -----------------------------------------------------------

# The output contract, kept as a literal so no formatting pass has to escape its
# braces. Field names match ``prd_models`` where they correspond.
_JSON_SHAPE = """{
  "overview": "two or three sentences on what this product is and who it serves",
  "goals": ["a product goal that serves a business objective above"],
  "personas": [
    {
      "name": "the user type, drawn from the stakeholders or requirements above",
      "description": "one line on who they are and what they are trying to do",
      "needs": ["what this persona needs from the product"]
    }
  ],
  "features": [
    {
      "name": "the feature, named as a product capability",
      "summary": "what the feature does, in product terms rather than business terms",
      "requirement_ids": ["FR-1", "NFR-2"],
      "behaviours": ["a specific behaviour of this feature under normal use"],
      "edge_cases": ["a case the feature must handle that the happy path does not"],
      "acceptance_criteria": ["a testable statement that decides whether this is done"]
    }
  ],
  "user_journeys": [
    {
      "name": "the journey, e.g. first purchase",
      "persona": "the persona name taking it",
      "steps": ["one step of the journey, in order"],
      "requirement_ids": ["FR-1"]
    }
  ],
  "success_metrics": ["how success is measured, only where the BRD supports it"],
  "assumptions": ["a product assumption this PRD makes"],
  "open_questions": ["a product question that must be answered before build"]
}"""


def _section(heading: str, lines) -> list:
    """One prompt section, or nothing when there is nothing to say."""
    listed = [line(value) for value in lines if line(value)]
    if not listed:
        return []
    return ["{}:".format(heading)] + ["- {}".format(value) for value in listed] + [""]


def build_prd_prompt(brd_data: BRDData, refinement: Optional[NormalizedTranscript] = None) -> str:
    """
    The PRD prompt: the approved BRD, the optional refinement transcript, the contract.

    Requirement ids are given with their statements because the model has to name the
    ids it serves, and it can only do that if it was told them.
    """
    functional = getattr(brd_data, "functional_requirements", ()) or ()
    non_functional = getattr(brd_data, "non_functional_requirements", ()) or ()

    lines = [
        "You are a senior product manager writing a Product Requirements Document.",
        "",
        "The Business Requirements Document below has been reviewed and approved. It is "
        "the requirements source of truth. Your PRD must define product behaviour that "
        "satisfies it -- not restate it.",
        "",
        "Project: {}".format(line(getattr(brd_data, "project_title", "")) or "Untitled Project"),
        "",
    ]

    lines += _section(
        "Approved functional requirements (id: statement)",
        [
            "{}: {}".format(line(getattr(item, "requirement_id", "")), line(getattr(item, "statement", "")))
            for item in functional
            if line(getattr(item, "requirement_id", ""))
        ],
    )
    lines += _section(
        "Approved non-functional requirements (id: statement)",
        [
            "{}: {}".format(line(getattr(item, "requirement_id", "")), line(getattr(item, "statement", "")))
            for item in non_functional
            if line(getattr(item, "requirement_id", ""))
        ],
    )
    lines += _section("Business objectives", getattr(brd_data, "business_objectives", ()) or ())
    lines += _section(
        "Stakeholders",
        [
            " — ".join(
                part
                for part in (
                    line(getattr(person, "name", "")),
                    line(getattr(person, "role", "")),
                    line(getattr(person, "stated_interest", "")),
                )
                if part
            )
            for person in getattr(brd_data, "stakeholders", ()) or ()
        ],
    )
    lines += _section(
        "Assumptions recorded in the BRD",
        [line(getattr(item, "statement", "")) for item in getattr(brd_data, "assumptions", ()) or ()],
    )
    lines += _section("Business rules and key decisions", getattr(brd_data, "key_decisions", ()) or ())
    lines += _section("Acceptance criteria stated in the BRD", getattr(brd_data, "acceptance_criteria", ()) or ())
    lines += _section("Out of scope", getattr(brd_data, "deferred_or_out_of_scope_items", ()) or ())
    lines += _section("Open questions from the BRD", getattr(brd_data, "open_questions", ()) or ())

    refinement_text = block(getattr(refinement, "raw_text", "")) if refinement is not None else ""
    if refinement_text:
        lines += [
            "Optional product-refinement discussion (supporting evidence only; the BRD "
            "above still governs):",
            refinement_text[:MAX_REFINEMENT_LENGTH],
            "",
        ]

    lines += [
        "Rules:",
        "- Add product-level detail the BRD does not already give: personas, features, "
        "user journeys, concrete behaviour, edge cases and acceptance criteria.",
        "- Do not restate a requirement as a feature. A feature is a capability that may "
        "serve several requirements.",
        "- Every feature and every journey must list the requirement ids it serves, using "
        "the ids exactly as spelled above. A feature naming no requirement will be discarded.",
        "- Never introduce a requirement id that is not listed above.",
        "- Only state success metrics the material above supports. Leave the list empty "
        "rather than inventing a target.",
        "- Put anything you are unsure about in open_questions instead of guessing.",
        "",
        "Return JSON only, in exactly this shape:",
        _JSON_SHAPE,
    ]
    return "\n".join(lines)


# --- Reading the response -------------------------------------------------


def parse_prd_response(raw) -> dict:
    """
    The model's answer as a dict of PRD sections.

    A bare list is accepted as the features list, because a model asked for a document
    will occasionally answer with just the features.
    """
    try:
        payload = json_payload(raw, "AI PRD generator")
    except ModelResponseError as error:
        raise PRDResponseError(str(error)) from error

    if isinstance(payload, list):
        return {"features": payload}
    if not isinstance(payload, dict):
        raise PRDResponseError("The AI PRD generator's response was not a PRD object.")

    nested = first(payload, "prd", "document")
    if isinstance(nested, dict):
        payload = nested

    if not isinstance(first(payload, "features", "capabilities", "product_features"), (list, tuple)):
        raise PRDResponseError("The AI PRD generator's response contained no features.")
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


def assemble_prd(
    payload: dict,
    brd_data: BRDData,
    refinement: Optional[NormalizedTranscript] = None,
) -> PRDData:
    """
    A validated PRD from a model proposal. Every repair is recorded in ``notes``.

    Deterministic: given the same proposal and the same BRD this returns the same PRD,
    with no second model call to correct the first.
    """
    lookup = _requirement_lookup(brd_data)
    known = brd_requirement_ids(brd_data)
    notes: list = []
    unknown_ids: list = []

    def requirement_ids(row: dict) -> tuple:
        """BRD ids this row names, in BRD spelling; unknown ones recorded and dropped."""
        accepted: list = []
        for candidate in strings(first(row, "requirement_ids", "requirements", "brd_requirement_ids")):
            resolved = lookup.get(normalise_id(candidate))
            if resolved is None:
                if candidate not in unknown_ids:
                    unknown_ids.append(candidate)
            elif resolved not in accepted:
                accepted.append(resolved)
        return tuple(accepted)

    features: list = []
    dropped_untraceable: list = []
    for row in _rows(payload, "features", "capabilities", "product_features"):
        name = excerpt(first(row, "name", "feature", "title", "summary"), MAX_STATEMENT_LENGTH)
        if not name:
            continue
        served = requirement_ids(row)
        if not served:
            dropped_untraceable.append(name)
            continue
        if len(features) >= MAX_FEATURES:
            notes.append(
                "The AI PRD generator proposed more than {} features; the rest were not "
                "kept.".format(MAX_FEATURES)
            )
            break
        features.append(
            Feature(
                feature_id="F-{}".format(len(features) + 1),
                name=name,
                summary=excerpt(first(row, "summary", "description", "detail"), MAX_SUMMARY_LENGTH),
                requirement_ids=served,
                behaviours=_capped(first(row, "behaviours", "behaviors", "functional_behaviour")),
                edge_cases=_capped(first(row, "edge_cases", "edge_case", "exceptions")),
                acceptance_criteria=_capped(
                    first(row, "acceptance_criteria", "acceptance", "criteria")
                ),
            )
        )

    if dropped_untraceable:
        notes.append(
            "{} proposed feature(s) named no BRD requirement and were dropped: {}.".format(
                len(dropped_untraceable), _sample(dropped_untraceable)
            )
        )
    if unknown_ids:
        notes.append(
            "{} requirement id(s) the AI PRD generator referenced are not in the BRD and "
            "were removed: {}.".format(len(unknown_ids), _sample(unknown_ids))
        )

    journeys: list = []
    for row in _rows(payload, "user_journeys", "journeys", "user_flows"):
        name = excerpt(first(row, "name", "journey", "title"), MAX_STATEMENT_LENGTH)
        steps = _capped(first(row, "steps", "flow", "stages"))
        if not name or not steps:
            continue
        if len(journeys) >= MAX_JOURNEYS:
            notes.append(
                "The AI PRD generator proposed more than {} user journeys; the rest were "
                "not kept.".format(MAX_JOURNEYS)
            )
            break
        journeys.append(
            UserJourney(
                name=name,
                persona=excerpt(first(row, "persona", "actor", "user"), MAX_STATEMENT_LENGTH),
                steps=steps,
                requirement_ids=requirement_ids(row),
            )
        )

    personas: list = []
    for row in _rows(payload, "personas", "users", "user_types"):
        name = excerpt(first(row, "name", "persona", "title"), MAX_STATEMENT_LENGTH)
        if not name or len(personas) >= MAX_PERSONAS:
            continue
        personas.append(
            Persona(
                name=name,
                description=excerpt(first(row, "description", "summary", "detail"), MAX_SUMMARY_LENGTH),
                needs=_capped(first(row, "needs", "goals", "wants")),
            )
        )

    prd = PRDData(
        project_title=line(getattr(brd_data, "project_title", "")) or "Untitled Project",
        overview=excerpt(first(payload, "overview", "product_overview", "summary"), MAX_OVERVIEW_LENGTH),
        goals=_capped(first(payload, "goals", "product_goals", "objectives")),
        personas=tuple(personas),
        features=tuple(features),
        journeys=tuple(journeys),
        success_metrics=_capped(first(payload, "success_metrics", "metrics", "kpis")),
        assumptions=_capped(first(payload, "assumptions", "product_assumptions")),
        open_questions=_capped(first(payload, "open_questions", "questions", "unknowns")),
        source_brd_title=line(getattr(brd_data, "project_title", "")),
        source_requirement_ids=known,
        refinement_source=line(getattr(refinement, "source", "")) if refinement is not None else "",
    )

    if prd.features and prd.uncovered_requirement_ids:
        notes.append(
            "{} BRD requirement(s) are not covered by any feature or journey: {}.".format(
                len(prd.uncovered_requirement_ids), _sample(prd.uncovered_requirement_ids)
            )
        )
    if not prd.features:
        notes.append("The AI PRD generator's response held no feature that traced to the BRD.")

    return replace(prd, notes=tuple(notes))


def build_baseline_prd(
    brd_data: BRDData, refinement: Optional[NormalizedTranscript] = None
) -> PRDData:
    """
    A PRD derived from the BRD by deterministic mapping: one feature per requirement.

    This is the fallback, and it is deliberately shallow -- it carries no journey, no
    edge case and no invented metric, because none of those can be derived without
    judgement. It exists so a reviewer with no model available still gets a traceable
    starting point rather than an empty screen.
    """
    statements = _requirement_statements(brd_data)
    features = tuple(
        Feature(
            feature_id="F-{}".format(position),
            name=excerpt(statements.get(identifier, identifier), 120) or identifier,
            summary=excerpt(statements.get(identifier, ""), MAX_SUMMARY_LENGTH),
            requirement_ids=(identifier,),
        )
        for position, identifier in enumerate(brd_requirement_ids(brd_data)[:MAX_FEATURES], start=1)
    )
    return PRDData(
        project_title=line(getattr(brd_data, "project_title", "")) or "Untitled Project",
        overview=excerpt(
            " ".join(strings(getattr(brd_data, "business_objectives", ()) or ())),
            MAX_OVERVIEW_LENGTH,
        ),
        goals=_capped(getattr(brd_data, "business_objectives", ()) or ()),
        personas=tuple(
            Persona(
                name=line(getattr(person, "name", "")),
                description=excerpt(
                    " — ".join(
                        part
                        for part in (
                            line(getattr(person, "role", "")),
                            line(getattr(person, "stated_interest", "")),
                        )
                        if part
                    ),
                    MAX_SUMMARY_LENGTH,
                ),
            )
            for person in (getattr(brd_data, "stakeholders", ()) or ())[:MAX_PERSONAS]
            if line(getattr(person, "name", ""))
        ),
        features=features,
        assumptions=_capped(
            [line(getattr(item, "statement", "")) for item in getattr(brd_data, "assumptions", ()) or ()]
        ),
        open_questions=_capped(getattr(brd_data, "open_questions", ()) or ()),
        source_brd_title=line(getattr(brd_data, "project_title", "")),
        source_requirement_ids=brd_requirement_ids(brd_data),
        refinement_source=line(getattr(refinement, "source", "")) if refinement is not None else "",
        is_baseline=True,
    )


def _with_note(prd: PRDData, note: str) -> PRDData:
    """The PRD with ``note`` first, because it explains everything after it."""
    return replace(prd, notes=(note,) + tuple(prd.notes))


def generate_prd(
    brd_data: Optional[BRDData],
    refinement: Optional[NormalizedTranscript] = None,
    generate=None,
) -> PRDData:
    """
    Propose a PRD for one approved BRD. Approves nothing and writes nothing.

    ``generate`` is a callable taking the prompt and returning the model's response
    text. Injected rather than built here so this module holds no client, no credential
    and no import of ``main``; passing ``None`` -- which is what happens when no API key
    is configured -- falls back to ``build_baseline_prd``.

    With no BRD, or a BRD carrying no identified requirement, this returns an empty PRD
    whose note says why. That is the safe answer: there is nothing to trace a product
    definition to, and generating one anyway would be inventing requirements.
    """
    if brd_data is None or not brd_requirement_ids(brd_data):
        return PRDData(
            notes=(
                "A PRD needs an approved BRD holding at least one identified requirement. "
                "Generate and approve a BRD first.",
            )
        )

    baseline = lambda: build_baseline_prd(brd_data, refinement)  # noqa: E731

    if generate is None:
        return _with_note(
            baseline(),
            "The AI PRD generator was not available, so this PRD maps each BRD "
            "requirement to one feature rather than defining product behaviour. "
            "Configure GEMINI_API_KEY to have a full PRD generated.",
        )

    try:
        raw = generate(build_prd_prompt(brd_data, refinement))
    except Exception as error:  # noqa: BLE001 - any client failure means the same thing
        # The exception type only. A client's message can carry request context that has
        # no business in a PRD note.
        return _with_note(
            baseline(),
            "The AI PRD generator could not be reached ({}), so this PRD maps each BRD "
            "requirement to one feature instead.".format(type(error).__name__),
        )

    try:
        payload = parse_prd_response(raw)
    except PRDResponseError as error:
        return _with_note(
            baseline(),
            "{} This PRD maps each BRD requirement to one feature instead.".format(error),
        )

    prd = assemble_prd(payload, brd_data, refinement)
    if prd.is_empty:
        fallback = baseline()
        return replace(fallback, notes=tuple(prd.notes) + tuple(fallback.notes))
    return prd
