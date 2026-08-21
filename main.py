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
    resolve_handshake,
)


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


# Session-state suffixes holding the Jira project selection and its create-metadata.
# Named once because three separate events have to clear exactly this set: choosing
# a different site, re-querying the project list, and disconnecting.
_JIRA_PROJECT_SUFFIXES = ("projects", "projects_site", "project", "metadata", "metadata_for")


def _disconnect(provider) -> None:
    """Drop every trace of the provider session from this browser session."""
    suffixes = ("tokens", "handshake", "discovery", "transcript", "identity", "sites", "site")
    for suffix in suffixes + _JIRA_PROJECT_SUFFIXES:
        st.session_state.pop(_skey(provider.name, suffix), None)


def _clear_jira_project_state(service) -> None:
    """
    Forget the project list, the project selection and its metadata.

    The picker's own widget state goes too: a shorter new list would leave a
    stored index pointing past the end of it.
    """
    for suffix in _JIRA_PROJECT_SUFFIXES:
        st.session_state.pop(_skey(service.name, suffix), None)
    st.session_state.pop("select_jira_project", None)


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


def _render_jira_section() -> None:
    """
    The optional Jira Cloud connection, site selection, project selection and
    create-metadata check.

    Read-only throughout: it connects an account, lists the sites that account
    granted access to, lists the projects it can see on the chosen site, and reads
    what creating an issue there would require. There is no work plan and no issue
    creation here, so a connected session cannot change anything in Jira. It renders
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
        st.warning(
            "This Jira session was authorized before the app requested "
            + ", ".join("`{}`".format(scope) for scope in stale)
            + ". Atlassian grants scopes at consent time and a token refresh cannot add "
            "one, so the steps below will fail until you disconnect and connect again."
        )

    _render_jira_sites_panel(service, tokens)

    site = st.session_state.get(_skey(service.name, "site"))
    if site is not None:
        _render_jira_projects_panel(service, tokens, site)

    st.caption(
        "Authorized to read your Atlassian identity, the Jira sites you granted, and the "
        "projects and create-screen metadata on the site you select. Nothing has been "
        "created or changed in Jira, and no issue can be created without your explicit "
        "confirmation."
    )


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
    (
        "Manual Paste",
        "Upload Transcript File (.txt)",
        "Google Meet",
        "Microsoft Teams",
    ),
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
# it. Connection only -- nothing in this section can write to Jira.
_render_jira_section()


