import os
import json
import logging
from dataclasses import replace
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
from jira_models import CreatedIssue
from architecture_generator import generate_architecture
from architecture_models import LAYERS, LAYER_LABEL, ArchitectureData
from implementation_plan_generator import (
    break_dependency_cycles,
    generate_implementation_plan,
)
from implementation_plan_models import (
    DEFAULT_PRIORITY,
    PRIORITIES,
    ImplementationPlan,
    component_index,
)
from lifecycle_models import (
    APPROVED,
    ARCHITECTURE,
    DELIVERY_STATUS,
    DISCOVERY_BRD,
    IMPLEMENTATION_PLAN,
    IMPLEMENTED_STAGES,
    LIFECYCLE_STAGES,
    PRD,
    STAGE_LABEL,
    TEST_CASES,
    TEST_EXECUTION,
    lifecycle_from,
)
from jira_planner import action_item_index, generate_work_plan
from implementation_plan_jira import (
    DeliveryMapping,
    delivery_progress,
    known_issue_keys,
    map_plan_to_work_plan,
    pending_plan_keys,
    record_created_issues,
)
from prd_generator import generate_prd
from prd_models import PRDData
from test_case_generator import generate_test_suite, _fallback_test_suite
from test_case_models import TestCase, TestSuite, TEST_EXECUTION_NOT_RUN, TEST_EXECUTION_PASS, TEST_EXECUTION_FAIL, TEST_EXECUTION_BLOCKED
from execution_engine import (
    generate_execution_evidence,
    get_execution_status_summary,
    approve_test_execution,
)
from sprint_completion import complete_sprint, recommend_next_sprint
from sprint_completion_models import SprintCompletion
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

logger = logging.getLogger(__name__)


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
    # The stable plan item -> issue key mapping an approved implementation plan
    # produced in this project, the last run's per-item outcomes, and the guards for
    # that one write. A mapping is only meaningful against the project its keys live
    # in, so choosing a different project or disconnecting drops it with everything
    # else here. The mapped proposal itself is not stored: it is recomputed from the
    # approved plan and this project's metadata, so the preview cannot drift from the
    # plan the reviewer approved.
    "delivery_mapping",
    "delivery_results",
    "delivery_creating",
    "delivery_confirm",
)

# The four evidence sources this app ingests, offered by the BRD generator below.
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

# Which ingestion route produced that BRD. The transcript itself is not kept, so this
# is all the provenance the Discovery stage can report.
BRD_SOURCE_SESSION_KEY = "brd_source"

# Whether the reviewer explicitly approved that BRD. The PRD stage is gated on it:
# a generated BRD is a draft until a person says otherwise, and nothing sets this
# except the approval button.
BRD_APPROVED_SESSION_KEY = "brd_approved"

# The PRD derived from the approved BRD, whether the reviewer approved it, and the
# optional product-refinement transcript that enriched it.
PRD_SESSION_KEY = "prd_data"
PRD_APPROVED_SESSION_KEY = "prd_approved"
PRD_REFINEMENT_SESSION_KEY = "prd_refinement"

# Streamlit widget keys for the PRD review editors, kept under one prefix so a new PRD
# cannot inherit the previous one's editor values.
_PRD_WIDGET_PREFIX = "prd_review__"

# The architecture derived from the approved PRD, whether the reviewer approved it, and
# the optional architecture-discussion transcript that informed it.
ARCHITECTURE_SESSION_KEY = "architecture_data"
ARCHITECTURE_APPROVED_SESSION_KEY = "architecture_approved"
ARCHITECTURE_DISCUSSION_SESSION_KEY = "architecture_discussion"

# Widget keys for the architecture review editors, under their own prefix for the same
# reason as the PRD's.
_ARCH_WIDGET_PREFIX = "arch_review__"

# The implementation plan derived from the approved PRD and architecture, and whether the
# reviewer approved it. No transcript here: the plan is a decomposition of two approved
# artifacts, and a fourth meeting would add opinion rather than evidence.
IMPLEMENTATION_PLAN_SESSION_KEY = "implementation_plan_data"
IMPLEMENTATION_PLAN_APPROVED_SESSION_KEY = "implementation_plan_approved"

# Widget keys for the implementation plan review editors, under their own prefix for the
# same reason as the PRD's and the architecture's.
_PLAN_WIDGET_PREFIX = "plan_review__"

# The test cases derived from the approved implementation plan, and whether the reviewer approved them.
TEST_CASES_SESSION_KEY = "test_cases_data"
TEST_CASES_APPROVED_SESSION_KEY = "test_cases_approved"

# Widget keys for the test cases review editors, under their own prefix.
_TEST_CASES_WIDGET_PREFIX = "test_cases_review__"

# The test execution data for the approved test cases, and whether the reviewer approved them.
TEST_EXECUTION_SESSION_KEY = "test_execution_data"
TEST_EXECUTION_APPROVED_SESSION_KEY = "test_execution_approved"

# Widget keys for the test execution review editors, under their own prefix.
_TEST_EXECUTION_WIDGET_PREFIX = "test_execution_review__"

# Sprint Completion capability: history of completed sprints and the next-sprint proposal.
# Capability lives inside the existing Delivery Status area; not a 9th navigation stage.
SPRINT_COMPLETION_HISTORY_KEY = "sprint_completion_history"
SPRINT_COMPLETION_LAST_KEY = "sprint_completion_last"
SPRINT_COMPLETION_NEXT_KEY = "sprint_completion_next"
SPRINT_COMPLETION_APPROVED_KEY = "sprint_completion_next_approved"


def _clear_prd_widgets() -> None:
    """Drop PRD review-editor widget state."""
    for key in list(st.session_state.keys()):
        if str(key).startswith(_PRD_WIDGET_PREFIX):
            st.session_state.pop(key, None)


def _clear_test_cases_state() -> None:
    """
    Forget the test cases, their approval, and their review editors.
    """
    for key in (
        TEST_CASES_SESSION_KEY,
        TEST_CASES_APPROVED_SESSION_KEY,
    ):
        st.session_state.pop(key, None)
    for key in list(st.session_state.keys()):
        if str(key).startswith(_TEST_CASES_WIDGET_PREFIX):
            st.session_state.pop(key, None)


def _clear_test_execution_state() -> None:
    """
    Forget the test execution data, their approval, and their review editors.
    """
    for key in (
        TEST_EXECUTION_SESSION_KEY,
        TEST_EXECUTION_APPROVED_SESSION_KEY,
    ):
        st.session_state.pop(key, None)
    for key in list(st.session_state.keys()):
        if str(key).startswith(_TEST_EXECUTION_WIDGET_PREFIX):
            st.session_state.pop(key, None)


def _held_test_execution():
    test_execution = st.session_state.get(TEST_EXECUTION_SESSION_KEY)
    return test_execution if isinstance(test_execution, (list, tuple)) else None


def _persist_test_execution(test_execution) -> list:
    st.session_state[TEST_EXECUTION_SESSION_KEY] = test_execution
    return test_execution


def _clear_implementation_plan_state() -> None:
    """
    Forget the implementation plan, its approval and its editors.

    Called when the architecture changes, for the reason the architecture is cleared when
    the PRD changes: a plan decomposes one specific design, so a new design makes a held
    plan wrong rather than merely old, and its approval cannot carry over to work nobody
    has reviewed.
    """
    for key in (
        IMPLEMENTATION_PLAN_SESSION_KEY,
        IMPLEMENTATION_PLAN_APPROVED_SESSION_KEY,
    ):
        st.session_state.pop(key, None)
    for key in list(st.session_state.keys()):
        if str(key).startswith(_PLAN_WIDGET_PREFIX):
            st.session_state.pop(key, None)
    _clear_test_cases_state()


def _clear_architecture_state() -> None:
    """
    Forget the architecture, its approval and its editors.

    Called when the PRD changes for the same reason ``_clear_prd_state`` is called when
    the BRD changes: an architecture is the design for one specific PRD, so a new PRD
    makes a held architecture wrong rather than merely old. The implementation plan
    decomposed from that architecture goes with it.
    """
    for key in (
        ARCHITECTURE_SESSION_KEY,
        ARCHITECTURE_APPROVED_SESSION_KEY,
        ARCHITECTURE_DISCUSSION_SESSION_KEY,
    ):
        st.session_state.pop(key, None)
    for key in list(st.session_state.keys()):
        if str(key).startswith(_ARCH_WIDGET_PREFIX):
            st.session_state.pop(key, None)
    _clear_implementation_plan_state()
    _clear_test_cases_state()


def _clear_prd_state() -> None:
    """
    Forget the PRD, its approval and its editors.

    Called when the BRD changes: a PRD is a product definition of one specific BRD, so
    a new BRD makes a held PRD wrong rather than merely old, and its approval cannot
    carry over to a document nobody has reviewed. The architecture derived from that PRD
    goes with it.
    """
    for key in (PRD_SESSION_KEY, PRD_APPROVED_SESSION_KEY, PRD_REFINEMENT_SESSION_KEY):
        st.session_state.pop(key, None)
    _clear_prd_widgets()
    _clear_architecture_state()


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


def _store_brd(brd_data: BRDData, source: str = "") -> None:
    """
    Keep the generated BRD for the optional Jira step.

    Required because every button click re-runs this script: by the time a
    "Generate Jira Work Plan" click is handled, the run that produced the BRD is
    over and its local variable is gone. Nothing else about BRD generation,
    validation, display or export changes.

    A plan built from the previous BRD is dropped rather than left behind. A work
    plan is a proposal about one specific BRD, so a newly generated BRD makes a
    cached plan wrong, not merely old. The same applies to a PRD and to any approval
    recorded against the BRD that has just been replaced.
    """
    st.session_state[BRD_SESSION_KEY] = brd_data
    st.session_state[BRD_SOURCE_SESSION_KEY] = str(source or "")
    st.session_state.pop(BRD_APPROVED_SESSION_KEY, None)
    _clear_prd_state()
    for suffix in (
        "plan",
        "plan_for",
        "created",
        "creating",
        "confirm_create",
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
    _render_plan_delivery_panel(service, tokens, site, project, metadata)


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
        ):
            st.session_state.pop(_skey(JIRA_STATE_NAME, suffix), None)
        _clear_jira_plan_review_widgets()

    if st.button("Generate Jira Work Plan", key="generate_jira_work_plan"):
        _clear_jira_plan_review_widgets()
        # A regenerated plan is a different proposal, so a previous run's results and
        # its in-flight guard must not carry over onto it.
        for suffix in (
            "created",
            "creating",
            "confirm_create",
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


def _create_selected_issues(service, tokens, cloud_id, project, plan, known_keys=None) -> tuple:
    """
    Create the selected issues, parents first, and report each outcome.

    Stops at the first failure. Continuing would create children whose parent is
    missing, and a run that half-built a hierarchy is harder to clean up than one that
    stopped where it broke.

    ``known_keys`` maps a plan key to the Jira issue key it was *already* created as in
    an earlier run. Such an item is not sent again -- its key is simply remembered so a
    child created now is parented onto the real issue. This is what makes a retry after
    a partial failure safe: Jira's create endpoint has no idempotency key, so a repeat
    has to be prevented before the request rather than detected after it. Nothing passes
    this argument for the BRD-derived plan, which has no cross-run mapping to consult.

    The token is refreshed *before* the run rather than in reaction to a 401, so no
    create is ever re-sent: ``JiraService.create_issue`` raises an authentication error
    on 401 instead of the expiry error ``call_with_refresh`` would retry.
    """
    created: list = []
    keys_by_plan_key: dict = dict(known_keys or {})
    already_created = set(keys_by_plan_key)

    if tokens.is_expired() and tokens.can_refresh():
        try:
            tokens = refresh_tokens(service, tokens)
            st.session_state[_skey(service.name, "tokens")] = tokens
        except Exception as e:
            _report_provider_error(service.display_name, e)
            return ()

    for issue in creation_order(plan):
        if issue.plan_key in already_created:
            # Already in Jira. Skipped rather than re-sent, and no result is recorded:
            # the mapping already holds the outcome this item had.
            continue
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
    _render_created_results(results or (), site.url)


def _approved_implementation_plan() -> tuple:
    """
    The approved implementation plan, or ``(None, why not)``.

    The delivery gate. Mapping a draft plan onto issues would create work from
    something nobody signed off, so an unapproved plan is refused here rather than
    warned about later.
    """
    plan = st.session_state.get(IMPLEMENTATION_PLAN_SESSION_KEY)
    if not isinstance(plan, ImplementationPlan) or plan.is_empty:
        return None, (
            "No implementation plan is held yet. Generate one in the Implementation Plan "
            "stage — it needs an approved PRD and an approved architecture first."
        )
    if not bool(st.session_state.get(IMPLEMENTATION_PLAN_APPROVED_SESSION_KEY)):
        return None, (
            "This implementation plan is not approved yet. Review and approve it in the "
            "Implementation Plan stage; nothing is created in Jira from a draft plan."
        )
    return plan, ""


def _render_delivery_mapping(mapping, site_url: str = "") -> None:
    """
    The plan item → Jira issue mapping, which is the delivery record.

    Rendered from the stored mapping rather than from a run, so it survives the reruns
    that follow creation and stays readable after a partial failure.
    """
    if mapping is None or mapping.is_empty:
        return
    st.markdown("**Delivery mapping — implementation plan item → Jira issue**")
    for link in mapping.links:
        url = issue_browse_url(site_url, link.issue_key) if site_url else ""
        shown = "[{}]({})".format(link.issue_key, url) if url else "`{}`".format(link.issue_key)
        trace = []
        if link.feature_ids:
            trace.append("PRD {}".format(", ".join(link.feature_ids)))
        if link.component_ids:
            trace.append("architecture {}".format(", ".join(link.component_ids)))
        st.write(
            "- `{}` → {}{} — {}{}".format(
                link.plan_item_id,
                shown,
                " ({})".format(link.issue_type_name) if link.issue_type_name else "",
                link.summary or "",
                " — traces to {}".format(" and ".join(trace)) if trace else "",
            )
        )


def _render_plan_delivery_panel(service, tokens, site, project, metadata) -> None:
    """
    Step 6: an approved implementation plan onto this project's issues.

    Four gates, in this order: the plan must be approved, the mapping must be valid
    against this project's own hierarchy, the session must hold the write scope, and the
    reviewer must confirm twice. Only the last branch writes, and only for plan items
    that have no Jira issue yet — so a retry after a partial failure finishes the run
    instead of duplicating what already exists.
    """
    st.markdown("**Step 6 — deliver the approved implementation plan**")

    plan, blocked = _approved_implementation_plan()
    if plan is None:
        st.caption(blocked)
        return

    mapping_key = _skey(JIRA_STATE_NAME, "delivery_mapping")
    results_key = _skey(JIRA_STATE_NAME, "delivery_results")
    creating_key = _skey(JIRA_STATE_NAME, "delivery_creating")
    confirm_key = _skey(JIRA_STATE_NAME, "delivery_confirm")

    # Recomputed rather than cached, so what is previewed and created is always the
    # plan that is currently approved against this project's current metadata.
    work_plan = map_plan_to_work_plan(plan, metadata, project)
    mapping = st.session_state.get(mapping_key)
    if not isinstance(mapping, DeliveryMapping):
        mapping = DeliveryMapping(
            project_identifier=project.api_identifier,
            project_label=project.display_label,
            site_url=site.url,
        )
    results = st.session_state.get(results_key) or ()

    for note in work_plan.notes:
        st.info(note)

    if work_plan.is_empty:
        return

    progress = delivery_progress(work_plan, mapping, results)
    st.write(
        "**{}** of **{}** mapped plan item(s) exist in **{}**.{}".format(
            progress.created,
            progress.total,
            project.display_label,
            " {} item(s) cannot be created in this project.".format(progress.excluded)
            if progress.excluded
            else "",
        )
    )
    _render_delivery_mapping(mapping, site.url)
    if results:
        _render_created_results(results, site.url)

    if progress.is_complete:
        st.success(
            "Every mapped plan item has a Jira issue. Delivery status is read from these "
            "creation records; this app does not poll Jira for an issue's workflow state."
        )
        return

    problems = validate_work_plan(work_plan, metadata, project)
    if problems:
        st.warning(
            "This plan cannot be created in {} yet:".format(project.display_label)
        )
        for problem in problems:
            st.write("- {}".format(problem))
        return

    granted = tokens.public_summary().get("scopes")
    if granted and service.WRITE_SCOPE not in granted:
        st.warning(
            "This Jira session was authorized without `{}`, so it cannot create issues. "
            "Disconnect and connect again to grant it. Nothing has been created.".format(
                service.WRITE_SCOPE
            )
        )
        return

    pending = pending_plan_keys(work_plan, mapping)
    if not pending:
        st.caption("Nothing is left to create from this plan.")
        return

    verb = "Create" if not mapping.created_count else "Create the remaining"
    st.caption(
        "{} item(s) would be created: {}. Parents are created before the children that "
        "name them, and an item that already has a Jira issue is skipped rather than "
        "created again.".format(
            len(pending), ", ".join("`{}`".format(key) for key in pending[:12])
            + (", …" if len(pending) > 12 else "")
        )
    )

    if not st.session_state.get(confirm_key):
        if st.button(
            "{} {} Issue(s) in Jira".format(verb, len(pending)), key="request_plan_delivery"
        ):
            st.session_state[confirm_key] = True
            st.rerun()
        return

    st.warning(
        "Confirm: create {} issue(s) in {} from the approved implementation plan. This "
        "cannot be undone from this app.".format(len(pending), project.display_label)
    )
    confirmed = st.button("Yes — create them now", key="confirm_plan_delivery")
    if st.button("Cancel", key="cancel_plan_delivery"):
        st.session_state.pop(confirm_key, None)
        st.rerun()
        return
    if not confirmed:
        return

    # Stored before the first request and cleared only after the run, so a rerun that
    # arrives mid-run finds it set and cannot start a second one.
    if st.session_state.get(creating_key):
        st.info("A creation run is already in progress.")
        return
    st.session_state[creating_key] = True
    try:
        with st.spinner("Creating {} issue(s) in Jira...".format(len(pending))):
            run = _create_selected_issues(
                service,
                tokens,
                site.id,
                project,
                work_plan,
                known_keys=known_issue_keys(mapping),
            )
    finally:
        st.session_state.pop(creating_key, None)
        st.session_state.pop(confirm_key, None)

    # The mapping is folded in before anything is rendered, so even a run that failed
    # part way through leaves the successes recorded and un-creatable a second time.
    st.session_state[mapping_key] = record_created_issues(mapping, run, work_plan, plan)
    st.session_state[results_key] = run
    _render_created_results(run or (), site.url)
    if any(not record.succeeded for record in run or ()):
        st.info(
            "The issues above that were created are recorded in the delivery mapping and "
            "will not be created again. Use this step once more to create what is left."
        )


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


# --- Project lifecycle workspace ---


def _brd_approved() -> bool:
    """Whether the reviewer approved the BRD held in this session."""
    return bool(st.session_state.get(BRD_APPROVED_SESSION_KEY))


def _held_prd():
    """The PRD this session holds, or ``None``."""
    prd = st.session_state.get(PRD_SESSION_KEY)
    return prd if isinstance(prd, PRDData) else None


def _persist_prd(prd: PRDData) -> PRDData:
    st.session_state[PRD_SESSION_KEY] = prd
    return prd


def _render_brd_approval() -> None:
    """
    The explicit BRD approval control, which is what unlocks the PRD stage.

    Nothing else sets or clears this: generating a BRD leaves it pending, and
    approving is a deliberate act recorded against the BRD in front of the reviewer.
    """
    if _brd_approved():
        st.success("This BRD is approved and is the basis for the PRD.")
        if st.button("Revoke BRD approval", key="revoke_brd_approval"):
            st.session_state[BRD_APPROVED_SESSION_KEY] = False
            _flash(
                "info",
                "BRD approval was revoked. The PRD already generated from it is kept, "
                "but the BRD is pending review again.",
            )
        return

    st.caption(
        "Approving records that you reviewed the requirements above. It creates nothing "
        "and changes no requirement; it unlocks PRD generation from this BRD."
    )
    if st.button("Approve BRD", key="approve_brd"):
        st.session_state[BRD_APPROVED_SESSION_KEY] = True
        _flash("success", "BRD approved. The PRD can now be generated from it.")


def _prd_refinement() -> Optional[NormalizedTranscript]:
    """
    The optional product-refinement discussion, if the reviewer supplied one.

    Optional by design: the PRD is derived from the approved BRD, and this only enriches
    it. A transcript already loaded from a provider in this session can be reused rather
    than pasted again; anything pasted here becomes the same ``NormalizedTranscript``
    every other ingestion route produces.
    """
    loaded = [
        value
        for key, value in st.session_state.items()
        if str(key).endswith("__transcript") and isinstance(value, NormalizedTranscript)
    ]

    with st.expander("Optional product-refinement discussion"):
        st.caption(
            "Not required. The PRD is generated from the approved BRD; a product "
            "discussion only adds product detail on top of it."
        )
        if loaded:
            transcript = loaded[0]
            if st.checkbox(
                "Use the {} transcript already loaded in this session".format(
                    transcript.source or "loaded"
                ),
                key=_PRD_WIDGET_PREFIX + "use_loaded",
            ):
                st.session_state[PRD_REFINEMENT_SESSION_KEY] = transcript
                return transcript

        pasted = st.text_area(
            "Paste a product-refinement discussion (optional)",
            value="",
            key=_PRD_WIDGET_PREFIX + "refinement_text",
            height=140,
        )
        text = str(pasted or "").strip()
        if not text:
            st.session_state.pop(PRD_REFINEMENT_SESSION_KEY, None)
            return None
        transcript = NormalizedTranscript(raw_text=text, source="manual")
        st.session_state[PRD_REFINEMENT_SESSION_KEY] = transcript
        return transcript


def _render_prd_traceability(prd: PRDData) -> None:
    """Which BRD requirements this PRD covers, and which it does not."""
    covered = prd.covered_requirement_ids
    st.caption(
        "Traceability: {} of {} BRD requirement(s) covered — {}".format(
            len(covered),
            len(prd.source_requirement_ids),
            ", ".join(covered) if covered else "none",
        )
    )
    if prd.uncovered_requirement_ids:
        st.warning(
            "Not covered by any feature or journey: {}. Either that is deliberate — "
            "record it as an open question — or the PRD is incomplete.".format(
                ", ".join(prd.uncovered_requirement_ids)
            )
        )


def _prd_lines(label: str, values, suffix: str) -> tuple:
    """One editable list of single-line statements."""
    return _criteria_from_text(
        st.text_area(
            label,
            value=_criteria_text(values),
            key=_PRD_WIDGET_PREFIX + suffix,
        )
    )


def _render_prd_editor(prd: PRDData) -> PRDData:
    """
    The review and edit surface: every edit is kept, and none of them approves anything.

    Requirement ids are shown but not editable. Traceability is derived from the BRD,
    so letting it be typed over would let a reviewer claim coverage the BRD does not
    support.
    """
    overview = st.text_area(
        "Product overview",
        value=prd.overview,
        key=_PRD_WIDGET_PREFIX + "overview",
        height=120,
    )
    goals = _prd_lines("Product goals (one per line)", prd.goals, "goals")
    metrics = _prd_lines(
        "Success metrics (one per line)", prd.success_metrics, "success_metrics"
    )
    assumptions = _prd_lines(
        "Product assumptions (one per line)", prd.assumptions, "assumptions"
    )
    questions = _prd_lines(
        "Open questions (one per line)", prd.open_questions, "open_questions"
    )

    personas = []
    for position, persona in enumerate(prd.personas, start=1):
        with st.expander("Persona — {}".format(persona.name)):
            personas.append(
                replace(
                    persona,
                    description=st.text_area(
                        "Description",
                        value=persona.description,
                        key="{}persona_{}_description".format(_PRD_WIDGET_PREFIX, position),
                    ),
                    needs=_prd_lines(
                        "Needs (one per line)", persona.needs, "persona_{}_needs".format(position)
                    ),
                )
            )

    features = []
    for feature in prd.features:
        with st.expander(
            "{} — {} ({})".format(
                feature.feature_id, feature.name, ", ".join(feature.requirement_ids)
            )
        ):
            st.caption(
                "Serves BRD requirement(s): {}".format(", ".join(feature.requirement_ids))
            )
            features.append(
                replace(
                    feature,
                    name=st.text_input(
                        "Feature name",
                        value=feature.name,
                        key="{}{}_name".format(_PRD_WIDGET_PREFIX, feature.feature_id),
                    ),
                    summary=st.text_area(
                        "Summary",
                        value=feature.summary,
                        key="{}{}_summary".format(_PRD_WIDGET_PREFIX, feature.feature_id),
                    ),
                    behaviours=_prd_lines(
                        "Functional behaviour (one per line)",
                        feature.behaviours,
                        "{}_behaviours".format(feature.feature_id),
                    ),
                    edge_cases=_prd_lines(
                        "Edge cases (one per line)",
                        feature.edge_cases,
                        "{}_edge_cases".format(feature.feature_id),
                    ),
                    acceptance_criteria=_prd_lines(
                        "Acceptance criteria (one per line)",
                        feature.acceptance_criteria,
                        "{}_criteria".format(feature.feature_id),
                    ),
                )
            )

    journeys = []
    for position, journey in enumerate(prd.journeys, start=1):
        with st.expander(
            "Journey — {}{}".format(
                journey.name, " ({})".format(journey.persona) if journey.persona else ""
            )
        ):
            if journey.requirement_ids:
                st.caption(
                    "Serves BRD requirement(s): {}".format(", ".join(journey.requirement_ids))
                )
            journeys.append(
                replace(
                    journey,
                    steps=_prd_lines(
                        "Steps, in order (one per line)",
                        journey.steps,
                        "journey_{}_steps".format(position),
                    ),
                )
            )

    edited = replace(
        prd,
        overview=overview,
        goals=goals,
        success_metrics=metrics,
        assumptions=assumptions,
        open_questions=questions,
        personas=tuple(personas),
        features=tuple(features),
        journeys=tuple(journeys),
    )
    if edited != prd:
        # Editing keeps the PRD pending review. Only the approval button approves it.
        return _persist_prd(edited)
    return prd


def _render_prd_readonly(prd: PRDData) -> None:
    """The approved PRD, shown rather than offered for editing."""
    if prd.overview:
        st.markdown(prd.overview)
    for heading, values in (
        ("Goals", prd.goals),
        ("Success metrics", prd.success_metrics),
        ("Assumptions", prd.assumptions),
        ("Open questions", prd.open_questions),
    ):
        if values:
            st.markdown("**{}**".format(heading))
            for value in values:
                st.markdown("- {}".format(value))
    for persona in prd.personas:
        st.markdown("**Persona — {}** {}".format(persona.name, persona.description))
    for feature in prd.features:
        st.markdown(
            "**{} {}** — serves {}".format(
                feature.feature_id, feature.name, ", ".join(feature.requirement_ids)
            )
        )
        for label, values in (
            ("Behaviour", feature.behaviours),
            ("Edge cases", feature.edge_cases),
            ("Acceptance criteria", feature.acceptance_criteria),
        ):
            for value in values:
                st.markdown("- _{}_: {}".format(label, value))
    for journey in prd.journeys:
        st.markdown("**Journey — {}**".format(journey.name))
        for step in journey.steps:
            st.markdown("- {}".format(step))


def _render_prd_stage(lifecycle) -> None:
    """
    The PRD stage: generate from the approved BRD, review, edit, then approve explicitly.

    Blocked safely when there is no BRD or the BRD is not approved: the stage says what
    is missing instead of offering a control that would have to invent requirements.
    """
    if lifecycle.brd is None:
        st.info(
            "No BRD in this session yet. Generate one at the top of this page; the PRD "
            "is derived from the approved BRD."
        )
        return
    if not _brd_approved():
        st.info(
            "The BRD is pending review. Open the Discovery → BRD stage and approve it to "
            "generate a PRD from it."
        )
        return

    refinement = _prd_refinement()
    prd = _held_prd()

    if st.button("Generate PRD from the approved BRD", key="generate_prd"):
        _clear_prd_widgets()
        st.session_state.pop(PRD_APPROVED_SESSION_KEY, None)
        # A new PRD invalidates the architecture designed against the previous one.
        _clear_architecture_state()
        with st.spinner("Deriving the product definition from the approved BRD..."):
            prd = _persist_prd(
                generate_prd(lifecycle.brd, refinement, generate=_planner_generate())
            )

    if prd is None:
        st.caption(
            "Not generated yet. This derives product overview, personas, features, "
            "journeys, behaviour, edge cases and acceptance criteria from the approved "
            "BRD. It creates nothing outside this session."
        )
        return

    for note in prd.notes:
        st.warning(note)

    if prd.is_empty:
        st.caption(
            "No PRD content could be derived. Nothing has been approved and nothing "
            "downstream was generated."
        )
        return

    if prd.refinement_source:
        st.caption(
            "A product-refinement discussion ({}) was supplied alongside the BRD.".format(
                prd.refinement_source
            )
        )
    _render_prd_traceability(prd)

    if bool(st.session_state.get(PRD_APPROVED_SESSION_KEY)):
        st.success("This PRD is approved.")
        _render_prd_readonly(prd)
        if st.button("Revoke PRD approval to edit", key="revoke_prd_approval"):
            st.session_state[PRD_APPROVED_SESSION_KEY] = False
        return

    prd = _render_prd_editor(prd)
    st.caption(
        "Approving records that you reviewed this PRD. Later stages -- architecture, "
        "implementation plan, sprints and tests -- are not implemented yet, so nothing "
        "downstream is generated."
    )
    if st.button("Approve PRD", key="approve_prd"):
        st.session_state[PRD_APPROVED_SESSION_KEY] = True
        _flash("success", "PRD approved. The architecture can now be generated from it.")


def _prd_approved() -> bool:
    return bool(st.session_state.get(PRD_APPROVED_SESSION_KEY))


def _held_architecture():
    architecture = st.session_state.get(ARCHITECTURE_SESSION_KEY)
    return architecture if isinstance(architecture, ArchitectureData) else None


def _persist_architecture(architecture: ArchitectureData) -> ArchitectureData:
    st.session_state[ARCHITECTURE_SESSION_KEY] = architecture
    return architecture


def _architecture_discussion() -> Optional[NormalizedTranscript]:
    """
    The optional architecture or design discussion, if the reviewer supplied one.

    Optional by design: the architecture is derived from the approved PRD, and this only
    adds technical evidence -- a decision already taken, a constraint already known.
    """
    with st.expander("Optional architecture discussion"):
        st.caption(
            "Not required. The architecture is generated from the approved PRD; a design "
            "discussion only adds technical evidence on top of it."
        )
        pasted = st.text_area(
            "Paste an architecture or design discussion (optional)",
            value="",
            key=_ARCH_WIDGET_PREFIX + "discussion_text",
            height=140,
        )
        text = str(pasted or "").strip()
        if not text:
            st.session_state.pop(ARCHITECTURE_DISCUSSION_SESSION_KEY, None)
            return None
        transcript = NormalizedTranscript(raw_text=text, source="manual")
        st.session_state[ARCHITECTURE_DISCUSSION_SESSION_KEY] = transcript
        return transcript


def _render_architecture_traceability(architecture: ArchitectureData, prd: PRDData) -> None:
    """Which PRD features this architecture realises, which it does not, and whether it is stale."""
    covered = architecture.covered_feature_ids
    st.caption(
        "Traceability: {} of {} PRD feature(s) realised — {}".format(
            len(covered),
            len(architecture.source_feature_ids),
            ", ".join(covered) if covered else "none",
        )
    )
    if architecture.uncovered_feature_ids:
        st.warning(
            "Not realised by any component, decision or flow: {}. Either that is "
            "deliberate, or the architecture is incomplete.".format(
                ", ".join(architecture.uncovered_feature_ids)
            )
        )
    current = tuple(feature.feature_id for feature in prd.features)
    if current != tuple(architecture.source_feature_ids):
        st.warning(
            "The PRD's features changed after this architecture was generated. Regenerate "
            "it so the design matches the approved PRD."
        )


def _arch_lines(label: str, values, suffix: str) -> tuple:
    """One editable list of single-line statements."""
    return _criteria_from_text(
        st.text_area(label, value=_criteria_text(values), key=_ARCH_WIDGET_PREFIX + suffix)
    )


def _render_architecture_editor(architecture: ArchitectureData) -> ArchitectureData:
    """
    The review and edit surface: every edit is kept, and none of them approves anything.

    Feature ids are shown but not editable, for the reason the PRD editor does not let
    requirement ids be typed over: traceability is derived from the artifact upstream, so
    letting it be rewritten here would let a reviewer claim coverage the PRD does not
    support.
    """
    overview = st.text_area(
        "Architecture overview",
        value=architecture.overview,
        key=_ARCH_WIDGET_PREFIX + "overview",
        height=120,
    )
    domains = _arch_lines("Core domains (one per line)", architecture.domains, "domains")
    auth = _arch_lines(
        "Authentication and authorization (one per line)",
        architecture.auth_approach,
        "auth",
    )
    dependencies = _arch_lines(
        "Technical dependencies (one per line)", architecture.dependencies, "dependencies"
    )

    components = []
    for layer in LAYERS:
        in_layer = architecture.layer(layer)
        st.markdown("**{}** — {} component(s)".format(LAYER_LABEL[layer], len(in_layer)))
        for component in in_layer:
            with st.expander("{} — {}".format(component.component_id, component.name)):
                st.caption(
                    "Realises PRD feature(s): {}".format(
                        ", ".join(component.feature_ids) or "none — cross-cutting"
                    )
                )
                components.append(
                    replace(
                        component,
                        name=st.text_input(
                            "Component name",
                            value=component.name,
                            key="{}{}_name".format(_ARCH_WIDGET_PREFIX, component.component_id),
                        ),
                        responsibility=st.text_area(
                            "Responsibility",
                            value=component.responsibility,
                            key="{}{}_responsibility".format(
                                _ARCH_WIDGET_PREFIX, component.component_id
                            ),
                        ),
                        apis=_arch_lines(
                            "API boundaries (one per line)",
                            component.apis,
                            "{}_apis".format(component.component_id),
                        ),
                        data=_arch_lines(
                            "Data or state owned (one per line)",
                            component.data,
                            "{}_data".format(component.component_id),
                        ),
                        dependencies=_arch_lines(
                            "Dependencies (one per line)",
                            component.dependencies,
                            "{}_dependencies".format(component.component_id),
                        ),
                    )
                )

    decisions = []
    for decision in architecture.decisions:
        with st.expander("{} — {}".format(decision.decision_id, decision.title)):
            decisions.append(
                replace(
                    decision,
                    choice=st.text_area(
                        "Choice",
                        value=decision.choice,
                        key="{}{}_choice".format(_ARCH_WIDGET_PREFIX, decision.decision_id),
                    ),
                    rationale=st.text_area(
                        "Rationale",
                        value=decision.rationale,
                        key="{}{}_rationale".format(_ARCH_WIDGET_PREFIX, decision.decision_id),
                    ),
                )
            )

    flows = []
    for position, flow in enumerate(architecture.flows, start=1):
        with st.expander("Flow — {}".format(flow.name)):
            flows.append(
                replace(
                    flow,
                    steps=_arch_lines(
                        "Steps, in order (one per line)",
                        flow.steps,
                        "flow_{}_steps".format(position),
                    ),
                )
            )

    integrations = []
    for position, integration in enumerate(architecture.integrations, start=1):
        with st.expander("Integration — {}".format(integration.name)):
            integrations.append(
                replace(
                    integration,
                    purpose=st.text_area(
                        "Purpose",
                        value=integration.purpose,
                        key="{}integration_{}_purpose".format(_ARCH_WIDGET_PREFIX, position),
                    ),
                    direction=st.text_input(
                        "Direction",
                        value=integration.direction,
                        key="{}integration_{}_direction".format(_ARCH_WIDGET_PREFIX, position),
                    ),
                )
            )

    risks = []
    for position, risk in enumerate(architecture.risks, start=1):
        with st.expander("Risk — {}".format(risk.statement)):
            risks.append(
                replace(
                    risk,
                    impact=st.text_area(
                        "Impact",
                        value=risk.impact,
                        key="{}risk_{}_impact".format(_ARCH_WIDGET_PREFIX, position),
                    ),
                    mitigation=st.text_area(
                        "Mitigation",
                        value=risk.mitigation,
                        key="{}risk_{}_mitigation".format(_ARCH_WIDGET_PREFIX, position),
                    ),
                )
            )

    edited = replace(
        architecture,
        overview=overview,
        domains=domains,
        auth_approach=auth,
        dependencies=dependencies,
        components=tuple(components),
        decisions=tuple(decisions),
        flows=tuple(flows),
        integrations=tuple(integrations),
        risks=tuple(risks),
    )
    if edited != architecture:
        # Editing keeps the architecture pending review. Only the approval button approves it.
        return _persist_architecture(edited)
    return architecture


def _render_architecture_readonly(architecture: ArchitectureData) -> None:
    """The approved architecture, shown rather than offered for editing."""
    if architecture.overview:
        st.markdown(architecture.overview)
    for heading, values in (
        ("Core domains", architecture.domains),
        ("Authentication and authorization", architecture.auth_approach),
        ("Technical dependencies", architecture.dependencies),
    ):
        if values:
            st.markdown("**{}**".format(heading))
            for value in values:
                st.markdown("- {}".format(value))
    for layer in LAYERS:
        in_layer = architecture.layer(layer)
        if not in_layer:
            continue
        st.markdown("**{}**".format(LAYER_LABEL[layer]))
        for component in in_layer:
            st.markdown(
                "- **{} {}** — realises {}: {}".format(
                    component.component_id,
                    component.name,
                    ", ".join(component.feature_ids) or "cross-cutting",
                    component.responsibility,
                )
            )
            for label, values in (
                ("API", component.apis),
                ("Data", component.data),
                ("Depends on", component.dependencies),
            ):
                for value in values:
                    st.markdown("    - _{}_: {}".format(label, value))
    for decision in architecture.decisions:
        st.markdown(
            "**{} {}** — {} ({})".format(
                decision.decision_id, decision.title, decision.choice, decision.rationale
            )
        )
    for flow in architecture.flows:
        st.markdown("**Flow — {}**".format(flow.name))
        for step in flow.steps:
            st.markdown("- {}".format(step))
    for integration in architecture.integrations:
        st.markdown(
            "**Integration — {}** {} {}".format(
                integration.name, integration.direction, integration.purpose
            )
        )
    for risk in architecture.risks:
        st.markdown(
            "**Risk — {}** impact: {} mitigation: {}".format(
                risk.statement, risk.impact, risk.mitigation
            )
        )


def _render_architecture_stage(lifecycle) -> None:
    """
    The architecture stage: generate from the approved PRD, review, edit, approve explicitly.

    Blocked safely when there is no PRD or the PRD is not approved: the stage says what is
    missing instead of offering a control that would have to invent product scope.
    """
    prd = lifecycle.prd
    if prd is None or prd.is_empty:
        st.info(
            "No PRD in this session yet. Open the Product Definition → PRD stage and "
            "generate one; the architecture is derived from the approved PRD."
        )
        return
    if not _prd_approved() or not _brd_approved():
        st.info(
            "The PRD is pending review. Open the Product Definition → PRD stage and "
            "approve it to generate an architecture from it."
        )
        return

    discussion = _architecture_discussion()
    architecture = _held_architecture()

    if st.button("Generate architecture from the approved PRD", key="generate_architecture"):
        for key in list(st.session_state.keys()):
            if str(key).startswith(_ARCH_WIDGET_PREFIX):
                st.session_state.pop(key, None)
        st.session_state.pop(ARCHITECTURE_APPROVED_SESSION_KEY, None)
        with st.spinner("Deriving the technical architecture from the approved PRD..."):
            architecture = _persist_architecture(
                generate_architecture(prd, discussion, generate=_planner_generate())
            )

    if architecture is None:
        st.caption(
            "Not generated yet. This derives backend, web and mobile components, API "
            "boundaries, data ownership, authentication, data flows, integrations, "
            "decisions, dependencies and technical risks from the approved PRD. It "
            "creates nothing outside this session."
        )
        return

    for note in architecture.notes:
        st.warning(note)

    if architecture.is_empty:
        st.caption(
            "No architecture content could be derived. Nothing has been approved and "
            "nothing downstream was generated."
        )
        return

    if architecture.discussion_source:
        st.caption(
            "An architecture discussion ({}) was supplied alongside the PRD.".format(
                architecture.discussion_source
            )
        )
    _render_architecture_traceability(architecture, prd)

    if bool(st.session_state.get(ARCHITECTURE_APPROVED_SESSION_KEY)):
        st.success("This architecture is approved.")
        _render_architecture_readonly(architecture)
        if st.button("Revoke architecture approval to edit", key="revoke_architecture_approval"):
            st.session_state[ARCHITECTURE_APPROVED_SESSION_KEY] = False
        return

    architecture = _render_architecture_editor(architecture)
    st.caption(
        "Approving records that you reviewed this architecture, and unlocks the "
        "Implementation Plan stage. Nothing is created in Jira, and the sprint and test "
        "stages after it are not implemented yet."
    )
    if st.button("Approve architecture", key="approve_architecture"):
        st.session_state[ARCHITECTURE_APPROVED_SESSION_KEY] = True
        _flash("success", "Architecture approved.")


def _architecture_approved() -> bool:
    return bool(st.session_state.get(ARCHITECTURE_APPROVED_SESSION_KEY))


def _held_implementation_plan():
    plan = st.session_state.get(IMPLEMENTATION_PLAN_SESSION_KEY)
    return plan if isinstance(plan, ImplementationPlan) else None


def _persist_implementation_plan(plan: ImplementationPlan) -> ImplementationPlan:
    st.session_state[IMPLEMENTATION_PLAN_SESSION_KEY] = plan
    _clear_test_cases_state()
    return plan


def _held_test_cases():
    test_cases = st.session_state.get(TEST_CASES_SESSION_KEY)
    return test_cases if isinstance(test_cases, (list, tuple)) else None


def _persist_test_cases(test_cases) -> list:
    st.session_state[TEST_CASES_SESSION_KEY] = test_cases
    return test_cases


def _render_plan_traceability(
    plan: ImplementationPlan, prd: PRDData, architecture: ArchitectureData
) -> None:
    """
    What this plan builds, what it leaves unbuilt, and whether it is stale.

    Two coverage questions, not one: an uncovered PRD feature means the plan does not
    deliver approved product behaviour, and an uncovered architecture component means the
    plan does not build a part of the approved design. They fail differently, so they are
    reported separately.
    """
    st.caption(
        "Traceability: {} of {} PRD feature(s) delivered, {} of {} architecture "
        "component(s) built — {} epic(s), {} story/stories, {} task(s)".format(
            len(plan.covered_feature_ids),
            len(plan.source_feature_ids),
            len(plan.covered_component_ids),
            len(plan.source_component_ids),
            len(plan.epics),
            len(plan.stories),
            plan.task_count,
        )
    )
    if plan.uncovered_feature_ids:
        st.warning(
            "Not delivered by any story: {}. Either that is deliberate, or the plan is "
            "incomplete.".format(", ".join(plan.uncovered_feature_ids))
        )
    if plan.uncovered_component_ids:
        st.warning(
            "Not built by any story or task: {}. The approved design promises these "
            "components.".format(", ".join(plan.uncovered_component_ids))
        )
    unready = plan.unready_stories
    if unready:
        st.warning(
            "{} story/stories are not ready to implement: {}.".format(
                len(unready),
                "; ".join(
                    "{} ({})".format(story.story_id, ", ".join(story.readiness_gaps))
                    for story in unready[:5]
                ),
            )
        )
    if tuple(feature.feature_id for feature in prd.features) != tuple(plan.source_feature_ids):
        st.warning(
            "The PRD's features changed after this plan was generated. Regenerate it so "
            "the work matches the approved PRD."
        )
    if tuple(
        component.component_id for component in architecture.components
    ) != tuple(plan.source_component_ids):
        st.warning(
            "The architecture's components changed after this plan was generated. "
            "Regenerate it so the work matches the approved design."
        )


def _plan_lines(label: str, values, suffix: str) -> tuple:
    """One editable list of single-line statements."""
    return _criteria_from_text(
        st.text_area(label, value=_criteria_text(values), key=_PLAN_WIDGET_PREFIX + suffix)
    )


def _plan_priority(current: str, suffix: str) -> str:
    """A priority chosen from the vocabulary, so an unorderable value cannot be typed."""
    options = list(PRIORITIES)
    index = options.index(current) if current in options else options.index(DEFAULT_PRIORITY)
    return st.selectbox(
        "Priority", options, index=index, key=_PLAN_WIDGET_PREFIX + suffix
    )


def _plan_components(current, choices: list, suffix: str) -> tuple:
    """
    Architecture components chosen from a list rather than typed.

    Offered as a choice because a component id typed by hand can name a component the
    approved design does not contain, which is exactly the traceability the generator
    spends its notes protecting.
    """
    if not choices:
        return tuple(current)
    selected = st.multiselect(
        "Architecture components",
        choices,
        default=[value for value in current if value in choices],
        key=_PLAN_WIDGET_PREFIX + suffix,
    )
    return tuple(selected)


def _render_plan_story_editor(story, component_choices: list, story_choices: list):
    """One story's editable fields, including its technical tasks."""
    st.caption(
        "Delivers PRD feature(s): {} · {}".format(
            ", ".join(story.feature_ids) or "none",
            "ready to implement" if story.is_ready else "not ready: " + ", ".join(story.readiness_gaps),
        )
    )
    title = st.text_input(
        "Story title", value=story.title, key="{}{}_title".format(_PLAN_WIDGET_PREFIX, story.story_id)
    )
    user_story = st.text_area(
        "User story",
        value=story.user_story,
        key="{}{}_user_story".format(_PLAN_WIDGET_PREFIX, story.story_id),
        help="As a <role>, I want <capability> so that <benefit>.",
    )
    criteria = _plan_lines(
        "Acceptance criteria (one per line)",
        story.acceptance_criteria,
        "{}_criteria".format(story.story_id),
    )
    priority = _plan_priority(story.priority, "{}_priority".format(story.story_id))
    components = _plan_components(
        story.component_ids, component_choices, "{}_components".format(story.story_id)
    )
    others = [value for value in story_choices if value != story.story_id]
    depends_on = tuple(
        st.multiselect(
            "Depends on (stories that must be delivered first)",
            others,
            default=[value for value in story.depends_on if value in others],
            key="{}{}_depends".format(_PLAN_WIDGET_PREFIX, story.story_id),
        )
    )
    expectations = _plan_lines(
        "Test expectations (one per line)",
        story.test_expectations,
        "{}_tests".format(story.story_id),
    )
    estimate = st.text_input(
        "Estimate",
        value=story.estimate,
        key="{}{}_estimate".format(_PLAN_WIDGET_PREFIX, story.story_id),
    )

    tasks = []
    for task in story.tasks:
        st.markdown("**{} — {}**".format(task.label, task.task_id))
        tasks.append(
            replace(
                task,
                title=st.text_input(
                    "Task title",
                    value=task.title,
                    key="{}{}_title".format(_PLAN_WIDGET_PREFIX, task.task_id),
                ),
                detail=st.text_area(
                    "Task detail",
                    value=task.detail,
                    key="{}{}_detail".format(_PLAN_WIDGET_PREFIX, task.task_id),
                ),
                component_ids=_plan_components(
                    task.component_ids, component_choices, "{}_components".format(task.task_id)
                ),
                estimate=st.text_input(
                    "Task estimate",
                    value=task.estimate,
                    key="{}{}_estimate".format(_PLAN_WIDGET_PREFIX, task.task_id),
                ),
            )
        )

    return replace(
        story,
        title=title,
        user_story=user_story,
        acceptance_criteria=criteria,
        priority=priority,
        component_ids=components,
        depends_on=depends_on,
        test_expectations=expectations,
        estimate=estimate,
        tasks=tuple(tasks),
    )


def _render_plan_editor(
    plan: ImplementationPlan, architecture: ArchitectureData
) -> ImplementationPlan:
    """
    The review and edit surface: every edit is kept, and none of them approves anything.

    Feature ids are shown but not editable, for the reason the architecture editor does not
    let feature ids be typed over: traceability is derived from the artifact upstream, so
    letting it be rewritten here would let a reviewer claim coverage the PRD does not
    support. Component ids and dependencies *are* editable, because those are engineering
    decisions -- but they are chosen from a list, so neither can name something that does
    not exist.
    """
    overview = st.text_area(
        "Plan overview and sequencing",
        value=plan.overview,
        key=_PLAN_WIDGET_PREFIX + "overview",
        height=120,
    )
    component_choices = list(plan.source_component_ids) or list(
        component_index(architecture).keys()
    )
    story_choices = [story.story_id for story in plan.stories]

    epics = []
    stories: list = []
    edited_story_ids: set = set()
    for epic in plan.epics:
        under = plan.stories_for(epic.epic_id)
        with st.expander(
            "{} — {} ({} story/stories)".format(epic.epic_id, epic.name, len(under)),
            expanded=False,
        ):
            st.caption(
                "Delivers PRD feature(s): {}".format(", ".join(epic.feature_ids) or "none")
            )
            epics.append(
                replace(
                    epic,
                    name=st.text_input(
                        "Epic name",
                        value=epic.name,
                        key="{}{}_name".format(_PLAN_WIDGET_PREFIX, epic.epic_id),
                    ),
                    goal=st.text_area(
                        "Epic goal",
                        value=epic.goal,
                        key="{}{}_goal".format(_PLAN_WIDGET_PREFIX, epic.epic_id),
                    ),
                    priority=_plan_priority(
                        epic.priority, "{}_priority".format(epic.epic_id)
                    ),
                )
            )
            for story in under:
                st.markdown("---")
                st.markdown("**{} — {}**".format(story.story_id, story.title))
                stories.append(
                    _render_plan_story_editor(story, component_choices, story_choices)
                )
                edited_story_ids.add(story.story_id)

    orphans = tuple(story for story in plan.stories if story.story_id not in edited_story_ids)
    if orphans:
        st.markdown("**Stories under no epic**")
        for story in orphans:
            with st.expander("{} — {}".format(story.story_id, story.title), expanded=False):
                stories.append(
                    _render_plan_story_editor(story, component_choices, story_choices)
                )

    # Plan order, not editor order: the editor walks epics first and orphans last, and
    # reordering the plan as a side effect of rendering it would change what is approved.
    by_id = {story.story_id: story for story in stories}
    ordered = [by_id.get(story.story_id, story) for story in plan.stories]
    repaired, dropped = break_dependency_cycles(ordered)
    if dropped:
        st.warning(
            "Those dependencies would make the work unstartable, so the closing link was "
            "removed: {}.".format(", ".join(dropped))
        )

    edited = replace(plan, overview=overview, epics=tuple(epics), stories=tuple(repaired))
    if edited != plan:
        # Editing keeps the plan pending review. Only the approval button approves it.
        return _persist_implementation_plan(edited)
    return plan


def _render_plan_readonly(plan: ImplementationPlan) -> None:
    """The approved plan, shown rather than offered for editing."""
    if plan.overview:
        st.markdown(plan.overview)

    def show_story(story) -> None:
        st.markdown(
            "- **{} {}** [{}] — delivers {}{}".format(
                story.story_id,
                story.title,
                story.priority,
                ", ".join(story.feature_ids) or "nothing traced",
                " · after {}".format(", ".join(story.depends_on)) if story.depends_on else "",
            )
        )
        if story.user_story:
            st.markdown("    - _{}_".format(story.user_story))
        for criterion in story.acceptance_criteria:
            st.markdown("    - _Accepts_: {}".format(criterion))
        for task in story.tasks:
            st.markdown(
                "    - _{}_ {} — {}{}".format(
                    task.label,
                    task.task_id,
                    task.title,
                    " ({})".format(", ".join(task.component_ids)) if task.component_ids else "",
                )
            )
        for expectation in story.test_expectations:
            st.markdown("    - _Tests_: {}".format(expectation))

    for epic in plan.epics:
        st.markdown(
            "**{} {}** [{}] — {}".format(epic.epic_id, epic.name, epic.priority, epic.goal)
        )
        for story in plan.stories_for(epic.epic_id):
            show_story(story)
    orphans = plan.orphan_stories
    if orphans:
        st.markdown("**Stories under no epic**")
        for story in orphans:
            show_story(story)

    order = plan.ordered_story_ids
    if order:
        st.caption("Dependency order: {}".format(" → ".join(order)))


def _render_test_case_traceability(test_cases: list[TestSuite], plan: ImplementationPlan) -> None:
    """Show traceability from test cases back to stories and requirements."""
    if not test_cases:
        st.caption("No test cases to show traceability for.")
        return

    # Group test cases by story reference
    test_cases_by_story = {}
    for suite in test_cases:
        for tc in suite.test_cases:
            story_ref = tc.story_reference
            if story_ref not in test_cases_by_story:
                test_cases_by_story[story_ref] = []
            test_cases_by_story[story_ref].append(tc)

    st.caption(
        f"Traceability: {sum(len(suite.test_cases) for suite in test_cases)} test case(s) covering "
        f"{len(test_cases_by_story)} story(s) from the implementation plan."
    )

    # Show which stories have test cases
    story_ids_with_tests = set(test_cases_by_story.keys())
    all_story_ids = {story.story_id for story in plan.stories}
    stories_without_tests = all_story_ids - story_ids_with_tests

    if stories_without_tests:
        st.warning(
            "No test cases generated for story(s): {}. Either that is deliberate — "
            "record it as an open question — or test case generation is incomplete.".format(
                ", ".join(sorted(stories_without_tests))
            )
        )


def _render_test_case_readonly(test_cases: list[TestSuite]) -> None:
    """The approved test cases, shown rather than offered for editing."""
    if not test_cases:
        st.caption("No test cases available.")
        return

    for suite in test_cases:
        st.markdown("**Test Suite for Story {}**".format(suite.story_id))
        for test_case in suite.test_cases:
            st.markdown(
                "- **{}** [{}] — {}".format(
                    test_case.test_id,
                    test_case.priority,
                    test_case.scenario,
                )
            )
            if test_case.preconditions:
                st.markdown("    - _Preconditions_: {}".format(test_case.preconditions))
            if test_case.steps:
                st.markdown("    - _Steps_: {}".format(test_case.steps))
            if test_case.expected_result:
                st.markdown("    - _Expected Result_: {}".format(test_case.expected_result))
            st.markdown("    - _Type_: {}".format(test_case.test_type))

    st.caption("These test cases are approved and ready for execution.")


def _render_test_case_editor(test_cases: list[TestSuite]) -> list[TestSuite]:
    """Review and edit surface for test cases: every edit is kept, and none of them approves anything.

    Test IDs are shown but not editable to preserve traceability.
    Story reference is shown but not editable to preserve traceability.
    """
    if not test_cases:
        st.caption("No test cases to edit.")
        return test_cases

    edited_suites = []
    for suite_idx, suite in enumerate(test_cases):
        with st.expander(f"Test Suite for Story {suite.story_id}", expanded=True):
            edited_test_cases = []
            for tc_idx, test_case in enumerate(suite.test_cases):
                with st.container(border=True):
                    st.markdown(f"**Test Case {tc_idx + 1}**")
                    st.caption(f"*Test ID:* `{test_case.test_id}` (read-only for traceability)")
                    st.caption(f"*Story Reference:* `{test_case.story_reference}` (read-only for traceability)")

                    scenario = st.text_area(
                        "Scenario",
                        value=test_case.scenario,
                        key=f"{_TEST_CASES_WIDGET_PREFIX}suite_{suite_idx}_tc_{tc_idx}_scenario",
                        height=68,
                    )

                    preconditions = st.text_area(
                        "Preconditions",
                        value=test_case.preconditions,
                        key=f"{_TEST_CASES_WIDGET_PREFIX}suite_{suite_idx}_tc_{tc_idx}_preconditions",
                        height=68,
                    )

                    steps = st.text_area(
                        "Steps",
                        value=test_case.steps,
                        key=f"{_TEST_CASES_WIDGET_PREFIX}suite_{suite_idx}_tc_{tc_idx}_steps",
                        height=68,
                    )

                    expected_result = st.text_area(
                        "Expected Result",
                        value=test_case.expected_result,
                        key=f"{_TEST_CASES_WIDGET_PREFIX}suite_{suite_idx}_tc_{tc_idx}_expected_result",
                        height=68,
                    )

                    # Priority selector
                    priority_options = ["High", "Medium", "Low"]
                    try:
                        priority_index = priority_options.index(test_case.priority)
                    except ValueError:
                        priority_index = 1  # Default to Medium

                    priority = st.selectbox(
                        "Priority",
                        options=priority_options,
                        index=priority_index,
                        key=f"{_TEST_CASES_WIDGET_PREFIX}suite_{suite_idx}_tc_{tc_idx}_priority",
                    )

                    # Test type selector
                    test_type_options = ["Functional", "Negative", "Edge Case", "Integration", "Security"]
                    try:
                        test_type_index = test_type_options.index(test_case.test_type)
                    except ValueError:
                        test_type_index = 0  # Default to Functional

                    test_type = st.selectbox(
                        "Test Type",
                        options=test_type_options,
                        index=test_type_index,
                        key=f"{_TEST_CASES_WIDGET_PREFIX}suite_{suite_idx}_tc_{tc_idx}_test_type",
                    )

                    # Create updated test case
                    updated_test_case = TestCase(
                        test_id=test_case.test_id,
                        story_reference=test_case.story_reference,
                        scenario=scenario,
                        preconditions=preconditions,
                        steps=steps,
                        expected_result=expected_result,
                        priority=priority,
                        test_type=test_type,
                        is_approved=test_case.is_approved  # Preserve approval status
                    )
                    edited_test_cases.append(updated_test_case)

            # Create updated suite
            edited_suite = TestSuite(
                story_id=suite.story_id,
                test_cases=edited_test_cases
            )
            edited_suites.append(edited_suite)

    return edited_suites


def _render_implementation_plan_stage(lifecycle) -> None:
    """
    The implementation plan stage: generate from the approved PRD and architecture, review,
    edit, approve explicitly.

    Blocked safely when either upstream artifact is missing or unapproved: the stage says
    what is missing instead of offering a control that would have to invent scope or invent
    a system. Nothing here writes to Jira -- turning an approved plan into issues is the
    delivery stage's job, and doing it here would create work nobody had reviewed.
    """
    prd = lifecycle.prd
    architecture = lifecycle.architecture
    if architecture is None or architecture.is_empty:
        st.info(
            "No architecture in this session yet. Open the Architecture stage and generate "
            "one; the implementation plan is derived from the approved PRD and architecture."
        )
        return
    if not (_architecture_approved() and _prd_approved() and _brd_approved()):
        st.info(
            "The architecture is pending review. Open the Architecture stage and approve "
            "it to generate an implementation plan from it."
        )
        return

    plan = _held_implementation_plan()

    if st.button(
        "Generate implementation plan from the approved PRD and architecture",
        key="generate_implementation_plan",
    ):
        for key in list(st.session_state.keys()):
            if str(key).startswith(_PLAN_WIDGET_PREFIX):
                st.session_state.pop(key, None)
        st.session_state.pop(IMPLEMENTATION_PLAN_APPROVED_SESSION_KEY, None)
        with st.spinner("Decomposing the approved design into epics, stories and tasks..."):
            plan = _persist_implementation_plan(
                generate_implementation_plan(prd, architecture, generate=_planner_generate())
            )

    if plan is None:
        st.caption(
            "Not generated yet. This decomposes the approved PRD and architecture into "
            "epics, stories with acceptance criteria, and the technical tasks that build "
            "them, with priorities and dependency order. It creates nothing in Jira and "
            "nothing outside this session."
        )
        return

    for note in plan.notes:
        st.warning(note)

    if plan.is_empty:
        st.caption(
            "No plan content could be derived. Nothing has been approved and nothing "
            "downstream was generated."
        )
        return

    _render_plan_traceability(plan, prd, architecture)

    if bool(st.session_state.get(IMPLEMENTATION_PLAN_APPROVED_SESSION_KEY)):
        st.success("This implementation plan is approved.")

        # --- AI Coding Agent Capability (when implementation plan and test cases are approved) ---
        if lifecycle.state("test_cases").status == "Approved":
            _render_coding_agent_capability(lifecycle)
        else:
            st.info(
                "Generate and approve test cases to enable the AI Coding Agent."
            )

        _render_plan_readonly(plan)
        if st.button(
            "Revoke implementation plan approval to edit", key="revoke_plan_approval"
        ):
            st.session_state[IMPLEMENTATION_PLAN_APPROVED_SESSION_KEY] = False
        return

    # --- Test Execution Capability ---
    if bool(st.session_state.get(TEST_CASES_APPROVED_SESSION_KEY)) and lifecycle.state("implementation_plan").status == "Approved":
        _render_test_execution_capability(lifecycle)

    plan = _render_plan_editor(plan, architecture)
    st.caption(
        "Approving records that you reviewed this plan. Nothing is created in Jira: the "
        "delivery stage is what turns an approved plan into issues, and the sprint and "
        "test stages after it are not implemented yet."
    )
    if st.button("Approve implementation plan", key="approve_implementation_plan"):
        st.session_state[IMPLEMENTATION_PLAN_APPROVED_SESSION_KEY] = True
        _flash("success", "Implementation plan approved.")


def _render_coding_agent_capability(lifecycle) -> None:
    """
    AI Coding Agent capability: implements approved stories end-to-end.
    """
    st.markdown("#### AI Coding Agent")

    if lifecycle.state("implementation_plan").status != "Approved":
        st.info("Generate and approve an implementation plan to enable the AI Coding Agent.")
        return

    if lifecycle.state("test_cases").status != "Approved":
        st.info("Generate and approve test cases to enable AI Coding Agent verification.")
        return

    plan = lifecycle.implementation_plan
    if not plan or plan.is_empty:
        st.info("No implementation plan available.")
        return

    st.markdown("**Select Story to Implement**")
    story_options = {f"{story.story_id}: {story.title}": story for story in plan.stories}

    if not story_options:
        st.info("No stories found in implementation plan.")
        return

    selected_story_label = st.selectbox(
        "Choose a story from the approved implementation plan:",
        options=list(story_options.keys()),
        key="coding_agent_story_select"
    )

    selected_story = story_options.get(selected_story_label)
    if not selected_story:
        return

    with st.expander("Story Details", expanded=False):
        st.markdown(f"**ID:** {selected_story.story_id}")
        st.markdown(f"**Title:** {selected_story.title}")
        st.markdown(f"**User Story:** {selected_story.user_story}")
        st.markdown("**Acceptance Criteria:**")
        for criterion in selected_story.acceptance_criteria:
            st.markdown(f"- {criterion}")
        if selected_story.tasks:
            st.markdown("**Technical Tasks:**")
            for task in selected_story.tasks:
                st.markdown(f"- {task.title}")

    if st.button("Run AI Coding Agent", key="run_coding_agent"):
        with st.spinner("Running AI Coding Agent..."):
            from coding_agent import run_ai_coding_agent
            result = run_ai_coding_agent(selected_story)
            st.session_state.coding_agent_result = result
            st.session_state.coding_agent_story_id = selected_story.story_id

    if 'coding_agent_result' in st.session_state and st.session_state.coding_agent_story_id == selected_story.story_id:
        result = st.session_state.coding_agent_result
        st.markdown("### Execution Results")
        if result.blocked:
            st.error(f"**Blocked:** {result.blocked_reason}")
        else:
            st.success("**Execution Completed**")

        if result.files_changed:
            st.markdown("**Files Changed:**")
            for change in result.files_changed:
                if change.change_type == "created":
                    st.markdown(f"- ✅ **{change.file_path}** (created)")
                else:
                    st.markdown(f"- 🔄 **{change.file_path}** (modified)")
        else:
            st.markdown("**Files Changed:** None")

        if result.fix_attempts > 0:
            st.markdown(f"**Fix Attempts:** {result.fix_attempts}")

        if result.test_suites:
            st.markdown("**Test Results:**")
            for suite in result.test_suites:
                for tc in suite.test_cases:
                    st.markdown(f"- **{tc.test_id}**: {tc.scenario} ({tc.execution_status})")
        else:
            st.markdown("**Test Results:** None")

        if result.evidence_generated:
            st.info("✅ Evidence generated for traceability")
        else:
            st.warning("⚠️ No evidence generated")


def _render_plan_delivery_status(lifecycle) -> None:
    """
    What the approved implementation plan became in Jira, read-only.

    Reads session state and nothing else: no token, no request, so opening this stage
    cannot create, change or re-read anything. Status here means *delivery creation*
    status, which is a fact this app recorded when it created the issues. It is not an
    issue's Jira workflow status: this app has no read-issue endpoint, and the per-issue
    read that once existed was removed along with the requirement-drift feature it
    served. Saying "In Progress" would otherwise imply a workflow read that never
    happened.
    """
    mapping = st.session_state.get(_skey(JIRA_STATE_NAME, "delivery_mapping"))
    if not isinstance(mapping, DeliveryMapping) or mapping.is_empty:
        st.caption(
            "No implementation-plan item has been created in Jira yet, so there is no "
            "delivery mapping to show."
        )
        return

    st.markdown(
        "**{}** implementation-plan item(s) created in **{}**.".format(
            mapping.created_count, mapping.project_label or mapping.project_identifier
        )
    )
    _render_delivery_mapping(mapping, mapping.site_url)
    st.caption(
        "Recorded when each issue was created. Nothing in Jira changes the plan, the "
        "architecture, the PRD or the BRD — the trail runs one way."
    )


def _render_sprint_completion_capability(lifecycle) -> None:
    """
    Sprint Completion capability: evaluate the current sprint using implementation,
    review, and test evidence; propose next sprint; preserve history.
    This capability lives inside the Delivery Status area and is not a separate
    lifecycle stage.
    """
    # Need approved implementation plan and test cases to evaluate a sprint
    plan = lifecycle.implementation_plan
    if plan is None or getattr(plan, "is_empty", True):
        st.info(
            "Generate and approve an implementation plan to use sprint completion."
        )
        return
    if not bool(st.session_state.get(IMPLEMENTATION_PLAN_APPROVED_SESSION_KEY)):
        st.info(
            "The implementation plan is pending review. Approve it to enable sprint completion."
        )
        return
    test_cases = _held_test_cases()
    if test_cases is None:
        st.info(
            "Generate test cases to enable sprint completion evidence evaluation."
        )
        return
    if not bool(st.session_state.get(TEST_CASES_APPROVED_SESSION_KEY)):
        st.info(
            "The test cases are pending review. Approve them to enable sprint completion."
        )
        return

    # Find the most recent sprint plan in delivery mapping (if any) or session
    mapping = st.session_state.get(_skey(JIRA_STATE_NAME, "delivery_mapping"))
    if mapping is None or mapping.is_empty:
        st.info(
            "Create Jira issues from the implementation plan to enable sprint completion."
        )
        return

    # For simplicity, we assume the delivery mapping contains the current sprint's issues.
    # In a fuller implementation, we would have a selected sprint plan stored in session.
    # Here we reuse the existing sprint recommendation logic to get a plan from the mapping.
    from sprint_generator import recommend_sprint
    sprint_plan = recommend_sprint(mapping)
    if sprint_plan is None or getattr(sprint_plan, "is_empty", True):
        st.info(
            "The delivery mapping contains no issues; cannot evaluate sprint completion."
        )
        return

    # Evaluate sprint completion using the evidence we have
    sprint_completion = complete_sprint(
        lifecycle, sprint_plan, test_cases=test_cases
    )

    # Display completion results
    st.markdown("### Sprint Completion")
    st.markdown(f"**Sprint:** {sprint_plan.sprint_name}")
    st.markdown(f"**Goal:** {sprint_plan.sprint_goal}")
    st.markdown(f"**Status:** {sprint_completion.overall_status}")
    if sprint_completion.approved:
        st.success("✅ Sprint completion approved by reviewer")
    else:
        st.warning("⏳ Sprint completion pending reviewer approval")

    # Show per-story completion
    completed_count = sum(
        1 for sc in sprint_completion.story_completions if sc.is_completed
    )
    total_stories = len(sprint_completion.story_completions)
    st.caption(
        f"{completed_count}/{total_stories} stories complete "
        f"({sprint_completion.overall_status.lower()})"
    )

    with st.expander("Story completion details"):
        for sc in sprint_completion.story_completions:
            status = "✅ Done" if sc.is_completed else "❌ Not done"
            st.markdown(
                f"- **{sc.story_id or '(no story id)'}**: {status} — {sc.detail}"
            )

    # Show remaining backlog (unfinished stories carried forward)
    if sprint_completion.remaining_backlog:
        st.markdown("### Remaining Backlog")
        st.caption(
            "These stories were not completed and will be carried into the next sprint."
        )
        for issue in sprint_completion.remaining_backlog:
            st.markdown(
                f"- **{issue.issue_key}**: {issue.summary}"
                f"  \n  *{issue.rationale}*"
            )

    # Show next sprint recommendation
    next_sprint = recommend_next_sprint(lifecycle, sprint_completion)
    st.markdown("### Next Sprint Recommendation")
    st.markdown(f"**Sprint:** {next_sprint.sprint_name}")
    st.markdown(f"**Goal:** {next_sprint.sprint_goal}")
    st.markdown(f"**Duration:** {next_sprint.duration_weeks} weeks")

    # Approval controls for the next sprint
    col1, col2 = st.columns(2)
    with col1:
        if st.button(
            "Approve Next Sprint",
            key="sprint_completion_approve_next",
            disabled=sprint_completion.approved,
        ):
            # Mark the next sprint as approved (store in session)
            st.session_state[SPRINT_COMPLETION_NEXT_KEY] = next_sprint
            st.session_state[SPRINT_COMPLETION_APPROVED_KEY] = True
            st.success("Next sprint approved")
            st.rerun()
    with col2:
        if st.button(
            "Save Sprint Completion",
            key="sprint_completion_save",
            disabled=sprint_completion.approved,
        ):
            # Save this sprint completion to history
            history = st.session_state.get(SPRINT_COMPLETION_HISTORY_KEY, [])
            history.append(sprint_completion)
            st.session_state[SPRINT_COMPLETION_HISTORY_KEY] = history
            st.session_state[SPRINT_COMPLETION_LAST_KEY] = sprint_completion
            st.success("Sprint completion saved to history")
            st.rerun()

    # Show history of saved sprint completions
    history = st.session_state.get(SPRINT_COMPLETION_HISTORY_KEY, [])
    if history:
        st.markdown("### Sprint Completion History")
        st.caption(f"Saved completions: {len(history)}")
        for i, sc in enumerate(reversed(history)):
            with st.expander(
                f"{sc.sprint_plan.sprint_name} — {sc.overall_status} "
                f"({'✅ Approved' if sc.approved else '⏳ Pending'})"
            ):
                st.markdown(f"**Goal:** {sc.sprint_plan.sprint_goal}")
                completed = sum(
                    1 for s in sc.story_completions if s.is_completed
                )
                total = len(sc.story_completions)
                st.caption(f"{completed}/{total} stories complete")
                if not sc.approved:
                    st.caption("⏳ Pending reviewer approval")


def _render_test_execution_capability(lifecycle) -> None:
    """
    Test Execution Capability: Execute approved test cases and collect execution evidence.

    This capability executes repository tests for approved test cases, captures
    execution evidence, and requires human approval before this evidence is
    consumed by Sprint Completion.

    Execution mapping:
    - exit code 0 → Pass
    - non-zero test failure → Fail
    - unable/unexecutable → Blocked/Not Run with clear reason
    - no executable mapping → Not Run

    Safety:
    - workspace confinement, timeout, safe subprocess
    - no arbitrary path traversal
    - evidence captured with test IDs and story references
    - human approval required before delivery evidence
    """
    st.markdown("### Test Execution & Delivery Evidence")

    # Need approved test cases and implementation plan
    test_cases = _held_test_cases()
    if not test_cases:
        st.info("Generate and approve test cases to enable test execution.")
        return

    if not bool(st.session_state.get(TEST_CASES_APPROVED_SESSION_KEY)):
        st.info("The test cases are pending review. Approve them to enable test execution.")
        return

    if lifecycle.state("implementation_plan").status != "Approved":
        st.info("Generate and approve an implementation plan to enable test execution.")
        return

    # Get the implementation plan
    plan = lifecycle.implementation_plan
    if not plan or plan.is_empty:
        st.info("No implementation plan available.")
        return

    st.caption("Execution evidence is required for Sprint Completion to determine story completion.")

    # Select story to execute
    story_options = {f"{story.story_id}: {story.title}": story for story in plan.stories}

    if not story_options:
        st.info("No stories found in implementation plan.")
        return

    selected_story_label = st.selectbox(
        "Choose a story to execute:",
        options=list(story_options.keys()),
        key="test_execution_story_select"
    )

    selected_story = story_options.get(selected_story_label)
    if not selected_story:
        return

    with st.expander("Story Details", expanded=False):
        st.markdown(f"**ID:** {selected_story.story_id}")
        st.markdown(f"**Title:** {selected_story.title}")
        st.markdown(f"**User Story:** {selected_story.user_story}")
        st.markdown("**Acceptance Criteria:**")
        for criterion in selected_story.acceptance_criteria:
            st.markdown(f"- {criterion}")
        if selected_story.tasks:
            st.markdown("**Technical Tasks:**")
            for task in selected_story.tasks:
                st.markdown(f"- {task.title}")

    # Check if we have existing execution evidence for this story
    execution_evidence_key = f"execution_evidence_{selected_story.story_id}"
    existing_evidence = st.session_state.get(execution_evidence_key)

    if st.button("Execute Tests for this Story", key=f"execute_tests_{selected_story.story_id}"):
        with st.spinner(f"Executing tests for story {selected_story.story_id}..."):
            try:
                # Generate execution evidence
                evidence = generate_execution_evidence(
                    selected_story.story_id,
                    test_cases,
                    os.getcwd()  # Use current workspace
                )

                # Store evidence in session state
                st.session_state[execution_evidence_key] = evidence

                st.success(f"Test execution completed for story {selected_story.story_id}")
                st.rerun()

            except Exception as e:
                st.error(f"Test execution failed: {str(e)}")
                logger.error(f"Test execution error: {e}")

    # Display execution evidence if available
    if existing_evidence:
        st.markdown("#### Execution Evidence")

        # Show execution summary
        summary = get_execution_status_summary(existing_evidence)
        st.markdown(f"**Status:** {summary['status']}")
        st.markdown(f"**Results:** {summary['passed']} passed, {summary['failed']} failed, {summary['blocked']} blocked, {summary['not_run']} not run")

        # Show detailed results
        if existing_evidence.session.execution_results:
            st.markdown("**Test Results:**")
            for result in existing_evidence.session.execution_results:
                status_emoji = {
                    TEST_EXECUTION_PASS: "✅",
                    TEST_EXECUTION_FAIL: "❌",
                    TEST_EXECUTION_BLOCKED: "⚠️",
                    TEST_EXECUTION_NOT_RUN: "⏸️"
                }.get(result.execution_status, "❓")

                st.markdown(f"{status_emoji} **{result.test_id}**: {result.execution_status}")
                if result.actual_result:
                    st.caption(f"Result: {result.actual_result}")
                if result.notes:
                    st.caption(f"Notes: {result.notes}")
                if result.defect_reference:
                    st.caption(f"Defect: {result.defect_reference}")

        # Human approval section
        if not existing_evidence.approved:
            st.markdown("#### Human Approval Required")
            st.caption("Execution evidence must be approved before it can be used for Sprint Completion decisions.")

            approver_notes = st.text_area(
                "Approval Notes (optional)",
                key=f"approver_notes_{selected_story.story_id}",
                height=100,
                placeholder="Add any notes about the test execution results..."
            )

            col1, col2 = st.columns(2)
            with col1:
                if st.button("Approve Execution Evidence", key=f"approve_evidence_{selected_story.story_id}"):
                    try:
                        approved_evidence = approve_test_execution(
                            existing_evidence,
                            approver_notes
                        )
                        st.session_state[execution_evidence_key] = approved_evidence
                        st.success("Execution evidence approved!")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Approval failed: {str(e)}")
            with col2:
                if st.button("Reject Evidence", key=f"reject_evidence_{selected_story.story_id}"):
                    # Clear the evidence so user can re-run
                    st.session_state.pop(execution_evidence_key, None)
                    st.info("Execution evidence cleared. You can re-run tests.")
                    st.rerun()
        else:
            st.success("✅ Execution evidence approved and ready for Sprint Completion")
            st.caption(f"Approver notes: {existing_evidence.human_approval_notes or 'None'}")

    # Show how this evidence will be used
    st.markdown("#### How This Evidence is Used")
    st.caption("""
    - Approved execution evidence is consumed by Sprint Completion to determine story completion
    - Stories require: implementation complete, review complete, and all tests passing (if testing required)
    - Test execution evidence shows actual test results (pass/fail/blocked/not run)
    - Sprint Completion will not mark stories as complete if tests are failing or blocked
    """)

def _render_test_cases_stage(lifecycle) -> None:
    """
    The test cases stage: generate from the approved implementation plan, review, edit,
    approve explicitly.

    Blocked safely when the upstream plan is missing or unapproved: the stage says
    what is missing instead of offering a control that would have to invent scope.
    Nothing here writes to Jira or anywhere else -- nothing is marked Done merely
    because test cases were generated.
    """
    plan = lifecycle.implementation_plan
    if plan is None or getattr(plan, "is_empty", True):
        st.info(
            "No implementation plan in this session yet. Open the Implementation Plan "
            "stage and generate one; the test cases are derived from the approved plan."
        )
        return
    if not bool(st.session_state.get(IMPLEMENTATION_PLAN_APPROVED_SESSION_KEY)):
        st.info(
            "The implementation plan is pending review. Open the Implementation Plan "
            "stage and approve it to generate test cases from it."
        )
        return

    test_cases = _held_test_cases()

    if st.button(
        "Generate test cases from the approved implementation plan",
        key="generate_test_cases",
    ):
        for key in list(st.session_state.keys()):
            if str(key).startswith(_TEST_CASES_WIDGET_PREFIX):
                st.session_state.pop(key, None)
        st.session_state.pop(TEST_CASES_APPROVED_SESSION_KEY, None)
        with st.spinner("Generating test cases from the approved implementation plan..."):
            try:
                generated = generate_test_suite(plan, client=CLIENT)
            except Exception:
                generated = _fallback_test_suite(plan)
            if not generated:
                generated = _fallback_test_suite(plan)
            test_cases = _persist_test_cases(generated)

    if test_cases is None:
        st.caption(
            "Not generated yet. This generates test cases from each story in the "
            "approved implementation plan, covering functional, negative, edge case, "
            "integration, and security tests where appropriate."
        )
        return

    if not test_cases:
        st.caption(
            "No test cases could be derived from the approved implementation plan."
        )
        return

    _render_test_case_traceability(test_cases, plan)

    if bool(st.session_state.get(TEST_CASES_APPROVED_SESSION_KEY)):
        st.success("These test cases are approved.")
        _render_test_case_readonly(test_cases)
        if st.button("Revoke test cases approval to edit", key="revoke_test_cases_approval"):
            st.session_state[TEST_CASES_APPROVED_SESSION_KEY] = False
        return

    st.caption(
        "Approving records that you reviewed these test cases. Nothing is written to "
        "Jira or to the plan."
    )
    if st.button("Approve test cases", key="approve_test_cases"):
        st.session_state[TEST_CASES_APPROVED_SESSION_KEY] = True
        _flash("success", "Test cases approved.")
        return

    # Edit mode: show editable test cases
    edited_test_cases = _render_test_case_editor(test_cases)
    if edited_test_cases is not test_cases:
        # User made edits, persist them
        test_cases = _persist_test_cases(edited_test_cases)
        st.caption("Test cases updated. Review your changes and approve when ready.")


def _render_test_execution_traceability(test_cases, test_execution) -> None:
    """Show traceability from test execution back to test cases."""
    if not test_cases:
        st.caption("No test cases available.")
        return

    st.caption(
        "Traceability: Test execution results for {} test case(s). "
        "Each result links back to its test case, story and Jira issue.".format(
            sum(len(suite.test_cases) for suite in test_cases)
        )
    )


def _render_test_execution_editor(test_cases) -> list:
    """Review and update surface for test execution results.

    Test IDs are shown but not editable to preserve traceability.
    Story reference is shown but not editable to preserve traceability.
    """
    if not test_cases:
        return []

    updated_suites = []
    for suite_idx, suite in enumerate(test_cases):
        updated_test_cases = []
        for tc_idx, test_case in enumerate(suite.test_cases):
            with st.container(border=True):
                st.markdown(f"**Test Case {tc_idx + 1}: {test_case.test_id}**")
                st.caption(f"*Story Reference:* `{test_case.story_reference}` (read-only)")
                st.caption(f"*Scenario:* {test_case.scenario}")
                st.caption(f"*Type:* {test_case.test_type} | *Priority:* {test_case.priority}")

                # Execution status
                status_options = [
                    TEST_EXECUTION_NOT_RUN,
                    TEST_EXECUTION_PASS,
                    TEST_EXECUTION_FAIL,
                    TEST_EXECUTION_BLOCKED,
                ]
                try:
                    status_index = status_options.index(test_case.execution_status)
                except ValueError:
                    status_index = 0

                new_status = st.selectbox(
                    "Execution Status",
                    options=status_options,
                    index=status_index,
                    key=f"{_TEST_EXECUTION_WIDGET_PREFIX}suite_{suite_idx}_tc_{tc_idx}_status",
                )

                # Actual result
                new_actual_result = st.text_area(
                    "Actual Result",
                    value=test_case.actual_result,
                    key=f"{_TEST_EXECUTION_WIDGET_PREFIX}suite_{suite_idx}_tc_{tc_idx}_actual_result",
                    height=68,
                )

                # Notes
                new_notes = st.text_area(
                    "Notes",
                    value=test_case.notes,
                    key=f"{_TEST_EXECUTION_WIDGET_PREFIX}suite_{suite_idx}_tc_{tc_idx}_notes",
                    height=68,
                )

                # Defect reference
                new_defect_reference = st.text_input(
                    "Defect/Bug Reference (optional)",
                    value=test_case.defect_reference,
                    key=f"{_TEST_EXECUTION_WIDGET_PREFIX}suite_{suite_idx}_tc_{tc_idx}_defect",
                )

                # Create updated test case with execution data
                updated_test_case = TestCase(
                    test_id=test_case.test_id,
                    story_reference=test_case.story_reference,
                    scenario=test_case.scenario,
                    preconditions=test_case.preconditions,
                    steps=test_case.steps,
                    expected_result=test_case.expected_result,
                    priority=test_case.priority,
                    test_type=test_case.test_type,
                    is_approved=test_case.is_approved,
                    execution_status=new_status,
                    actual_result=new_actual_result,
                    notes=new_notes,
                    defect_reference=new_defect_reference,
                )
                updated_test_cases.append(updated_test_case)

        updated_suite = TestSuite(
            story_id=suite.story_id,
            test_cases=updated_test_cases
        )
        updated_suites.append(updated_suite)

    return updated_suites


def _render_test_execution_stage(lifecycle) -> None:
    """
    The test execution stage: record execution results from the approved test cases.

    Blocked safely when the upstream test cases are missing or unapproved: the stage says
    what is missing instead of offering a control that would have to invent evidence.
    Nothing here writes to Jira or anywhere else -- nothing is marked Done merely
    because test execution was recorded.
    """
    test_cases = _held_test_cases()
    if test_cases is None:
        st.info(
            "No test cases have been generated yet. Open the Test Cases stage and "
            "generate them first; test execution is recorded against approved test cases."
        )
        return
    if not bool(st.session_state.get(TEST_CASES_APPROVED_SESSION_KEY)):
        st.info(
            "The test cases are pending review. Open the Test Cases stage and approve "
            "them to record execution results."
        )
        return

    _render_test_execution_traceability(test_cases, _held_test_execution())

    # Edit mode: show editable test execution
    updated_suites = _render_test_execution_editor(test_cases)
    if updated_suites:
        _persist_test_execution(updated_suites)
        st.caption("Test execution results updated.")

    # Show summary
    if _held_test_execution():
        updated_suites = _held_test_execution()
    else:
        updated_suites = test_cases

    if not updated_suites:
        return

    # Count execution results
    total_tests = 0
    passed = 0
    failed = 0
    blocked = 0
    not_run = 0
    defects = []

    for suite in updated_suites:
        for tc in suite.test_cases:
            total_tests += 1
            if tc.execution_status == TEST_EXECUTION_PASS:
                passed += 1
            elif tc.execution_status == TEST_EXECUTION_FAIL:
                failed += 1
                if tc.defect_reference:
                    defects.append(f"{tc.test_id}: {tc.defect_reference}")
            elif tc.execution_status == TEST_EXECUTION_BLOCKED:
                blocked += 1
            else:
                not_run += 1

    st.markdown("**Execution Summary:**")
    st.markdown(f"- Total: {total_tests}")
    st.markdown(f"- Passed: {passed}")
    st.markdown(f"- Failed: {failed}")
    st.markdown(f"- Blocked: {blocked}")
    st.markdown(f"- Not Run: {not_run}")

    if defects:
        st.markdown("**Defects/Bugs:**")
        for defect in defects:
            st.markdown(f"- {defect}")

    st.caption(
        "Recording execution results does not mark Jira issues as Done. Completion "
        "requires explicit confirmation based on actual evidence."
    )


def _render_lifecycle_stage(lifecycle, stage: str) -> None:
    """
    One stage: its status, and either where it already lives or that it is not built.

    A stage without a generator gets no control at all. A button that produced nothing
    would claim a capability this app does not have.
    """
    st.markdown("#### {}".format(STAGE_LABEL[stage]))
    state = lifecycle.state(stage)
    st.markdown("**Status:** {}".format(state.status))
    if state.detail:
        st.caption(state.detail)

    if stage == DISCOVERY_BRD:
        if lifecycle.brd is None:
            st.info(
                "Select a transcript source at the top of this page and generate a BRD. "
                "Manual paste, .txt upload, Google Meet and Microsoft Teams all work."
            )
        else:
            st.success("Current BRD: **{}**".format(lifecycle.brd.project_title or "Untitled"))
            st.caption(
                "The BRD, its evidence validation and its Markdown export are rendered "
                "above. This stage reports its state; it does not repeat it."
            )
            _render_brd_approval()
    elif stage == PRD:
        _render_prd_stage(lifecycle)
    elif stage == ARCHITECTURE:
        _render_architecture_stage(lifecycle)
    elif stage == IMPLEMENTATION_PLAN:
        _render_implementation_plan_stage(lifecycle)
    elif stage == TEST_CASES:
        _render_test_cases_stage(lifecycle)
    elif stage == TEST_EXECUTION:
        _render_test_execution_stage(lifecycle)
    elif stage == DELIVERY_STATUS:
        st.caption(
            "The Jira connection, site and project selection, work plan, review and "
            "issue creation are in the Jira section above, along with the created issue "
            "keys and the requirements each one came from. Step 6 there maps the approved "
            "implementation plan onto this project's own issue hierarchy and records the "
            "plan item → issue key mapping."
        )
        _render_plan_delivery_status(lifecycle)
        _render_sprint_completion_capability(lifecycle)
    else:
        st.info(
            "{} is not implemented yet. Nothing here generates an artifact.".format(
                STAGE_LABEL[stage]
            )
        )


def _render_lifecycle_workspace() -> None:
    """
    The project workspace: every lifecycle stage, its status, and the selected stage.

    Read-only. Each status is derived from an artifact this session actually holds, so
    no stage can report progress that was not made, and the stages with no generator
    yet say so rather than offering a control that does nothing.
    """
    st.divider()
    st.subheader("Project delivery lifecycle")
    st.caption(
        "The delivery flow this project is being built towards. Discovery → BRD, "
        "Product Definition → PRD, Architecture, Implementation Plan and the Jira "
        "delivery stage are implemented; the stages between them are navigable and "
        "report that they are not implemented yet."
    )

    brd_data = st.session_state.get(BRD_SESSION_KEY)
    prd_data = st.session_state.get(PRD_SESSION_KEY)
    architecture_data = st.session_state.get(ARCHITECTURE_SESSION_KEY)
    plan_data = st.session_state.get(IMPLEMENTATION_PLAN_SESSION_KEY)
    test_cases_data = st.session_state.get(TEST_CASES_SESSION_KEY)
    lifecycle = lifecycle_from(
        brd=brd_data if isinstance(brd_data, BRDData) else None,
        discovery_source=str(st.session_state.get(BRD_SOURCE_SESSION_KEY) or ""),
        plan=st.session_state.get(_skey(JIRA_STATE_NAME, "plan")),
        created=st.session_state.get(_skey(JIRA_STATE_NAME, "created")) or (),
        brd_approved=_brd_approved(),
        prd=prd_data if isinstance(prd_data, PRDData) else None,
        prd_approved=bool(st.session_state.get(PRD_APPROVED_SESSION_KEY)),
        architecture=(
            architecture_data if isinstance(architecture_data, ArchitectureData) else None
        ),
        architecture_approved=bool(st.session_state.get(ARCHITECTURE_APPROVED_SESSION_KEY)),
        implementation_plan=(
            plan_data if isinstance(plan_data, ImplementationPlan) else None
        ),
        implementation_plan_approved=bool(
            st.session_state.get(IMPLEMENTATION_PLAN_APPROVED_SESSION_KEY)
        ),
        test_cases=test_cases_data,
        test_cases_approved=bool(st.session_state.get(TEST_CASES_APPROVED_SESSION_KEY)),
        delivery_mapping=st.session_state.get(_skey(JIRA_STATE_NAME, "delivery_mapping")),
    )
    for position, stage in enumerate(LIFECYCLE_STAGES, start=1):
        st.markdown(
            "{}. **{}** — {}{}".format(
                position,
                STAGE_LABEL[stage],
                lifecycle.state(stage).status,
                "" if stage in IMPLEMENTED_STAGES else " (not implemented yet)",
            )
        )

    labels = [STAGE_LABEL[stage] for stage in LIFECYCLE_STAGES]
    st.sidebar.markdown("### Project workspace")
    selected = st.sidebar.radio("Lifecycle stage", labels, key="lifecycle_stage")
    stage = LIFECYCLE_STAGES[labels.index(selected)] if selected in labels else DISCOVERY_BRD
    _render_lifecycle_stage(lifecycle, stage)


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
            _store_brd(brd_data, transcript_to_process.source)

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

# --- Project Lifecycle Workspace ---
# Rendered last so the status it reports can read the BRD above and the plan and
# created issues from the Jira section. It takes no service and no token.
_render_lifecycle_workspace()
