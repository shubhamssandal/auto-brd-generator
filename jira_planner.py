"""
AI-assisted BRD-to-Jira work planning.

The deterministic mapping in ``jira_processor.build_work_plan`` proposes one issue
per BRD requirement. That is faithful but shallow: a BRD holding thirty related
requirements becomes thirty siblings under one container, with no grouping and with
action items attached only when they literally spell out a requirement id. This
module adds the missing step -- deciding *how* the approved requirements should be
grouped, decomposed and parented inside the selected project's own hierarchy -- and
asks a language model to make that judgement.

Three boundaries make that safe.

**The BRD is the only input.** The planner receives the approved ``BRDData`` and
nothing else. It never sees the original transcript, so it cannot reinterpret what
was said: every requirement it works from already passed the verbatim evidence check
in ``main.validate_and_create_brd_data``. The model may group, split, order and
title; it may not add a requirement that is not in the BRD.

**Every proposed issue must name its sources.** An item carries the BRD requirement
ids and action-item ids it came from, and an item that names none is dropped before
the plan is ever shown -- see ``PlannedIssue.is_traceable``. Grouping issues inherit
the union of their children's sources, because that is what a container of traceable
work traces to. This is the check that stops a plausible-sounding invented issue from
reaching the review screen.

**The model's answer is a proposal, not a plan.** Everything the model returns passes
through ``assemble_plan``, which is ordinary deterministic code: unknown requirement
ids are removed, unknown issue-type names fall back to the type Jira reported for
that rung, levels outside what the project supports are flattened into it, parent
references that do not resolve are re-pointed or cleared, cycles are broken, and
anything that changed is recorded in ``JiraWorkPlan.notes``. There is no second model
call to fix up the first one. If the model is unavailable, returns nothing usable, or
returns a plan that survives none of this, the deterministic
``jira_processor.build_work_plan`` is used instead and the plan says so.

Nothing here talks to Jira, and nothing here creates a language-model client. The
call is injected as ``generate(prompt) -> str`` so this module has no network access
of its own, no credential of its own, and no import of ``main`` -- which is also what
keeps it testable without a live model.
"""

import json
import re
from dataclasses import dataclass, field, replace
from typing import Optional

from brd_models import BRDData
from jira_models import (
    JiraProject,
    JiraProjectMetadata,
    JiraWorkPlan,
    PlannedIssue,
)
from jira_processor import (
    PROVENANCE,
    build_work_plan,
    confirmed_requirements,
    plan_keys,
    plannable_levels,
    summary_line,
)

# Prefix for the ids this module gives BRD action items. ``brd_models.ActionItem``
# has no id of its own -- it is a free-text line with an optional owner -- so the
# planner has to mint one in order to be able to refer to it at all. The number is
# the action item's 1-based position in the BRD, which keeps the id stable for a
# given BRD instead of shifting when a blank line is skipped.
ACTION_ITEM_ID_PREFIX = "AI-"

# Caps on what one model response may turn into. These are not style limits; they
# bound how much a single response can inflate session state and the review screen.
# Exceeding one is reported in the plan notes rather than silently truncated.
MAX_PLANNED_ISSUES = 150
MAX_ACCEPTANCE_CRITERIA = 12
MAX_DESCRIPTION_LENGTH = 4000

# ``rationale`` is one short traceability sentence -- "covers FR-3 and FR-7" -- and is
# capped because a model asked for a sentence will sometimes answer with a paragraph
# of its own reasoning, which is exactly what must not reach the UI or Jira.
MAX_RATIONALE_LENGTH = 300

# A requirement statement quoted inside a grouping issue's description is an excerpt,
# not the requirement. The full statement stays in the BRD and in the issue that
# actually implements it.
MAX_EXCERPT_LENGTH = 300

# How many items of a kind a note lists by name before it just gives the count.
_NOTE_SAMPLE = 5

# Bound on the parent-repair fixed point. Each pass can only move an item deeper or
# detach it, so this terminates well before the bound; it exists so a malformed
# response cannot spin.
_MAX_REPAIR_PASSES = 12

# Characters allowed in a plan key. Plan keys reach Streamlit widget keys, so a key
# is restricted to something that cannot collide with the review widget's own
# separators.
_PLAN_KEY_SAFE = re.compile(r"[^0-9A-Za-z_.\- ()]+")
MAX_PLAN_KEY_LENGTH = 48


class PlannerResponseError(Exception):
    """
    The model's answer could not be read as a work plan.

    Carries a message safe to show a reviewer: what was wrong with the shape of the
    response, never the response itself. A raw payload can run to thousands of
    characters and is not something to paste into a Streamlit note.
    """


def _line(value) -> str:
    """One line of text: whitespace collapsed, ``None`` becoming ``""``."""
    return " ".join(str(value if value is not None else "").split())


def _block(value) -> str:
    """Multi-line text with its own line breaks kept and trailing space removed."""
    text = str(value if value is not None else "").replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


def _excerpt(value, limit: int = MAX_EXCERPT_LENGTH) -> str:
    """One line, truncated with an ellipsis rather than cut mid-word-ish."""
    text = _line(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _strings(value) -> list:
    """
    A list of non-empty single-line strings from whatever the model sent.

    Accepts a list, a bare string, or a list of small objects such as
    ``[{"id": "FR-1"}]``, because all three turn up in practice. Anything else
    contributes nothing rather than raising: a malformed member of one field is not a
    reason to discard an otherwise usable item.
    """
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        single = _line(value)
        return [single] if single else []
    if isinstance(value, dict):
        value = list(value.values())
    if not isinstance(value, (list, tuple, set)):
        single = _line(value)
        return [single] if single else []

    out: list = []
    for item in value:
        if isinstance(item, dict):
            cleaned = ""
            for key in ("id", "requirement_id", "action_item_id", "key", "value", "item"):
                if item.get(key):
                    cleaned = _line(item.get(key))
                    break
        else:
            cleaned = _line(item)
        if cleaned:
            out.append(cleaned)
    return out


def _first(payload: dict, *names):
    """The first of ``names`` present in ``payload`` with a non-empty value."""
    for name in names:
        if name in payload:
            value = payload.get(name)
            if value not in (None, "", [], (), {}):
                return value
    return None


def action_item_index(brd_data: BRDData) -> dict:
    """
    Map of planner-minted action-item id to the BRD action item it names.

    Ordered by BRD position and numbered from it, so ``AI-3`` is the third action
    item in the BRD whether or not the first two carried text. Action items with no
    text are left out: there is nothing for a planner to link.
    """
    index: dict = {}
    for position, action_item in enumerate(getattr(brd_data, "action_items", None) or (), start=1):
        if _line(getattr(action_item, "item", "")):
            index["{}{}".format(ACTION_ITEM_ID_PREFIX, position)] = action_item
    return index


def _action_item_line(identifier: str, action_item) -> str:
    """One action item rendered for a prompt or a description, with what was stated."""
    parts = [_line(getattr(action_item, "item", ""))]
    owner = _line(getattr(action_item, "owner", ""))
    due = _line(getattr(action_item, "due_date", ""))
    if owner:
        parts.append("owner stated: {}".format(owner))
    if due:
        parts.append("due date stated: {}".format(due))
    return "{}: {}".format(identifier, " — ".join(part for part in parts if part))


# The output contract, kept as a literal so no formatting pass has to escape its
# braces. Field names match ``PlannedIssue`` where they correspond, which makes the
# mapping in ``_read_item`` readable rather than clever.
_JSON_SHAPE = """{
  "items": [
    {
      "id": "unique short id for this item, e.g. E1 or F2 or S7",
      "summary": "one line, no line breaks, under 255 characters",
      "description": "what this item covers, in plain sentences drawn only from the BRD content above",
      "issue_type": "exactly one of the Jira issue type names listed for the chosen level",
      "level": 0,
      "parent": "id of another item in this list, or \\"\\" for a top-level item",
      "requirement_ids": ["FR-1", "FR-2"],
      "action_item_ids": ["AI-1"],
      "acceptance_criteria": ["a testable statement taken from the BRD"],
      "rationale": "one short sentence naming the BRD requirements this item serves"
    }
  ]
}"""


def _level_lines(levels: tuple) -> list:
    """The selected project's usable hierarchy, described in its own vocabulary."""
    lines: list = []
    for level in levels:
        names = ", ".join(level.type_names) or "(unnamed)"
        role = "subtask level — an item here MUST have a parent" if level.subtask else "standard level"
        lines.append(
            "- level {}: issue type(s) {} ({})".format(level.depth, names, role)
        )
    return lines


def _brd_section(heading: str, lines) -> list:
    kept = [_line(item) for item in lines if _line(item)]
    if not kept:
        return []
    return [heading] + ["- {}".format(item) for item in kept] + [""]


def build_planning_prompt(
    brd_data: BRDData,
    project: JiraProject,
    metadata: JiraProjectMetadata,
    levels: tuple = (),
) -> str:
    """
    The planning prompt for one BRD and one project. Sends nothing.

    Everything the model is allowed to plan from is in here: the approved BRD's
    requirements with their ids, the supporting context that helps decide how they
    group, and the issue types Jira actually reported for this project. The transcript
    is deliberately absent -- the planner works from approved requirements, not from
    what was said in the meeting.
    """
    levels = levels or plannable_levels(metadata)
    requirements = confirmed_requirements(brd_data)
    keys = plan_keys(requirements)
    actions = action_item_index(brd_data)

    functional = {id(item) for item in brd_data.functional_requirements}

    lines: list = [
        "You are planning Jira work items for a software delivery team.",
        "",
        "You will be given an APPROVED Business Requirements Document (BRD) and the "
        "issue types one specific Jira project actually offers. Your job is to decide "
        "how the approved requirements should be grouped and broken down into work "
        "items in that project's hierarchy.",
        "",
        "TARGET JIRA PROJECT",
        "- project: {}".format(_line(project.display_label) or _line(project.api_identifier)),
        "- usable hierarchy levels, shallowest first:",
    ]
    lines.extend(_level_lines(levels))
    lines.append("")

    lines.extend(
        [
            "APPROVED BRD",
            "- title: {}".format(_line(brd_data.project_title) or "Untitled Project"),
        ]
    )
    overview = getattr(brd_data, "project_overview", None)
    if overview is not None and _line(getattr(overview, "project_name", "")):
        lines.append("- project name: {}".format(_line(overview.project_name)))
    lines.append("")

    lines.extend(_brd_section("Business objectives", brd_data.business_objectives))
    lines.extend(_brd_section("Background", brd_data.background_information))

    lines.append("Requirements — these are the ONLY requirements that exist:")
    for requirement, key in zip(requirements, keys):
        kind = "functional" if id(requirement) in functional else "non-functional"
        lines.append("- {} ({}): {}".format(key, kind, _line(requirement.statement)))
    lines.append("")

    lines.extend(_brd_section("Acceptance criteria stated for the BRD as a whole", brd_data.acceptance_criteria))
    lines.extend(_brd_section("Dependencies", brd_data.dependencies))
    lines.extend(_brd_section("Risks", brd_data.risks))
    lines.extend(_brd_section("Key decisions", brd_data.key_decisions))

    if actions:
        lines.append("Action items — each has an id you may reference:")
        for identifier, action_item in actions.items():
            lines.append("- {}".format(_action_item_line(identifier, action_item)))
        lines.append("")

    excluded = _brd_section(
        "Explicitly out of scope — plan NO work for these",
        brd_data.deferred_or_out_of_scope_items,
    )
    lines.extend(excluded)

    lines.extend(
        [
            "RULES",
            "1. Invent nothing. Every item you produce must restate or decompose "
            "requirements that appear in the list above. Do not add a requirement, a "
            "feature, an integration or a phase that the BRD does not state.",
            "2. Every item must name at least one requirement id in "
            "\"requirement_ids\", or at least one action item id in "
            "\"action_item_ids\". An item that names neither will be discarded.",
            "3. Use only the requirement ids and action item ids listed above, spelled "
            "exactly as they appear. Do not make up ids.",
            "4. Group related requirements. Where several requirements describe one "
            "coherent area of work, put them under one item at a shallower level and "
            "name that area after what the BRD calls it. Do not produce one item per "
            "requirement just because that is simple, and do not force everything into "
            "a single group either.",
            "5. Break down a requirement that clearly contains several distinct pieces "
            "of work into one item per piece, each naming that same requirement id.",
            "6. Associate each action item with the item whose work it belongs to, "
            "judging by what the action item says. It does NOT have to mention a "
            "requirement id. Leave an action item out entirely if it genuinely belongs "
            "to none of the work — it will be reported for human review, which is "
            "better than attaching it somewhere it does not belong.",
            "7. \"issue_type\" must be one of the names listed for the level you chose, "
            "copied exactly. \"level\" must be one of the level numbers listed above.",
            "8. \"parent\" must be the id of another item in your list at the level "
            "immediately above this item's level, or \"\" for a top-level item. An item "
            "at the subtask level must have a parent. Do not create a cycle.",
            "9. Put only the deepest hierarchy the listed levels allow. If only one "
            "level is listed, produce a flat list.",
            "10. Acceptance criteria must come from the BRD's own acceptance criteria "
            "or requirement statements. Do not invent test cases.",
            "11. \"rationale\" is one short sentence of traceability. Do not include "
            "your reasoning, your planning process, or any step-by-step explanation "
            "anywhere in your answer.",
            "",
            "OUTPUT",
            "Return JSON only — no prose before or after it, no code fence — in exactly "
            "this shape:",
            _JSON_SHAPE,
        ]
    )
    return "\n".join(lines)


@dataclass(frozen=True)
class PlannerItem:
    """
    One item exactly as the model proposed it, cleaned but not yet validated.

    A deliberate halfway house: parsing has already established that the response was
    JSON of roughly the right shape and that the strings are strings, but nothing here
    has been checked against the BRD or against the project's issue types. ``level``
    is ``None`` when the model gave none or gave something unreadable, which
    ``assemble_plan`` resolves from the issue-type name or the parent instead.
    """

    identifier: str
    summary: str
    description: str = ""
    issue_type: str = ""
    level: Optional[int] = None
    parent: str = ""
    requirement_ids: tuple = ()
    action_item_ids: tuple = ()
    acceptance_criteria: tuple = ()
    rationale: str = ""


# Top-level keys a response might carry the item list under. Asked for ``items``;
# these are the near-misses worth accepting rather than discarding a whole plan over.
_ITEM_LIST_KEYS = ("items", "issues", "work_items", "planned_issues", "plan")


def _maybe_int(value) -> Optional[int]:
    """``value`` as an int when it plainly is one, else ``None``. Bools are not ints here."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if float(value).is_integer() else None
    text = _line(value)
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return None


def _first_json_span(text: str) -> Optional[str]:
    """
    The first balanced ``{...}`` or ``[...]`` in ``text``, or ``None``.

    String literals are tracked so a brace inside a description cannot end the span
    early. This exists because a model told to return JSON only will still occasionally
    prefix a sentence, and re-prompting to fix that would be a second model call.
    """
    start = None
    for index, character in enumerate(text):
        if character in "{[":
            start = index
            break
    if start is None:
        return None

    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        character = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == opener:
            depth += 1
        elif character == closer:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _json_payload(raw):
    """
    The model's response as Python data.

    Raises ``PlannerResponseError`` rather than returning something half-read, so the
    caller's one decision -- use this plan or fall back to the deterministic one --
    stays a single branch.
    """
    if isinstance(raw, (dict, list)):
        return raw

    text = str(raw or "").strip()
    if not text:
        raise PlannerResponseError("The AI planner returned an empty response.")

    unfenced = re.sub(r"^```[A-Za-z]*\s*", "", text)
    unfenced = re.sub(r"\s*```\s*$", "", unfenced).strip()

    for candidate in (unfenced, text):
        try:
            return json.loads(candidate)
        except (TypeError, ValueError):
            pass

    span = _first_json_span(unfenced)
    if span is not None:
        try:
            return json.loads(span)
        except ValueError:
            pass

    raise PlannerResponseError("The AI planner's response could not be read as JSON.")


def _item_rows(payload) -> list:
    """The list of proposed items inside a parsed response, in the order given."""
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = None
        for key in _ITEM_LIST_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                rows = value
                break
        if rows is None:
            # A single item returned unwrapped. Accepting it costs nothing and a model
            # asked for one grouping issue sometimes answers exactly that.
            if _first(payload, "summary", "title") is not None:
                rows = [payload]
            else:
                rows = []
    else:
        rows = []
    return [row for row in rows if isinstance(row, dict)]


def _read_item(row: dict) -> Optional[PlannerItem]:
    """
    One row as a ``PlannerItem``, or ``None`` when it cannot be one.

    An item needs an id -- parents reference it -- and a summary, since Jira refuses an
    issue without one and this app is the thing filling that field in. Everything else
    is optional and repaired later.
    """
    identifier = _line(_first(row, "id", "key", "item_id", "plan_key"))
    summary = _line(_first(row, "summary", "title", "name"))
    if not identifier or not summary:
        return None

    issue_type = _line(_first(row, "issue_type", "issuetype", "type", "issue_type_name"))
    raw_level = _first(row, "level", "depth", "hierarchy_depth", "plan_level")
    level = _maybe_int(raw_level)
    if level is None and not issue_type:
        # "level": "Epic" -- the level slot used for a type name. Read it as the name
        # rather than throwing the only hierarchy signal in the row away.
        issue_type = _line(raw_level)

    criteria = _strings(_first(row, "acceptance_criteria", "criteria", "acceptanceCriteria"))

    return PlannerItem(
        identifier=identifier,
        summary=summary,
        description=_block(_first(row, "description", "details", "body"))[:MAX_DESCRIPTION_LENGTH],
        issue_type=issue_type,
        level=level,
        parent=_line(_first(row, "parent", "parent_id", "parent_key", "parent_plan_key")),
        requirement_ids=tuple(
            _strings(_first(row, "requirement_ids", "source_requirement_ids", "requirements"))
        ),
        action_item_ids=tuple(
            _strings(_first(row, "action_item_ids", "source_action_item_ids", "action_items"))
        ),
        acceptance_criteria=tuple(criteria[:MAX_ACCEPTANCE_CRITERIA]),
        rationale=_line(_first(row, "rationale", "traceability", "justification"))[
            :MAX_RATIONALE_LENGTH
        ],
    )


def parse_planner_response(raw) -> tuple:
    """
    A model response as proposed items, in the order the model gave them.

    Pure and side-effect free: no BRD, no metadata, no Jira. Raises
    ``PlannerResponseError`` when there is nothing usable, with a message safe to show
    a reviewer.
    """
    rows = _item_rows(_json_payload(raw))
    if not rows:
        raise PlannerResponseError("The AI planner's response held no work items.")

    items = tuple(item for item in (_read_item(row) for row in rows) if item is not None)
    if not items:
        raise PlannerResponseError(
            "The AI planner's response held no work item with both an id and a summary."
        )
    return items


@dataclass
class _Draft:
    """
    One proposed item mid-assembly. Mutable on purpose.

    Assembly is a sequence of repairs -- resolve the level, resolve the type, re-point
    the parent, inherit sources from children -- and each pass needs to see the result
    of the last. Doing that with ``dataclasses.replace`` on a frozen record would mean
    rebuilding the index between every pass. The frozen ``PlannedIssue`` records are
    built once at the end, from drafts that have stopped changing.
    """

    identifier: str
    plan_key: str
    summary: str
    description: str
    depth: int
    parent: str
    requirement_ids: list = field(default_factory=list)
    action_item_ids: list = field(default_factory=list)
    acceptance_criteria: tuple = ()
    rationale: str = ""


def _sanitise_plan_key(candidate: str, position: int) -> str:
    """A plan key safe to put in a Streamlit widget key, from the model's own id."""
    cleaned = _PLAN_KEY_SAFE.sub("-", _line(candidate)).strip(" -")
    cleaned = cleaned[:MAX_PLAN_KEY_LENGTH].strip(" -")
    return cleaned or "ITEM-{}".format(position)


def _unique(existing: set, candidate: str, position: int) -> str:
    """``candidate`` if unused, else the same disambiguated the way ``plan_keys`` does."""
    key = candidate
    if key in existing:
        key = "{} ({})".format(candidate, position)
        suffix = position
        while key in existing:
            suffix += 1
            key = "{} ({})".format(candidate, suffix)
    existing.add(key)
    return key


def _normalise_id(value: str) -> str:
    """An id in comparison form: trimmed of stray punctuation, case-folded."""
    return _line(value).strip(" .,;:()[]").lower()


def _requirement_lookup(requirements: list, keys: list) -> dict:
    """Comparison form of every acceptable requirement id, mapped to the plan's own."""
    lookup: dict = {}
    for requirement, key in zip(requirements, keys):
        for candidate in (key, getattr(requirement, "requirement_id", "")):
            normalised = _normalise_id(candidate)
            if normalised:
                lookup.setdefault(normalised, key)
    return lookup


def _depth_by_type_name(levels: tuple) -> dict:
    """
    Issue-type name in comparison form to the one rung offering it.

    A name offered at more than one rung maps to ``None``: two rungs both calling a
    type "Task" gives no signal, and picking one would be guessing at hierarchy.
    """
    seen: dict = {}
    for level in levels:
        for name in level.type_names:
            key = _line(name).lower()
            if not key:
                continue
            seen[key] = level.depth if key not in seen else None
    return seen


def _merge(target: list, additions) -> None:
    """Append what is not already there, order preserved."""
    for value in additions:
        if value not in target:
            target.append(value)


def _sample(values, limit: int = _NOTE_SAMPLE) -> str:
    """``limit`` values quoted for a note, with a count when there are more."""
    listed = list(values)
    shown = ", ".join("'{}'".format(_excerpt(value, 80)) for value in listed[:limit])
    if len(listed) > limit:
        shown += " and {} more".format(len(listed) - limit)
    return shown


def _planned_description(
    draft: _Draft,
    requirements_by_key: dict,
    actions: dict,
) -> str:
    """
    One proposed issue's description, with its traceability written into it.

    The traceability block is part of the description rather than only UI state because
    the description is what reaches Jira: someone opening the created issue can see
    which BRD requirements it came from without this app in front of them. The verbatim
    transcript quote is included only for an issue that traces to exactly one
    requirement -- for a grouping issue it would mean pasting every child's evidence
    into the parent.
    """
    lines: list = []
    if draft.description:
        lines.extend([draft.description, ""])

    traced = [key for key in draft.requirement_ids if key in requirements_by_key]
    if traced:
        lines.append("**Traces to BRD requirements**")
        for key in traced:
            lines.append(
                "- {} — {}".format(key, _excerpt(requirements_by_key[key].statement))
            )
        lines.append("")

    linked = [
        identifier for identifier in draft.action_item_ids if identifier in actions
    ]
    if linked:
        lines.append("**Linked action items from the meeting**")
        for identifier in linked:
            lines.append("- {}".format(_action_item_line(identifier, actions[identifier])))
        lines.append("")

    if len(traced) == 1:
        evidence = _block(getattr(requirements_by_key[traced[0]], "source_evidence", ""))
        if evidence:
            lines.append("**Source evidence — verbatim from the transcript**")
            lines.extend("> {}".format(line) for line in evidence.splitlines())
            lines.append("")

    lines.append(PROVENANCE)
    return "\n".join(lines).strip()


def assemble_plan(
    items,
    brd_data: BRDData,
    project: JiraProject,
    metadata: JiraProjectMetadata,
) -> JiraWorkPlan:
    """
    Deterministically turn proposed items into a plan for this project. Creates nothing.

    Ordinary code, no model call: this is the check on the model's answer, so it cannot
    itself depend on one. Every repair it makes is recorded in the returned plan's
    notes. Returns a plan with no issues -- and a note saying why -- when nothing
    survives, which is the caller's signal to fall back to the deterministic mapping.
    """
    notes: list = []
    identifier = project.api_identifier
    levels = plannable_levels(metadata)
    requirements = confirmed_requirements(brd_data)
    keys = plan_keys(requirements)
    requirements_by_key = dict(zip(keys, requirements))
    requirement_lookup = _requirement_lookup(requirements, keys)
    actions = action_item_index(brd_data)
    action_lookup = {_normalise_id(key): key for key in actions}

    def finished(issues: tuple) -> JiraWorkPlan:
        return JiraWorkPlan(
            project_identifier=identifier,
            project_label=project.display_label,
            issues=issues,
            notes=tuple(notes),
        )

    if not levels:
        notes.append(
            "No plan was generated: this project reported no issue type that passed the "
            "required-field check, so there is no level an issue could be proposed at."
        )
        return finished(())

    deepest = levels[-1].depth
    deepest_standard = next(
        (level.depth for level in reversed(levels) if not level.subtask), None
    )
    depth_by_name = _depth_by_type_name(levels)
    levels_by_depth = {level.depth: level for level in levels}

    # --- Pass 1: one draft per item, ids made unique, sources resolved -------------
    drafts: dict = {}
    used_keys: set = set()
    duplicates = 0
    unknown_requirements: list = []
    unknown_actions: list = []
    pending_depth: list = []

    for position, item in enumerate(items[:MAX_PLANNED_ISSUES], start=1):
        if item.identifier in drafts:
            # A repeated id makes every parent reference to it ambiguous, and guessing
            # which one was meant would silently reparent someone's work item.
            duplicates += 1
            continue

        resolved_requirements: list = []
        for candidate in item.requirement_ids:
            key = requirement_lookup.get(_normalise_id(candidate))
            if key is None:
                unknown_requirements.append(candidate)
            elif key not in resolved_requirements:
                resolved_requirements.append(key)

        resolved_actions: list = []
        for candidate in item.action_item_ids:
            key = action_lookup.get(_normalise_id(candidate))
            if key is None:
                unknown_actions.append(candidate)
            elif key not in resolved_actions:
                resolved_actions.append(key)

        depth = depth_by_name.get(_line(item.issue_type).lower())
        if depth is None and item.level is not None:
            depth = item.level if item.level in levels_by_depth else None
        if depth is None:
            depth = -1  # resolved from the parent in pass 2, or defaulted to the top.
            pending_depth.append(item.identifier)

        drafts[item.identifier] = _Draft(
            identifier=item.identifier,
            plan_key=_unique(used_keys, _sanitise_plan_key(item.identifier, position), position),
            summary=summary_line(item.summary),
            description=item.description,
            depth=depth,
            parent=item.parent,
            requirement_ids=resolved_requirements,
            action_item_ids=resolved_actions,
            acceptance_criteria=tuple(
                _line(criterion)
                for criterion in item.acceptance_criteria[:MAX_ACCEPTANCE_CRITERIA]
                if _line(criterion)
            ),
            rationale=item.rationale,
        )

    if len(items) > MAX_PLANNED_ISSUES:
        notes.append(
            "The AI planner proposed {} items; the first {} were kept. Review the plan "
            "and create in stages if more are needed.".format(
                len(items), MAX_PLANNED_ISSUES
            )
        )
    if duplicates:
        notes.append(
            "{} proposed item(s) repeated an id already used by another item and were "
            "dropped, because a repeated id makes every parent reference to it "
            "ambiguous.".format(duplicates)
        )
    if unknown_requirements:
        notes.append(
            "Requirement id(s) {} were referenced by the AI planner but are not in this "
            "BRD, so they were removed from the traceability of the items naming "
            "them.".format(_sample(unknown_requirements))
        )
    if unknown_actions:
        notes.append(
            "Action item id(s) {} were referenced by the AI planner but are not in this "
            "BRD, so they were removed.".format(_sample(unknown_actions))
        )

    # Parent references that name nothing in the plan cannot be honoured.
    orphaned = 0
    for draft in drafts.values():
        if draft.parent and draft.parent not in drafts:
            draft.parent = ""
            orphaned += 1

    # --- Pass 2: depth for items whose level and type name gave no answer ----------
    for _ in range(_MAX_REPAIR_PASSES):
        settled = True
        for draft in drafts.values():
            if draft.depth >= 0:
                continue
            parent = drafts.get(draft.parent)
            if parent is not None and parent.depth >= 0:
                draft.depth = min(parent.depth + 1, deepest)
                settled = False
        if settled:
            break
    unplaced = [draft for draft in drafts.values() if draft.depth < 0]
    for draft in unplaced:
        draft.depth = 0
    if pending_depth:
        notes.append(
            "{} proposed item(s) named no issue type this project offers, or no level it "
            "supports. Each was placed at the level its parent implies, or at the top "
            "level.".format(len(pending_depth))
        )

    # --- Pass 3: parent relationships made consistent with the levels -------------
    def ancestors_loop(start: str) -> bool:
        """Whether following ``start``'s parents comes back to something already seen."""
        seen = {start}
        current = drafts[start].parent
        while current:
            if current in seen:
                return True
            seen.add(current)
            current = drafts[current].parent if current in drafts else ""
        return False

    # Checked against the live state one item at a time, so breaking a cycle at its
    # first member leaves the rest of that chain intact: an item naming itself as its
    # own parent loses that reference, and A->B->A loses only A's.
    cycles = 0
    for key in list(drafts):
        if ancestors_loop(key):
            drafts[key].parent = ""
            cycles += 1

    flattened = 0
    detached = 0
    skipped = 0
    for _ in range(_MAX_REPAIR_PASSES):
        changed = False
        for draft in drafts.values():
            parent = drafts.get(draft.parent)
            while parent is not None and parent.depth >= draft.depth:
                # The parent is no shallower than the child -- normal once a level the
                # project does not offer has been flattened into one it does. Re-point
                # at the nearest ancestor that is genuinely shallower rather than
                # dropping the relationship, so a flattened plan keeps its shape.
                draft.parent = parent.parent
                parent = drafts.get(draft.parent)
                flattened += 1
                changed = True
            if parent is not None and parent.depth < draft.depth - 1:
                # A parent more than one level above its child skips a level, which the
                # levels this project reported do not allow: a subtask in particular
                # needs a parent at the level directly above it, so one hung off a
                # higher ancestor could not be created at all. Raise the child to sit
                # directly beneath the parent the planner chose, rather than dropping
                # the relationship -- the same choice the re-pointing above makes. Its
                # own children are re-checked on the next pass.
                draft.depth = parent.depth + 1
                skipped += 1
                changed = True
            if not draft.parent and levels_by_depth[draft.depth].subtask:
                # A subtask with no parent cannot be created. Raise it to the deepest
                # level that can stand alone rather than discarding the work item.
                if deepest_standard is None:
                    continue
                draft.depth = deepest_standard
                detached += 1
                changed = True
        if not changed:
            break

    if orphaned:
        notes.append(
            "{} proposed item(s) named a parent that is not in this plan, so they are "
            "proposed without a parent.".format(orphaned)
        )
    if cycles:
        notes.append(
            "{} proposed item(s) formed a parent cycle, so their parent reference was "
            "removed.".format(cycles)
        )
    if flattened:
        notes.append(
            "This project supports {} hierarchy level(s), so {} parent relationship(s) "
            "the AI planner proposed were re-pointed to the nearest level above the "
            "child.".format(len(levels), flattened)
        )
    if detached:
        notes.append(
            "{} proposed item(s) were placed at a subtask level with no parent. A "
            "subtask cannot be created without one, so each was raised to the deepest "
            "level that can stand alone.".format(detached)
        )
    if skipped:
        notes.append(
            "{} proposed item(s) named a parent more than one level above them, which "
            "skips a level this project reports. Each was moved to the level directly "
            "below its parent.".format(skipped)
        )

    # --- Pass 4: traceability, containers inheriting from what they contain --------
    order = _topological(drafts)
    children: dict = {}
    for draft in drafts.values():
        if draft.parent:
            children.setdefault(draft.parent, []).append(draft.identifier)

    for key in reversed(order):
        draft = drafts[key]
        for child_key in children.get(key, ()):  # children are already unioned
            child = drafts[child_key]
            _merge(draft.requirement_ids, child.requirement_ids)
            _merge(draft.action_item_ids, child.action_item_ids)

    untraceable = [
        draft for draft in drafts.values()
        if not draft.requirement_ids and not draft.action_item_ids
    ]
    for draft in untraceable:
        drafts.pop(draft.identifier, None)
    if untraceable:
        notes.append(
            "{} proposed item(s) traced to no BRD requirement and no action item, so "
            "they were dropped: {}. Nothing is proposed that the BRD does not "
            "state.".format(len(untraceable), _sample(d.summary for d in untraceable))
        )

    # Dropping a subtree's root can only leave children that were themselves dropped,
    # but a re-pointed parent makes that hard to prove by inspection, so it is checked.
    for draft in drafts.values():
        if draft.parent and draft.parent not in drafts:
            draft.parent = ""

    if not drafts:
        notes.append(
            "No plan was generated from the AI planner's response: nothing in it "
            "survived validation against this BRD and this project."
        )
        return finished(())

    # --- Pass 5: the plan itself --------------------------------------------------
    issues: list = []
    for key in _topological(drafts):
        draft = drafts[key]
        level = levels_by_depth[draft.depth]
        issue_type = level.type_named(_item_type_name(items, draft.identifier)) or level.default_type
        issues.append(
            PlannedIssue(
                plan_key=draft.plan_key,
                summary=draft.summary,
                issue_type_id=issue_type.id,
                issue_type_name=issue_type.name,
                hierarchy_level=issue_type.hierarchy_level,
                description=_planned_description(draft, requirements_by_key, actions),
                acceptance_criteria=draft.acceptance_criteria,
                parent_plan_key=drafts[draft.parent].plan_key if draft.parent else "",
                source_requirement_id=(
                    draft.requirement_ids[0] if len(draft.requirement_ids) == 1 else ""
                ),
                source_requirement_ids=tuple(draft.requirement_ids),
                source_action_item_ids=tuple(draft.action_item_ids),
                rationale=draft.rationale,
            )
        )

    notes.extend(
        _coverage_notes(drafts, requirements_by_key, actions, levels, len(issues))
    )
    return finished(tuple(issues))


def _item_type_name(items, identifier: str) -> str:
    """The issue-type name the model asked for on one item, or ``""``."""
    for item in items:
        if item.identifier == identifier:
            return item.issue_type
    return ""


def _topological(drafts: dict) -> list:
    """
    Draft ids with every parent before its children, insertion order among siblings.

    Kept in the plan's own tuple order rather than only derived at creation time,
    because the review screen renders roots and recurses: a child stored before its
    parent would render outside the tree it belongs to.
    """
    placed: list = []
    seen: set = set()
    remaining = list(drafts)
    while remaining:
        progressed = False
        for key in list(remaining):
            parent = drafts[key].parent
            if not parent or parent in seen or parent not in drafts:
                placed.append(key)
                seen.add(key)
                remaining.remove(key)
                progressed = True
        if not progressed:
            # Unreachable once cycles are broken; emitting the rest keeps the plan
            # whole rather than losing items to a defect in that repair.
            placed.extend(remaining)
            break
    return placed


def _coverage_notes(
    drafts: dict,
    requirements_by_key: dict,
    actions: dict,
    levels: tuple,
    issue_count: int,
) -> list:
    """What the plan says about the BRD as a whole: what it covers and what it left."""
    notes: list = []
    covered_requirements: set = set()
    covered_actions: set = set()
    for draft in drafts.values():
        covered_requirements.update(draft.requirement_ids)
        covered_actions.update(draft.action_item_ids)

    depths = sorted({draft.depth for draft in drafts.values()})
    notes.append(
        "The AI planner grouped {} of this BRD's {} requirement(s) into {} proposed "
        "issue(s) across {} of this project's {} usable hierarchy level(s).".format(
            len(covered_requirements),
            len(requirements_by_key),
            issue_count,
            len(depths),
            len(levels),
        )
    )

    missing = [key for key in requirements_by_key if key not in covered_requirements]
    if missing:
        notes.append(
            "{} BRD requirement(s) are not covered by any proposed issue: {}. They "
            "remain in the BRD; add them before creating if they belong in this "
            "project.".format(len(missing), _sample(missing))
        )

    unlinked = [key for key in actions if key not in covered_actions]
    if unlinked:
        notes.append(
            "{} action item(s) could not be linked to any proposed issue and are listed "
            "here for review rather than attached to work they may not belong to: "
            "{}.".format(
                len(unlinked),
                _sample(_line(actions[key].item) for key in unlinked),
            )
        )
    return notes


def _with_note(plan: JiraWorkPlan, note: str) -> JiraWorkPlan:
    """The same plan with ``note`` first, so the reason leads the list a reviewer reads."""
    return replace(plan, notes=(note,) + tuple(plan.notes))


def generate_work_plan(
    brd_data: BRDData,
    project: JiraProject,
    metadata: JiraProjectMetadata,
    generate=None,
) -> JiraWorkPlan:
    """
    Propose Jira issues for one project from one approved BRD. Creates nothing.

    ``generate`` is a callable taking the planning prompt and returning the model's
    response text. Injected rather than built here so this module holds no client, no
    credential and no import of ``main``; passing ``None`` -- which is what happens
    when no API key is configured -- falls back to ``jira_processor.build_work_plan``.

    Every failure mode ends the same way: the deterministic one-to-one plan, with a
    note saying why the AI planner was not used. A reviewer is never shown an empty
    screen because a model call failed, and never shown an AI plan that did not pass
    ``assemble_plan``.
    """
    deterministic = lambda: build_work_plan(brd_data, project, metadata)  # noqa: E731

    identifier = project.api_identifier
    read_for = metadata.project_identifier
    if identifier and read_for and identifier != read_for:
        # build_work_plan reports this mismatch itself, and refuses to plan against
        # another project's issue types. Re-wording it here would say it twice.
        return deterministic()

    if not plannable_levels(metadata) or not confirmed_requirements(brd_data):
        # Also already reported: no usable issue type, or no confirmed requirement.
        return deterministic()

    if generate is None:
        return _with_note(
            deterministic(),
            "The AI planner was not available, so this plan maps each BRD requirement "
            "to one issue rather than grouping them. Configure GEMINI_API_KEY to have "
            "requirements grouped into a deeper hierarchy.",
        )

    try:
        raw = generate(build_planning_prompt(brd_data, project, metadata))
    except Exception as error:  # noqa: BLE001 - any client failure means the same thing
        # The exception type only. A client's message can carry request context that
        # has no business in a plan note.
        return _with_note(
            deterministic(),
            "The AI planner could not be reached ({}), so this plan maps each BRD "
            "requirement to one issue instead.".format(type(error).__name__),
        )

    try:
        items = parse_planner_response(raw)
    except PlannerResponseError as error:
        return _with_note(
            deterministic(),
            "{} This plan maps each BRD requirement to one issue instead.".format(error),
        )

    plan = assemble_plan(items, brd_data, project, metadata)
    if plan.is_empty:
        fallback = deterministic()
        return replace(fallback, notes=tuple(plan.notes) + tuple(fallback.notes))
    return plan
