"""
JIRA-010 requirement change detection: comparison logic, prompt, parsing, validation.

The one place that decides whether newer state disagrees with the approved BRD, and
what to propose about it. Two entry paths reach it and produce the same
``RequirementChange`` objects:

* a meeting -- pasted notes, an uploaded transcript, a Google Meet or Microsoft Teams
  transcript -- arriving as the ``NormalizedTranscript`` every ingestion route already
  produces, so no new ingestion path exists here;
* an issue in Jira edited away from what this app last synchronized into it.

Three properties this module is built around.

**Nothing here mutates anything.** Every function is pure: the BRD it is handed is
not touched, no Jira call is reachable from here, and applying a decision is a
separate, explicit call the reviewer's approval leads to.

**Model output is untrusted.** A model may classify, quote, and draft wording. It may
not decide what a requirement currently says, which requirement an id refers to, or
which Jira work is affected -- those are filled in from stored state after the
response is read, so a fabricated id or key cannot become part of a proposal. Every
repair is recorded in ``ChangeProposal.notes`` rather than applied silently.

**Ambiguity is preserved, not resolved.** Evidence that supports no classification
becomes an ``UNCLEAR`` change that cannot be approved, instead of being forced into
one of the four categories.

No model client lives here. The caller passes a ``generate(prompt) -> str`` callable,
exactly as ``jira_planner.generate_work_plan`` takes one, so this module holds no API
key, cannot reach the network, and is testable without a live model.
"""

import re
from copy import deepcopy
from dataclasses import replace
from typing import Optional

from brd_models import BRDData, Requirement, NormalizedTranscript
from jira_models import (
    APPLICABLE_CHANGE_TYPES,
    APPROVAL_STATES,
    CHANGE_SOURCES,
    CHANGE_TYPES,
    ChangeProposal,
    RequirementChange,
)

# Reused from the planner rather than re-implemented: the same tolerant text helpers
# and the same balanced-brace JSON scanner, so a prose-prefixed or fenced response is
# read the same way in both features and neither needs a second model call to fix one.
from jira_planner import (
    PlannerResponseError,
    _block,
    _excerpt,
    _first,
    _json_payload,
    _line,
    _sample,
)
from jira_processor import adf_to_text, confirmed_requirements, issue_description_text

# Caps. Each exists so one unusable response cannot produce an unreviewable page.
MAX_CHANGES = 60
MAX_STATEMENT_LENGTH = 1000
MAX_EVIDENCE_LENGTH = 800
MAX_PROMPT_REQUIREMENTS = 120
MAX_PROMPT_EVIDENCE_CHARS = 20000
MAX_ISSUES_COMPARED = 40

_CHANGE_LIST_KEYS = ("changes", "requirement_changes", "detected_changes", "items", "results")

# How a model's wording for a change kind maps onto the five this app has. Anything
# absent from here is not guessed at: it becomes ``UNCLEAR``.
_TYPE_SYNONYMS = {
    "new": "NEW",
    "added": "NEW",
    "addition": "NEW",
    "new_requirement": "NEW",
    "changed": "CHANGED",
    "change": "CHANGED",
    "modified": "CHANGED",
    "modification": "CHANGED",
    "updated": "CHANGED",
    "update": "CHANGED",
    "amended": "CHANGED",
    "removed": "REMOVED_DEFERRED",
    "removal": "REMOVED_DEFERRED",
    "removed_deferred": "REMOVED_DEFERRED",
    "deferred": "REMOVED_DEFERRED",
    "dropped": "REMOVED_DEFERRED",
    "descoped": "REMOVED_DEFERRED",
    "out_of_scope": "REMOVED_DEFERRED",
    "unchanged": "UNCHANGED",
    "no_change": "UNCHANGED",
    "same": "UNCHANGED",
    "confirmed": "UNCHANGED",
    "unclear": "UNCLEAR",
    "ambiguous": "UNCLEAR",
    "uncertain": "UNCLEAR",
}

_CONFIDENCE = ("high", "medium", "low")


class DetectorResponseError(Exception):
    """
    A model response that could not be read as a set of proposed changes.

    Carries a message safe to show a reviewer. The raw payload is never included: it
    is model output of unknown content, and the reviewer's decision is the same
    either way -- there is nothing to review from this run.
    """


# --- Sources ---------------------------------------------------------------


def normalize_change_source(value) -> str:
    """
    One of ``CHANGE_SOURCES`` for whatever a caller names the source.

    ``NormalizedTranscript.source`` already uses four of these five names, so a
    transcript's own source usually passes through untouched. Anything unrecognised
    becomes ``"manual"``: a source label is a display detail, and refusing to detect
    changes because an ingestion route spelled itself differently would be worse than
    labelling pasted-looking text as pasted.
    """
    name = _line(value).lower().replace("-", "_").replace(" ", "_")
    if name in CHANGE_SOURCES:
        return name
    if name in ("meet", "googlemeet", "google"):
        return "google_meet"
    if name in ("teams", "msteams", "microsoftteams", "microsoft"):
        return "microsoft_teams"
    if name in ("file", "txt", "transcript_file", "uploaded"):
        return "upload"
    return "manual"


def requirement_index(brd_data: BRDData) -> dict:
    """
    Every confirmed requirement keyed by its id, upper-cased.

    Built from ``jira_processor.confirmed_requirements`` so the requirements a change
    may refer to are exactly the ones the work planner would plan -- one definition of
    "an approved requirement", not two. Upper-cased because a model that answers
    ``fr-7`` means ``FR-7``; that is a spelling difference, not a different id.
    """
    index: dict = {}
    for requirement in confirmed_requirements(brd_data):
        key = _norm_id(requirement.requirement_id)
        if key and key not in index:
            index[key] = requirement
    return index


def _norm_id(value) -> str:
    return _line(value).upper()


def _statement(value) -> str:
    return _excerpt(value, MAX_STATEMENT_LENGTH)


def _evidence(value) -> str:
    text = _block(value)
    if len(text) <= MAX_EVIDENCE_LENGTH:
        return text
    return text[: MAX_EVIDENCE_LENGTH - 1].rstrip() + "…"


# --- Impact: which work a requirement is actually linked to ----------------


def affected_work(requirement_id: str, plan=None, created=()) -> tuple:
    """
    The planned items and created Jira issues that name ``requirement_id``.

    Read straight off the stored plan and the stored creation results -- the same
    traceability JIRA-008 already records -- so an impact claim is only ever as strong
    as a mapping this app wrote down itself. Nothing is inferred from issue text and
    nothing is followed transitively: a parent is not "affected" because its child is,
    because that is a dependency this app has no evidence for.
    """
    identifier = _norm_id(requirement_id)
    if not identifier:
        return ((), ())

    plan_keys = tuple(
        issue.plan_key
        for issue in (plan.issues if plan is not None else ())
        if identifier in tuple(_norm_id(one) for one in issue.requirement_ids)
    )
    issue_keys = tuple(
        record.issue_key
        for record in created or ()
        if record.succeeded
        and record.issue_key
        and identifier in tuple(_norm_id(one) for one in record.source_requirement_ids)
    )
    return (plan_keys, issue_keys)


def requirements_for_issue(issue_key: str, created=()) -> tuple:
    """
    The BRD requirement ids one created Jira issue came from, or ``()``.

    The reverse direction of the same stored mapping. Empty is a real answer: an issue
    created from an action item, or a container issue, has no requirement behind it,
    and inventing one would be exactly the guess JIRA-010 forbids.
    """
    key = _line(issue_key)
    for record in created or ():
        if record.succeeded and record.issue_key == key:
            return tuple(_norm_id(one) for one in record.source_requirement_ids if _line(one))
    return ()


def _impact_lines(change_type: str, plan_keys: tuple, issue_keys: tuple) -> tuple:
    """The impact sentence and the proposed action, from stored links only."""
    if change_type == "NEW":
        return (
            "No Jira work is linked to this requirement yet, because it is not in the "
            "approved BRD.",
            "Add the requirement to the BRD, then regenerate the work plan to place it.",
        )

    where = []
    if issue_keys:
        where.append("{} created Jira issue(s): {}".format(len(issue_keys), ", ".join(issue_keys)))
    if plan_keys:
        where.append(
            "{} planned item(s): {}".format(len(plan_keys), ", ".join(plan_keys))
        )
    if not where:
        return (
            "No planned or created Jira work is linked to this requirement, so no Jira "
            "impact is known from stored mappings.",
            "Update the requirement in the BRD. Nothing in Jira is affected as far as "
            "this app has recorded.",
        )

    linked = " and ".join(where)
    if change_type == "REMOVED_DEFERRED":
        return (
            "Deferring this requirement affects {}.".format(linked),
            "Move the requirement to the deferred list. Existing Jira issues are not "
            "closed or deleted by this app — review them yourself.",
        )
    return (
        "This requirement is restated by {}, which still carry the old wording.".format(linked),
        "Update the requirement in the BRD, then review each affected Jira issue. "
        "Approving here changes nothing in Jira.",
    )


# --- The prompt ------------------------------------------------------------

# A literal, so no ``format`` pass has to escape its braces.
_JSON_SHAPE = """{
  "changes": [
    {
      "change_id": "C-1",
      "requirement_id": "FR-3",
      "change_type": "CHANGED",
      "proposed_statement": "The system shall ...",
      "source_evidence": "one sentence copied word for word from the new meeting text",
      "confidence": "high",
      "reason": "why that evidence implies this change"
    }
  ]
}"""


def build_change_prompt(
    brd_data: BRDData,
    transcript: NormalizedTranscript,
    source_type: str = "",
) -> str:
    """
    Ask the model to compare the approved requirements with one new meeting.

    Sends the approved requirement ids and statements, what the BRD already records as
    deferred, and the new meeting text. Deliberately does *not* send Jira keys, plan
    keys or issue summaries: the model has no business proposing which work is
    affected, and text it never saw is text it cannot fabricate a reference to.

    The rules in here are restated as deterministic checks in ``validate_changes``. A
    prompt is a request, not a guarantee, so nothing downstream depends on the model
    having followed one.
    """
    source = normalize_change_source(source_type or getattr(transcript, "source", ""))
    requirements = confirmed_requirements(brd_data)[:MAX_PROMPT_REQUIREMENTS]

    lines = [
        "You are a business analyst reconciling an approved Business Requirements "
        "Document with what a later meeting decided.",
        "",
        "APPROVED REQUIREMENTS (the current agreed truth):",
    ]
    if requirements:
        lines += [
            "- {}: {}".format(_line(item.requirement_id) or "(no id)", _excerpt(item.statement, 400))
            for item in requirements
        ]
    else:
        lines.append("- (none recorded)")

    deferred = [_excerpt(item, 200) for item in brd_data.deferred_or_out_of_scope_items if _line(item)]
    if deferred:
        lines += ["", "ALREADY DEFERRED OR OUT OF SCOPE:"] + [
            "- {}".format(item) for item in deferred[:40]
        ]

    text = _block(getattr(transcript, "raw_text", ""))
    truncated = len(text) > MAX_PROMPT_EVIDENCE_CHARS
    lines += [
        "",
        "NEW MEETING CONTENT (source: {}{}):".format(
            source,
            ", " + _line(transcript.meeting_title) if getattr(transcript, "meeting_title", "") else "",
        ),
        text[:MAX_PROMPT_EVIDENCE_CHARS],
    ]
    if truncated:
        lines.append("[the meeting content was truncated for length]")

    lines += [
        "",
        "TASK. For each approved requirement the meeting actually speaks to, and for "
        "each requirement the meeting newly asks for, report one change.",
        "",
        "RULES.",
        "1. change_type is exactly one of NEW, CHANGED, REMOVED_DEFERRED, UNCHANGED, "
        "UNCLEAR.",
        "2. Use CHANGED only when the meeting materially alters what the requirement "
        "says -- a different number, scope, rule or actor. Rewording that means the "
        "same thing is UNCHANGED.",
        "3. Use REMOVED_DEFERRED only when the meeting explicitly drops, postpones or "
        "descopes the requirement.",
        "4. Use UNCLEAR when the meeting touches a requirement but does not settle it. "
        "Do not guess, and do not resolve an ambiguity yourself.",
        "5. requirement_id must be an id listed above, copied exactly. For NEW, leave "
        "requirement_id empty -- a new id is assigned outside this response.",
        "6. source_evidence must be copied word for word from the new meeting content "
        "above. Never paraphrase it, never write it yourself, and never quote the "
        "approved requirements as evidence. A change whose evidence cannot be found in "
        "the meeting content is discarded.",
        "7. proposed_statement is required for NEW and CHANGED: one sentence, the full "
        "requirement as it should now read. Leave it empty otherwise.",
        "8. Do not invent requirements, stakeholders, business rules, ids or Jira "
        "issue keys. Do not mention Jira. Report only what this meeting supports.",
        "9. Report nothing at all rather than filling the list with speculation.",
        "",
        "Reply with JSON only, in exactly this shape:",
        _JSON_SHAPE,
    ]
    return "\n".join(lines)


# --- Reading the response --------------------------------------------------


def parse_change_response(raw) -> tuple:
    """
    The rows a model response holds, in the order given, as plain dicts.

    Tolerant about the envelope -- a bare list, a wrapper under any of several likely
    keys, a fenced or prose-prefixed body -- and strict about nothing else: judging a
    row is ``validate_changes``' job, and splitting "could it be read" from "is it
    usable" keeps one failure from masking the other.

    Raises ``DetectorResponseError`` when there is no readable JSON at all.
    """
    try:
        payload = _json_payload(raw)
    except PlannerResponseError:
        raise DetectorResponseError(
            "The change detector's response could not be read as JSON, so no change "
            "was proposed from this source."
        )

    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = None
        for key in _CHANGE_LIST_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                rows = value
                break
        if rows is None:
            # A single change returned unwrapped, which happens when only one thing
            # changed. Accepting it costs nothing.
            rows = [payload] if _first(payload, "change_type", "type", "requirement_id") else []
    else:
        raise DetectorResponseError(
            "The change detector's response was not a list of changes, so no change "
            "was proposed from this source."
        )

    return tuple(row for row in rows if isinstance(row, dict))


def _resolve_type(value) -> str:
    name = _line(value).lower().replace("-", "_").replace("/", "_").replace(" ", "_")
    if name in CHANGE_TYPES:
        return name
    if name.upper() in CHANGE_TYPES:
        return name.upper()
    return _TYPE_SYNONYMS.get(name, "UNCLEAR")


def _quotes_the_source(evidence: str, haystack: str) -> bool:
    """
    Whether ``evidence`` really appears in the meeting content.

    The same rule BRD generation applies to requirement evidence: a quote that cannot
    be found in the source is not evidence. Compared once as given and once with
    whitespace collapsed, because a model re-wrapping a line it copied correctly is a
    formatting difference, not a fabrication.
    """
    quote = _block(evidence)
    if not quote or not haystack:
        return False
    if quote in haystack:
        return True
    return " ".join(quote.split()) in " ".join(haystack.split())


def _next_new_id(index: dict, taken: set) -> str:
    """The next free ``FR-n``, counting past both the BRD's ids and this batch's."""
    used = set(index) | {_norm_id(one) for one in taken}
    numbers = [
        int(match.group(1))
        for match in (re.match(r"^FR-?(\d+)$", key) for key in used)
        if match
    ]
    candidate = max(numbers) + 1 if numbers else 1
    while "FR-{}".format(candidate) in used:
        candidate += 1
    return "FR-{}".format(candidate)


def validate_changes(
    rows,
    brd_data: BRDData,
    transcript: NormalizedTranscript,
    plan=None,
    created=(),
    source_type: str = "",
) -> ChangeProposal:
    """
    Turn model rows into proposals that cannot lie about stored state.

    Every check here is deterministic and every one of them is the reason a mutation
    cannot be reached from raw model output:

    * an unknown ``requirement_id`` on a CHANGED, UNCHANGED or REMOVED_DEFERRED row
      discards the row and says so -- there is nothing to reconcile against;
    * ``old_text`` is overwritten from the stored requirement, never read from the
      response, so a proposal cannot misquote what was approved;
    * evidence that is not in this meeting's text is removed and the change is held
      for manual review rather than shown as a quote nobody said;
    * an unrecognised ``change_type`` becomes ``UNCLEAR``, which cannot be approved;
    * a NEW row gets an id minted here, and one that named an existing requirement is
      held for manual review instead of being silently reclassified;
    * ``proposed_new_text`` is required for NEW and CHANGED; a CHANGED row that
      restates the requirement unchanged is recorded as UNCHANGED;
    * ``affected_plan_keys`` and ``affected_issue_keys`` come from the stored plan and
      creation results, never from the response;
    * duplicate ``change_id`` values are made unique;
    * ``approval_state`` is forced to ``pending``, whatever the response said.
    """
    source = normalize_change_source(source_type or getattr(transcript, "source", ""))
    reference = _source_reference(transcript)
    haystack = _block(getattr(transcript, "raw_text", ""))
    index = requirement_index(brd_data)

    changes: list = []
    notes: list = []
    unknown_ids: list = []
    used_change_ids: set = set()
    used_requirement_types: set = set()
    minted: set = set()

    rows = list(rows)
    if len(rows) > MAX_CHANGES:
        notes.append(
            "The detector returned {} changes; only the first {} are shown.".format(
                len(rows), MAX_CHANGES
            )
        )
        rows = rows[:MAX_CHANGES]

    for position, row in enumerate(rows, start=1):
        change_type = _resolve_type(_first(row, "change_type", "type", "classification", "kind"))
        raw_id = _norm_id(_first(row, "requirement_id", "requirement", "id"))
        proposed = _statement(
            _first(row, "proposed_statement", "proposed_new_text", "new_statement", "statement")
        )
        evidence = _evidence(_first(row, "source_evidence", "evidence", "quote", "excerpt"))
        reason = _excerpt(_first(row, "reason", "rationale", "explanation", "notes"), 300)
        confidence = _line(_first(row, "confidence", "certainty")).lower()
        if confidence not in _CONFIDENCE:
            confidence = ""

        needs_review = False
        review_reason = ""
        requirement: Optional[Requirement] = None

        if change_type == "NEW":
            if raw_id and raw_id in index:
                # The model called it new while naming a requirement that exists. That
                # is a real ambiguity: leave it for a person.
                needs_review = True
                review_reason = (
                    "Reported as a new requirement but it names {}, which already "
                    "exists. Decide whether this is a change to {} or a genuinely new "
                    "requirement.".format(raw_id, raw_id)
                )
                requirement_id = raw_id
                requirement = index[raw_id]
            else:
                requirement_id = _next_new_id(index, minted)
                minted.add(requirement_id)
        else:
            requirement = index.get(raw_id)
            if requirement is None:
                unknown_ids.append(raw_id or "(no id)")
                continue
            requirement_id = _norm_id(requirement.requirement_id)

        old_text = _statement(requirement.statement) if requirement is not None else ""

        duplicate_key = (change_type, requirement_id)
        if change_type != "NEW" and duplicate_key in used_requirement_types:
            notes.append(
                "A duplicate {} change for {} was discarded.".format(
                    change_type.lower(), requirement_id
                )
            )
            continue
        used_requirement_types.add(duplicate_key)

        if change_type == "UNCHANGED":
            proposed = ""
        elif change_type == "UNCLEAR":
            needs_review = True
            review_reason = review_reason or (
                reason or "The evidence does not settle what this requirement should say."
            )
        elif not proposed:
            needs_review = True
            review_reason = review_reason or (
                "No proposed requirement wording was returned, so there is nothing to "
                "apply. Write the wording yourself or discard this."
            )
        elif change_type == "CHANGED" and _line(proposed) == _line(old_text):
            change_type = "UNCHANGED"
            proposed = ""
            notes.append(
                "{} was reported as changed but the proposed wording matches the "
                "approved wording, so it is recorded as unchanged.".format(requirement_id)
            )

        if not _quotes_the_source(evidence, haystack):
            notes.append(
                "The evidence quoted for {} could not be found in this source, so it "
                "is not shown and the change needs manual review.".format(requirement_id)
            )
            evidence = ""
            needs_review = True
            review_reason = review_reason or (
                "The quoted evidence is not in this meeting's content, so the change "
                "cannot be traced to what was said."
            )

        plan_keys, issue_keys = affected_work(requirement_id, plan, created)
        impact, action = _impact_lines(change_type, plan_keys, issue_keys)
        if change_type == "UNCHANGED":
            impact = "Nothing changes: the meeting restates what the BRD already says."
            action = "No action. Recorded so the review shows the requirement was covered."
        elif needs_review:
            action = "Review this yourself. It cannot be approved as it stands."

        change_id = _line(_first(row, "change_id", "id")) or "C-{}".format(position)
        if change_id in used_change_ids:
            change_id = "{}-{}".format(change_id, position)
        used_change_ids.add(change_id)

        changes.append(
            RequirementChange(
                change_id=change_id,
                source_type=source,
                change_type=change_type,
                requirement_id=requirement_id,
                old_text=old_text,
                proposed_new_text=proposed,
                source_evidence=evidence,
                source_reference=reference,
                affected_plan_keys=plan_keys,
                affected_issue_keys=issue_keys,
                impact=impact,
                proposed_action=action,
                confidence=confidence,
                needs_manual_review=needs_review,
                review_reason=review_reason,
                # Not negotiable, whatever the response said. ``reviewer_note`` is left
                # empty on purpose: it belongs to the person reviewing this, and filling
                # it with the model's own rationale would overwrite their note with
                # untrusted prose before they ever wrote one.
                approval_state="pending",
            )
        )

    if unknown_ids:
        notes.append(
            "{} change(s) named a requirement that is not in this BRD and were "
            "discarded: {}.".format(len(unknown_ids), _sample(unknown_ids))
        )

    return ChangeProposal(
        source_type=source,
        source_reference=reference,
        changes=tuple(changes),
        notes=tuple(notes),
    )


# --- The meeting path, end to end ------------------------------------------


def _source_reference(transcript: NormalizedTranscript) -> str:
    return _line(
        getattr(transcript, "meeting_title", "")
        or getattr(transcript, "transcript_id", "")
        or ""
    )


def detect_meeting_changes(
    brd_data: BRDData,
    transcript: NormalizedTranscript,
    plan=None,
    created=(),
    generate=None,
    source_type: str = "",
) -> ChangeProposal:
    """
    Compare one new meeting against the approved BRD. Changes nothing.

    The single entry point for all four meeting sources -- pasted notes, an uploaded
    transcript, Google Meet, Microsoft Teams -- because each of them has already been
    turned into a ``NormalizedTranscript`` by the ingestion this app has. The source
    only survives as a label on each change.

    ``generate`` is a callable taking the prompt and returning the model's response,
    injected exactly as ``jira_planner.generate_work_plan`` takes one, so this module
    holds no client and no credential.

    Every failure ends the same way: an empty proposal whose notes say why. Unlike
    planning there is no deterministic fallback -- deciding whether a meeting changed a
    requirement is a judgement, and inventing one from string comparison would be the
    guess this feature exists to prevent. So "no model, no comparison" is reported
    rather than worked around.

    Returned proposals are always pending. Nothing here writes to the BRD, and Jira is
    not reachable from this module at all.
    """
    source = normalize_change_source(source_type or getattr(transcript, "source", ""))
    reference = _source_reference(transcript)
    empty = lambda note: ChangeProposal(  # noqa: E731
        source_type=source, source_reference=reference, notes=(note,)
    )

    if not confirmed_requirements(brd_data):
        return empty(
            "This BRD has no confirmed requirements, so there is nothing for this "
            "source to be compared against."
        )
    if not _block(getattr(transcript, "raw_text", "")):
        return empty("This source has no content, so no comparison was made.")
    if generate is None:
        return empty(
            "Comparing a meeting against the approved requirements needs the "
            "configured AI model, and none is available, so no change was proposed. "
            "Set GEMINI_API_KEY to use change detection."
        )

    try:
        raw = generate(build_change_prompt(brd_data, transcript, source))
    except Exception as error:  # noqa: BLE001 - any client failure means the same thing
        # The exception type only. A client's message can carry request context that
        # has no business on a review screen.
        return empty(
            "The change detector could not be reached ({}), so no change was proposed "
            "from this source.".format(type(error).__name__)
        )

    try:
        rows = parse_change_response(raw)
    except DetectorResponseError as error:
        return empty(str(error))

    if not rows:
        return empty(
            "The detector reported no change between this source and the approved "
            "requirements."
        )

    return validate_changes(rows, brd_data, transcript, plan, created, source)


# --- The Jira path: an issue edited away from what this app sent -----------


def synchronized_baseline(plan=None, created=()) -> dict:
    """
    What this app last sent to Jira, keyed by the issue key Jira returned.

    Each entry is the summary and description of the ``PlannedIssue`` the reviewer
    approved, reconstructed with ``jira_processor.issue_description_text`` -- the same
    function that built the payload -- so drift is measured against what was actually
    sent rather than a second guess at it. An issue whose plan item is gone from this
    session has no baseline and is absent here: reporting drift with nothing to compare
    against would be a guess.
    """
    items = {issue.plan_key: issue for issue in (plan.issues if plan is not None else ())}
    baseline: dict = {}
    for record in created or ():
        if not (record.succeeded and record.issue_key):
            continue
        planned = items.get(record.plan_key)
        if planned is None:
            continue
        baseline[record.issue_key] = {
            "plan_key": record.plan_key,
            "summary": _line(planned.summary),
            "description": _block(issue_description_text(planned)),
            "requirement_ids": tuple(
                _norm_id(one) for one in record.source_requirement_ids if _line(one)
            ),
            "action_item_ids": tuple(
                _line(one) for one in record.source_action_item_ids if _line(one)
            ),
        }
    return baseline


def _same_text(left: str, right: str) -> bool:
    """Whether two texts say the same thing once whitespace is disregarded."""
    return " ".join(_block(left).split()) == " ".join(_block(right).split())


def _field_report(label: str, previous: str, current: str) -> tuple:
    return ("{}:\n{}".format(label, previous), "{}:\n{}".format(label, current))


def _description_requirement_text(value) -> str:
    """The first description paragraph, where this app writes the requirement text."""
    text = _block(value)
    for paragraph in re.split(r"\n\s*\n", text):
        candidate = _statement(paragraph)
        if candidate:
            return candidate
    return ""


def _stored_baseline(baseline) -> dict:
    """Keep only complete, stored Jira snapshots; malformed entries are unusable."""
    if not isinstance(baseline, dict):
        return {}

    kept = {}
    for raw_key, entry in baseline.items():
        key = _line(raw_key)
        if not key or not isinstance(entry, dict):
            continue
        requirement_ids = entry.get("requirement_ids")
        if not isinstance(requirement_ids, (tuple, list)):
            continue
        kept[key] = {
            "plan_key": _line(entry.get("plan_key")),
            "summary": _line(entry.get("summary")),
            "description": _block(entry.get("description")),
            "requirement_ids": tuple(
                _norm_id(one) for one in requirement_ids if _line(one)
            ),
            "action_item_ids": tuple(
                _line(one) for one in (entry.get("action_item_ids") or ()) if _line(one)
            ),
        }
    return kept


def detect_jira_changes(
    brd_data: BRDData, issues, plan=None, created=(), baseline=None, failures=()
) -> ChangeProposal:
    """
    Find mapped Jira issues that no longer match what this app sent. Changes nothing.

    ``issues`` is what ``JiraService.get_issue_fields`` returned for each issue: a
    ``issue_key``, a ``summary`` and a ``description`` still in Atlassian Document
    Format. The description is flattened with ``jira_processor.adf_to_text`` before it
    is compared, because Jira normalises its own markup and comparing raw documents
    would report a difference on every check.

    No model is involved. The candidate wording already exists -- somebody typed it into
    Jira -- so there is nothing to generate and nothing that could be fabricated. A Jira
    edit is never treated as the new business truth either: it becomes a pending
    proposal with the old requirement beside it, and a reviewer decides.

    Only issues with a stored baseline and exactly one linked BRD requirement can
    produce an approvable change. Everything else is reported as a note or held for
    manual review rather than guessed at.
    """
    baseline = (
        _stored_baseline(baseline)
        if baseline is not None
        else synchronized_baseline(plan, created)
    )
    index = requirement_index(brd_data)
    failed_keys = tuple(dict.fromkeys(_line(key) for key in (failures or ()) if _line(key)))

    changes: list = []
    notes: list = []
    unmapped: list = []
    no_baseline: list = []
    seen_issue_keys: set = set()
    checked = 0

    entries = issues if isinstance(issues, (tuple, list)) else ()
    for entry in entries[:MAX_ISSUES_COMPARED]:
        if not isinstance(entry, dict):
            continue
        key = _line(entry.get("issue_key"))
        if not key:
            continue
        if key in seen_issue_keys:
            continue
        seen_issue_keys.add(key)

        sent = baseline.get(key)
        if sent is None:
            no_baseline.append(key)
            continue
        checked += 1

        current_summary = _line(entry.get("summary"))
        current_description = _block(adf_to_text(entry.get("description")))
        summary_moved = not _same_text(sent["summary"], current_summary)
        description_moved = not _same_text(sent["description"], current_description)
        if not (summary_moved or description_moved):
            continue

        fields = []
        previous_parts = []
        current_parts = []
        if summary_moved:
            fields.append("summary")
            before, after = _field_report("Summary", sent["summary"], current_summary)
            previous_parts.append(before)
            current_parts.append(after)
        if description_moved:
            fields.append("description")
            before, after = _field_report(
                "Description", sent["description"], current_description
            )
            previous_parts.append(before)
            current_parts.append(after)

        requirement_ids = sent["requirement_ids"]
        if not requirement_ids:
            # A container issue, or one created from an action item. Its text drifting
            # says nothing about a requirement, and naming one anyway would invent the
            # mapping this feature exists to avoid.
            unmapped.append(key)
            continue

        known = [one for one in requirement_ids if one in index]
        if len(requirement_ids) != 1 or len(known) != 1:
            # A Jira issue can cover multiple requirements, but this requirement model
            # represents one approved BRD item. Selecting one would be a fabricated
            # mapping, so report the issue and leave every BRD requirement untouched.
            unmapped.append(key)
            continue
        requirement_id = known[0]
        old_text = _statement(index[requirement_id].statement)
        needs_review = False
        review_reason = ""

        proposed = ""
        if not needs_review:
            # Requirement issues are generated with their approved statement as the
            # first description paragraph. Prefer that wording when it changed; Jira
            # summaries can be shortened for the API limit and are the fallback only.
            description_requirement = _description_requirement_text(current_description)
            if description_moved and description_requirement and not _same_text(
                description_requirement, old_text
            ):
                proposed = description_requirement
            elif summary_moved and current_summary and not _same_text(current_summary, old_text):
                proposed = _statement(current_summary)
            else:
                needs_review = True
                review_reason = (
                    "The Jira description or summary changed, but no changed requirement "
                    "statement could be determined safely. Review the field values before "
                    "changing the BRD."
                )

        if proposed and _same_text(proposed, old_text):
            # The summary moved away from what was sent but still says what the
            # requirement says: the issue was reworded, the requirement was not.
            notes.append(
                "{} was edited but still matches requirement {}, so no requirement "
                "change is proposed.".format(key, requirement_id)
            )
            continue

        plan_keys, issue_keys = affected_work(requirement_id, plan, created)
        other = [one for one in issue_keys if one != key]
        impact = (
            "{} no longer matches the wording this app sent for requirement {}.".format(
                key, requirement_id or "(undetermined)"
            )
            if not needs_review
            else "{} no longer matches the wording this app sent for it.".format(key)
        )
        if other:
            impact += " {} other issue(s) restate the same requirement: {}.".format(
                len(other), ", ".join(other)
            )

        changes.append(
            RequirementChange(
                change_id="J-{}".format(key),
                source_type="jira",
                change_type="CHANGED",
                requirement_id=requirement_id,
                old_text=old_text,
                proposed_new_text=proposed,
                # A Jira edit is its own evidence: the field, and what it now holds.
                source_evidence="",
                source_reference=key,
                jira_issue_key=key,
                jira_field=", ".join(fields),
                jira_current_value="\n\n".join(current_parts),
                previous_value="\n\n".join(previous_parts),
                affected_plan_keys=plan_keys,
                affected_issue_keys=issue_keys or (key,),
                impact=impact,
                proposed_action=(
                    "Review this yourself. It cannot be approved as it stands."
                    if needs_review
                    else "Accept the Jira wording into the BRD, keep it in Jira only, or "
                    "reject it. Approving changes the BRD only — this app does not write "
                    "back to Jira."
                ),
                needs_manual_review=needs_review,
                review_reason=review_reason,
                approval_state="pending",
            )
        )

    if no_baseline:
        notes.append(
            "{} issue(s) have no recorded baseline in this session and were not "
            "compared: {}.".format(len(no_baseline), _sample(no_baseline))
        )
    if failed_keys:
        notes.append(
            "{} Jira issue(s) could not be read, so no drift decision was made for "
            "them: {}.".format(len(failed_keys), _sample(failed_keys))
        )
    if unmapped:
        notes.append(
            "{} edited issue(s) are not linked to a BRD requirement, so no requirement "
            "change is proposed for them: {}.".format(len(unmapped), _sample(unmapped))
        )
    if checked and not changes and not unmapped and not failed_keys:
        notes.append(
            "{} mapped issue(s) were compared and all still match what this app "
            "sent.".format(checked)
        )
    if not checked and not no_baseline:
        notes.append(
            "No issue created by this app in this session was available to compare."
        )

    return ChangeProposal(
        source_type="jira",
        source_reference="",
        changes=tuple(changes[:MAX_CHANGES]),
        notes=tuple(notes),
    )


# --- Decisions, and applying only what was approved ------------------------


def decide_change(
    proposal: ChangeProposal, change_id: str, state: str, note: str = ""
) -> ChangeProposal:
    """
    Record one reviewer decision. Applies nothing.

    A decision that is not one of ``jira_models.APPROVAL_STATES``, or an approval of a
    change that is not decidable, is ignored: this is the gate, so it refuses rather
    than trusts. Returns a new proposal -- changes are frozen, and rebuilding is what
    keeps a half-applied decision impossible.
    """
    if state not in APPROVAL_STATES:
        return proposal

    updated = []
    for change in proposal.changes:
        if change.change_id != change_id:
            updated.append(change)
            continue
        if state == "approved" and not change.is_decidable:
            updated.append(change)
            continue
        updated.append(replace(change, approval_state=state, reviewer_note=_line(note)))
    return replace(proposal, changes=tuple(updated))


def apply_approved_changes(brd_data: BRDData, proposal: ChangeProposal) -> tuple:
    """
    Apply the approved changes to a copy of the BRD. Returns ``(brd, applied)``.

    The only function in this feature that changes a requirement, and it changes
    nothing a reviewer did not approve: the loop is over ``proposal.approved``, which is
    ``approval_state == "approved"`` *and* decidable, so a pending, rejected, Jira-only,
    unchanged or manual-review change cannot reach it. A copy is returned rather than
    the BRD edited in place, so a caller that does not store the result has changed
    nothing.

    Jira is not touched. An approved change updates the BRD and the affected issues are
    reported for a separate decision -- one requirement changing is not consent to
    rewrite every issue that restates it.
    """
    updated = deepcopy(brd_data)
    applied: list = []

    for change in proposal.approved:
        if change.change_type == "NEW":
            updated.functional_requirements.append(
                Requirement(
                    requirement_id=change.requirement_id,
                    statement=change.proposed_new_text,
                    source_evidence=change.source_evidence,
                )
            )
            applied.append(change)
            continue

        target = None
        for group in (updated.functional_requirements, updated.non_functional_requirements):
            for requirement in group:
                if _norm_id(requirement.requirement_id) == change.requirement_id:
                    target = (group, requirement)
                    break
            if target is not None:
                break
        if target is None:
            # The requirement went away between detection and approval. Skipped rather
            # than re-added: the proposal describes an edit, not a creation.
            continue

        group, requirement = target
        if not _same_text(requirement.statement, change.old_text):
            # Detection describes the BRD as it stood then. Do not overwrite a newer
            # local edit merely because the proposal was approved before it changed.
            continue
        if change.change_type == "REMOVED_DEFERRED":
            group.remove(requirement)
            updated.deferred_or_out_of_scope_items.append(
                "{} — {}".format(requirement.requirement_id, requirement.statement)
            )
        else:
            requirement.statement = change.proposed_new_text
            if change.source_evidence:
                # The requirement now says what a later meeting decided, so its evidence
                # is that meeting's words. A Jira-driven change carries no quote, and the
                # original evidence is left in place rather than replaced with nothing.
                requirement.source_evidence = change.source_evidence
        applied.append(change)

    return (updated, tuple(applied))
