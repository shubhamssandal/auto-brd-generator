import os
import json
from typing import Optional

import streamlit as st
from dotenv import load_dotenv

from google import genai
from google.genai import types
from brd_models import (
    BRDData,
    Requirement,
    Stakeholder,
    ProjectOverview,
    Assumption,
    ActionItem,
    NormalizedTranscript,
)
from transcript_processor import (
    normalize_manual_notes,
    normalize_uploaded_file,
    TranscriptProcessingError,
)
from jira_models import ChangeProposal, CreatedIssue
from jira_change_detector import (
    apply_approved_changes,
    decide_change,
    detect_jira_changes,
    detect_meeting_changes,
    synchronized_baseline,
)
from jira_planner import action_item_index, generate_work_plan
from jira_processor import (
    compatible_issue_types,
    creation_order,
    delete_planned_issue,
    issue_browse_url,
    issue_creation_payload,
    set_planned_issue_type,
    update_planned_issue,
    validate_work_plan,
)
from jira_service import JiraService
from providers import (
    GoogleMeetProvider,
    MSTeamsProvider,
    OAuthHandshake,
    OAuthStateError,
    ProviderAPIError,
    ProviderAuthenticationError,
    ProviderConsentRequiredError,
    ProviderError,
    ProviderNotConfiguredError,
    ProviderTokenExpiredError,
    TokenSet,
    TranscriptParseError,
    TranscriptUnavailableError,
    begin_handshake,
    call_with_refresh,
    refresh_tokens,
    resolve_handshake,
)


# Load environment variables from a .env file
load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# --- Global Gemini Client Configuration ---
CLIENT = None

if GEMINI_API_KEY:
    CLIENT = genai.Client(api_key=GEMINI_API_KEY)

# The one model this app calls, named once so BRD generation and Jira work planning
# cannot drift onto different models.
GEMINI_MODEL = "gemini-3.6-flash"


def _gemini_json(prompt: str) -> str:
    """
    Send one prompt to the configured model and return its response text.

    Exists so the Jira planner can be handed a model call as a plain ``str -> str``
    function. ``jira_planner`` therefore holds no client, no API key and no import of
    this module: it cannot reach the network by itself, and it can be tested without a
    live model. Requires ``CLIENT``; callers check that through ``_planner_generate``.
    """
    response = CLIENT.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    return response.text or ""


def _planner_generate():
    """
    The model call the Jira planner should use, or ``None`` when none is configured.

    ``None`` is not a failure: ``generate_work_plan`` falls back to the deterministic
    one-to-one mapping and records why in the plan's notes, so the Jira step still
    works without a key.
    """
    return _gemini_json if CLIENT is not None else None


def _validate_requirements(
    requirements_data: list[dict], original_notes: str
) -> tuple[list[Requirement], list[Assumption]]:
    """Helper to validate a list of requirements against source notes."""
    validated_reqs = []
    new_assumptions = []
    for req_data in requirements_data:
        evidence = req_data.get("source_evidence", "").strip()
        if evidence and evidence in original_notes:
            validated_reqs.append(Requirement(**req_data))
        else:
            statement = req_data.get("statement", "N/A")
            category = "functional" if "FR" in req_data.get("requirement_id", "") else "non-functional"
            new_assumptions.append(
                Assumption(
                    statement=f"A {category} requirement was suggested but its evidence could not be verified in the notes: '{statement}'",
                    confirmation_needed="Verify if this is a true requirement and find supporting evidence in the source notes.",
                )
            )
    return validated_reqs, new_assumptions


def _parse_stakeholders(raw_stakeholders: list) -> list[Stakeholder]:
    """Safely parse stakeholder entries from dictionary or string formats."""
    parsed = []
    for s in raw_stakeholders:
        if isinstance(s, dict):
            parsed.append(Stakeholder(**s))
        elif isinstance(s, str):
            parsed.append(Stakeholder(name=s))
    return parsed


def _parse_action_items(raw_items: list) -> list[ActionItem]:
    """Safely parse action item entries from dictionary or string formats."""
    parsed = []
    for item in raw_items:
        if isinstance(item, dict):
            parsed.append(
                ActionItem(
                    item=item.get("item", item.get("task", item.get("statement", ""))),
                    owner=item.get("owner"),
                    due_date=item.get("due_date", item.get("deadline")),
                )
            )
        elif isinstance(item, str):
            parsed.append(ActionItem(item=item))
    return parsed


def validate_and_create_brd_data(data: dict, original_notes: str) -> BRDData:
    """
    Validates the extracted data from Gemini against the original notes
    and maps it to BRDData objects.
    """
    all_assumptions = []
    for a in data.get("assumptions", []):
        if isinstance(a, dict):
            all_assumptions.append(Assumption(**a))
        elif isinstance(a, str):
            all_assumptions.append(Assumption(statement=a, confirmation_needed="Stakeholder confirmation required."))

    validated_frs, fr_assumptions = _validate_requirements(data.get("functional_requirements", []), original_notes)
    validated_nfrs, nfr_assumptions = _validate_requirements(data.get("non_functional_requirements", []), original_notes)
    all_assumptions.extend(fr_assumptions)
    all_assumptions.extend(nfr_assumptions)

    overview_data = data.get("project_overview", {})
    project_overview = ProjectOverview(**overview_data) if isinstance(overview_data, dict) else ProjectOverview()

    brd_data = BRDData(
        project_title=data.get("project_title", "Untitled Project"),
        project_overview=project_overview,
        business_objectives=[str(x) for x in data.get("business_objectives", [])],
        stakeholders=_parse_stakeholders(data.get("stakeholders", [])),
        key_decisions=[str(x) for x in data.get("key_decisions", [])],
        functional_requirements=validated_frs,
        non_functional_requirements=validated_nfrs,
        acceptance_criteria=[str(x) for x in data.get("acceptance_criteria", [])],
        dependencies=[str(x) for x in data.get("dependencies", [])],
        risks=[str(x) for x in data.get("risks", [])],
        assumptions=all_assumptions,
        open_questions=[str(x) for x in data.get("open_questions", [])],
        deferred_or_out_of_scope_items=[str(x) for x in data.get("deferred_or_out_of_scope_items", [])],
        action_items=_parse_action_items(data.get("action_items", [])),
        background_information=[str(x) for x in data.get("background_information", [])],
    )
    return brd_data


def generate_brd_from_notes(notes: str) -> BRDData:
    """
    Sends meeting notes to the Gemini API and returns a structured BRDData object.
    """
    prompt = f"""
    You are an expert Business Analyst. Your task is to analyze the following meeting notes and extract key information to create a structured Business Requirements Document (BRD).

    The meeting notes are the absolute source of truth. Do not invent or infer information that isn't present in the notes. Preserve all uncertainty.

    Follow these rules strictly:
    1.  **Confirmed Requirements**: Only classify an item as a 'functional_requirement' or 'non_functional_requirement' if it is explicitly stated as a requirement. Each requirement MUST include a 'source_evidence' field containing a VERBATIM, COPY-PASTED SUBSTRING from the original notes that directly supports it. Do NOT paraphrase, summarize, or alter this evidence in any way. If no direct, verbatim evidence exists, do not create a requirement.
    2.  **Evidence Integrity**: The `source_evidence` must be a single, contiguous block of text copied exactly from the notes, including any original line breaks or indentation.
    3.  **Key Decisions**: Capture explicit decisions agreed upon during the meeting.
    4.  **Acceptance Criteria**: Capture testable conditions directly supported by confirmed requirements.
    5.  **Dependencies & Risks**: Capture technical/business dependencies and risks/blockers explicitly mentioned.
    6.  **Action Items**: Capture action items or next steps with owners/dates if stated in the notes.
    7.  **Open Questions**: Capture any points that are unclear, unresolved, or need follow-up as 'open_questions'.
    8.  **Out of Scope**: Capture anything explicitly deferred, parked, or excluded as 'deferred_or_out_of_scope_items'.
    9.  **Stakeholders**: List all mentioned individuals. For the `requirement` field in the JSON, only include direct asks or requests. For suggestions, recommendations, or technical flags (e.g., "probably worth a spike"), place them in the `concern` field instead.
    10. **Source of Truth**: Do not "clean up" or "fix" the notes. If the notes are ambiguous, reflect that ambiguity in your output, likely as an open question or assumption.

    Return the output as a single JSON object. Do not include any text or formatting before or after the JSON object. The JSON schema should be:

    {{
      "project_title": "Project Title from Notes (or 'Untitled Project')",
      "project_overview": {{
        "project_name": "Project Name if available",
        "date": "Date of meeting if available"
      }},
      "business_objectives": ["List of strings"],
      "stakeholders": [
        {{"name": "string", "role": "string", "concern": "string", "requirement": "string", "responsibility": "string"}}
      ],
      "key_decisions": ["List of agreed decisions mentioned in notes"],
      "functional_requirements": [
        {{"requirement_id": "FR-1", "statement": "The system shall...", "source_evidence": "Exact quote from notes"}}
      ],
      "non_functional_requirements": [
        {{"requirement_id": "NFR-1", "statement": "The system must...", "source_evidence": "Exact quote from notes"}}
      ],
      "acceptance_criteria": ["Testable acceptance criteria derived from confirmed requirements"],
      "dependencies": ["List of dependencies mentioned"],
      "risks": ["List of risks, technical spikes, or blockers mentioned"],
      "assumptions": [
        {{"statement": "What is being assumed", "confirmation_needed": "Who to confirm with or what to clarify"}}
      ],
      "open_questions": ["List of strings"],
      "deferred_or_out_of_scope_items": ["List of strings"],
      "action_items": [
        {{"item": "Task description", "owner": "Name or null", "due_date": "Date or null"}}
      ],
      "background_information": ["List of strings"]
    }}

    Here are the meeting notes:
    ---
    {notes}
    ---
    """

    response = CLIENT.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json"
        ),
    )
    data = json.loads(response.text)
    
    return validate_and_create_brd_data(data, notes)


def generate_brd_from_transcript(transcript: NormalizedTranscript) -> BRDData:
    """
    Passes a normalized transcript through the BRD generation and evidence validation pipeline.
    """
    brd_data = generate_brd_from_notes(transcript.raw_text)
    if transcript.meeting_title and brd_data.project_title == "Untitled Project":
        brd_data.project_title = transcript.meeting_title
    return brd_data


def display_requirements(
    title: str, requirements: list[Requirement], empty_message: str
) -> None:
    st.subheader(title)

    if not requirements:
        st.write(empty_message)
        return

    for requirement in requirements:
        st.markdown(f"**{requirement.requirement_id}:** {requirement.statement}")
        with st.container(border=True):
            st.markdown(f"**Source Evidence:**\n> {requirement.source_evidence}")


def convert_brd_to_markdown(brd_data: BRDData) -> str:
    """Converts the BRDData object into a Markdown string."""
    lines = [f"# {brd_data.project_title}\n"]

    # Project Overview
    lines.append("## 1. Project Overview")
    lines.append(f"**Project Name:** {brd_data.project_overview.project_name or 'Not specified in source notes'}")
    lines.append(f"**Date:** {brd_data.project_overview.date or 'Not specified in source notes'}")

    # Business Objectives
    lines.append("\n## 2. Business Objectives")
    if brd_data.business_objectives:
        for obj in brd_data.business_objectives:
            lines.append(f"- {obj}")
    else:
        lines.append("None specified in source notes.")

    # Stakeholders
    lines.append("\n## 3. Stakeholders")
    if brd_data.stakeholders:
        for s in brd_data.stakeholders:
            role = f" — {s.role}" if s.role else ""
            lines.append(f"### {s.name}{role}")
            if s.stated_interest:
                lines.append(f"- **Interest:** {s.stated_interest}")
            if s.concern:
                lines.append(f"- **Concern:** {s.concern}")
            if s.requirement:
                lines.append(f"- **Request:** {s.requirement}")
            if s.responsibility:
                lines.append(f"- **Responsibility:** {s.responsibility}")
    else:
        lines.append("None specified in source notes.")

    # Key Decisions
    if brd_data.key_decisions:
        lines.append("\n## 4. Key Decisions")
        for decision in brd_data.key_decisions:
            lines.append(f"- {decision}")

    # Scope
    lines.append("\n## 5. Scope")
    lines.append("### In Scope")
    lines.append("#### Functional Requirements")
    if brd_data.functional_requirements:
        for req in brd_data.functional_requirements:
            lines.append(f"**{req.requirement_id}:** {req.statement}")
            lines.append(f"> _Source Evidence: {req.source_evidence}_")
    else:
        lines.append("No confirmed functional requirements extracted.")
    
    lines.append("\n#### Non-Functional Requirements")
    if brd_data.non_functional_requirements:
        for req in brd_data.non_functional_requirements:
            lines.append(f"**{req.requirement_id}:** {req.statement}")
            lines.append(f"> _Source Evidence: {req.source_evidence}_")
    else:
        lines.append("No confirmed non-functional requirements extracted.")

    lines.append("\n### Deferred or Out of Scope")
    if brd_data.deferred_or_out_of_scope_items:
        for item in brd_data.deferred_or_out_of_scope_items:
            lines.append(f"- {item}")
    else:
        lines.append("None specified.")

    # Acceptance Criteria
    if brd_data.acceptance_criteria:
        lines.append("\n## 6. Acceptance Criteria")
        for ac in brd_data.acceptance_criteria:
            lines.append(f"- {ac}")

    # Dependencies & Risks
    if brd_data.dependencies or brd_data.risks:
        lines.append("\n## 7. Dependencies & Risks")
        if brd_data.dependencies:
            lines.append("### Dependencies")
            for dep in brd_data.dependencies:
                lines.append(f"- {dep}")
        if brd_data.risks:
            lines.append("### Risks & Technical Considerations")
            for risk in brd_data.risks:
                lines.append(f"- {risk}")

    # Assumptions
    lines.append("\n## 8. Assumptions")
    if brd_data.assumptions:
        for assumption in brd_data.assumptions:
            lines.append(f"- **Assumption:** {assumption.statement}")
            lines.append(f"  - **Confirmation Needed:** {assumption.confirmation_needed}")
    else:
        lines.append("None identified.")

    # Open Questions
    lines.append("\n## 9. Open Questions")
    if brd_data.open_questions:
        for q in brd_data.open_questions:
            lines.append(f"- {q}")
    else:
        lines.append("None identified.")

    # Action Items
    if brd_data.action_items:
        lines.append("\n## 10. Action Items & Next Steps")
        for act in brd_data.action_items:
            owner_info = f" (Owner: {act.owner})" if act.owner else ""
            due_info = f" [Due: {act.due_date}]" if act.due_date else ""
            lines.append(f"- [ ] {act.item}{owner_info}{due_info}")

    # Background Information
    if brd_data.background_information:
        lines.append("\n## 11. Background Information / Technical Context")
        for item in brd_data.background_information:
            lines.append(f"- {item}")

    return "\n".join(lines)


def display_brd(brd_data: BRDData):
    """Renders the entire BRD in the Streamlit UI."""
    st.header(brd_data.project_title)

    st.subheader("1. Project Overview")
    st.write(f"**Project Name:** {brd_data.project_overview.project_name or 'Not specified in source notes'}")
    st.write(f"**Date:** {brd_data.project_overview.date or 'Not specified in source notes'}")

    st.subheader("2. Business Objectives")
    if brd_data.business_objectives:
        for objective in brd_data.business_objectives:
            st.write(f"- {objective}")
    else:
        st.write("None specified in source notes.")

    st.subheader("3. Stakeholders")
    if brd_data.stakeholders:
        for stakeholder in brd_data.stakeholders:
            details = [stakeholder.name]
            if stakeholder.role:
                details.append(stakeholder.role)
            st.markdown(f"**{' — '.join(details)}**")
            if stakeholder.stated_interest:
                st.write(f"Interest: {stakeholder.stated_interest}")
            if stakeholder.concern:
                st.write(f"Concern: {stakeholder.concern}")
            if stakeholder.requirement:
                st.write(f"Request: {stakeholder.requirement}")
            if stakeholder.responsibility:
                st.write(f"Responsibility: {stakeholder.responsibility}")
    else:
        st.write("None specified in source notes.")

    if brd_data.key_decisions:
        st.subheader("4. Key Decisions")
        for decision in brd_data.key_decisions:
            st.success(f"**Decision:** {decision}")

    st.header("5. Scope")
    with st.container():
        st.subheader("In Scope")
        display_requirements(
            "Functional Requirements",
            brd_data.functional_requirements,
            "No confirmed functional requirements were extracted from the source notes.",
        )
        display_requirements(
            "Non-Functional Requirements",
            brd_data.non_functional_requirements,
            "No confirmed non-functional requirements were extracted from the source notes.",
        )
        st.subheader("Deferred or Out of Scope")
        if not brd_data.deferred_or_out_of_scope_items:
            st.write("No items were explicitly deferred or marked as out of scope.")
        for item in brd_data.deferred_or_out_of_scope_items:
            st.info(item)

    if brd_data.acceptance_criteria:
        st.subheader("6. Acceptance Criteria")
        for ac in brd_data.acceptance_criteria:
            st.markdown(f"- {ac}")

    if brd_data.dependencies or brd_data.risks:
        st.subheader("7. Dependencies & Risks")
        if brd_data.dependencies:
            st.markdown("**Dependencies:**")
            for dep in brd_data.dependencies:
                st.write(f"- {dep}")
        if brd_data.risks:
            st.markdown("**Risks & Technical Considerations:**")
            for risk in brd_data.risks:
                st.write(f"- {risk}")

    st.subheader("8. Assumptions")
    if brd_data.assumptions:
        for assumption in brd_data.assumptions:
            st.warning(f"**Assumption:** {assumption.statement}\n\n**Confirmation Needed:** {assumption.confirmation_needed}")
    else:
        st.write("None identified.")

    st.subheader("9. Open Questions")
    if brd_data.open_questions:
        for question in brd_data.open_questions:
            st.warning(question)
    else:
        st.write("None identified.")

    if brd_data.action_items:
        st.subheader("10. Action Items & Next Steps")
        for act in brd_data.action_items:
            owner_info = f" — **Owner:** {act.owner}" if act.owner else ""
            due_info = f" (Due: {act.due_date})" if act.due_date else ""
            st.markdown(f"- [ ] {act.item}{owner_info}{due_info}")

    if brd_data.background_information:
        st.subheader("11. Background Information / Technical Context")
        for item in brd_data.background_information:
            st.write(f"- {item}")



# --- Provider Session Helpers (Streamlit UI layer) ---
#
# Every provider shares this code path: connect -> discover -> select -> preview ->
# the one BRD pipeline below. Access and refresh tokens live only in Streamlit's
# server-side session state; they are never rendered, logged, or written to disk.

PROVIDER_CLASSES = {
    "google_meet": GoogleMeetProvider,
    "microsoft_teams": MSTeamsProvider,
    # Jira is not a transcript source, but it uses the same signed-state OAuth
    # callback, so registering it here is what routes a verified `?code=` back
    # to the right service.
    "jira": JiraService,
}


def _redirect_uri() -> str:
    """The registered OAuth redirect URI; must match the provider console entry."""
    return os.getenv("APP_REDIRECT_URI", "").strip() or "http://localhost:8501"


def _skey(provider_name: str, suffix: str) -> str:
    """Namespaced session-state key for one provider."""
    return f"{provider_name}__{suffix}"


def _flash(kind: str, message: str) -> None:
    """
    Queue a message for the next render.

    The OAuth callback clears the query string, which re-runs the script, so a
    message written directly at callback time would vanish before it was seen.
    """
    flashes = st.session_state.get("_flashes")
    if not isinstance(flashes, list):
        flashes = []
    flashes.append((kind, message))
    st.session_state["_flashes"] = flashes


def _render_flashes() -> None:
    flashes = st.session_state.get("_flashes")
    if not isinstance(flashes, list) or not flashes:
        return
    st.session_state["_flashes"] = []
    for kind, message in flashes:
        if kind == "success":
            st.success(message)
        elif kind == "warning":
            st.warning(message)
        elif kind == "info":
            st.info(message)
        else:
            st.error(message)


def _report_provider_error(display_name: str, error: Exception) -> None:
    """Explain a provider failure without exposing tokens or credential values."""
    if isinstance(error, ProviderNotConfiguredError):
        st.error(f"{display_name} is not configured: {error}")
    elif isinstance(error, ProviderConsentRequiredError):
        st.error(f"{display_name} refused this request as unauthorized. {error}")
    elif isinstance(error, ProviderTokenExpiredError):
        st.error(
            f"The {display_name} session expired and could not be renewed. "
            f"Please connect again. ({error})"
        )
    elif isinstance(error, ProviderAuthenticationError):
        st.error(f"{display_name} authentication failed: {error}")
    elif isinstance(error, TranscriptUnavailableError):
        st.warning(f"No transcript is available: {error}")
    elif isinstance(error, TranscriptParseError):
        st.error(f"The {display_name} transcript could not be parsed: {error}")
    elif isinstance(error, ProviderAPIError):
        st.error(f"{display_name} API error: {error}")
    elif isinstance(error, ProviderError):
        st.error(f"{display_name} error: {error}")
    else:
        st.error(f"Unexpected error while contacting {display_name}: {error}")


def _connected_tokens(provider) -> Optional[TokenSet]:
    tokens = st.session_state.get(_skey(provider.name, "tokens"))
    return tokens if isinstance(tokens, TokenSet) else None


# Session-state suffixes holding the Jira project selection, its create-metadata and
# the work plan derived from them. Named once because three separate events have to
# clear exactly this set: choosing a different site, re-querying the project list,
# and disconnecting.
_JIRA_PROJECT_SUFFIXES = (
    "projects",
    "projects_site",
    "project",
    "metadata",
    "metadata_for",
    "plan",
    "plan_for",
    # JIRA-007. "created" holds the per-issue outcome of a creation run and
    # "creating" is the in-flight guard that stops a rerun from writing twice. Both
    # belong to one plan against one target, so every path that drops a plan drops
    # them too -- otherwise a fresh plan would inherit another plan's results.
    "created",
    "creating",
    "confirm_create",
    # JIRA-010. "changes" holds a detected requirement-change proposal. Its impact
    # analysis names the plan keys and issue keys of this project, so it goes stale
    # for exactly the same reasons the plan does and is dropped alongside it.
    "changes",
    # Immutable values sent at issue-creation time. Jira drift is compared against
    # this snapshot, never against a plan the reviewer may later edit.
    "change_baseline",
)

# The four evidence sources this app ingests. Named once because two pickers offer
# them: the BRD generator above and requirement-change detection below, which compares
# a later meeting against an approved BRD. Both routes reach the same normalizers.
TRANSCRIPT_SOURCES = (
    "Manual Paste",
    "Upload Transcript File (.txt)",
    "Google Meet",
    "Microsoft Teams",
)

# The reviewed BRD, kept so it survives the script re-run that every button click
# causes. Without it a work plan could never be generated: by the time the click is
# handled, the run that produced the BRD is over.
BRD_SESSION_KEY = "brd_data"

# Streamlit widget keys for the work-plan review editors. Not under ``jira__``:
# those suffixes are plan data, and a leftover text-input value would otherwise
# outlive the plan it described.
_JIRA_REVIEW_WIDGET_PREFIX = "jira_review__"


def _clear_jira_plan_review_widgets() -> None:
    """Drop review-editor widget state so a new or absent plan cannot inherit it."""
    for key in list(st.session_state.keys()):
        if str(key).startswith(_JIRA_REVIEW_WIDGET_PREFIX):
            st.session_state.pop(key, None)


def _disconnect(provider) -> None:
    """Drop every trace of the provider session from this browser session."""
    suffixes = ("tokens", "handshake", "discovery", "transcript", "identity", "sites", "site")
    for suffix in suffixes + _JIRA_PROJECT_SUFFIXES:
        st.session_state.pop(_skey(provider.name, suffix), None)
    _clear_jira_plan_review_widgets()


def _clear_jira_project_state(service) -> None:
    """
    Forget the project list, the project selection and its metadata.

    The picker's own widget state goes too: a shorter new list would leave a
    stored index pointing past the end of it.
    """
    for suffix in _JIRA_PROJECT_SUFFIXES:
        st.session_state.pop(_skey(service.name, suffix), None)
    st.session_state.pop("select_jira_project", None)
    _clear_jira_plan_review_widgets()


# Session-state namespace for Jira. Spelled once so the work-plan panel can address
# its own keys without being handed the service object: with no service and no token
# in scope, that panel has no way to reach Jira at all.
JIRA_STATE_NAME = JiraService().name


def _store_brd(brd_data: BRDData) -> None:
    """
    Keep the generated BRD for the optional Jira step.

    Required because every button click re-runs this script: by the time a
    "Generate Jira Work Plan" click is handled, the run that produced the BRD is
    over and its local variable is gone. Nothing else about BRD generation,
    validation, display or export changes.

    A plan built from the previous BRD is dropped rather than left behind. A work
    plan is a proposal about one specific BRD, so a newly generated BRD makes a
    cached plan wrong, not merely old. A detected requirement-change proposal goes
    with it: it compares against the requirements that were approved when it ran.
    """
    st.session_state[BRD_SESSION_KEY] = brd_data
    for suffix in (
        "plan",
        "plan_for",
        "created",
        "creating",
        "confirm_create",
        "changes",
        "change_baseline",
    ):
        st.session_state.pop(_skey(JIRA_STATE_NAME, suffix), None)
    _clear_jira_plan_review_widgets()


def _handle_oauth_callback() -> None:
    """
    Process an OAuth redirect back to this page.

    Denials are reported and dropped. A ``code`` is only exchanged after the signed
    ``state`` verifies, which is both the CSRF check and the provider routing: the
    state is HMAC-signed by this process, so a forged or replayed value is rejected
    and no token is ever stored for it.
    """
    query_params = st.query_params

    if "error" in query_params:
        error_code = str(query_params.get("error", "")).strip()
        description = str(query_params.get("error_description", "")).strip()
        if error_code == "access_denied":
            _flash(
                "warning",
                "Authorization was declined, so no account was connected. "
                "Manual Paste and TXT Upload are unaffected.",
            )
        else:
            _flash(
                "error",
                "The provider returned an authorization error"
                + (f" ({error_code})" if error_code else "")
                + "."
                + (f" {description}" if description else ""),
            )
        st.query_params.clear()
        return

    if "code" not in query_params:
        return

    oauth_code = str(query_params.get("code", ""))
    oauth_state = str(query_params.get("state", ""))

    try:
        handshake = resolve_handshake(oauth_state)
    except OAuthStateError as e:
        # Unverifiable state: a forged/replayed callback, a stale link, or an app
        # restart. Nothing is exchanged and no session is created.
        _flash(
            "error",
            f"This sign-in could not be verified ({e}), so it was rejected. "
            "Start the connection again from this page.",
        )
        st.query_params.clear()
        return

    provider_cls = PROVIDER_CLASSES.get(handshake.provider)
    if provider_cls is None:
        _flash("error", "The sign-in response named an unknown provider and was ignored.")
        st.query_params.clear()
        return

    provider = provider_cls()
    try:
        raw_tokens = provider.exchange_code_for_token(
            oauth_code,
            _redirect_uri(),
            code_verifier=handshake.code_verifier,
        )
        tokens = TokenSet.from_response(raw_tokens)
        st.session_state[_skey(handshake.provider, "tokens")] = tokens
        st.session_state.pop(_skey(handshake.provider, "handshake"), None)
        st.session_state.pop(_skey(handshake.provider, "identity"), None)
        hint = getattr(
            provider,
            "post_connect_hint",
            f"Select **{provider.display_name}** under Transcript Source to continue.",
        )
        message = f"Connected to {provider.display_name}. {hint}"
        if not tokens.can_refresh():
            message += (
                " Note: no refresh token was issued, so this session ends when the "
                "access token expires."
            )
        _flash("success", message)
    except Exception as e:
        _flash("error", f"{provider.display_name} sign-in could not be completed: {e}")
    finally:
        st.query_params.clear()


def _provider_call(provider, tokens: TokenSet, operation, spinner_text: str):
    """
    Run one provider operation with expiry-aware refresh and a single 401 retry.

    Returns None and renders an explanation if the call fails. A dead session is
    cleared so the UI offers "Connect" again instead of looping on a bad token.
    """
    try:
        with st.spinner(spinner_text):
            result, refreshed = call_with_refresh(provider, tokens, operation)
        st.session_state[_skey(provider.name, "tokens")] = refreshed
        return result
    except ProviderConsentRequiredError as e:
        _report_provider_error(provider.display_name, e)
        return None
    except ProviderAuthenticationError as e:
        _disconnect(provider)
        _report_provider_error(provider.display_name, e)
        return None
    except Exception as e:
        _report_provider_error(provider.display_name, e)
        return None


# Shown under a Connect button unless the service overrides `connect_caption`.
DEFAULT_CONNECT_CAPTION = (
    "You will authorize read-only access on the provider's own sign-in page and be "
    "returned here. The request carries a signed, single-use state value and a PKCE "
    "challenge; your credentials are never seen by this app."
)


def _identity_label(provider, tokens: TokenSet) -> Optional[str]:
    """Best-effort 'signed in as' label. Never shows any token material."""
    key = _skey(provider.name, "identity")
    if key in st.session_state:
        return st.session_state[key]

    try:
        profile = provider.get_user_profile(tokens.access_token) or {}
    except Exception:
        profile = {}

    label = None
    for field in ("email", "mail", "userPrincipalName", "displayName", "name"):
        value = str(profile.get(field) or "").strip()
        if value:
            label = value
            break

    st.session_state[key] = label
    return label


def _render_connect_button(provider) -> None:
    """
    Render the authorization link, issuing a fresh signed state + PKCE pair.

    The PKCE challenge is offered to every service; one that does not document
    PKCE support simply ignores it and overrides ``connect_caption`` so the UI
    does not claim a protection that was not used. The signed state is
    unconditional.
    """
    handshake_key = _skey(provider.name, "handshake")
    handshake = st.session_state.get(handshake_key)
    if not isinstance(handshake, OAuthHandshake) or handshake.is_stale():
        handshake = begin_handshake(provider.name)
        st.session_state[handshake_key] = handshake

    try:
        auth_url = provider.get_authorization_url(
            _redirect_uri(),
            state=handshake.state,
            code_challenge=handshake.code_challenge,
            code_challenge_method=handshake.code_challenge_method,
        )
    except ProviderError as e:
        _report_provider_error(provider.display_name, e)
        return

    st.link_button(f"Connect {provider.display_name}", auth_url, type="primary")
    st.caption(getattr(provider, "connect_caption", DEFAULT_CONNECT_CAPTION))


def _render_connected_panel(provider, tokens: TokenSet) -> None:
    summary = tokens.public_summary()
    left, right = st.columns([4, 1])

    with left:
        st.success(f"Connected to {provider.display_name}")
        identity = _identity_label(provider, tokens)
        if identity:
            st.caption(f"Signed in as {identity}")
        remaining = summary.get("expires_in_seconds")
        if remaining is None:
            st.caption("Access token: the provider advertised no expiry.")
        else:
            st.caption(
                "Access token expires in ~{} minute(s). Automatic renewal: {}.".format(
                    max(0, int(remaining) // 60),
                    "available" if summary.get("has_refresh_token") else "unavailable",
                )
            )

    with right:
        if st.button("Disconnect", key=f"disconnect_{provider.name}"):
            _disconnect(provider)
            st.rerun()

    if summary.get("scopes"):
        with st.expander("Granted scopes"):
            for scope in summary["scopes"]:
                st.write(f"- `{scope}`")

    st.caption(
        "Tokens stay in this session's server-side state only. They are never displayed, "
        "logged, or saved to disk."
    )


def _render_entry_metadata(entry: dict) -> None:
    """Show exactly what the provider reported for the selected meeting."""
    title = entry.get("title")
    date = entry.get("date")
    participants = entry.get("participants") or []

    st.write(f"**Meeting title:** {title if title else '_not provided by this API_'}")
    st.write(f"**Date / time:** {date if date else '_not provided by this API_'}")
    if participants:
        st.write("**Participants:** " + ", ".join(str(p) for p in participants))
    else:
        st.caption("Participants: not provided by this API for this meeting.")

    details = {k: v for k, v in (entry.get("details") or {}).items() if v not in (None, "", [], {})}
    if details:
        with st.expander("Provider metadata"):
            st.json(details)


def _render_discovery_panel(provider, tokens: TokenSet) -> None:
    """Steps 1 and 2: list what the account can reach, then load one transcript."""
    name = provider.name
    discovery_key = _skey(name, "discovery")

    st.markdown("**Step 1 — find meetings with transcripts**")
    if st.button("Retrieve available meetings & transcripts", key=f"discover_{name}"):
        result = _provider_call(
            provider,
            tokens,
            lambda token: provider.discover_transcripts(access_token=token),
            f"Querying {provider.display_name} for meetings and transcripts...",
        )
        if result is not None:
            st.session_state[discovery_key] = result

    discovery = st.session_state.get(discovery_key)
    if not isinstance(discovery, dict):
        return

    for note in discovery.get("notes") or []:
        st.info(note)
    if discovery.get("truncated"):
        st.warning(
            "Not every meeting could be listed in one pass — see the notes above for exactly "
            "what was skipped. Nothing was dropped silently."
        )

    entries = [e for e in (discovery.get("transcripts") or []) if isinstance(e, dict)]
    if not entries:
        st.warning(
            f"{provider.display_name} returned no retrievable transcripts for this account."
        )
        return

    st.markdown("**Step 2 — choose a transcript**")
    indices = list(range(len(entries)))
    chosen = st.selectbox(
        f"Transcripts available to this account ({len(entries)} found)",
        indices,
        format_func=lambda i: str(
            entries[i].get("display_label") or entries[i].get("title") or entries[i].get("id")
        ),
        key=f"select_{name}",
    )
    entry = entries[chosen]
    _render_entry_metadata(entry)

    if not entry.get("available", True):
        st.warning(
            "The provider reports this transcript is not ready to download yet. "
            "Loading it may fail until the file has been generated."
        )

    if st.button("Load transcript", key=f"load_{name}"):
        loaded = _load_transcript(provider, tokens, entry.get("id"), entry.get("title"))
        if loaded is not None:
            st.session_state[_skey(name, "transcript")] = loaded
            st.rerun()


def _load_transcript(
    provider,
    tokens: TokenSet,
    identifier,
    meeting_title: Optional[str] = None,
) -> Optional[NormalizedTranscript]:
    """Retrieve one transcript and normalize it into the shared model."""
    resource = str(identifier or "").strip()
    if not resource:
        st.error("No transcript identifier was selected.")
        return None

    transcript = _provider_call(
        provider,
        tokens,
        lambda token: provider.get_transcript(resource, access_token=token),
        f"Retrieving the transcript from {provider.display_name}...",
    )
    if transcript is None:
        return None

    # Carry across the real title the provider reported during discovery when the
    # transcript resource itself has none. Nothing is invented here.
    if meeting_title and not transcript.meeting_title:
        transcript.meeting_title = str(meeting_title)
    return transcript


def _render_manual_fallback(provider, tokens: TokenSet) -> None:
    """Identifier entry for meetings discovery cannot reach."""
    name = provider.name
    if name == "google_meet":
        label = "Conference record, transcript resource, or Drive document id"
        placeholder = "conferenceRecords/abc-123 or a Google Drive document id"
        help_text = (
            "Use this when you already know the identifier: a Meet conference record "
            "(`conferenceRecords/...`), a transcript resource "
            "(`conferenceRecords/.../transcripts/...`), or the id of the Google Docs "
            "transcript file in Drive."
        )
    else:
        label = "Teams join URL or transcript resource"
        placeholder = "https://teams.microsoft.com/l/meetup-join/... or {meetingId}/transcripts/{transcriptId}"
        help_text = (
            "Use this for meetings that are not on your calendar. Microsoft Graph can only "
            "look up an online meeting by its join URL, so paste the full "
            "'Join Microsoft Teams Meeting' link."
        )

    with st.expander("Or enter an identifier manually"):
        st.caption(help_text)
        value = st.text_input(label, placeholder=placeholder, key=f"manual_{name}")
        if st.button("Load transcript from identifier", key=f"manual_load_{name}"):
            if not value.strip():
                st.error("Enter an identifier first.")
            else:
                loaded = _load_transcript(provider, tokens, value.strip())
                if loaded is not None:
                    st.session_state[_skey(name, "transcript")] = loaded
                    st.rerun()


def _render_loaded_transcript(provider) -> Optional[NormalizedTranscript]:
    """
    Step 3: preview the retrieved transcript and hand it to the shared pipeline.

    Returns the transcript only when the user presses Generate, so provider
    transcripts enter exactly the same pipeline as pasted and uploaded notes.
    """
    name = provider.name
    transcript = st.session_state.get(_skey(name, "transcript"))
    if not isinstance(transcript, NormalizedTranscript):
        return None

    st.divider()
    st.markdown("**Step 3 — review, then generate**")

    columns = st.columns(3)
    columns[0].metric(
        "Characters", int(transcript.metadata.get("char_count") or len(transcript.raw_text))
    )
    columns[1].metric(
        "Lines",
        int(transcript.metadata.get("line_count") or len(transcript.raw_text.splitlines())),
    )
    columns[2].metric("Speakers named", len(transcript.participants))

    if transcript.meeting_title:
        st.write(f"**Meeting:** {transcript.meeting_title}")
    if transcript.meeting_date:
        st.write(f"**Date:** {transcript.meeting_date}")
    if transcript.participants:
        st.write("**Speakers:** " + ", ".join(transcript.participants))
    else:
        st.caption("No speaker names were provided by the source for this transcript.")

    if transcript.metadata.get("truncation_warning"):
        st.warning(transcript.metadata["truncation_warning"])
    if transcript.metadata.get("speaker_attribution_note"):
        st.info(transcript.metadata["speaker_attribution_note"])
    if transcript.metadata.get("unresolved_participant_count"):
        st.info(
            "{} speaker(s) could not be named by the provider and appear as neutral "
            "placeholders such as 'Speaker 1'.".format(
                transcript.metadata["unresolved_participant_count"]
            )
        )

    with st.expander("Preview transcript", expanded=True):
        st.text_area(
            "Transcript preview",
            value=transcript.raw_text,
            height=250,
            disabled=True,
            key=f"preview_{name}",
        )

    left, right = st.columns([2, 1])
    with left:
        generate = st.button(
            "Generate BRD from this transcript", key=f"generate_{name}", type="primary"
        )
    with right:
        if st.button("Clear loaded transcript", key=f"clear_{name}"):
            st.session_state.pop(_skey(name, "transcript"), None)
            st.rerun()

    return transcript if generate else None


def _render_jira_sites_panel(service, tokens: TokenSet) -> None:
    """
    Pick which Jira Cloud site to work with. Read-only.

    Reuses ``_provider_call``, so the spinner, the single refresh-and-retry on an
    expired token, and the authentication / authorization / API error messages are
    the same ones the transcript providers already use. Only the chosen site's
    non-secret identifiers are cached -- see ``JiraSite``.
    """
    sites_key = _skey(service.name, "sites")
    site_key = _skey(service.name, "site")

    st.markdown("**Step 1 — choose a Jira site**")
    if st.button("Retrieve accessible Jira sites", key="discover_jira_sites"):
        result = _provider_call(
            service,
            tokens,
            lambda token: service.list_accessible_sites(access_token=token),
            "Asking Atlassian which Jira sites this account can reach...",
        )
        if result is not None:
            st.session_state[sites_key] = result
            # A choice made against the previous list must not survive a re-query,
            # and the picker's own state has to go too: the new list may be shorter.
            st.session_state.pop(site_key, None)
            st.session_state.pop("select_jira_site", None)
            # Whatever was selected here, the project list below was fetched for a
            # site that may no longer be the selected one.
            _clear_jira_project_state(service)

    sites = st.session_state.get(sites_key)
    if not isinstance(sites, list):
        st.caption("Nothing has been requested from Jira yet. This only reads a list of sites.")
        return

    if not sites:
        st.session_state.pop(site_key, None)
        granted = tuple(tokens.public_summary().get("scopes") or ())
        if granted and service.SITE_SCOPE not in granted:
            st.warning(
                "Atlassian returned no Jira sites, and this session was authorized without "
                "the `{}` scope. That endpoint only reports sites the token holds a Jira "
                "scope for, so this session cannot see any site regardless of what the "
                "account owns. Disconnect and connect again to consent to the scope this "
                "app now requests.".format(service.SITE_SCOPE)
            )
        else:
            st.warning(
                "This Atlassian account granted access to no Jira Cloud site. If you expected "
                "one, check that you selected a site on Atlassian's consent screen — only the "
                "sites chosen there are reported — then disconnect and connect again."
            )
        return

    if len(sites) == 1:
        site = sites[0]
        st.caption("One accessible site was returned, so it is selected automatically.")
    else:
        indices = list(range(len(sites)))
        chosen = st.selectbox(
            "Jira sites accessible to this account ({} found)".format(len(sites)),
            indices,
            format_func=lambda i: sites[i].display_label,
            key="select_jira_site",
        )
        site = sites[chosen]

    st.session_state[site_key] = site
    st.success("Selected Jira site: {}".format(site.display_label))

    if site.scopes:
        with st.expander("Scopes this authorization holds on the selected site"):
            for scope in site.scopes:
                st.write(f"- `{scope}`")
        st.caption(
            "Atlassian documents that this reflects the token's scopes, not your Jira "
            "permissions, so it is not a promise about what you can see inside the site."
        )


def _render_jira_projects_panel(service, tokens: TokenSet, site) -> None:
    """
    Steps 2 and 3: pick a project on the selected site, then read what creating an
    issue in it would require. Read-only.

    Reuses ``_provider_call`` for the spinner, the single refresh-and-retry on an
    expired token, and the authentication / authorization / API error messages, so
    Jira behaves like the transcript providers. Only non-secret identifiers and
    discovered metadata are cached -- see ``JiraProject`` and
    ``JiraProjectMetadata``.
    """
    projects_key = _skey(service.name, "projects")
    projects_site_key = _skey(service.name, "projects_site")
    project_key = _skey(service.name, "project")
    metadata_key = _skey(service.name, "metadata")
    metadata_for_key = _skey(service.name, "metadata_for")

    # A project list belongs to one site. If the site selection moved, the cached
    # list and everything derived from it describe the wrong site.
    if st.session_state.get(projects_site_key) not in (None, site.id):
        _clear_jira_project_state(service)

    st.markdown("**Step 2 — choose a Jira project**")
    if st.button("Retrieve projects on this site", key="discover_jira_projects"):
        result = _provider_call(
            service,
            tokens,
            lambda token: service.list_projects(access_token=token, cloud_id=site.id),
            "Asking Jira which projects this account can see...",
        )
        if result is not None:
            _clear_jira_project_state(service)
            st.session_state[projects_key] = result
            st.session_state[projects_site_key] = site.id

    discovery = st.session_state.get(projects_key)
    if not isinstance(discovery, dict):
        st.caption(
            "No project has been requested yet. This reads a list of projects and creates "
            "nothing."
        )
        return

    for note in discovery.get("notes") or []:
        st.info(note)

    projects = [p for p in (discovery.get("projects") or []) if p is not None]
    if not projects:
        st.session_state.pop(project_key, None)
        granted = tuple(tokens.public_summary().get("scopes") or ())
        if granted and service.PROJECT_SCOPE not in granted:
            st.warning(
                "Jira returned no project, and this session was authorized without the "
                "`{}` scope — the one that grants project and issue data. Disconnect and "
                "connect again to consent to the scopes this app now requests.".format(
                    service.PROJECT_SCOPE
                )
            )
        else:
            st.warning(
                "Jira returned no project on this site. Atlassian documents that a Jira "
                "account's own permissions still apply whatever scopes were granted, so "
                "this normally means the connected account cannot browse any project here. "
                "Ask a Jira administrator for Browse Projects permission on the project you "
                "need, or select a different site."
            )
        return

    if len(projects) == 1:
        project = projects[0]
        st.caption("One project was returned, so it is selected automatically.")
    else:
        indices = list(range(len(projects)))
        chosen = st.selectbox(
            "Projects visible to this account on this site ({} found)".format(len(projects)),
            indices,
            format_func=lambda i: projects[i].display_label,
            key="select_jira_project",
        )
        project = projects[chosen]

    st.session_state[project_key] = project
    st.success("Selected Jira project: {}".format(project.display_label))
    if project.project_type_key:
        st.caption("Jira reports this project's type as `{}`.".format(project.project_type_key))

    # Metadata describes one project on one site, so it is cached against both.
    wanted = (site.id, project.api_identifier)
    if st.session_state.get(metadata_for_key) not in (None, wanted):
        st.session_state.pop(metadata_key, None)
        st.session_state.pop(metadata_for_key, None)

    st.markdown("**Step 3 — check what this project requires**")
    if st.button("Read issue types & required fields", key="discover_jira_metadata"):
        result = _provider_call(
            service,
            tokens,
            lambda token: service.get_project_metadata(
                access_token=token,
                cloud_id=site.id,
                project_id_or_key=project.api_identifier,
            ),
            "Reading issue types and required fields for {}...".format(project.display_label),
        )
        if result is not None:
            st.session_state[metadata_key] = result
            st.session_state[metadata_for_key] = wanted

    metadata = st.session_state.get(metadata_key)
    if metadata is None:
        st.caption(
            "Not read yet. This reads the issue types Jira offers on this project's create "
            "screen and the fields it marks required. It creates nothing."
        )
        return

    _render_jira_project_metadata(metadata)
    _render_jira_work_plan_panel(project, metadata, wanted)
    _render_jira_creation_panel(service, tokens, site, project, metadata)


def _render_jira_project_metadata(metadata) -> None:
    """
    Show the discovered issue types and the required-field check.

    Nothing here names a hierarchy level: what Jira reported is what is shown,
    because the target project's hierarchy is the project's own business.
    """
    for note in metadata.notes or ():
        st.info(note)
    if metadata.truncated:
        st.warning(
            "Not every issue type or field could be read in one pass — the notes above say "
            "exactly what was skipped. Nothing was dropped silently."
        )

    if not metadata.issue_types:
        st.warning(
            "Jira reported no issue type for this project's create screen. That is a Jira "
            "project configuration or permission matter, not something this app can change."
        )
        return

    plannable = len(metadata.plannable_issue_types)
    st.write(
        "**{} issue type(s) discovered, {} of which passed the required-field check.** "
        "Jira's own configuration decides these and their hierarchy; none of it is "
        "assumed here.".format(len(metadata.issue_types), plannable)
    )

    icons = {"ok": "✅", "blocked": "⚠️", "unknown": "❓"}
    for issue_type in metadata.issue_types:
        state = issue_type.validation_state
        header = "{} {} — {}".format(
            icons.get(state, "❓"),
            issue_type.name or issue_type.id,
            issue_type.hierarchy_note,
        )
        with st.expander(header):
            if issue_type.description:
                st.caption(issue_type.description)

            if state == "unknown":
                st.warning(
                    "The required fields for this issue type could not be read, so it has "
                    "not been checked."
                )
            elif state == "blocked":
                st.warning(
                    "Jira requires field(s) here that a generated work plan has no source "
                    "for: "
                    + ", ".join(
                        "`{}`".format(field.field_id)
                        for field in issue_type.unsupported_required_fields
                    )
                )
            else:
                st.success(
                    "Every field Jira requires here is one a work plan could supply, or one "
                    "Jira defaults by itself."
                )

            if issue_type.required_fields:
                st.write("Fields Jira marks required:")
                for field in issue_type.required_fields:
                    st.write(
                        "- `{}` — {}{}{}".format(
                            field.field_id,
                            field.name or "no name reported",
                            " ({})".format(field.schema_type) if field.schema_type else "",
                            " · Jira supplies a default" if field.has_default_value else "",
                        )
                    )
            elif state == "ok":
                st.write("Jira marked no field as required for this issue type.")

    st.caption(
        "This checks Jira's required fields against the fields a later step would be able "
        "to fill in. It is a pre-flight check, not a guarantee that Jira will accept a "
        "given issue. Nothing has been created or changed in Jira."
    )


def _render_jira_work_plan_panel(project, metadata, scope) -> None:
    """
    Step 4: propose Jira issues from the reviewed BRD. Creates nothing.

    The plan comes from ``jira_planner.generate_work_plan``, which asks the configured
    model how the approved requirements should be grouped into this project's own
    hierarchy and then validates that answer deterministically. With no model
    configured -- or a model that returns nothing usable -- it falls back to the
    one-to-one mapping and says so in the plan's notes, so this panel behaves the same
    either way.

    Deliberately takes no service and no token. With neither in scope this panel has
    no way to reach Jira, so "generating a plan creates nothing" is a property of the
    code rather than a promise in a caption. ``scope`` is the (site, project) pair the
    metadata above was read for, used to drop a plan that describes a different
    target.

    Review edits stay in this browser session. Creating issues in Jira is a later,
    explicit step: this panel still takes no service and no token.
    """
    plan_key = _skey(JIRA_STATE_NAME, "plan")
    plan_for_key = _skey(JIRA_STATE_NAME, "plan_for")

    st.markdown("**Step 4 — generate a Jira work plan**")

    brd_data = st.session_state.get(BRD_SESSION_KEY)
    if not isinstance(brd_data, BRDData):
        st.caption(
            "No BRD is available yet. A work plan restates reviewed BRD requirements as "
            "proposed Jira issues, so generate a BRD above first."
        )
        return

    # A plan describes one BRD against one project's issue types. If the target moved,
    # the cached plan is about something else.
    if st.session_state.get(plan_for_key) not in (None, scope):
        for suffix in (
            "plan",
            "plan_for",
            "created",
            "creating",
            "confirm_create",
            "changes",
            "change_baseline",
        ):
            st.session_state.pop(_skey(JIRA_STATE_NAME, suffix), None)
        _clear_jira_plan_review_widgets()

    if st.button("Generate Jira Work Plan", key="generate_jira_work_plan"):
        _clear_jira_plan_review_widgets()
        # A regenerated plan is a different proposal, so a previous run's results and
        # its in-flight guard must not carry over onto it. A change proposal goes too:
        # its impact analysis names plan keys this new plan may not contain.
        for suffix in (
            "created",
            "creating",
            "confirm_create",
            "changes",
            "change_baseline",
        ):
            st.session_state.pop(_skey(JIRA_STATE_NAME, suffix), None)
        with st.spinner("Grouping BRD requirements into this project's hierarchy..."):
            st.session_state[plan_key] = generate_work_plan(
                brd_data, project, metadata, generate=_planner_generate()
            )
        st.session_state[plan_for_key] = scope

    plan = st.session_state.get(plan_key)
    if plan is None:
        st.caption(
            "Not generated yet. This proposes issues from the BRD using only the issue "
            "types checked above, and creates nothing in Jira."
        )
        return

    _render_work_plan(plan, metadata, project)


def _persist_work_plan(plan):
    st.session_state[_skey(JIRA_STATE_NAME, "plan")] = plan
    return plan


def _render_work_plan(plan, metadata, project=None) -> None:
    """
    Show the proposal, parent above child, and let the reviewer edit it.

    Issue types are named as Jira named them for this project. Nothing here is
    renamed into Epic/Story/Task: the levels shown are the ones the project reported.
    """
    for note in plan.notes:
        st.info(note)

    if plan.is_empty:
        st.warning(
            "No issue could be proposed for this project. The notes above say why. "
            "Nothing has been created in Jira."
        )
        return

    st.success(
        "Proposed {} issue(s) for {}, as {}. Nothing has been created in Jira.".format(
            len(plan.issues),
            plan.project_label or plan.project_identifier,
            ", ".join("`{}`".format(name) for name in plan.issue_type_names)
            or "the issue types checked above",
        )
    )

    for root in plan.roots:
        plan = _render_planned_issue(plan, root, 0, metadata)

    for message in validate_work_plan(plan, metadata, project):
        st.error(message)

    st.caption(
        "Proposal only — nothing has been created in Jira. Edits, selection and deletion "
        "stay in this session and do not create issues."
    )


def _criteria_text(criteria) -> str:
    return "\n".join(str(item) for item in criteria if str(item).strip())


def _criteria_from_text(text: str) -> tuple:
    return tuple(line.strip() for line in str(text or "").splitlines() if line.strip())


def _linked_action_items(issue) -> list:
    """
    One issue's linked action items as ``(id, text)`` pairs.

    The plan stores ids, because an id is what survives a round trip through session
    state and into a Jira description; the text is what a reviewer actually needs to
    read. It is resolved against the BRD still in session, and the id is shown alone
    if that BRD has been replaced.
    """
    identifiers = list(issue.source_action_item_ids)
    if not identifiers:
        return []
    brd_data = st.session_state.get(BRD_SESSION_KEY)
    index = action_item_index(brd_data) if isinstance(brd_data, BRDData) else {}
    return [
        (identifier, str(getattr(index.get(identifier), "item", "") or ""))
        for identifier in identifiers
    ]


def _render_issue_traceability(issue) -> None:
    """
    Where one proposed issue came from in the BRD. Read-only.

    Read-only deliberately: an issue's sources are the record of what justifies
    proposing it, so they are shown rather than offered as editable fields --
    ``jira_processor._EDITABLE_ISSUE_FIELDS`` excludes them for the same reason. A
    reviewer who disagrees with the traceability deletes the issue rather than
    rewriting its provenance.

    An issue with no requirement id is not an error: the deterministic mapping's
    grouping issue restates the project title rather than a requirement, so the absence
    is stated plainly instead of flagged.
    """
    if issue.parent_plan_key:
        st.caption("Proposed beneath {} in this plan.".format(issue.parent_plan_key))

    if issue.requirement_ids:
        st.markdown(
            "**Source BRD requirement(s):** "
            + ", ".join("`{}`".format(identifier) for identifier in issue.requirement_ids)
        )
    else:
        st.caption("No BRD requirement id is recorded on this issue.")

    linked = _linked_action_items(issue)
    if linked:
        st.markdown("**Linked action item(s) from the meeting**")
        for identifier, text in linked:
            st.markdown(
                "- `{}` {}".format(identifier, text) if text else "- `{}`".format(identifier)
            )

    if issue.rationale:
        st.markdown("**Why this issue:** {}".format(issue.rationale))


def _render_planned_issue(plan, issue, depth: int, metadata):
    """
    One proposed issue and, indented beneath it, whatever names it as parent.

    Indentation and the ``↳`` marker are what make the parent/child relationship
    visible. Editors sit behind an expander per issue rather than nested
    expanders, which Streamlit does not allow.
    """
    current = next((item for item in plan.issues if item.plan_key == issue.plan_key), None)
    if current is None:
        return plan
    issue = current
    widget = _JIRA_REVIEW_WIDGET_PREFIX + issue.plan_key + "__"

    selected = st.checkbox(
        "Include {}".format(issue.plan_key),
        value=issue.selected,
        key=widget + "selected",
    )
    if selected != issue.selected:
        plan = _persist_work_plan(update_planned_issue(plan, issue.plan_key, selected=selected))
        issue = next(item for item in plan.issues if item.plan_key == issue.plan_key)

    st.markdown(
        "{}{}`{}` **{}**".format(
            "&nbsp;" * (6 * depth),
            "↳ " if depth else "",
            issue.issue_type_name or issue.issue_type_id or "issue type not named",
            issue.summary,
        )
    )

    with st.expander("Details — {}".format(issue.plan_key)):
        if issue.hierarchy_level is not None:
            st.caption("Jira hierarchy level {} in this project.".format(issue.hierarchy_level))
        _render_issue_traceability(issue)

        summary = st.text_input(
            "Summary",
            value=issue.summary,
            key=widget + "summary",
        )
        description = st.text_area(
            "Description",
            value=issue.description,
            key=widget + "description",
        )
        criteria_value = st.text_area(
            "Acceptance criteria (one per line)",
            value=_criteria_text(issue.acceptance_criteria),
            key=widget + "criteria",
        )
        criteria = _criteria_from_text(criteria_value)
        if (
            summary != issue.summary
            or description != issue.description
            or criteria != issue.acceptance_criteria
        ):
            plan = _persist_work_plan(
                update_planned_issue(
                    plan,
                    issue.plan_key,
                    summary=summary,
                    description=description,
                    acceptance_criteria=criteria,
                )
            )
            issue = next(item for item in plan.issues if item.plan_key == issue.plan_key)

        options = compatible_issue_types(issue, metadata)
        if options and (len(options) > 1 or options[0].id != issue.issue_type_id):
            type_ids = [item.id for item in options]
            labels = {item.id: item.name or item.id for item in options}
            current_id = issue.issue_type_id if issue.issue_type_id in type_ids else type_ids[0]
            chosen_id = st.selectbox(
                "Issue type",
                type_ids,
                index=type_ids.index(current_id),
                format_func=lambda type_id: labels.get(type_id, type_id),
                key=widget + "type",
            )
            if chosen_id != issue.issue_type_id:
                chosen = next(item for item in options if item.id == chosen_id)
                plan = _persist_work_plan(
                    set_planned_issue_type(plan, issue.plan_key, chosen, metadata)
                )
                issue = next(item for item in plan.issues if item.plan_key == issue.plan_key)
        elif issue.issue_type_name or issue.issue_type_id:
            st.caption(
                "Issue type: {}".format(issue.issue_type_name or issue.issue_type_id)
            )

        if st.button("Delete this proposed issue", key=widget + "delete"):
            plan = _persist_work_plan(delete_planned_issue(plan, issue.plan_key))
            _clear_jira_plan_review_widgets()
            st.rerun()

    for child in plan.children_of(issue.plan_key):
        plan = _render_planned_issue(plan, child, depth + 1, metadata)
    return plan


def _traced_to(record) -> str:
    """The BRD ids one result came from, as a caption suffix, or "" if it names none."""
    parts = []
    if record.source_requirement_ids:
        parts.append("requirement(s) {}".format(", ".join(record.source_requirement_ids)))
    if record.source_action_item_ids:
        parts.append("action item(s) {}".format(", ".join(record.source_action_item_ids)))
    return " — from {}".format(" and ".join(parts)) if parts else ""


def _render_created_results(created, site_url: str = "") -> None:
    """
    What a creation run actually did, successes and failures kept apart.

    Rendered from stored results rather than from the run itself, so the outcome
    survives the reruns that follow it and a partial failure stays visible instead of
    vanishing on the next click.

    Each line maps one proposed item to what became of it: the plan key, the summary
    that was sent, the Jira key as a link to the issue, and the BRD requirements and
    action items it came from. That is the whole trail from the meeting to the issue,
    on one line, without a second place to look it up.
    """
    succeeded = [record for record in created if record.succeeded]
    failed = [record for record in created if not record.succeeded]

    if succeeded:
        st.success("Created {} issue(s) in Jira.".format(len(succeeded)))
        for record in succeeded:
            url = issue_browse_url(site_url, record.issue_key)
            shown_key = (
                "[{}]({})".format(record.issue_key, url) if url
                else "**{}**".format(record.issue_key or record.issue_id)
            )
            st.markdown(
                "- `{}` {} — created as {}{}".format(
                    record.plan_key, record.summary, shown_key, _traced_to(record)
                )
            )
    if failed:
        st.error(
            "{} issue(s) were not created. The successes above were still created and "
            "are listed by their Jira key.".format(len(failed))
        )
        for record in failed:
            st.write(
                "- `{}` {} — not created: {}{}".format(
                    record.plan_key, record.summary, record.error, _traced_to(record)
                )
            )
        st.caption(
            "Nothing is retried automatically: a create cannot be repeated safely without "
            "risking a duplicate. Deselect what already exists in Jira before trying again."
        )


def _result_for(issue, **outcome) -> CreatedIssue:
    """One creation outcome, carrying the plan item's own traceability with it."""
    return CreatedIssue(
        plan_key=issue.plan_key,
        summary=issue.summary,
        source_requirement_ids=issue.requirement_ids,
        source_action_item_ids=tuple(issue.source_action_item_ids),
        **outcome,
    )


def _create_selected_issues(service, tokens, cloud_id, project, plan) -> tuple:
    """
    Create the selected issues, parents first, and report each outcome.

    Stops at the first failure. Continuing would create children whose parent is
    missing, and a run that half-built a hierarchy is harder to clean up than one that
    stopped where it broke.

    The token is refreshed *before* the run rather than in reaction to a 401, so no
    create is ever re-sent: ``JiraService.create_issue`` raises an authentication error
    on 401 instead of the expiry error ``call_with_refresh`` would retry.
    """
    created: list = []
    keys_by_plan_key: dict = {}

    if tokens.is_expired() and tokens.can_refresh():
        try:
            tokens = refresh_tokens(service, tokens)
            st.session_state[_skey(service.name, "tokens")] = tokens
        except Exception as e:
            _report_provider_error(service.display_name, e)
            return ()

    for issue in creation_order(plan):
        parent_key = keys_by_plan_key.get(issue.parent_plan_key, "")
        payload = issue_creation_payload(issue, project.api_identifier, parent_key)
        try:
            body = service.create_issue(
                access_token=tokens.access_token,
                cloud_id=cloud_id,
                payload=payload,
            )
        except Exception as e:
            created.append(_result_for(issue, error=str(e)))
            break

        record = _result_for(
            issue,
            issue_key=str(body.get("key") or ""),
            issue_id=str(body.get("id") or ""),
        )
        created.append(record)
        if record.issue_key:
            keys_by_plan_key[issue.plan_key] = record.issue_key

    return tuple(created)


def _render_jira_creation_panel(service, tokens, site, project, metadata) -> None:
    """
    Step 5: create the selected issues, after an explicit confirmation.

    Two deliberate properties. Nothing is created on a page load or a rerun -- a write
    happens only in the branch a confirm button was pressed in. And a run is guarded by
    a stored flag, so a double-click or a rerun mid-run cannot start a second one.
    """
    plan = st.session_state.get(_skey(JIRA_STATE_NAME, "plan"))
    created_key = _skey(JIRA_STATE_NAME, "created")
    creating_key = _skey(JIRA_STATE_NAME, "creating")
    confirm_key = _skey(JIRA_STATE_NAME, "confirm_create")

    created = st.session_state.get(created_key)
    if created:
        st.markdown("**Step 5 — create the selected issues**")
        _render_created_results(created, site.url)
        return

    if plan is None or plan.is_empty:
        return

    st.markdown("**Step 5 — create the selected issues**")

    # A session authorized before the write scope existed cannot create anything, and a
    # refresh cannot widen a grant. Checked before anything is offered, so this is one
    # sentence here rather than a 403 partway through a run. An empty scope list is not
    # evidence of absence: some token responses omit `scope` entirely.
    granted = tokens.public_summary().get("scopes")
    if granted and service.WRITE_SCOPE not in granted:
        st.warning(
            "This Jira session was authorized without `{}`, so it cannot create issues. "
            "Disconnect and connect again to grant it. Nothing has been created.".format(
                service.WRITE_SCOPE
            )
        )
        return

    problems = validate_work_plan(plan, metadata, project)
    if problems:
        st.warning(
            "This plan cannot be created yet. Fix the {} problem(s) listed above "
            "first.".format(len(problems))
        )
        return

    ordered = creation_order(plan)
    selected = [issue for issue in plan.issues if issue.selected]
    if not ordered:
        # Two causes reach here: nothing is selected, and no selected issue can be
        # placed after its parent. Neither is named, because asserting the wrong one is
        # worse than reporting that there is nothing this plan can create.
        st.warning(
            "Nothing can be created from this plan: either no issue is selected, or a "
            "parent relationship could not be resolved into a creation order."
        )
        return

    if len(ordered) < len(selected):
        # An omission has to be stated before the confirmation, not discovered from a
        # result list that is shorter than the plan.
        placed = {issue.plan_key for issue in ordered}
        st.warning(
            "{} selected issue(s) cannot be placed after their parent and will not be "
            "created: {}.".format(
                len(selected) - len(ordered),
                ", ".join(
                    issue.plan_key for issue in selected if issue.plan_key not in placed
                ),
            )
        )

    st.write(
        "**{} issue(s)** would be created in **{}**, as {}.".format(
            len(ordered),
            project.display_label,
            ", ".join(
                "`{}`".format(name)
                for name in dict.fromkeys(
                    issue.issue_type_name or issue.issue_type_id for issue in ordered
                )
            ),
        )
    )
    st.caption(
        "Parents are created before the children that name them. This writes to Jira and "
        "cannot be undone from here — nothing is retried automatically, because repeating "
        "a create risks a duplicate issue."
    )

    if not st.session_state.get(confirm_key):
        if st.button("Create Selected Issues in Jira", key="request_jira_creation"):
            st.session_state[confirm_key] = True
            st.rerun()
        return

    st.warning(
        "Confirm: create {} issue(s) in {}. This cannot be undone from this app.".format(
            len(ordered), project.display_label
        )
    )
    confirmed = st.button("Yes — create them now", key="confirm_jira_creation")
    if st.button("Cancel", key="cancel_jira_creation"):
        st.session_state.pop(confirm_key, None)
        st.rerun()
        return

    if not confirmed:
        return

    # The guard is stored before the first request and cleared only after the run, so a
    # rerun that arrives mid-run finds it set and does not start a second run.
    if st.session_state.get(creating_key):
        st.info("A creation run is already in progress.")
        return
    st.session_state[creating_key] = True
    try:
        with st.spinner("Creating {} issue(s) in Jira...".format(len(ordered))):
            results = _create_selected_issues(service, tokens, site.id, project, plan)
    finally:
        st.session_state.pop(creating_key, None)
        st.session_state.pop(confirm_key, None)

    if results:
        st.session_state[created_key] = results
        st.session_state[_skey(JIRA_STATE_NAME, "change_baseline")] = synchronized_baseline(
            plan, results
        )
    _render_created_results(results or (), site.url)


def _render_jira_section() -> None:
    """
    The optional Jira Cloud connection, site selection, project selection,
    create-metadata check, proposed work plan and issue creation.

    Read-only up to the plan: it connects an account, lists the sites that account
    granted access to, lists the projects it can see on the chosen site, reads what
    creating an issue there would require, and proposes a plan from the reviewed BRD.
    The plan is a proposal held in this browser session, and review edits stay here.
    The one write is issue creation, which happens only after an explicit
    confirmation in step 5 and never on a page load or a rerun. This section renders
    regardless of whether a BRD exists, because the OAuth redirect re-runs the
    script with no transcript in hand and the connected state still has to be
    visible when the user returns.
    """
    service = JiraService()

    st.divider()
    st.subheader("Jira (optional)")

    if not service.is_configured():
        st.caption(
            "Jira Cloud is not configured, so this step is skipped. Transcript ingestion "
            "and BRD generation are unaffected."
        )
        missing = service.get_missing_configuration()
        if missing:
            st.markdown(
                "**Missing environment variables:** "
                + ", ".join(f"`{item}`" for item in missing)
            )
            st.caption(
                "Only variable names are listed. Set their values in your local `.env` file — "
                "this app never displays, logs, or stores credential values."
            )
        with st.expander("View Jira setup & configuration requirements"):
            st.markdown(service.get_setup_instructions())
        return

    tokens = _connected_tokens(service)
    if tokens is None:
        st.caption(
            "Connect a Jira Cloud account to use a generated BRD as the starting point for a "
            "Jira work plan. Connecting by itself creates nothing in Jira."
        )
        _render_connect_button(service)
        return

    _render_connected_panel(service, tokens)

    # A session authorized before a scope was added keeps the narrower grant, and a
    # refresh cannot widen it. Saying so here beats letting the user hit a 403 and
    # guess whether it was scopes or Jira permissions.
    stale = service.missing_scopes(tokens.public_summary().get("scopes"))
    if stale:
        # Only the last step needs the write scope, so a session missing just that one
        # can still read, plan and review. Telling such a user that everything below
        # fails would be false and would push them into a reconnect they may not need.
        consequence = (
            "the steps below will fail"
            if [scope for scope in stale if scope != service.WRITE_SCOPE]
            else "reading and planning still work, but no issue can be created"
        )
        st.warning(
            "This Jira session was authorized before the app requested "
            + ", ".join("`{}`".format(scope) for scope in stale)
            + ". Atlassian grants scopes at consent time and a token refresh cannot add "
            "one, so {} until you disconnect and connect again.".format(consequence)
        )

    _render_jira_sites_panel(service, tokens)

    site = st.session_state.get(_skey(service.name, "site"))
    if site is not None:
        _render_jira_projects_panel(service, tokens, site)

    st.caption(
        "Authorized to read your Atlassian identity, the Jira sites you granted, and the "
        "projects and create-screen metadata on the site you select. A generated work plan "
        "is a proposal held in this session only. Nothing has been created or changed in "
        "Jira, and no issue can be created without your explicit confirmation."
    )


# --- Requirement change detection ---

# How each detected change kind is headed on its review card. The stored values are
# the deterministic ones from ``jira_models.CHANGE_TYPES``; these are only labels.
_CHANGE_HEADINGS = {
    "NEW": "New requirement",
    "CHANGED": "Changed requirement",
    "REMOVED_DEFERRED": "Removed or deferred",
    "UNCHANGED": "Unchanged",
    "UNCLEAR": "Unclear — needs manual review",
}


def _new_change_evidence(label: str) -> Optional[NormalizedTranscript]:
    """
    The later meeting to compare against the approved BRD, or ``None`` until asked.

    Every route here is one this app already has. Pasted and uploaded text go through
    the same two normalizers BRD generation uses, and Google Meet and Microsoft Teams
    reuse the transcript already retrieved in the source section above rather than
    fetching it a second time. There is no second ingestion path and no second
    transcript shape: all four arrive as one ``NormalizedTranscript``.
    """
    if label == "Manual Paste":
        notes = st.text_area(
            "Paste the notes from the later meeting",
            height=200,
            placeholder="Paste what was decided after the BRD was approved...",
            key="change_notes",
        )
        if st.button("Detect requirement changes", key="detect_manual", type="primary"):
            try:
                return normalize_manual_notes(notes)
            except TranscriptProcessingError as error:
                st.error(str(error))
        return None

    if label == "Upload Transcript File (.txt)":
        uploaded = st.file_uploader(
            "Upload the later transcript",
            type=["txt"],
            key="change_upload",
        )
        if st.button("Detect requirement changes", key="detect_upload", type="primary"):
            if uploaded is None:
                st.error("Upload a .txt transcript file before detecting changes.")
            else:
                try:
                    return normalize_uploaded_file(uploaded)
                except TranscriptProcessingError as error:
                    st.error(str(error))
        return None

    provider_name = "google_meet" if label == "Google Meet" else "microsoft_teams"
    transcript = st.session_state.get(_skey(provider_name, "transcript"))
    if not isinstance(transcript, NormalizedTranscript):
        st.caption(
            "No {} transcript is loaded in this session. Load one in the transcript "
            "source section above and it can be compared here without being retrieved "
            "again.".format(label)
        )
        return None

    st.caption(
        "Comparing the {} transcript already loaded above{}. It is not retrieved "
        "again.".format(
            label,
            ": {}".format(transcript.meeting_title) if transcript.meeting_title else "",
        )
    )
    if st.button(
        "Detect requirement changes", key="detect_{}".format(provider_name), type="primary"
    ):
        return transcript
    return None


def _render_requirement_change(change) -> None:
    """
    One proposed change, with what it is based on and what it would touch.

    Read-only. The card shows the reviewer everything a decision needs -- the source,
    the approved wording, the proposed wording, the evidence, and the Jira work linked
    to that requirement in this session's stored mappings -- and offers no control that
    could act on it. Approving a change is a separate, explicit step.
    """
    heading = "{} — {}".format(
        _CHANGE_HEADINGS.get(change.change_type, change.change_type),
        change.requirement_id or "new requirement",
    )
    with st.expander(heading, expanded=change.change_type != "UNCHANGED"):
        st.markdown(
            "**Source:** {}{}".format(
                change.source_label,
                " — {}".format(change.source_reference) if change.source_reference else "",
            )
        )

        if change.old_text:
            st.markdown("**Approved requirement (old):** {}".format(change.old_text))
        elif change.change_type == "NEW":
            st.caption(
                "Not in the approved BRD. The id above is proposed for it, not one the "
                "detector chose."
            )

        if change.proposed_new_text:
            st.markdown("**Proposed requirement (new):** {}".format(change.proposed_new_text))

        if change.is_from_jira:
            if change.jira_field:
                st.markdown("**Changed Jira field(s):** {}".format(change.jira_field))
            if change.previous_value:
                st.markdown("**Jira value before:**\n```text\n{}\n```".format(change.previous_value))
            if change.jira_current_value:
                st.markdown("**Current Jira value:**\n```text\n{}\n```".format(change.jira_current_value))

        if change.source_evidence:
            st.markdown("**Source Evidence:**\n> {}".format(change.source_evidence))
        else:
            st.caption(
                "No evidence from this source could be verified for this change, so "
                "none is shown."
            )

        if change.affected_issue_keys:
            st.markdown(
                "**Affected Jira issue(s):** "
                + ", ".join("`{}`".format(key) for key in change.affected_issue_keys)
            )
        if change.affected_plan_keys:
            st.markdown(
                "**Affected planned item(s):** "
                + ", ".join("`{}`".format(key) for key in change.affected_plan_keys)
            )
        if not (change.affected_issue_keys or change.affected_plan_keys):
            st.caption(
                "No planned or created Jira work is linked to this requirement in this "
                "session's stored mappings."
            )

        if change.impact:
            st.markdown("**Impact:** {}".format(change.impact))
        if change.proposed_action:
            st.markdown("**Proposed action:** {}".format(change.proposed_action))
        if change.confidence:
            st.caption("Detector confidence: {}".format(change.confidence))

        if change.needs_manual_review:
            st.warning(
                change.review_reason
                or "This change could not be classified confidently and needs a person."
            )

        st.caption(
            "Status: {}. Nothing in the BRD or in Jira has been changed.".format(
                change.approval_state
            )
        )


def _record_change_decision(changes_key: str, proposal: ChangeProposal, change_id: str, state: str) -> None:
    """Persist a reviewer decision; this records no BRD or Jira mutation."""
    st.session_state[changes_key] = decide_change(proposal, change_id, state)
    st.rerun()


def _render_change_decisions(proposal: ChangeProposal, changes_key: str, brd_data: BRDData) -> None:
    """Explicit review and apply controls shared by meeting and Jira proposals."""
    for change in proposal.changes:
        if not change.is_pending:
            continue

        if change.is_from_jira:
            approve, accept, reject, keep = st.columns(4)
            if approve.button(
                "Approve", key="approve_change_{}".format(change.change_id), disabled=not change.is_decidable
            ):
                _record_change_decision(changes_key, proposal, change.change_id, "approved")
            if accept.button(
                "Accept Jira → BRD",
                key="accept_jira_change_{}".format(change.change_id),
                disabled=not change.is_decidable,
            ):
                _record_change_decision(changes_key, proposal, change.change_id, "approved")
            if reject.button("Reject", key="reject_change_{}".format(change.change_id)):
                _record_change_decision(changes_key, proposal, change.change_id, "rejected")
            if keep.button("Keep Jira only", key="keep_jira_change_{}".format(change.change_id)):
                _record_change_decision(changes_key, proposal, change.change_id, "jira_only")
        else:
            approve, reject = st.columns(2)
            if approve.button(
                "Approve", key="approve_change_{}".format(change.change_id), disabled=not change.is_decidable
            ):
                _record_change_decision(changes_key, proposal, change.change_id, "approved")
            if reject.button("Reject", key="reject_change_{}".format(change.change_id)):
                _record_change_decision(changes_key, proposal, change.change_id, "rejected")

    if proposal.approved and st.button("Apply approved changes to BRD", key="apply_requirement_changes"):
        updated, applied = apply_approved_changes(brd_data, proposal)
        if not applied:
            st.warning(
                "No change was applied because its approved BRD value is no longer current. "
                "The BRD and Jira were left unchanged."
            )
            return
        _store_brd(updated)
        _flash("success", "Applied {} approved requirement change(s) to the BRD.".format(len(applied)))
        st.rerun()


def _render_jira_drift_detection(brd_data: BRDData, changes_key: str) -> None:
    """Read only the saved Jira issue snapshots and turn direct edits into proposals."""
    baseline = st.session_state.get(_skey(JIRA_STATE_NAME, "change_baseline"))
    if not isinstance(baseline, dict) or not baseline:
        st.caption(
            "No saved Jira synchronization baseline is available in this session. "
            "Jira edits are not compared without one."
        )
        return

    service = JiraService()
    tokens = _connected_tokens(service)
    site = st.session_state.get(_skey(service.name, "site"))
    if tokens is None or site is None:
        st.caption(
            "Connect Jira and select the saved issue's site before checking for direct edits."
        )
        return

    issue_keys = tuple(str(key).strip() for key in baseline if str(key).strip())
    if not issue_keys:
        st.caption("The saved Jira baseline is malformed, so no issue was checked.")
        return

    if not st.button("Check Jira for requirement drift", key="detect_jira_drift", type="primary"):
        return

    current_issues = []
    failures = []
    for issue_key in issue_keys:
        current = _provider_call(
            service,
            tokens,
            lambda token, key=issue_key: service.get_issue_fields(token, site.id, key),
            "Reading {} from Jira...".format(issue_key),
        )
        if isinstance(current, dict):
            current_issues.append(current)
        else:
            failures.append(issue_key)

    st.session_state[changes_key] = detect_jira_changes(
        brd_data,
        current_issues,
        plan=st.session_state.get(_skey(JIRA_STATE_NAME, "plan")),
        created=st.session_state.get(_skey(JIRA_STATE_NAME, "created")) or (),
        baseline=baseline,
        failures=failures,
    )


def _render_requirement_changes_section() -> None:
    """
    Compare a later meeting against the approved BRD. Changes nothing.

    Deliberately holds no Jira service and no token, exactly as the work-plan panel
    does, so "detecting a change creates nothing" is a property of the code rather than
    a caption. The plan and the creation results are read out of this session only, to
    report which work a changed requirement is already linked to.

    Every proposal begins pending. A reviewer can explicitly approve, reject, or keep
    a Jira edit only; applying is a separate button and never writes back to Jira.
    """
    st.divider()
    st.subheader("Requirement changes (optional)")

    brd_data = st.session_state.get(BRD_SESSION_KEY)
    if not isinstance(brd_data, BRDData):
        st.caption(
            "No BRD is available yet. Change detection compares a later meeting against "
            "approved requirements, so generate a BRD above first."
        )
        return

    changes_key = _skey(JIRA_STATE_NAME, "changes")

    st.markdown("**Jira-driven drift**")
    st.caption(
        "Compare the current Jira fields with the values saved when these issues were "
        "created. This is read-only and produces pending proposals."
    )
    _render_jira_drift_detection(brd_data, changes_key)

    st.markdown("**Meeting-driven changes**")
    st.caption(
        "Each result is a proposal awaiting your approval: detecting and reviewing "
        "changes nothing in the BRD and nothing in Jira."
    )

    label = st.radio(
        "New evidence source",
        TRANSCRIPT_SOURCES,
        horizontal=True,
        key="change_source",
    )
    transcript = _new_change_evidence(label)

    if transcript is not None:
        with st.spinner("Comparing this source against the approved requirements..."):
            st.session_state[changes_key] = detect_meeting_changes(
                brd_data,
                transcript,
                plan=st.session_state.get(_skey(JIRA_STATE_NAME, "plan")),
                created=st.session_state.get(_skey(JIRA_STATE_NAME, "created")) or (),
                generate=_planner_generate(),
            )

    proposal = st.session_state.get(changes_key)
    if not isinstance(proposal, ChangeProposal):
        st.caption("Not run yet. Detecting changes creates nothing and changes nothing.")
        return

    for note in proposal.notes:
        st.info(note)

    if proposal.is_empty:
        st.success("No requirement change was proposed from this source.")
        return

    pending = len(proposal.pending)
    review = len(proposal.needing_review)
    st.markdown(
        "**{} proposed change(s)**, {} pending your decision{}.".format(
            len(proposal.changes),
            pending,
            ", {} needing manual review".format(review) if review else "",
        )
    )

    for change in proposal.changes:
        _render_requirement_change(change)

    _render_change_decisions(proposal, changes_key, brd_data)


def _render_provider_section(provider) -> Optional[NormalizedTranscript]:
    """Full UI for one provider: configuration, auth, discovery, preview, generate."""
    if not provider.is_configured():
        st.warning("Status: **Provider Not Configured**")
        missing = provider.get_missing_configuration()
        if missing:
            st.markdown(
                "**Missing environment variables:** "
                + ", ".join(f"`{item}`" for item in missing)
            )
            st.caption(
                "Only variable names are listed. Set their values in your local `.env` file — "
                "this app never displays, logs, or stores credential values."
            )
        with st.expander("View setup & configuration requirements", expanded=True):
            st.markdown(provider.get_setup_instructions())
        st.info("Manual Paste and Upload Transcript File keep working without this provider.")
        return None

    tokens = _connected_tokens(provider)
    if tokens is None:
        st.info(
            f"{provider.display_name} is configured. Authorize read-only access to list and "
            "retrieve your meeting transcripts."
        )
        _render_connect_button(provider)
        return None

    _render_connected_panel(provider, tokens)
    st.divider()
    _render_discovery_panel(provider, tokens)
    _render_manual_fallback(provider, tokens)
    return _render_loaded_transcript(provider)


# --- Main Streamlit App ---

st.title("Auto-BRD Generator")
st.write(
    "Convert raw meeting notes and transcripts into a structured Business Requirements Document."
)

# Handle OAuth redirects (denial, forged/expired state, or a verified code) before
# anything else renders.
_handle_oauth_callback()
_render_flashes()

# --- Transcript Source Selection ---
source_option = st.radio(
    "Select Transcript Source",
    TRANSCRIPT_SOURCES,
    horizontal=True,
)

transcript_to_process: Optional[NormalizedTranscript] = None

if source_option == "Manual Paste":
    meeting_notes = st.text_area(
        "Paste meeting notes",
        height=300,
        placeholder="Paste your meeting notes here...",
    )
    if st.button("Generate BRD", key="generate_manual"):
        try:
            transcript_to_process = normalize_manual_notes(meeting_notes)
        except TranscriptProcessingError as e:
            st.error(str(e))

elif source_option == "Upload Transcript File (.txt)":
    uploaded_file = st.file_uploader(
        "Upload a transcript text file",
        type=["txt"],
        help="Select a .txt file containing meeting notes or transcripts.",
    )
    if uploaded_file is not None:
        try:
            preview_transcript = normalize_uploaded_file(uploaded_file)
            st.success(
                f"Loaded `{uploaded_file.name}` "
                f"({preview_transcript.metadata.get('line_count', 0)} lines, "
                f"{preview_transcript.metadata.get('char_count', 0)} characters)"
            )
            with st.expander("Preview Transcript Content", expanded=False):
                st.text_area(
                    "Transcript Preview",
                    value=preview_transcript.raw_text,
                    height=200,
                    disabled=True,
                )
        except TranscriptProcessingError as e:
            st.error(str(e))

    if st.button("Generate BRD", key="generate_upload"):
        if uploaded_file is None:
            st.error("Please upload a .txt transcript file before generating the BRD.")
        else:
            try:
                transcript_to_process = normalize_uploaded_file(uploaded_file)
            except TranscriptProcessingError as e:
                st.error(str(e))

elif source_option == "Google Meet":
    st.subheader("Google Meet Integration")
    transcript_to_process = _render_provider_section(GoogleMeetProvider())

elif source_option == "Microsoft Teams":
    st.subheader("Microsoft Teams Integration")
    transcript_to_process = _render_provider_section(MSTeamsProvider())

# --- Unified BRD Generation Pipeline ---
# Manual paste, TXT upload, Google Meet and Microsoft Teams all converge here: one
# NormalizedTranscript, one Gemini call, one evidence-validation pass, one export.
if transcript_to_process:
    if not GEMINI_API_KEY:
        st.error("Gemini API key is not configured. Please set it in your .env file.")
    elif not CLIENT:
        st.error("Gemini client could not be initialized. Please check your API key.")
    else:
        try:
            with st.spinner("Analyzing transcript and generating BRD with Gemini..."):
                brd_data = generate_brd_from_transcript(transcript_to_process)

            # Kept for the optional Jira step below, which runs after a re-run.
            _store_brd(brd_data)

            # --- Display the generated BRD ---
            display_brd(brd_data)

            # --- Add Download Button ---
            st.divider()
            markdown_content = convert_brd_to_markdown(brd_data)
            download_filename = f"{brd_data.project_title.replace(' ', '_')}_BRD.md"
            st.download_button(
                label="Download BRD as Markdown",
                data=markdown_content,
                file_name=download_filename,
                mime="text/markdown",
            )
        except json.JSONDecodeError:
            st.error("The response from the AI was not valid JSON. Please try again.")
        except Exception as e:
            st.error(f"An unexpected error occurred during BRD generation: {e}")

# --- Optional Jira Connection ---
# Rendered last, after the BRD, and entirely optional: a BRD-only user can ignore
# it. The only write is issue creation, which requires an explicit confirmation.
_render_jira_section()

# --- Optional Requirement Change Detection ---
# Compares a later meeting against the approved BRD and reports what it would change.
# Rendered after Jira so the impact it reports can name the plan and the issues created
# above. It takes no service and no token, so it cannot write anywhere.
_render_requirement_changes_section()
