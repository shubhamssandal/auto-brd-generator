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
from providers import GoogleMeetProvider, MSTeamsProvider


# Load environment variables from a .env file
load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# --- Global Gemini Client Configuration ---
CLIENT = None

if GEMINI_API_KEY:
    CLIENT = genai.Client(api_key=GEMINI_API_KEY)

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
        model="gemini-3.6-flash",
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



# --- Main Streamlit App ---

st.title("Auto-BRD Generator")
st.write(
    "Convert raw meeting notes and transcripts into a structured Business Requirements Document."
)

# Check for OAuth redirects in query parameters
query_params = st.query_params
if "code" in query_params:
    oauth_code = query_params.get("code")
    oauth_state = query_params.get("state", "")
    redirect_uri = os.getenv("APP_REDIRECT_URI", "http://localhost:8501")

    if oauth_state == "google_meet":
        meet_provider = GoogleMeetProvider()
        try:
            tokens = meet_provider.exchange_code_for_token(oauth_code, redirect_uri)
            st.session_state["google_access_token"] = tokens.get("access_token")
            st.session_state["google_refresh_token"] = tokens.get("refresh_token")
            st.success("Successfully authenticated with Google Workspace!")
        except Exception as e:
            st.error(f"Google Authentication Error: {e}")
        st.query_params.clear()

    elif oauth_state == "microsoft_teams":
        teams_provider = MSTeamsProvider()
        try:
            tokens = teams_provider.exchange_code_for_token(oauth_code, redirect_uri)
            st.session_state["teams_access_token"] = tokens.get("access_token")
            st.session_state["teams_refresh_token"] = tokens.get("refresh_token")
            st.success("Successfully authenticated with Microsoft Teams!")
        except Exception as e:
            st.error(f"Microsoft Teams Authentication Error: {e}")
        st.query_params.clear()

# --- Transcript Source Selection ---
source_option = st.radio(
    "Select Transcript Source",
    (
        "Manual Paste",
        "Upload Transcript File (.txt)",
        "Google Meet",
        "Microsoft Teams",
    ),
    horizontal=True,
)

transcript_to_process: Optional[NormalizedTranscript] = None
app_redirect_uri = os.getenv("APP_REDIRECT_URI", "http://localhost:8501")

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
    meet_provider = GoogleMeetProvider()

    if not meet_provider.is_configured():
        st.warning("Status: Provider Not Configured")
        missing = meet_provider.get_missing_configuration()
        if missing:
            st.markdown(f"**Missing Configuration Variables:** `{', '.join(missing)}`")
        with st.expander("View Setup & Configuration Requirements", expanded=True):
            st.markdown(meet_provider.get_setup_instructions())
    else:
        st.success("Google Meet credentials detected in environment.")
        google_token = st.session_state.get("google_access_token")

        if not google_token:
            auth_url = meet_provider.get_authorization_url(app_redirect_uri, state="google_meet")
            st.info("Authenticate with your Google Workspace account to access meeting transcripts.")
            st.link_button("Connect Google Workspace", auth_url, type="primary")
        else:
            st.write("Status: **Connected to Google Workspace**")
            col_a, col_b = st.columns([3, 1])
            with col_b:
                if st.button("Disconnect Google", key="disconnect_google"):
                    st.session_state.pop("google_access_token", None)
                    st.session_state.pop("google_refresh_token", None)
                    st.rerun()

            meeting_id_input = st.text_input(
                "Enter Google Meet Conference Record ID or Google Drive Transcript File ID",
                placeholder="e.g. conferenceRecords/abc-xyz-123 or Google Drive Document ID",
            )
            if st.button("Fetch & Generate BRD", key="generate_google_meet"):
                if not meeting_id_input.strip():
                    st.error("Please enter a meeting or document ID.")
                else:
                    try:
                        with st.spinner("Fetching transcript from Google Meet / Drive..."):
                            transcript_to_process = meet_provider.get_transcript(
                                meeting_id_input.strip(), access_token=google_token
                            )
                    except ProviderError as pe:
                        st.error(f"Google Meet error: {pe}")
                    except Exception as ex:
                        st.error(f"Unexpected error retrieving Google transcript: {ex}")

elif source_option == "Microsoft Teams":
    st.subheader("Microsoft Teams Integration")
    teams_provider = MSTeamsProvider()

    if not teams_provider.is_configured():
        st.warning("Status: Provider Not Configured")
        missing = teams_provider.get_missing_configuration()
        if missing:
            st.markdown(f"**Missing Configuration Variables:** `{', '.join(missing)}`")
        with st.expander("View Setup & Configuration Requirements", expanded=True):
            st.markdown(teams_provider.get_setup_instructions())
    else:
        st.success("Microsoft Teams credentials detected in environment.")
        teams_token = st.session_state.get("teams_access_token")

        if not teams_token:
            auth_url = teams_provider.get_authorization_url(app_redirect_uri, state="microsoft_teams")
            st.info("Authenticate with your Microsoft 365 account to access Teams transcripts.")
            st.link_button("Connect Microsoft Teams", auth_url, type="primary")
        else:
            st.write("Status: **Connected to Microsoft Teams**")
            col_a, col_b = st.columns([3, 1])
            with col_b:
                if st.button("Disconnect Teams", key="disconnect_teams"):
                    st.session_state.pop("teams_access_token", None)
                    st.session_state.pop("teams_refresh_token", None)
                    st.rerun()

            teams_id_input = st.text_input(
                "Enter Teams Meeting ID or Transcript Resource Path",
                placeholder="e.g. MSo0ZjQ.../transcripts/MS1lY2... or Online Meeting ID",
            )
            if st.button("Fetch & Generate BRD", key="generate_ms_teams"):
                if not teams_id_input.strip():
                    st.error("Please enter a Teams meeting or transcript ID.")
                else:
                    try:
                        with st.spinner("Fetching transcript from Microsoft Graph..."):
                            transcript_to_process = teams_provider.get_transcript(
                                teams_id_input.strip(), access_token=teams_token
                            )
                    except ProviderError as pe:
                        st.error(f"Microsoft Teams error: {pe}")
                    except Exception as ex:
                        st.error(f"Unexpected error retrieving Teams transcript: {ex}")

# --- Unified BRD Generation Pipeline ---
if transcript_to_process:
    if not GEMINI_API_KEY:
        st.error("Gemini API key is not configured. Please set it in your .env file.")
    elif not CLIENT:
        st.error("Gemini client could not be initialized. Please check your API key.")
    else:
        try:
            with st.spinner("Analyzing transcript and generating BRD with Gemini..."):
                brd_data = generate_brd_from_transcript(transcript_to_process)

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


